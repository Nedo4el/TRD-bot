"""Тесты robot_flat: config, state, risk, volume_profile, стратегия v3, order_flow."""

from __future__ import annotations

import asyncio
import json
import math
import time
from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest

import robot_flat.order_flow as order_flow_mod
from core.bybit_client import BybitClient, Candle, Position
from core.indicators import adx_di
from core.volume_profile import (
    build_volume_profile,
    compute_poc_va,
    find_poc,
    find_value_area,
)
from robot_flat.config import FlatConfig
from robot_flat.main import _core_config
from robot_flat.order_flow import OrderFlow
from robot_flat.risk import (
    cooldown_remaining,
    daily_stop_reason,
    entry_qty,
    on_price,
    position_pnl,
    should_kill,
    slippage_ok,
)
from robot_flat.state import (
    PHASE_IDLE,
    PHASE_IN_POSITION,
    PHASE_STOPPED,
    OpenPosition,
    StateStore,
)
from robot_flat.strategy import FlatParams, FlatStrategy, Instruction, _Profile

# ============================== config ==============================


def _cfg(**overrides: Any) -> FlatConfig:
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
        "order_link_prefix": "fl-",
        "funding_aware": False,
        "funding_window_sec": 60.0,
        "requests_per_second": 100.0,
        "log_level": "INFO",
        "log_file": "logs/flat.log",
        "state_file": "data/flat_state.json",
        "ws_enabled": True,
    }
    base.update(overrides)
    return FlatConfig(**base)


def test_config_defaults() -> None:
    cfg = _cfg()
    assert cfg.symbol == "BTCUSDT"
    assert cfg.order_link_prefix == "fl-"
    assert cfg.timeframe == "5"
    assert cfg.kill_switch_file == ""  # в тестах выключен (в .env — data/flat.kill)
    assert not hasattr(cfg, "htf_timeframe")  # flat — один ТФ


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
    # +1.9%: BE/trail ещё не включены, стоп остаётся -2%
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


def test_on_price_all_disabled_keeps_stop() -> None:
    # весь риск-слой выключен (ATR-стопы стратегии) — стоп не трогаем
    d = _on_price(
        price=110.0,
        peak=110.0,
        current_stop=95.0,
        stop_pct=0.0,
        be_trigger_pct=0.0,
        trail_pct=0.0,
    )
    assert d.stop_loss == 95.0
    assert not d.update_server


def test_on_price_be_only_when_stop_pct_zero() -> None:
    # v3: стоп даёт стратегия (sl_pct=0) — pre-BE стоп не двигаем,
    # после +3% безубыток ставит стоп на цену входа
    d = _on_price(
        price=102.9,
        peak=102.9,
        current_stop=95.0,
        stop_pct=0.0,
        be_trigger_pct=0.03,
        trail_pct=0.0,
    )
    assert not d.be_active
    assert d.stop_loss == 95.0
    d2 = _on_price(
        price=103.0,
        peak=103.0,
        current_stop=95.0,
        stop_pct=0.0,
        be_trigger_pct=0.03,
        trail_pct=0.0,
    )
    assert d2.be_active
    assert d2.stop_loss == pytest.approx(100.0)
    assert d2.update_server
    # шорт зеркально: −3% от входа → стоп на вход
    d3 = _on_price(
        price=97.0,
        peak=97.0,
        current_stop=105.0,
        stop_pct=0.0,
        be_trigger_pct=0.03,
        trail_pct=0.0,
        side="short",
    )
    assert d3.be_active
    assert d3.stop_loss == pytest.approx(100.0)


# ============================== volume_profile ==============================


def test_build_volume_profile_poc() -> None:
    # 90% объёма в коридоре 100–101, хвост 101–102 → POC в коридоре
    c1 = Candle(0, 100.5, 101.0, 100.0, 100.5, 90.0)
    c2 = Candle(300_000, 101.0, 102.0, 100.9, 101.5, 10.0)
    profile = build_volume_profile([c1, c2], num_bins=10)
    assert profile
    poc = find_poc(profile)
    assert poc is not None
    assert 100.8 <= poc <= 101.2
    assert find_poc([]) is None


