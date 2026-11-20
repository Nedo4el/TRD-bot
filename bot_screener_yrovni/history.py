"""Вся история инструмента для ATH/ATL: постраничная загрузка + кэш."""

from __future__ import annotations

import time
from typing import Any

from core.logger import get_logger

logger = get_logger(__name__)

PAGE_LIMIT = 1000
MAX_PAGES = 6
CACHE_TTL_SEC = 6 * 3600

_cache: dict[str, tuple[float, tuple[float, float]]] = {}


async def load_all_time(fetcher: Any, symbol: str) -> tuple[float, float] | None:
    """Вернуть (ATH, ATL) по всей доступной истории инструмента.

    Хай и лой меняются редко, поэтому результат кэшируется на 6 часов:
    первый скан платит за историю, следующие берут из памяти.

    Args:
        fetcher: объект с ``get_klines(symbol, interval, limit, end=...)``.
        symbol: торговая пара.

    Returns:
        (ATH, ATL) или None, если истории нет.
    """
    cached = _cache.get(symbol)
    if cached is not None and time.time() - cached[0] < CACHE_TTL_SEC:
        return cached[1]

    high = 0.0
    low: float | None = None
    end: int | None = None
    pages = 0

    while pages < MAX_PAGES:
        candles = await fetcher.get_klines(symbol, "D", PAGE_LIMIT, end=end)
        if not candles:
            break
        for candle in candles:
            high = max(high, float(candle["high"]))
            low = float(candle["low"]) if low is None else min(low, float(candle["low"]))
        pages += 1
        if len(candles) < PAGE_LIMIT:
            break
        end = int(candles[0]["open_time"]) - 1

    if high <= 0 or low is None or low <= 0:
        return None

    result = (high, low)
    _cache[symbol] = (time.time(), result)
    logger.info(
        "ATH/ATL %s: %.8g / %.8g (%d стр. истории)",
        symbol,
        high,
        low,
        pages,
    )
    return result
