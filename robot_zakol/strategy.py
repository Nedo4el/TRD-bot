from __future__ import annotations

from dataclasses import dataclass

from core.bybit_client import Candle
from core.strategies import BaseStrategy, Signal


@dataclass
class ZakolConfig:
    """Параметры стратегии zakol (заполнить под ТЗ)."""

    ema_fast: int = 9
    ema_slow: int = 21
    min_candles: int = 30


class ZakolStrategy(BaseStrategy):
    """Zakol — стратегия с нуля (заглушка: всегда hold)."""

    name = "zakol"

    def __init__(self, cfg: ZakolConfig | None = None) -> None:
        self.cfg = cfg or ZakolConfig()

    def check_signal(self, candles: list[Candle]) -> Signal:
        """Вернуть сигнал по закрытым свечам.

        Args:
            candles: закрытые свечи от старых к новым.

        Returns:
            Signal: buy | sell | hold.
        """
        if len(candles) < self.cfg.min_candles:
            return Signal(
                action="hold",
                reason=f"мало свечей ({len(candles)}/{self.cfg.min_candles})",
            )
        return Signal(action="hold", reason="zakol: стратегия не реализована")
