from __future__ import annotations

from core.bybit_client import Candle
from core.strategies import BaseStrategy, Signal


class FlatStrategy(BaseStrategy):
    """Боковик — тестовая стратегия для проверки ордеров."""

    name = "flat"
    _counter: int = 0

    def check_signal(self, candles: list[Candle]) -> Signal:
        if len(candles) < 3:
            return Signal(action="hold", reason="недостаточно свечей")

        self._counter += 1

        # Каждые 3 закрытые свечи — тестовый buy-ордер
        if self._counter >= 3:
            self._counter = 0
            price = candles[-1].close
            return Signal(
                action="buy",
                reason=f"тестовый ордер @ {price:.2f}",
                stop_loss=price * 0.995,
                take_profit=price * 1.01,
            )

        return Signal(action="hold", reason=f"ожидание ({self._counter}/3)")
