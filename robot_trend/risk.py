"""Риск-хелперы robot_trend: размер позиции, гейты, kill-switch.

Чистые функции (без состояния и без зависимостей от config) — их же
тестируем отдельно.
"""

from __future__ import annotations


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
