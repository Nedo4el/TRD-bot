"""WS-потоки: публичный ticker + приватные order/execution → asyncio.Queue."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any

from pybit.unified_trading import WebSocket

from core.config import Config
from core.utils import exponential_backoff

logger = logging.getLogger(__name__)


class DataFeed:
    """Собирает события WS в очереди asyncio (fill через private WS)."""

    def __init__(
        self,
        config: Config,
        loop: asyncio.AbstractEventLoop,
        symbol: str,
    ) -> None:
        self._config = config
        self._loop = loop
        self._symbol = symbol
        self.prices: asyncio.Queue[float] = asyncio.Queue()
        self.order_events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.exec_events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self.last_price: float = 0.0

    def start(self) -> None:
        self._stop.clear()
        self._threads = [
            threading.Thread(
                target=self._public_worker, name="zakol-ws-public", daemon=True
            ),
            threading.Thread(
                target=self._private_worker, name="zakol-ws-private", daemon=True
            ),
        ]
        for t in self._threads:
            t.start()
        logger.info("DataFeed запущен (%s)", self._symbol)

    def stop(self) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=5)
        logger.info("DataFeed остановлен")

    def _push(self, queue: asyncio.Queue[Any], item: Any) -> None:
        self._loop.call_soon_threadsafe(queue.put_nowait, item)

    def _on_ticker(self, message: dict[str, Any]) -> None:
        data = message.get("data", {})
        if data.get("symbol") != self._symbol:
            return
        try:
            price = float(data["lastPrice"])
        except (KeyError, TypeError, ValueError):
            return
        self.last_price = price
        self._push(self.prices, price)

    def _on_order(self, message: dict[str, Any]) -> None:
        self._push(self.order_events, message)

    def _on_execution(self, message: dict[str, Any]) -> None:
        self._push(self.exec_events, message)

    def _public_worker(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            ws: WebSocket | None = None
            try:
                ws = WebSocket(
                    testnet=self._config.testnet, channel_type=self._config.category
                )
                ws.ticker_stream(self._symbol, self._on_ticker)
                attempt = 0
                while not self._stop.is_set():
                    if not ws.is_connected():
                        raise ConnectionError("public ws down")
                    time.sleep(1)
            except Exception as exc:  # noqa: BLE001
                if self._stop.is_set():
                    break
                delay = exponential_backoff(attempt, base=2.0, cap=60.0)
                attempt += 1
                logger.warning("public WS: %s, retry %.1fs", exc, delay)
                time.sleep(delay)
            finally:
                if ws is not None:
                    try:
                        ws.exit()
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("public ws close: %s", exc)

    def _private_worker(self) -> None:
        if not self._config.api_key or not self._config.api_secret:
            logger.warning("private WS пропущен: нет API-ключей")
            return
        attempt = 0
        while not self._stop.is_set():
            ws: WebSocket | None = None
            try:
                ws = WebSocket(
                    testnet=self._config.testnet,
                    channel_type="private",
                    api_key=self._config.api_key,
                    api_secret=self._config.api_secret,
                )
                ws.order_stream(self._on_order)
                ws.execution_stream(self._on_execution)
                attempt = 0
                while not self._stop.is_set():
                    if not ws.is_connected():
                        raise ConnectionError("private ws down")
                    time.sleep(1)
            except Exception as exc:  # noqa: BLE001
                if self._stop.is_set():
                    break
                delay = exponential_backoff(attempt, base=2.0, cap=60.0)
                attempt += 1
                logger.warning("private WS: %s, retry %.1fs", exc, delay)
                time.sleep(delay)
            finally:
                if ws is not None:
                    try:
                        ws.exit()
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("private ws close: %s", exc)
