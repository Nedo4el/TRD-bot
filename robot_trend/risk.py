"""Риск-хелперы robot_trend: размер позиции, гейты, kill-switch.

Чистые функции (без состояния и без зависимостей от config) — их же
тестируем отдельно.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RiskDecision:
    """Новый стоп после обновления по цене (двигается только в плюс)."""

    stop_loss: float
    be_active: bool
    update_server: bool


def on_price(
    entry: float,
    peak: float,
    price: float,
    current_stop: float,
    stop_pct: float,
    be_trigger_pct: float,
    be_offset_pct: float,
    trail_pct: float,
    be_active: bool = False,
    side: str = "long",
) -> RiskDecision:
    """Обновить peak/BE/trail по новой цене позиции.

    Активация BE и трейлинга одна: прибыль >= be_trigger_pct.
    trail_pct <= 0 — трейлинг выключен, be_trigger_pct <= 0 — BE выключен.
    Стоп никогда не уходит в сторону убытка (монотонность).
    """
    if side == "short":
        return _on_price_short(
            entry,
            peak,
            price,
            current_stop,
            stop_pct,
            be_trigger_pct,
            be_offset_pct,
            trail_pct,
            be_active,
        )
    new_peak = max(peak, price)
    be_now = be_active or (
        be_trigger_pct > 0 and price >= entry * (1.0 + be_trigger_pct)
    )
    base = entry * (1.0 + be_offset_pct) if be_now else entry * (1.0 - stop_pct)
    candidates = [current_stop, base]
    if trail_pct > 0 and be_now:
        candidates.append(new_peak * (1.0 - trail_pct))
    new_stop = max(candidates)
    update = new_stop != current_stop or be_now != be_active
    return RiskDecision(stop_loss=new_stop, be_active=be_now, update_server=update)


def _on_price_short(
    entry: float,
    trough: float,
    price: float,
    current_stop: float,
    stop_pct: float,
    be_trigger_pct: float,
    be_offset_pct: float,
    trail_pct: float,
    be_active: bool,
) -> RiskDecision:
    """Зеркальный on_price: минимум цены, стоп сверху."""
    new_trough = min(trough, price)
    be_now = be_active or (
        be_trigger_pct > 0 and price <= entry * (1.0 - be_trigger_pct)
    )
    base = entry * (1.0 - be_offset_pct) if be_now else entry * (1.0 + stop_pct)
    candidates = [current_stop, base]
    if trail_pct > 0 and be_now:
        candidates.append(new_trough * (1.0 + trail_pct))
    new_stop = min(candidates)
    update = new_stop != current_stop or be_now != be_active
    return RiskDecision(stop_loss=new_stop, be_active=be_now, update_server=update)


def entry_qty(
    fixed_qty: float,
    deposit_usd: float,
    position_pct: float,
    price: float,
) -> float:
    """Объём входа в базовом активе (округление вниз делает биржа).

    Args:
        fixed_qty: фикс. объём из QTY (0 — считаем от депозита).
        deposit_usd: депозит USDT.
        position_pct: доля депозита в %.
        price: цена актива.

    Returns:
        qty (>0); 0 — посчитать не удалось (нет цены или нулевой размер).
    """
    if fixed_qty > 0:
        return fixed_qty
    if price <= 0 or deposit_usd <= 0:
        return 0.0
    notional = deposit_usd * (position_pct / 100.0)
    return notional / price


def position_pnl(entry: float, price: float, qty: float, side: str) -> float:
    """PnL позиции: long растёт с ценой, short — падает."""
    if side == "short":
        return (entry - price) * qty
    return (price - entry) * qty


def slippage_ok(expected: float, actual: float, slippage_pct: float) -> bool:
    """Фактическая цена уложилась в допуск SLIPPAGE_PCT (0 = проверка выкл.)."""
    if slippage_pct <= 0:
        return True
    if expected <= 0 or actual <= 0:
        return False
    return abs(actual - expected) / expected <= slippage_pct


def cooldown_remaining(last_close_at: float, now: float, cooldown_sec: float) -> float:
    """Сколько секунд ещё действует пауза после закрытия (0 = паузы нет)."""
    if cooldown_sec <= 0 or last_close_at <= 0:
        return 0.0
    return max(last_close_at + cooldown_sec - now, 0.0)


def daily_stop_reason(
    day_pnl: float,
    day_trades: int,
    daily_loss_limit: float,
    max_trades_per_day: int,
) -> str | None:
    """Причина дневного стоп-крана (None — входы разрешены). 0 = лимит выкл.

    Блокирует новые входы, но не останавливает бота и не трогает позицию.
    """
    if daily_loss_limit > 0 and day_pnl <= -abs(daily_loss_limit):
        return "daily_loss"
    if max_trades_per_day > 0 and day_trades >= max_trades_per_day:
        return "max_trades"
    return None


def should_kill(
    session_pnl: float,
    peak_session_pnl: float,
    deposit_usd: float,
    max_loss_usd: float,
    max_drawdown_pct: float,
) -> bool:
    """Kill-switch по итогам сессии. Значение 0 = лимит выключен."""
    if max_loss_usd > 0 and session_pnl <= -abs(max_loss_usd):
        return True
    if max_drawdown_pct <= 0:
        return False
    drawdown = peak_session_pnl - session_pnl
    return drawdown >= deposit_usd * abs(max_drawdown_pct)
