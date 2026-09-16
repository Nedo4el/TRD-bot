from __future__ import annotations

from dataclasses import dataclass

from core.bybit_client import Candle
from core.strategies import BaseStrategy, Signal


@dataclass
class FlatConfig:
    """Параметры боковика на основе POC."""

    poc_lookback: int = 60
    range_pct: float = 40.0


class FlatStrategy(BaseStrategy):
    """Боковик: цена вокруг POC в пределах ±range_pct/2."""

    name = "flat"
    cfg: FlatConfig = FlatConfig()

    def __init__(self, cfg: FlatConfig | None = None):
        if cfg:
            self.cfg = cfg

    def check_signal(self, candles: list[Candle]) -> Signal:
        if len(candles) < 20:
            return Signal(action="hold", reason=f"мало свечей ({len(candles)})")

        closes = [c.close for c in candles]
        volumes = [c.volume for c in candles]
        highs = [c.high for c in candles]
        lows = [c.low for c in candles]

        # POC: цена с максимальным объёмом за lookback свечей
        poc = self._calc_poc(closes[-self.cfg.poc_lookback:], volumes[-self.cfg.poc_lookback:])

        current_price = closes[-1]
        deviation = (current_price - poc) / poc * 100 if poc > 0 else 0
        half_range = self.cfg.range_pct / 2

        is_flat = abs(deviation) <= half_range

        if is_grid := is_flat:
            # Грид: buy ниже POC, sell выше POC
            buy_price = poc * (1 - half_range / 100)
            sell_price = poc * (1 + half_range / 100)

            return Signal(
                action="hold",
                reason=(
                    f"БОКОВИК | POC={poc:.4f} | цена={current_price:.4f} | "
                    f"откл={deviation:+.1f}% (диапазон ±{half_range:.0f}%) | "
                    f"GRID: buy={buy_price:.4f} sell={sell_price:.4f}"
                ),
            )

        direction = "ВВЕРХ" if deviation > 0 else "ВНИЗ"
        return Signal(
            action="hold",
            reason=(
                f"ТРЕНД {direction} | POC={poc:.4f} | цена={current_price:.4f} | "
                f"откл={deviation:+.1f}% > ±{half_range:.0f}%"
            ),
        )

    @staticmethod
    def _calc_poc(closes: list[float], volumes: list[float]) -> float:
        """Point of Control — цена с максимальным объёмом."""
        if not closes or not volumes:
            return 0.0

        # Группируем по уровням (50 бакетов)
        mn, mx = min(closes), max(closes)
        if mn == mx:
            return mn

        n_buckets = 50
        bucket_size = (mx - mn) / n_buckets
        buckets_vol = [0.0] * n_buckets

        for price, vol in zip(closes, volumes):
            idx = min(int((price - mn) / bucket_size), n_buckets - 1)
            buckets_vol[idx] += vol

        # POC = центр бакета с максимальным объёмом
        best_idx = buckets_vol.index(max(buckets_vol))
        poc = mn + (best_idx + 0.5) * bucket_size

        return poc
