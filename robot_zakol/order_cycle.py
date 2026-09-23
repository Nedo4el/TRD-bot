"""State machine: TTL-лимитка → fill → позиция → SL/TP/trail → IDLE."""

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
    on_price,
    should_kill,
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


class OrderCycle:
    """Основной цикл робота: place/cancel/re-place и управление позицией."""

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
        self._cancel_reason: str | None = None

    def request_stop(self) -> None:
        self._stop.set()

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
                await self._place_limit()
        await self._on_shutdown()

    async def _order_qty(self, price: float) -> float:
        if self.cfg.fixed_qty > 0:
            return self.cfg.fixed_qty
        notional = self.cfg.order_notional()
        if price <= 0 or notional <= 0:
            raise ValueError("cannot size order: price/notional invalid")
        return notional / price

    async def _place_limit(self) -> None:
        price = await self._current_price()
        qty = await self._order_qty(price)
        target = limit_buy_price(price, self.cfg.offset_pct, self.tick_size)
        link = _order_link_id()
        resp = await self.client.place_order(
            symbol=self.cfg.symbol,
            side="Buy",
            qty=qty,
            order_type="Limit",
            price=target,
            post_only=True,
            order_link_id=link,
        )
        result = resp.get("result", {})
        order_id = str(result.get("orderId", ""))
        self.store.state.phase = PHASE_WORKING
        self.store.state.pending = PendingOrder(
            order_id=order_id,
            order_link_id=link,
            price=target,
            qty=qty,
            placed_at=time.monotonic(),
            filled_qty=0.0,
        )
        await self.store.save()
        logger.info(
            "place buy %.8g qty=%.8g link=%s",
            target,
            qty,
            link,
        )

    async def _await_working(self) -> None:
        pending = self.store.state.pending
        if pending is None:
            self.store.state.phase = PHASE_IDLE
            await self.store.save()
            return
        loop = asyncio.get_running_loop()
        deadline = pending.placed_at + self.cfg.ttl_sec
        logger.debug("wait fill ttl=%.1fs", max(deadline - time.monotonic(), 0.0))
        try:
            await asyncio.wait_for(
                self._watch_until_fill_or_deadline(deadline),
                timeout=max(deadline - time.monotonic(), 0.001) + 0.05,
            )
        except asyncio.TimeoutError:
            pass
        _ = loop
        await self._on_ttl()

    async def _watch_until_fill_or_deadline(self, deadline: float) -> None:
        while not self._stop.is_set() and time.monotonic() < deadline:
            if (
                self.store.state.pending is None
                or self.store.state.phase != PHASE_WORKING
            ):
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
        pending = self.store.state.pending
        if pending is None:
            return
        for row in rows:
            if not isinstance(row, dict):
                continue
            row_id = str(row.get("orderId") or row.get("orderLinkId") or "")
            link = str(row.get("orderLinkId") or "")
            if row_id != pending.order_id and link != pending.order_link_id:
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
                pending.filled_qty = max(pending.filled_qty, filled)
            if row.get("execType") or row.get("execQty"):
                filled = float(row.get("cumExecQty") or row.get("execQty") or 0)
                if filled:
                    pending.filled_qty = max(pending.filled_qty, filled)
        if pending.filled_qty >= pending.qty * self.cfg.partial_fill_pct:
            asyncio.get_running_loop().create_task(self._on_filled())

    async def _on_ttl(self) -> None:
        pending = self.store.state.pending
        if pending is None:
            self.store.state.phase = PHASE_IDLE
            await self.store.save()
            return
        ref_price = pending.price / (1.0 - self.cfg.offset_pct)
        price = await self._current_price()
        delta = abs(price - ref_price) / ref_price if ref_price else 1.0
        if pending.filled_qty >= pending.qty * self.cfg.partial_fill_pct:
            await self._on_filled()
            return
        if pending.filled_qty > 0:
            await self._cancel_pending(keep_partial=True)
            await self._on_filled()
            return
        if delta < self.cfg.min_price_change:
            logger.info("ttl extend delta=%.4f%%", delta * 100)
            await self._cancel_pending()
            self.store.state.phase = PHASE_IDLE
            await self.store.save()
            return
        logger.info("ttl re-place delta=%.4f%%", delta * 100)
        await self._cancel_pending()
        self.store.state.phase = PHASE_IDLE
        await self.store.save()

    async def _cancel_pending(self, keep_partial: bool = False) -> None:
        pending = self.store.state.pending
        if pending is None:
            return
        try:
            await self.client.cancel_order(self.cfg.symbol, pending.order_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("cancel failed: %s", exc)
        if keep_partial and pending.filled_qty > 0:
            logger.info("partial kept qty=%.8g", pending.filled_qty)
        self.store.state.pending = None

    async def _on_filled(self) -> None:
        pending = self.store.state.pending
        if pending is None:
            return
        filled = pending.filled_qty
        if fill_ratio(filled, pending.qty) < self.cfg.partial_fill_pct:
            await self._cancel_pending()
            self.store.state.phase = PHASE_IDLE
            await self.store.save()
            return
        entry = pending.price
        pos = OpenPosition(
            entry_price=entry,
            qty=filled if filled > 0 else pending.qty,
            peak_price=entry,
            stop_loss=initial_stop(entry, self.cfg.stop_pct),
            opened_at=time.time(),
        )
        self.store.state.pending = None
        self.store.state.position = pos
        self.store.state.phase = PHASE_IN_POSITION
        await self.store.save()
        await self._apply_server_sl_tp(pos)
        logger.info(
            "FILL entry=%.8g qty=%.8g sl=%.8g",
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
        )
        if decision.update_server:
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
        return bool(float(open_pos.size) < pos.qty * (1.0 - self.cfg.partial_fill_pct))

    async def _apply_server_sl_tp(self, pos: OpenPosition) -> None:
        try:
            await self.client.set_stop_loss_take_profit(
                symbol=self.cfg.symbol,
                stop_loss=pos.stop_loss,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("set SL: %s", exc)

    async def _close_position(self, reason: str) -> None:
        pos = self.store.state.position
        if pos is None:
            self.store.state.phase = PHASE_IDLE
            await self.store.save()
            return
        pnl = (await self._current_price() - pos.entry_price) * pos.qty
        self.store.state.session_pnl += pnl
        if pnl < 0:
            self.store.state.consecutive_losses += 1
        else:
            self.store.state.consecutive_losses = 0
        self.store.state.position = None
        self.store.state.phase = PHASE_IDLE
        if should_kill(
            self.store.state.consecutive_losses,
            self.store.state.session_pnl,
            self.cfg.max_loss_usd,
            self.cfg.max_consecutive_losses,
        ):
            self.store.state.kill = True
            logger.error(
                "KILL: pnl=%.2f losses=%d (%s)",
                self.store.state.session_pnl,
                self.store.state.consecutive_losses,
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
        self.store.state.pending = None
        await self.store.save()
        logger.warning("STOPPED kill=%s", self.store.state.kill)

    async def _on_shutdown(self) -> None:
        pending = self.store.state.pending
        if pending is not None and self.store.state.phase == PHASE_WORKING:
            logger.info("shutdown: cancel pending %s", pending.order_id)
            try:
                await self.client.cancel_order(self.cfg.symbol, pending.order_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("shutdown cancel: %s", exc)
            self.store.state.pending = None
        await self.store.save()

    async def recover(self) -> None:
        """Синхронизировать state с биржей после рестарта."""
        st = self.store.state
        if st.phase == PHASE_WORKING and st.pending is not None:
            orders = await self.client.get_open_orders(self.cfg.symbol)
            ids = {str(o.get("orderId", "")) for o in orders}
            if st.pending.order_id not in ids:
                logger.warning("pending order gone — IDLE")
                st.pending = None
                st.phase = PHASE_IDLE
                await self.store.save()
        pos = await self.client.get_position(self.cfg.symbol)
        if pos is not None and pos.size > 0:
            if st.position is None:
                st.position = OpenPosition(
                    entry_price=pos.avg_price,
                    qty=pos.size,
                    peak_price=pos.avg_price,
                    stop_loss=initial_stop(pos.avg_price, self.cfg.stop_pct),
                    opened_at=time.time(),
                )
            st.phase = PHASE_IN_POSITION
            await self.store.save()
            logger.info(
                "recovered position size=%.8g entry=%.8g", pos.size, pos.avg_price
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
