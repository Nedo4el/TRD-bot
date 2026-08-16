"""Обёртка над pybit: REST-запросы, WebSocket, rate limiting, переподключение.

Все публичные методы — асинхронные: синхронные вызовы pybit выполняются
в отдельных потоках (asyncio.to_thread), чтобы не блокировать главный цикл.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, cast

from pybit.exceptions import FailedRequestError, InvalidRequestError
from pybit.unified_trading import HTTP, WebSocket

from config import Config
from logger import get_logger
from utils import exponential_backoff, retry, run_in_thread

logger = get_logger(__name__)


@dataclass
class Candle:
    """Одна свеча (свечной график)."""

    open_time: int  # время открытия свечи (unix, мс)
    open: float  # цена открытия
    high: float  # максимум
    low: float  # минимум
    close: float  # цена закрытия
    volume: float  # объём


@dataclass
class Position:
    """Открытая позиция по инструменту."""

    symbol: str
    side: str  # "Buy" (long) или "Sell" (short)
    size: float  # размер позиции в базовом активе
    avg_price: float  # средняя цена входа
    unrealised_pnl: float  # нереализованная прибыль/убыток
    stop_loss: float | None = None
    take_profit: float | None = None


class BybitClient:
    """Единая точка доступа к Bybit API v5.

    Отвечает за:
    - подключение HTTP-сессии pybit (testnet/mainnet),
    - rate limiting (защита от блокировки по лимитам API),
    - REST-методы: цена, свечи, баланс, ордера, стоп-лосс/тейк-профит,
    - WebSocket-поток цен с автопереподключением (exponential backoff).
    """

    def __init__(self, config: Config) -> None:
        self.config = config

        # Создаём синхронную HTTP-сессию pybit.
        # testnet=True — тестовая сеть (нужны testnet-ключи).
        self._http = HTTP(
            testnet=config.testnet,
            api_key=config.api_key,
            api_secret=config.api_secret,
            recv_window=10000,  # окно валидности подписи запроса, мс
        )

        # --- Rate limiting ---
        # Минимальный интервал между запросами, чтобы не превысить лимиты API.
        # Bybit ограничивает частоту запросов (вес запросов в секунду);
        # лимит 10 req/s — консервативное значение для личных ключей.
        self._min_call_interval = 1.0 / config.requests_per_second
        self._last_call_times: deque[float] = deque()

        # --- WebSocket ---
        self._ws: WebSocket | None = None
        self._ws_thread: threading.Thread | None = None
        self._ws_stop = threading.Event()  # сигнал остановки WS-потока
        self.last_ws_price: dict[str, float] = {}  # последние цены из WS
        self._ws_lock = threading.Lock()

    # ========================== Rate limiting ==========================

    def _rate_limit(self) -> None:
        """Выдержать паузу, если запросы идут слишком часто.

        Поддерживаем скользящее окно последних N вызовов и спим,
        если следующий запрос «рванёт» лимит.
        """
        now = time.monotonic()
        # Убираем из окна записи старше 1 секунды
        while self._last_call_times and now - self._last_call_times[0] > 1.0:
            self._last_call_times.popleft()
        # Если окно заполнено — спим до истечения старейшего вызова
        if len(self._last_call_times) >= self.config.requests_per_second:
            sleep_for = self._last_call_times[0] + 1.0 - now
            if sleep_for > 0:
                logger.debug("Rate limit: пауза %.3fс", sleep_for)
                time.sleep(sleep_for)
        self._last_call_times.append(time.monotonic())

    # ============================ REST API =============================

    async def _call(self, method_name: str, *args: Any, **kwargs: Any) -> Any:
        """Единая точка вызова REST-метода pybit.

        Добавляет rate limiting и повторные попытки при сбоях сети.

        Args:
            method_name: имя метода HTTP-сессии pybit (например, "get_kline").
            args, kwargs: аргументы метода.

        Returns:
            Ответ API в виде dict (сырой ответ pybit).
        """
        method = getattr(self._http, method_name)
        self._rate_limit()

        @retry(max_retries=3, base_delay=1.0)
        def _sync_call() -> Any:
            # Синхронный вызов pybit — см. аннотации типов у pybit
            return method(*args, **kwargs)  # type: ignore[no-any-return]

        try:
            result = await run_in_thread(_sync_call)
        except (FailedRequestError, InvalidRequestError) as exc:
            # Ошибки API (неверные параметры, нехватка средств и т.п.)
            # логируются на уровне error, но НЕ считаются временными.
            logger.error("Bybit API ошибка в %s: %s", method_name, exc)
            raise
        except Exception as exc:
            # Временные сбои (сеть, таймауты) — retry уже отработал внутри.
            logger.error("Сбой вызова %s: %s", method_name, exc)
            raise
        return result

    async def get_price(self, symbol: str) -> float:
        """Получить текущую цену инструмента (REST)."""
        resp = await self._call(
            "get_tickers", category=self.config.category, symbol=symbol
        )
        # result.list[0].lastPrice — последняя цена
        tickers = resp["result"]["list"]
        if not tickers:
            raise ValueError(f"Тикер {symbol} не найден")
        return float(tickers[0]["lastPrice"])

    async def get_klines(
        self,
        symbol: str,
        interval: str | None = None,
        limit: int | None = None,
    ) -> list[Candle]:
        """Получить исторические свечи.

        Args:
            symbol: торговая пара (например, BTCUSDT).
            interval: таймфрейм ("1", "5", "15", "60", "240", "D").
            limit: сколько свечей запросить.

        Returns:
            Список свечей от старых к новым.
        """
        resp = await self._call(
            "get_kline",
            category=self.config.category,
            symbol=symbol,
            interval=interval or self.config.timeframe,
            limit=limit or self.config.kline_limit,
        )
        # Каждая строка: [start, open, high, low, close, volume, turnover]
        rows = resp["result"]["list"]
        candles: list[Candle] = []
        for row in reversed(rows):  # pybit отдаёт новые первыми — разворачиваем
            candles.append(
                Candle(
                    open_time=int(row[0]),
                    open=float(row[1]),
                    high=float(row[2]),
                    low=float(row[3]),
                    close=float(row[4]),
                    volume=float(row[5]),
                ),
            )
        return candles

    async def get_balance(self) -> float:
        """Получить доступный баланс (USDT) в объединённом аккаунте."""
        resp = await self._call(
            "get_wallet_balance",
            accountType="UNIFIED",
            coin="USDT",
        )
        accounts = resp["result"]["list"]
        if not accounts:
            return 0.0
        return float(accounts[0]["totalEquity"])

    async def place_order(
        self,
        symbol: str,
        side: str,
        qty: float,
        order_type: str = "Market",
        price: float | None = None,
    ) -> dict[str, Any]:
        """Выставить ордер (рыночный или лимитный).

        Args:
            symbol: торговая пара.
            side: "Buy" или "Sell".
            qty: количество в базовом активе (например, 0.001 BTC).
            order_type: "Market" или "Limit".
            price: цена для лимитного ордера (для Market не нужна).

        Returns:
            Ответ API с данными созданного ордера.
        """
        params: dict[str, Any] = {
            "category": self.config.category,
            "symbol": symbol,
            "side": side,
            "orderType": order_type,
            "qty": str(qty),
            "timeInForce": "IOC" if order_type == "Market" else "GTC",
        }
        if price is not None:
            params["price"] = str(price)
        return cast(dict[str, Any], await self._call("place_order", **params))

    async def set_stop_loss_take_profit(
        self,
        symbol: str,
        stop_loss: float,
        take_profit: float,
    ) -> dict[str, Any]:
        """Установить стоп-лосс и тейк-профит для открытой позиции.

        Работает для производных инструментов (linear/inverse).
        Для спота Bybit не поддерживает серверные SL/TP — вернёт ошибку,
        которую вызывающий код обрабатывает отдельно.
        """
        return cast(
            dict[str, Any],
            await self._call(
                "set_trading_stop",
                category=self.config.category,
                symbol=symbol,
                stopLoss=str(stop_loss),
                takeProfit=str(take_profit),
                positionIdx=0,
            ),
        )

    async def get_position(self, symbol: str) -> Position | None:
        """Получить открытую позицию по инструменту (None если позиции нет)."""
        resp = await self._call(
            "get_positions",
            category=self.config.category,
            symbol=symbol,
        )
        rows = resp["result"]["list"]
        if not rows or float(rows[0]["size"]) == 0:
            return None
        row = rows[0]
        return Position(
            symbol=symbol,
            side=row["side"],
            size=float(row["size"]),
            avg_price=float(row["avgPrice"]),
            unrealised_pnl=float(row["unrealisedPnl"]),
            stop_loss=float(row["stopLoss"]) if row.get("stopLoss") else None,
            take_profit=float(row["takeProfit"]) if row.get("takeProfit") else None,
        )

    async def close_position(
        self, symbol: str, qty: float, side: str
    ) -> dict[str, Any]:
        """Закрыть позицию рыночным ордером в противоположную сторону.

        Args:
            symbol: торговая пара.
            qty: объём для закрытия.
            side: сторона ОТКРЫТОЙ позиции (Buy => продаём для закрытия).
        """
        close_side = "Sell" if side == "Buy" else "Buy"
        return await self.place_order(symbol=symbol, side=close_side, qty=qty)

    # =========================== WebSocket =============================

    def _on_ws_message(self, message: dict[str, Any]) -> None:
        """Колбэк WS-потока: сохраняем последнюю цену по каждому символу."""
        try:
            data = message.get("data", {})
            symbol = data.get("symbol")
            price = data.get("lastPrice")
            if symbol and price:
                self.last_ws_price[symbol] = float(price)
        except (TypeError, ValueError) as exc:
            logger.warning("Некорректное WS-сообщение: %s (%s)", message, exc)

    def _ws_worker(self) -> None:
        """Фоновый поток WebSocket с автопереподключением.

        Схема:
        1. Создаём WebSocket-соединение и подписываемся на тикеры.
        2. Пока соединение живо — просто ждём.
        3. При обрыве — повторяем попытку с экспоненциальной задержкой.

        WebSocket из pybit сам работает в своём потоке; здесь мы лишь
        контролируем его жизненный цикл и пересоздаём при обрыве.
        """
        attempt = 0
        while not self._ws_stop.is_set():
            ws: WebSocket | None = None
            try:
                # channel_type зависит от категории: spot/linear/inverse
                ws = WebSocket(
                    testnet=self.config.testnet,
                    channel_type=self.config.category,
                )
                # Подписка на поток тикеров; колбэк вызывается при каждом обновлении
                ws.ticker_stream(
                    self.config.symbol,
                    self._on_ws_message,
                )
                with self._ws_lock:
                    self._ws = ws
                attempt = 0  # успешное подключение — сбрасываем backoff
                logger.info("WebSocket подключён: %s", self.config.symbol)

                # Ждём, пока соединение живо или не запрошена остановка
                while not self._ws_stop.is_set():
                    if not ws.is_connected():
                        raise ConnectionError("WebSocket разорван")
                    time.sleep(1)
            # Ловим всё: в цикле переподключения любая ошибка
            # должна привести к повторной попытке, а не к падению потока
            except Exception as exc:  # noqa: BLE001
                if self._ws_stop.is_set():
                    break
                delay = exponential_backoff(attempt, base=2.0, cap=60.0)
                attempt += 1
                logger.warning(
                    "WebSocket оборвался (%s). Переподключение через %.1fс (попытка %d)",
                    exc,
                    delay,
                    attempt,
                )
                time.sleep(delay)
            finally:
                if ws is not None:
                    try:
                        ws.exit()  # корректное закрытие соединения
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("Ошибка при закрытии WS: %s", exc)
                with self._ws_lock:
                    self._ws = None

    def start_ws(self) -> None:
        """Запустить WebSocket-поток (неблокирующий)."""
        if self._ws_thread is not None and self._ws_thread.is_alive():
            return
        self._ws_stop.clear()
        self._ws_thread = threading.Thread(
            target=self._ws_worker,
            name="bybit-ws",
            daemon=True,  # демон: не мешает завершению процесса
        )
        self._ws_thread.start()
        logger.info("WebSocket-поток запущен")

    def stop_ws(self) -> None:
        """Остановить WebSocket-поток и закрыть соединение."""
        self._ws_stop.set()
        with self._ws_lock:
            if self._ws is not None:
                try:
                    self._ws.exit()
                except Exception as exc:  # noqa: BLE001
                    logger.debug("Ошибка при закрытии WS: %s", exc)
        if self._ws_thread is not None:
            self._ws_thread.join(timeout=5)
        logger.info("WebSocket-поток остановлен")

    def close(self) -> None:
        """Закрыть все соединения (вызывается при завершении бота)."""
        self.stop_ws()

    def reconnect(self) -> None:
        """Пересоздать HTTP-сессию после длительных сбоев связи.

        pybit-сессия содержит свои клиенты/таймауты; при нестабильной
        сети надёжнее создать её заново, чем пытаться «оживить».
        """
        try:
            self._http = HTTP(
                testnet=self.config.testnet,
                api_key=self.config.api_key,
                api_secret=self.config.api_secret,
                recv_window=10000,
            )
            logger.info("HTTP-сессия Bybit пересоздана")
        except Exception as exc:  # noqa: BLE001
            logger.error("Не удалось пересоздать HTTP-сессию: %s", exc)
