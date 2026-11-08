"""Общий fetcher дашборда: тикеры, свечи, спред, пагинация.

Защищает планировщик от 403/429 (CloudFront-бан Bybit при стартовом залпе):
повторные попытки с экспоненциальным backoff.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from pybit.unified_trading import HTTP

from core.logger import get_logger

logger = get_logger(__name__)

_RETRYABLE_STATUS = (403, 429)


class DashboardFetcher:
    """REST-клиент Bybit v5 для дашборда (read-only, без пула соединений)."""

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        testnet: bool = False,
        min_delay: float = 0.05,
        max_retries: int = 3,
        retry_delay: float = 1.0,
    ) -> None:
        self._http = HTTP(
            testnet=testnet,
            api_key=api_key,
            api_secret=api_secret,
            recv_window=10000,
        )
        self._min_delay = min_delay
        self._max_retries = max_retries
        self._retry_delay = retry_delay

    @staticmethod
    def _is_rate_limited(exc: BaseException) -> bool:
        """Распознать 403/429 (бан CloudFront / rate-limit)."""
        status = getattr(exc, "status_code", None)
        if isinstance(status, int) and status in _RETRYABLE_STATUS:
            return True
        text = str(exc).lower()
        return any(code in text for code in ("403", "429"))

    async def _fetch(
        self,
        func: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> dict:
        """Выполнить синхронный запрос pybit с ретраями на 403/429.

        Returns:
            Сырой ответ API (dict).

        Raises:
            RuntimeError: если все попытки исчерпаны.
        """
        last: BaseException | None = None
        for attempt in range(1, self._max_retries + 1):
            await asyncio.sleep(self._min_delay * attempt)
            try:
                return await asyncio.to_thread(func, *args, **kwargs)
            except Exception as exc:  # noqa: BLE001
                last = exc
                if self._is_rate_limited(exc) and attempt < self._max_retries:
                    delay = self._retry_delay * (2 ** (attempt - 1))
                    logger.warning(
                        "Bybit rate-limit (%s), ретрай через %.1fс (попытка %d/%d)",
                        type(exc).__name__,
                        delay,
                        attempt,
                        self._max_retries,
                    )
                    await asyncio.sleep(delay)
                    continue
                break
        if last is not None:
            raise RuntimeError(f"Bybit API error: {last}") from last
        raise RuntimeError("Bybit API error: unknown")

    async def get_tickers(self) -> list[dict]:
        """Все USDT-M фьючерсные тикеры (пустой список при сбое)."""
        func = self._http.get_tickers
        try:
            resp = await self._fetch(func, category="linear")
        except RuntimeError as exc:
            logger.error("get_tickers: %s", exc)
            return []
        if resp.get("retCode") != 0:
            logger.error("get_tickers error: %s", resp.get("retMsg"))
            return []
        return resp["result"]["list"]  # type: ignore[no-any-return]

    async def get_klines(
        self,
        symbol: str,
        interval: str,
        limit: int = 100,
    ) -> list[dict]:
        """Свечи символа (от старых к новым, пустой список при сбое)."""
        func = self._http.get_kline
        try:
            resp = await self._fetch(
                func,
                category="linear",
                symbol=symbol,
                interval=interval,
                limit=limit,
            )
        except RuntimeError as exc:
            logger.warning("get_klines %s: %s", symbol, exc)
            return []
        if resp.get("retCode") != 0:
            logger.error("get_klines %s error: %s", symbol, resp.get("retMsg"))
            return []
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

    async def get_spread(self, symbol: str) -> float:
        """Спред (Ask - Bid) в процентах, 0.0 — недоступен."""
        func = self._http.get_orderbook
        try:
            resp = await self._fetch(
                func,
                category="linear",
                symbol=symbol,
                limit=1,
            )
        except RuntimeError as exc:
            logger.warning("get_spread %s: %s", symbol, exc)
            return 0.0
        if resp.get("retCode") != 0:
            return 0.0
        result = resp.get("result", {})
        bids = result.get("b", [])
        asks = result.get("a", [])
        if not bids or not asks:
            return 0.0
        best_bid = float(bids[0][0])
        best_ask = float(asks[0][0])
        if best_bid <= 0:
            return 0.0
        return (best_ask - best_bid) / best_bid * 100

    async def get_filtered_symbols(
        self,
        min_turnover_24h: float,
    ) -> list[tuple[str, float]]:
        """Символы с оборотом >= min_turnover_24h, отсортированы по обороту."""
        tickers = await self.get_tickers()
        filtered: list[tuple[str, float]] = []
        for ticker in tickers:
            symbol = str(ticker.get("symbol", ""))
            if not symbol.endswith("USDT"):
                continue
            try:
                turnover = float(ticker.get("turnover24h", "0"))
            except (ValueError, TypeError):
                continue
            if turnover >= min_turnover_24h:
                filtered.append((symbol, turnover))
        filtered.sort(key=lambda item: item[1], reverse=True)
        return filtered

    async def get_klines_range(
        self,
        symbol: str,
        interval: str,
        start_ms: int,
        end_ms: int,
        limit: int = 1000,
    ) -> list[dict]:
        """Свечи за период с пагинацией (end → start, newest first)."""
        all_candles: list[dict] = []
        current_end = end_ms
        func = self._http.get_kline
        while current_end > start_ms:
            try:
                resp = await self._fetch(
                    func,
                    category="linear",
                    symbol=symbol,
                    interval=interval,
                    limit=limit,
                    end=current_end,
                    start=start_ms,
                )
            except RuntimeError as exc:
                logger.warning("get_klines_range %s: %s", symbol, exc)
                return all_candles
            if resp.get("retCode") != 0:
                logger.error(
                    "get_klines_range %s error: %s", symbol, resp.get("retMsg")
                )
                return all_candles
            raw = resp["result"]["list"]
            if not raw:
                return all_candles
            for c in raw:
                all_candles.append(
                    {
                        "open_time": int(c[0]),
                        "open": float(c[1]),
                        "high": float(c[2]),
                        "low": float(c[3]),
                        "close": float(c[4]),
                        "volume": float(c[5]),
                        "turnover": float(c[6]) if len(c) > 6 else 0.0,
                    }
                )
            oldest_time = int(raw[-1][0])
            if oldest_time >= current_end:
                break
            current_end = oldest_time - 1
        return all_candles
