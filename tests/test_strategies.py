"""Тесты стратегий (заглушки — заполнять после написания логики)."""

from __future__ import annotations

from core.bybit_client import Candle
from core.strategies import Signal


# Фиксированные свечи для тестов
def _make_candles(n: int = 200, base_price: float = 1.0) -> list[Candle]:
    """Создать N одинаковых свечей."""
    return [
        Candle(
            open_time=i * 60000,
            open=base_price,
            high=base_price + 0.01,
            low=base_price - 0.01,
            close=base_price,
            volume=100.0,
        )
        for i in range(n)
    ]


class TestPlaceholders:
    """Заглушки — убрать после реализации стратегий."""

    def test_candles_exist(self) -> None:
        candles = _make_candles(10)
        assert len(candles) == 10

    def test_signal_default(self) -> None:
        sig = Signal(action="hold", reason="test")
        assert sig.action == "hold"
        assert sig.stop_loss is None
        assert sig.take_profit is None

    def test_signal_with_stops(self) -> None:
        sig = Signal(action="buy", reason="test", stop_loss=0.9, take_profit=1.1)
        assert sig.stop_loss == 0.9
        assert sig.take_profit == 1.1