def test_value_area_covers_poc() -> None:
    bars = [Candle(i * 300_000, 100.4, 101.0, 100.0, 100.7, 10.0) for i in range(6)]
    profile = build_volume_profile(bars, num_bins=20)
    area = find_value_area(profile, 0.7)
    assert area is not None
    val, vah = area
    poc = find_poc(profile)
    assert poc is not None
    assert 100.0 <= val <= poc <= vah <= 101.0
    assert find_value_area(profile, 0.0) is None


def test_compute_poc_va_degenerate() -> None:
    assert compute_poc_va([]) is None
    one = [Candle(0, 100.0, 101.0, 99.0, 100.0, 1.0)]
    assert compute_poc_va(one) is None  # нужен минимум 2 бара


# ============================== стратегия v3 ==============================


def _mk(bar: int, o: float, h: float, l: float, c: float, v: float = 100.0) -> Candle:
    """Бар M5 по индексу (open_time = bar × 5 мин)."""
    return Candle(bar * 300_000, o, h, l, c, v)


def _flat_bars(n: int = 30) -> list[Candle]:
    """Плоский боковик в коридоре 100.4–100.6 (ADX мал, входов нет)."""
    return [_mk(i, 100.5, 100.6, 100.4, 100.5) for i in range(n)]


def _frozen(
    candles: list[Candle],
    poc: float = 100.5,
    val: float = 100.15,
    vah: float = 100.85,
    params: FlatParams | None = None,
) -> FlatStrategy:
    """Стратегия с замороженным профилем (ts = последняя свеча окна)."""
    strat = FlatStrategy(params if params is not None else FlatParams())
    strat._profile = _Profile(
        ts=candles[-1].open_time,
        poc=poc,
        val=val,
        vah=vah,
    )
    return strat


def test_params_defaults() -> None:
    p = FlatParams()
    assert (p.poc_window_min, p.poc_refresh_min) == (30, 60)
    assert (p.value_area_pct, p.profile_bins) == (0.7, 100)
    assert (p.adx_period, p.adx_threshold) == (14, 25.0)
    assert (p.grid_levels, p.middle_pct) == (3, 0.5)
    assert (p.breakout_pct, p.breakout_return_min) == (0.05, 10)
    assert (p.tp_offset_pct, p.tp_split) == (0.01, 0.5)
    assert (p.sl_pct, p.be_trigger_pct) == (0.0, 0.03)  # стоп стратегии, BE +3%


def test_params_validation() -> None:
    with pytest.raises(ValueError, match="VALUE_AREA_PCT"):
        FlatParams(value_area_pct=0.0)
    with pytest.raises(ValueError, match="MIDDLE_PCT"):
        FlatParams(middle_pct=1.0)
    with pytest.raises(ValueError, match="GRID_LEVELS"):
        FlatParams(grid_levels=0)
    with pytest.raises(ValueError, match="BREAKOUT_PCT"):
        FlatParams(breakout_pct=0.0)
    with pytest.raises(ValueError, match="BREAKOUT_RETURN_MIN"):
        FlatParams(breakout_return_min=0)
    with pytest.raises(ValueError, match="TP_SPLIT"):
        FlatParams(tp_split=1.0)
    with pytest.raises(ValueError, match="SL_PCT"):
        FlatParams(sl_pct=-0.01)
    with pytest.raises(ValueError, match="ADX_THRESHOLD"):
        FlatParams(adx_threshold=-1.0)


def test_params_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POC_WINDOW_MIN", "45")
    monkeypatch.setenv("ADX_THRESHOLD", "30")
    monkeypatch.setenv("MIDDLE_PCT", "0.3")
    monkeypatch.setenv("BE_TRIGGER_PCT", "0.05")
    p = FlatParams.from_env()
    assert p.poc_window_min == 45
    assert p.adx_threshold == 30.0
    assert p.middle_pct == 0.3
    assert p.be_trigger_pct == 0.05


