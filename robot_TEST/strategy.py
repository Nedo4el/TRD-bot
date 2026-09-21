"""TEST стратегия — заглушка для быстрого прототипирования.

Как использовать:
1. Перепиши check_signal() на свою логику
2. Добавь параметры в TestConfig и .env
3. Запусти: py robot_TEST/main.py
"""

from __future__ import annotations

from dataclasses import dataclass

from core.bybit_client import Candle
from core.strategies import BaseStrategy, Signal


@dataclass
class TestConfig:
    """Параметры стратегии — добавляй свои."""

    lookback: int = 100


class TestStrategy(BaseStrategy):
    """Заглушка — замени на свою стратегию."""

    name = "test"

    def __init__(self, cfg: TestConfig | None = None):
        self.cfg = cfg or TestConfig()

    def check_signal(self, candles: list[Candle]) -> Signal:
        if len(candles) < self.cfg.lookback:
            return Signal(action="hold", reason=f"мало свечей ({len(candles)}/{self.cfg.lookback})")

        # === ТВОЙ КОД ЗДЕСЬ ===
        price = candles[-1].close
        return Signal(
            action="hold",
            reason=f"TEST | цена={price:.4f} | свечей={len(candles)}",
        )
