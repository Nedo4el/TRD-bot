"""Новая стратегия robot_zakol: бракет-сетка 2/3/5%, отложенный стоп, BE, трейлинг."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from robot_zakol.backtest.backtest import _place, run_single
from robot_zakol.backtest.config import FillConfig, StrategyConfig
from robot_zakol.backtest.data_loader import Series, Tick
from robot_zakol.backtest.fill_model import FillModel
from robot_zakol.backtest.strategy import StrategyState
from robot_zakol.config import ZakolConfig


def _cfg(**overrides: Any) -> StrategyConfig:
    base: dict[str, Any] = {
        "grid_pcts": (0.02, 0.03, 0.05),
        "ttl_sec": 10.0,
        "stop_pct": 0.01,
        "stop_delay_sec": 10.0,
        "take_pct": 0.015,
        "trail_pct": 0.003,
        "be_trigger_pct": 0.0,
        "be_offset_pct": 0.005,
        "max_loss_usd": 0.0,
        "max_drawdown_pct": 0.0,
        "deposit_usd": 100.0,
        "position_pct": 10.0,
        "partial_fill_pct": 0.0,
        "tick_size": 0.01,
    }
    base.update(overrides)
    return StrategyConfig(**base)


def _ideal() -> FillConfig:
    return FillConfig(
        name="ideal",
        ideal=True,
        queue_factor=0.0,
        latency_ms=0.0,
        slippage_pct=0.0,
        post_only_strict=False,
        allow_partial=False,
    )


def _run(ticks: list[Tick], **overrides: Any) -> Any:
    series = Series(ticks=ticks, fidelity="1S", source_note="test")
    return run_single(series, _cfg(**overrides), _ideal(), "ideal")


def test_grid_places_three_levels_each_side() -> None:
    """Сетка: лонги -2/-3/-5%, шорты +2/+3/+5%, qty делится на 3."""
    strat = _cfg()
    fm = FillModel(_ideal(), strat.tick_size)
    st = StrategyState(strat, fm, strat.deposit_usd)
    _place(st, Tick(ts_ms=0, price=100.0, size=1.0), strat)

    assert st.phase == "WORKING"
    assert len(st.pending_buys) == 3
    assert len(st.pending_sells) == 3
    buys = sorted(o.target for o in st.pending_buys)
    sells = sorted(o.target for o in st.pending_sells)
    assert buys == pytest.approx([95.0, 97.0, 98.0])
    assert sells == pytest.approx([102.0, 103.0, 105.0])
    qty_each = (100.0 * 0.10) / 100.0 / 3  # notional $10 / цена 100 / 3
    assert st.pending_buys[0].qty == pytest.approx(qty_each)
    deadlines = {o.deadline_ts_ms for o in st.pendings()}
    assert deadlines == {10_000}


def test_grid_long_only_places_no_sells() -> None:
    strat = _cfg(long_only=True)
    fm = FillModel(_ideal(), strat.tick_size)
    st = StrategyState(strat, fm, strat.deposit_usd)
    _place(st, Tick(ts_ms=0, price=100.0, size=1.0), strat)
    assert len(st.pending_buys) == 3
    assert st.pending_sells == []


def test_grid_group_fill_averages_entry() -> None:
    """Резкий слив захватывает все три лонга одним тиком — вход по средней."""
    res = _run(
        [
            Tick(ts_ms=0, price=100.0, size=1.0),
            Tick(ts_ms=1000, price=94.0, size=1.0),
        ],
    )
    assert res.metrics.trades == 1
    t = res.trades[0]
    assert t.side == "long"
    assert t.entry_price == pytest.approx((98.0 + 97.0 + 95.0) / 3)
    assert t.qty == pytest.approx(0.1)


def test_first_fill_cancels_rest_of_grid() -> None:
    """После филла первого уровня остальные ордера сняты до тейка/стопа."""
    res = _run(
        [
            Tick(ts_ms=0, price=100.0, size=1.0),
            Tick(ts_ms=1000, price=97.5, size=1.0),  # филл только уровня 98
            Tick(ts_ms=2000, price=94.0, size=1.0),  # уровни 97/95 уже сняты
        ],
    )
    assert res.metrics.trades == 1
    assert res.trades[0].entry_price == pytest.approx(98.0)


def test_short_grid_fills_on_pump() -> None:
    res = _run(
        [
            Tick(ts_ms=0, price=100.0, size=1.0),
            Tick(ts_ms=1000, price=104.0, size=1.0),  # филл 102 и 103
        ],
    )
    assert res.metrics.trades == 1
    t = res.trades[0]
    assert t.side == "short"
    assert t.entry_price == pytest.approx((102.0 + 103.0) / 2)


def test_stop_placed_after_delay_from_current_price() -> None:
    """Стопа нет первые 10с (даже при сливе), потом 1% от текущей цены."""
    res = _run(
        [
            Tick(ts_ms=0, price=100.0, size=1.0),
            Tick(ts_ms=1000, price=96.0, size=1.0),  # вход long @97
            Tick(ts_ms=5000, price=80.0, size=1.0),  # слив без стопа — держим
            Tick(ts_ms=12000, price=110.0, size=1.0),  # стоп = 110*0.99
            Tick(ts_ms=13000, price=108.0, size=1.0),  # касание стопа
        ],
        grid_pcts=(0.03,),
        take_pct=0.0,
        trail_pct=0.0,
    )
    assert res.metrics.trades == 1
    t = res.trades[0]
    assert t.exit_ts == 13_000  # до 11000мс выхода не было
    assert t.exit_price == pytest.approx(110.0 * 0.99)
    assert t.pnl > 0


def test_be_moves_stop_after_trigger() -> None:
    """BE +0.5% работает до выставления стопа: вход → +0.5% → возврат → be."""
    res = _run(
        [
            Tick(ts_ms=0, price=100.0, size=1.0),
            Tick(ts_ms=1000, price=96.0, size=1.0),  # вход long @97
            Tick(ts_ms=3000, price=97.5, size=1.0),  # >=97*1.005 → BE
            Tick(ts_ms=4000, price=97.0, size=1.0),  # откат к BE-уровню
        ],
        grid_pcts=(0.03,),
        take_pct=0.0,
        trail_pct=0.0,
        be_trigger_pct=0.005,
    )
    assert res.metrics.trades == 1
    t = res.trades[0]
    assert t.reason == "be"
    assert t.exit_price == pytest.approx(97.0 * 1.005)


def test_take_zone_trailing_exit() -> None:
    """Тейк-зона 1.5%: выход трейлингом 0.3% от пика, без фикс. тейка."""
    res = _run(
        [
            Tick(ts_ms=0, price=100.0, size=1.0),
            Tick(ts_ms=1000, price=96.0, size=1.0),  # вход long @97
            Tick(ts_ms=3000, price=99.0, size=1.0),  # зона тейка, пик 99
            Tick(ts_ms=4000, price=98.6, size=1.0),  # откат через 99*0.997
        ],
        grid_pcts=(0.03,),
        be_trigger_pct=0.0,
    )
    assert res.metrics.trades == 1
    t = res.trades[0]
    assert t.reason == "trail"
    assert t.exit_price == pytest.approx(99.0 * 0.997)


def test_ttl_expires_and_replaces_grid() -> None:
    """Без филлов сетка переставляется каждые 10 секунд."""
    res = _run(
        [
            Tick(ts_ms=0, price=100.0, size=1.0),
            Tick(ts_ms=10_000, price=100.0, size=1.0),  # TTL истёк
            Tick(ts_ms=11_000, price=96.0, size=1.0),  # вход после re-place
        ],
        grid_pcts=(0.03,),
        take_pct=0.0,
        trail_pct=0.0,
    )
    assert res.metrics.ttl_cancels == 1
    assert res.metrics.trades == 1
    assert res.trades[0].entry_price == pytest.approx(97.0)


def test_grid_pcts_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GRID_PCTS", "0.01,0.02")
    monkeypatch.setenv("STOP_DELAY_SEC", "7")
    cfg = ZakolConfig(symbol="X")
    assert cfg.grid_pcts == (0.01, 0.02)
    assert cfg.stop_delay_sec == 7.0


def test_grid_pcts_validation() -> None:
    with pytest.raises(ValidationError, match="GRID_PCTS"):
        ZakolConfig(symbol="X", grid_pcts=(1.5,))
    with pytest.raises(ValidationError, match="секундах"):
        ZakolConfig(symbol="X", stop_delay_sec=-1.0)
