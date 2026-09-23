"""Обёртка над pybit: REST-запросы, WebSocket, rate limiting, переподключение.

Все публичные методы — асинхронные: синхронные вызовы pybit выполняются
в отдельных потоках (asyncio.to_thread), чтобы не блокировать главный цикл.
"""

from __future__ import annotations

import asyncio
import math
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, cast

from pybit.exceptions import FailedRequestError, InvalidRequestError
from pybit.unified_trading import HTTP, WebSocket

from core.config import Config
from core.logger import get_logger
from core.utils import exponential_backoff, retry, run_in_thread

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
    position_idx: int = 0  # 0 — one-way, 1 — long (хедж), 2 — short (хедж)


def _fmt_num(value: float) -> str:
    """Отформатировать число для API биржи: без scientific notation и хвостов."""
    text = f"{value:.12f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


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

        # Кэш фильтров инструментов по символам (qtyStep, tickSize, минимумы)
        self._instruments: dict[str, dict[str, Any]] = {}
        # Кэш шагов объёма по символам (для округления размеров ордеров)
        self._qty_steps: dict[str, float] = {}

    # ========================== Rate limiting ==========================

    async def _rate_limit(self) -> None:
        """Ограничить частоту REST-вызовов скользящим окном 5 секунд.

        Максимум за окно: requests_per_second * 5 (по умолчанию 10*5=50).
        Ждём через asyncio.sleep, чтобы не блокировать event loop.
        """
        window = 5.0
        max_in_window = max(int(self.config.requests_per_second * window), 1)
        now = time.monotonic()
        while self._last_call_times and now - self._last_call_times[0] > window:
            self._last_call_times.popleft()
        if len(self._last_call_times) >= max_in_window:
            sleep_for = self._last_call_times[0] + window - now
            if sleep_for > 0:
                logger.debug("Rate limit: пауза %.3fс", sleep_for)
                await asyncio.sleep(sleep_for)
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
        await self._rate_limit()

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
        start: int | None = None,
        end: int | None = None,
    ) -> list[Candle]:
        """Получить исторические свечи.

        Args:
            symbol: торговая пара (например, BTCUSDT).
            interval: таймфрейм ("1", "5", "15", "60", "240", "D").
            limit: сколько свечей запросить (макс. 1000).
            start: начальный timestamp в миллисекундах (включительно).
            end: конечный timestamp в миллисекундах (включительно).

        Returns:
            Список свечей от старых к новым.
        """
        params: dict[str, Any] = {
            "category": self.config.category,
            "symbol": symbol,
            "interval": interval or self.config.timeframe,
            "limit": limit or self.config.kline_limit,
        }
        if start is not None:
            params["start"] = start
        if end is not None:
            params["end"] = end
        resp = await self._call("get_kline", **params)
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
        *,
        post_only: bool = False,
        reduce_only: bool = False,
        position_idx: int | None = None,
        order_link_id: str | None = None,
    ) -> dict[str, Any]:
        """Выставить ордер (рыночный или лимитный).

        Args:
            symbol: торговая пара.
            side: "Buy" или "Sell".
            qty: количество в базовом активе (например, 0.001 BTC).
            order_type: тип ордера ("Market" или "Limit").
            price: цена лимитного ордера (округляется до tickSize биржи).
            post_only: True — PostOnly (отклоняется, если исполнится как taker).
            reduce_only: True — ордер только уменьшает позицию (фьючерсы).
            position_idx: 1/2 — хедж-режим (long/short); None — one-way.
            order_link_id: клиентский id (orderLinkId) для идемпотентности.

        Returns:
            Ответ API с данными созданного ордера.
        """
        params: dict[str, Any] = {
            "category": self.config.category,
            "symbol": symbol,
            "side": side,
            "orderType": order_type,
            "qty": _fmt_num(self._round_qty(qty, await self._get_qty_step(symbol))),
            "timeInForce": "IOC" if order_type == "Market" else "GTC",
        }
        if order_type == "Limit" and post_only:
            params["timeInForce"] = "PostOnly"
        if price is not None:
            tick = (await self.get_instrument_filters(symbol)).tick_size
            params["price"] = _fmt_num(round(price / tick) * tick)
        if reduce_only:
            params["reduceOnly"] = True
        if position_idx is not None:
            params["positionIdx"] = position_idx
        if order_link_id:
            params["orderLinkId"] = order_link_id
        return cast(dict[str, Any], await self._call("place_order", **params))

    async def cancel_order(self, symbol: str, order_id: str) -> dict[str, Any]:
        """Отменить ордер по orderId (обычный или conditional)."""
        return cast(
            dict[str, Any],
            await self._call(
                "cancel_order",
                category=self.config.category,
                symbol=symbol,
                orderId=order_id,
            ),
        )

    async def cancel_all_orders(self, symbol: str) -> dict[str, Any]:
        """Отменить все открытые ордера символа (включая conditional)."""
        return cast(
            dict[str, Any],
            await self._call(
                "cancel_all_orders",
                category=self.config.category,
                symbol=symbol,
            ),
        )

    async def get_open_orders(self, symbol: str) -> list[dict[str, Any]]:
        """Все открытые ордера символа (с пагинацией по курсору).

        Returns:
            Список сырых ордеров API (orderId, orderStatus, cumExecQty, ...).
        """
        orders: list[dict[str, Any]] = []
        cursor = ""
        while True:
            params: dict[str, Any] = {
                "category": self.config.category,
                "symbol": symbol,
                "limit": 50,
            }
            if cursor:
                params["cursor"] = cursor
            resp = await self._call("get_open_orders", **params)
            orders.extend(resp["result"].get("list", []))
            cursor = resp["result"].get("nextPageCursor") or ""
            if not cursor:
                return orders

    async def get_order(self, symbol: str, order_id: str) -> dict[str, Any] | None:
        """Найти ордер в истории по orderId (None — не найден)."""
        resp = await self._call(
            "get_order_history",
            category=self.config.category,
            symbol=symbol,
            orderId=order_id,
            limit=1,
        )
        rows = resp["result"].get("list", [])
        return rows[0] if rows else None

    async def get_orderbook(self, symbol: str, limit: int = 50) -> dict[str, Any]:
        """Стакан уровня symbol: {"b": [[price, qty], ...], "a": [...]}."""
        resp = await self._call(
            "get_orderbook",
            category=self.config.category,
            symbol=symbol,
            limit=limit,
        )
        return cast(dict[str, Any], resp["result"])

    async def get_funding_rate(self, symbol: str) -> float | None:
        """Последний зафиксированный funding rate инструмента (None — нет данных).

        Returns:
            Ставка дробью (0.0001 = 0.01% за интервал финансирования).
        """
        resp = await self._call(
            "get_funding_rate_history",
            category=self.config.category,
            symbol=symbol,
            limit=1,
        )
        rows = resp["result"].get("list", [])
        if not rows:
            return None
        rate = rows[0].get("fundingRate")
        return float(rate) if rate is not None else None

    async def place_stop_market(
        self,
        symbol: str,
        side: str,
        qty: float,
        trigger_price: float,
        *,
        reduce_only: bool = True,
        position_idx: int | None = None,
    ) -> dict[str, Any]:
        """Выставить conditional stop-market ордер (срабатывает по triggerPrice).

        Args:
            symbol: торговая пара.
            side: сторона ордера ("Buy"/"Sell") — для закрытия лонга Sell.
            qty: объём.
            trigger_price: цена срабатывания (MarkPrice).
            reduce_only: только уменьшение позиции.
            position_idx: 1/2 — хедж-режим; None — one-way.

        Returns:
            Ответ API с данными созданного ордера.
        """
        tick = (await self.get_instrument_filters(symbol)).tick_size
        trigger = _fmt_num(round(trigger_price / tick) * tick)
        params: dict[str, Any] = {
            "category": self.config.category,
            "symbol": symbol,
            "side": side,
            "orderType": "Market",
            "qty": _fmt_num(self._round_qty(qty, await self._get_qty_step(symbol))),
            "stopOrderType": "Stop",
            "triggerPrice": trigger,
            "triggerBy": "MarkPrice",
        }
        if reduce_only:
            params["reduceOnly"] = True
        if position_idx is not None:
            params["positionIdx"] = position_idx
        return cast(dict[str, Any], await self._call("place_order", **params))

    @dataclass(frozen=True)
    class InstrumentFilters:
        """Торговые фильтры инструмента (шаги и минимумы биржи)."""

        tick_size: float
        qty_step: float
        min_qty: float
        min_notional: float

    async def get_instrument_filters(
        self, symbol: str
    ) -> BybitClient.InstrumentFilters:
        """Получить фильтры инструмента (с кэшем): tickSize, qtyStep, минимумы."""
        row = await self._get_instrument(symbol)
        price_filter = row.get("priceFilter", {})
        lot = row.get("lotSizeFilter", {})
        return BybitClient.InstrumentFilters(
            tick_size=float(price_filter.get("tickSize", 0.0001)),
            qty_step=float(lot.get("qtyStep", 0.001)),
            min_qty=float(lot.get("minOrderQty", 0.0)),
            min_notional=float(lot.get("minNotionalValue", 0.0)),
        )

    async def _get_instrument(self, symbol: str) -> dict[str, Any]:
        """Сырая строка get_instruments_info по символу (с кэшем)."""
        if symbol not in self._instruments:
            resp = await self._call(
                "get_instruments_info",
                category=self.config.category,
                symbol=symbol,
            )
            rows = resp["result"]["list"]
            if not rows:
                raise ValueError(f"Инструмент {symbol} не найден")
            self._instruments[symbol] = rows[0]
        return self._instruments[symbol]

    async def _get_qty_step(self, symbol: str) -> float:
        """Получить шаг объёма инструмента (с кэшем).

        Args:
            symbol: торговая пара.

        Returns:
            Минимальный шаг объёма (lotSizeFilter.qtyStep).
        """
        row = await self._get_instrument(symbol)
        step = float(row["lotSizeFilter"]["qtyStep"])
        self._qty_steps[symbol] = step
        return step

    @staticmethod
    def _round_qty(qty: float, step: float) -> float:
        """Округлить объём вниз до кратного шагу биржи."""
        return math.floor(qty / step) * step

    async def set_stop_loss_take_profit(
        self,
        symbol: str,
        stop_loss: float,
        take_profit: float | None = None,
    ) -> dict[str, Any]:
        """Установить стоп-лосс (и опционально TP) для открытой позиции.

        Работает для производных инструментов (linear/inverse).
        Для спота Bybit не поддерживает серверные SL/TP — вернёт ошибку,
        которую вызывающий код обрабатывает отдельно.
        """
        params: dict[str, Any] = {
            "category": self.config.category,
            "symbol": symbol,
            "stopLoss": str(stop_loss),
            "positionIdx": 0,
        }
        if take_profit is not None:
            params["takeProfit"] = str(take_profit)
        return cast(
            dict[str, Any],
            await self._call("set_trading_stop", **params),
        )

    async def get_position(self, symbol: str) -> Position | None:
        """Получить открытую позицию по инструменту (None если позиции нет)."""
        positions = await self.get_positions(symbol)
        return positions[0] if positions else None

    async def get_positions(self, symbol: str) -> list[Position]:
        """Все открытые позиции символа (one-way — до 1, хедж — до 2)."""
        resp = await self._call(
            "get_positions",
            category=self.config.category,
            symbol=symbol,
        )
        positions: list[Position] = []
        for row in resp["result"]["list"]:
            if float(row["size"]) == 0:
                continue
            positions.append(
                Position(
                    symbol=symbol,
                    side=row["side"],
                    size=float(row["size"]),
                    avg_price=float(row["avgPrice"]),
                    unrealised_pnl=float(row["unrealisedPnl"]),
                    stop_loss=float(row["stopLoss"]) if row.get("stopLoss") else None,
                    take_profit=float(row["takeProfit"])
                    if row.get("takeProfit")
                    else None,
                    position_idx=int(row.get("positionIdx") or 0),
                ),
            )
        return positions

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
