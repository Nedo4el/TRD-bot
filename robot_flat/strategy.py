from __future__ import annotations

from core.bybit_client import Candle
from core.strategies import BaseStrategy, Signal


class FlatStrategy(BaseStrategy):
    """Боковик на основе POC. Переписать с нуля."""

    name = "flat"

    def __init__(self, cfg: object = None) -> None:
        self.cfg = cfg
        self._poc: float | None = None

    def check_signal(self, candles: list[Candle]) -> Signal:
        return Signal(action="hold", reason="TODO: реализовать стратегию")