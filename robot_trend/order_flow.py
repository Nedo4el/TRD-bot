"""Технический торговый цикл robot_trend.

Разделение ответственности:
- **стратегия** (``strategy.decide``) — решает, что делать (enter/exit);
- **этот модуль** — как: исполнение ордеров, серверный SL/TP, гейты входа
  (kill → день UTC → cooldown → фандинг → слippаж), kill-switch,
  дневные счётчики, recover (state ↔ биржа).

Логика входа/выхода живёт в ``strategy.decide()`` (EMA + ADX + Supertrend),
этот модуль только исполняет её решения.
"""

from __future__ import annotations

import asyncio
import logging
import math
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.bybit_client import BybitClient, Candle, Position
from core.notifier import Notifier
from robot_trend.config import TrendConfig
from robot_trend.data_feed import DataFeed
from robot_trend.risk import (
    cooldown_remaining,
    daily_stop_reason,
    entry_qty,
    on_price,
    position_pnl,
    should_kill,
    slippage_ok,
)
from robot_trend.state import (
    PHASE_IDLE,
    PHASE_IN_POSITION,
    PHASE_STOPPED,
    OpenPosition,
    StateStore,
)
from robot_trend.strategy import Instruction, TrendStrategy

logger = logging.getLogger(__name__)

# сколько раз ждать появления позиции после Market-ордера
FILL_RETRIES = 5
FILL_DELAY = 0.2


