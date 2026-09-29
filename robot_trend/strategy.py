"""Стратегия robot_trend — СТАБ (каркас без логики входа/выхода).

ПОТОМ ЗАПОЛНИМ:
- ``decide()`` — хук живого цикла (order_flow); пока всегда None = hold,
  сделок нет, но вся техника вокруг работает и тестируется.
- ``check_signal()`` — хук корневого ``backtest.py --strategy trend``;
  пока всегда hold (бэктест отработает с нулём сделок).
"""

from __future__ import annotations

from dataclasses import dataclass

from core.bybit_client import Candle
from core.strategies import BaseStrategy, Signal
from robot_trend.state import OpenPosition


@dataclass(frozen=True)
class Instruction:
    """Решение стратегии. Исполняет технический слой (order_flow)."""

    action: str  # "enter" | "exit"
    side: str = ""  # "long" | "short" (для enter)
    stop: float | None = None  # уровень SL (для enter)
    take: float | None = None  # уровень TP (для enter)
    reason: str = ""


class TrendStrategy(BaseStrategy):
    """Трендовая стратегия (стаб). Логика — потом заполним."""

    name = "trend"

    def check_signal(self, candles: list[Candle]) -> Signal:
        """Хук старого бэктеста: пока всегда hold.

        Args:
            candles: свечи от старых к новым.

        Returns:
            Signal(action="hold") — решений не принимаем.
        """
        return Signal(action="hold", reason="потом заполним")

    def decide(
        self,
        candles: list[Candle],
        position: OpenPosition | None,
    ) -> Instruction | None:
        """Решение живого цикла (ПОТОМ ЗАПОЛНИМ).

        Args:
            candles: окно свечей (старые → новые, последняя закрытая).
            position: открытая позиция или None.

        Returns:
            Instruction("enter"/"exit") либо None = hold.
        """
        # ПОТОМ ЗАПОЛНИМ: логика входа/выхода по свечам.
        return None
