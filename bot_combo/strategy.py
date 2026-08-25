"""Стратегия «Комбинированный фильтр» (скальпинг, M1).

Три условия должны совпасть ОДНОВРЕМЕННО:

Вход в LONG (покупка):
1. Тренд вверх:   закрытие выше EMA(21) и EMA(50).
2. RSI(14) выходит из перепроданности: пересёк уровень 30 снизу вверх.
3. Не перекуплено: закрытие НИЖЕ верхней полосы Боллинджера.

Вход в SHORT (продажа) — зеркально:
1. Тренд вниз:    закрытие ниже EMA(21) и EMA(50).
2. RSI(14) выходит из перекупленности: пересёк уровень 70 сверху вниз.
3. Не перепродано: закрытие ВЫШЕ нижней полосы Боллинджера.

Выход: SL = 0.3% и TP = 0.5% от входа (задаются в .env бота).
Позиция всегда одна — движок сам не откроет новую до закрытия.
"""

from __future__ import annotations

from core.bybit_client import Candle
from core.indicators import bollinger, ema, rsi
from core.strategies import BaseStrategy, Signal


class ComboFilterStrategy(BaseStrategy):
    """EMA-тренд + выход RSI из зоны + фильтр полос Боллинджера."""

    name = "combo"

    def __init__(
        self,
        ema_fast: int = 21,
        ema_slow: int = 50,
        rsi_period: int = 14,
        rsi_oversold: float = 30.0,
        rsi_overbought: float = 70.0,
        bb_period: int = 20,
        bb_deviation: float = 2.0,
    ) -> None:
        """Все параметры настраиваются в .env бота (см. main.py)."""
        self.ema_fast = ema_fast
        self.ema_slow = ema_slow
        self.rsi_period = rsi_period
        self.rsi_oversold = rsi_oversold
        self.rsi_overbought = rsi_overbought
        self.bb_period = bb_period
        self.bb_deviation = bb_deviation

    # ------------------------------------------------------------------
    # Основная логика: возвращает +1 (long), -1 (short) или 0 (ждать)
    # ------------------------------------------------------------------
    def check_conditions(self, candles: list[Candle]) -> int:
        """Проверить все условия на последней ЗАКРЫТОЙ свече.

        Args:
            candles: закрытые свечи от старых к новым.

        Returns:
            1 — сигнал на покупку, -1 — на продажу, 0 — сигнала нет.
        """
        closes = [c.close for c in candles]
        need = max(self.ema_slow, self.bb_period, self.rsi_period + 1) + 3
        if len(candles) < need:
            return 0  # данных пока мало — ждём

        last_close = closes[-1]

        # --- Значения индикаторов на прошлой и текущей свечах ---
        ema_fast_vals = ema(closes, self.ema_fast)
        ema_slow_vals = ema(closes, self.ema_slow)
        rsi_vals = rsi(closes, self.rsi_period)
        upper_bb, _mid_bb, lower_bb = bollinger(
            closes, self.bb_period, self.bb_deviation
        )

        prev_rsi = rsi_vals[-2]
        now_rsi = rsi_vals[-1]
        now_upper_bb = upper_bb[-1]
        now_lower_bb = lower_bb[-1]

        # ================== Условия для LONG ==================
        # 1) Восходящий тренд: цена выше обеих EMA
        uptrend = last_close > ema_fast_vals[-1] and last_close > ema_slow_vals[-1]
        # 2) RSI выходит из перепроданности: пересёк 30 снизу вверх
        rsi_left_oversold = (
            prev_rsi < self.rsi_oversold and now_rsi >= self.rsi_oversold
        )
        # 3) Исключаем перекупленность: цена ниже верхней полосы
        not_overbought = last_close < now_upper_bb

        if uptrend and rsi_left_oversold and not_overbought:
            return 1

        # ================= Условия для SHORT ==================
        # 1) Нисходящий тренд: цена ниже обеих EMA
        downtrend = last_close < ema_fast_vals[-1] and last_close < ema_slow_vals[-1]
        # 2) RSI выходит из перекупленности: пересёк 70 сверху вниз
        rsi_left_overbought = (
            prev_rsi > self.rsi_overbought and now_rsi <= self.rsi_overbought
        )
        # 3) Исключаем перепроданность: цена выше нижней полосы
        not_oversold = last_close > now_lower_bb

        if downtrend and rsi_left_overbought and not_oversold:
            return -1

        return 0  # ни одно из сочетаний не совпало

    def check_signal(self, candles: list[Candle]) -> Signal:
        """Обёртка для движка: переводит число в Signal."""
        code = self.check_conditions(candles)
        if code == 1:
            return Signal(
                "buy",
                "тренд вверх (EMA21+50), RSI вышел из перепроданности, "
                "цена под верхней полосой BB",
            )
        if code == -1:
            return Signal(
                "sell",
                "тренд вниз (EMA21+50), RSI вышел из перекупленности, "
                "цена над нижней полосой BB",
            )
        return Signal("hold", "условия комбинированного фильтра не совпали")
