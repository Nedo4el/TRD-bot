"""Стратегия robot_yrovni_D: три лимитки вокруг POC + стоп/BE/тейк/трейлинг.

Модель: скринер присылает сигнал входа в зону POC-кластера. Робот ставит
три PostOnly-лимитки (POC, POC*1.02, POC*0.98) в сторону ``side_hint``,
закрывает их reduce-only стопом -10%, переводит стоп в безубыток на +1%,
на +7% выставляет тейк и включает трейлинг 3%.

Стратегия держит состояние позиции и исполняет действия через
:class:`robot_yrovni_D.broker.Broker`; всё, что происходит, уходит в
JSONL-журнал (:class:`robot_yrovni_D.json_log.JsonlLogger`).
"""

from __future__ import annotations

import math
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from core.logger import get_logger
from robot_yrovni_D.broker import Broker
from robot_yrovni_D.config import LevelConfig
from robot_yrovni_D.json_log import JsonlLogger
from robot_yrovni_D.signals import PocSignal

logger = get_logger(__name__)

EPS = 1e-12
LINK_ID_MAX = 36
_LINK_ID_BAD = re.compile(r"[^A-Za-z0-9._-]")


def _poc_distance(price: float, poc: float) -> float:
    """Расстояние до POC по формуле скринера: ``|цена - POC| / POC``."""
    if poc <= 0:
        return 0.0
    return abs(price - poc) / poc


class Action(str, Enum):
    """Действия тика, которые стратегия выполнила (для отчёта/логов)."""

    HOLD = "HOLD"
    MOVE_BE = "MOVE_BE"
    CANCEL_PENDING_LIMITS = "CANCEL_PENDING_LIMITS"
    ACTIVATE_TAKE = "ACTIVATE_TAKE"
    CLOSE_STOP = "CLOSE_STOP"
    CLOSE_TRAIL = "CLOSE_TRAIL"
    CLOSE_MAX_HOLD = "CLOSE_MAX_HOLD"
    CANCEL_ALL = "CANCEL_ALL"


@dataclass(slots=True)
class LimitOrder:
    """Лимитка входа на одном из трёх уровней."""

    level: str
    order_id: str
    link_id: str
    price: float
    qty: float
    side: str
    filled_qty: float = 0.0
    cancelled: bool = False

    @property
    def open(self) -> bool:
        """Заявка ещё может исполниться."""
        return not self.cancelled and self.filled_qty < self.qty - EPS

    def to_dict(self) -> dict[str, Any]:
        """Представление для журнала."""
        return {
            "level": self.level,
            "order_id": self.order_id,
            "link_id": self.link_id,
            "price": self.price,
            "qty": self.qty,
            "side": self.side,
        }