def test_check_signal_empty_and_short_window() -> None:
    strat = FlatStrategy(FlatParams())
    assert strat.name == "flat"
    assert strat.check_signal([]).action == "hold"
    assert strat.check_signal(_flat_bars(5)).action == "hold"  # < 28 баров


def test_grid_levels_inside_va_outside_middle() -> None:
    strat = _frozen(_flat_bars())
    p = strat.params
    prof = strat._profile
    assert prof is not None
    longs, shorts = strat._grid(prof)
    assert len(longs) == 3 and len(shorts) == 3
    half_mid = (prof.vah - prof.val) * p.middle_pct / 2
    for lv in longs:
        assert prof.val < lv < prof.poc - half_mid  # внутри сектора, вне middle
    for lv in shorts:
        assert prof.poc + half_mid < lv < prof.vah
    assert all(a < b for a, b in pairwise(longs))  # по возрастанию
    # вырожденный диапазон → сетки нет
    assert strat._grid(_Profile(ts=0, poc=100.0, val=100.0, vah=100.0)) == ([], [])


def test_hold_when_no_level_touched() -> None:
    candles = _flat_bars()
    sig = _frozen(candles).check_signal(candles)
    assert sig.action == "hold"
    assert "не коснулись" in sig.reason


def test_entry_long_touch_levels_and_levels() -> None:
    candles = _flat_bars(30)
    candles[-1] = _mk(29, 100.5, 100.6, 100.19, 100.5)  # касание всех long
    strat = _frozen(candles)
    sig = strat.check_signal(candles)
    assert sig.action == "buy"
    # из коснутых выбирается ближайший к POC (= верхний long-уровень)
    longs, _ = strat._grid(strat._profile)  # type: ignore[arg-type]
    assert sig.entry_price == pytest.approx(max(longs))
    assert sig.stop_loss == pytest.approx(100.15 * 0.95)  # VAL − 5%
    assert sig.take_profit == pytest.approx(100.5)  # POC
    assert sig.tp_split == 0.5
    assert sig.take_profit is not None and sig.take_profit2 is not None
    assert sig.take_profit2 >= sig.take_profit  # кламп к POC (offset 1% шире VA)
    assert "grid long" in sig.reason
    # уровень занят — повторного входа на нём нет
    again = strat.check_signal(candles)
    assert again.action == "buy"  # второй уровень (близкие свободны)
    assert again.entry_price == pytest.approx(longs[1])


def test_entry_short_mirror() -> None:
    candles = _flat_bars(30)
    candles[-1] = _mk(29, 100.5, 100.72, 100.4, 100.6)  # касание нижнего short
    strat = _frozen(candles)
    sig = strat.check_signal(candles)
    assert sig.action == "sell"
    _, shorts = strat._grid(strat._profile)  # type: ignore[arg-type]
    assert sig.entry_price == pytest.approx(min(shorts))
    assert sig.stop_loss == pytest.approx(100.85 * 1.05)  # VAH + 5%
    assert sig.take_profit == pytest.approx(100.5)
    # зеркальный TP2: обратная (long) сетка + offset, кламп к POC
    if sig.take_profit2 is not None and sig.take_profit is not None:
        assert sig.take_profit2 <= sig.take_profit


def test_tp2_beyond_poc_when_offset_small() -> None:
    params = FlatParams(tp_offset_pct=0.001)
    candles = _flat_bars(30)
    candles[-1] = _mk(29, 100.5, 100.6, 100.19, 100.5)
    sig = _frozen(candles, params=params).check_signal(candles)
    assert sig.action == "buy"
    assert sig.take_profit2 is not None
    # min(shorts) × 0.999 = 100.71875 × 0.999 ≈ 100.618 > POC
    assert sig.take_profit2 == pytest.approx(100.71875 * 0.999)
    assert sig.take_profit is not None
    assert sig.take_profit2 > sig.take_profit


