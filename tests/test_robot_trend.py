"""Тесты robot_trend: config, state, risk, стаб-стратегия, order_flow."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import math
import time
from pathlib import Path
from typing import Any

import pytest

from core.bybit_client import BybitClient, Candle, Position
from robot_trend.config import TrendConfig
from robot_trend.main import _core_config
from robot_trend.order_flow import OrderFlow
from robot_trend.risk import (
    cooldown_remaining,
    daily_stop_reason,
    entry_qty,
    on_price,
    position_pnl,
    should_kill,
    slippage_ok,
)
from robot_trend.state import (
    PHASE_IDLE,
    PHASE_IN_POSITION,
    PHASE_STOPPED,
    OpenPosition,
    StateStore,
)
from robot_trend.strategy import Instruction, TrendParams, TrendStrategy, _Snapshot

# ============================== config ==============================


def _cfg(**overrides: Any) -> TrendConfig:
    base: dict[str, Any] = {
        "api_key": "k",
        "api_secret": "s",
        "testnet": False,
        "category": "linear",
        "symbol": "BTCUSDT",
        "timeframe": "5",
        "deposit_usd": 100.0,
        "fixed_qty": 0.0,
        "position_pct": 10.0,
        "candles_warmup": 5,
        "poll_sec": 0.3,
        "max_loss_usd": 0.0,
        "max_drawdown_pct": 0.0,
        "slippage_pct": 0.005,
        "cooldown_sec": 0.0,
        "daily_loss_limit": 0.0,
        "max_trades_per_day": 0,
        "reconnect_sec": 2.0,
        "heartbeat_sec": 1.0,
        "recv_window": 10000,
        "time_sync": True,
        "kill_switch_file": "",
        "order_link_prefix": "tr-",
        "funding_aware": False,
        "funding_window_sec": 60.0,
        "requests_per_second": 100.0,
        "log_level": "INFO",
        "log_file": "logs/trend.log",
        "state_file": "data/trend_state.json",
        "ws_enabled": True,
    }
    base.update(overrides)
    return TrendConfig(**base)


def test_config_defaults() -> None:
    cfg = _cfg()
    assert cfg.symbol == "BTCUSDT"
    assert cfg.order_link_prefix == "tr-"
    assert cfg.timeframe == "5"


def test_config_timeframe_must_be_positive_int() -> None:
    with pytest.raises(ValueError, match="TIMEFRAME"):
        _cfg(timeframe="abc")
    with pytest.raises(ValueError, match="TIMEFRAME"):
        _cfg(timeframe="0")


def test_config_position_pct_range() -> None:
    with pytest.raises(ValueError, match="POSITION_PCT"):
        _cfg(position_pct=0.0)
    with pytest.raises(ValueError, match="POSITION_PCT"):
        _cfg(position_pct=150.0)


def test_config_slippage_and_prefix_validation() -> None:
    with pytest.raises(ValueError, match="SLIPPAGE_PCT"):
        _cfg(slippage_pct=1.5)
    with pytest.raises(ValueError, match="ORDER_LINK_ID_PREFIX"):
        _cfg(order_link_prefix="")


def test_config_negative_seconds_rejected() -> None:
    with pytest.raises(ValueError):
        _cfg(cooldown_sec=-1.0)
    with pytest.raises(ValueError):
        _cfg(poll_sec=0.0)
    with pytest.raises(ValueError):
        _cfg(recv_window=0)


def test_validate_for_live_requires_keys() -> None:
    cfg = _cfg(api_key="", api_secret="")
    with pytest.raises(ValueError, match="BYBIT_API_KEY"):
        cfg.validate_for_live()


def test_validate_for_live_rejects_spot_and_no_size() -> None:
    with pytest.raises(ValueError, match="linear"):
        _cfg(category="spot").validate_for_live()
    with pytest.raises(ValueError, match="QTY"):
        _cfg(fixed_qty=0.0, deposit_usd=0.0).validate_for_live()
    with pytest.raises(ValueError, match="WS_ENABLED"):
        _cfg(ws_enabled=False).validate_for_live()


def test_order_notional() -> None:
    assert _cfg(deposit_usd=200.0, position_pct=10.0).order_notional() == 20.0
    assert _cfg(fixed_qty=0.01).order_notional() == 0.0


# ============================== state ==============================


def test_state_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    store = StateStore(path, symbol="BTCUSDT")
    store.state.position = OpenPosition(
        entry_price=100.0, qty=0.1, side="long", stop_loss=95.0, take_profit=110.0
    )
    store.state.phase = PHASE_IN_POSITION
    store.state.day = "2026-09-29"
    store.state.day_pnl = -3.5
    store.state.day_trades = 2
    store.state.last_close_at = 123.0
    store.state.kill = True
    asyncio.run(store.save())

    loaded = StateStore(path, symbol="BTCUSDT")
    st = loaded.state
    assert st.phase == PHASE_IN_POSITION
    assert st.position is not None
    assert st.position.entry_price == 100.0
    assert st.position.stop_loss == 95.0
    assert st.day_pnl == -3.5
    assert st.day_trades == 2
    assert st.last_close_at == 123.0
    assert st.kill is True
    assert st.symbol == "BTCUSDT"


def test_state_corrupt_file(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text("{oops", encoding="utf-8")
    store = StateStore(path, symbol="BTCUSDT")
    assert store.state.position is None
    assert store.state.phase == PHASE_IDLE


def test_state_wrong_symbol_raises(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"symbol": "ETHUSDT"}), encoding="utf-8")
    with pytest.raises(ValueError, match="другого инструмента"):
        StateStore(path, symbol="BTCUSDT")


def test_state_newer_schema_raises(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps({"symbol": "BTCUSDT", "schema_version": 99}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="новее кода"):
        StateStore(path, symbol="BTCUSDT")


# ============================== risk ==============================


def test_entry_qty_fixed_and_pct() -> None:
    assert entry_qty(0.01, 100.0, 10.0, 50.0) == 0.01
    # 100$ * 10% / 50 = 0.2
    assert entry_qty(0.0, 100.0, 10.0, 50.0) == pytest.approx(0.2)
    assert entry_qty(0.0, 100.0, 10.0, 0.0) == 0.0


def test_position_pnl_long_short() -> None:
    assert position_pnl(100.0, 110.0, 2.0, "long") == 20.0
    assert position_pnl(100.0, 110.0, 2.0, "short") == -20.0


def test_slippage_ok() -> None:
    assert slippage_ok(100.0, 100.4, 0.005)
    assert not slippage_ok(100.0, 102.0, 0.005)
    assert slippage_ok(100.0, 102.0, 0.0)  # выключено
    assert not slippage_ok(0.0, 100.0, 0.01)


def test_cooldown_remaining() -> None:
    now = 1000.0
    assert cooldown_remaining(now - 10.0, now, 60.0) == pytest.approx(50.0)
    assert cooldown_remaining(now - 120.0, now, 60.0) == 0.0
    assert cooldown_remaining(now, now, 0.0) == 0.0
    assert cooldown_remaining(0.0, now, 60.0) == 0.0


def test_daily_stop_reason() -> None:
    assert daily_stop_reason(-10.0, 0, 10.0, 0) == "daily_loss"
    assert daily_stop_reason(0.0, 5, 0.0, 5) == "max_trades"
    assert daily_stop_reason(0.0, 0, 0.0, 0) is None
    assert daily_stop_reason(-100.0, 100, 0.0, 0) is None  # всё выключено


def test_should_kill() -> None:
    # убыток сессии
    assert should_kill(-10.0, 0.0, 100.0, 10.0, 0.0)
    # просадка от пика: пик 10, сейчас -1, порог 5 → kill
    assert should_kill(-1.0, 10.0, 100.0, 0.0, 0.05)
    # ничего не включено
    assert not should_kill(-1000.0, 0.0, 100.0, 0.0, 0.0)


# ---- on_price: BE + трейлинг (активация +2%, стоп двигается только в плюс) ----


def _on_price(**overrides: Any) -> Any:
    base: dict[str, Any] = {
        "entry": 100.0,
        "peak": 100.0,
        "price": 100.0,
        "current_stop": 98.0,
        "stop_pct": 0.02,
        "be_trigger_pct": 0.02,
        "be_offset_pct": 0.0,
        "trail_pct": 0.02,
        "be_active": False,
        "side": "long",
    }
    base.update(overrides)
    return on_price(**base)


def test_on_price_before_trigger_keeps_initial_stop() -> None:
    # +1.9%:BE/trail ещё не включены, стоп остаётся -2%
    d = _on_price(price=101.9, peak=101.9)
    assert d.stop_loss == pytest.approx(98.0)
    assert not d.be_active
    assert not d.update_server


def test_on_price_be_moves_stop_to_entry() -> None:
    # +2%: безубыток — стоп на цену входа
    d = _on_price(price=102.0, peak=102.0)
    assert d.be_active
    assert d.stop_loss == pytest.approx(100.0)
    assert d.update_server


def test_on_price_trail_follows_peak() -> None:
    # +5%: trail = пик - 2% = 105 * 0.98 = 102.9 (выше безубытка)
    d = _on_price(price=105.0, peak=105.0)
    assert d.be_active
    assert d.stop_loss == pytest.approx(102.9)
    # откат: стоп не опускается (монотонность)
    d2 = _on_price(price=103.0, peak=105.0, current_stop=d.stop_loss, be_active=True)
    assert d2.stop_loss == pytest.approx(102.9)
    assert not d2.update_server


def test_on_price_short_mirrored() -> None:
    # шорт: активация при цене <= входа - 2%, стоп сверху
    # trail = минимум цены * 1.02
    d = _on_price(
        price=95.0,
        peak=95.0,
        current_stop=102.0,
        side="short",
    )
    assert d.be_active
    assert d.stop_loss == pytest.approx(95.0 * 1.02)  # 96.9
    # до активации стоп остаётся +2% от входа
    d0 = _on_price(price=98.5, peak=98.5, current_stop=102.0, side="short")
    assert not d0.be_active
    assert d0.stop_loss == pytest.approx(102.0)


def test_on_price_trail_disabled_when_zero() -> None:
    # trail_pct=0: только BE, стоп стоит на входе даже откатываясь с пика
    d = _on_price(price=110.0, peak=110.0, trail_pct=0.0)
    assert d.be_active
    assert d.stop_loss == pytest.approx(100.0)


# ============================== стратегия ==============================


def _trend_candles(
    flat: int = 260,
    rally: int = 20,
    pull: int = 6,
    resume: int = 4,
    vol: float = 600.0,
) -> list[Candle]:
    """Флэт → рост → откат (Supertrend вниз) → возобновление (вверх).

    На последней свече resume=4 происходит флип Supertrend вверх при
    выполненном ADX/EMA/объёме — стратегия даёт вход long.
    """
    out: list[Candle] = []
    price = 100.0
    ts = 0

    def add(o: float, c: float, v: float) -> None:
        nonlocal price, ts
        out.append(
            Candle(ts * 300_000, o, max(o, c) * 1.002, min(o, c) * 0.999, c, v),
        )
        price = c
        ts += 1

    for i in range(flat):
        add(price, price + (0.05 if i % 2 else -0.05), 100.0)
    for _ in range(rally):
        add(price, price * 1.008, 300.0)
    for _ in range(pull):
        add(price, price * 0.99, 150.0)
    for _ in range(resume):
        add(price, price * 1.012, vol)
    return out


def test_params_defaults_match_table() -> None:
    p = TrendParams()
    assert (p.ema_fast, p.ema_slow, p.ema_macro) == (20, 50, 200)
    assert (p.adx_period, p.adx_threshold) == (14, 25.0)
    assert (p.st_atr_period, p.st_factor) == (10, 3.0)
    assert (p.atr_period, p.sl_pct, p.tp_pct) == (14, 0.02, 0.05)
    assert (p.be_trigger_pct, p.be_offset_pct, p.trail_pct) == (0.02, 0.0, 0.02)
    assert (p.vol_period, p.vol_mult) == (20, 1.3)


def test_params_validation() -> None:
    with pytest.raises(ValueError, match="EMA"):
        TrendParams(ema_fast=50, ema_slow=20)
    with pytest.raises(ValueError, match=">= 2"):
        TrendParams(vol_period=1)
    with pytest.raises(ValueError, match="SL_PCT"):
        TrendParams(sl_pct=0.0)
    with pytest.raises(ValueError, match="TRAIL_PCT"):
        TrendParams(trail_pct=-0.01)
    with pytest.raises(ValueError, match="ADX_THRESHOLD"):
        TrendParams(adx_threshold=-1.0)


def test_params_from_env_symbol_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "ST_ATR_PERIOD",
        "ST_FACTOR",
        "ST_ATR_PERIOD_MAJOR",
        "ST_FACTOR_MAJOR",
    ):
        monkeypatch.delenv(name, raising=False)
    # BTC/ETH — длинный профиль
    assert TrendParams.from_env("BTCUSDT").st_atr_period == 55
    assert TrendParams.from_env("ETHUSDT").st_factor == 2.0
    # альты — короткий
    assert TrendParams.from_env("ADAUSDT").st_atr_period == 10
    assert TrendParams.from_env("ADAUSDT").st_factor == 3.0
    # override из окружения
    monkeypatch.setenv("ST_ATR_PERIOD", "14")
    assert TrendParams.from_env("SOLUSDT").st_atr_period == 14
    monkeypatch.setenv("ADX_THRESHOLD", "30")
    assert TrendParams.from_env("SOLUSDT").adx_threshold == 30.0


def test_decide_hold_on_insufficient_data() -> None:
    strat = TrendStrategy()
    assert strat.decide([], None) is None
    pos = OpenPosition(entry_price=100.0, qty=0.1, side="long")
    assert strat.decide(_trend_candles(flat=50, rally=0, resume=0), pos) is None


def test_decide_enter_long_on_supertrend_flip() -> None:
    strat = TrendStrategy()
    candles = _trend_candles()
    snap = strat._snapshot(candles)
    assert snap is not None and snap.st_flip == 1
    assert strat._entry(snap) == "long"

    ins = strat.decide(candles, None)
    assert ins is not None and ins.action == "enter"
    assert ins.side == "long"
    # стоп/тейк в процентах от цены входа: -2% / +5%
    assert ins.stop == pytest.approx(snap.close * 0.98)
    assert ins.take == pytest.approx(snap.close * 1.05)
    assert "ST flip long" in ins.reason


def test_decide_no_entry_without_volume() -> None:
    # объём свечи ниже VOL_MULT x SMA20 — пробой не подтверждён
    strat = TrendStrategy()
    snap = strat._snapshot(_trend_candles(vol=100.0))
    assert snap is not None and snap.vol_ratio < 1.3
    assert strat._entry(snap) is None
    assert strat.decide(_trend_candles(vol=100.0), None) is None


def test_decide_no_entry_when_adx_filter_blocks() -> None:
    strat = TrendStrategy(TrendParams(adx_threshold=1000.0))
    assert strat.decide(_trend_candles(), None) is None


def test_entry_short_and_stop_take_mirror() -> None:
    # зеркальный путь: разворот вниз + медвежий режим
    snap = _Snapshot(
        close=100.0,
        trend_up=False,
        trend_down=True,
        adx=30.0,
        di_plus=5.0,
        di_minus=50.0,
        st_dir=-1,
        st_flip=-1,
        atr=1.0,
        vol_ratio=2.0,
    )
    strat = TrendStrategy()
    assert strat._entry(snap) == "short"
    stop, take = strat._stop_take(snap, "short")
    assert stop == pytest.approx(100.0 * 1.02)
    assert take == pytest.approx(100.0 * 0.95)
    # тот же снапшот без флипа/объёма — входа нет
    assert strat._entry(dataclasses.replace(snap, st_flip=0)) is None
    assert strat._entry(dataclasses.replace(snap, vol_ratio=0.5)) is None


def test_decide_exit_on_supertrend_flip_against_position() -> None:
    strat = TrendStrategy()
    pos = OpenPosition(entry_price=110.0, qty=0.1, side="long")
    # откат перевёл Supertrend вниз → выход
    crash = _trend_candles(resume=0)
    ins = strat.decide(crash, pos)
    assert ins is not None and ins.action == "exit"
    assert "supertrend" in ins.reason
    # тренд жив — выхода нет
    assert strat.decide(_trend_candles(), pos) is None
    # шорт: рост против него → выход, падение в пользу → hold
    short = OpenPosition(entry_price=110.0, qty=0.1, side="short")
    assert strat.decide(_trend_candles(), short) is not None
    assert strat.decide(crash, short) is None


def test_check_signal_entry_then_exit() -> None:
    strat = TrendStrategy()
    assert strat.check_signal([]).action == "hold"

    sig = strat.check_signal(_trend_candles())
    assert sig.action == "buy"
    assert sig.stop_loss is not None and sig.take_profit is not None
    # позиция в памяти — повторного входа нет
    assert strat.check_signal(_trend_candles()).action == "hold"

    close = strat.check_signal(_trend_candles(resume=0))
    assert close.action == "close_long"
    # сброс памяти после закрытия сделки
    strat.on_position_closed("long")
    assert strat.check_signal(_trend_candles(resume=0)).action == "hold"


# ============================== фейки для order_flow ==============================


def _candles(n: int = 6, offset: int = 0) -> list[Candle]:
    """n свечей, последняя — формирующаяся (её order_flow отрезает)."""
    out: list[Candle] = []
    for i in range(n):
        t = (offset + i) * 300_000
        out.append(
            Candle(
                open_time=t, open=100.0, high=101.0, low=99.0, close=100.0, volume=1.0
            )
        )
    return out


class FakeClient:
    def __init__(self) -> None:
        self.price = 100.0
        self.candles: list[Candle] = _candles()
        self.position: Position | None = None
        self.placed: list[dict[str, Any]] = []
        self.sl_calls: list[tuple[float, float | None]] = []
        self.closed = 0
        self.cancelled_all = 0
        self.last_pnl: float | None = None
        self.funding_at: float | None = None
        self.fail_sl = False

    async def get_price(self, symbol: str) -> float:
        return self.price

    async def get_klines(
        self,
        symbol: str,
        interval: str | None = None,
        limit: int | None = None,
        start: int | None = None,
        end: int | None = None,
    ) -> list[Candle]:
        return list(self.candles)

    async def place_order(
        self,
        symbol: str,
        side: str,
        qty: float,
        order_type: str = "Market",
        price: float | None = None,
        *,
        post_only: bool = False,
        reduce_only: bool = False,
        position_idx: int | None = None,
        order_link_id: str | None = None,
    ) -> dict[str, Any]:
        self.placed.append(
            {"side": side, "qty": qty, "type": order_type, "link": order_link_id}
        )
        if order_type == "Market" and not reduce_only:
            self.position = Position(
                symbol=symbol,
                side=side,
                size=qty,
                avg_price=self.price,
                unrealised_pnl=0.0,
            )
        return {"result": {"orderId": "oid-1"}}

    async def set_stop_loss_take_profit(
        self,
        symbol: str,
        stop_loss: float,
        take_profit: float | None = None,
    ) -> dict[str, Any]:
        if self.fail_sl:
            raise RuntimeError("set_trading_stop failed")
        self.sl_calls.append((stop_loss, take_profit))
        return {}

    async def get_position(self, symbol: str) -> Position | None:
        return self.position

    async def close_position(
        self, symbol: str, qty: float, side: str
    ) -> dict[str, Any]:
        self.closed += 1
        self.position = None
        return {}

    async def get_last_closed_pnl(self, symbol: str) -> float | None:
        return self.last_pnl

    async def get_next_funding_time(self, symbol: str) -> float | None:
        return self.funding_at

    async def cancel_all_orders(self, symbol: str) -> dict[str, Any]:
        self.cancelled_all += 1
        return {}


class FakeFeed:
    def __init__(self) -> None:
        self.candles: asyncio.Queue[Any] = asyncio.Queue()
        self.prices: asyncio.Queue[Any] = asyncio.Queue()
        self.order_events: asyncio.Queue[Any] = asyncio.Queue()
        self.exec_events: asyncio.Queue[Any] = asyncio.Queue()
        self.wallet_events: asyncio.Queue[Any] = asyncio.Queue()
        self.position_events: asyncio.Queue[Any] = asyncio.Queue()
        self.last_price = 100.0
        self.last_balance = 0.0
        self.resync_needed = False


class FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def notify(self, text: str) -> None:
        self.sent.append(text)


class CountingStrategy(TrendStrategy):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0
        self.instruction: Instruction | None = None

    def decide(self, candles: list[Candle], position: Any) -> Instruction | None:
        self.calls += 1
        return self.instruction


def _filters() -> BybitClient.InstrumentFilters:
    return BybitClient.InstrumentFilters(
        tick_size=0.1,
        qty_step=0.001,
        min_qty=0.001,
        min_notional=5.0,
    )


def _make_flow(
    tmp_path: Path,
    cfg: TrendConfig | None = None,
    client: FakeClient | None = None,
    feed: FakeFeed | None = None,
    notifier: FakeNotifier | None = None,
    strategy: CountingStrategy | None = None,
) -> tuple[OrderFlow, FakeClient, FakeFeed, CountingStrategy, StateStore]:
    cfg = cfg or _cfg()
    client = client or FakeClient()
    feed = feed or FakeFeed()
    notifier = notifier or FakeNotifier()
    strategy = strategy or CountingStrategy()
    store = StateStore(tmp_path / "state.json", symbol=cfg.symbol)
    flow = OrderFlow(  # type: ignore[arg-type]
        cfg=cfg,
        client=client,  # type: ignore[arg-type]
        feed=feed,  # type: ignore[arg-type]
        store=store,
        notifier=notifier,  # type: ignore[arg-type]
        strategy=strategy,
        filters=_filters(),
    )
    return flow, client, feed, strategy, store


def _enter_instruction() -> Instruction:
    return Instruction(
        action="enter",
        side="long",
        stop=95.0,
        take=105.0,
        reason="test",
    )


# ============================== order_flow: вход ==============================


def test_enter_full_flow(tmp_path: Path) -> None:
    flow, client, _feed, strategy, store = _make_flow(tmp_path)
    strategy.instruction = _enter_instruction()
    asyncio.run(flow._tick())

    assert len(client.placed) == 1
    order = client.placed[0]
    assert order["side"] == "Buy"
    assert order["qty"] == pytest.approx(0.1)  # 100$*10% / 100$
    assert order["type"] == "Market"
    assert order["link"] is not None
    assert order["link"].startswith("tr-")
    assert len(order["link"]) <= 36

    assert client.sl_calls == [(95.0, 105.0)]
    st = store.state
    assert st.phase == PHASE_IN_POSITION
    assert st.position is not None
    assert st.position.entry_price == 100.0
    assert st.position.side == "long"
    assert st.position.stop_loss == 95.0


def test_enter_without_stop_rejected(tmp_path: Path) -> None:
    flow, client, _feed, strategy, store = _make_flow(tmp_path)
    strategy.instruction = Instruction(action="enter", side="long", reason="no sl")
    asyncio.run(flow._tick())
    assert client.placed == []
    assert store.state.position is None


def test_enter_bad_side_rejected(tmp_path: Path) -> None:
    flow, client, _feed, strategy, store = _make_flow(tmp_path)
    strategy.instruction = Instruction(
        action="enter", side="long!", stop=95.0, reason="bad"
    )
    asyncio.run(flow._tick())
    assert client.placed == []
    assert store.state.position is None


def test_enter_blocked_by_kill_file(tmp_path: Path) -> None:
    kill = tmp_path / "trend.kill"
    kill.write_text("", encoding="utf-8")
    cfg = _cfg(kill_switch_file=str(kill))
    flow, client, _feed, strategy, store = _make_flow(tmp_path, cfg=cfg)
    strategy.instruction = _enter_instruction()
    asyncio.run(flow._tick())
    assert client.placed == []
    assert store.state.kill is True
    assert store.state.phase == PHASE_STOPPED
    assert flow._stop.is_set()


def test_enter_blocked_by_cooldown(tmp_path: Path) -> None:
    cfg = _cfg(cooldown_sec=60.0)
    flow, client, _feed, strategy, store = _make_flow(tmp_path, cfg=cfg)
    store.state.last_close_at = time.time()
    strategy.instruction = _enter_instruction()
    asyncio.run(flow._tick())
    assert client.placed == []
    assert store.state.position is None


def test_enter_blocked_by_daily(tmp_path: Path) -> None:
    cfg = _cfg(daily_loss_limit=10.0, max_trades_per_day=3)
    flow, client, _feed, strategy, store = _make_flow(tmp_path, cfg=cfg)
    asyncio.run(flow._roll_day())  # зафиксировать текущие сутки UTC
    store.state.day_pnl = -10.0
    strategy.instruction = _enter_instruction()
    asyncio.run(flow._tick())
    assert client.placed == []
    # порог по числу сделок
    store.state.day_pnl = 0.0
    store.state.day_trades = 3
    client.candles = _candles(6, offset=10)
    asyncio.run(flow._tick())
    assert client.placed == []


def test_enter_blocked_by_funding(tmp_path: Path) -> None:
    cfg = _cfg(funding_aware=True, funding_window_sec=60.0)
    flow, client, _feed, strategy, _store = _make_flow(tmp_path, cfg=cfg)
    client.funding_at = time.time() + 30.0
    strategy.instruction = _enter_instruction()
    asyncio.run(flow._tick())
    assert client.placed == []


def test_enter_blocked_by_slippage(tmp_path: Path) -> None:
    flow, client, feed, strategy, store = _make_flow(tmp_path)
    feed.last_price = 90.0  # REST даёт 100 — расхождение 10% > 0.5%
    strategy.instruction = _enter_instruction()
    asyncio.run(flow._tick())
    assert client.placed == []
    assert store.state.position is None


def test_enter_sl_setup_failure_closes_position(tmp_path: Path) -> None:
    flow, client, _feed, strategy, store = _make_flow(tmp_path)
    client.fail_sl = True
    strategy.instruction = _enter_instruction()
    asyncio.run(flow._tick())
    # позиция открылась, SL не встала → аварийное закрытие
    assert client.closed == 1
    st = store.state
    assert st.position is None
    assert st.phase == PHASE_IDLE
    assert st.day_trades == 1


# ============================== order_flow: выход ==============================


def _open_position(flow: OrderFlow, store: StateStore) -> None:
    store.state.position = OpenPosition(
        entry_price=100.0,
        qty=0.1,
        side="long",
        stop_loss=95.0,
        take_profit=105.0,
        opened_at=time.time(),
    )
    store.state.phase = PHASE_IN_POSITION
    flow.client.position = Position(
        symbol="BTCUSDT", side="Buy", size=0.1, avg_price=100.0, unrealised_pnl=0.0
    )


def test_exit_flow_finalizes_counters(tmp_path: Path) -> None:
    flow, client, _feed, strategy, store = _make_flow(tmp_path)
    _open_position(flow, store)
    client.last_pnl = 7.5
    strategy.instruction = Instruction(action="exit", reason="take")
    client.candles = _candles(6, offset=10)
    asyncio.run(flow._tick())

    assert client.closed == 1
    st = store.state
    assert st.position is None
    assert st.phase == PHASE_IDLE
    assert st.day_pnl == pytest.approx(7.5)
    assert st.day_trades == 1
    assert st.session_pnl == pytest.approx(7.5)
    assert st.peak_session_pnl == pytest.approx(7.5)
    assert st.last_close_at > 0


def test_exit_without_position_ignored(tmp_path: Path) -> None:
    flow, client, _feed, strategy, _store = _make_flow(tmp_path)
    strategy.instruction = Instruction(action="exit", reason="x")
    asyncio.run(flow._tick())
    assert client.closed == 0


def test_position_gone_finalizes_as_server(tmp_path: Path) -> None:
    flow, client, _feed, _strategy, store = _make_flow(tmp_path)
    _open_position(flow, store)
    client.position = None  # биржа закрыла по SL
    client.last_pnl = -3.0
    asyncio.run(flow._tick())
    st = store.state
    assert st.position is None
    assert st.session_pnl == pytest.approx(-3.0)
    assert st.day_trades == 1
    assert st.day_pnl == pytest.approx(-3.0)


def test_unknown_action_logged_not_crash(tmp_path: Path) -> None:
    flow, client, _feed, strategy, _store = _make_flow(tmp_path)
    strategy.instruction = Instruction(action="dance", reason="?")
    asyncio.run(flow._tick())
    assert client.placed == []
    assert client.closed == 0


# ============================== kill / день ==============================


def test_kill_by_drawdown_stops_cycle(tmp_path: Path) -> None:
    cfg = _cfg(deposit_usd=100.0, max_drawdown_pct=0.05)  # порог 5$
    flow, client, _feed, _strategy, store = _make_flow(tmp_path, cfg=cfg)
    store.state.session_pnl = -1.0
    store.state.peak_session_pnl = 10.0  # просадка 11 >= 5
    asyncio.run(flow._tick())
    assert store.state.kill is True
    assert store.state.phase == PHASE_STOPPED
    assert flow._stop.is_set()
    assert client.placed == []


def test_kill_by_session_loss(tmp_path: Path) -> None:
    cfg = _cfg(max_loss_usd=10.0)
    flow, _client, _feed, _strategy, store = _make_flow(tmp_path, cfg=cfg)
    store.state.session_pnl = -10.0
    asyncio.run(flow._tick())
    assert store.state.kill is True


def test_roll_day_resets_counters(tmp_path: Path) -> None:
    flow, _client, _feed, _strategy, store = _make_flow(tmp_path)
    store.state.day = "2020-01-01"
    store.state.day_pnl = 5.0
    store.state.day_trades = 7
    asyncio.run(flow._roll_day())
    assert store.state.day != "2020-01-01"
    assert store.state.day_pnl == 0.0
    assert store.state.day_trades == 0


# ============================== order_flow: свечи / wake ==============================


def test_tick_dedup_decides_once(tmp_path: Path) -> None:
    flow, _client, _feed, strategy, _store = _make_flow(tmp_path)
    asyncio.run(flow._tick())
    assert strategy.calls == 1
    asyncio.run(flow._tick())  # те же свечи — решения нет
    assert strategy.calls == 1


def test_tick_new_candle_decides_again(tmp_path: Path) -> None:
    flow, client, _feed, strategy, _store = _make_flow(tmp_path)
    asyncio.run(flow._tick())
    client.candles = _candles(6, offset=10)
    asyncio.run(flow._tick())
    assert strategy.calls == 2


def test_order_link_prefix_and_len(tmp_path: Path) -> None:
    flow, _client, _feed, _strategy, _store = _make_flow(tmp_path)
    link = flow._order_link_id()
    assert link.startswith("tr-")
    assert len(link) <= 36
    assert flow._order_link_id() != link  # идемпотентность: каждый раз новый


def test_wait_wake_returns_on_candle_and_stop(tmp_path: Path) -> None:
    flow, _client, feed, _strategy, _store = _make_flow(tmp_path)

    async def _case_candle() -> bool:
        feed.candles.put_nowait({"confirm": True})
        return await flow._wait_wake()

    async def _case_stop() -> bool:
        flow.request_stop()
        return await flow._wait_wake()

    async def _case_timeout() -> bool:
        flow2, *_ = _make_flow(tmp_path / "b")
        start = time.monotonic()
        ok = await flow2._wait_wake()
        elapsed = time.monotonic() - start
        return ok and elapsed >= 0.3

    assert asyncio.run(_case_candle()) is True
    assert asyncio.run(_case_stop()) is False
    assert asyncio.run(_case_timeout()) is True


# ============================== recover ==============================


def test_recover_finalizes_position_closed_offline(tmp_path: Path) -> None:
    flow, client, _feed, _strategy, store = _make_flow(tmp_path)
    store.state.position = OpenPosition(
        entry_price=100.0, qty=0.1, side="long", stop_loss=95.0
    )
    store.state.phase = PHASE_IN_POSITION
    client.position = None
    client.last_pnl = 5.0
    assert asyncio.run(flow.recover()) is True
    st = store.state
    assert st.position is None
    assert st.phase == PHASE_IDLE
    assert st.session_pnl == pytest.approx(5.0)


def test_recover_adopts_external_position(tmp_path: Path) -> None:
    flow, client, _feed, _strategy, store = _make_flow(tmp_path)
    client.position = Position(
        symbol="BTCUSDT",
        side="Sell",
        size=0.42,
        avg_price=99.0,
        unrealised_pnl=0.0,
        stop_loss=105.0,
        take_profit=90.0,
    )
    assert asyncio.run(flow.recover()) is True
    st = store.state
    assert st.position is not None
    assert st.position.side == "short"
    assert st.position.qty == 0.42
    assert st.position.stop_loss == 105.0
    assert st.phase == PHASE_IN_POSITION


def test_recover_syncs_qty_and_ensures_sl(tmp_path: Path) -> None:
    flow, client, _feed, _strategy, store = _make_flow(tmp_path)
    store.state.position = OpenPosition(
        entry_price=100.0, qty=0.1, side="long", stop_loss=95.0, take_profit=110.0
    )
    store.state.phase = PHASE_IN_POSITION
    client.position = Position(
        symbol="BTCUSDT", side="Buy", size=0.05, avg_price=101.0, unrealised_pnl=0.0
    )
    assert asyncio.run(flow.recover()) is True
    assert store.state.position is not None
    assert store.state.position.qty == 0.05
    assert store.state.position.entry_price == 101.0
    assert client.sl_calls == [(95.0, 110.0)]


def test_recover_cancels_stray_orders_when_flat(tmp_path: Path) -> None:
    flow, client, _feed, _strategy, _store = _make_flow(tmp_path)
    assert asyncio.run(flow.recover()) is True
    assert client.cancelled_all == 1
    assert client.sl_calls == []


def test_recover_returns_false_when_exchange_down(tmp_path: Path) -> None:
    flow, client, _feed, _strategy, _store = _make_flow(tmp_path)

    async def _boom(symbol: str) -> Position | None:
        raise RuntimeError("api down")

    client.get_position = _boom  # type: ignore[method-assign]
    assert asyncio.run(flow.recover()) is False


# ============================== main ==============================


def test_core_config_mapping() -> None:
    trend = _cfg(
        recv_window=5000,
        time_sync=False,
        timeframe="15",
        requests_per_second=42.0,
    )
    cfg = _core_config(trend)
    assert cfg.symbol == "BTCUSDT"
    assert cfg.timeframe == "15"
    assert cfg.recv_window == 5000
    assert cfg.time_sync is False
    assert cfg.requests_per_second == 42.0
    assert cfg.simulation_mode is False
    assert cfg.ws_enabled is False


def test_qty_floors_to_step(tmp_path: Path) -> None:
    cfg = _cfg(position_pct=10.0)  # 0.1 BTC при цене 100
    flow, client, _feed, strategy, _store = _make_flow(tmp_path, cfg=cfg)
    strategy.instruction = _enter_instruction()
    asyncio.run(flow._tick())
    qty = client.placed[0]["qty"]
    assert qty == math.floor(qty / 0.001) * 0.001


def test_entry_below_min_notional_skipped(tmp_path: Path) -> None:
    cfg = _cfg(deposit_usd=1.0, position_pct=10.0)  # notional 0.1$ < 5$
    flow, client, _feed, strategy, _store = _make_flow(tmp_path, cfg=cfg)
    strategy.instruction = _enter_instruction()
    asyncio.run(flow._tick())
    assert client.placed == []
