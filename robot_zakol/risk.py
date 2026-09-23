"""Риск-менеджмент: SL, trailing, BE, kill-switch, partial fill."""

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


def initial_stop(entry: float, stop_pct: float) -> float:
    return entry * (1.0 - stop_pct)


def should_be(entry: float, price: float, be_trigger_pct: float) -> bool:
    return price >= entry * (1.0 + be_trigger_pct)


def trail_level(peak: float, trail_pct: float) -> float:
    return peak * (1.0 - trail_pct)


def on_price(
    entry: float,
    peak: float,
    price: float,
    current_stop: float,
    cfg: ZakolConfig,
    be_active: bool,
) -> RiskDecision:
    """Обновить peak/SL/trail по новой цене. Монотонно вверх для SL."""
    new_peak = max(peak, price)
    be_now = be_active or should_be(entry, price, cfg.be_trigger_pct)
    trail = trail_level(new_peak, cfg.trail_pct)
    base = entry * (1.0 - cfg.stop_pct) if not be_now else entry
    new_stop = max(current_stop, base, trail)
    update = abs(new_stop - current_stop) > 0 or new_peak != peak or be_now != be_active
    return RiskDecision(
        stop_loss=new_stop,
        trail_stop=trail,
        be_active=be_now,
        update_server=update,
    )


def should_kill(
    consecutive_losses: int,
    session_pnl: float,
    max_loss_usd: float,
    max_consecutive_losses: int,
) -> bool:
    if session_pnl <= -abs(max_loss_usd):
        return True
    return consecutive_losses >= max_consecutive_losses


def partial_fill_ok(filled_qty: float, order_qty: float, partial_pct: float) -> bool:
    if order_qty <= 0:
        return False
    return fill_ratio(filled_qty, order_qty) >= partial_pct


def fill_ratio(filled_qty: float, order_qty: float) -> float:
    if order_qty <= 0:
        return 0.0
    return filled_qty / order_qty
