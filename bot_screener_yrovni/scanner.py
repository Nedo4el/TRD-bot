"""Volume Profile screener: POC по окнам, экстремумы окон и ATH/ATL.

POC — цена с максимальным накопленным оборотом за окно. Оборот свечи
делится поровну по ценовым корзинам её диапазона high-low: распределения
объёма внутри свечи Bybit не отдаёт, это приближение.

HIGH/LOW — экстремумы окна от месяца и длиннее, ATH/ATL — экстремумы всей
истории инструмента. Это не POC: накопленного объёма у них нет.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from core.logger import get_logger

logger = get_logger(__name__)

MSK = timezone(timedelta(hours=3))

# Окна анализа: имя -> (интервал Bybit, сколько последних свечей брать).
# 12h-3d — часовые, 7d-1m — 4-часовые, длиннее — дневные: так в каждом
# окне остаётся достаточно свечей, чтобы профиль из 100 корзин не был пустым.
WINDOWS: dict[str, tuple[str, int]] = {
    "12h": ("60", 12),
    "24h": ("60", 24),
    "3d": ("60", 72),
    "7d": ("240", 42),
    "14d": ("240", 84),
    "1m": ("240", 180),
    "3m": ("D", 90),
    "6m": ("D", 180),
    "1y": ("D", 365),
}

EXTREME_WINDOWS = ("1m", "3m", "6m", "1y")
MIN_CANDLES = 10

KIND_POC = "POC"
KIND_HIGH = "HIGH"
KIND_LOW = "LOW"
KIND_ATH = "ATH"
KIND_ATL = "ATL"


@dataclass
class Level:
    """Уровень в пределах порога приближения к текущей цене.

    ``distance_pct`` со знаком: плюс — уровень выше цены, минус — ниже.
    """

    kind: str
    period: str
    price: float
    distance_pct: float
    volume: float = 0.0


@dataclass
class VPSignal:
    """Результат сканирования одного символа."""

    symbol: str
    current_price: float
    levels: list[Level] = field(default_factory=list)
    scan_time: str = ""


def interval_limits() -> dict[str, int]:
    """Сколько свечей качать на каждый интервал, чтобы покрыть все окна."""
    limits: dict[str, int] = {}
    for interval, count in WINDOWS.values():
        limits[interval] = max(limits.get(interval, 0), count)
    return limits


def windows_from_series(
    by_interval: dict[str, list[dict[str, Any]]],
) -> dict[str, list[dict[str, Any]]]:
    """Нарезать окна из загруженных серий: одна серия на интервал."""
    result: dict[str, list[dict[str, Any]]] = {}
    for period, (interval, count) in WINDOWS.items():
        candles = by_interval.get(interval)
        if candles:
            result[period] = candles[-count:]
    return result


def _weight(candle: dict[str, Any]) -> float:
    """Вес свечи: оборот в USD, иначе объём в монетах."""
    turnover = float(candle.get("turnover") or 0.0)
    if turnover > 0:
        return turnover
    return float(candle.get("volume") or 0.0)


def build_volume_profile(
    candles: list[dict[str, Any]],
    num_bins: int = 100,
) -> list[tuple[float, float]]:
    """Построить Volume Profile — распределение оборота по ценовым корзинам."""
    if len(candles) < MIN_CANDLES:
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
        weight = _weight(candle)
        if weight <= 0 or c_high <= c_low:
            continue

        low_bin = max(0, min(int((c_low - price_min) / bin_width), num_bins - 1))
        high_bin = max(0, min(int((c_high - price_min) / bin_width), num_bins - 1))
        weight_per_bin = weight / (high_bin - low_bin + 1)

        for b in range(low_bin, high_bin + 1):
            volume_by_bin[b] = volume_by_bin.get(b, 0.0) + weight_per_bin

    result = [
        (price_min + idx * bin_width + bin_width / 2, vol)
        for idx, vol in volume_by_bin.items()
    ]
    result.sort(key=lambda item: item[1], reverse=True)
    return result


def find_poc(
    candles: list[dict[str, Any]],
    num_bins: int = 100,
) -> tuple[float, float] | None:
    """Найти POC — цену с максимальным накопленным оборотом."""
    profile = build_volume_profile(candles, num_bins)
    return profile[0] if profile else None


def window_extremes(candles: list[dict[str, Any]]) -> tuple[float, float] | None:
    """High и low окна."""
    if not candles:
        return None
    return max(c["high"] for c in candles), min(c["low"] for c in candles)


def _current_price(series: dict[str, list[dict[str, Any]]]) -> float:
    for period in WINDOWS:
        candles = series.get(period)
        if candles:
            return float(candles[-1]["close"])
    return 0.0


def _add_level(
    levels: list[Level],
    kind: str,
    period: str,
    price: float,
    volume: float,
    current_price: float,
    proximity_pct: float,
) -> None:
    if price <= 0:
        return
    distance = (price - current_price) / current_price * 100
    if abs(distance) > proximity_pct:
        return
    levels.append(Level(kind, period, price, round(distance, 2), volume))


def scan_symbol(
    symbol: str,
    series: dict[str, list[dict[str, Any]]],
    *,
    all_time: tuple[float, float] | None = None,
    num_bins: int = 100,
    proximity_pct: float = 10.0,
    max_price: float = 0.0,
) -> VPSignal:
    """Найти POC по окнам, экстремумы длинных окон и ATH/ATL.

    Args:
        symbol: торговая пара.
        series: {окно: свечи} — см. ``windows_from_series``.
        all_time: (ATH, ATL) по всей истории или None.
        num_bins: число ценовых корзин профиля.
        proximity_pct: порог приближения к цене в %, в обе стороны.
        max_price: максимальная цена монеты, 0 = без фильтра.

    Returns:
        VPSignal; уровни отсортированы по модулю расстояния до цены.
    """
    now = datetime.now(MSK).strftime("%H:%M")
    current_price = _current_price(series)
    if current_price <= 0:
        return VPSignal(symbol, 0.0, scan_time=now)
    if max_price > 0 and current_price > max_price:
        return VPSignal(symbol, 0.0, scan_time=now)

    levels: list[Level] = []
    for period, candles in series.items():
        if len(candles) < MIN_CANDLES:
            continue
        poc = find_poc(candles, num_bins)
        if poc is not None:
            _add_level(
                levels, KIND_POC, period, poc[0], poc[1],
                current_price, proximity_pct,
            )
        if period in EXTREME_WINDOWS:
            extremes = window_extremes(candles)
            if extremes is not None:
                high, low = extremes
                _add_level(
                    levels, KIND_HIGH, period, high, 0.0,
                    current_price, proximity_pct,
                )
                _add_level(
                    levels, KIND_LOW, period, low, 0.0,
                    current_price, proximity_pct,
                )

    if all_time is not None:
        ath, atl = all_time
        _add_level(
            levels, KIND_ATH, "all", ath, 0.0, current_price, proximity_pct,
        )
        _add_level(
            levels, KIND_ATL, "all", atl, 0.0, current_price, proximity_pct,
        )

    levels.sort(key=lambda level: abs(level.distance_pct))
    return VPSignal(symbol, current_price, levels, now)