def test_adx_trend_gate_blocks_entry() -> None:
    # сильный нисходящий тренд к POC → ADX >= 25 → входов нет,
    # хотя финальный бар закрыт в VA и касается long-уровня
    bars: list[Candle] = []
    price = 100.0
    for i in range(40):  # медленный рост
        bars.append(_mk(i, price, price * 1.002, price * 0.999, price * 1.001))
        price *= 1.001
    for i in range(40, 55):  # падение к VA (~−0.23%/бар)
        bars.append(_mk(i, price, price * 1.001, price * 0.998, price * 0.9977))
        price *= 0.9977
    bars.append(_mk(55, price, 100.6, 100.19, 100.5))  # закрытие в VA, касание
    strat = _frozen(bars)
    sig = strat.check_signal(bars)
    assert sig.action == "hold"
    assert "тренд" in sig.reason
    adx_s, _, _ = adx_di(
        [c.high for c in bars],
        [c.low for c in bars],
        [c.close for c in bars],
        14,
    )
    assert adx_s[-1] >= 25.0  # предусловие теста


def test_guard_breakout_return_and_halt() -> None:
    bars = _flat_bars(30)
    strat = _frozen(bars)
    # свеча вне диапазона (вниз) — таймер стартует, входов нет
    bars.append(_mk(30, 100.4, 100.5, 99.4, 99.5))
    sig = strat.check_signal(bars)
    assert sig.action == "hold"
    assert strat._breakout_since == bars[-1].open_time
    # вернулась — таймер сброшен
    bars.append(_mk(31, 99.6, 100.5, 99.5, 100.5))
    strat.check_signal(bars)
    assert strat._breakout_since is None
    # снова вне 10+ минут подряд → халт + форс-пересчёт POC
    bars.append(_mk(32, 100.5, 100.6, 99.4, 99.5))
    strat.check_signal(bars)
    assert strat._breakout_since == bars[-1].open_time
    bars.append(_mk(33, 99.5, 99.6, 99.4, 99.5))
    strat.check_signal(bars)
    bars.append(_mk(34, 99.5, 99.6, 99.4, 99.5))
    sig = strat.check_signal(bars)
    assert sig.action == "hold"
    assert "халт" in sig.reason
    assert strat._profile is not None
    assert strat._profile.ts == bars[-1].open_time  # POC пересчитан
    assert strat._breakout_since is None


def test_hourly_profile_refresh() -> None:
    bars = _flat_bars(30)
    strat = _frozen(bars)
    frozen_ts = strat._profile.ts  # type: ignore[union-attr]
    # +55 минут — профиль ещё заморожен
    for i in range(11):
        bars.append(_mk(30 + i, 100.5, 100.6, 100.4, 100.5))
    strat.check_signal(bars)
    assert strat._profile is not None and strat._profile.ts == frozen_ts
    # +60 минут — hourly rolling-пересчёт от последней свечи
    bars.append(_mk(41, 100.5, 100.6, 100.4, 100.5))
    strat.check_signal(bars)
    assert strat._profile is not None
    assert strat._profile.ts == bars[-1].open_time


def test_grid_capacity_and_release() -> None:
    candles = _flat_bars(30)
    # порог ADX поднят: сами касания просаживают low и поднимают ADX —
    # здесь изолируем логику книги, не тренд-гейт
    strat = _frozen(candles, params=FlatParams(adx_threshold=99.0))
    # три входа подряд по разным уровням (касания: все / средний / нижний)
    for i, low in enumerate([100.19, 100.23, 100.19]):
        candles.append(_mk(30 + i, 100.5, 100.6, low, 100.5))
        assert strat.check_signal(candles).action == "buy"
    # сторона заполнена
    candles.append(_mk(33, 100.5, 100.6, 100.19, 100.5))
    sig = strat.check_signal(candles)
    assert sig.action == "hold"
    assert "заполнена" in sig.reason
    # позиция закрыта → книга свободна → вход снова возможен
    strat.on_position_closed("long")
    assert strat.check_signal(candles).action == "buy"


