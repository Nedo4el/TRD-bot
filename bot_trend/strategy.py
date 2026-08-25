from __future__ import annotations

from core.bybit_client import Candle
from core.strategies import BaseStrategy, Signal


class TrendStrategy(BaseStrategy):
    """Тренд (следование за трендом)."""

    name = "trend"

    def check_signal(self, candles: list[Candle]) -> Signal:
        # TODO: реализовать стратегию
        return Signal(action="hold", reason="strategy not implemented")
