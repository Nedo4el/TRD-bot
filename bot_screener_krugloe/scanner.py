"""Скринер круглых чисел: поиск близости цены к психологическим уровням.

Круглые уровни:
  - До $1: 0.0050, 0.0100, 0.0150, ..., 0.9950 (шаг 0.005)
  - От $1: 1.00, 1.50, 2.00, 2.50, ... (шаг 0.50)

Сигнал: цена находится в пределах proximity_pct от круглого уровня.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from core.logger import get_logger

logger = get_logger(__name__)


@dataclass
class RoundLevel:
    """Один круглый уровень."""

    level: float
    distance_pct: float  # расстояние от цены до уровня в %
    side: str  # "above" (уровень выше цены) или "below" (уровень ниже цены)


@dataclass
class RoundSignal:
    """Результат анализа одного символа."""

    symbol: str
    timeframe: str
    price: float
    nearest_up: float
    nearest_down: float
    dist_up_pct: float
    dist_down_pct: float
    level: float
    proximity_pct: float
    turnover_24h: float = 0.0
    signal_time: str = ""


def generate_round_levels(max_price: float = 30.0) -> list[float]:
    """Генерация круглых уровней от 0 до max_price.

    До $1: шаг 0.050 (0.050, 0.100, 0.150, ..., 0.950)
    От $1: шаг 0.50 (1.00, 1.50, 2.00, ..., 30.00)

    Returns:
        Список круглых уровней, отсортированный по возрастанию.
    """
    levels: list[float] = []

    # До $1: шаг 0.050
    price = 0.050
    while price < 1.0:
        levels.append(round(price, 4))
        price += 0.050

    # От $1 и выше: шаг 0.50
    price = 1.0
    while price <= max_price:
        levels.append(round(price, 4))
        price += 0.50

    return levels


def find_nearest_levels(
    price: float,
    levels: list[float],
) -> tuple[float, float, float, float]:
    """Найти ближайший круглый уровень выше и ниже цены.

    Returns:
        (nearest_up, nearest_down, dist_up_pct, dist_down_pct).
    """
    if price <= 0 or not levels:
        return 0.0, 0.0, 0.0, 0.0

    nearest_up = 0.0
    nearest_down = 0.0

    for lvl in levels:
        if lvl <= price:
            nearest_down = lvl
        elif nearest_up == 0.0:
            nearest_up = lvl
            break

    dist_up = ((nearest_up - price) / price * 100) if nearest_up > 0 and price > 0 else 999.0
    dist_down = ((price - nearest_down) / price * 100) if nearest_down > 0 and price > 0 else 999.0

    return nearest_up, nearest_down, dist_up, dist_down


def scan_symbol(
    symbol: str,
    timeframe: str,
    price: float,
    *,
    levels: list[float] | None = None,
    proximity_pct: float = 5.0,
    max_price: float = 0.0,
    turnover_24h: float = 0.0,
) -> RoundSignal | None:
    """Проверить, находится ли цена рядом с круглым уровнем.

    Args:
        symbol: торговая пара.
        timeframe: таймфрейм.
        price: текущая цена.
        levels: список круглых уровней (None = генерация по умолчанию).
        proximity_pct: максимальное расстояние до уровня в %.
        max_price: максимальная цена (0 = без фильтра).
        turnover_24h: оборот за 24ч.

    Returns:
        RoundSignal или None если цена далеко от уровней.
    """
    if price <= 0:
        return None

    if max_price > 0 and price > max_price:
        return None

    if levels is None:
        levels = generate_round_levels(max_price=max(price * 2, 30.0))

    nearest_up, nearest_down, dist_up, dist_down = find_nearest_levels(price, levels)

    # Определяем ближайший уровень
    if dist_up <= dist_down:
        nearest = nearest_up
        proximity = dist_up
    else:
        nearest = nearest_down
        proximity = dist_down

    if proximity > proximity_pct:
        return None

    return RoundSignal(
        symbol=symbol,
        timeframe=timeframe,
        price=price,
        nearest_up=nearest_up,
        nearest_down=nearest_down,
        dist_up_pct=round(dist_up, 2),
        dist_down_pct=round(dist_down, 2),
        level=nearest,
        proximity_pct=round(proximity, 2),
        turnover_24h=turnover_24h,
        signal_time=datetime.now(timezone.utc).strftime("%H:%M:%S"),
    )