def test_decide_live_enter_and_busy_position() -> None:
    candles = _flat_bars(30)
    candles[-1] = _mk(29, 100.5, 100.6, 100.19, 100.5)
    strat = _frozen(candles)
    ins = strat.decide(candles, None)
    assert ins is not None and ins.action == "enter" and ins.side == "long"
    assert ins.stop == pytest.approx(95.1425)
    assert ins.take == pytest.approx(100.5)
    assert "grid long" in ins.reason
    # позиция открыта — новых входов нет (SL/TP делает биржа)
    pos = OpenPosition(entry_price=100.28, qty=0.1, side="long")
    assert strat.decide(candles, pos) is None


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
        self.cancelled_orders: list[str] = []
        self.last_pnl: float | None = None
        self.funding_at: float | None = None
        self.fail_sl = False
        self.fill_limit = True  # False — лимитка не исполняется
        self.fill_on_cancel = False  # гонка: исполнилась в момент отмены

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
            {
                "side": side,
                "qty": qty,
                "type": order_type,
                "price": price,
                "link": order_link_id,
            }
        )
        if order_type in ("Market", "Limit") and not reduce_only and self.fill_limit:
            self.position = Position(
                symbol=symbol,
                side=side,
                size=qty,
                avg_price=(price or self.price)
                if order_type == "Limit"
                else self.price,
                unrealised_pnl=0.0,
            )
        return {"result": {"orderId": "oid-1"}}

    async def cancel_order(self, symbol: str, order_id: str) -> dict[str, Any]:
        self.cancelled_orders.append(order_id)
        if self.fill_on_cancel:
            self.position = Position(
                symbol=symbol,
                side="Buy",
                size=0.1,
                avg_price=99.0,
                unrealised_pnl=0.0,
            )
        return {}

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


class CountingStrategy(FlatStrategy):
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
    cfg: FlatConfig | None = None,
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
        price=99.0,
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
    assert order["type"] == "Limit"  # вход только лимитками
    assert order["price"] == pytest.approx(99.0)  # уровень стратегии
    assert order["link"] is not None
    assert order["link"].startswith("fl-")
    assert len(order["link"]) <= 36

    assert client.sl_calls == [(95.0, 105.0)]
    st = store.state
    assert st.phase == PHASE_IN_POSITION
    assert st.position is not None
    assert st.position.entry_price == 99.0
    assert st.position.side == "long"
    assert st.position.stop_loss == 95.0


def test_enter_limit_price_fallback_to_rest(tmp_path: Path) -> None:
    # инструкция без уровня — лимитка по текущей цене
    flow, client, _feed, strategy, _store = _make_flow(tmp_path)
    strategy.instruction = Instruction(
        action="enter",
        side="long",
        stop=95.0,
        take=105.0,
        reason="no level",
    )
    asyncio.run(flow._tick())
    assert client.placed[0]["type"] == "Limit"
    assert client.placed[0]["price"] == pytest.approx(100.0)


def test_enter_limit_not_filled_cancelled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(order_flow_mod, "ENTRY_FILL_RETRIES", 1)
    flow, client, _feed, strategy, store = _make_flow(tmp_path)
    client.fill_limit = False
    strategy.instruction = _enter_instruction()
    asyncio.run(flow._tick())
    assert client.placed[0]["type"] == "Limit"
    assert client.cancelled_orders == ["oid-1"]  # снята
    assert client.sl_calls == []
    assert store.state.position is None
    assert store.state.phase == PHASE_IDLE


