from __future__ import annotations

from dataclasses import dataclass, field

from core.bybit_client import Candle
from core.strategies import BaseStrategy, Signal


@dataclass
class FlatConfig:
    """Минимальные параметры поиска боковика."""

    atr_period: int = 14
    atr_lookback: int = 60
    atr_decrease_pct: float = 0.7
    range_pct: float = 15.0
    min_candles: int = 100
    bb_squeeze_threshold: float = 30.0


class FlatStrategy(BaseStrategy):
    """Боковик — поиск консолидации после импульса."""

    name = "flat"
    cfg: FlatConfig = FlatConfig()

    def __init__(self, cfg: FlatConfig | None = None):
        if cfg:
            self.cfg = cfg

    def check_signal(self, candles: list[Candle]) -> Signal:
        if len(candles) < self.cfg.min_candles:
            return Signal(action="hold", reason=f"мало свечей ({len(candles)}/{self.cfg.min_candles})")

        closes = [c.close for c in candles]
        highs = [c.high for c in candles]
        lows = [c.low for c in candles]

        # ATR текущий и N свечей назад
        atr_now = self._atr(highs, lows, closes, self.cfg.atr_period)

        if len(candles) >= self.cfg.atr_lookback + self.cfg.atr_period:
            atr_prev = self._atr(
                highs[-self.cfg.atr_lookback:],
                lows[-self.cfg.atr_lookback:],
                closes[-self.cfg.atr_lookback:],
                self.cfg.atr_period,
            )
        else:
            atr_prev = atr_now

        # 1. ATR снижается
        atr_decreasing = atr_now < atr_prev * self.cfg.atr_decrease_pct

        # 2. Цена в коридоре
        avg_price = sum(closes[-60:]) / min(60, len(closes))
        current_price = closes[-1]
        deviation_pct = abs(current_price - avg_price) / avg_price * 100
        in_range = deviation_pct <= self.cfg.range_pct

        # 3. BB Width сжимается
        recent_high = max(highs[-20:])
        recent_low = min(lows[-20:])
        bb_width_pct = (recent_high - recent_low) / avg_price * 100
        bb_squeezed = bb_width_pct < self.cfg.bb_squeeze_threshold

        is_flat = atr_decreasing and in_range and bb_squeezed

        if is_flat:
            grid_step = atr_now * 0.5
            buy_price = current_price - grid_step
            sell_price = current_price + grid_step

            return Signal(
                action="hold",
                reason=(
                    f"БОКОВИК | ATR: {atr_now:.4f} < {atr_prev:.4f}*{self.cfg.atr_decrease_pct} | "
                    f"откл: {deviation_pct:.1f}% < {self.cfg.range_pct}% | "
                    f"BB: {bb_width_pct:.1f}% | "
                    f"GRID: buy={buy_price:.4f} sell={sell_price:.4f} step={grid_step:.4f}"
                ),
            )

        return Signal(
            action="hold",
            reason=(
                f"ТРЕНД | ATR: {atr_now:.4f} (prev={atr_prev:.4f}) | "
                f"откл: {deviation_pct:.1f}% | BB: {bb_width_pct:.1f}%"
            ),
        )

    @staticmethod
    def _atr(highs: list[float], lows: list[float], closes: list[float], period: int) -> float:
        if len(closes) < period + 1:
            return 0.0

        trs = []
        for i in range(1, len(closes)):
            tr = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )
            trs.append(tr)

        return sum(trs[-period:]) / period
