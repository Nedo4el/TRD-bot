"""Тесты robot_zakol: config, risk, state, order_cycle, fill_model, metrics."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.config import Config
from robot_zakol.backtest.backtest import run_single
from robot_zakol.backtest.config import FillConfig, StrategyConfig, mode_presets
from robot_zakol.backtest.data_loader import Candle, Series, Tick, candles_to_ticks
from robot_zakol.backtest.fill_model import (
    FillModel,
    PendingLimit,
    queue_fill_prob,
    should_post_only_reject,
)
from robot_zakol.backtest.metrics import build_metrics
from robot_zakol.backtest.strategy import (
    StrategyState,
    TradeLog,
    limit_target,
    update_position_risk,
)
from robot_zakol.config import ZakolConfig
from robot_zakol.risk import (
    fill_ratio,
    initial_stop,
    limit_buy_price,
    limit_sell_price,
    on_price,
    partial_fill_ok,
    should_be,
    should_kill,
    take_level,
    trail_level,
)
from robot_zakol.state import (
    PHASE_IDLE,
    PHASE_IN_POSITION,
    PHASE_STOPPED,
    PHASE_WORKING,
    OpenPosition,
    PendingOrder,
    StateStore,
)


def _cfg(**overrides: Any) -> ZakolConfig:
    base: dict[str, Any] = {
        "api_key": "k",
        "api_secret": "s",
        "symbol": "BTCUSDT",
        "fixed_qty": 0.001,
        "offset_pct": 0.03,
        "ttl_sec": 20.0,
        "min_price_change": 0.003,
        "stop_pct": 0.02,
        "trail_pct": 0.02,
        "be_trigger_pct": 0.005,
        "be_offset_pct": 0.002,
        "max_loss_usd": 10.0,
        "max_drawdown_pct": 0.05,
        "deposit_usd": 100.0,
        "partial_fill_pct": 0.80,
    }
    base.update(overrides)
    return ZakolConfig(**base)


def test_limit_buy_price_offset() -> None:
    assert limit_buy_price(100.0, 0.03) == pytest.approx(97.0)
    assert limit_sell_price(100.0, 0.03) == pytest.approx(103.0)


def test_limit_buy_price_tick() -> None:
    assert limit_buy_price(100.0, 0.03, tick=0.5) == 97.0
    assert limit_sell_price(100.0, 0.03, tick=0.5) == 103.0


def test_sl_and_take_levels() -> None:
    cfg = _cfg()
    assert initial_stop(100.0, 0.02) == 98.0
    assert initial_stop(100.0, 0.03, "short") == 103.0
    assert cfg.take_pct == 0.05
    assert take_level(100.0, 0.05) == pytest.approx(105.0)
    assert take_level(100.0, 0.05, "short") == pytest.approx(95.0)


def test_be_trigger() -> None:
    assert not should_be(100.0, 100.4, 0.005)
    assert should_be(100.0, 100.5, 0.005)


def test_trail_level() -> None:
    assert trail_level(105.0, 0.02) == pytest.approx(102.9)


def test_on_price_updates_peak_and_trail() -> None:
    cfg = _cfg()
    d = on_price(
        entry=100.0,
        peak=100.0,
        price=105.0,
        current_stop=98.0,
        cfg=cfg,
        be_active=False,
    )
    assert d.be_active
    assert d.trail_stop == pytest.approx(102.9)
    assert d.stop_loss == pytest.approx(102.9)
    assert d.update_server


def test_on_price_be_offset() -> None:
    cfg = _cfg()
    d = on_price(
        entry=100.0,
        peak=100.5,
        price=100.5,
        current_stop=98.0,
        cfg=cfg,
        be_active=False,
    )
    assert d.be_active
    assert d.stop_loss == pytest.approx(100.2)


def test_kill_by_drawdown_and_max_loss() -> None:
    assert not should_kill(-4.0, 0.0, 100.0, 10.0, 0.05)
    assert should_kill(-5.0, 0.0, 100.0, 10.0, 0.05)
    assert should_kill(0.0, 6.0, 100.0, 10.0, 0.05)
    assert should_kill(-10.0, 0.0, 100.0, 10.0, 0.05)
    assert not should_kill(-4.9, 0.0, 100.0, 10.0, 0.05)


def test_kill_disabled_when_zero() -> None:
    assert not should_kill(-100.0, 0.0, 100.0, 0.0, 0.0)
    assert not should_kill(-100.0, 500.0, 100.0, 0.0, 0.0)


def test_partial_fill() -> None:
    assert partial_fill_ok(0.8, 1.0, 0.80)
    assert not partial_fill_ok(0.79, 1.0, 0.80)
    assert fill_ratio(0.5, 0.0) == 0.0


def test_config_defaults_and_validation() -> None:
    cfg = _cfg()
    assert cfg.offset_pct == 0.03
    assert cfg.min_price_change == 0.003
    assert cfg.trail_pct == 0.02
    with pytest.raises(ValueError):
        ZakolConfig(symbol="X", offset_pct=1.5)
    with pytest.raises(ValueError):
        ZakolConfig(symbol="X", ttl_sec=0)


def test_state_store_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "s.json"
    store = StateStore(path)
    store.state.phase = PHASE_WORKING
    store.state.pending_buy = PendingOrder(
        order_id="1",
        order_link_id="l",
        price=97.0,
        qty=0.001,
        placed_at=1.0,
        side="Buy",
    )
    store.state.position = OpenPosition(
        entry_price=97.0,
        qty=0.001,
        peak_price=98.0,
        stop_loss=95.06,
        side="short",
    )
    store.state.peak_session_pnl = -2.0
    store.state.session_pnl = -3.5
    loop = asyncio.new_event_loop()
    loop.run_until_complete(store.save())
    loop.close()
    store2 = StateStore(path)
    assert store2.state.phase == PHASE_WORKING
    assert store2.state.pending_buy is not None
    assert store2.state.pending_buy.order_id == "1"
    assert store2.state.pending_buy.side == "Buy"
    assert store2.state.position is not None
    assert store2.state.position.entry_price == 97.0
    assert store2.state.position.side == "short"
    assert store2.state.peak_session_pnl == -2.0
    assert store2.state.session_pnl == -3.5


def test_state_corrupt_file(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    store = StateStore(path)
    assert store.state.phase == PHASE_IDLE
    assert store.state.position is None


class FakeClient:
    def __init__(self) -> None:
        self.placed: list[dict[str, Any]] = []
        self.cancelled: list[str] = []
        self.cancel_all = 0
        self.sl_calls: list[tuple[float, float | None]] = []
        self.orders: list[dict[str, Any]] = []
        self.pos: Any = None
        self.price = 100.0
        self.open_orders_calls = 0
        self.order_reads = 0
        self.closed_pnl: float | None = None
        self.order_fills: dict[str, float] = {}
        self.fail_cancel: set[str] = set()
        self.fail_on_place: int | None = None  # номер вызова place_order (1-based)
        self.balance = 0.0
        self.balance_calls = 0

    async def get_balance(self) -> float:
        self.balance_calls += 1
        return self.balance

    async def place_order(self, **kwargs: Any) -> dict[str, Any]:
        if self.fail_on_place == len(self.placed) + 1:
            raise RuntimeError("place rejected")
        self.placed.append(kwargs)
        return {"result": {"orderId": f"oid-{len(self.placed)}"}}

    async def cancel_order(self, symbol: str, order_id: str) -> dict[str, Any]:
        if order_id in self.fail_cancel:
            raise RuntimeError("cancel rejected")
        self.cancelled.append(order_id)
        return {}

    async def cancel_all_orders(self, symbol: str) -> dict[str, Any]:
        self.cancel_all += 1
        return {}

    async def get_open_orders(self, symbol: str) -> list[dict[str, Any]]:
        self.open_orders_calls += 1
        return self.orders

    async def get_order(self, symbol: str, order_id: str) -> dict[str, Any] | None:
        self.order_reads += 1
        if order_id not in self.order_fills:
            return None
        return {
            "orderId": order_id,
            "cumExecQty": self.order_fills[order_id],
            "orderStatus": "Filled",
        }

    async def get_last_closed_pnl(self, symbol: str) -> float | None:
        return self.closed_pnl

    async def get_position(self, symbol: str) -> Any:
        return self.pos

    async def get_price(self, symbol: str) -> float:
        return self.price

    async def set_stop_loss_take_profit(self, **kwargs: Any) -> dict[str, Any]:
        self.sl_calls.append(
            (kwargs["stop_loss"], kwargs.get("take_profit")),
        )
        return {}


class FakeFeed:
    def __init__(self) -> None:
        self.prices: asyncio.Queue[float] = asyncio.Queue()
        self.order_events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.exec_events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.last_price = 100.0
        self.last_balance = 0.0
        self.resync_needed = False


def _make_cycle(
    tmp_path: Path,
    client: FakeClient,
    qty_step: float = 0.0,
    notifier: Any = None,
    cfg: ZakolConfig | None = None,
) -> Any:
    from robot_zakol.order_cycle import OrderCycle

    cfg = cfg or _cfg()
    store = StateStore(tmp_path / "st.json")
    feed = FakeFeed()
    return OrderCycle(
        cfg=cfg,
        client=client,
        feed=feed,
        store=store,
        tick_size=0.5,
        qty_step=qty_step,
        notifier=notifier,
    )


def test_place_bracket_sets_working(tmp_path: Path) -> None:
    client = FakeClient()
    cycle = _make_cycle(tmp_path, client)

    async def run() -> None:
        await cycle._place_bracket()

    asyncio.run(run())
    st = cycle.store.state
    assert st.phase == PHASE_WORKING
    assert st.pending_buy is not None
    assert st.pending_sell is not None
    assert len(client.placed) == 2
    sides = [o["side"] for o in client.placed]
    assert sides == ["Buy", "Sell"]
    assert all(o["post_only"] is True for o in client.placed)
    assert client.placed[0]["price"] == pytest.approx(97.0)
    assert client.placed[1]["price"] == pytest.approx(103.0)
    assert all(o["order_link_id"] for o in client.placed)


def test_partial_fill_opens_position_with_sl(tmp_path: Path) -> None:
    """Частичный филл ниже порога = позиция со SL (как в бэктесте)."""
    client = FakeClient()
    cycle = _make_cycle(tmp_path, client)

    async def run() -> None:
        cycle.store.state.pending_buy = PendingOrder(
            order_id="oid-1",
            order_link_id="l",
            price=97.0,
            qty=0.001,
            placed_at=0.0,
            filled_qty=0.0005,
            side="Buy",
        )
        await cycle._on_filled(cycle.store.state.pending_buy)

    asyncio.run(run())
    st = cycle.store.state
    assert st.phase == PHASE_IN_POSITION
    assert st.position is not None
    assert st.position.qty == pytest.approx(0.0005)
    assert st.position.stop_loss == pytest.approx(97.0 * 0.98)
    assert client.sl_calls


def test_on_filled_zero_fill_rejected(tmp_path: Path) -> None:
    client = FakeClient()
    cycle = _make_cycle(tmp_path, client)

    async def run() -> None:
        cycle.store.state.pending_buy = PendingOrder(
            order_id="oid-1",
            order_link_id="l",
            price=97.0,
            qty=0.001,
            placed_at=0.0,
            filled_qty=0.0,
            side="Buy",
        )
        await cycle._on_filled(cycle.store.state.pending_buy)

    asyncio.run(run())
    assert cycle.store.state.phase == PHASE_IDLE
    assert cycle.store.state.position is None


def test_on_filled_long_opens_with_sl_and_tp(tmp_path: Path) -> None:
    client = FakeClient()
    cycle = _make_cycle(tmp_path, client)

    async def run() -> None:
        cycle.store.state.pending_buy = PendingOrder(
            order_id="oid-1",
            order_link_id="l",
            price=97.0,
            qty=0.001,
            placed_at=0.0,
            filled_qty=0.001,
            side="Buy",
        )
        await cycle._on_filled(cycle.store.state.pending_buy)

    asyncio.run(run())
    st = cycle.store.state
    assert st.phase == PHASE_IN_POSITION
    assert st.position is not None
    assert st.position.side == "long"
    assert st.position.entry_price == 97.0
    assert st.position.stop_loss == pytest.approx(97.0 * 0.98)
    assert client.sl_calls
    sl, take = client.sl_calls[0]
    assert sl == pytest.approx(97.0 * 0.98)
    assert take == pytest.approx(97.0 * 1.05)


def test_on_filled_short_cancels_other_side(tmp_path: Path) -> None:
    client = FakeClient()
    cycle = _make_cycle(tmp_path, client)

    async def run() -> None:
        cycle.store.state.pending_sell = PendingOrder(
            order_id="oid-s",
            order_link_id="ls",
            price=103.0,
            qty=0.001,
            placed_at=0.0,
            filled_qty=0.001,
            side="Sell",
        )
        cycle.store.state.pending_buy = PendingOrder(
            order_id="oid-b",
            order_link_id="lb",
            price=97.0,
            qty=0.001,
            placed_at=0.0,
            filled_qty=0.0,
            side="Buy",
        )
        await cycle._on_filled(cycle.store.state.pending_sell)

    asyncio.run(run())
    st = cycle.store.state
    assert st.phase == PHASE_IN_POSITION
    assert st.position is not None
    assert st.position.side == "short"
    assert st.position.stop_loss == pytest.approx(103.0 * 1.02)
    assert st.pending_buy is None
    assert st.pending_sell is None
    assert "oid-b" in client.cancelled
    sl, take = client.sl_calls[0]
    assert sl == pytest.approx(103.0 * 1.02)
    assert take == pytest.approx(103.0 * 0.95)


def test_kill_after_max_drawdown(tmp_path: Path) -> None:
    client = FakeClient()
    cycle = _make_cycle(tmp_path, client)

    async def run() -> None:
        cycle.store.state.position = OpenPosition(
            entry_price=97.0,
            qty=0.001,
            peak_price=97.0,
            stop_loss=95.06,
        )
        cycle.store.state.session_pnl = 0.0
        cycle.store.state.peak_session_pnl = 5.0
        client.pos = None
        cycle.feed.last_price = 96.0
        client.price = 96.0
        await cycle._close_position(reason="test")

    asyncio.run(run())
    assert cycle.store.state.kill is True
    assert cycle.store.state.phase == PHASE_IDLE


def test_recover_pending_gone(tmp_path: Path) -> None:
    client = FakeClient()
    cycle = _make_cycle(tmp_path, client)
    client.orders = []

    async def run() -> None:
        cycle.store.state.phase = PHASE_WORKING
        cycle.store.state.pending_buy = PendingOrder(
            order_id="missing",
            order_link_id="l",
            price=97.0,
            qty=0.001,
            placed_at=0.0,
        )
        await cycle.recover()

    asyncio.run(run())
    assert cycle.store.state.phase == PHASE_IDLE
    assert cycle.store.state.pending_buy is None


def test_mode_presets_three() -> None:
    presets = mode_presets(100.0)
    assert set(presets) == {"ideal", "realistic", "pessimistic"}
    ideal = presets["ideal"][1]
    assert ideal.ideal and ideal.slippage_pct == 0.0
    pess = presets["pessimistic"][1]
    assert pess.queue_factor == 2.0 and pess.slippage_pct == 0.001


def test_queue_fill_prob_formula() -> None:
    assert queue_fill_prob(10.0, 10.0) == pytest.approx(0.5)
    assert queue_fill_prob(1.0, 0.0) == pytest.approx(1.0)


def test_post_only_gap_reject() -> None:
    assert should_post_only_reject(100.0, 96.0, 97.0, 0.1, True)
    assert not should_post_only_reject(100.0, 97.5, 97.0, 0.1, True)
    assert not should_post_only_reject(100.0, 96.0, 97.0, 0.1, False)


def test_fill_model_ideal_full() -> None:
    fm = FillModel(
        FillConfig(name="ideal", ideal=True, queue_factor=0, latency_ms=0),
        tick_size=0.1,
    )
    order = PendingLimit(target=97.0, qty=1.0, placed_ts_ms=0, deadline_ts_ms=20000)
    out = fm.on_tick(order, 96.5, 1.0, 98.0, 100)
    assert out.filled_qty == 1.0
    assert out.fill_price == 97.0


def test_fill_model_post_only_reject_on_arrive() -> None:
    fm = FillModel(
        FillConfig(
            name="r",
            post_only_strict=True,
            queue_factor=1.0,
            latency_ms=100.0,
        ),
        tick_size=0.1,
    )
    order = PendingLimit(
        target=97.0,
        qty=1.0,
        placed_ts_ms=0,
        deadline_ts_ms=20000,
    )
    in_flight = fm.on_tick(order, 98.0, 1.0, 100.0, 50)
    assert in_flight.in_flight
    out = fm.on_tick(order, 96.0, 1.0, 98.0, 150)
    assert out.post_only_reject


def test_fill_model_realistic_touch_then_fill() -> None:
    fm = FillModel(
        FillConfig(name="r", post_only_strict=True, queue_factor=1.0, latency_ms=0),
        tick_size=0.1,
    )
    order = PendingLimit(
        target=97.0,
        qty=1.0,
        placed_ts_ms=0,
        deadline_ts_ms=20000,
    )
    out = fm.on_tick(order, 97.0, 10.0, 98.0, 10)
    assert out.filled_qty > 0 or out.queue_reject or out.partial


def test_limit_target() -> None:
    assert limit_target(100.0, 0.03, 0.01) == pytest.approx(97.0)
    assert limit_target(100.0, 0.03, 0.01, "Sell") == pytest.approx(103.0)


def test_fill_model_sell_ideal() -> None:
    fm = FillModel(
        FillConfig(name="ideal", ideal=True, queue_factor=0, latency_ms=0),
        tick_size=0.1,
    )
    order = PendingLimit(
        target=103.0,
        qty=1.0,
        placed_ts_ms=0,
        deadline_ts_ms=20000,
        side="Sell",
    )
    out = fm.on_tick(order, 103.5, 1.0, 101.0, 100)
    assert out.filled_qty == 1.0
    assert out.fill_price == 103.0


def test_fill_model_sell_post_only_reject() -> None:
    fm = FillModel(
        FillConfig(name="r", post_only_strict=True, queue_factor=1.0, latency_ms=0),
        tick_size=0.1,
    )
    order = PendingLimit(
        target=103.0,
        qty=1.0,
        placed_ts_ms=0,
        deadline_ts_ms=20000,
        side="Sell",
    )
    out = fm.on_tick(order, 104.0, 1.0, 101.0, 100)
    assert out.post_only_reject


def test_fill_model_sell_no_fill_below_target() -> None:
    fm = FillModel(
        FillConfig(name="ideal", ideal=True, queue_factor=0, latency_ms=0),
        tick_size=0.1,
    )
    order = PendingLimit(
        target=103.0,
        qty=1.0,
        placed_ts_ms=0,
        deadline_ts_ms=20000,
        side="Sell",
    )
    out = fm.on_tick(order, 100.0, 1.0, 101.0, 100)
    assert out.filled_qty == 0.0


def test_fill_model_buy_no_fill_above_target_realistic() -> None:
    fm = FillModel(
        FillConfig(name="r", post_only_strict=True, queue_factor=1.0, latency_ms=0),
        tick_size=0.1,
    )
    order = PendingLimit(
        target=97.0,
        qty=1.0,
        placed_ts_ms=0,
        deadline_ts_ms=20000,
        side="Buy",
    )
    out = fm.on_tick(order, 100.0, 1.0, 101.0, 100)
    assert out.filled_qty == 0.0
    assert not out.queue_reject
    assert not out.post_only_reject


def test_fill_model_sell_no_fill_below_target_realistic() -> None:
    fm = FillModel(
        FillConfig(name="r", post_only_strict=True, queue_factor=1.0, latency_ms=0),
        tick_size=0.1,
    )
    order = PendingLimit(
        target=103.0,
        qty=1.0,
        placed_ts_ms=0,
        deadline_ts_ms=20000,
        side="Sell",
    )
    out = fm.on_tick(order, 100.0, 1.0, 99.0, 100)
    assert out.filled_qty == 0.0
    assert not out.queue_reject


def test_update_position_risk_trail_exit() -> None:
    cfg = StrategyConfig()
    from robot_zakol.backtest.strategy import Position

    pos = Position(
        entry_price=100.0,
        qty=1.0,
        peak=105.0,
        stop=103.0,
        be_active=True,
    )
    _need, reason = update_position_risk(pos, 102.8, cfg)
    assert reason == "trail"
    assert pos.stop <= 103.0


def test_metrics_warnings_trail_dominates() -> None:
    cfg = StrategyConfig()
    fm = FillModel(FillConfig(ideal=True), 0.01)
    st = StrategyState(cfg, fm, 100.0)
    for i in range(10):
        st.trades.append(
            TradeLog(
                entry_ts=i,
                entry_price=100.0,
                exit_ts=i + 1,
                exit_price=102.0,
                reason="trail",
                pnl=1.0,
                pnl_pct=2.0,
                slippage=0.0,
                was_partial=False,
                qty=1.0,
                hold_ms=1000,
            )
        )
    series = Series(ticks=[], fidelity="1M", source_note="x")
    m = build_metrics(st, 100.0, series)
    assert m.trades == 10
    assert m.win_rate == 1.0
    assert m.exit_trail_pct == 1.0
    assert m.sharpe > 0
    assert m.profit_factor >= 999.0


def test_candles_to_ticks_count() -> None:
    candles = [
        Candle(
            open_time=i * 60000,
            open=1.0,
            high=1.1,
            low=0.9,
            close=1.05,
            volume=10.0,
        )
        for i in range(3)
    ]
    ticks = candles_to_ticks(candles, "1")
    assert len(ticks) == 3 * 60
    assert ticks[0].price == 1.0


def test_update_position_risk_be_lifts_stop() -> None:
    cfg = StrategyConfig()
    from robot_zakol.backtest.strategy import Position

    pos = Position(
        entry_price=100.0,
        qty=1.0,
        peak=100.0,
        stop=98.0,
        be_active=False,
    )
    need, reason = update_position_risk(pos, 100.5, cfg)
    assert pos.be_active
    assert pos.stop == pytest.approx(100.0 * (1.0 + cfg.be_offset_pct))
    assert reason is None
    assert need


def test_on_price_short_mirrored() -> None:
    cfg = _cfg()
    d = on_price(
        entry=100.0,
        peak=100.0,
        price=95.0,
        current_stop=102.0,
        cfg=cfg,
        be_active=False,
        side="short",
    )
    assert d.be_active
    assert d.trail_stop == pytest.approx(96.9)
    assert d.stop_loss == pytest.approx(96.9)
    assert d.update_server


def test_on_price_trail_disabled_when_zero() -> None:
    cfg = _cfg(trail_pct=0.0, be_trigger_pct=0.0, stop_pct=0.03)
    d = on_price(
        entry=100.0,
        peak=100.0,
        price=105.0,
        current_stop=97.0,
        cfg=cfg,
        be_active=False,
    )
    assert not d.be_active
    assert d.trail_stop is None
    assert d.stop_loss == pytest.approx(97.0)
    assert d.update_server  # сместился peak (не trail)


def _bt_cfg(**overrides: Any) -> StrategyConfig:
    base: dict[str, Any] = {
        "offset_pct": 0.03,
        "ttl_sec": 20.0,
        "stop_pct": 0.03,
        "take_pct": 0.05,
        "trail_pct": 0.0,
        "be_trigger_pct": 0.0,
        "max_loss_usd": 0.0,
        "max_drawdown_pct": 0.0,
        "deposit_usd": 100.0,
        "position_pct": 10.0,
        "partial_fill_pct": 0.0,
        "tick_size": 0.01,
    }
    base.update(overrides)
    return StrategyConfig(**base)


def _ideal_fill() -> FillConfig:
    return FillConfig(
        name="ideal",
        ideal=True,
        queue_factor=0.0,
        latency_ms=0.0,
        slippage_pct=0.0,
        post_only_strict=False,
        allow_partial=False,
    )


def test_backtest_long_fills_and_takes_profit() -> None:
    ticks = [
        Tick(ts_ms=0, price=100.0, size=1.0),
        Tick(ts_ms=1000, price=96.0, size=1.0),
        Tick(ts_ms=2000, price=102.0, size=1.0),
    ]
    series = Series(ticks=ticks, fidelity="1S", source_note="synthetic")
    res = run_single(series, _bt_cfg(), _ideal_fill(), "ideal")
    assert res.metrics.trades == 1
    t = res.trades[0]
    assert t.side == "long"
    assert t.reason == "tp"
    assert t.entry_price == pytest.approx(97.0)
    assert t.exit_price == pytest.approx(101.85)
    assert t.pnl > 0


def test_backtest_short_fills_and_takes_profit() -> None:
    ticks = [
        Tick(ts_ms=0, price=100.0, size=1.0),
        Tick(ts_ms=1000, price=104.0, size=1.0),
        Tick(ts_ms=2000, price=96.0, size=1.0),
    ]
    series = Series(ticks=ticks, fidelity="1S", source_note="synthetic")
    res = run_single(series, _bt_cfg(), _ideal_fill(), "ideal")
    assert res.metrics.trades == 1
    t = res.trades[0]
    assert t.side == "short"
    assert t.reason == "tp"
    assert t.entry_price == pytest.approx(103.0)
    assert t.exit_price == pytest.approx(97.85)
    assert t.pnl > 0


def test_backtest_long_stop_loss() -> None:
    ticks = [
        Tick(ts_ms=0, price=100.0, size=1.0),
        Tick(ts_ms=1000, price=96.0, size=1.0),
        Tick(ts_ms=2000, price=93.0, size=1.0),
    ]
    series = Series(ticks=ticks, fidelity="1S", source_note="synthetic")
    res = run_single(series, _bt_cfg(), _ideal_fill(), "ideal")
    assert res.metrics.trades == 1
    t = res.trades[0]
    assert t.reason == "sl"
    assert t.pnl < 0


# =========================================================================
#  Live: блокеры и защита позиции (сессия 2026-09-26)
# =========================================================================


class FakeNotifier:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def notify(self, text: str) -> None:
        self.messages.append(text)


def _pending(
    order_id: str, side: str, price: float, filled: float = 0.0
) -> PendingOrder:
    return PendingOrder(
        order_id=order_id,
        order_link_id=f"l-{order_id}",
        price=price,
        qty=0.001,
        placed_at=time.monotonic(),
        filled_qty=filled,
        side=side,
    )


# --- а.1 MAX_CONCURRENT -------------------------------------------------


def test_validate_for_live_max_concurrent_zero_raises() -> None:
    cfg = _cfg(max_concurrent=0)
    with pytest.raises(ValueError, match="MAX_CONCURRENT"):
        cfg.validate_for_live()


def test_validate_for_live_max_concurrent_one_ok() -> None:
    cfg = _cfg(max_concurrent=1)
    cfg.validate_for_live()


def test_position_pct_must_be_in_range() -> None:
    with pytest.raises(ValueError, match="POSITION_PCT"):
        _cfg(position_pct=0.0)
    with pytest.raises(ValueError, match="POSITION_PCT"):
        _cfg(position_pct=150.0)
    assert _cfg(position_pct=1.0).position_pct == 1.0
    assert _cfg(position_pct=100.0).position_pct == 100.0


# --- а.2 утечка waiter-задач WS -----------------------------------------


def test_await_working_does_not_leak_tasks(tmp_path: Path) -> None:
    client = FakeClient()
    cycle = _make_cycle(tmp_path, client)

    async def run() -> None:
        cycle.store.state.phase = PHASE_WORKING
        cycle.store.state.pending_buy = _pending("oid-1", "Buy", 97.0)
        baseline = len(asyncio.all_tasks())
        for _ in range(10):
            deadline = time.monotonic() + 0.02
            await asyncio.wait_for(
                cycle._watch_until_fill_or_deadline(deadline),
                timeout=1.0,
            )
        await asyncio.sleep(0)
        assert len(asyncio.all_tasks()) == baseline

    asyncio.run(run())


def test_fill_after_idle_iteration_is_delivered(tmp_path: Path) -> None:
    client = FakeClient()
    cycle = _make_cycle(tmp_path, client)

    async def run() -> tuple[PendingOrder, Any]:
        cycle.store.state.phase = PHASE_WORKING
        pending = _pending("oid-1", "Buy", 97.0)
        cycle.store.state.pending_buy = pending
        watch = asyncio.create_task(
            cycle._watch_until_fill_or_deadline(time.monotonic() + 3.0)
        )
        await asyncio.sleep(0.7)  # больше одной паузы 0.5с — старый waiter утек
        cycle.feed.order_events.put_nowait(
            {
                "data": {
                    "orderId": "oid-1",
                    "orderStatus": "Filled",
                    "cumExecQty": 0.001,
                }
            }
        )
        await asyncio.sleep(0.1)
        watch.cancel()
        await asyncio.gather(watch, return_exceptions=True)
        await asyncio.sleep(0.05)
        return pending, cycle.store.state

    pending, state = asyncio.run(run())
    assert pending.filled_qty == pytest.approx(0.001)
    assert state.phase == PHASE_IN_POSITION
    assert state.position is not None


# --- а.5 try/finally + save после первого ордера ------------------------


def test_run_calls_on_shutdown_on_exception(tmp_path: Path) -> None:
    client = FakeClient()
    client.fail_on_place = 1
    cycle = _make_cycle(tmp_path, client)

    async def run() -> None:
        cycle.store.state.phase = PHASE_IDLE
        cycle.store.state.pending_buy = _pending("oid-keep", "Buy", 97.0)
        with pytest.raises(RuntimeError, match="place rejected"):
            await cycle.run()
        await asyncio.sleep(0.01)

    asyncio.run(run())
    assert "oid-keep" in client.cancelled
    assert cycle.store.state.pending_buy is None
    assert cycle.store.state.pending_sell is None


def test_place_bracket_saves_state_after_first_order(tmp_path: Path) -> None:
    client = FakeClient()
    client.fail_on_place = 2
    cycle = _make_cycle(tmp_path, client)

    async def run() -> None:
        with pytest.raises(RuntimeError, match="place rejected"):
            await cycle._place_bracket()

    asyncio.run(run())
    st = cycle.store.state
    assert st.pending_buy is not None
    assert st.pending_buy.order_id == "oid-1"
    assert st.pending_sell is None
    raw = json.loads((tmp_path / "st.json").read_text(encoding="utf-8"))
    assert raw["pending_buy"]["order_id"] == "oid-1"


# --- а.3 REST-фолбэк статуса ордера -------------------------------------


def test_on_ttl_uses_rest_fallback_when_ws_silent(tmp_path: Path) -> None:
    client = FakeClient()
    client.order_fills = {"oid-1": 0.001}
    cycle = _make_cycle(tmp_path, client)

    async def run() -> None:
        cycle.store.state.phase = PHASE_WORKING
        cycle.store.state.pending_buy = _pending("oid-1", "Buy", 97.0)
        await cycle._on_ttl()

    asyncio.run(run())
    st = cycle.store.state
    assert client.order_reads == 1
    assert st.phase == PHASE_IN_POSITION
    assert st.position is not None
    assert st.position.qty == pytest.approx(0.001)
    assert "oid-1" in client.cancelled


def test_await_working_detects_fill_via_rest(tmp_path: Path) -> None:
    client = FakeClient()
    client.order_fills = {"oid-1": 0.001}
    cycle = _make_cycle(tmp_path, client)

    async def run() -> None:
        pending = _pending("oid-1", "Buy", 97.0)
        pending.placed_at = time.monotonic() - 100.0  # TTL уже истёк
        cycle.store.state.phase = PHASE_WORKING
        cycle.store.state.pending_buy = pending
        await cycle._await_working()

    asyncio.run(run())
    assert cycle.store.state.phase == PHASE_IN_POSITION


def test_ttl_replace_cancels_both_and_goes_idle(tmp_path: Path) -> None:
    client = FakeClient()
    cycle = _make_cycle(tmp_path, client)
    cycle.feed.last_price = 105.0  # F.3: delta 5% >= MIN_PRICE_CHANGE -> re-place

    async def run() -> None:
        cycle.store.state.phase = PHASE_WORKING
        cycle.store.state.pending_buy = _pending("oid-1", "Buy", 97.0)
        cycle.store.state.pending_sell = _pending("oid-2", "Sell", 103.0)
        await cycle._on_ttl()

    asyncio.run(run())
    assert client.order_reads == 2
    assert set(client.cancelled) == {"oid-1", "oid-2"}
    assert cycle.store.state.phase == PHASE_IDLE


def test_ttl_extends_when_price_quiet(tmp_path: Path) -> None:
    """F.3: цена не сдвинулась -> лимитки живут, TTL продлевается."""
    client = FakeClient()
    cycle = _make_cycle(tmp_path, client)

    async def run() -> None:
        cycle.store.state.phase = PHASE_WORKING
        buy = _pending("oid-1", "Buy", 97.0)
        buy.placed_at = time.monotonic() - 100.0
        cycle.store.state.pending_buy = buy
        cycle.store.state.pending_sell = _pending("oid-2", "Sell", 103.0)
        await cycle._on_ttl()

    asyncio.run(run())
    st = cycle.store.state
    assert st.phase == PHASE_WORKING
    assert st.pending_buy is not None
    assert st.pending_buy.placed_at > time.monotonic() - 5.0
    assert client.cancelled == []


# --- б.3 reduceOnly на закрывающем ордере -------------------------------


def _bracket_with_failed_cancel(tmp_path: Path, client: FakeClient) -> Any:
    client.fail_cancel = {"oid-s"}
    client.orders = [{"orderId": "oid-s"}]
    cycle = _make_cycle(tmp_path, client)

    async def run() -> None:
        cycle.store.state.phase = PHASE_WORKING
        cycle.store.state.pending_buy = _pending("oid-b", "Buy", 97.0, filled=0.001)
        cycle.store.state.pending_sell = _pending("oid-s", "Sell", 103.0)
        await cycle._on_filled(cycle.store.state.pending_buy)

    asyncio.run(run())
    return cycle


def test_closing_order_is_reduce_only(tmp_path: Path) -> None:
    client = FakeClient()
    _bracket_with_failed_cancel(tmp_path, client)
    guards = [o for o in client.placed if o.get("reduce_only")]
    assert len(guards) == 1
    assert guards[0]["side"] == "Sell"
    assert guards[0]["price"] == pytest.approx(103.0)


def test_closing_order_does_not_flip_position(tmp_path: Path) -> None:
    client = FakeClient()
    cycle = _bracket_with_failed_cancel(tmp_path, client)
    st = cycle.store.state
    assert st.phase == PHASE_IN_POSITION
    assert st.position is not None
    assert st.position.side == "long"
    # единственная новая лимитка — reduceOnly, открыть шорт она не может
    assert all(o.get("reduce_only") for o in client.placed)
    assert st.pending_buy is None  # исполненная сторона
    # D.4: неснятая сторона остаётся в state — recover/shutdown её снимут
    assert st.pending_sell is not None
    assert st.pending_sell.order_id == "oid-s"


# --- б.4 partial fill → позиция со SL -----------------------------------


def test_ttl_partial_fill_opens_position_like_backtest(tmp_path: Path) -> None:
    client = FakeClient()
    cycle = _make_cycle(tmp_path, client)

    async def run() -> None:
        pending = _pending("oid-1", "Buy", 97.0, filled=0.0005)
        pending.placed_at = time.monotonic() - 100.0
        cycle.store.state.phase = PHASE_WORKING
        cycle.store.state.pending_buy = pending
        await cycle._await_working()

    asyncio.run(run())
    st = cycle.store.state
    assert st.phase == PHASE_IN_POSITION
    assert st.position is not None
    assert st.position.qty == pytest.approx(0.0005)
    assert client.sl_calls


# --- б.5/b.6 kill, SL/TP, recover ---------------------------------------


def test_enter_stopped_keeps_sl_tp(tmp_path: Path) -> None:
    client = FakeClient()
    cycle = _make_cycle(tmp_path, client)

    async def run() -> None:
        cycle.store.state.kill = True
        cycle.store.state.position = OpenPosition(
            entry_price=97.0,
            qty=0.001,
            peak_price=97.0,
            stop_loss=95.06,
        )
        cycle.store.state.pending_buy = _pending("oid-1", "Buy", 97.0)
        await cycle._enter_stopped()

    asyncio.run(run())
    assert client.cancel_all == 0  # cancel_all_orders снял бы SL/TP
    assert "oid-1" in client.cancelled
    assert client.sl_calls  # стоп переустановлен
    assert cycle.store.state.phase == PHASE_STOPPED


def test_recover_respects_kill_flag(tmp_path: Path) -> None:
    client = FakeClient()
    client.pos = SimpleNamespace(
        size=0.001, side="Buy", avg_price=97.0, unrealised_pnl=0.0
    )
    cycle = _make_cycle(tmp_path, client)

    async def run() -> None:
        cycle.store.state.kill = True
        await cycle.recover()

    asyncio.run(run())
    st = cycle.store.state
    assert st.phase == PHASE_STOPPED
    assert st.position is not None
    assert client.sl_calls


def test_kill_triggered_by_drawdown_in_background(tmp_path: Path) -> None:
    client = FakeClient()
    client.pos = SimpleNamespace(
        size=0.001, side="Buy", avg_price=97.0, unrealised_pnl=-6.0
    )
    cycle = _make_cycle(tmp_path, client)

    async def run() -> None:
        cycle.store.state.position = OpenPosition(
            entry_price=97.0, qty=0.001, peak_price=97.0, stop_loss=95.06
        )
        cycle.store.state.phase = PHASE_IN_POSITION
        cycle.store.state.session_pnl = 0.0
        cycle.store.state.peak_session_pnl = 0.0
        cycle.feed.prices.put_nowait(96.0)
        await cycle._manage_position()

    asyncio.run(run())
    st = cycle.store.state
    assert st.kill is True
    assert st.position is not None  # позиция не закрыта, просто стоп торговать
    assert st.phase == PHASE_IN_POSITION


def test_recover_applies_server_sl_tp(tmp_path: Path) -> None:
    client = FakeClient()
    client.pos = SimpleNamespace(
        size=0.001, side="Buy", avg_price=97.0, unrealised_pnl=0.0
    )
    cycle = _make_cycle(tmp_path, client)

    async def run() -> None:
        cycle.store.state.position = OpenPosition(
            entry_price=97.0, qty=0.001, peak_price=97.0, stop_loss=95.06
        )
        cycle.store.state.phase = PHASE_IN_POSITION
        await cycle.recover()

    asyncio.run(run())
    assert client.sl_calls
    sl, take = client.sl_calls[0]
    assert sl == pytest.approx(95.06)
    assert take == pytest.approx(97.0 * 1.05)
    assert cycle.store.state.phase == PHASE_IN_POSITION


def test_recover_after_cancel_all_restores_sl(tmp_path: Path) -> None:
    """После снятия всех ордеров (kill/рестарт) SL/TP ставятся заново."""
    client = FakeClient()
    client.pos = SimpleNamespace(
        size=0.001, side="Sell", avg_price=103.0, unrealised_pnl=0.0
    )
    cycle = _make_cycle(tmp_path, client)

    async def run() -> None:
        await cycle.recover()

    asyncio.run(run())
    assert client.sl_calls
    assert cycle.store.state.position is not None
    assert cycle.store.state.position.side == "short"


# --- б.8 порог _position_gone + реальный pnl ----------------------------


def test_size_gone_no_false_positive_on_rounding(tmp_path: Path) -> None:
    client = FakeClient()
    cycle = _make_cycle(tmp_path, client, qty_step=0.0001)
    assert not cycle._size_gone(0.001234, 0.0012)  # округление до шага
    assert not cycle._size_gone(0.001, 0.001)
    assert cycle._size_gone(0.001, 0.0)  # позицию закрыла биржа
    assert cycle._size_gone(0.001, 0.0005)  # существенно меньше


def test_manage_position_keeps_position_on_rounding(tmp_path: Path) -> None:
    client = FakeClient()
    client.pos = SimpleNamespace(
        size=0.0012, side="Buy", avg_price=97.0, unrealised_pnl=0.0
    )
    cycle = _make_cycle(tmp_path, client, qty_step=0.0001)

    async def run() -> None:
        cycle.store.state.position = OpenPosition(
            entry_price=97.0, qty=0.001234, peak_price=97.0, stop_loss=95.06
        )
        cycle.store.state.phase = PHASE_IN_POSITION
        cycle.feed.prices.put_nowait(96.0)
        await cycle._manage_position()

    asyncio.run(run())
    st = cycle.store.state
    assert st.phase == PHASE_IN_POSITION
    assert st.position is not None


def test_exchange_exit_uses_realized_pnl(tmp_path: Path) -> None:
    client = FakeClient()
    client.pos = None
    client.closed_pnl = -1.25
    cycle = _make_cycle(tmp_path, client)

    async def run() -> None:
        cycle.store.state.position = OpenPosition(
            entry_price=97.0, qty=0.001, peak_price=97.0, stop_loss=95.06
        )
        cycle.store.state.phase = PHASE_IN_POSITION
        await cycle._manage_position()

    asyncio.run(run())
    st = cycle.store.state
    assert st.phase == PHASE_IDLE
    assert st.session_pnl == pytest.approx(-1.25)


def test_recover_uses_realized_pnl_for_closed_position(tmp_path: Path) -> None:
    client = FakeClient()
    client.pos = None
    client.closed_pnl = -2.5
    cycle = _make_cycle(tmp_path, client)

    async def run() -> None:
        cycle.store.state.phase = PHASE_IN_POSITION
        cycle.store.state.position = OpenPosition(
            entry_price=97.0, qty=0.001, peak_price=97.0, stop_loss=95.06
        )
        await cycle.recover()

    asyncio.run(run())
    st = cycle.store.state
    assert st.phase == PHASE_IDLE
    assert st.position is None
    assert st.session_pnl == pytest.approx(-2.5)
    assert st.peak_session_pnl == 0.0  # peak растёт только вверх


# --- в.1 ресинк после реконнекта WS -------------------------------------


def test_data_feed_reconnect_sets_resync_flag() -> None:
    from robot_zakol.data_feed import DataFeed

    async def run() -> bool:
        cfg = Config(api_key="", api_secret="", symbol="BTCUSDT")
        feed = DataFeed(cfg, asyncio.get_running_loop(), "BTCUSDT")
        feed._request_resync()
        await asyncio.sleep(0)
        return feed.resync_needed

    assert asyncio.run(run()) is True


def test_resync_after_reconnect_reconciles_state(tmp_path: Path) -> None:
    client = FakeClient()
    cycle = _make_cycle(tmp_path, client)

    async def run() -> None:
        cycle.store.state.phase = PHASE_WORKING
        cycle.store.state.pending_buy = _pending("missing", "Buy", 97.0)
        cycle.feed.resync_needed = True
        await cycle._maybe_resync()

    asyncio.run(run())
    assert cycle.feed.resync_needed is False
    assert client.open_orders_calls >= 1
    assert cycle.store.state.pending_buy is None
    assert cycle.store.state.phase == PHASE_IDLE


def test_private_ws_without_keys_alerts() -> None:
    from robot_zakol.data_feed import DataFeed

    alerts: list[str] = []

    async def on_alert(text: str) -> None:
        alerts.append(text)

    async def run() -> list[str]:
        cfg = Config(api_key="", api_secret="", symbol="BTCUSDT")
        feed = DataFeed(cfg, asyncio.get_running_loop(), "BTCUSDT", on_alert)
        feed._private_worker()  # без ключей: выходит сразу, но громко
        await asyncio.sleep(0.01)
        return alerts

    got = asyncio.run(run())
    assert got and "API" in got[0]


# --- в.2 wallet/position стримы -----------------------------------------


def test_data_feed_subscribes_wallet_and_position(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import robot_zakol.data_feed as feed_mod

    subs: list[str] = []
    holder: dict[str, Any] = {}

    class FakeWS:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def order_stream(self, callback: Any) -> None:
            subs.append("order")

        def execution_stream(self, callback: Any) -> None:
            subs.append("execution")

        def wallet_stream(self, callback: Any) -> None:
            subs.append("wallet")

        def position_stream(self, callback: Any) -> None:
            subs.append("position")

        def is_connected(self) -> bool:
            return True

        def exit(self) -> None:
            return None

    def fake_sleep(_seconds: float) -> None:
        holder["feed"]._stop.set()

    monkeypatch.setattr(feed_mod, "WebSocket", FakeWS)
    monkeypatch.setattr(feed_mod, "time", SimpleNamespace(sleep=fake_sleep))

    cfg = Config(api_key="k", api_secret="s", symbol="BTCUSDT")

    async def run() -> list[str]:
        loop = asyncio.get_running_loop()
        holder["feed"] = feed_mod.DataFeed(cfg, loop, "BTCUSDT")
        await loop.run_in_executor(None, holder["feed"]._private_worker)
        return subs

    got = asyncio.run(run())
    assert {"order", "execution", "wallet", "position"} <= set(got)


def test_balance_updated_from_wallet_stream() -> None:
    from robot_zakol.data_feed import DataFeed

    async def run() -> tuple[float, int]:
        cfg = Config(api_key="k", api_secret="s", symbol="BTCUSDT")
        feed = DataFeed(cfg, asyncio.get_running_loop(), "BTCUSDT")
        feed._on_wallet({"data": [{"totalEquity": "123.45"}]})
        await asyncio.sleep(0)  # _push кладёт в очередь через call_soon_threadsafe
        return feed.last_balance, feed.wallet_events.qsize()

    balance, queued = asyncio.run(run())
    assert balance == pytest.approx(123.45)
    assert queued == 1


def test_equity_baseline_seeded_on_first_balance(tmp_path: Path) -> None:
    client = FakeClient()
    cycle = _make_cycle(tmp_path, client)

    async def run() -> None:
        cycle.feed.last_balance = 100.0
        await cycle._maybe_equity_kill()

    asyncio.run(run())
    st = cycle.store.state
    assert st.start_equity == pytest.approx(100.0)
    assert st.peak_equity == pytest.approx(100.0)
    assert st.kill is False  # первая точка — не kill
    assert client.balance_calls == 0  # есть stream — REST не нужен


def test_equity_kill_on_drawdown_from_stream(tmp_path: Path) -> None:
    client = FakeClient()
    cycle = _make_cycle(tmp_path, client)

    async def run() -> None:
        cycle.feed.last_balance = 100.0
        await cycle._maybe_equity_kill()
        cycle.feed.last_balance = 94.0  # -6% < -5% DD
        await cycle._maybe_equity_kill()

    asyncio.run(run())
    st = cycle.store.state
    assert st.start_equity == pytest.approx(100.0)
    assert st.peak_equity == pytest.approx(100.0)
    assert st.kill is True


def test_equity_kill_disabled_when_limits_zero(tmp_path: Path) -> None:
    client = FakeClient()
    cycle = _make_cycle(
        tmp_path, client, cfg=_cfg(max_loss_usd=0.0, max_drawdown_pct=0.0)
    )

    async def run() -> None:
        cycle.feed.last_balance = 100.0
        await cycle._maybe_equity_kill()
        cycle.feed.last_balance = 50.0
        await cycle._maybe_equity_kill()

    asyncio.run(run())
    assert cycle.store.state.kill is False
    assert client.balance_calls == 0


def test_equity_rest_fallback_when_no_stream(tmp_path: Path) -> None:
    client = FakeClient()
    client.balance = 88.0
    cycle = _make_cycle(tmp_path, client)

    async def run() -> None:
        assert cycle.feed.last_balance == 0.0
        await cycle._maybe_equity_kill()

    asyncio.run(run())
    st = cycle.store.state
    assert st.start_equity == pytest.approx(88.0)
    assert client.balance_calls == 1


# --- в.4 notifier -------------------------------------------------------


def test_notifier_called_on_fill(tmp_path: Path) -> None:
    client = FakeClient()
    notifier = FakeNotifier()
    cycle = _make_cycle(tmp_path, client, notifier=notifier)

    async def run() -> None:
        cycle.store.state.pending_buy = _pending("oid-1", "Buy", 97.0, filled=0.001)
        await cycle._on_filled(cycle.store.state.pending_buy)
        await asyncio.sleep(0.01)

    asyncio.run(run())
    assert any("FILL" in m for m in notifier.messages)


def test_notifier_called_on_kill(tmp_path: Path) -> None:
    client = FakeClient()
    notifier = FakeNotifier()
    cycle = _make_cycle(tmp_path, client, notifier=notifier)

    async def run() -> None:
        cycle.store.state.position = OpenPosition(
            entry_price=97.0, qty=0.001, peak_price=97.0, stop_loss=95.06
        )
        cycle.store.state.session_pnl = 0.0
        cycle.store.state.peak_session_pnl = 5.0
        client.price = 96.0
        cycle.feed.last_price = 96.0
        await cycle._close_position(reason="test")
        await asyncio.sleep(0.01)

    asyncio.run(run())
    assert any("KILL" in m for m in notifier.messages)


def test_notifier_called_on_run_exception(tmp_path: Path) -> None:
    client = FakeClient()
    client.fail_on_place = 1
    notifier = FakeNotifier()
    cycle = _make_cycle(tmp_path, client, notifier=notifier)

    async def run() -> None:
        with pytest.raises(RuntimeError, match="place rejected"):
            await cycle.run()
        await asyncio.sleep(0.01)

    asyncio.run(run())
    assert any("исключение" in m for m in notifier.messages)


# --- б.1 shutdown: main дожидается цикла --------------------------------


def _patch_main(monkeypatch: pytest.MonkeyPatch, events: list[str]) -> Any:
    """Подменить все внешние зависимости robot_zakol.main."""
    import signal as signal_mod

    import robot_zakol.main as main_mod

    class FakeFilters:
        tick_size = 0.5
        qty_step = 0.001

    class FakeMainClient:
        def __init__(self, cfg: Any) -> None:
            pass

        async def get_instrument_filters(self, symbol: str) -> Any:
            return FakeFilters()

        def close(self) -> None:
            events.append("client-close")

    class FakeMainFeed:
        def __init__(self, cfg: Any, loop: Any, symbol: str, on_alert: Any = None):
            self.resync_needed = False

        def start(self) -> None:
            events.append("feed-start")

        def stop(self) -> None:
            events.append("feed-stop")

    class FakeMainStore:
        def __init__(self, path: Any, symbol: str = "") -> None:
            self.path = path
            self.symbol = symbol

        async def save(self) -> None:
            events.append("store-save")

    class FakeMainNotifier:
        def __init__(self, cfg: Any) -> None:
            pass

        async def notify(self, text: str) -> None:
            events.append(f"notify:{text}")

    class FakeCycle:
        def __init__(self, **kwargs: Any) -> None:
            pass

        async def recover(self) -> None:
            events.append("recover")

        def request_stop(self) -> None:
            events.append("request-stop")

        async def run(self) -> None:
            events.append("run-start")
            await asyncio.sleep(0.01)
            events.append("run-end")

    monkeypatch.setattr(main_mod, "BybitClient", FakeMainClient)
    monkeypatch.setattr(main_mod, "DataFeed", FakeMainFeed)
    monkeypatch.setattr(main_mod, "StateStore", FakeMainStore)
    monkeypatch.setattr(main_mod, "Notifier", FakeMainNotifier)
    monkeypatch.setattr(main_mod, "OrderCycle", FakeCycle)
    monkeypatch.setattr(main_mod, "load_config", lambda: _cfg())
    monkeypatch.setattr(main_mod, "setup_logging", lambda *a, **k: None)
    monkeypatch.setattr(
        main_mod,
        "signal",
        SimpleNamespace(
            SIGINT=signal_mod.SIGINT,
            SIGTERM=signal_mod.SIGTERM,
            signal=lambda *a, **k: None,
        ),
    )
    return main_mod


def test_shutdown_awaits_runner_before_closing_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    main_mod = _patch_main(monkeypatch, events)
    asyncio.run(main_mod._amain())
    assert events.index("run-end") < events.index("client-close")
    assert events.index("run-end") < events.index("feed-stop")
    assert "store-save" in events


def test_amain_cleanup_on_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    main_mod = _patch_main(monkeypatch, events)

    class FailingCycle:
        def __init__(self, **kwargs: Any) -> None:
            pass

        async def recover(self) -> None:
            raise RuntimeError("recover failed")

        def request_stop(self) -> None:
            events.append("request-stop")

        async def run(self) -> None:
            events.append("run-start")

    monkeypatch.setattr(main_mod, "OrderCycle", FailingCycle)
    with pytest.raises(RuntimeError, match="recover failed"):
        asyncio.run(main_mod._amain())
    assert "feed-stop" in events
    assert "client-close" in events
    assert "store-save" in events
    assert "run-start" not in events


# --- Доработки по чеклисту 2026-09-26 (B.5, D.4, D.5, E.1, E.3) ----------


def test_cancel_failure_keeps_state_when_order_alive(tmp_path: Path) -> None:
    client = FakeClient()
    client.fail_cancel = {"oid-1"}
    client.orders = [{"orderId": "oid-1"}]  # ордер жив на бирже
    cycle = _make_cycle(tmp_path, client)

    async def run() -> bool:
        cycle.store.state.phase = PHASE_WORKING
        cycle.store.state.pending_buy = _pending("oid-1", "Buy", 97.0)
        ok: bool = await cycle._cancel_pending(cycle.store.state.pending_buy)
        return ok

    ok = asyncio.run(run())
    assert ok is False
    assert cycle.store.state.pending_buy is not None


def test_cancel_failure_clears_state_when_order_gone(tmp_path: Path) -> None:
    client = FakeClient()
    client.fail_cancel = {"oid-1"}
    client.orders = []  # на бирже ордера уже нет
    cycle = _make_cycle(tmp_path, client)

    async def run() -> bool:
        cycle.store.state.phase = PHASE_WORKING
        cycle.store.state.pending_buy = _pending("oid-1", "Buy", 97.0)
        ok: bool = await cycle._cancel_pending(cycle.store.state.pending_buy)
        return ok

    ok = asyncio.run(run())
    assert ok is True
    assert cycle.store.state.pending_buy is None


def test_ttl_cancel_failure_backoff_keeps_working(tmp_path: Path) -> None:
    """E.3 + D.4: снять не вышло -> state цел, backoff, без смены фазы."""
    client = FakeClient()
    client.fail_cancel = {"oid-1", "oid-2"}
    client.orders = [{"orderId": "oid-1"}, {"orderId": "oid-2"}]
    cycle = _make_cycle(tmp_path, client)
    cycle.feed.last_price = 105.0

    async def run() -> None:
        cycle.store.state.phase = PHASE_WORKING
        cycle.store.state.pending_buy = _pending("oid-1", "Buy", 97.0)
        cycle.store.state.pending_sell = _pending("oid-2", "Sell", 103.0)
        await cycle._on_ttl()

    asyncio.run(run())
    st = cycle.store.state
    assert st.phase == PHASE_WORKING
    assert st.pending_buy is not None and st.pending_sell is not None
    assert cycle._ttl_backoff >= 1.0


def test_state_refuses_other_symbol(tmp_path: Path) -> None:
    path = tmp_path / "st.json"
    path.write_text(json.dumps({"symbol": "ETHUSDT"}), encoding="utf-8")
    with pytest.raises(ValueError, match="инструмента"):
        StateStore(path, symbol="BTCUSDT")


def test_state_refuses_newer_schema(tmp_path: Path) -> None:
    path = tmp_path / "st.json"
    path.write_text(
        json.dumps({"symbol": "BTCUSDT", "schema_version": 99}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="schema"):
        StateStore(path, symbol="BTCUSDT")


def test_state_writes_symbol_on_save(tmp_path: Path) -> None:
    path = tmp_path / "st.json"
    store = StateStore(path, symbol="BTCUSDT")
    asyncio.run(store.save())
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["symbol"] == "BTCUSDT"
    assert raw["schema_version"] == 1
    # тот же state открывается без ошибок
    StateStore(path, symbol="BTCUSDT")


def test_apply_server_sl_tp_clears_take_when_disabled(tmp_path: Path) -> None:
    """B.5: take_pct=0 -> отправляем 0, биржа снимает старый TP."""
    client = FakeClient()
    cycle = _make_cycle(tmp_path, client, cfg=_cfg(take_pct=0.0))

    async def run() -> None:
        pos = OpenPosition(
            entry_price=100.0,
            qty=1.0,
            peak_price=100.0,
            stop_loss=97.0,
            side="long",
        )
        await cycle._apply_server_sl_tp(pos)

    asyncio.run(run())
    assert client.sl_calls[-1] == (97.0, 0.0)


def test_task_result_logged_on_failure(caplog: pytest.LogCaptureFixture) -> None:
    """E.1: исключение фоновой задачи уходит в лог, а не в пустоту."""

    async def run() -> None:
        async def boom() -> None:
            raise RuntimeError("boom-task")

        task = asyncio.get_running_loop().create_task(boom())
        await asyncio.sleep(0)
        from robot_zakol.order_cycle import _log_task_result

        _log_task_result(task)

    with caplog.at_level(logging.ERROR):
        asyncio.run(run())
    assert any("boom-task" in r.message for r in caplog.records)
