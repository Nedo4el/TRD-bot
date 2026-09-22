"""Robot Grid Flat — исполнение нейтральной сетки вокруг POC (3h).

Запуск:  py robot_grid_flat/main.py
Остановка: Ctrl+C или kill-switch (бот завершается сам).

Движок core/engine.py не используется: сетке нужен свой цикл
(post-only лимиты, хедж-позиции, TP на ордерах, сеточный stop-market).
Вся логика решений — robot_grid_flat/strategy.py, здесь только I/O.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from core.bybit_client import BybitClient
from core.config import (
    Config,
    get_env_bool,
    get_env_float,
    get_env_int,
    get_env_str,
    load_bot_env,
)
from core.engine import setup_logging_and_validate
from core.logger import get_logger
from core.notifier import Notifier
from core.utils import exponential_backoff
from robot_grid_flat.strategy import (
    Decision,
    GridConfig,
    GridStrategy,
    InstrumentInfo,
    MarketSnapshot,
    PlannedEntry,
    level_qty,
    tp_price_for,
)

logger = get_logger(__name__)

STATE_FILENAME = "grid_state.json"
PAUSE_FILENAME = "PAUSE"
FUNDING_TTL_S = 300.0
DEPTH_BAND = 0.001
RECONNECT_AFTER_ERRORS = 5


def load_grid_config() -> GridConfig:
    """Собрать GridConfig из .env бота (дефолты — в GridConfig)."""
    return GridConfig(
        poc_window_min=get_env_int("POC_WINDOW_MIN", 180),
        poc_bins=get_env_int("POC_BINS", 100),
        value_area_pct=get_env_float("VALUE_AREA_PCT", 70.0),
        atr_period=get_env_int("ATR_PERIOD", 14),
        atr_extend_mult=get_env_float("ATR_EXTEND_MULT", 1.0),
        poc_unstable_pct=get_env_float("POC_UNSTABLE_PCT", 0.5),
        poc_unstable_bars=get_env_int("POC_UNSTABLE_BARS", 3),
        grid_step_atr_mult=get_env_float("GRID_STEP_ATR_MULT", 0.3),
        min_tick_mult=get_env_int("MIN_TICK_MULT", 3),
        fee_taker=get_env_float("FEE_TAKER", 0.00055),
        fee_maker=get_env_float("FEE_MAKER", 0.0002),
        step_fee_mult=get_env_float("STEP_FEE_MULT", 3.0),
        levels_min=get_env_int("LEVELS_MIN", 10),
        levels_max=get_env_int("LEVELS_MAX", 50),
        risk_per_trade_pct=get_env_float("RISK_PER_TRADE_PCT", 1.0),
        max_exposure_pct=get_env_float("MAX_EXPOSURE_PCT", 20.0),
        leverage_cap=get_env_float("LEVERAGE_CAP", 3.0),
        max_open_levels=get_env_int("MAX_OPEN_LEVELS", 10),
        grid_sl_atr_mult=get_env_float("GRID_SL_ATR_MULT", 1.0),
        trail_trigger_pct=get_env_float("TRAIL_TRIGGER_PCT", 3.0),
        trail_giveback_pct=get_env_float("TRAIL_GIVEBACK_PCT", 1.0),
        kill_24h_pct=get_env_float("KILL_24H_PCT", 5.0),
        funding_max_pct=get_env_float("FUNDING_MAX_PCT", 0.05),
        max_spread_pct=get_env_float("MAX_SPREAD_PCT", 0.05),
        depth_mult=get_env_float("DEPTH_MULT", 5.0),
        impulse_pct=get_env_float("IMPULSE_PCT", 5.0),
        impulse_window=get_env_int("IMPULSE_WINDOW", 5),
        impulse_cooldown_bars=get_env_int("IMPULSE_COOLDOWN_BARS", 30),
        recenter_interval_s=get_env_float("RECENTER_INTERVAL_S", 1800.0),
        poc_recenter_pct=get_env_float("POC_RECENTER_PCT", 0.5),
        trend_ema_fast=get_env_int("TREND_EMA_FAST", 50),
        trend_ema_slow=get_env_int("TREND_EMA_SLOW", 200),
        trend_threshold_pct=get_env_float("TREND_THRESHOLD", 2.0),
        order_size_cap_usd=get_env_float("ORDER_SIZE_CAP_USD", 0.0),
    )


def parse_news_windows(raw: str) -> list[tuple[float, float]]:
    """Разобрать NEWS_WINDOWS='ts1:ts2,ts3:ts4' (unix, секунды)."""
    windows: list[tuple[float, float]] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            start_s, end_s = chunk.split(":")
            windows.append((float(start_s), float(end_s)))
        except ValueError:
            logger.warning("Не разобрано окно новостей: %r", chunk)
    return windows


class GridExecutor:
    """Исполнитель решений GridStrategy: срезы рынка, ордера, стопы, state."""

    def __init__(self, config: Config, strategy: GridStrategy, bot_dir: Path) -> None:
        self.config = config
        self.strategy = strategy
        self.bot_dir = bot_dir
        self.client = BybitClient(config)
        self.notifier = Notifier(config)
        self.state_path = bot_dir / STATE_FILENAME
        self.pause_path = bot_dir / PAUSE_FILENAME
        self.news_windows = parse_news_windows(get_env_str("NEWS_WINDOWS", ""))
        self.hedge = get_env_bool("HEDGE_MODE", True)

        self._instrument: InstrumentInfo | None = None
        self._funding_pct: float | None = None
        self._funding_at: float = 0.0
        self._consecutive_errors = 0
        self._sim_counter = 0
        self._last_notify_key: tuple[str, str] | None = None

    def _pos_idx(self, position_side: str) -> int | None:
        """positionIdx для хедж-режима (1=long, 2=short); None — one-way."""
        if not self.hedge:
            return None
        return 1 if position_side == "Buy" else 2

    # ----------------------------- State ---------------------------------

    def _restore_state(self) -> None:
        """Загрузить grid_state.json (сетка переживает рестарт)."""
        if not self.state_path.exists():
            logger.info("grid_state.json нет — старт с чистого листа")
            return
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            self.strategy.restore(data)
        except (json.JSONDecodeError, TypeError, ValueError, KeyError) as exc:
            logger.warning("grid_state повреждён (%s) — чистый старт", exc)

    def _save_state(self) -> None:
        """Атомарно сохранить состояние на диск."""
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(self.strategy.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(self.state_path)

    # --------------------------- Срез рынка -------------------------------

    async def _funding(self, now: float) -> float | None:
        """Funding rate в % (кэш FUNDING_TTL_S)."""
        if self._funding_pct is not None and now - self._funding_at < FUNDING_TTL_S:
            return self._funding_pct
        try:
            rate = await self.client.get_funding_rate(self.config.symbol)
        except Exception as exc:  # noqa: BLE001 — сбой API не должен ронять цикл
            logger.warning("Funding недоступен: %s", exc)
            return self._funding_pct
        self._funding_pct = rate * 100.0 if rate is not None else None
        self._funding_at = now
        return self._funding_pct

    def _news_ok(self, now: float) -> bool:
        """False — сейчас окно новостей (CPI/FOMC/листинг ±15 мин)."""
        return not any(start <= now <= end for start, end in self.news_windows)

    async def _snapshot(self) -> MarketSnapshot:
        """Собрать всё, что нужно стратегии на один цикл."""
        symbol = self.config.symbol
        if self._instrument is None:
            filters = await self.client.get_instrument_filters(symbol)
            self._instrument = InstrumentInfo(
                tick_size=filters.tick_size,
                qty_step=filters.qty_step,
                min_qty=filters.min_qty,
                min_notional=filters.min_notional,
            )

        candles = await self.client.get_klines(
            symbol, interval=self.config.timeframe, limit=self.config.kline_limit
        )
        book = await self.client.get_orderbook(symbol, limit=50)
        bid = float(book["b"][0][0])
        ask = float(book["a"][0][0])
        mid = (bid + ask) / 2

        bid_depth = sum(
            float(p) * float(q)
            for p, q in book["b"]
            if float(p) >= bid * (1 - DEPTH_BAND)
        )
        ask_depth = sum(
            float(p) * float(q)
            for p, q in book["a"]
            if float(p) <= ask * (1 + DEPTH_BAND)
        )

        price = self.client.last_ws_price.get(symbol)
        if price is None:
            price = mid

        now = time.time()
        equity = await self.client.get_balance()
        positions = await self.client.get_positions(symbol)
        long_qty = sum(p.size for p in positions if p.side == "Buy")
        short_qty = sum(p.size for p in positions if p.side == "Sell")
        spread_pct = (ask - bid) / mid * 100.0 if mid > 0 else None

        return MarketSnapshot(
            now=now,
            candles=candles,
            price=price,
            bid=bid,
            ask=ask,
            equity=equity,
            long_qty=long_qty,
            short_qty=short_qty,
            funding_pct=await self._funding(now),
            spread_pct=spread_pct,
            depth_notional=min(bid_depth, ask_depth),
            news_ok=self._news_ok(now),
            manual_pause=self.pause_path.exists(),
            tf_minutes=max(int(self.config.timeframe), 1),
            instrument=self._instrument,
        )

    # -------------------------- Синхронизация -----------------------------

    async def _sync_orders(self) -> None:
        """Разрешить судьбу входов и TP: исполнены/отменены → обновить уровень."""
        symbol = self.config.symbol
        open_orders = await self.client.get_open_orders(symbol)
        open_ids = {o.get("orderId") for o in open_orders}

        for key, level in list(self.strategy.levels.items()):
            if level.status == "entry" and level.order_id not in open_ids:
                await self._resolve_entry(key)
            elif (
                level.status == "position"
                and level.tp_order_id
                and level.tp_order_id not in open_ids
            ):
                await self._resolve_tp(key)

        for key, level in list(self.strategy.levels.items()):
            if level.status == "position" and not level.tp_order_id and level.qty > 0:
                await self._place_tp(key)

    async def _resolve_entry(self, key: str) -> None:
        """Ордер исчез из открытых — исполнен (в т.ч. частично) или отменён."""
        level = self.strategy.levels.get(key)
        if level is None:
            return
        try:
            meta = await self.client.get_order(self.config.symbol, level.order_id)
        except Exception as exc:  # noqa: BLE001 — сбой API не должен ронять цикл
            logger.warning("Не удалось получить ордер %s: %s", level.order_id, exc)
            return
        if meta is None:
            logger.warning("Ордер %s не найден в истории", level.order_id)
            return
        status = meta.get("orderStatus", "")
        filled = float(meta.get("cumExecQty") or 0)
        avg = float(meta.get("avgPrice") or 0) or level.level_price
        if filled > 0:
            self.strategy.mark_entry_filled(key, avg, filled)
        elif status in ("Cancelled", "Rejected", "Deactivated"):
            self.strategy.mark_entry_cancelled(key)

    async def _resolve_tp(self, key: str) -> None:
        """TP исчез из открытых — исполнен или отменён (поставим заново)."""
        level = self.strategy.levels.get(key)
        if level is None:
            return
        try:
            meta = await self.client.get_order(self.config.symbol, level.tp_order_id)
        except Exception as exc:  # noqa: BLE001 — сбой API не должен ронять цикл
            logger.warning("Не удалось получить TP %s: %s", level.tp_order_id, exc)
            self.strategy.clear_tp(key)
            return
        if meta is not None and meta.get("orderStatus") == "Filled":
            self.strategy.mark_tp_filled(key)
        else:
            self.strategy.clear_tp(key)

    async def _place_tp(self, key: str) -> None:
        """Выставить TP-limit для исполненного уровня."""
        level = self.strategy.levels.get(key)
        if level is None:
            return
        step = level.step or (self.strategy.plan.step if self.strategy.plan else 0.0)
        if step <= 0:
            return
        tp = tp_price_for(
            level.side, level.entry_price, step, self.strategy.cfg.fee_maker
        )
        close_side = "Sell" if level.side == "Buy" else "Buy"
        if self.config.simulation_mode:
            self.strategy.mark_tp_placed(key, f"sim-tp-{self._sim_counter}")
            self._sim_counter += 1
            logger.info("[SIMULATION] TP %s %.8g qty=%.8g", close_side, tp, level.qty)
            return
        try:
            resp = await self.client.place_order(
                symbol=self.config.symbol,
                side=close_side,
                qty=level.qty,
                order_type="Limit",
                price=tp,
                reduce_only=True,
                position_idx=self._pos_idx(level.side),
            )
        except Exception as exc:  # noqa: BLE001 — сбой API не должен ронять цикл
            logger.warning("Не выставлен TP %s: %s", key, exc)
            return
        self.strategy.mark_tp_placed(key, resp["result"]["orderId"])

    # --------------------------- Исполнение -------------------------------

    async def _execute(self, decision: Decision, snap: MarketSnapshot) -> None:
        """Исполнить решение стратегии (только I/O, логики решений нет)."""
        if decision.cancel_entries or decision.cancel_sides:
            sides = None if decision.cancel_entries else set(decision.cancel_sides)
            await self._cancel_entries(sides)

        if decision.close_all:
            await self._close_all(snap)

        if decision.kind in ("halt", "wait", "pause", "reset"):
            return

        if decision.entries:
            await self._place_entries(decision.entries, snap)

        if decision.kind in ("build", "rebuild", "run"):
            await self._maintain_sl(snap, force=decision.kind in ("build", "rebuild"))

    async def _cancel_entries(self, sides: set[str] | None) -> None:
        """Отменить входящие ордера (все или только указанных сторон)."""
        for key, level in list(self.strategy.levels.items()):
            if level.status != "entry":
                continue
            if sides is not None and level.side not in sides:
                continue
            if self.config.simulation_mode:
                self.strategy.mark_entry_cancelled(key)
                logger.info("[SIMULATION] отмена входа %s", key)
                continue
            try:
                await self.client.cancel_order(self.config.symbol, level.order_id)
            except Exception as exc:  # noqa: BLE001 — сбой API не должен ронять цикл
                logger.debug("Отмена %s: %s (ордер мог уже закрыться)", key, exc)

    async def _place_entries(
        self, entries: list[PlannedEntry], snap: MarketSnapshot
    ) -> None:
        """Выставить post-only входы на свободные уровни."""
        inst = snap.instrument
        for entry in entries:
            if entry.side == "Buy" and entry.level_price >= snap.ask:
                continue
            if entry.side == "Sell" and entry.level_price <= snap.bid:
                continue
            qty = level_qty(entry.usd, entry.level_price, inst)
            if qty < inst.min_qty or entry.usd < inst.min_notional:
                logger.warning(
                    "Уровень %.8g пропущен: qty=%.8g < минимума %.8g",
                    entry.level_price,
                    qty,
                    inst.min_qty,
                )
                continue
            if self.config.simulation_mode:
                self._sim_counter += 1
                self.strategy.register_entry(
                    entry.side, entry.level_price, entry.usd, f"sim-{self._sim_counter}"
                )
                logger.info(
                    "[SIMULATION] вход %s %.8g qty=%.8g ($%.2f)",
                    entry.side,
                    entry.level_price,
                    qty,
                    entry.usd,
                )
                continue
            try:
                resp = await self.client.place_order(
                    symbol=self.config.symbol,
                    side=entry.side,
                    qty=qty,
                    order_type="Limit",
                    price=entry.level_price,
                    post_only=True,
                    position_idx=self._pos_idx(entry.side),
                )
            except Exception as exc:  # noqa: BLE001 — сбой API не должен ронять цикл
                logger.warning(
                    "Не выставлен вход %s %.8g: %s",
                    entry.side,
                    entry.level_price,
                    exc,
                )
                continue
            self.strategy.register_entry(
                entry.side, entry.level_price, entry.usd, resp["result"]["orderId"]
            )

    async def _close_all(self, snap: MarketSnapshot) -> None:
        """Отменить все ордера и закрыть все позиции маркетом."""
        if self.config.simulation_mode:
            logger.info(
                "[SIMULATION] закрытие всей сетки (long=%.8g short=%.8g)",
                snap.long_qty,
                snap.short_qty,
            )
            return
        try:
            await self.client.cancel_all_orders(self.config.symbol)
        except Exception as exc:  # noqa: BLE001 — сбой API не должен ронять цикл
            logger.warning("cancel_all_orders: %s", exc)
        for side, qty in (("Buy", snap.long_qty), ("Sell", snap.short_qty)):
            if qty <= 0:
                continue
            close_side = "Sell" if side == "Buy" else "Buy"
            try:
                await self.client.place_order(
                    symbol=self.config.symbol,
                    side=close_side,
                    qty=qty,
                    order_type="Market",
                    reduce_only=True,
                    position_idx=self._pos_idx(side),
                )
                logger.info("Закрыта сторона %s qty=%.8g", side, qty)
            except Exception as exc:  # noqa: BLE001 — сбой API не должен ронять цикл
                logger.error("Не закрыта сторона %s: %s", side, exc)

    async def _maintain_sl(self, snap: MarketSnapshot, force: bool) -> None:
        """Сеточный stop-market: граница ± 1 ATR, qty = открытая позиция стороны."""
        plan = self.strategy.plan
        if plan is None:
            return
        inst = snap.instrument
        specs = (
            (
                "Buy",
                snap.long_qty,
                plan.lower - self.strategy.cfg.grid_sl_atr_mult * plan.atr,
            ),
            (
                "Sell",
                snap.short_qty,
                plan.upper + self.strategy.cfg.grid_sl_atr_mult * plan.atr,
            ),
        )
        for side, qty, trigger in specs:
            order_id, cur_qty, cur_trigger = self.strategy.sl_info(side)
            if qty <= 0:
                if order_id:
                    await self._cancel_sl(side, order_id)
                    self.strategy.clear_sl(side)
                continue
            changed = (
                not order_id
                or abs(cur_qty - qty) > inst.qty_step / 2
                or abs(cur_trigger - trigger) > inst.tick_size / 2
            )
            if not changed and not force:
                continue
            if order_id:
                await self._cancel_sl(side, order_id)
                self.strategy.clear_sl(side)
            close_side = "Sell" if side == "Buy" else "Buy"
            if self.config.simulation_mode:
                self.strategy.register_sl(side, f"sim-sl-{side}", qty, trigger)
                logger.info(
                    "[SIMULATION] SL-сетки %s trigger=%.8g qty=%.8g",
                    close_side,
                    trigger,
                    qty,
                )
                continue
            try:
                resp = await self.client.place_stop_market(
                    symbol=self.config.symbol,
                    side=close_side,
                    qty=qty,
                    trigger_price=trigger,
                    reduce_only=True,
                    position_idx=self._pos_idx(side),
                )
            except Exception as exc:  # noqa: BLE001 — сбой API не должен ронять цикл
                logger.warning("Не выставлен SL-сетки %s: %s", side, exc)
                continue
            self.strategy.register_sl(side, resp["result"]["orderId"], qty, trigger)
            logger.info("SL-сетки %s: trigger=%.8g qty=%.8g", close_side, trigger, qty)

    async def _cancel_sl(self, side: str, order_id: str) -> None:
        """Отменить сеточный стоп стороны (игнорируем «уже закрыт»)."""
        if self.config.simulation_mode:
            return
        try:
            await self.client.cancel_order(self.config.symbol, order_id)
        except Exception as exc:  # noqa: BLE001 — сбой API не должен ронять цикл
            logger.debug("Отмена SL %s: %s", side, exc)

    # --------------------------- Уведомления ------------------------------

    async def _notify_transition(self, decision: Decision) -> None:
        """Уведомить о смене состояния (build/rebuild/pause/reset/halt)."""
        if decision.kind in ("run", "wait"):
            return
        key = (decision.kind, decision.reason)
        if key == self._last_notify_key:
            return
        self._last_notify_key = key
        text = f"[grid {self.config.symbol}] {decision.kind.upper()}: {decision.reason}"
        await self.notifier.notify(text)

    # --------------------------- Главный цикл -----------------------------

    async def run(self) -> None:
        """Цикл: срез → синхронизация → решение → исполнение → state."""
        logger.info(
            "Grid Flat [%s]: %s @ %s, testnet=%s, simulation=%s, hedge=%s",
            self.strategy.name,
            self.config.symbol,
            self.config.timeframe,
            self.config.testnet,
            self.config.simulation_mode,
            self.hedge,
        )
        if self.strategy.halted:
            logger.error(
                "В grid_state стоит halt (%s) — очистите файл или снимите флаг вручную",
                self.strategy.halt_reason,
            )
            return
        if self.config.ws_enabled:
            self.client.start_ws()
        self._restore_state()

        iteration = 0
        while True:
            try:
                snap = await self._snapshot()
                if not self.config.simulation_mode:
                    await self._sync_orders()

                decision = self.strategy.next(snap)
                logger.debug("Решение: %s (%s)", decision.kind, decision.reason)
                if decision.kind != "run":
                    logger.info("СЕТКА %s: %s", decision.kind.upper(), decision.reason)

                await self._execute(decision, snap)
                await self._notify_transition(decision)
                self._save_state()
                self._consecutive_errors = 0

                if decision.kind == "halt":
                    await self.notifier.notify(
                        f"[grid {self.config.symbol}] БОТ ОСТАНОВЛЕН: {decision.reason}"
                    )
                    break
            except Exception:
                self._consecutive_errors += 1
                logger.exception("Ошибка в главном цикле")
                if self._consecutive_errors >= RECONNECT_AFTER_ERRORS:
                    logger.warning("Серия ошибок — переподключаюсь к Bybit")
                    self.client.reconnect()
                    self._consecutive_errors = 0
                await asyncio.sleep(exponential_backoff(self._consecutive_errors, 5.0))

            iteration += 1
            if iteration % 60 == 0:
                plan = self.strategy.plan
                logger.info(
                    "Итерация %d: POC=%s открытых уровней=%d",
                    iteration,
                    f"{plan.poc:.8g}" if plan else "-",
                    len(self.strategy.levels),
                )
            await asyncio.sleep(self.config.poll_interval)


async def main() -> None:
    """Точка входа: конфиг → лог → стратегия → цикл."""
    bot_dir = Path(__file__).resolve().parent
    load_bot_env(bot_dir)

    grid_cfg = load_grid_config()
    config = Config()
    setup_logging_and_validate(config)

    strategy = GridStrategy(grid_cfg)
    executor = GridExecutor(config, strategy, bot_dir)
    try:
        await executor.run()
    except KeyboardInterrupt:
        logger.info("Остановка по Ctrl+C")
    finally:
        executor._save_state()
        executor.client.close()


if __name__ == "__main__":
    asyncio.run(main())
