"""Volume Profile screener: уровни с максимальным накопленным объемом.

Строит гистограмму распределения объема по ценовым уровням,
находит топ-N уровней и отслеживает приближение цены к ним.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from core.logger import get_logger

logger = get_logger(__name__)


@dataclass
class VolumeLevel:
    """Один объемный уровень."""

    price: float
    total_volume: float
    level_type: str  # "SUPPORT" | "RESISTANCE"
    distance_pct: float = 0.0
    position: str = ""  # "ABOVE" | "BELOW"
    signal: str = ""  # "BREAKOUT_UP" | "BREAKOUT_DOWN" | "APPROACH"
    rank: int = 0


@dataclass
class VPSignal:
    """Результат сканирования одного символа."""

    symbol: str
    timeframe: str
    current_price: float
    levels: list[VolumeLevel] = field(default_factory=list)
    scan_time: str = ""


def build_volume_profile(
    candles: list[dict],
    num_bins: int = 100,
) -> list[tuple[float, float]]:
    """Построить Volume Profile — распределение объема по ценовым корзинам.

    Args:
        candles: список свечей [{open, high, low, close, volume}].
        num_bins: количество ценовых корзин.

    Returns:
        Список (price_level, total_volume), отсортированный по объему.
    """
    if len(candles) < 10:
        return []

    price_min = min(c["low"] for c in candles)
    price_max = max(c["high"] for c in candles)

    if price_max <= price_min:
        return []

    bin_width = (price_max - price_min) / num_bins
    volume_by_bin: dict[int, float] = {}

    for candle in candles:
        c_low = candle["low"]
        c_high = candle["high"]
        c_volume = candle["volume"]

        if c_volume <= 0 or c_high <= c_low:
            continue

        low_bin = max(0, min(int((c_low - price_min) / bin_width), num_bins - 1))
        high_bin = max(0, min(int((c_high - price_min) / bin_width), num_bins - 1))

        bins_covered = high_bin - low_bin + 1
        vol_per_bin = c_volume / bins_covered

        for b in range(low_bin, high_bin + 1):
            volume_by_bin[b] = volume_by_bin.get(b, 0.0) + vol_per_bin

    result = []
    for bin_idx, vol in volume_by_bin.items():
        price_level = price_min + bin_idx * bin_width + bin_width / 2
        result.append((price_level, vol))

    result.sort(key=lambda x: x[1], reverse=True)
    return result


def find_top_levels(
    candles: list[dict],
    num_levels: int = 10,
    num_bins: int = 100,
) -> list[VolumeLevel]:
    """Найти топ-N уровней с максимальным объемом.

    Args:
        candles: список свечей.
        num_levels: количество уровней.
        num_bins: количество ценовых корзин.

    Returns:
        Список VolumeLevel, отсортированный по рангу.
    """
    profile = build_volume_profile(candles, num_bins)
    if not profile:
        return []

    current_price = candles[-1]["close"]
    levels = []

    for rank, (price, volume) in enumerate(profile[:num_levels], 1):
        level_type = "SUPPORT" if price < current_price else "RESISTANCE"
        distance_pct = abs(current_price - price) / current_price * 100
        position = "ABOVE" if current_price > price else "BELOW"

        levels.append(
            VolumeLevel(
                price=price,
                total_volume=volume,
                level_type=level_type,
                distance_pct=round(distance_pct, 2),
                position=position,
                rank=rank,
            )
        )

    return levels


def detect_signals(
    levels: list[VolumeLevel],
    proximity_pct: float = 5.0,
) -> list[VolumeLevel]:
    """Определить сигналы для уровней в пределах proximity_pct.

    Args:
        levels: список уровней.
        proximity_pct: порог приближения в процентах.

    Returns:
        Список уровней с определенным сигналом.
    """
    signals = []

    for level in levels:
        if level.distance_pct <= proximity_pct:
            if level.position == "BELOW" and level.level_type == "RESISTANCE":
                level.signal = "BREAKOUT_UP"
            elif level.position == "ABOVE" and level.level_type == "SUPPORT":
                level.signal = "BREAKOUT_DOWN"
            elif level.position == "BELOW" and level.level_type == "SUPPORT":
                level.signal = "APPROACH_SUPPORT"
            elif level.position == "ABOVE" and level.level_type == "RESISTANCE":
                level.signal = "APPROACH_RESISTANCE"
            signals.append(level)

    return signals


def scan_symbol(
    symbol: str,
    timeframe: str,
    candles: list[dict],
    *,
    num_levels: int = 10,
    num_bins: int = 100,
    proximity_pct: float = 5.0,
) -> VPSignal:
    """Просканировать один символ на приближение к объемным уровням.

    Args:
        symbol: торговая пара.
        timeframe: таймфрейм.
        candles: список свечей.
        num_levels: количество уровней.
        num_bins: количество ценовых корзин.
        proximity_pct: порог приближения в %.

    Returns:
        VPSignal с уровнями и сигналами.
    """
    now = datetime.now(timezone.utc).strftime("%H:%M")

    if len(candles) < 50:
        return VPSignal(
            symbol=symbol, timeframe=timeframe, current_price=0.0, scan_time=now
        )

    current_price = candles[-1]["close"]
    levels = find_top_levels(candles, num_levels, num_bins)
    signals = detect_signals(levels, proximity_pct)

    return VPSignal(
        symbol=symbol,
        timeframe=timeframe,
        current_price=current_price,
        levels=signals,
        scan_time=now,
    )
