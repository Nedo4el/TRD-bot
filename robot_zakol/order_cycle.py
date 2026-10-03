"""State machine: пара TTL-лимиток ±offset → fill → позиция → SL/TP → IDLE."""

from __future__ import annotations

import asyncio
import logging
import math
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from core.config import PROJECT_ROOT
from robot_zakol.config import ZakolConfig
from robot_zakol.risk import (
    cooldown_remaining,
    daily_stop_reason,
    fill_ratio,
    initial_stop,
    limit_buy_price,
    limit_sell_price,
    on_price,
    position_pnl,
    should_kill,
    slippage_ok,
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


def _order_link_id(prefix: str) -> str:
    """Уникальный orderLinkId с настраиваемым префиксом (идемпотентность)."""
    return f"{prefix}{uuid.uuid4().hex[:16]}"


def _order_id(resp: dict[str, Any]) -> str:
    return str(resp.get("result", {}).get("orderId", ""))


def _log_task_result(task: asyncio.Task[Any]) -> None:
    """E.1: залогировать упавшую фоновую задачу вместо 'never retrieved'."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("фоновая задача упала: %s", exc, exc_info=exc)


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
        # E.4: не даём _on_filled выполняться параллельно с собой же
        self._fill_lock = asyncio.Lock()
        # E.3: экспоненциальный backoff при неудачном снятии лимитки на TTL
        self._ttl_backoff = 0.0
        # последняя причина запрета входа (чтобы не спамить лог каждый цикл)
        self._gate_reason: str | None = None

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
                # файл-флаг → state.kill; дальше обработает стейт-машина
                await self._kill_switch_hit()
                st = self.store.state
                if st.kill or st.phase == PHASE_STOPPED:
                    await self._enter_stopped()
                    break
                if st.phase == PHASE_IN_POSITION:
                    await self._manage_position()
                elif st.phase == PHASE_WORKING:
                    await self._await_working()
                elif await self._entry_blocked():
                    continue  # вход запрещён: день/кулдаун/фандинг
                else:
                    await self._place_bracket()
        except Exception as exc:
            logger.exception("run прерван")
            self._notify(f"zakol: исключение в цикле — {exc}")
            raise
        finally:
            await self._on_shutdown()

    # --------------------------- Вход: разрешён? ---------------------------

    async def _entry_blocked(self) -> bool:
        """Проверки перед новым входом. True — вход запрещён, цикл ждёт.

        Каждый запрет логируется один раз (при смене причины), а не каждый цикл.
        Килл-файл проверяется отдельно, в начале каждой итерации run().
        """
        await self._roll_day()
        st = self.store.state
        reason: str | None = None
        detail = ""
        wait = 1.0
        stop = daily_stop_reason(
            st.day_pnl,
            st.day_trades,
            self.cfg.daily_loss_limit,
            self.cfg.max_trades_per_day,
        )
        if stop is not None:
            reason = stop
            detail = (
                f"дневной стоп-кран ({stop}): "
                f"pnl дня {st.day_pnl:+.2f}, сделок {st.day_trades}"
            )
        else:
            wait = cooldown_remaining(
                st.last_close_at, time.time(), self.cfg.cooldown_sec
            )
            if wait > 0:
                reason = "cooldown"
                detail = f"кулдаун после закрытия: ещё {wait:.0f}с"
            else:
                left = await self._funding_left()
                if left is not None:
                    reason = "funding"
                    detail = f"фандинг через {left:.0f}с — вход отложен"
                    wait = left
        if reason is None:
            if self._gate_reason is not None:
                logger.info("вход разрешён снова")
                self._gate_reason = None
            return False
        if reason != self._gate_reason:
            self._gate_reason = reason
            logger.warning("вход запрещён: %s", detail)
            self._notify(f"zakol: вход запрещён — {detail}")
        await asyncio.sleep(max(min(wait, 5.0), 0.1))
        return True

    def kill_switch_file(self) -> Path | None:
        """Путь к файлу-флагу ручной остановки (None — KILL_SWITCH выкл.)."""
        raw = self.cfg.kill_switch_file.strip()
        if not raw:
            return None
        path = Path(raw)
        return path if path.is_absolute() else PROJECT_ROOT / path

    async def _kill_switch_hit(self) -> bool:
        """Файл-флаг на диске — ручная остановка без правки .env."""
        path = self.kill_switch_file()
        if path is None or not path.exists() or self.store.state.kill:
            return False
        logger.error("KILL_SWITCH: найден файл %s — ручная остановка", path)
        await self._trigger_kill("manual")
        return True

    async def _roll_day(self) -> None:
        """Перевести дневные счётчики на новые сутки (граница — по UTC)."""
        today = datetime.now(timezone.utc).date().isoformat()
        st = self.store.state
        if st.day == today:
            return
        if st.day and (st.day_pnl or st.day_trades):
            logger.info(
                "сутки %s закрыты: pnl=%+.2f сделок=%d",
                st.day,
                st.day_pnl,
                st.day_trades,
            )
        st.day = today
        st.day_pnl = 0.0
        st.day_trades = 0
        await self.store.save()

    async def _funding_left(self) -> float | None:
        """FUNDING_AWARE: секунд до фандинга, если сейчас входить нельзя."""
        if not self.cfg.funding_aware:
            return None
        try:
            next_funding = await self.client.get_next_funding_time(self.cfg.symbol)
        except Exception as exc:  # noqa: BLE001
            logger.warning("get_next_funding_time: %s", exc)
            return None
        if next_funding is None:
            return None
        left = float(next_funding) - time.time()
        if 0 <= left <= self.cfg.funding_window_sec:
            return left
        return None

    async def _order_qty(self, price: float) -> float:
        """Размер ордера в единицах инструмента, кратный qty_step биржи.

        Клиент всё равно округляет вниз — считаем так сразу, чтобы лог,
        state и фактический ордер на бирже совпадали.
        """
        if self.cfg.fixed_qty > 0:
            qty = self.cfg.fixed_qty
        else:
            notional = self.cfg.order_notional()
            if price <= 0 or notional <= 0:
                raise ValueError("cannot size order: price/notional invalid")
            qty = notional / price
        if self.qty_step > 0:
            qty = math.floor(qty / self.qty_step) * self.qty_step
            if qty < self.qty_step:
                raise ValueError(
                    f"order qty {qty:g} < min step {self.qty_step:g} — "
                    "увеличь POSITION_PCT",
                )
        return qty

    def _fresh_price(self, ref: float) -> float:
        """SLIPPAGE_PCT: цена из фида ушла дальше допуска — берём свежую.

        За время cancel-раунда тик мог обновиться; лимитки считаем по нему.
        """
        actual = self.feed.last_price
        if actual > 0 and not slippage_ok(ref, actual, self.cfg.slippage_pct):
            logger.info(
                "slippage: цена %.8g → %.8g (допуск %.2f%%) — пересчёт",
                ref,
                actual,
                self.cfg.slippage_pct * 100,
            )
            return actual
        return ref

    async def _place_bracket(self) -> None:
        """Поставить PostOnly-лимитки: buy -offset (+ sell +offset, если не long_only)."""
        price = await self._current_price()
        await self._cancel_stray_orders()
        price = self._fresh_price(price)
        qty = await self._order_qty(price)
        buy_price = limit_buy_price(price, self.cfg.offset_pct, self.tick_size)
        sell_price = limit_sell_price(price, self.cfg.offset_pct, self.tick_size)
        buy_link = _order_link_id(self.cfg.order_link_prefix)
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
        if self.cfg.long_only:
            self.store.state.phase = PHASE_WORKING
            await self.store.save()
            logger.info("bracket long-only buy %.8g qty=%.8g", buy_price, qty)
            return
        sell_link = _order_link_id(self.cfg.order_link_prefix)
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
                    # E.1: исключения задач не должны уходить в молчаливый gather
                    if isinstance(result, BaseException):
                        if not isinstance(result, asyncio.CancelledError):
                            logger.error("waiter упал: %s", result, exc_info=result)
                        continue
                    if isinstance(result, dict):
                        self._handle_ws_fill(result)

    def _spawn(self, coro: Any) -> None:
        """Фоновая задача с логированием исключения (E.1)."""
        task = asyncio.get_running_loop().create_task(coro)
        task.add_done_callback(_log_task_result)

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
            self._spawn(self._on_filled(matched))

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
        if delta < self.cfg.min_price_change:
            # F.3: цена почти не сдвинулась — не мусорим cancel/replace,
            # держим те же лимитки и просто продлеваем TTL
            logger.info("ttl extend delta=%.4f%%", delta * 100)
            for p in pendings:
                p.placed_at = time.monotonic()
            await self.store.save()
            return
        logger.info("ttl re-place delta=%.4f%%", delta * 100)
        for p in pendings:
            if not await self._cancel_pending(p):
                # E.3: ордер мог остаться на бирже — state не трогаем,
                # ретраим с растущей паузой, чтобы не долбить API
                self._ttl_backoff = min(max(self._ttl_backoff * 2, 1.0), 60.0)
                logger.error(
                    "ttl cancel failed — retry через %.0fs",
                    self._ttl_backoff,
                )
                await asyncio.sleep(self._ttl_backoff)
                return
        self._ttl_backoff = 0.0
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
        """Снять лимитку. False — снять не удалось, state НЕ чистим."""
        ok = True
        try:
            await self.client.cancel_order(self.cfg.symbol, pending.order_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("cancel failed: %s", exc)
            ok = False
            self._notify(f"zakol: cancel {pending.order_id} не прошёл — {exc}")
        if not ok:
            # биржа могла отказаться, потому что ордер уже снят/исполнен
            ok = not await self._order_is_open(pending.order_id)
        if keep_partial and pending.filled_qty > 0:
            logger.info("partial kept qty=%.8g", pending.filled_qty)
        if not ok:
            # D.4: ордер жив на бирже — state обязан его помнить
            logger.error("cancel %s не прошёл — state сохранён", pending.order_id)
            return False
        st = self.store.state
        if st.pending_buy is pending:
            st.pending_buy = None
        if st.pending_sell is pending:
            st.pending_sell = None
        return True

    async def _on_filled(self, pending: PendingOrder) -> None:
        # E.4: только одна обработка филла за раз (WS-таск vs _on_ttl)
        async with self._fill_lock:
            await self._handle_filled(pending)

    async def _handle_filled(self, pending: PendingOrder) -> None:
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
                # D.4: снять не вышло — other остаётся в state (recover снимет)
                await self._guard_reduce_only(other, side)
        st = self.store.state
        if st.pending_buy is pending:
            st.pending_buy = None
        if st.pending_sell is pending:
            st.pending_sell = None
        st.position = pos
        st.phase = PHASE_IN_POSITION
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
        # B.5: take_pct=0 -> отправляем явный 0, чтобы биржа сняла старый TP
        take: float | None = 0.0
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
        await self._roll_day()
        st = self.store.state
        st.session_pnl += pnl
        st.peak_session_pnl = max(st.peak_session_pnl, st.session_pnl)
        # дневной стоп-кран + кулдаун после закрытия
        st.day_pnl += pnl
        st.day_trades += 1
        st.last_close_at = time.time()
        st.position = None
        st.phase = PHASE_IDLE
        if not st.kill and self._kill_by_equity():
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
        # позицию закроет _on_shutdown (SL/TP остаются до подтверждения)
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
        """Остановка: снять лимитки, закрыть позицию, убрать заявки.

        Вызывается при любом выходе из run(): kill-switch, SIGTERM,
        исключение в цикле. Если позицию закрыть не удалось — возвращаем
        серверный SL/TP (защита остаётся на бирже).
        """
        st = self.store.state
        for p in self._pendings():
            logger.info("shutdown: cancel pending %s", p.order_id)
            try:
                await self.client.cancel_order(self.cfg.symbol, p.order_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("shutdown cancel: %s", exc)
        st.pending_buy = None
        st.pending_sell = None
        pos = st.position
        if pos is not None:
            await self._shutdown_close(pos)
        else:
            await self._cancel_rest()
        if st.kill:
            st.phase = PHASE_STOPPED
        await self.store.save()

    async def _shutdown_close(self, pos: OpenPosition) -> None:
        """Закрыть позицию рыночным ордером; при неудаче — вернуть SL/TP."""
        side = "Buy" if pos.side == "long" else "Sell"
        try:
            await self.client.close_position(self.cfg.symbol, pos.qty, side)
        except Exception as exc:  # noqa: BLE001
            logger.warning("shutdown: закрытие не вышло — %s", exc)
            await self._apply_server_sl_tp(pos)
            return
        if not await self._wait_closed():
            logger.warning("shutdown: закрытие не подтвердилось — SL/TP остаются")
            await self._apply_server_sl_tp(pos)
            return
        try:
            await self._close_position("stop")
        except Exception as exc:  # noqa: BLE001
            logger.warning("shutdown: финализация закрытия: %s", exc)
        await self._cancel_rest()

    async def _wait_closed(self, timeout: float = 10.0) -> bool:
        """Дождаться исчезновения позиции на бирже (закрытие подтверждено)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                pos = await self.client.get_position(self.cfg.symbol)
            except Exception as exc:  # noqa: BLE001
                logger.warning("wait closed: %s", exc)
                return False
            if pos is None or pos.size <= 0:
                return True
            await asyncio.sleep(0.5)
        return False

    async def _cancel_rest(self) -> None:
        """Снять все оставшиеся заявки (лимитки/conditional-сироты)."""
        try:
            await self.client.cancel_all_orders(self.cfg.symbol)
        except Exception as exc:  # noqa: BLE001
            logger.warning("shutdown cancel_all: %s", exc)

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
            await self._roll_day()
            if realized is not None:
                st.session_pnl += realized
                st.peak_session_pnl = max(st.peak_session_pnl, st.session_pnl)
                st.day_pnl += realized
                st.day_trades += 1
                st.last_close_at = time.time()
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
