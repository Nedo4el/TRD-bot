"""Тесты robot_zakol (заглушка)."""

from __future__ import annotations

from core.bybit_client import Candle
from robot_zakol.strategy import ZakolConfig, ZakolStrategy


def _candles(n: int) -> list[Candle]:
    return [
        Candle(
            open_time=i * 60000,
            open=1.0,
            high=1.01,
            low=0.99,
            close=1.0,
            volume=10.0,
        )
        for i in range(n)
    ]


def test_hold_when_not_enough_candles() -> None:
    strategy = ZakolStrategy(ZakolConfig(min_candles=30))
    signal = strategy.check_signal(_candles(10))
    assert signal.action == "hold"
    assert "мало свечей" in signal.reason


def test_hold_when_not_implemented() -> None:
    strategy = ZakolStrategy(ZakolConfig(min_candles=30))
    signal = strategy.check_signal(_candles(50))
    assert signal.action == "hold"
    assert "не реализована" in signal.reason
