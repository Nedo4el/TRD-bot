"""State machine: пара TTL-лимиток ±offset → fill → позиция → SL/TP → IDLE."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any, Protocol

from robot_zakol.config import ZakolConfig
from robot_zakol.risk import (
    fill_ratio,
    initial_stop,
    limit_buy_price,
    limit_sell_price,
    on_price,
    position_pnl,
    should_kill,
    take_level,
)
from robot_zakol.state import (
    PHASE_IDLE,
    PHASE_IN_POSITION,
    PHASE_STOPPED,
    PHASE_WORKING,
    OpenPosition,
    PendingOrder,
    StateStore,
)

logger = logging.getLogger(__name__)


class FeedProtocol(Protocol):
    """Минимальный контракт фида для OrderCycle."""

    prices: asyncio.Queue[float]
    order_events: asyncio.Queue[dict[str, Any]]
    exec_events: asyncio.Queue[dict[str, Any]]
    last_price: float


def _order_link_id() -> str:
    return f"zk-{uuid.uuid4().hex[:16]}"


def _order_id(resp: dict[str, Any]) -> str:
    return str(resp.get("result", {}).get("orderId", ""))


class OrderCycle:
    """Цикл робота: брекет ±offset, fill любой стороны, SL/TP."""

    def __init__(
        self,
        cfg: ZakolConfig,
        client: Any,
        feed: FeedProtocol,
        store: StateStore,
        tick_size: float = 0.0,
    ) -> None:
        self.cfg = cfg
        self.client = client
        self.feed = feed
        self.store = store
        self.tick_size = tick_size
        self._stop = asyncio.Event()

    def request_stop(self) -> None:
        self._stop.set()

    def _pendings(self) -> list[PendingOrder]:
        st = self.store.state
        return [p for p in (st.pending_buy, st.pending_sell) if p is not None]

    async def run(self) -> None:
        while not self._stop.is_set():
            st = self.store.state
            if st.kill or st.phase == PHASE_STOPPED:
                await self._enter_stopped()
                break
            if st.phase == PHASE_IN_POSITION:
                await self._manage_position()
            elif st.phase == PHASE_WORKING:
                await self._await_working()
            else:
                await self._place_bracket()
        await self._on_shutdown()

    async def _order_qty(self, price: float) -> float:
        if self.cfg.fixed_qty > 0:
            return self.cfg.fixed_qty
        notional = self.cfg.order_notional()
        if price <= 0 or notional <= 0:
            raise ValueError("cannot size order: price/notional invalid")
        return notional / price

    async def _place_bracket(self) -> None:
        """Поставить пару PostOnly-лимиток: buy -offset и sell +offset."""
        price = await self._current_price()
        qty = await self._order_qty(price)
        buy_price = limit_buy_price(price, self.cfg.offset_pct, self.tick_size)
        sell_price = limit_sell_price(price, self.cfg.offset_pct, self.tick_size)
        buy_link = _order_link_id()
        resp_buy = await self.client.place_order(
            symbol=self.cfg.symbol,
            side="Buy",
            qty=qty,
            order_type="Limit",
            price=buy_price,
            post_only=True,
            order_link_id=buy_link,
        )
        self.store.state.pending_buy = PendingOrder(
            order_id=_order_id(resp_buy),
            order_link_id=buy_link,
            price=buy_price,
            qty=qty,
            placed_at=time.monotonic(),
            filled_qty=0.0,
            side="Buy",
        )
        sell_link = _order_link_id()
        resp_sell = await self.client.place_order(
            symbol=self.cfg.symbol,
            side="Sell",
            qty=qty,
            order_type="Limit",
            price=sell_price,
            post_only=True,
            order_link_id=sell_link,
        )
        self.store.state.pending_sell = PendingOrder(
            order_id=_order_id(resp_sell),
            order_link_id=sell_link,
            price=sell_price,
            qty=qty,
            placed_at=time.monotonic(),
            filled_qty=0.0,
            side="Sell",
        )
        self.store.state.phase = PHASE_WORKING
        await self.store.save()
        logger.info(
            "bracket buy %.8g / sell %.8g qty=%.8g",
            buy_price,
            sell_price,
            qty,
        )

    async def _await_working(self) -> None:
        pendings = self._pendings()
        if not pendings:
            self.store.state.phase = PHASE_IDLE
            await self.store.save()
            return
        deadline = min(p.placed_at + self.cfg.ttl_sec for p in pendings)
        logger.debug("wait fill ttl=%.1fs", max(deadline - time.monotonic(), 0.0))
        try:
            await asyncio.wait_for(
                self._watch_until_fill_or_deadline(deadline),
                timeout=max(deadline - time.monotonic(), 0.001) + 0.05,
            )
        except asyncio.TimeoutError:
            pass
        await self._on_ttl()

    async def _watch_until_fill_or_deadline(self, deadline: float) -> None:
        while not self._stop.is_set() and time.monotonic() < deadline:
            if not self._pendings() or self.store.state.phase != PHASE_WORKING:
                return
            done, _ = await asyncio.wait(
                [
                    asyncio.create_task(self.feed.order_events.get()),
                    asyncio.create_task(self.feed.exec_events.get()),
                ],
                timeout=min(0.5, max(deadline - time.monotonic(), 0.01)),
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in done:
                msg = task.result()
                self._handle_ws_fill(msg)

    def _handle_ws_fill(self, msg: dict[str, Any]) -> None:
        data = msg.get("data")
        rows = data if isinstance(data, list) else [data]
        matched: PendingOrder | None = None
        for row in rows:
            if not isinstance(row, dict):
                continue
            row_id = str(row.get("orderId") or row.get("orderLinkId") or "")
            link = str(row.get("orderLinkId") or "")
            for p in self._pendings():
                if row_id == p.order_id or link == p.order_link_id:
                    matched = p
                    break
            if matched is None:
                continue
            if row.get("orderStatus") in (
                "Filled",
                "PartiallyFilledCanceled",
                "Cancelled",
                "Rejected",
                "Deactivated",
                "Expired",
            ):
                filled = float(row.get("cumExecQty") or 0)
                matched.filled_qty = max(matched.filled_qty, filled)
            if row.get("execType") or row.get("execQty"):
                filled = float(row.get("cumExecQty") or row.get("execQty") or 0)
                if filled:
                    matched.filled_qty = max(matched.filled_qty, filled)
            break
        if matched is None:
            return
        filled = matched.filled_qty
        if filled > 0 and filled >= matched.qty * self.cfg.partial_fill_pct:
            asyncio.get_running_loop().create_task(self._on_filled(matched))

    async def _on_ttl(self) -> None:
        pendings = self._pendings()
        if not pendings:
            self.store.state.phase = PHASE_IDLE
            await self.store.save()
            return
        for p in pendings:
            if p.filled_qty > 0:
                if fill_ratio(p.filled_qty, p.qty) >= self.cfg.partial_fill_pct:
                    await self._on_filled(p)
                    return
                await self._cancel_pending(p, keep_partial=True)
                await self._on_filled(p)
                return
        ref = self._ref_price()
        price = await self._current_price()
        delta = abs(price - ref) / ref if ref else 1.0
        kind = "extend" if delta < self.cfg.min_price_change else "re-place"
        logger.info("ttl %s delta=%.4f%%", kind, delta * 100)
        for p in pendings:
            await self._cancel_pending(p)
        self.store.state.phase = PHASE_IDLE
        await self.store.save()

    def _ref_price(self) -> float | None:
        """Цена, от которой ставился брекет (ref для delta)."""
        st = self.store.state
        if st.pending_buy is not None:
            return st.pending_buy.price / (1.0 - self.cfg.offset_pct)
        if st.pending_sell is not None:
            return st.pending_sell.price / (1.0 + self.cfg.offset_pct)
        return None

    async def _cancel_pending(
        self, pending: PendingOrder, keep_partial: bool = False
    ) -> None:
        try:
            await self.client.cancel_order(self.cfg.symbol, pending.order_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("cancel failed: %s", exc)
        if keep_partial and pending.filled_qty > 0:
            logger.info("partial kept qty=%.8g", pending.filled_qty)
        st = self.store.state
        if st.pending_buy is pending:
            st.pending_buy = None
        if st.pending_sell is pending:
            st.pending_sell = None

    async def _on_filled(self, pending: PendingOrder) -> None:
        filled = pending.filled_qty
        if filled <= 0 or fill_ratio(filled, pending.qty) < self.cfg.partial_fill_pct:
            await self._cancel_pending(pending)
            self.store.state.phase = PHASE_IDLE
            await self.store.save()
            return
        entry = pending.price
        side = "long" if pending.side == "Buy" else "short"
        pos = OpenPosition(
            entry_price=entry,
            qty=filled,
            peak_price=entry,
            stop_loss=initial_stop(entry, self.cfg.stop_pct, side),
            side=side,
            opened_at=time.time(),
        )
        other = (
            self.store.state.pending_sell
            if pending.side == "Buy"
            else self.store.state.pending_buy
        )
        if other is not None:
            await self._cancel_pending(other)
        self.store.state.pending_buy = None
        self.store.state.pending_sell = None
        self.store.state.position = pos
        self.store.state.phase = PHASE_IN_POSITION
        await self.store.save()
        await self._apply_server_sl_tp(pos)
        logger.info(
            "FILL %s entry=%.8g qty=%.8g sl=%.8g",
            side,
            entry,
            pos.qty,
            pos.stop_loss,
        )

    async def _manage_position(self) -> None:
        pos = self.store.state.position
        if pos is None:
            self.store.state.phase = PHASE_IDLE
            await self.store.save()
            return
        if await self._position_gone(pos):
            await self._close_position(reason="exchange_exit")
            return
        price = await self._current_price()
        decision = on_price(
            entry=pos.entry_price,
            peak=pos.peak_price,
            price=price,
            current_stop=pos.stop_loss,
            cfg=self.cfg,
            be_active=pos.be_active,
            side=pos.side,
        )
        if decision.update_server:
            if pos.side == "short":
                pos.peak_price = min(pos.peak_price, price)
            else:
                pos.peak_price = max(pos.peak_price, price)
            pos.stop_loss = decision.stop_loss
            pos.be_active = decision.be_active
            pos.trail_stop = decision.trail_stop
            await self.store.save()
            await self._apply_server_sl_tp(pos)
            logger.debug("risk update sl=%.8g be=%s", pos.stop_loss, pos.be_active)
        try:
            await asyncio.wait_for(self.feed.prices.get(), timeout=1.0)
        except asyncio.TimeoutError:
            pass

    async def _position_gone(self, pos: OpenPosition) -> bool:
        try:
            open_pos = await self.client.get_position(self.cfg.symbol)
        except Exception as exc:  # noqa: BLE001
            logger.warning("get_position: %s", exc)
            return False
        if open_pos is None or float(open_pos.size) <= 0:
            return True
        threshold = pos.qty * (1.0 - self.cfg.partial_fill_pct)
        return bool(float(open_pos.size) < threshold * (1.0 - 1e-6))

    async def _apply_server_sl_tp(self, pos: OpenPosition) -> None:
        take = None
        if self.cfg.take_pct > 0:
            take = take_level(pos.entry_price, self.cfg.take_pct, pos.side)
        try:
            await self.client.set_stop_loss_take_profit(
                symbol=self.cfg.symbol,
                stop_loss=pos.stop_loss,
                take_profit=take,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("set SL/TP: %s", exc)

    async def _close_position(self, reason: str) -> None:
        pos = self.store.state.position
        if pos is None:
            self.store.state.phase = PHASE_IDLE
            await self.store.save()
            return
        price = await self._current_price()
        pnl = position_pnl(pos.entry_price, price, pos.qty, pos.side)
        self.store.state.session_pnl += pnl
        self.store.state.peak_session_pnl = max(
            self.store.state.peak_session_pnl,
            self.store.state.session_pnl,
        )
        self.store.state.position = None
        self.store.state.phase = PHASE_IDLE
        if should_kill(
            session_pnl=self.store.state.session_pnl,
            peak_session_pnl=self.store.state.peak_session_pnl,
            deposit_usd=self.cfg.deposit_usd,
            max_loss_usd=self.cfg.max_loss_usd,
            max_drawdown_pct=self.cfg.max_drawdown_pct,
        ):
            self.store.state.kill = True
            logger.error(
                "KILL: pnl=%.2f peak=%.2f dd=%.2f%% (%s)",
                self.store.state.session_pnl,
                self.store.state.peak_session_pnl,
                (
                    (self.store.state.peak_session_pnl - self.store.state.session_pnl)
                    / max(self.cfg.deposit_usd, 1e-9)
                    * 100.0
                ),
                reason,
            )
        else:
            logger.info("close %s pnl=%.2f", reason, pnl)
        await self.store.save()

    async def _enter_stopped(self) -> None:
        try:
            await self.client.cancel_all_orders(self.cfg.symbol)
        except Exception as exc:  # noqa: BLE001
            logger.warning("cancel_all on stop: %s", exc)
        self.store.state.phase = PHASE_STOPPED
        self.store.state.pending_buy = None
        self.store.state.pending_sell = None
        await self.store.save()
        logger.warning("STOPPED kill=%s", self.store.state.kill)

    async def _on_shutdown(self) -> None:
        for p in self._pendings():
            logger.info("shutdown: cancel pending %s", p.order_id)
            try:
                await self.client.cancel_order(self.cfg.symbol, p.order_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("shutdown cancel: %s", exc)
        self.store.state.pending_buy = None
        self.store.state.pending_sell = None
        await self.store.save()

    async def recover(self) -> None:
        """Синхронизировать state с биржей после рестарта."""
        st = self.store.state
        if st.phase == PHASE_WORKING and self._pendings():
            orders = await self.client.get_open_orders(self.cfg.symbol)
            ids = {str(o.get("orderId", "")) for o in orders}
            for attr in ("pending_buy", "pending_sell"):
                p = getattr(st, attr)
                if p is not None and p.order_id not in ids:
                    logger.warning("pending order gone (%s)", attr)
                    setattr(st, attr, None)
            if not self._pendings():
                st.phase = PHASE_IDLE
                await self.store.save()
        pos = await self.client.get_position(self.cfg.symbol)
        if pos is not None and pos.size > 0:
            if st.position is None:
                side = "long" if pos.side == "Buy" else "short"
                st.position = OpenPosition(
                    entry_price=pos.avg_price,
                    qty=pos.size,
                    peak_price=pos.avg_price,
                    stop_loss=initial_stop(pos.avg_price, self.cfg.stop_pct, side),
                    side=side,
                    opened_at=time.time(),
                )
            st.phase = PHASE_IN_POSITION
            await self.store.save()
            logger.info(
                "recovered position size=%.8g entry=%.8g side=%s",
                pos.size,
                pos.avg_price,
                st.position.side,
            )
        elif st.phase == PHASE_IN_POSITION:
            st.position = None
            st.phase = PHASE_IDLE
            await self.store.save()

    async def _current_price(self) -> float:
        if self.feed.last_price > 0:
            return self.feed.last_price
        price = await self.client.get_price(self.cfg.symbol)
        self.feed.last_price = price
        return float(price)
