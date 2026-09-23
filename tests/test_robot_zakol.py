"""Тесты robot_zakol: config, risk, state, order_cycle, fill_model, metrics."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from robot_zakol.backtest.config import FillConfig, StrategyConfig, mode_presets
from robot_zakol.backtest.data_loader import Candle, Series, candles_to_ticks
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
    on_price,
    partial_fill_ok,
    should_be,
    should_kill,
    trail_level,
)
from robot_zakol.state import (
    PHASE_IDLE,
    PHASE_IN_POSITION,
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
        "be_trigger_pct": 0.01,
        "max_loss_usd": 10.0,
        "max_consecutive_losses": 5,
        "partial_fill_pct": 0.80,
    }
    base.update(overrides)
    return ZakolConfig(**base)


def test_limit_buy_price_offset() -> None:
    assert limit_buy_price(100.0, 0.03) == pytest.approx(97.0)


def test_limit_buy_price_tick() -> None:
    assert limit_buy_price(100.0, 0.03, tick=0.5) == 97.0


def test_sl_formula_no_tp() -> None:
    assert initial_stop(100.0, 0.02) == 98.0
    assert not hasattr(_cfg(), "tp_pct")


def test_be_trigger() -> None:
    assert not should_be(100.0, 100.9, 0.01)
    assert should_be(100.0, 101.0, 0.01)


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


def test_kill_by_losses_and_max_loss() -> None:
    assert not should_kill(4, -9.0, 10.0, 5)
    assert should_kill(5, 0.0, 10.0, 5)
    assert should_kill(0, -10.0, 10.0, 5)


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
    store.state.pending = PendingOrder(
        order_id="1",
        order_link_id="l",
        price=97.0,
        qty=0.001,
        placed_at=1.0,
    )
    store.state.position = OpenPosition(
        entry_price=97.0,
        qty=0.001,
        peak_price=98.0,
        stop_loss=95.06,
    )
    store.state.consecutive_losses = 2
    store.state.session_pnl = -3.5
    loop = asyncio.new_event_loop()
    loop.run_until_complete(store.save())
    loop.close()
    store2 = StateStore(path)
    assert store2.state.phase == PHASE_WORKING
    assert store2.state.pending is not None
    assert store2.state.pending.order_id == "1"
    assert store2.state.position is not None
    assert store2.state.position.entry_price == 97.0
    assert store2.state.consecutive_losses == 2


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

    async def place_order(self, **kwargs: Any) -> dict[str, Any]:
        self.placed.append(kwargs)
        return {"result": {"orderId": "oid-1"}}

    async def cancel_order(self, symbol: str, order_id: str) -> dict[str, Any]:
        self.cancelled.append(order_id)
        return {}

    async def cancel_all_orders(self, symbol: str) -> dict[str, Any]:
        self.cancel_all += 1
        return {}

    async def get_open_orders(self, symbol: str) -> list[dict[str, Any]]:
        return self.orders

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


def _make_cycle(tmp_path: Path, client: FakeClient) -> Any:
    from robot_zakol.order_cycle import OrderCycle

    cfg = _cfg()
    store = StateStore(tmp_path / "st.json")
    feed = FakeFeed()
    return OrderCycle(cfg=cfg, client=client, feed=feed, store=store, tick_size=0.5)


def test_place_limit_sets_working(tmp_path: Path) -> None:
    client = FakeClient()
    cycle = _make_cycle(tmp_path, client)

    async def run() -> None:
        await cycle._place_limit()

    asyncio.run(run())
    assert cycle.store.state.phase == PHASE_WORKING
    assert cycle.store.state.pending is not None
    assert client.placed[0]["post_only"] is True
    assert client.placed[0]["side"] == "Buy"
    assert client.placed[0]["order_link_id"]


def test_on_filled_requires_partial_threshold(tmp_path: Path) -> None:
    client = FakeClient()
    cycle = _make_cycle(tmp_path, client)

    async def run() -> None:
        cycle.store.state.pending = PendingOrder(
            order_id="oid-1",
            order_link_id="l",
            price=97.0,
            qty=0.001,
            placed_at=0.0,
            filled_qty=0.0005,
        )
        await cycle._on_filled()

    asyncio.run(run())
    assert cycle.store.state.phase == PHASE_IDLE
    assert cycle.store.state.position is None
    assert "oid-1" in client.cancelled


def test_on_filled_full_fill_opens_position_no_tp(tmp_path: Path) -> None:
    client = FakeClient()
    cycle = _make_cycle(tmp_path, client)

    async def run() -> None:
        cycle.store.state.pending = PendingOrder(
            order_id="oid-1",
            order_link_id="l",
            price=97.0,
            qty=0.001,
            placed_at=0.0,
            filled_qty=0.001,
        )
        await cycle._on_filled()

    asyncio.run(run())
    st = cycle.store.state
    assert st.phase == PHASE_IN_POSITION
    assert st.position is not None
    assert st.position.entry_price == 97.0
    assert st.position.stop_loss == pytest.approx(97.0 * 0.98)
    assert client.sl_calls
    assert client.sl_calls[0][1] is None


def test_kill_after_five_losses(tmp_path: Path) -> None:
    client = FakeClient()
    cycle = _make_cycle(tmp_path, client)

    async def run() -> None:
        cycle.store.state.position = OpenPosition(
            entry_price=97.0,
            qty=0.001,
            peak_price=97.0,
            stop_loss=95.06,
        )
        cycle.store.state.consecutive_losses = 4
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
        cycle.store.state.pending = PendingOrder(
            order_id="missing",
            order_link_id="l",
            price=97.0,
            qty=0.001,
            placed_at=0.0,
        )
        await cycle.recover()

    asyncio.run(run())
    assert cycle.store.state.phase == PHASE_IDLE
    assert cycle.store.state.pending is None


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
    need, reason = update_position_risk(pos, 101.0, cfg)
    assert pos.be_active
    assert pos.stop >= 100.0
    assert reason is None
    assert need
