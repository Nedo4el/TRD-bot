"""Брокер робота уровней: ордера Bybit v5 + бумажное исполнение для DRY_RUN.

Стратегия не знает про pybit: она работает с протоколом :class:`Broker`.
В DRY_RUN те же вызовы уходят в :class:`PaperBook` — заявки «исполняются»
по реальной цене (REST), поэтому прогон показывает полный жизненный цикл
без отправки ордеров на биржу.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from core.bybit_client import BybitClient
from core.indicators import atr
from core.logger import get_logger

logger = get_logger(__name__)

DEFAULT_MIN_NOTIONAL = 5.0
FILL_EPS = 1e-12


@dataclass(frozen=True, slots=True)
class OrderResult:
    """Результат выставления ордера (реального или бумажного)."""

    order_id: str
    link_id: str
    side: str
    qty: float
    order_type: str
    price: float | None = None
    trigger: float | None = None


@dataclass(frozen=True, slots=True)
class OpenOrder:
    """Открытый ордер в терминах стратегии."""

    order_id: str
    link_id: str
    status: str
    filled_qty: float
    avg_price: float
    price: float


@dataclass(frozen=True, slots=True)
class PositionInfo:
    """Позиция на бирже: размер (положительный) и средняя цена входа."""

    qty: float
    side: str
    avg_price: float


class Broker(Protocol):
    """Минимальный интерфейс биржи, нужный стратегии."""

    dry_run: bool

    async def price(self, symbol: str) -> float:
        """Текущая цена символа."""

    async def atr_pct(self, symbol: str) -> float | None:
        """ATR(period) на рабочем ТФ в долях от цены (None — нет данных)."""

    async def next_funding_ts(self, symbol: str) -> float | None:
        """Unix-время следующего фандинга (None — не perpetual/нет данных)."""

    async def min_notional(self, symbol: str) -> float:
        """Минимальный notional ордера по инструменту, USDT."""

    async def place_limit(
        self,
        symbol: str,
        side: str,
        price: float,
        qty: float,
        link_id: str,
    ) -> OrderResult:
        """PostOnly лимитка на вход."""

    async def place_stop(
        self,
        symbol: str,
        side: str,
        trigger: float,
        qty: float,
        link_id: str,
    ) -> OrderResult:
        """Reduce-only stop-market, сторона ``side`` закрывает позицию."""

    async def place_take(
        self,
        symbol: str,
        side: str,
        price: float,
        qty: float,
        link_id: str,
    ) -> OrderResult:
        """Reduce-only тейк-лимит."""

    async def cancel(self, symbol: str, order_id: str) -> bool:
        """Снять ордер; False — ордера уже нет."""

    async def cancel_all(self, symbol: str) -> None:
        """Снять все ордера символа."""

    async def market_close(self, symbol: str, qty: float, position_side: str) -> None:
        """Закрыть позицию по рынку (``position_side``: Buy для long)."""

    async def open_orders(self, symbol: str) -> list[OpenOrder]:
        """Открытые ордера символа."""

    async def position(self, symbol: str) -> PositionInfo | None:
        """Текущая позиция символа (None — позиции нет)."""


# ---------------------------------------------------------------- бумажный ---


@dataclass
class _PaperOrder:
    """Заявка в бумажной книге."""

    order_id: str
    kind: str  # limit | stop | take
    symbol: str
    side: str
    qty: float
    link_id: str
    price: float | None = None
    trigger: float | None = None
    filled: float = 0.0
    status: str = "New"

    @property
    def reduce_only(self) -> bool:
        return self.kind in ("stop", "take")


@dataclass
class _PaperPosition:
    """Позиция бумажной книги: знаковый размер и средняя цена."""

    qty: float = 0.0
    avg_price: float = 0.0
    realized: float = 0.0


class PaperBook:
    """Имитация биржи для DRY_RUN: заявки исполняются по наблюдаемой цене."""

    def __init__(self) -> None:
        self._orders: dict[str, _PaperOrder] = {}
        self._positions: dict[str, _PaperPosition] = {}
        self._last_price: dict[str, float] = {}
        self._seq = 0
        self.fills: list[dict[str, Any]] = []

    # --- заявки ---

    def place(
        self,
        kind: str,
        symbol: str,
        side: str,
        qty: float,
        link_id: str,
        *,
        price: float | None = None,
        trigger: float | None = None,
    ) -> OrderResult:
        """Поставить заявку в книгу и вернуть результат."""
        self._seq += 1
        order = _PaperOrder(
            order_id=f"paper-{kind}-{self._seq}",
            kind=kind,
            symbol=symbol,
            side=side,
            qty=qty,
            link_id=link_id,
            price=price,
            trigger=trigger,
        )
        self._orders[order.order_id] = order
        return OrderResult(
            order_id=order.order_id,
            link_id=link_id,
            side=side,
            qty=qty,
            order_type=kind,
            price=price,
            trigger=trigger,
        )

    def cancel(self, order_id: str) -> bool:
        """Снять заявку; False — её нет или она уже исполнена."""
        order = self._orders.get(order_id)
        if order is None or order.status != "New":
            return False
        order.status = "Cancelled"
        return True

    def cancel_symbol(self, symbol: str) -> None:
        """Снять все открытые заявки символа."""
        for order in self._orders.values():
            if order.symbol == symbol and order.status == "New":
                order.status = "Cancelled"

    def open_orders(self, symbol: str) -> list[OpenOrder]:
        """Открытые заявки символа."""
        return [
            OpenOrder(
                order_id=order.order_id,
                link_id=order.link_id,
                status=order.status,
                filled_qty=order.filled,
                avg_price=order.price or 0.0,
                price=order.price or order.trigger or 0.0,
            )
            for order in self._orders.values()
            if order.symbol == symbol and order.status == "New"
        ]

    def position(self, symbol: str) -> PositionInfo | None:
        """Позиция книги (None — нулевая)."""
        current = self._positions.get(symbol)
        if current is None or abs(current.qty) < FILL_EPS:
            return None
        return PositionInfo(
            qty=abs(current.qty),
            side="Buy" if current.qty > 0 else "Sell",
            avg_price=current.avg_price,
        )

    # --- исполнение ---

    def on_price(self, symbol: str, price: float) -> list[dict[str, Any]]:
        """Проверить заявки по новой цене; вернуть список исполнений."""
        self._last_price[symbol] = price
        filled: list[dict[str, Any]] = []
        for order in list(self._orders.values()):
            if order.symbol != symbol or order.status != "New":
                continue
            fill_price = self._fill_price(order, price)
            if fill_price is None:
                continue
            order.status = "Filled"
            order.filled = order.qty
            self._apply(order, fill_price)
            filled.append(
                {
                    "order_id": order.order_id,
                    "kind": order.kind,
                    "link_id": order.link_id,
                    "side": order.side,
                    "qty": order.qty,
                    "price": fill_price,
                },
            )
        return filled

    @staticmethod
    def _fill_price(order: _PaperOrder, price: float) -> float | None:
        if order.kind == "limit":
            if order.price is None:
                return None
            if order.side == "Buy" and price <= order.price:
                return order.price
            if order.side == "Sell" and price >= order.price:
                return order.price
            return None
        if order.kind == "stop":
            if order.trigger is None:
                return None
            if order.side == "Buy" and price >= order.trigger:
                return order.trigger
            if order.side == "Sell" and price <= order.trigger:
                return order.trigger
            return None
        if order.kind == "take":
            if order.price is None:
                return None
            if order.side == "Buy" and price <= order.price:
                return order.price
            if order.side == "Sell" and price >= order.price:
                return order.price
        return None

    def _apply(self, order: _PaperOrder, price: float) -> None:
        """Применить исполнение к позиции книги."""
        state = self._positions.setdefault(order.symbol, _PaperPosition())
        signed = order.qty if order.side == "Buy" else -order.qty
        if order.reduce_only:
            reduce = min(abs(signed), abs(state.qty))
            if reduce <= 0:
                return
            direction = 1.0 if state.qty > 0 else -1.0
            state.realized += (price - state.avg_price) * reduce * direction
            state.qty -= direction * reduce
            if abs(state.qty) < FILL_EPS:
                state.qty = 0.0
                state.avg_price = 0.0
            return
        new_qty = state.qty + signed
        if state.qty == 0 or state.qty * signed > 0:
            total = abs(state.qty) + abs(signed)
            state.avg_price = (
                state.avg_price * abs(state.qty) + price * abs(signed)
            ) / total
        state.qty = new_qty

    def market_close(self, symbol: str, qty: float, position_side: str) -> None:
        """Закрыть позицию по последней известной цене заявки."""
        state = self._positions.get(symbol)
        if state is None or abs(state.qty) < FILL_EPS:
            return
        last = self._last_price.get(symbol, state.avg_price)
        self.cancel_symbol(symbol)
        reduce = min(qty, abs(state.qty))
        direction = 1.0 if state.qty > 0 else -1.0
        state.realized += (last - state.avg_price) * reduce * direction
        state.qty -= direction * reduce
        if abs(state.qty) < FILL_EPS:
            state.qty = 0.0
            state.avg_price = 0.0


# ------------------------------------------------------------------ брокер ---


class BybitBroker:
    """Реальный брокер Bybit v5 (``core.bybit_client``) с режимом DRY_RUN."""

    def __init__(
        self,
        client: BybitClient,
        *,
        dry_run: bool,
        atr_period: int = 14,
        atr_timeframe: str = "5",
    ) -> None:
        self._client = client
        self.dry_run = dry_run
        self._atr_period = atr_period
        self._atr_timeframe = atr_timeframe
        self._paper = PaperBook() if dry_run else None

    async def price(self, symbol: str) -> float:
        cached = self._client.last_ws_price.get(symbol)
        price = cached if cached else await self._client.get_price(symbol)
        if self._paper is not None:
            self._paper.on_price(symbol, price)
        return price

    async def atr_pct(self, symbol: str) -> float | None:
        try:
            candles = await self._client.get_klines(
                symbol,
                interval=self._atr_timeframe,
                limit=self._atr_period + 2,
            )
        except Exception as exc:  # noqa: BLE001 — фильтр не должен ронять сигнал
            logger.warning("ATR %s: %s", symbol, exc)
            return None
        if len(candles) < self._atr_period + 1:
            return None
        series = atr(
            [candle.high for candle in candles],
            [candle.low for candle in candles],
            [candle.close for candle in candles],
            self._atr_period,
        )
        last_price = candles[-1].close
        if not series or last_price <= 0:
            return None
        return series[-1] / last_price

    async def next_funding_ts(self, symbol: str) -> float | None:
        try:
            return await self._client.get_next_funding_time(symbol)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Фандинг %s: %s", symbol, exc)
            return None

    async def min_notional(self, symbol: str) -> float:
        try:
            filters = await self._client.get_instrument_filters(symbol)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Фильтры %s: %s", symbol, exc)
            return DEFAULT_MIN_NOTIONAL
        return filters.min_notional or DEFAULT_MIN_NOTIONAL

    async def place_limit(
        self,
        symbol: str,
        side: str,
        price: float,
        qty: float,
        link_id: str,
    ) -> OrderResult:
        if self._paper is not None:
            return self._paper.place(
                "limit",
                symbol,
                side,
                qty,
                link_id,
                price=price,
            )
        resp = await self._client.place_order(
            symbol,
            side,
            qty,
            order_type="Limit",
            price=price,
            post_only=True,
            order_link_id=link_id,
        )
        return OrderResult(
            order_id=_order_id(resp),
            link_id=link_id,
            side=side,
            qty=qty,
            order_type="limit",
            price=price,
        )

    async def place_stop(
        self,
        symbol: str,
        side: str,
        trigger: float,
        qty: float,
        link_id: str,
    ) -> OrderResult:
        if self._paper is not None:
            return self._paper.place(
                "stop",
                symbol,
                side,
                qty,
                link_id,
                trigger=trigger,
            )
        resp = await self._client.place_stop_market(
            symbol,
            side,
            qty,
            trigger,
            reduce_only=True,
            order_link_id=link_id,
        )
        return OrderResult(
            order_id=_order_id(resp),
            link_id=link_id,
            side=side,
            qty=qty,
            order_type="stop",
            trigger=trigger,
        )

    async def place_take(
        self,
        symbol: str,
        side: str,
        price: float,
        qty: float,
        link_id: str,
    ) -> OrderResult:
        if self._paper is not None:
            return self._paper.place(
                "take",
                symbol,
                side,
                qty,
                link_id,
                price=price,
            )
        resp = await self._client.place_order(
            symbol,
            side,
            qty,
            order_type="Limit",
            price=price,
            post_only=False,
            reduce_only=True,
            order_link_id=link_id,
        )
        return OrderResult(
            order_id=_order_id(resp),
            link_id=link_id,
            side=side,
            qty=qty,
            order_type="take",
            price=price,
        )

    async def cancel(self, symbol: str, order_id: str) -> bool:
        if self._paper is not None:
            return self._paper.cancel(order_id)
        try:
            await self._client.cancel_order(symbol, order_id)
            return True
        except Exception as exc:  # noqa: BLE001 — ордер мог исполниться/истечь
            logger.debug("cancel %s %s: %s", symbol, order_id, exc)
            return False

    async def cancel_all(self, symbol: str) -> None:
        if self._paper is not None:
            self._paper.cancel_symbol(symbol)
            return
        try:
            await self._client.cancel_all_orders(symbol)
        except Exception as exc:  # noqa: BLE001
            logger.warning("cancel_all %s: %s", symbol, exc)

    async def market_close(self, symbol: str, qty: float, position_side: str) -> None:
        if self._paper is not None:
            self._paper.market_close(symbol, qty, position_side)
            return
        close_side = "Sell" if position_side == "Buy" else "Buy"
        await self._client.place_order(
            symbol,
            close_side,
            qty,
            order_type="Market",
            reduce_only=True,
        )

    async def open_orders(self, symbol: str) -> list[OpenOrder]:
        if self._paper is not None:
            return self._paper.open_orders(symbol)
        rows = await self._client.get_open_orders(symbol)
        return [
            OpenOrder(
                order_id=str(row.get("orderId") or ""),
                link_id=str(row.get("orderLinkId") or ""),
                status=str(row.get("orderStatus") or ""),
                filled_qty=float(row.get("cumExecQty") or 0.0),
                avg_price=float(row.get("avgPrice") or 0.0),
                price=float(row.get("price") or 0.0),
            )
            for row in rows
        ]

    async def position(self, symbol: str) -> PositionInfo | None:
        if self._paper is not None:
            return self._paper.position(symbol)
        current = await self._client.get_position(symbol)
        if current is None or current.size <= 0:
            return None
        return PositionInfo(
            qty=current.size,
            side=current.side,
            avg_price=current.avg_price,
        )


def _order_id(resp: dict[str, Any]) -> str:
    """Достать orderId из ответа pybit."""
    result = resp.get("result") or {}
    return str(result.get("orderId") or "")
