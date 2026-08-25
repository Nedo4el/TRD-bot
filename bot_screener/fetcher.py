"""Получение данных с Bybit: тикеры и свечи.

Все методы асинхронные — синхронные вызовы pybit оборачиваются
в asyncio.to_thread, чтобы не блокировать главный цикл скринера.
"""

from __future__ import annotations

import asyncio

from pybit.unified_trading import HTTP

from core.logger import get_logger

logger = get_logger(__name__)


class Fetcher:
    """Загрузчик данных с Bybit (REST API)."""

    def __init__(self, api_key: str, api_secret: str, testnet: bool = True) -> None:
        self._http = HTTP(
            testnet=testnet,
            api_key=api_key,
            api_secret=api_secret,
            recv_window=10000,
        )
        self._min_delay = 0.05  # 50 мс между запросами

    async def get_linear_tickers(self) -> list[dict]:
        """Получить все USDT-M фьючерсные тикеры.

        Returns:
            Список тикеров (raw dict от Bybit).
        """

        def _fetch() -> dict:
            return self._http.get_tickers(category="linear")

        try:
            resp = await asyncio.to_thread(_fetch)
            if resp["retCode"] == 0:
                return resp["result"]["list"]
            logger.error("get_tickers error: %s", resp["retMsg"])
            return []
        except (OSError, KeyError, ValueError) as e:
            logger.error("get_tickers exception: %s", e)
            return []

    async def get_klines(
        self,
        symbol: str,
        interval: str,
        limit: int = 100,
    ) -> list[dict]:
        """Получить свечи для символа.

        Args:
            symbol: торговая пара (например XRPUSDT).
            interval: таймфрейм (1, 5, 15, 60, ...).
            limit: количество свечей.

        Returns:
            Список свечей [{openTime, open, high, low, close, volume}, ...].
        """
        await asyncio.sleep(self._min_delay)

        def _fetch() -> dict:
            return self._http.get_kline(
                category="linear",
                symbol=symbol,
                interval=interval,
                limit=limit,
            )

        try:
            resp = await asyncio.to_thread(_fetch)
            if resp["retCode"] == 0:
                raw = resp["result"]["list"]
                # Bybit отдаёт: [openTime, open, high, low, close, volume, turnover]
                return [
                    {
                        "open_time": int(c[0]),
                        "open": float(c[1]),
                        "high": float(c[2]),
                        "low": float(c[3]),
                        "close": float(c[4]),
                        "volume": float(c[5]),
                        "turnover": float(c[6]) if len(c) > 6 else 0.0,
                    }
                    for c in raw
                ]
            logger.error("get_klines %s error: %s", symbol, resp["retMsg"])
            return []
        except (OSError, KeyError, ValueError) as e:
            logger.error("get_klines %s exception: %s", symbol, e)
            return []

    async def get_all_linear_symbols(self) -> list[str]:
        """Получить список всех USDT-M фьючерсных символов.

        Returns:
            Список символов (например ["BTCUSDT", "ETHUSDT", ...]).
        """
        tickers = await self.get_linear_tickers()
        symbols = []
        for t in tickers:
            symbol = t.get("symbol", "")
            # USDT-M фьючерсы: символ заканчивается на USDT
            if symbol.endswith("USDT"):
                symbols.append(symbol)
        return symbols

    async def get_filtered_symbols(
        self,
        min_turnover_24h: float = 5_000_000,
    ) -> list[tuple[str, float]]:
        """Отфильтровать символы по обороту за 24ч.

        Args:
            min_turnover_24h: минимальный оборот в USD.

        Returns:
            Список (symbol, turnover_24h) отсортированный по обороту.
        """
        tickers = await self.get_linear_tickers()
        filtered: list[tuple[str, float]] = []

        for t in tickers:
            symbol = t.get("symbol", "")
            if not symbol.endswith("USDT"):
                continue

            # Оборот за 24ч (turnover24h в ответе Bybit)
            turnover_str = t.get("turnover24h", "0")
            try:
                turnover = float(turnover_str)
            except (ValueError, TypeError):
                continue

            if turnover >= min_turnover_24h:
                filtered.append((symbol, turnover))

        # Сортировка по обороту (убывание)
        filtered.sort(key=lambda x: x[1], reverse=True)
        return filtered
