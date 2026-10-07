"""WS-потоки: публичный ticker + kline, приватные order/execution/wallet/position.

События складываются в asyncio.Queue главного цикла. После обрыва и
успешного переподключения поднимается флаг `resync_needed` — order_flow
в этом случае сверяет состояние с биржей через REST.

Копия robot_trend/data_feed.py (без изменений логики).
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable, Coroutine
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
        on_alert: Callable[[str], Coroutine[Any, Any, None]] | None = None,
        reconnect_sec: float = 2.0,
        heartbeat_sec: float = 1.0,
        timeframe: str = "5",
    ) -> None:
        self._config = config
        self._loop = loop
        self._symbol = symbol
        self._on_alert = on_alert
        self._timeframe = timeframe
        # RECONNECT_SEC — база экспоненциального backoff (потолок 60с),
        # HEARTBEAT_SEC — период проверки живости соединения
        self._reconnect_sec = reconnect_sec
        self._heartbeat_sec = heartbeat_sec
        self.prices: asyncio.Queue[float] = asyncio.Queue()
        self.candles: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.order_events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.exec_events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.wallet_events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.position_events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self.last_price: float = 0.0
        self.last_balance: float = 0.0
        # меняется только в потоке event loop (см. _mark_resync)
        self.resync_needed: bool = False
        self._public_up = False
        self._private_up = False

    def is_connected(self) -> bool:
        """WS жив: пабличный + (приватный, если заданы ключи)."""
        has_keys = bool(self._config.api_key and self._config.api_secret)
        return self._public_up and (self._private_up or not has_keys)

    def start(self) -> None:
        self._stop.clear()
        self._threads = [
            threading.Thread(
                target=self._public_worker, name="flat-ws-public", daemon=True
            ),
            threading.Thread(
                target=self._private_worker, name="flat-ws-private", daemon=True
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

    def _mark_resync(self) -> None:
        """Только в потоке event loop."""
        if not self.resync_needed:
            self.resync_needed = True
            logger.warning("WS переподключён — нужен REST-ресинк")

    def _request_resync(self) -> None:
        """Попасть в поток event loop из WS-потока."""
        try:
            self._loop.call_soon_threadsafe(self._mark_resync)
        except RuntimeError:
            logger.debug("loop закрыт — ресинк пропущен")

    def _alert(self, text: str) -> None:
        """Критическое сообщение: лог + фоновый вызов on_alert."""
        logger.error(text)
        if self._on_alert is None:
            return
        try:
            self._loop.call_soon_threadsafe(self._spawn_alert, text)
        except RuntimeError:
            logger.debug("loop закрыт — алерт пропущен: %s", text)

    def _spawn_alert(self, text: str) -> None:
        if self._on_alert is not None:
            self._loop.create_task(self._on_alert(text))

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

    def _on_kline(self, message: dict[str, Any]) -> None:
        """Только закрытые свечи (confirm=true) — сигнал к решению."""
        data = message.get("data", [])
        rows = data if isinstance(data, list) else [data]
        for row in rows:
            if isinstance(row, dict) and row.get("confirm"):
                self._push(self.candles, row)

    def _on_order(self, message: dict[str, Any]) -> None:
        self._push(self.order_events, message)

    def _on_execution(self, message: dict[str, Any]) -> None:
        self._push(self.exec_events, message)

    def _on_position(self, message: dict[str, Any]) -> None:
        self._push(self.position_events, message)

    def _on_wallet(self, message: dict[str, Any]) -> None:
        self._update_balance(message)
        self._push(self.wallet_events, message)

    def _update_balance(self, message: dict[str, Any]) -> None:
        data = message.get("data", {})
        rows = data if isinstance(data, list) else [data]
        for row in rows:
            if not isinstance(row, dict):
                continue
            equity = row.get("totalEquity")
            if equity in (None, ""):
                continue
            try:
                self.last_balance = float(equity)
            except (TypeError, ValueError):
                continue
            return

    def _public_worker(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            ws: WebSocket | None = None
            try:
                reconnected = attempt > 0
                ws = WebSocket(
                    testnet=self._config.testnet, channel_type=self._config.category
                )
                ws.ticker_stream(self._symbol, self._on_ticker)
                ws.kline_stream(self._timeframe, self._symbol, self._on_kline)
                self._public_up = True
                attempt = 0
                if reconnected:
                    self._request_resync()
                while not self._stop.is_set():
                    if not ws.is_connected():
                        raise ConnectionError("public ws down")
                    time.sleep(self._heartbeat_sec)
            except Exception as exc:  # noqa: BLE001
                if self._stop.is_set():
                    break
                delay = exponential_backoff(
                    attempt,
                    base=self._reconnect_sec,
                    cap=max(self._reconnect_sec, 60.0),
                )
                attempt += 1
                logger.warning("public WS: %s, retry %.1fs", exc, delay)
                time.sleep(delay)
            finally:
                self._public_up = False
                if ws is not None:
                    try:
                        ws.exit()
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("public ws close: %s", exc)

    def _private_worker(self) -> None:
        if not self._config.api_key or not self._config.api_secret:
            self._alert(
                "flat: приватный WS отключён — нет API-ключей, "
                "филлы приходить не будут",
            )
            return
        attempt = 0
        while not self._stop.is_set():
            ws: WebSocket | None = None
            try:
                reconnected = attempt > 0
                ws = WebSocket(
                    testnet=self._config.testnet,
                    channel_type="private",
                    api_key=self._config.api_key,
                    api_secret=self._config.api_secret,
                )
                ws.order_stream(self._on_order)
                ws.execution_stream(self._on_execution)
                ws.wallet_stream(self._on_wallet)
                ws.position_stream(self._on_position)
                self._private_up = True
                attempt = 0
                if reconnected:
                    self._request_resync()
                while not self._stop.is_set():
                    if not ws.is_connected():
                        raise ConnectionError("private ws down")
                    time.sleep(self._heartbeat_sec)
            except Exception as exc:  # noqa: BLE001
                if self._stop.is_set():
                    break
                delay = exponential_backoff(
                    attempt,
                    base=self._reconnect_sec,
                    cap=max(self._reconnect_sec, 60.0),
                )
                attempt += 1
                logger.warning("private WS: %s, retry %.1fs", exc, delay)
                time.sleep(delay)
            finally:
                self._private_up = False
                if ws is not None:
                    try:
                        ws.exit()
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("private ws close: %s", exc)
