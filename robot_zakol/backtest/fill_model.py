"""Реалистичная fill-модель: очередь, PostOnly, latency, partial, slippage."""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass

from robot_zakol.backtest.config import FillConfig

logger = logging.getLogger(__name__)


@dataclass
class PendingLimit:
    """Активная PostOnly buy-лимитка в бэктесте."""

    target: float
    qty: float
    placed_ts_ms: int
    deadline_ts_ms: int
    filled_qty: float = 0.0
    active: bool = True
    arrived: bool = False
    touched: bool = False


@dataclass
class FillOutcome:
    """Результат попытки fill на тике."""

    filled_qty: float = 0.0
    fill_price: float = 0.0
    post_only_reject: bool = False
    partial: bool = False
    queue_reject: bool = False
    in_flight: bool = False


class FillModel:
    """Оценивает fill/отказ/частичный объём на каждом тике."""

    def __init__(self, cfg: FillConfig, tick_size: float) -> None:
        self.cfg = cfg
        self.tick_size = tick_size
        self._rng = random.Random(cfg.seed)

    def on_tick(
        self,
        order: PendingLimit,
        tick_price: float,
        tick_size_vol: float,
        prev_price: float | None,
        ts_ms: int,
    ) -> FillOutcome:
        """Проверить один тик против лимитки.

        PostOnly reject: уровень прошли, пока ордер был в пути (latency).
        После arrival: price <= target → queue fill (частичный возможен).
        ideal: price <= target → полный fill без очереди/latency.
        """
        if not order.active:
            return FillOutcome()
        if ts_ms > order.deadline_ts_ms:
            return FillOutcome()
        target = order.target
        latency = self.cfg.latency_ms

        if self.cfg.ideal:
            if tick_price <= target:
                return FillOutcome(
                    filled_qty=order.qty - order.filled_qty,
                    fill_price=target,
                )
            return FillOutcome()

        if not order.arrived:
            if ts_ms < order.placed_ts_ms + latency:
                return FillOutcome(in_flight=True)
            order.arrived = True
            if self.cfg.post_only_strict and tick_price < target - self.tick_size:
                logger.debug(
                    "PostOnly reject on arrive: price=%.8g target=%.8g",
                    tick_price,
                    target,
                )
                return FillOutcome(post_only_reject=True)

        if tick_price > target:
            return FillOutcome()

        order.touched = True
        need = order.qty - order.filled_qty
        if need <= 0:
            return FillOutcome()
        if tick_price < target - self.tick_size:
            return FillOutcome(filled_qty=need, fill_price=target)
        ref_vol = max(tick_size_vol, 1e-12)
        queue_ahead = self.cfg.queue_factor * ref_vol
        fill_prob = need / (need + queue_ahead)
        fill_prob = max(0.0, min(1.0, fill_prob))
        if self._rng.random() > fill_prob:
            return FillOutcome(queue_reject=True)

        available = ref_vol if self.cfg.allow_partial else need
        filled = min(need, available)
        if filled <= 0:
            return FillOutcome(queue_reject=True)
        partial = filled < need - 1e-12
        return FillOutcome(
            filled_qty=filled,
            fill_price=target,
            partial=partial,
        )

    def entry_latency_price(
        self,
        signal_price: float,
        market_price: float,
        latency_ms: float,
        ts_ms: int,
        order_ts_ms: int,
    ) -> float:
        """Симулировать задержку: если latency > 0 — берём текущую цену рынка."""
        if latency_ms <= 0:
            return signal_price
        if ts_ms - order_ts_ms >= latency_ms and market_price < signal_price:
            return market_price
        return signal_price

    def exit_slippage(self, raw_price: float, side: str) -> float:
        """Slippage на market-выходе (SL/trail): хуже для нас."""
        slip = self.cfg.slippage_pct
        if slip <= 0:
            return raw_price
        if side == "sell":
            return raw_price * (1.0 - slip)
        return raw_price * (1.0 + slip)


def should_post_only_reject(
    prev_price: float | None,
    tick_price: float,
    target: float,
    tick_size: float,
    strict: bool,
) -> bool:
    """Чистая проверка PostOnly gap-reject (для тестов)."""
    if not strict or prev_price is None:
        return False
    return prev_price > target and tick_price < target - tick_size


def queue_fill_prob(need_qty: float, queue_ahead: float) -> float:
    """fill_probability = our_qty / (our_qty + queue_ahead)."""
    if need_qty <= 0:
        return 0.0
    return need_qty / (need_qty + max(queue_ahead, 0.0))
