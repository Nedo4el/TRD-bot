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
    last_balance: float
    resync_needed: bool


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
        qty_step: float = 0.0,
        notifier: Any = None,
    ) -> None:
        self.cfg = cfg
        self.client = client
        self.feed = feed
        self.store = store
        self.tick_size = tick_size
        self.qty_step = qty_step
        self.notifier = notifier
        self._stop = asyncio.Event()
        self._last_balance_poll = 0.0

    def request_stop(self) -> None:
        self._stop.set()

    def _pendings(self) -> list[PendingOrder]:
        st = self.store.state
        return [p for p in (st.pending_buy, st.pending_sell) if p is not None]

    def _notify(self, text: str) -> None:
        """Отправить уведомление фоном, не блокируя торговый цикл."""
        if self.notifier is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.debug("notify без event loop: %s", text)
            return
        loop.create_task(self._safe_notify(text))

    async def _safe_notify(self, text: str) -> None:
        try:
            await self.notifier.notify(text)
        except Exception as exc:  # noqa: BLE001
            logger.warning("notifier: %s", exc)

    async def run(self) -> None:
        try:
            while not self._stop.is_set():
                await self._maybe_resync()
                await self._maybe_equity_kill()
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
        except Exception as exc:
            logger.exception("run прерван")
            self._notify(f"zakol: исключение в цикле — {exc}")
            raise
        finally:
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
        await self._cancel_stray_orders()
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
        # сохраняем сразу: если второй ордер упадёт — на диске уже есть первый
        await self.store.save()
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

    async def _cancel_stray_orders(self) -> None:
        """Снять обычные ордера, которых нет в state (после сбоя/рестарта).

        Conditional-ордера (SL/TP) не трогаем: у них заполнен stopOrderType.
        """
        known = {p.order_id for p in self._pendings()}
        try:
            orders = await self.client.get_open_orders(self.cfg.symbol)
        except Exception as exc:  # noqa: BLE001
            logger.warning("get_open_orders: %s", exc)
            return
        for order in orders:
            if str(order.get("stopOrderType") or "UNKNOWN") not in ("", "UNKNOWN"):
                continue
            order_id = str(order.get("orderId") or "")
            if not order_id or order_id in known:
                continue
            logger.warning("stray order %s — cancel", order_id)
            try:
                await self.client.cancel_order(self.cfg.symbol, order_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("cancel stray %s: %s", order_id, exc)

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
            waiters = [
                asyncio.create_task(self.feed.order_events.get()),
                asyncio.create_task(self.feed.exec_events.get()),
            ]
            try:
                await asyncio.wait(
                    waiters,
                    timeout=min(0.5, max(deadline - time.monotonic(), 0.01)),
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                # отменяем невостребованные waiter'ы: иначе очередь начнёт
                # отдавать события брошенным задачам (событие филла теряется)
                for task in waiters:
                    if not task.done():
                        task.cancel()
                results = await asyncio.gather(*waiters, return_exceptions=True)
                for result in results:
                    if isinstance(result, dict):
                        self._handle_ws_fill(result)

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
            await self._sync_fill_from_rest(p)
        for p in pendings:
            if p.filled_qty > 0:
                # частичное исполнение к дедлайну = позиция (как в бэктесте)
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

    async def _sync_fill_from_rest(self, pending: PendingOrder) -> None:
        """Фолбэк: WS молчит — узнать факт исполнения через REST."""
        if pending.filled_qty > 0:
            return
        try:
            order = await self.client.get_order(self.cfg.symbol, pending.order_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("get_order %s: %s", pending.order_id, exc)
            return
        if not order:
            return
        filled = float(order.get("cumExecQty") or 0.0)
        if filled > 0:
            pending.filled_qty = max(pending.filled_qty, filled)
            logger.info(
                "REST fill %s qty=%.8g status=%s",
                pending.order_id,
                filled,
                order.get("orderStatus"),
            )

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
    ) -> bool:
        """Снять лимитку. False — биржа отказала (ордер может остаться)."""
        ok = True
        try:
            await self.client.cancel_order(self.cfg.symbol, pending.order_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("cancel failed: %s", exc)
            ok = False
            self._notify(f"zakol: cancel {pending.order_id} не прошёл — {exc}")
        if keep_partial and pending.filled_qty > 0:
            logger.info("partial kept qty=%.8g", pending.filled_qty)
        st = self.store.state
        if st.pending_buy is pending:
            st.pending_buy = None
        if st.pending_sell is pending:
            st.pending_sell = None
        return ok

    async def _on_filled(self, pending: PendingOrder) -> None:
        filled = pending.filled_qty
        if filled <= 0:
            await self._cancel_pending(pending)
            self.store.state.phase = PHASE_IDLE
            await self.store.save()
            return
        partial = fill_ratio(filled, pending.qty) < self.cfg.partial_fill_pct
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
            cancelled = await self._cancel_pending(other)
            if not cancelled:
                await self._guard_reduce_only(other, side)
        self.store.state.pending_buy = None
        self.store.state.pending_sell = None
        self.store.state.position = pos
        self.store.state.phase = PHASE_IN_POSITION
        await self.store.save()
        await self._apply_server_sl_tp(pos)
        logger.info(
            "FILL %s entry=%.8g qty=%.8g sl=%.8g partial=%s",
            side,
            entry,
            pos.qty,
            pos.stop_loss,
            partial,
        )
        self._notify(
            f"zakol FILL {side} {self.cfg.symbol} entry={entry:.8g} qty={filled:.8g}",
        )

    async def _guard_reduce_only(self, pending: PendingOrder, pos_side: str) -> None:
        """Снятие противоположной лимитки не удалось.

        Переставляем её с reduceOnly: она сможет только закрыть позицию,
        но никогда не откроет противоположную (переворот без стопа).
        """
        if not await self._order_is_open(pending.order_id):
            return  # отмена фактически прошла — на бирже ордера нет
        side = "Sell" if pos_side == "long" else "Buy"
        try:
            resp = await self.client.place_order(
                symbol=self.cfg.symbol,
                side=side,
                qty=pending.qty,
                order_type="Limit",
                price=pending.price,
                post_only=True,
                reduce_only=True,
                order_link_id=f"{pending.order_link_id}-ro",
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("reduceOnly guard: %s", exc)
            self._notify(f"zakol: ордер {side} не снят и не заменён — {exc}")
            return
        logger.warning("reduceOnly guard поставлен: %s", _order_id(resp))
        self._notify(f"zakol: ордер {side} не снят — заменён на reduceOnly")

    async def _order_is_open(self, order_id: str) -> bool:
        try:
            orders = await self.client.get_open_orders(self.cfg.symbol)
        except Exception as exc:  # noqa: BLE001
            logger.warning("get_open_orders: %s", exc)
            return False
        return any(str(o.get("orderId") or "") == order_id for o in orders)

    async def _manage_position(self) -> None:
        pos = self.store.state.position
        if pos is None:
            self.store.state.phase = PHASE_IDLE
            await self.store.save()
            return
        ok, size, unrealised = await self._position_snapshot()
        if not ok:
            return  # REST не ответил — не рискуем закрыть позицию по ошибке
        if size <= 0:
            await self._close_position(reason="exchange_exit")
            return
        if self._kill_by_equity(unrealised):
            await self._trigger_kill("background")
            return
        if self._size_gone(pos.qty, size):
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

    async def _position_snapshot(self) -> tuple[bool, float, float]:
        """Срез позиции с биржи: (данные получены, size, unrealised PnL)."""
        try:
            ex = await self.client.get_position(self.cfg.symbol)
        except Exception as exc:  # noqa: BLE001
            logger.warning("get_position: %s", exc)
            return False, 0.0, 0.0
        if ex is None:
            return True, 0.0, 0.0
        return (
            True,
            float(ex.size),
            float(getattr(ex, "unrealised_pnl", 0.0) or 0.0),
        )

    def _size_gone(self, state_qty: float, size: float) -> bool:
        """Позиция на бирже исчезла/резко меньше — с допуском на шаг объёма."""
        if size <= 0:
            return True
        tolerance = max(self.qty_step * 0.5, state_qty * 1e-6, 1e-12)
        return size < state_qty - tolerance

    def _kill_by_equity(self, unrealised: float = 0.0) -> bool:
        """Kill-switch по учтённой и нереализованной прибыли."""
        st = self.store.state
        if st.kill:
            return True
        return should_kill(
            session_pnl=st.session_pnl + unrealised,
            peak_session_pnl=st.peak_session_pnl,
            deposit_usd=self.cfg.deposit_usd,
            max_loss_usd=self.cfg.max_loss_usd,
            max_drawdown_pct=self.cfg.max_drawdown_pct,
        )

    async def _trigger_kill(self, reason: str) -> None:
        st = self.store.state
        st.kill = True
        await self.store.save()
        dd = (
            (st.peak_session_pnl - st.session_pnl)
            / max(self.cfg.deposit_usd, 1e-9)
            * 100.0
        )
        logger.error(
            "KILL (%s): pnl=%.2f peak=%.2f dd=%.2f%%",
            reason,
            st.session_pnl,
            st.peak_session_pnl,
            dd,
        )
        self._notify(
            f"zakol KILL ({reason}): pnl={st.session_pnl:.2f} "
            f"peak={st.peak_session_pnl:.2f} dd={dd:.2f}%",
        )

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
        pnl = await self._exit_pnl(pos, reason)
        st = self.store.state
        st.session_pnl += pnl
        st.peak_session_pnl = max(st.peak_session_pnl, st.session_pnl)
        st.position = None
        st.phase = PHASE_IDLE
        if self._kill_by_equity():
            await self._trigger_kill(reason)
        else:
            logger.info("close %s pnl=%.2f", reason, pnl)
            self._notify(f"zakol close {reason}: pnl={pnl:+.2f}")
        await self.store.save()

    async def _exit_pnl(self, pos: OpenPosition, reason: str) -> float:
        """PnL закрытия: для exchange_exit берём факт с биржи, не текущую цену."""
        if reason == "exchange_exit":
            realized = await self._realized_pnl()
            if realized is not None:
                logger.info("exchange_exit pnl=%.4f (closedPnl, REST)", realized)
                return realized
        price = await self._current_price()
        return position_pnl(pos.entry_price, price, pos.qty, pos.side)

    async def _realized_pnl(self) -> float | None:
        """Последний закрытый ордер символа (None — данных нет)."""
        try:
            pnl: float | None = await self.client.get_last_closed_pnl(self.cfg.symbol)
        except Exception as exc:  # noqa: BLE001
            logger.warning("get_last_closed_pnl: %s", exc)
            return None
        return pnl

    async def _enter_stopped(self) -> None:
        # cancel_all_orders снимает и conditional (SL/TP) — снимаем только свои
        # входные лимитки, защита позиции на бирже должна остаться
        for p in self._pendings():
            logger.info("stop: cancel pending %s", p.order_id)
            try:
                await self.client.cancel_order(self.cfg.symbol, p.order_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("stop cancel %s: %s", p.order_id, exc)
        self.store.state.phase = PHASE_STOPPED
        self.store.state.pending_buy = None
        self.store.state.pending_sell = None
        pos = self.store.state.position
        if pos is not None:
            await self._apply_server_sl_tp(pos)
        await self.store.save()
        logger.warning("STOPPED kill=%s", self.store.state.kill)
        self._notify(f"zakol STOPPED kill={self.store.state.kill}")

    async def _maybe_resync(self) -> None:
        """После реконнекта WS — сверить state с биржей через REST."""
        if not self.feed.resync_needed:
            return
        self.feed.resync_needed = False
        logger.warning("REST-ресинк после реконнекта WS")
        try:
            await self.recover()
        except Exception as exc:  # noqa: BLE001
            logger.warning("resync failed: %s", exc)
            self._notify(f"zakol: ресинк не удался — {exc}")

    async def _rest_equity(self) -> float:
        """Баланс счёта через REST, не чаще раза в 30с (фолбэк при мёртвом WS)."""
        now = time.monotonic()
        if now - self._last_balance_poll < 30.0:
            return 0.0
        self._last_balance_poll = now
        try:
            return float(await self.client.get_balance())
        except Exception as exc:  # noqa: BLE001
            logger.warning("get_balance: %s", exc)
            return 0.0

    def _kill_by_balance(self, equity: float) -> bool:
        """Kill по балансу: падение от пика (DD) или от стартового (max loss)."""
        st = self.store.state
        if st.kill:
            return True
        return should_kill(
            session_pnl=equity - st.start_equity,
            peak_session_pnl=st.peak_equity - st.start_equity,
            deposit_usd=st.start_equity,
            max_loss_usd=self.cfg.max_loss_usd,
            max_drawdown_pct=self.cfg.max_drawdown_pct,
        )

    async def _maybe_equity_kill(self) -> None:
        """Фоновый контроль баланса: equity берём из wallet stream, иначе REST."""
        if self.cfg.max_loss_usd <= 0 and self.cfg.max_drawdown_pct <= 0:
            return
        equity = self.feed.last_balance
        if equity <= 0:
            equity = await self._rest_equity()
        if equity <= 0:
            return
        st = self.store.state
        if st.start_equity <= 0:
            st.start_equity = equity
        st.peak_equity = max(st.peak_equity, st.start_equity, equity)
        if not self._kill_by_balance(equity):
            return
        logger.error(
            "equity kill: %.2f start=%.2f peak=%.2f",
            equity,
            st.start_equity,
            st.peak_equity,
        )
        await self._trigger_kill("equity")

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
            if st.phase == PHASE_WORKING and self._pendings():
                # позиция уже открыта — остатки брекета снимаем
                for p in self._pendings():
                    logger.warning("recover: bracket leftover %s — cancel", p.order_id)
                    try:
                        await self.client.cancel_order(self.cfg.symbol, p.order_id)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("recover cancel %s: %s", p.order_id, exc)
                st.pending_buy = None
                st.pending_sell = None
            st.phase = PHASE_STOPPED if st.kill else PHASE_IN_POSITION
            # SL/TP всегда заново: после cancel_all/рестарта их может не быть
            await self._apply_server_sl_tp(st.position)
            await self.store.save()
            logger.info(
                "recovered position size=%.8g entry=%.8g side=%s phase=%s",
                pos.size,
                pos.avg_price,
                st.position.side,
                st.phase,
            )
        elif st.phase == PHASE_IN_POSITION:
            realized = await self._realized_pnl()
            if realized is not None:
                st.session_pnl += realized
                st.peak_session_pnl = max(st.peak_session_pnl, st.session_pnl)
                logger.warning(
                    "позиция закрыта вне бота: pnl=%.4f (closedPnl, REST)",
                    realized,
                )
            else:
                logger.warning("позиция закрыта вне бота: pnl неизвестен")
            st.position = None
            st.phase = PHASE_IDLE
            await self.store.save()

    async def _current_price(self) -> float:
        if self.feed.last_price > 0:
            return self.feed.last_price
        price = await self.client.get_price(self.cfg.symbol)
        self.feed.last_price = price
        return float(price)