def test_enter_limit_cancel_race_fill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # лимитка исполнилась в момент отмены — позицию подхватываем
    monkeypatch.setattr(order_flow_mod, "ENTRY_FILL_RETRIES", 1)
    flow, client, _feed, strategy, store = _make_flow(tmp_path)
    client.fill_limit = False
    client.fill_on_cancel = True
    strategy.instruction = _enter_instruction()
    asyncio.run(flow._tick())
    assert client.cancelled_orders == ["oid-1"]
    st = store.state
    assert st.position is not None
    assert st.position.entry_price == pytest.approx(99.0)
    assert client.sl_calls == [(95.0, 105.0)]
    assert st.phase == PHASE_IN_POSITION


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
    kill = tmp_path / "flat.kill"
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
    flow.client.position = Position(  # type: ignore[attr-defined]
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


def test_tick_passes_position_to_strategy(tmp_path: Path) -> None:
    flow, _client, _feed, strategy, store = _make_flow(tmp_path)
    store.state.position = OpenPosition(entry_price=100.0, qty=0.1, side="long")
    # позиция подтверждена биржей — решаем с ней
    flow.client.position = Position(  # type: ignore[attr-defined]
        symbol="BTCUSDT", side="Buy", size=0.1, avg_price=100.0, unrealised_pnl=0.0
    )
    asyncio.run(flow._tick())
    assert strategy.calls == 1


def test_order_link_prefix_and_len(tmp_path: Path) -> None:
    flow, _client, _feed, _strategy, _store = _make_flow(tmp_path)
    link = flow._order_link_id()
    assert link.startswith("fl-")
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


# ============================== shutdown ==============================


def test_shutdown_closes_position_and_cancels(tmp_path: Path) -> None:
    flow, client, _feed, _strategy, store = _make_flow(tmp_path)
    client.position = Position(
        symbol="BTCUSDT",
        side="Buy",
        size=0.1,
        avg_price=100.0,
        unrealised_pnl=0.0,
    )
    store.state.position = OpenPosition(
        entry_price=100.0,
        qty=0.1,
        side="long",
        stop_loss=95.0,
        take_profit=105.0,
        opened_at=time.time(),
    )
    store.state.phase = PHASE_IN_POSITION
    asyncio.run(flow._on_shutdown())

    st = store.state
    assert client.closed == 1  # закрытие по рынку
    assert client.cancelled_all == 1  # остатки сняты после закрытия
    assert st.position is None
    assert st.phase == PHASE_IDLE


def test_shutdown_flat_cancels_orders(tmp_path: Path) -> None:
    flow, client, _feed, _strategy, store = _make_flow(tmp_path)
    store.state.kill = True
    store.state.phase = PHASE_STOPPED
    asyncio.run(flow._on_shutdown())

    assert client.closed == 0
    assert client.cancelled_all == 1
    assert store.state.phase == PHASE_STOPPED  # kill → STOPPED


def test_shutdown_close_fails_keeps_position(tmp_path: Path) -> None:
    flow, client, _feed, _strategy, store = _make_flow(tmp_path)

    async def _boom(symbol: str, qty: float, side: str) -> dict[str, Any]:
        raise RuntimeError("api down")

    client.close_position = _boom  # type: ignore[method-assign]
    store.state.position = OpenPosition(
        entry_price=100.0,
        qty=0.1,
        side="long",
        stop_loss=95.0,
        take_profit=105.0,
        opened_at=time.time(),
    )
    store.state.phase = PHASE_IN_POSITION
    asyncio.run(flow._on_shutdown())

    st = store.state
    assert st.position is not None  # позиция остаётся — SL/TP работает
    assert client.cancelled_all == 0  # заявки не снимаем
    assert st.phase == PHASE_IN_POSITION


# ============================== main ==============================


def test_core_config_mapping() -> None:
    flat = _cfg(
        recv_window=5000,
        time_sync=False,
        timeframe="15",
        requests_per_second=42.0,
    )
    cfg = _core_config(flat)
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
