"""Получение данных с Bybit: тикеры и свечи с пагинацией.

Все методы асинхронные — синхронные вызовы pybit оборачиваются
в asyncio.to_thread, чтобы не блокировать главный цикл скринера.
"""

from __future__ import annotations

import asyncio
from typing import Self

from pybit.unified_trading import HTTP

from core.logger import get_logger
from core.metrics import ScreenerMetrics, ScreenerMetricsTimer

logger = get_logger(__name__)


class _noop_ctx:
    """Контекстный менеджер-заглушка когда метрики не переданы."""

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        pass


class Fetcher:
    """Загрузчик данных с Bybit (REST API) с поддержкой пагинации."""

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        testnet: bool = True,
        metrics: ScreenerMetrics | None = None,
    ) -> None:
        self._http = HTTP(
            testnet=testnet,
            api_key=api_key,
            api_secret=api_secret,
            recv_window=10000,
        )
        self._min_delay = 0.05
        self._max_retries = 3
        self._retry_delay = 1.0
        self.metrics = metrics

    async def get_linear_tickers(self) -> list[dict]:
        """Получить все USDT-M фьючерсные тикеры."""
        for attempt in range(self._max_retries):
            await asyncio.sleep(self._min_delay * (attempt + 1))

            def _fetch() -> dict:
                return self._http.get_tickers(category="linear")  # type: ignore[no-any-return]

            try:
                with (
                    ScreenerMetricsTimer(self.metrics) if self.metrics else _noop_ctx()
                ):
                    resp = await asyncio.to_thread(_fetch)
                if resp["retCode"] == 0:
                    return resp["result"]["list"]  # type: ignore[no-any-return]
                logger.error("get_tickers error: %s", resp["retMsg"])
                if self.metrics:
                    self.metrics.record_api_error()
                return []
            except (OSError, ValueError, KeyError) as e:
                if self.metrics:
                    self.metrics.record_api_error()
                if attempt < self._max_retries - 1:
                    delay = self._retry_delay * (2**attempt)
                    logger.warning(
                        "get_tickers attempt %d failed, retrying in %.1fs: %s",
                        attempt + 1, delay, e,
                    )
                    await asyncio.sleep(delay)
                else:
                    logger.error("get_tickers exception after %d attempts: %s", self._max_retries, e)
                    return []
        return []

    async def get_klines(
        self,
        symbol: str,
        interval: str,
        limit: int = 100,
    ) -> list[dict]:
        """Получить свечи для символа (последние N свечей)."""
        for attempt in range(self._max_retries):
            await asyncio.sleep(self._min_delay * (attempt + 1))

            def _fetch() -> dict:
                return self._http.get_kline(  # type: ignore[no-any-return]
                    category="linear",
                    symbol=symbol,
                    interval=interval,
                    limit=limit,
                )

            try:
                with (
                    ScreenerMetricsTimer(self.metrics) if self.metrics else _noop_ctx()
                ):
                    resp = await asyncio.to_thread(_fetch)
                if resp["retCode"] == 0:
                    raw = resp["result"]["list"]
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
                if self.metrics:
                    self.metrics.record_api_error()
                return []
            except (OSError, ValueError, KeyError) as e:
                if self.metrics:
                    self.metrics.record_api_error()
                if attempt < self._max_retries - 1:
                    delay = self._retry_delay * (2**attempt)
                    logger.warning(
                        "get_klines %s attempt %d failed, retrying in %.1fs: %s",
                        symbol, attempt + 1, delay, e,
                    )
                    await asyncio.sleep(delay)
                else:
                    logger.error("get_klines %s exception after %d attempts: %s", symbol, self._max_retries, e)
                    return []
        return []

    async def get_klines_range(
        self,
        symbol: str,
        interval: str,
        start_ms: int,
        end_ms: int,
        limit: int = 1000,
    ) -> list[dict]:
        """Получить свечи за период с пагинацией (end -> start, newest first).

        Bybit возвращает свечи от newest к oldest. Мы идём от end_ms к start_ms,
        на каждой итерации берём limit свечей и двигаем end_ms назад.
        """
        all_candles: list[dict] = []
        current_end = end_ms

        while current_end > start_ms:
            for attempt in range(self._max_retries):
                await asyncio.sleep(self._min_delay * (attempt + 1))

                def _fetch() -> dict:
                    return self._http.get_kline(  # type: ignore[no-any-return]
                        category="linear",
                        symbol=symbol,
                        interval=interval,
                        limit=limit,
                        end=current_end,
                        start=start_ms,
                    )

                try:
                    with (
                        ScreenerMetricsTimer(self.metrics) if self.metrics else _noop_ctx()
                    ):
                        resp = await asyncio.to_thread(_fetch)
                    if resp["retCode"] == 0:
                        raw = resp["result"]["list"]
                        if not raw:
                            return all_candles

                        for c in raw:
                            all_candles.append({
                                "open_time": int(c[0]),
                                "open": float(c[1]),
                                "high": float(c[2]),
                                "low": float(c[3]),
                                "close": float(c[4]),
                                "volume": float(c[5]),
                                "turnover": float(c[6]) if len(c) > 6 else 0.0,
                            })

                        # oldest candle в батче — двигаем end_ms
                        oldest_time = int(raw[-1][0])
                        if oldest_time >= current_end:
                            break  # нет прогресса, выходим
                        current_end = oldest_time - 1
                        break
                    else:
                        logger.error("get_klines_range %s error: %s", symbol, resp["retMsg"])
                        if self.metrics:
                            self.metrics.record_api_error()
                        return all_candles
                except (OSError, ValueError, KeyError) as e:
                    if self.metrics:
                        self.metrics.record_api_error()
                    if attempt < self._max_retries - 1:
                        delay = self._retry_delay * (2**attempt)
                        logger.warning(
                            "get_klines_range %s attempt %d failed: %s",
                            symbol, attempt + 1, e,
                        )
                        await asyncio.sleep(delay)
                    else:
                        logger.error("get_klines_range %s failed: %s", symbol, e)
                        return all_candles
            else:
                break

        return all_candles

    async def get_all_linear_symbols(self) -> list[str]:
        """Получить список всех USDT-M фьючерсных символов."""
        tickers = await self.get_linear_tickers()
        return [t.get("symbol", "") for t in tickers if t.get("symbol", "").endswith("USDT")]

    async def get_filtered_symbols(
        self,
        min_turnover_24h: float = 5_000_000,
    ) -> list[tuple[str, float]]:
        """Отфильтровать символы по обороту за 24ч."""
        tickers = await self.get_linear_tickers()
        filtered: list[tuple[str, float]] = []

        for t in tickers:
            symbol = t.get("symbol", "")
            if not symbol.endswith("USDT"):
                continue
            turnover_str = t.get("turnover24h", "0")
            try:
                turnover = float(turnover_str)
            except (ValueError, TypeError):
                continue
            if turnover >= min_turnover_24h:
                filtered.append((symbol, turnover))

        filtered.sort(key=lambda x: x[1], reverse=True)
        return filtered
