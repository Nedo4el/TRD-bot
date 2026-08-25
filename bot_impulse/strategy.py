from __future__ import annotations

from core.bybit_client import Candle
from core.strategies import BaseStrategy, Signal


class ImpulseStrategy(BaseStrategy):
    """Импульс (моментум, быстрые входы)."""

    name = "impulse"

    def check_signal(self, candles: list[Candle]) -> Signal:
        # TODO: реализовать стратегию
        return Signal(action="hold", reason="strategy not implemented")