@dataclass(slots=True)
class StopOrder:
    """Reduce-only stop-market (страховка или безубыток)."""

    order_id: str
    trigger: float
    qty: float
    side: str
    at_be: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Представление для журнала."""
        return {
            "order_id": self.order_id,
            "trigger": self.trigger,
            "qty": self.qty,
            "side": self.side,
            "at_be": self.at_be,
        }


@dataclass(slots=True)
class TakeOrder:
    """Reduce-only тейк-лимит."""

    order_id: str
    price: float
    qty: float
    side: str

    def to_dict(self) -> dict[str, Any]:
        """Представление для журнала."""
        return {
            "order_id": self.order_id,
            "price": self.price,
            "qty": self.qty,
            "side": self.side,
        }


@dataclass(slots=True)
class PositionState:
    """Состояние сделки по одному сигналу."""

    signal: PocSignal
    side: str  # long / short
    limits: list[LimitOrder]
    qty: float = 0.0
    avg_entry: float = 0.0
    stop: StopOrder | None = None
    take: TakeOrder | None = None
    be_done: bool = False
    take_active: bool = False
    max_price: float = 0.0
    min_price: float = math.inf
    trail_stop: float | None = None
    opened_at: float = 0.0
    limits_deadline: float = 0.0
    last_snapshot_ts: float = 0.0
    counted_open: bool = False
    closed: bool = False

    @property
    def symbol(self) -> str:
        """Символ сделки."""
        return self.signal.symbol

    @property
    def signal_id(self) -> str:
        """Идентификатор сигнала (ключ идемпотентности)."""
        return self.signal.signal_id

    @property
    def entry_side(self) -> str:
        """Сторона входа: Buy для long, Sell для short."""
        return "Buy" if self.side == "long" else "Sell"

    @property
    def close_side(self) -> str:
        """Сторона закрытия позиции."""
        return "Sell" if self.side == "long" else "Buy"

    def open_limits(self) -> list[LimitOrder]:
        """Неисполненные и неотменённые лимитки."""
        return [limit for limit in self.limits if limit.open]

    def unrealized_pct(self, price: float) -> float:
        """Нереализованный результат в долях от средней цены входа."""
        if self.avg_entry <= 0:
            return 0.0
        if self.side == "long":
            return (price - self.avg_entry) / self.avg_entry
        return (self.avg_entry - price) / self.avg_entry


@dataclass(slots=True)
class DailyStats:
    """Счётчики для DAILY_SUMMARY."""

    day: str = ""
    signals_received: int = 0
    signals_skipped: int = 0
    positions_opened: int = 0
    positions_closed: int = 0
    wins: int = 0
    losses: int = 0
    total_pnl_usd: float = 0.0
    total_pnl_pct: float = 0.0
    hold_sec_total: float = 0.0
    peak_pnl_usd: float = 0.0
    max_drawdown_pct: float = 0.0

    def summary(self) -> dict[str, Any]:
        """Снимок счётчиков для журнала."""
        avg_hold = (
            self.hold_sec_total / self.positions_closed
            if self.positions_closed
            else 0.0
        )
        avg_pct = (
            self.total_pnl_pct / self.positions_closed if self.positions_closed else 0.0
        )
        return {
            "signals_received": self.signals_received,
            "signals_skipped": self.signals_skipped,
            "positions_opened": self.positions_opened,
            "positions_closed": self.positions_closed,
            "wins": self.wins,
            "losses": self.losses,
            "total_pnl_usd": round(self.total_pnl_usd, 6),
            "total_pnl_pct": round(avg_pct, 4),
            "avg_hold_sec": round(avg_hold, 1),
            "max_drawdown_pct": round(self.max_drawdown_pct, 4),
        }


class LevelStrategy:
    """Торговля POC-уровнями: лимитки, стоп, BE, тейк и трейлинг."""

    def __init__(
        self,
        config: LevelConfig,
        broker: Broker,
        log: JsonlLogger,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._config = config
        self._broker = broker
        self._log = log
        self._clock = clock
        self._positions: dict[str, PositionState] = {}
        self._last_exit_ts: dict[str, float] = {}
        self._blocked: dict[str, str] = {}
        self._stats = DailyStats(day=self._day(self._clock()))

    # ------------------------------------------------------------- доступ ---

    def positions(self) -> list[PositionState]:
        """Все незакрытые сделки (включая те, где ещё висят только лимитки)."""
        return [item for item in self._positions.values() if not item.closed]

    def position(self, symbol: str) -> PositionState | None:
        """Сделка по символу, если она ещё не закрыта."""
        current = self._positions.get(symbol.upper())
        if current is None or current.closed:
            return None
        return current

    def block_symbol(self, symbol: str, reason: str) -> None:
        """Запретить новые входы по символу до перезапуска процесса."""
        self._blocked[symbol.upper()] = reason

    # -------------------------------------------------------------- вход ---

    async def on_poc_signal(self, signal: PocSignal) -> list[LimitOrder]:
        """Обработать сигнал POC: проверить фильтры и выставить три лимитки."""
        cfg = self._config.for_symbol(signal.symbol)
        now = self._clock()
        self._reset_daily(now)
        self._stats.signals_received += 1
        self._log.log(
            "SIGNAL_RECEIVED",
            symbol=signal.symbol,
            signal_id=signal.signal_id,
            poc_price=signal.poc_price,
            confidence=signal.confidence,
            side_hint=signal.side_hint,
            current_price=signal.current_price,
        )

        reason, extra = await self._skip_reason(signal, cfg, now)
        if reason is not None:
            self._stats.signals_skipped += 1
            self._log.log(
                "SIGNAL_SKIPPED",
                symbol=signal.symbol,
                signal_id=signal.signal_id,
                reason=reason,
                extra=extra,
            )
            return []

        return await self.place_three_limits(signal)

    async def place_three_limits(self, signal: PocSignal) -> list[LimitOrder]:
        """Три PostOnly-лимитки вокруг POC (up/mid/down) + начальный стоп."""
        cfg = self._config.for_symbol(signal.symbol)
        now = self._clock()
        side = signal.side
        poc = signal.poc_price
        total_notional = cfg.deposit_usd * (cfg.position_pct / 100.0)
        total_qty = total_notional / poc
        min_notional = await self._broker.min_notional(signal.symbol)

        levels = (
            ("up", poc * (1.0 + cfg.level_offset_pct)),
            ("mid", poc),
            ("down", poc * (1.0 - cfg.level_offset_pct)),
        )
        orders: list[LimitOrder] = []
        for (level, price), share in zip(levels, cfg.order_split):
            qty = total_qty * share
            notional = price * qty
            if notional < min_notional:
                logger.warning(
                    "%s: лимитка %s на %.6f USDT меньше MIN_NOTIONAL %.2f — пропуск",
                    signal.symbol,
                    level,
                    notional,
                    min_notional,
                )
                continue
            result = await self._broker.place_limit(
                signal.symbol,
                side,
                price,
                qty,
                self._link_id(cfg, signal.signal_id, level),
            )
            orders.append(
                LimitOrder(
                    level=level,
                    order_id=result.order_id,
                    link_id=result.link_id,
                    price=price,
                    qty=qty,
                    side=side,
                ),
            )

        if not orders:
            self._stats.signals_skipped += 1
            self._log.log(
                "SIGNAL_SKIPPED",
                symbol=signal.symbol,
                signal_id=signal.signal_id,
                reason="min_notional",
                extra={"min_notional": min_notional, "total_qty": total_qty},
            )
            return []

        stop = await self.place_initial_stop(
            poc,
            side,
            sum(order.qty for order in orders),
            symbol=signal.symbol,
            signal_id=signal.signal_id,
        )
        position = PositionState(
            signal=signal,
            side=signal.side_hint,
            limits=orders,
            stop=stop,
            opened_at=now,
            limits_deadline=now + cfg.entry_ttl_sec,
            max_price=0.0,
            min_price=math.inf,
            last_snapshot_ts=now,
        )
        self._positions[signal.symbol] = position
        self._log.log(
            "LIMITS_PLACED",
            symbol=signal.symbol,
            signal_id=signal.signal_id,
            side=side,
            orders=[order.to_dict() for order in orders],
            stop=stop.to_dict() if stop else None,
        )
        return orders

    async def place_initial_stop(
        self,
        entry_price: float,
        side: str,
        qty: float,
        *,
        symbol: str,
        signal_id: str,
    ) -> StopOrder | None:
        """Страховочный reduce-only stop-market от цены входа."""
        cfg = self._config.for_symbol(symbol)
        if side == "Buy":
            trigger = entry_price * (1.0 - cfg.stop_initial_pct)
            close_side = "Sell"
        else:
            trigger = entry_price * (1.0 + cfg.stop_initial_pct)
            close_side = "Buy"
        if qty <= EPS:
            return None
        result = await self._broker.place_stop(
            symbol,
            close_side,
            trigger,
            qty,
            self._link_id(cfg, signal_id, "stop"),
        )
        return StopOrder(
            order_id=result.order_id,
            trigger=trigger,
            qty=qty,
            side=close_side,
        )

    # ---------------------------------------------------------- параллель ---

    async def _skip_reason(
        self,
        signal: PocSignal,
        cfg: LevelConfig,
        now: float,
    ) -> tuple[str | None, dict[str, Any]]:
        """Проверить фильтры входа; вернуть причину отказа или (None, {})."""
        blocked = self._blocked.get(signal.symbol.upper())
        if blocked is not None:
            return blocked, {}
        if signal.confidence < cfg.min_confidence:
            return "low_confidence", {
                "confidence": signal.confidence,
                "min_confidence": cfg.min_confidence,
            }

        active = self.positions()
        if len(active) >= cfg.max_concurrent or self.position(signal.symbol):
            return "max_concurrent", {
                "active": len(active),
                "max_concurrent": cfg.max_concurrent,
            }

        if cfg.kill_switch_file and Path(cfg.kill_switch_file).exists():
            return "kill_switch", {"file": cfg.kill_switch_file}

        last_exit = self._last_exit_ts.get(signal.symbol)
        if last_exit is not None and now - last_exit < cfg.cooldown_sec:
            return "cooldown", {
                "since_exit_sec": round(now - last_exit, 1),
                "cooldown_sec": cfg.cooldown_sec,
            }

        total_notional = cfg.deposit_usd * (cfg.position_pct / 100.0)
        min_notional = await self._broker.min_notional(signal.symbol)
        if total_notional / len(cfg.order_split) < min_notional:
            return "min_notional", {
                "order_notional": round(total_notional / len(cfg.order_split), 6),
                "min_notional": min_notional,
            }

        atr_pct = await self._broker.atr_pct(signal.symbol)
        if cfg.max_atr_pct > 0 and atr_pct is not None and atr_pct > cfg.max_atr_pct:
            return "atr_too_high", {
                "atr_pct": round(atr_pct, 6),
                "max_atr_pct": cfg.max_atr_pct,
            }

        price = signal.current_price
        if price <= 0:
            price = await self._broker.price(signal.symbol)
        distance = _poc_distance(price, signal.poc_price)
        if distance > cfg.max_distance_from_poc_pct:
            return "price_far_from_poc", {
                "distance_pct": round(distance, 6),
                "max_distance_pct": cfg.max_distance_from_poc_pct,
            }

        funding_ts = await self._broker.next_funding_ts(signal.symbol)
        if funding_ts is not None and now >= funding_ts - cfg.funding_window_sec:
            return "funding_window", {
                "next_funding_ts": funding_ts,
                "window_sec": cfg.funding_window_sec,
            }

        return None, {}

    # ------------------------------------------------------------ исполнение ---

    async def sync_fills(
        self,
        position: PositionState,
        current_price: float,
    ) -> list[LimitOrder]:
        """Сверить исполнения лимиток с биржей, обновить вход и стоп."""
        info = await self._broker.position(position.symbol)
        new_qty = info.qty if info is not None else 0.0
        avg_price = info.avg_price if info is not None else 0.0

        if new_qty < position.qty - EPS:
            if new_qty <= EPS:
                await self._external_close(position, current_price)
                return []
            position.qty = new_qty
            await self._resize_stop(position)
            return []
        if new_qty <= position.qty + EPS:
            return []

        delta = new_qty - position.qty
        filled = self._attribute_fills(position, avg_price, delta)
        position.qty = new_qty
        if avg_price > 0:
            position.avg_entry = avg_price
        else:
            position.avg_entry = self._weighted_entry(position)
        if not position.counted_open and position.qty > EPS:
            position.counted_open = True
            self._stats.positions_opened += 1
        await self._resize_stop(position)
        return filled

    async def _external_close(
        self,
        position: PositionState,
        current_price: float,
    ) -> None:
        """Позицию закрыла биржа (стоп/тейк исполнился) — зафиксировать факт."""
        reason = "stop"
        if position.take is not None:
            if (
                position.side == "long"
                and current_price >= position.take.price
                or position.side == "short"
                and current_price <= position.take.price
            ):
                reason = "take"
        elif position.be_done:
            reason = "be"
        await self.close_position(position, reason, current_price, send_market=False)

    def _attribute_fills(
        self,
        position: PositionState,
        avg_price: float,
        delta: float,
    ) -> list[LimitOrder]:
        """Разложить прирост позиции по лимиткам от ближайшей к средней цене."""
        candidates = sorted(
            position.open_limits(),
            key=lambda item: abs(item.price - avg_price),
        )

        filled: list[LimitOrder] = []
        remaining = delta
        for limit in candidates:
            if remaining <= EPS:
                break
            space = limit.qty - limit.filled_qty
            take = min(space, remaining)
            if take <= EPS:
                continue
            limit.filled_qty += take
            remaining -= take
            filled.append(limit)
            self._log.log(
                "LIMIT_FILLED",
                symbol=position.symbol,
                signal_id=position.signal_id,
                order_id=limit.order_id,
                price=limit.price,
                qty=take,
                level=limit.level,
                partial=limit.filled_qty < limit.qty - EPS,
            )
        if remaining > EPS:
            logger.warning(
                "%s: прирост позиции %.10g не разложился по лимиткам (%.10g)",
                position.symbol,
                delta,
                remaining,
            )
        return filled

    @staticmethod
    def _weighted_entry(position: PositionState) -> float:
        """Средняя цена входа по исполненным лимиткам."""
        total_qty = sum(limit.filled_qty for limit in position.limits)
        if total_qty <= EPS:
            return position.avg_entry
        total_value = sum(limit.filled_qty * limit.price for limit in position.limits)
        return total_value / total_qty

    async def _resize_stop(self, position: PositionState) -> None:
        """Подогнать стоп под фактический объём позиции и средний вход."""
        if position.qty <= EPS or position.stop is None:
            return
        cfg = self._config.for_symbol(position.symbol)
        offset = cfg.be_offset_pct if position.be_done else cfg.stop_initial_pct
        if position.side == "long":
            trigger = position.avg_entry * (1.0 - offset)
        else:
            trigger = position.avg_entry * (1.0 + offset)
        old = position.stop
        await self._broker.cancel(position.symbol, old.order_id)
        result = await self._broker.place_stop(
            position.symbol,
            position.close_side,
            trigger,
            position.qty,
            self._link_id(
                cfg,
                position.signal_id,
                "be" if position.be_done else "stop",
            ),
        )
        position.stop = StopOrder(
            order_id=result.order_id,
            trigger=trigger,
            qty=position.qty,
            side=position.close_side,
            at_be=position.be_done,
        )
        self._log.log(
            "STOP_UPDATED",
            symbol=position.symbol,
            signal_id=position.signal_id,
            old_trigger=old.trigger,
            new_trigger=trigger,
            qty=position.qty,
            reason="fill",
        )

    # ---------------------------------------------------------------- тик ---

    async def update_on_tick(
        self,
        current_price: float,
        position: PositionState,
    ) -> list[Action]:
        """Один тик по позиции: стоп -> BE -> тейк -> трейлинг -> TTL -> max hold."""
        cfg = self._config.for_symbol(position.symbol)
        now = self._clock()
        actions: list[Action] = []

        if position.closed:
            return [Action.HOLD]

        if position.qty > EPS:
            position.max_price = max(position.max_price, current_price)
            position.min_price = min(position.min_price, current_price)
            unrealized = position.unrealized_pct(current_price)

            if self._stop_hit(position, current_price):
                await self.close_position(position, "stop", current_price)
                self._snapshot(position, current_price, now, cfg)
                return [Action.CLOSE_STOP, Action.CANCEL_ALL]

            if unrealized >= cfg.be_trigger_pct and not position.be_done:
                await self.move_stop_to_breakeven(position, current_price)
                actions.append(Action.MOVE_BE)
                if cfg.cancel_limits_on_be and position.open_limits():
                    await self.cancel_pending_limits(position, "be")
                    actions.append(Action.CANCEL_PENDING_LIMITS)

            if not position.take_active and unrealized >= min(
                cfg.take_pct,
                cfg.trail_activate_pct,
            ):
                await self.activate_take_and_trail(position)
                actions.append(Action.ACTIVATE_TAKE)

            if position.take_active and self._trail_hit(position, current_price):
                await self.close_position(position, "trail", current_price)
                self._snapshot(position, current_price, now, cfg)
                return [*actions, Action.CLOSE_TRAIL, Action.CANCEL_ALL]

            if now - position.opened_at >= cfg.max_hold_sec:
                await self.close_position(position, "max_hold", current_price)
                self._snapshot(position, current_price, now, cfg)
                return [*actions, Action.CLOSE_MAX_HOLD, Action.CANCEL_ALL]
        else:
            distance = _poc_distance(current_price, position.signal.poc_price)
            if distance > cfg.cancel_if_distance_pct:
                await self.cancel_all(position, "distance")
                position.closed = True
                self._snapshot(position, current_price, now, cfg)
                return [Action.CANCEL_ALL]
            if now >= position.limits_deadline:
                await self.cancel_all(position, "ttl")
                position.closed = True
                self._snapshot(position, current_price, now, cfg)
                return [Action.CANCEL_ALL]

        self._snapshot(position, current_price, now, cfg)
        return actions or [Action.HOLD]

    def _stop_hit(self, position: PositionState, price: float) -> bool:
        """Цена дошла до стопа (страховка на случай, если ордер не встал)."""
        if position.stop is None:
            return False
        if position.side == "long":
            return price <= position.stop.trigger
        return price >= position.stop.trigger

    def _trail_stop(self, position: PositionState, cfg: LevelConfig) -> float | None:
        """Новый уровень трейлинга от экстремума."""
        if position.side == "long":
            if position.max_price <= 0:
                return None
            return position.max_price * (1.0 - cfg.trail_pct)
        if position.min_price == math.inf:
            return None
        return position.min_price * (1.0 + cfg.trail_pct)

    def _trail_hit(self, position: PositionState, price: float) -> bool:
        """Цена откатилась от экстремума на TRAIL_PCT."""
        if position.trail_stop is None:
            return False
        if position.side == "long":
            return price <= position.trail_stop
        return price >= position.trail_stop

    # ------------------------------------------------------------ действия ---

    async def move_stop_to_breakeven(
        self,
        position: PositionState,
        current_price: float | None = None,
    ) -> StopOrder | None:
        """Переставить стоп в безубыток (cancel + replace reduce-only stop)."""
        cfg = self._config.for_symbol(position.symbol)
        if position.stop is None or position.qty <= EPS:
            return None
        old = position.stop
        if position.side == "long":
            new_trigger = position.avg_entry * (1.0 + cfg.be_offset_pct)
        else:
            new_trigger = position.avg_entry * (1.0 - cfg.be_offset_pct)
        await self._broker.cancel(position.symbol, old.order_id)
        result = await self._broker.place_stop(
            position.symbol,
            position.close_side,
            new_trigger,
            position.qty,
            self._link_id(cfg, position.signal_id, "be"),
        )
        position.stop = StopOrder(
            order_id=result.order_id,
            trigger=new_trigger,
            qty=position.qty,
            side=position.close_side,
            at_be=True,
        )
        position.be_done = True
        unrealized = (
            position.unrealized_pct(current_price) if current_price is not None else 0.0
        )
        self._log.log(
            "STOP_MOVED_BE",
            symbol=position.symbol,
            signal_id=position.signal_id,
            old_stop=old.trigger,
            new_stop=new_trigger,
            unrealized_pct=round(unrealized * 100.0, 6),
            entry_price=position.avg_entry,
        )
        return position.stop

    async def cancel_pending_limits(
        self,
        position: PositionState,
        reason: str,
    ) -> list[str]:
        """Снять все неисполненные лимитки сделки."""
        cancelled: list[dict[str, Any]] = []
        for limit in position.open_limits():
            ok = await self._broker.cancel(position.symbol, limit.order_id)
            limit.cancelled = True
            cancelled.append(
                {
                    "order_id": limit.order_id,
                    "level": limit.level,
                    "price": limit.price,
                    "cancelled": ok,
                },
            )
        if cancelled:
            self._log.log(
                "LIMITS_CANCELLED",
                symbol=position.symbol,
                signal_id=position.signal_id,
                reason=reason,
                cancelled_orders=cancelled,
            )
        return [item["order_id"] for item in cancelled]

    async def activate_take_and_trail(
        self, position: PositionState
    ) -> TakeOrder | None:
        """Выставить тейк-лимит и включить трейлинг."""
        cfg = self._config.for_symbol(position.symbol)
        if position.take_active and position.take is not None:
            return position.take
        if position.qty <= EPS:
            return None
        if position.side == "long":
            take_price = position.avg_entry * (1.0 + cfg.take_pct)
        else:
            take_price = position.avg_entry * (1.0 - cfg.take_pct)
        result = await self._broker.place_take(
            position.symbol,
            position.close_side,
            take_price,
            position.qty,
            self._link_id(cfg, position.signal_id, "take"),
        )
        position.take = TakeOrder(
            order_id=result.order_id,
            price=take_price,
            qty=position.qty,
            side=position.close_side,
        )
        position.take_active = True
        position.trail_stop = self._trail_stop(position, cfg)
        self._log.log(
            "TAKE_ACTIVATED",
            symbol=position.symbol,
            signal_id=position.signal_id,
            take_price=take_price,
            trail_pct=cfg.trail_pct,
            max_price_seen=position.max_price,
        )
        return position.take

    async def close_position(
        self,
        position: PositionState,
        reason: str,
        exit_price: float,
        *,
        send_market: bool = True,
    ) -> None:
        """Закрыть позицию по рынку и снять все заявки сделки."""
        if position.closed:
            return
        if send_market and position.qty > EPS:
            await self._broker.market_close(
                position.symbol,
                position.qty,
                position.entry_side,
            )
        await self.cancel_all(position, reason)

        now = self._clock()
        pnl_pct = position.unrealized_pct(exit_price) * 100.0
        direction = 1.0 if position.side == "long" else -1.0
        pnl_usd = (exit_price - position.avg_entry) * position.qty * direction
        hold_sec = max(now - position.opened_at, 0.0)

        position.closed = True
        self._last_exit_ts[position.symbol] = now
        self._stats.positions_closed += 1
        self._stats.total_pnl_usd += pnl_usd
        self._stats.total_pnl_pct += pnl_pct
        self._stats.hold_sec_total += hold_sec
        if pnl_usd >= 0:
            self._stats.wins += 1
        else:
            self._stats.losses += 1
        self._stats.peak_pnl_usd = max(
            self._stats.peak_pnl_usd, self._stats.total_pnl_usd
        )
        if self._stats.peak_pnl_usd > 0:
            drawdown = (
                self._stats.peak_pnl_usd - self._stats.total_pnl_usd
            ) / self._stats.peak_pnl_usd
            self._stats.max_drawdown_pct = max(
                self._stats.max_drawdown_pct,
                drawdown * 100.0,
            )

        self._log.log(
            "POSITION_CLOSED",
            symbol=position.symbol,
            signal_id=position.signal_id,
            reason=reason,
            entry_price=position.avg_entry,
            exit_price=exit_price,
            pnl_pct=round(pnl_pct, 6),
            pnl_usd=round(pnl_usd, 6),
            hold_sec=round(hold_sec, 1),
        )

    async def cancel_all(self, position: PositionState, reason: str) -> list[str]:
        """Снять лимитки, стоп и тейк сделки (например, по distance/TTL)."""
        cancelled: list[dict[str, Any]] = []
        for limit in position.open_limits():
            ok = await self._broker.cancel(position.symbol, limit.order_id)
            limit.cancelled = True
            cancelled.append(
                {
                    "order_id": limit.order_id,
                    "kind": "limit",
                    "level": limit.level,
                    "cancelled": ok,
                },
            )
        if position.stop is not None:
            ok = await self._broker.cancel(position.symbol, position.stop.order_id)
            cancelled.append(
                {
                    "order_id": position.stop.order_id,
                    "kind": "stop",
                    "cancelled": ok,
                },
            )
            position.stop = None
        if position.take is not None:
            ok = await self._broker.cancel(position.symbol, position.take.order_id)
            cancelled.append(
                {
                    "order_id": position.take.order_id,
                    "kind": "take",
                    "cancelled": ok,
                },
            )
            position.take = None
        if cancelled:
            self._log.log(
                "LIMITS_CANCELLED",
                symbol=position.symbol,
                signal_id=position.signal_id,
                reason=reason,
                cancelled_orders=cancelled,
            )
        return [item["order_id"] for item in cancelled]

    # ---------------------------------------------------------- служебное ---

    def _snapshot(
        self,
        position: PositionState,
        price: float,
        now: float,
        cfg: LevelConfig,
    ) -> None:
        """TICK_SNAPSHOT не чаще TICK_SNAPSHOT_SEC."""
        if now - position.last_snapshot_ts < cfg.tick_snapshot_sec:
            return
        position.last_snapshot_ts = now
        self._log.log(
            "TICK_SNAPSHOT",
            symbol=position.symbol,
            signal_id=position.signal_id,
            current_price=price,
            unrealized_pct=round(position.unrealized_pct(price) * 100.0, 6),
            stop=position.stop.trigger if position.stop else None,
            take=position.take.price if position.take else None,
            trail_stop=position.trail_stop,
            max_price_seen=position.max_price,
            time_in_position_sec=round(max(now - position.opened_at, 0.0), 1),
        )

    def maybe_daily_summary(self) -> dict[str, Any] | None:
        """Раз в сутки (UTC) записать DAILY_SUMMARY и обнулить счётчики."""
        now = self._clock()
        day = self._day(now)
        if day == self._stats.day:
            return None
        summary = self._stats.summary()
        self._stats = DailyStats(day=day)
        self._log.log("DAILY_SUMMARY", **summary)
        return summary

    def _reset_daily(self, now: float) -> None:
        """Сбросить счётчики при смене суток без записи в журнал."""
        day = self._day(now)
        if day != self._stats.day:
            self._stats = DailyStats(day=day)

    @staticmethod
    def _day(now: float) -> str:
        return time.strftime("%Y-%m-%d", time.gmtime(now))

    @staticmethod
    def _link_id(cfg: LevelConfig, signal_id: str, suffix: str) -> str:
        """orderLinkId = ``zk-{signal_id}-{suffix}-{token}``, символы Bybit.

        Bybit ограничивает orderLinkId 36 символами и не даёт переиспользовать
        link id отменённых ордеров (110072), поэтому в конце добавляется
        метка времени: повторная постановка того же сигнала получает новые id,
        а уровень (``suffix``) и токен сохраняются всегда.
        """
        prefix = cfg.order_link_prefix
        token = f"{int(time.time() * 1000) % 10_000_000:07d}"
        tail = f"-{suffix}-{token}"
        budget = max(LINK_ID_MAX - len(prefix) - len(tail), 1)
        clean = _LINK_ID_BAD.sub("-", signal_id)
        return f"{prefix}{clean[:budget]}{tail}"[:LINK_ID_MAX]