def _utc_day() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class OrderFlow:
    """Исполнительный цикл: свечи → решение стратегии → ордера → состояние."""

    def __init__(
        self,
        cfg: TrendConfig,
        client: BybitClient,
        feed: DataFeed,
        store: StateStore,
        notifier: Notifier | None,
        strategy: TrendStrategy,
        filters: BybitClient.InstrumentFilters,
    ) -> None:
        self.cfg = cfg
        self.client = client
        self.feed = feed
        self.store = store
        self.notifier = notifier
        self.strategy = strategy
        self.filters = filters
        self._stop = asyncio.Event()
        self._last_decided: float = 0.0
        self._gate_logged: str | None = None

    # ==================== жизненный цикл ====================

    def request_stop(self) -> None:
        """Попросить цикл остановиться (после текущей итерации)."""
        self._stop.set()

    async def run(self) -> None:
        """Главный цикл: ждём свечу → решаем → исполняем."""
        try:
            await self._tick()  # старт: свечи + первое решение
            while not self._stop.is_set():
                if not await self._wait_wake():
                    break
                await self._tick()
        except Exception as exc:
            logger.exception("run упал")
            self._notify(f"trend: исключение в цикле — {exc}")
            raise
        finally:
            await self._on_shutdown()

    async def _wait_wake(self) -> bool:
        """Ждать закрытия свечи по WS, но не дольше POLL_SEC.

        Returns:
            False — запрошена остановка.
        """
        deadline = time.monotonic() + self.cfg.poll_sec
        while time.monotonic() < deadline:
            if self._stop.is_set():
                return False
            if not self.feed.candles.empty():
                # события — лишь триггер; решение всегда по свежему REST
                while not self.feed.candles.empty():
                    try:
                        self.feed.candles.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                return True
            await asyncio.sleep(0.2)
        return not self._stop.is_set()

    async def _tick(self) -> None:
        """Одна итерация: гейты/ресинк/свечи → решение стратегии."""
        await self._roll_day()
        if await self._kill_hit():
            self._stop.set()
            return
        if self.feed.resync_needed:
            self.feed.resync_needed = False
            logger.warning("ресинк: сверяем state с биржей")
            if not await self.recover():
                self.feed.resync_needed = True
        self._drain_queues()
        st = self.store.state
        if st.position is not None and await self._position_gone():
            return
        if st.position is not None:
            await self._update_risk()
        candles = await self._fetch_candles()
        closed = candles[:-1] if len(candles) > 1 else []
        if not closed:
            logger.warning("свечи недоступны — тик пропущен")
            return
        if closed[-1].open_time == self._last_decided:
            return  # новой закрытой свечи нет
        self._last_decided = closed[-1].open_time
        await self._on_candles(closed)

    async def _on_candles(self, closed: list[Candle]) -> None:
        """Отдать закрытые свечи стратегии и исполнить её решение."""
        st = self.store.state
        instruction = self.strategy.decide(closed, st.position)
        if instruction is None:
            return
        if instruction.action == "exit":
            if st.position is None:
                logger.debug("exit без позиции — игнор")
                return
            await self._exit(instruction.reason or "strategy")
        elif instruction.action == "enter":
            if st.position is not None:
                logger.debug("enter при открытой позиции — игнор")
                return
            await self._enter(instruction)
        else:
            logger.warning("неизвестный action стратегии: %r", instruction.action)

    # ==================== вход ====================

    async def _enter(self, instruction: Instruction) -> None:
        """Вход по сигналу стратегии: гейты → Market → серверный SL/TP."""
        blocked = await self._entry_blocked()
        if blocked is not None:
            self._log_gate(blocked)
            return
        self._gate_logged = None
        if instruction.stop is None:
            logger.error("вход отклонён: стратегия не дала уровень SL")
            return
        if instruction.side not in ("long", "short"):
            logger.error("вход отклонён: неизвестная сторона %r", instruction.side)
            return

        try:
            rest_price = await self.client.get_price(self.cfg.symbol)
        except Exception as exc:  # noqa: BLE001
            logger.warning("цена недоступна — вход пропущен: %s", exc)
            return
        feed_price = self.feed.last_price or rest_price
        if not slippage_ok(feed_price, rest_price, self.cfg.slippage_pct):
            self._log_gate("slippage")
            return

        qty = entry_qty(
            self.cfg.fixed_qty,
            self.cfg.deposit_usd,
            self.cfg.position_pct,
            rest_price,
        )
        qty = math.floor(qty / self.filters.qty_step) * self.filters.qty_step
        if qty <= 0 or qty < self.filters.min_qty:
            logger.error(
                "вход отклонён: qty=%s < min_qty=%s",
                qty,
                self.filters.min_qty,
            )
            return
        if qty * rest_price < self.filters.min_notional:
            logger.error(
                "вход отклонён: notional=%.2f < min=%s",
                qty * rest_price,
                self.filters.min_notional,
            )
            return

        side = "Buy" if instruction.side == "long" else "Sell"
        link = self._order_link_id()
        logger.info(
            "вход %s qty=%s ref=%.4f SL=%s TP=%s",
            instruction.side,
            qty,
            rest_price,
            instruction.stop,
            instruction.take,
        )
        try:
            await self.client.place_order(
                self.cfg.symbol,
                side,
                qty,
                "Market",
                order_link_id=link,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("ордер не выставлен: %s", exc)
            self._notify(f"trend: ошибка входа — {exc}")
            return

        pos = await self._wait_position()
        if pos is None:
            logger.error(
                "позиция не подтвердилась после Market-ордера — ждём recover",
            )
            return
        entry = pos.avg_price or rest_price
        if not slippage_ok(rest_price, entry, self.cfg.slippage_pct):
            logger.warning("проскальзывание: ref=%.4f fill=%.4f", rest_price, entry)

        # фиксируем позицию в state до SL: если стоп не встанет,
        # аварийное закрытие посчитает счётчики (день/кулдаун)
        st = self.store.state
        st.position = OpenPosition(
            entry_price=entry,
            qty=pos.size,
            side=instruction.side,
            stop_loss=instruction.stop,
            take_profit=instruction.take,
            opened_at=time.time(),
            peak_price=entry,
        )
        st.phase = PHASE_IN_POSITION
        await self.store.save()

        try:
            await self.client.set_stop_loss_take_profit(
                self.cfg.symbol,
                instruction.stop,
                instruction.take,
            )
        except Exception as exc:  # noqa: BLE001
            # защита: без стопа в позиции не остаёмся
            logger.error("SL/TP не встали (%s) — закрываем позицию", exc)
            self._notify(f"trend: SL/TP не установлены — позиция закрыта ({exc})")
            await self._safe_close(pos.size, side)
            return

        logger.info(
            "позиция открыта: %s qty=%s entry=%.4f",
            instruction.side,
            pos.size,
            entry,
        )
        self._notify(
            f"trend: открыт {instruction.side} qty={pos.size:g} "
            f"entry={entry:g} ({instruction.reason})",
        )

    async def _entry_blocked(self) -> str | None:
        """Причина блокировки входа (None — вход разрешён).

        Порядок: kill → день UTC → cooldown → фандинг → слippаж.
        """
        st = self.store.state
        if st.kill or self._kill_file_hit():
            return "kill"
        reason = daily_stop_reason(
            st.day_pnl,
            st.day_trades,
            self.cfg.daily_loss_limit,
            self.cfg.max_trades_per_day,
        )
        if reason is not None:
            return reason
        if (
            cooldown_remaining(
                st.last_close_at,
                time.time(),
                self.cfg.cooldown_sec,
            )
            > 0
        ):
            return "cooldown"
        try:
            if self.cfg.funding_aware:
                nxt = await self.client.get_next_funding_time(self.cfg.symbol)
                now = time.time()
                if nxt is not None and 0 <= nxt - now <= self.cfg.funding_window_sec:
                    return "funding"
            feed_price = self.feed.last_price
            if feed_price > 0:
                rest_price = await self.client.get_price(self.cfg.symbol)
                if not slippage_ok(feed_price, rest_price, self.cfg.slippage_pct):
                    return "slippage"
        except Exception as exc:  # noqa: BLE001
            # рынок недоступен — не входим, когда не можем проверить
            logger.warning("гейты не проверены (%s) — вход пропущен", exc)
            return "market_unavailable"
        return None

    def _log_gate(self, reason: str) -> None:
        """Логировать блокировку один раз при смене причины."""
        if reason != self._gate_logged:
            self._gate_logged = reason
            logger.info("вход заблокирован: %s", reason)

    # ==================== выход ====================

    async def _exit(self, reason: str) -> None:
        """Закрыть позицию по решению стратегии и зафиксировать итог."""
        st = self.store.state
        pos = st.position
        if pos is None:
            return
        logger.info("выход (%s)", reason)
        side = "Buy" if pos.side == "long" else "Sell"
        try:
            await self.client.close_position(self.cfg.symbol, pos.qty, side)
        except Exception as exc:  # noqa: BLE001
            logger.error("закрытие не вышло: %s", exc)
            self._notify(f"trend: ошибка выхода — {exc}")
            return
        if not await self._wait_no_position():
            # не подтверждено — позиция остаётся в state, дождётся
            # _position_gone/recover
            logger.warning("позиция не подтвердилась закрытой — ждём recover")
            return
        pnl = await self._closed_pnl(pos)
        await self._finalize_close(pnl, reason)

    async def _safe_close(self, qty: float, side: str) -> None:
        """Аварийное закрытие (SL/TP не встали): Market-ордер в закрытие."""
        try:
            await self.client.close_position(self.cfg.symbol, qty, side)
        except Exception as exc:  # noqa: BLE001
            logger.error("аварийное закрытие не вышло: %s", exc)
            self._notify(f"trend: КРИТИЧНО — аварийное закрытие не вышло: {exc}")
            return
        if not await self._wait_no_position():
            logger.error("аварийное закрытие не подтверждено — ждём recover")
            return
        if self.store.state.position is not None:
            await self._finalize_close(0.0, "sl_setup_failed")

    async def _position_gone(self) -> bool:
        """Позицию закрыла биржа (SL/TP) — зафиксировать итог."""
        try:
            ex = await self.client.get_position(self.cfg.symbol)
        except Exception as exc:  # noqa: BLE001
            logger.warning("проверка позиции не удалась: %s", exc)
            return False
        if ex is not None:
            return False
        st = self.store.state
        pos = st.position
        logger.info("позиция закрыта биржей (SL/TP)")
        pnl = 0.0
        if pos is not None:
            pnl = await self._closed_pnl(pos)
        await self._finalize_close(pnl, "server")
        return True

    async def _update_risk(self) -> None:
        """BE/trail: подтянуть серверный стоп по свежей цене (только в плюс)."""
        st = self.store.state
        pos = st.position
        if pos is None:
            return
        price = self.feed.last_price
        if price <= 0:
            try:
                price = await self.client.get_price(self.cfg.symbol)
            except Exception as exc:  # noqa: BLE001
                logger.warning("risk: цена недоступна (%s)", exc)
                return
        p = self.strategy.params
        if pos.peak_price <= 0:
            pos.peak_price = pos.entry_price
        peak = (
            max(pos.peak_price, price)
            if pos.side == "long"
            else min(pos.peak_price, price)
        )
        stop = pos.stop_loss
        if stop is None:
            stop = (
                pos.entry_price * (1.0 - p.sl_pct)
                if pos.side == "long"
                else pos.entry_price * (1.0 + p.sl_pct)
            )
        decision = on_price(
            entry=pos.entry_price,
            peak=peak,
            price=price,
            current_stop=stop,
            stop_pct=p.sl_pct,
            be_trigger_pct=p.be_trigger_pct,
            be_offset_pct=p.be_offset_pct,
            trail_pct=p.trail_pct,
            be_active=pos.be_active,
            side=pos.side,
        )
        if not decision.update_server:
            return
        pos.peak_price = peak
        pos.stop_loss = decision.stop_loss
        pos.be_active = decision.be_active
        await self.store.save()
        try:
            await self.client.set_stop_loss_take_profit(
                self.cfg.symbol,
                pos.stop_loss,
                pos.take_profit,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("risk: серверный SL не обновился (%s)", exc)
            return
        logger.info(
            "risk: %s stop=%.6g be=%s peak=%.6g",
            pos.side,
            pos.stop_loss,
            pos.be_active,
            pos.peak_price,
        )

    async def _closed_pnl(self, pos: OpenPosition) -> float:
        """PnL закрытия: с биржи, иначе расчёт по последней цене."""
        try:
            pnl = await self.client.get_last_closed_pnl(self.cfg.symbol)
        except Exception as exc:  # noqa: BLE001
            logger.warning("get_last_closed_pnl: %s", exc)
            pnl = None
        if pnl is not None:
            return pnl
        price = self.feed.last_price or pos.entry_price
        return position_pnl(pos.entry_price, price, pos.qty, pos.side)

    async def _finalize_close(self, pnl: float, reason: str) -> None:
        """Сбросить позицию в state и обновить счётчики (день/сессия)."""
        st = self.store.state
        st.position = None
        st.phase = PHASE_IDLE
        st.last_close_at = time.time()
        st.day_pnl += pnl
        st.day_trades += 1
        st.session_pnl += pnl
        st.peak_session_pnl = max(st.peak_session_pnl, st.session_pnl)
        await self.store.save()
        logger.info(
            "позиция закрыта (%s): pnl=%+.2f день=%+.2f/%d",
            reason,
            pnl,
            st.day_pnl,
            st.day_trades,
        )
        self._notify(f"trend: закрыта ({reason}), PnL {pnl:+.2f} USDT")

    # ==================== kill-switch / день ====================

    def _kill_file_hit(self) -> bool:
        path = self.cfg.kill_switch_file
        if not path:
            return False
        return Path(path).exists()

    async def _kill_hit(self) -> bool:
        """Файл-флаг или просадка сессии → kill (стойкий, через state)."""
        st = self.store.state
        if st.kill:
            return True
        reason = ""
        if self._kill_file_hit():
            reason = f"файл {self.cfg.kill_switch_file}"
        elif should_kill(
            st.session_pnl,
            st.peak_session_pnl,
            self.cfg.deposit_usd,
            self.cfg.max_loss_usd,
            self.cfg.max_drawdown_pct,
        ):
            reason = (
                f"просадка сессии pnl={st.session_pnl:.2f} "
                f"peak={st.peak_session_pnl:.2f}"
            )
        if not reason:
            return False
        logger.error("kill-switch сработал: %s", reason)
        st.kill = True
        st.phase = PHASE_STOPPED
        await self.store.save()
        self._notify(f"trend: kill-switch — {reason}")
        return True

    async def _roll_day(self) -> None:
        """Смена суток UTC — обнулить дневные счётчики."""
        st = self.store.state
        today = _utc_day()
        if st.day == today:
            return
        st.day = today
        st.day_pnl = 0.0
        st.day_trades = 0
        await self.store.save()

    # ==================== recover ====================

    async def recover(self) -> bool:
        """Сверить state с биржей (старт / WS-ресинк).

        Returns:
            True — сверка выполнена; False — биржа недоступна.
        """
        st = self.store.state
        try:
            ex = await self.client.get_position(self.cfg.symbol)
        except Exception as exc:  # noqa: BLE001
            logger.error("recover: позиция недоступна — %s", exc)
            return False

        if st.position is not None:
            if ex is None:
                pnl = 0.0
                try:
                    pnl = await self.client.get_last_closed_pnl(self.cfg.symbol) or 0.0
                except Exception as exc:  # noqa: BLE001
                    logger.warning("recover: get_last_closed_pnl: %s", exc)
                logger.info("recover: позиция закрыта биржей (SL/TP)")
                await self._finalize_close(pnl, "server")
                return True
            # сверить объём/цену (страховка) и вернуть серверный SL/TP
            st.position.qty = ex.size
            if ex.avg_price > 0:
                st.position.entry_price = ex.avg_price
            st.phase = PHASE_IN_POSITION
            await self.store.save()
            logger.info(
                "recover: позиция подтверждена %s qty=%s entry=%.4f",
                st.position.side,
                st.position.qty,
                st.position.entry_price,
            )
            if st.position.stop_loss is not None:
                try:
                    await self.client.set_stop_loss_take_profit(
                        self.cfg.symbol,
                        st.position.stop_loss,
                        st.position.take_profit,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.error("recover: SL/TP не подтверждены: %s", exc)
                    self._notify(f"trend: recover — SL/TP не подтверждены: {exc}")
            return True

        if ex is not None:
            # позиция есть на бирже, в state нет — принимаем с её SL/TP
            side = "long" if ex.side == "Buy" else "short"
            st.position = OpenPosition(
                entry_price=ex.avg_price,
                qty=ex.size,
                side=side,
                stop_loss=ex.stop_loss,
                take_profit=ex.take_profit,
                opened_at=time.time(),
            )
            st.phase = PHASE_IN_POSITION
            await self.store.save()
            logger.warning(
                "recover: принята позиция без state %s qty=%s", side, ex.size
            )
            self._notify(f"trend: recover — принята позиция без state ({side})")
            return True

        # позиции нет — нашим не должно быть и лимиток-сирот
        try:
            await self.client.cancel_all_orders(self.cfg.symbol)
        except Exception as exc:  # noqa: BLE001
            logger.warning("recover: cancel_all_orders: %s", exc)
        return True

    # ==================== служебное ====================

    async def _fetch_candles(self) -> list[Candle]:
        return await self.client.get_klines(
            self.cfg.symbol,
            interval=self.cfg.timeframe,
            limit=self.cfg.candles_warmup + 1,  # +1 — формирующаяся свеча
        )

    async def _wait_position(self) -> Position | None:
        for _ in range(FILL_RETRIES):
            pos = await self.client.get_position(self.cfg.symbol)
            if pos is not None and pos.size > 0:
                return pos
            await asyncio.sleep(FILL_DELAY)
        return None

    async def _wait_no_position(self) -> bool:
        for _ in range(FILL_RETRIES):
            pos = await self.client.get_position(self.cfg.symbol)
            if pos is None or pos.size <= 0:
                return True
            await asyncio.sleep(FILL_DELAY)
        return False

    def _order_link_id(self) -> str:
        """Идемпотентный id: префикс + 16 hex (<= 36 символов Bybit)."""
        return f"{self.cfg.order_link_prefix}{secrets.token_hex(8)}"

    def _drain_queues(self) -> None:
        """Сбросить WS-очереди: проверки идём через REST (проще и надёжнее)."""
        queues: tuple[Any, ...] = (
            self.feed.prices,
            self.feed.order_events,
            self.feed.exec_events,
            self.feed.wallet_events,
            self.feed.position_events,
        )
        for queue in queues:
            while not queue.empty():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    break

    def _notify(self, text: str) -> None:
        """Отправить сообщение фоном, не блокируя цикл."""
        if self.notifier is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.debug("notify вне event loop: %s", text)
            return
        loop.create_task(self._safe_notify(text))

    async def _safe_notify(self, text: str) -> None:
        if self.notifier is None:
            return
        try:
            await self.notifier.notify(text)
        except Exception as exc:  # noqa: BLE001
            logger.debug("notify: %s", exc)

    async def _on_shutdown(self) -> None:
        st = self.store.state
        if st.position is None and st.phase == PHASE_IN_POSITION:
            st.phase = PHASE_IDLE
        await self.store.save()
        logger.info("order_flow остановлен (phase=%s)", st.phase)
