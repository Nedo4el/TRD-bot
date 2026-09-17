from __future__ import annotations

from dataclasses import dataclass

from core.bybit_client import Candle
from core.strategies import BaseStrategy, Signal


@dataclass
class FlatConfig:
    """Параметры боковика после импульса."""

    poc_lookback: int = 60
    range_pct: float = 40.0
    impulse_min_pct: float = 15.0
    impulse_window: int = 5
    impulse_cooldown: int = 30


class FlatStrategy(BaseStrategy):
    """Боковик после импульса: POC-based коридор + buy/sell сигналы."""

    name = "flat"

    def __init__(self, cfg: FlatConfig | None = None):
        self.cfg = cfg or FlatConfig()
        self._impulse_candles_used = 0

    def check_signal(self, candles: list[Candle]) -> Signal:
        if len(candles) < self.cfg.poc_lookback:
            return Signal(action="hold", reason=f"мало свечей ({len(candles)})")

        closes = [c.close for c in candles]
        highs = [c.high for c in candles]
        lows = [c.low for c in candles]
        volumes = [c.volume for c in candles]

        impulse = self._detect_impulse(closes, highs, lows)
        if impulse is None:
            return Signal(action="hold", reason="нет импульса")

        impulse_idx, impulse_high, impulse_low = impulse
        after_impulse_candles = len(candles) - impulse_idx - 1

        if after_impulse_candles < self.cfg.impulse_cooldown:
            return Signal(
                action="hold",
                reason=(
                    f"импульс обнаружен, жду стабилизации "
                    f"({after_impulse_candles}/{self.cfg.impulse_cooldown} свечей)"
                ),
            )

        after_closes = closes[impulse_idx + 1:]
        after_volumes = volumes[impulse_idx + 1:]

        poc = self._calc_poc(
            after_closes[-self.cfg.poc_lookback:],
            after_volumes[-self.cfg.poc_lookback:],
        )

        if poc <= 0:
            return Signal(action="hold", reason="POC = 0")

        current_price = closes[-1]
        deviation = (current_price - poc) / poc * 100
        half_range = self.cfg.range_pct / 2

        if abs(deviation) > half_range:
            direction = "ВВЕРХ" if deviation > 0 else "ВНИЗ"
            return Signal(
                action="hold",
                reason=(
                    f"ТРЕНД {direction} | POC={poc:.4f} | цена={current_price:.4f} | "
                    f"откл={deviation:+.1f}% > ±{half_range:.0f}%"
                ),
            )

        buy_boundary = poc * (1 - half_range / 100)
        sell_boundary = poc * (1 + half_range / 100)
        entry_zone = half_range * 0.7

        if deviation <= -entry_zone:
            sl = poc * (1 - (half_range + 5) / 100)
            tp = poc * (1 + half_range / 100)
            return Signal(
                action="buy",
                reason=(
                    f"БОКОВИК BUY | POC={poc:.4f} | цена={current_price:.4f} | "
                    f"откл={deviation:+.1f}% | "
                    f"grid: buy={buy_boundary:.4f} sell={sell_boundary:.4f}"
                ),
                stop_loss=sl,
                take_profit=tp,
            )

        if deviation >= entry_zone:
            sl = poc * (1 + (half_range + 5) / 100)
            tp = poc * (1 - half_range / 100)
            return Signal(
                action="sell",
                reason=(
                    f"БОКОВИК SELL | POC={poc:.4f} | цена={current_price:.4f} | "
                    f"откл={deviation:+.1f}% | "
                    f"grid: buy={buy_boundary:.4f} sell={sell_boundary:.4f}"
                ),
                stop_loss=sl,
                take_profit=tp,
            )

        return Signal(
            action="hold",
            reason=(
                f"БОКОВИК | POC={poc:.4f} | цена={current_price:.4f} | "
                f"откл={deviation:+.1f}% (диапазон ±{half_range:.0f}%) | "
                f"GRID: buy={buy_boundary:.4f} sell={sell_boundary:.4f}"
            ),
        )

    def _detect_impulse(
        self,
        closes: list[float],
        highs: list[float],
        lows: list[float],
    ) -> tuple[int, float, float] | None:
        """Найти импульс: ≥ impulse_min_pct за impulse_window свечей.

        Ищет импульс, который завершился НЕ позже impulse_cooldown свечей назад,
        чтобы была стабилизация после него.

        Returns:
            Кортеж (impulse_end_idx, high, low) или None.
        """
        window = self.cfg.impulse_window
        cooldown = self.cfg.impulse_cooldown
        search_end = len(closes) - cooldown
        if search_end < window + 1:
            return None

        for end_idx in range(search_end - 1, window - 1, -1):
            start_idx = end_idx - window
            segment_highs = highs[start_idx : end_idx + 1]
            segment_lows = lows[start_idx : end_idx + 1]

            high = max(segment_highs)
            low = min(segment_lows)

            if low <= 0:
                continue

            move_pct = (high - low) / low * 100

            if move_pct >= self.cfg.impulse_min_pct:
                return (end_idx, high, low)

        return None

    @staticmethod
    def _calc_poc(closes: list[float], volumes: list[float]) -> float:
        """Point of Control — цена с максимальным объёмом."""
        if not closes or not volumes:
            return 0.0

        mn, mx = min(closes), max(closes)
        if mn == mx:
            return mn

        n_buckets = 50
        bucket_size = (mx - mn) / n_buckets
        buckets_vol = [0.0] * n_buckets

        for price, vol in zip(closes, volumes):
            idx = min(int((price - mn) / bucket_size), n_buckets - 1)
            buckets_vol[idx] += vol

        best_idx = buckets_vol.index(max(buckets_vol))
        return mn + (best_idx + 0.5) * bucket_size
