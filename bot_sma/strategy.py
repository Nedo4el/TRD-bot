"""Стратегия SMA crossover (пересечение скользящих средних).

Классика:
- быстрая SMA пересекает медленную снизу вверх -> сигнал на покупку;
- быстрая пересекает медленную сверху вниз -> сигнал на продажу.

Это самая простая стратегия — используйте её как шаблон
для написания своих.
"""

from __future__ import annotations

from core.bybit_client import Candle
from core.indicators import crossed_down, crossed_up, sma
from core.strategies import BaseStrategy, Signal


class SmaCrossStrategy(BaseStrategy):
    """Сигнал по пересечению быстрой и медленной SMA."""

    name = "sma"

    def __init__(self, fast_period: int = 7, slow_period: int = 25) -> None:
        """Args:
        fast_period: период быстрой средней (например, 7).
        slow_period: период медленной средней (например, 25).
        """
        if fast_period >= slow_period:
            raise ValueError(
                "FAST_MA_PERIOD должен быть меньше SLOW_MA_PERIOD "
                "(иначе пересечение средних не имеет смысла).",
            )
        self.fast_period = fast_period
        self.slow_period = slow_period

    def check_signal(self, candles: list[Candle]) -> Signal:
        """Золотое пересечение -> buy, мёртвое -> sell."""
        closes = [c.close for c in candles]
        if len(candles) < self.slow_period + 2:
            return Signal("hold", "недостаточно свечей для расчёта индикаторов")

        fast = sma(closes, self.fast_period)
        slow = sma(closes, self.slow_period)

        # Сравниваем значения средних на прошлой и текущей свечах
        prev_fast, prev_slow = fast[-2], slow[-2]
        now_fast, now_slow = fast[-1], slow[-1]

        if crossed_up(prev_fast, prev_slow, now_fast, now_slow):
            return Signal(
                "buy",
                f"золотое пересечение: SMA{self.fast_period}={now_fast:.2f} > "
                f"SMA{self.slow_period}={now_slow:.2f}",
            )
        if crossed_down(prev_fast, prev_slow, now_fast, now_slow):
            return Signal(
                "sell",
                f"мёртвое пересечение: SMA{self.fast_period}={now_fast:.2f} < "
                f"SMA{self.slow_period}={now_slow:.2f}",
            )
        return Signal(
            "hold",
            f"SMA{self.fast_period}={now_fast:.2f}, "
            f"SMA{self.slow_period}={now_slow:.2f}",
        )
