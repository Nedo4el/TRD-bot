"""Стратегия «Адаптивная от волатильности» (ATR-стопы).

Направление задаёт большой тренд:
- цена выше SMA(200)  -> разрешены ТОЛЬКО покупки (long);
- цена ниже SMA(200)  -> разрешены ТОЛЬКО продажи (short).

Вход — пересечение EMA(5) и EMA(13):
- золотое пересечение (5 вверх через 13) -> buy;
- мёртвое пересечение (5 вниз через 13)  -> sell.

Главная фишка — стопы зависят от волатильности ATR(14):
- Stop Loss   = 1.5 * ATR
- Take Profit = 2.5 * ATR
Чем спокойнее рынок, тем ближе стопы; чем бурнее — тем шире.

Два фильтра-ограничителя:
1. ATR больше средней волатильности за день -> рынок шумный, пропускаем.
2. ATR меньше 0.1% цены -> спред съест прибыль, пропускаем.
"""

from __future__ import annotations

from core.bybit_client import Candle
from core.indicators import atr, crossed_down, crossed_up, ema, sma
from core.strategies import BaseStrategy, Signal


class AdaptiveAtrStrategy(BaseStrategy):
    """Тренд по SMA200, вход по EMA5/13, стопы от ATR."""

    name = "adaptive"

    def __init__(
        self,
        trend_period: int = 200,
        ema_fast: int = 5,
        ema_slow: int = 13,
        atr_period: int = 14,
        atr_sl_mult: float = 1.5,
        atr_tp_mult: float = 2.5,
        atr_avg_lookback: int = 1440,
        min_atr_pct: float = 0.1,
    ) -> None:
        """Все параметры настраиваются в .env бота (см. main.py)."""
        self.trend_period = trend_period
        self.ema_fast = ema_fast
        self.ema_slow = ema_slow
        self.atr_period = atr_period
        self.atr_sl_mult = atr_sl_mult
        self.atr_tp_mult = atr_tp_mult
        # Сколько последних свечей считать "средней волатильностью за день"
        # (1440 свечей = одни сутки на таймфрейме M1)
        self.atr_avg_lookback = atr_avg_lookback
        self.min_atr_pct = min_atr_pct

    def check_signal(self, candles: list[Candle]) -> Signal:
        """Пересечение EMA по тренду + адаптивные SL/TP от ATR."""
        closes = [c.close for c in candles]
        if len(candles) < self.trend_period + 3:
            return Signal("hold", "недостаточно свечей для расчёта индикаторов")

        price = closes[-1]

        # --- 1. Фильтр направления по большому тренду ---
        sma_trend = sma(closes, self.trend_period)[-1]
        long_allowed = price > sma_trend  # выше SMA200 — только покупки
        short_allowed = price < sma_trend  # ниже SMA200 — только продажи

        # --- 2. Пересечение EMA на последней свече ---
        ema_fast_vals = ema(closes, self.ema_fast)
        ema_slow_vals = ema(closes, self.ema_slow)
        golden_cross = crossed_up(
            ema_fast_vals[-2],
            ema_slow_vals[-2],
            ema_fast_vals[-1],
            ema_slow_vals[-1],
        )
        death_cross = crossed_down(
            ema_fast_vals[-2],
            ema_slow_vals[-2],
            ema_fast_vals[-1],
            ema_slow_vals[-1],
        )

        if not (golden_cross or death_cross):
            return Signal(
                "hold",
                f"нет пересечения EMA{self.ema_fast}/{self.ema_slow}",
            )
        if golden_cross and not long_allowed:
            return Signal("hold", "buy-пересечение запрещено: цена ниже SMA200")
        if death_cross and not short_allowed:
            return Signal("hold", "sell-пересечение запрещено: цена выше SMA200")

        # --- 3. Фильтры волатильности ---
        highs = [c.high for c in candles]
        lows = [c.low for c in candles]
        atr_vals = atr(highs, lows, closes, self.atr_period)
        now_atr = atr_vals[-1]
        if now_atr <= 0:
            return Signal("hold", "ATR не рассчитан")

        # Слишком тихо: ATR меньше 0.1% цены — спред съест прибыль
        if now_atr < price * self.min_atr_pct / 100.0:
            return Signal(
                "hold",
                f"ATR слишком низкий ({now_atr:.6g} < "
                f"{self.min_atr_pct}% цены) — спред съест прибыль",
            )

        # Слишком бурно: ATR выше средней волатильности за день — шум
        lookback = atr_vals[-self.atr_avg_lookback :]
        avg_atr = sum(lookback) / len(lookback)
        if now_atr > avg_atr:
            return Signal(
                "hold",
                f"ATR {now_atr:.6g} выше среднего {avg_atr:.6g} — рынок шумный",
            )

        # --- 4. Адаптивные SL/TP от текущего ATR ---
        action = "buy" if golden_cross else "sell"
        sl_distance = self.atr_sl_mult * now_atr
        tp_distance = self.atr_tp_mult * now_atr
        if action == "buy":
            stop_loss = round(price - sl_distance, 8)
            take_profit = round(price + tp_distance, 8)
            reason = (
                f"золотое пересечение EMA{self.ema_fast}/{self.ema_slow}, "
                f"SL=1.5*ATR={sl_distance:.6g}, TP=2.5*ATR={tp_distance:.6g}"
            )
        else:
            stop_loss = round(price + sl_distance, 8)
            take_profit = round(price - tp_distance, 8)
            reason = (
                f"мёртвое пересечение EMA{self.ema_fast}/{self.ema_slow}, "
                f"SL=1.5*ATR={sl_distance:.6g}, TP=2.5*ATR={tp_distance:.6g}"
            )
        return Signal(action, reason, stop_loss=stop_loss, take_profit=take_profit)
