"""Данные для PositionGuard: дельты объёма (CVD), открытый интерес, фандинг.

Всё через публичный REST Bybit v5 (``BybitClient._call``). Ошибки сети не
пробрасываются: без данных guard просто не срабатывает.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from core.bybit_client import BybitClient, Candle
from robot_trend.guard import GuardParams, OiPoint

logger = logging.getLogger(__name__)

TF_MS = 5 * 60_000
OI_POLL_SEC = 60.0
FUNDING_POLL_SEC = 300.0


class GuardFeed:
    """Копит по 5м-бакетам дельту объёма, точки OI и последний фандинг."""

    def __init__(
        self,
        client: BybitClient,
        symbol: str,
        params: GuardParams,
    ) -> None:
        self._client = client
        self._symbol = symbol
        self._params = params
        #: open_time свечи -> накопленная дельта (buy volume - sell volume)
        self._deltas: dict[int, float] = {}
        self._seen: set[str] = set()
        self._oi: list[OiPoint] = []
        self._funding: float = 0.0
        self._last_oi_poll: float = 0.0
        self._last_funding_poll: float = 0.0
        #: трейды недоступны — CVD считается прокси по телам свечей
        self._proxy: bool = False

    # ==================== сбор данных ====================

    async def poll(self, candles: list[Candle]) -> None:
        """Обновить дельты, OI и фандинг (OI/фандинг — по расписанию)."""
        price = candles[-1].close if candles else 0.0
        await self._poll_trades()
        now = time.monotonic()
        if now - self._last_oi_poll >= OI_POLL_SEC:
            self._last_oi_poll = now
            await self._poll_oi(price)
        if now - self._last_funding_poll >= FUNDING_POLL_SEC:
            self._last_funding_poll = now
            await self._poll_funding()

    async def _poll_trades(self) -> None:
        """Разложить последние сделки по 5м-бакетам."""
        category = self._client.config.category
        payload: dict[str, Any] | None = None
        for method in ("get_public_trade_history", "get_public_trades"):
            try:
                payload = await self._client._call(
                    method,
                    category=category,
                    symbol=self._symbol,
                    limit=200,
                )
                break
            except Exception as exc:  # noqa: BLE001
                logger.debug("guard: %s недоступен (%s)", method, exc)
        if payload is None:
            self._proxy = True
            return
        rows = (payload.get("result") or {}).get("list") or []
        if not rows:
            return
        for row in rows:
            exec_id = str(row.get("execId") or "")
            if not exec_id or exec_id in self._seen:
                continue
            self._seen.add(exec_id)
            bucket = int(row.get("time") or 0) // TF_MS * TF_MS
            size = float(row.get("size") or 0.0)
            delta = size if row.get("side") == "Buy" else -size
            self._deltas[bucket] = self._deltas.get(bucket, 0.0) + delta
        if len(self._seen) > 20_000:
            self._seen.clear()
        self._proxy = False

    async def _poll_oi(self, price: float) -> None:
        try:
            payload = await self._client._call(
                "get_open_interest",
                category=self._client.config.category,
                symbol=self._symbol,
                intervalTime="5min",
                limit=1,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("guard: OI недоступен (%s)", exc)
            return
        rows = (payload.get("result") or {}).get("list") or []
        if not rows:
            return
        row = rows[0]
        self._oi.append(
            OiPoint(
                ts_ms=int(row.get("timestamp") or 0),
                oi=float(row.get("openInterest") or 0.0),
                price=price,
            ),
        )
        keep = max(self._params.oi_window * 3, 90)
        if len(self._oi) > keep:
            del self._oi[:-keep]

    async def _poll_funding(self) -> None:
        try:
            payload = await self._client._call(
                "get_tickers",
                category=self._client.config.category,
                symbol=self._symbol,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("guard: фандинг недоступен (%s)", exc)
            return
        rows = (payload.get("result") or {}).get("list") or []
        if rows and rows[0].get("fundingRate"):
            self._funding = float(rows[0]["fundingRate"])

    # ==================== срезы для PositionGuard ====================

    def cvd_series(self, candles: list[Candle]) -> list[float]:
        """Накопленная дельта, выровненная 1:1 со свечами."""
        total = 0.0
        out: list[float] = []
        for candle in candles:
            delta = self._deltas.get(candle.open_time)
            if delta is None:
                delta = candle.volume * (1.0 if candle.close >= candle.open else -1.0)
            total += delta
            out.append(total)
        return out

    def oi_points(self) -> list[OiPoint]:
        return list(self._oi)

    def funding(self) -> float:
        return self._funding

    @property
    def cvd_proxy(self) -> bool:
        """True — CVD собран по прокси (тело свечи), а не по реальным сделкам."""
        return self._proxy
