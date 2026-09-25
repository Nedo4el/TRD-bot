"""Риск-менеджмент: SL, TP, trailing, BE, kill-switch, partial fill."""

from __future__ import annotations

from dataclasses import dataclass

from robot_zakol.config import ZakolConfig


@dataclass(frozen=True)
class RiskDecision:
    """Что делать на текущей цене позиции."""

    stop_loss: float
    trail_stop: float | None
    be_active: bool
    update_server: bool


def limit_buy_price(price: float, offset_pct: float, tick: float = 0.0) -> float:
    """Цена PostOnly buy: price * (1 - offset), округлить до tick."""
    raw = price * (1.0 - offset_pct)
    if tick <= 0:
        return raw
    return round(raw / tick) * tick


def limit_sell_price(price: float, offset_pct: float, tick: float = 0.0) -> float:
    """Цена PostOnly sell: price * (1 + offset), округлить до tick."""
    raw = price * (1.0 + offset_pct)
    if tick <= 0:
        return raw
    return round(raw / tick) * tick


def initial_stop(entry: float, stop_pct: float, side: str = "long") -> float:
    """Стоп от точки входа: long ниже цены, short выше."""
    if side == "short":
        return entry * (1.0 + stop_pct)
    return entry * (1.0 - stop_pct)


def take_level(entry: float, take_pct: float, side: str = "long") -> float:
    """Уровень тейка: long выше цены, short ниже."""
    if side == "short":
        return entry * (1.0 - take_pct)
    return entry * (1.0 + take_pct)


def should_be(entry: float, price: float, be_trigger_pct: float) -> bool:
    return price >= entry * (1.0 + be_trigger_pct)


def trail_level(peak: float, trail_pct: float) -> float:
    return peak * (1.0 - trail_pct)


def position_pnl(entry: float, price: float, qty: float, side: str) -> float:
    """PnL позиции: long растёт с ценой, short — падает."""
    if side == "short":
        return (entry - price) * qty
    return (price - entry) * qty


def on_price(
    entry: float,
    peak: float,
    price: float,
    current_stop: float,
    cfg: ZakolConfig,
    be_active: bool,
    side: str = "long",
) -> RiskDecision:
    """Обновить peak/SL/trail по новой цене. Монотонно в сторону прибыли.

    trail_pct <= 0 — трейлинг выключен, be_trigger_pct <= 0 — BE выключен.
    """
    if side == "short":
        return _on_price_short(entry, peak, price, current_stop, cfg, be_active)
    new_peak = max(peak, price)
    be_now = be_active or (
        cfg.be_trigger_pct > 0 and should_be(entry, price, cfg.be_trigger_pct)
    )
    if be_now:
        base = entry * (1.0 + cfg.be_offset_pct)
    else:
        base = entry * (1.0 - cfg.stop_pct)
    candidates = [current_stop, base]
    trail: float | None = None
    if cfg.trail_pct > 0:
        trail = trail_level(new_peak, cfg.trail_pct)
        candidates.append(trail)
    new_stop = max(candidates)
    update = abs(new_stop - current_stop) > 0 or new_peak != peak or be_now != be_active
    return RiskDecision(
        stop_loss=new_stop,
        trail_stop=trail,
        be_active=be_now,
        update_server=update,
    )


def _on_price_short(
    entry: float,
    trough: float,
    price: float,
    current_stop: float,
    cfg: ZakolConfig,
    be_active: bool,
) -> RiskDecision:
    """Зеркальный on_price для шорта: минимум цены, стоп сверху."""
    new_trough = min(trough, price)
    be_now = be_active or (
        cfg.be_trigger_pct > 0 and price <= entry * (1.0 - cfg.be_trigger_pct)
    )
    if be_now:
        base = entry * (1.0 - cfg.be_offset_pct)
    else:
        base = entry * (1.0 + cfg.stop_pct)
    candidates = [current_stop, base]
    trail: float | None = None
    if cfg.trail_pct > 0:
        trail = new_trough * (1.0 + cfg.trail_pct)
        candidates.append(trail)
    new_stop = min(candidates)
    update = (
        abs(new_stop - current_stop) > 0 or new_trough != trough or be_now != be_active
    )
    return RiskDecision(
        stop_loss=new_stop,
        trail_stop=trail,
        be_active=be_now,
        update_server=update,
    )


def should_kill(
    session_pnl: float,
    peak_session_pnl: float,
    deposit_usd: float,
    max_loss_usd: float,
    max_drawdown_pct: float,
) -> bool:
    """Kill-switch. Значение 0 = лимит выключен."""
    if max_loss_usd > 0 and session_pnl <= -abs(max_loss_usd):
        return True
    if max_drawdown_pct <= 0:
        return False
    drawdown = peak_session_pnl - session_pnl
    return drawdown >= deposit_usd * abs(max_drawdown_pct)


def partial_fill_ok(filled_qty: float, order_qty: float, partial_pct: float) -> bool:
    if order_qty <= 0:
        return False
    return fill_ratio(filled_qty, order_qty) >= partial_pct


def fill_ratio(filled_qty: float, order_qty: float) -> float:
    if order_qty <= 0:
        return 0.0
    return filled_qty / order_qty
