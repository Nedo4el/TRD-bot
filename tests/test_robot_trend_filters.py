"""Фильтры входа robot_trend: боковик (ER), перегрев, филл, серия стопов."""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from pathlib import Path

import pytest

from core.indicators import efficiency_ratio
from robot_trend.state import PHASE_IN_POSITION, OpenPosition
from robot_trend.strategy import (
    Instruction,
    TrendParams,
    TrendStrategy,
    _HTFSnapshot,
    _Snapshot,
)
from tests.test_robot_trend import (
    _candles,
    _cfg,
    _enter_instruction,
    _make_flow,
)

# ============================== ER ==============================


def test_efficiency_ratio_trend_vs_chop() -> None:
    trend = [100.0 + i for i in range(40)]
    assert efficiency_ratio(trend, 36) > 0.95

    chop = [100.0 + (1.0 if i % 2 else 0.0) for i in range(40)]
    assert efficiency_ratio(chop, 36) < 0.1

    assert efficiency_ratio([100.0] * 10, 36) == 0.0
    assert efficiency_ratio(list(range(40)), 36) == pytest.approx(1.0)


# ============================== гейты стратегии ==============================


def _long_setup() -> tuple[_Snapshot, _HTFSnapshot]:
    ltf = _Snapshot(
        close=100.0,
        ema_fast=99.5,
        ema_slow=99.0,
        spread_pct=0.5,
        vol_ratio=2.0,
        pullback_long=True,
        pullback_short=False,
        er=0.5,
    )
    htf = _HTFSnapshot(
        trend_up=True,
        trend_down=False,
        adx=40.0,
        di_plus=30.0,
        di_minus=10.0,
        ema_fast=99.0,
        atr=2.0,
    )
    return ltf, htf


def test_entry_passes_with_filters_off() -> None:
    ltf, htf = _long_setup()
    assert TrendStrategy()._entry(ltf, htf) == "long"


def test_chop_filter_blocks_flat_market() -> None:
    ltf, htf = _long_setup()
    chop = TrendStrategy(TrendParams(chop_min_er=0.3))
    assert chop._entry(ltf, htf) == "long"
    assert chop._entry(replace(ltf, er=0.1), htf) is None


def test_overextension_filter_blocks_far_from_htf_ema() -> None:
    ltf, htf = _long_setup()
    near = TrendStrategy(TrendParams(max_ext_atr=1.0))
    assert near._entry(ltf, htf) == "long"
    # |100 - 99| / 2 = 0.5 ATR; порог 0.4 — уже перегрев
    far = TrendStrategy(TrendParams(max_ext_atr=0.4))
    assert far._entry(ltf, htf) is None


def test_params_validate_new_filters() -> None:
    with pytest.raises(ValueError, match="CHOP_PERIOD"):
        TrendParams(chop_period=3)
    with pytest.raises(ValueError, match="CHOP_MIN_ER"):
        TrendParams(chop_min_er=1.5)
    with pytest.raises(ValueError, match="MAX_EXT_ATR"):
        TrendParams(max_ext_atr=-1.0)


# ============================== order_flow ==============================


def test_entry_rejected_when_price_left_signal(tmp_path: Path) -> None:
    flow, client, _feed, strategy, store = _make_flow(tmp_path)
    client.price = 100.0
    strategy.instruction = Instruction(
        action="enter",
        side="long",
        stop=95.0,
        take=105.0,
        reason="test",
        ref_price=102.0,
        sl_pct=0.03,
        take_pct=0.10,
    )
    asyncio.run(flow._tick())
    assert client.placed == []
    assert store.state.position is None


def test_stop_and_take_rebuilt_from_fill(tmp_path: Path) -> None:
    flow, client, feed, strategy, store = _make_flow(tmp_path)
    client.price = 102.0
    feed.last_price = 102.0
    strategy.instruction = Instruction(
        action="enter",
        side="long",
        stop=94.0,
        take=112.0,
        reason="test",
        ref_price=102.0,
        sl_pct=0.03,
        take_pct=0.10,
    )
    asyncio.run(flow._tick())

    assert len(client.placed) == 1
    stop, take = client.sl_calls[0]
    assert stop == pytest.approx(102.0 * 0.97)
    assert take == pytest.approx(102.0 * 1.10)
    pos = store.state.position
    assert pos is not None
    assert pos.stop_loss == pytest.approx(102.0 * 0.97)
    assert pos.entry_price == 102.0


def test_reentry_lock_blocks_only_that_side(tmp_path: Path) -> None:
    flow, client, _feed, strategy, store = _make_flow(tmp_path)
    asyncio.run(flow._roll_day())
    store.state.stop_streak_side = "long"
    store.state.stop_streak = 2
    store.state.lock_until = time.time() + 600.0
    strategy.instruction = _enter_instruction()
    asyncio.run(flow._tick())
    assert client.placed == []

    strategy.instruction = Instruction(
        action="enter", side="short", stop=105.0, take=95.0, reason="test"
    )
    client.candles = _candles(6, offset=10)
    asyncio.run(flow._tick())
    assert len(client.placed) == 1


def test_two_stops_in_a_row_arm_lock(tmp_path: Path) -> None:
    flow, _client, _feed, _strategy, store = _make_flow(tmp_path)
    cfg = _cfg()
    assert store.state.lock_until == 0.0

    for expected in (1, 2):
        store.state.position = OpenPosition(
            entry_price=100.0,
            qty=0.1,
            side="long",
            stop_loss=97.0,
            opened_at=time.time(),
        )
        store.state.phase = PHASE_IN_POSITION
        asyncio.run(flow._finalize_close(-0.1, "server"))
        assert store.state.stop_streak == expected

    assert store.state.stop_streak_side == "long"
    assert store.state.lock_until > time.time() + cfg.reentry_lock_sec - 5.0


def test_win_resets_streak_for_that_side(tmp_path: Path) -> None:
    flow, _client, _feed, _strategy, store = _make_flow(tmp_path)
    store.state.stop_streak_side = "long"
    store.state.stop_streak = 1
    store.state.position = OpenPosition(
        entry_price=100.0, qty=0.1, side="long", stop_loss=97.0
    )
    store.state.phase = PHASE_IN_POSITION
    asyncio.run(flow._finalize_close(0.2, "take"))
    assert store.state.stop_streak == 0
    assert store.state.lock_until == 0.0
