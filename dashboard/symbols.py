"""Список монет Bybit USDT-M для окна выбора (с кэшем).

Публичный эндпоинт биржи — ключи не нужны. Кэш короткий, чтобы список
обновлялся без перезагрузки страницы и без лишних запросов.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from dashboard.fetcher import DashboardFetcher

logger = logging.getLogger(__name__)

# Время жизни кэша, сек.
CACHE_TTL_SEC = 60.0
# Порог оборота за 24ч — в списке только ликвидные пары.
MIN_TURNOVER_24H = 1_000_000.0


class SymbolsCache:
    """Кэш списка USDT-M фьючерсов Bybit."""

    def __init__(self, fetcher: DashboardFetcher | None = None) -> None:
        self._fetcher = fetcher or DashboardFetcher(api_key="", api_secret="")
        self._lock = asyncio.Lock()
        self._items: list[dict[str, Any]] = []
        self._fetched_at = 0.0

    async def get(self, force: bool = False) -> list[dict[str, Any]]:
        """Список монет (по обороту убыв.).

        Args:
            force: обновить кэш принудительно.
        """
        async with self._lock:
            fresh = (time.monotonic() - self._fetched_at) < CACHE_TTL_SEC
            if self._items and fresh and not force:
                return self._items
            items = await self._fetch()
            self._items = items
            self._fetched_at = time.monotonic()
            return items

    async def _fetch(self) -> list[dict[str, Any]]:
        """Забрать тикеры биржи и отфильтровать USDT-M пары."""
        tickers = await self._fetcher.get_tickers()
        items: list[dict[str, Any]] = []
        for ticker in tickers:
            symbol = str(ticker.get("symbol", ""))
            if not symbol.endswith("USDT"):
                continue
            try:
                turnover = float(ticker.get("turnover24h") or 0)
            except (TypeError, ValueError):
                continue
            if turnover < MIN_TURNOVER_24H:
                continue
            items.append(
                {
                    "symbol": symbol,
                    "last_price": _to_float(ticker.get("lastPrice")),
                    "turnover_24h": turnover,
                    "change_pct": _to_float(ticker.get("price24hPcnt")) * 100,
                }
            )
        items.sort(key=lambda item: item["turnover_24h"], reverse=True)
        logger.info("Список монет Bybit обновлён: %d пар", len(items))
        return items


def _to_float(value: Any) -> float:
    """Безопасное приведение к float."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
