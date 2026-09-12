"""Volume Profile screener — POC + дневные уровни.

POC — цена с максимальным накопленным объемом за период.
Дневные уровни — открытие/закрытие вчерашней свечи.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from core.logger import get_logger

logger = get_logger(__name__)

# Периоды для POC-анализа: (таймфрейм, количество свечей)
POC_PERIODS = {
    "12h": ("60", 12),
    "24h": ("60", 24),
    "7d": ("D", 7),
    "30d": ("D", 30),
}


@dataclass
class POCLevel:
    """Один POC-уровень."""

    period: str
    poc_price: float
    poc_volume: float
    distance_pct: float = 0.0


@dataclass
class DailyLevel:
    """Дневной уровень (open/close вчера)."""

    level_type: str  # "OPEN" | "CLOSE"
    price: float
    distance_pct: float = 0.0


@dataclass
class VPSignal:
    """Результат сканирования одного символа."""

    symbol: str
    current_price: float
    poc_levels: list[POCLevel] = field(default_factory=list)
    daily_levels: list[DailyLevel] = field(default_factory=list)
    scan_time: str = ""


def build_volume_profile(
    candles: list[dict],
    num_bins: int = 100,
) -> list[tuple[float, float]]:
    """Построить Volume Profile — распределение объема по ценовым корзинам."""
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


def find_poc(
    candles: list[dict],
    num_bins: int = 100,
) -> tuple[float, float] | None:
    """Найти POC — цену с максимальным объемом."""
    profile = build_volume_profile(candles, num_bins)
    if not profile:
        return None

    poc_price, poc_volume = profile[0]
    return poc_price, poc_volume


def find_daily_levels(
    hourly_candles: list[dict],
    current_price: float,
    proximity_pct: float = 10.0,
) -> list[DailyLevel]:
    """Найти дневные уровни (High/Low) по UTC (03:00 МСК).

    Собирает дневную свечу из 1H баров: 00:00–23:00 UTC.
    Уровни — High и Low вчерашней свечи (шпильки, не тело).

    Args:
        hourly_candles: часовые свечи (последние ~48ч).
        current_price: текущая цена.
        proximity_pct: порог приближения в %.

    Returns:
        Список дневных уровней в пределах proximity_pct.
    """
    if len(hourly_candles) < 24:
        return []

    from collections import defaultdict
from datetime import datetime, timedelta, timezone

MSK = timezone(timedelta(hours=3))

    days: dict[str, list[dict]] = defaultdict(list)
    for c in hourly_candles:
        dt = datetime.fromtimestamp(c["open_time"] / 1000, tz=timezone.utc)
        day_key = dt.strftime("%Y-%m-%d")
        days[day_key].append(c)

    sorted_days = sorted(days.keys())
    if len(sorted_days) < 2:
        return []

    yesterday_key = sorted_days[-2]
    yesterday_candles = days[yesterday_key]

    day_high = max(c["high"] for c in yesterday_candles)
    day_low = min(c["low"] for c in yesterday_candles)

    levels = []
    for level_type, price in [("HIGH", day_high), ("LOW", day_low)]:
        if price <= 0:
            continue
        distance_pct = abs(current_price - price) / current_price * 100
        if distance_pct <= proximity_pct:
            levels.append(
                DailyLevel(
                    level_type=level_type,
                    price=price,
                    distance_pct=round(distance_pct, 2),
                )
            )

    return levels


def scan_symbol(
    symbol: str,
    candles_by_period: dict[str, list[dict]],
    hourly_candles: list[dict],
    *,
    num_bins: int = 100,
    proximity_pct: float = 10.0,
) -> VPSignal:
    """Просканировать один символ: POC по 4 периодам + дневные уровни.

    Args:
        symbol: торговая пара.
        candles_by_period: {период: список свечей}.
        hourly_candles: часовые свечи для построения дневных уровней.
        num_bins: количество ценовых корзин.
        proximity_pct: порог приближения в %.

    Returns:
        VPSignal с POC-уровнями и дневными уровнями.
    """
    now = datetime.now(MSK).strftime("%H:%M")

    # Берём текущую цену из любого периода
    for period_candles in candles_by_period.values():
        if period_candles:
            current_price = period_candles[-1]["close"]
            break
    else:
        return VPSignal(symbol=symbol, current_price=0.0, scan_time=now)

    if current_price <= 0:
        return VPSignal(symbol=symbol, current_price=0.0, scan_time=now)

    poc_levels = []

    for period, candles in candles_by_period.items():
        if not candles:
            continue

        poc = find_poc(candles, num_bins)
        if poc is None:
            continue

        poc_price, poc_volume = poc
        distance_pct = abs(current_price - poc_price) / current_price * 100

        if distance_pct <= proximity_pct:
            poc_levels.append(
                POCLevel(
                    period=period,
                    poc_price=poc_price,
                    poc_volume=poc_volume,
                    distance_pct=round(distance_pct, 2),
                )
            )

    # Дневные уровни из часовых свечей (UTC)
    daily_levels = find_daily_levels(hourly_candles, current_price, proximity_pct)

    # Сортируем по расстоянию
    poc_levels.sort(key=lambda x: x.distance_pct)
    daily_levels.sort(key=lambda x: x.distance_pct)

    return VPSignal(
        symbol=symbol,
        current_price=current_price,
        poc_levels=poc_levels,
        daily_levels=daily_levels,
        scan_time=now,
    )
