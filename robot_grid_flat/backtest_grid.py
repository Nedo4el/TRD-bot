"""Бэктест сетки robot_grid_flat: лимитные входы/TP по свечам Bybit.

Запуск из корня проекта:
    py robot_grid_flat/backtest_grid.py

Модель (свечная аппроксимация live-логики):
- вход post-only: Buy заполняется, если low ≤ уровня; Sell — если high ≥ уровня;
- TP-limit: Buy — high ≥ TP; Sell — low ≤ TP (maker-комиссия);
- сеточный SL: если свеча пробила границу ±1×ATR — выход по триггеру,
  иначе по close (close, если гэп);
- reset/halt/trailing — закрытие всех позиций по close (taker);
- размеры уровней, стейт-машина — реальный GridStrategy (как в live);
- гарды новостей/funding/глубины отключены (исторических данных нет).
"""

from __future__ import annotations

import asyncio
import logging
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from backtest import fetch_candles
from core.bybit_client import BybitClient, Candle
from core.config import Config, load_bot_env
from robot_grid_flat.main import load_grid_config
from robot_grid_flat.strategy import (
    Decision,
    GridPlan,
    GridStrategy,
    InstrumentInfo,
    MarketSnapshot,
    level_key,
    level_qty,
    tp_price_for,
)

DEPOSIT = 100.0
MAX_EXPOSURE_PCT = 80.0
WARMUP_DAYS = 4
SNAPSHOT_BARS = 320

RUNS = [
    ("BTRUSDT", "12.08.26", "25.08.26"),
    ("BTRUSDT", "03.09.26", "22.09.26"),
    ("BRUSDT", "20.03.26", "07.08.26"),
    ("AKEUSDT", "26.07.26", "14.09.26"),
]


def parse_date(text: str, end: bool = False) -> int:
    """Дата DD.MM.YY → unix-мс (end=True — конец дня включительно)."""
    dt = datetime.strptime(text, "%d.%m.%y").replace(tzinfo=timezone.utc)
    if end:
        dt += timedelta(days=1) - timedelta(milliseconds=1)
    return int(dt.timestamp() * 1000)


@dataclass
class PendingEntry:
    side: str
    price: float
    usd: float
    step: float


@dataclass
class SimPosition:
    side: str
    entry: float
    qty: float
    step: float
    tp: float


@dataclass
class RunResult:
    symbol: str
    period: str
    bars: int
    build_note: str
    trades: int
    wins: int
    pnl_usd: float
    final_equity: float
    max_dd_pct: float
    resets: int
    halts: int
    win_rate: float = 0.0
    pnl_pct: float = 0.0


class GridSim:
    """Свечная симуляция исполнения сетки поверх GridStrategy."""

    def __init__(
        self,
        strategy: GridStrategy,
        instrument: InstrumentInfo,
        fee_maker: float,
        fee_taker: float,
    ) -> None:
        self.strategy = strategy
        self.instrument = instrument
        self.fee_maker = fee_maker
        self.fee_taker = fee_taker
        self.cash = DEPOSIT
        self.pendings: list[PendingEntry] = []
        self.positions: dict[str, SimPosition] = {}
        self.trades: list[float] = []
        self.build_note = ""
        self.resets = 0
        self.halts = 0
        self.halted = False

    def equity(self, price: float) -> float:
        """Кэш + нереализованный PnL открытых позиций по price."""
        unreal = 0.0
        for pos in self.positions.values():
            direction = 1.0 if pos.side == "Buy" else -1.0
            unreal += (price - pos.entry) * pos.qty * direction
        return self.cash + unreal

    def _fill_tps(self, candle: Candle) -> None:
        """Закрыть позиции, чей TP пробила свеча (обработка до входов)."""
        for key, pos in list(self.positions.items()):
            if pos.side == "Buy":
                hit = candle.high >= pos.tp
            else:
                hit = candle.low <= pos.tp
            if not hit:
                continue
            direction = 1.0 if pos.side == "Buy" else -1.0
            gross = (pos.tp - pos.entry) * pos.qty * direction
            fee = pos.tp * pos.qty * self.fee_maker
            self.cash += gross - fee
            self.trades.append(gross - fee)
            del self.positions[key]
            self.strategy.mark_tp_filled(key)

    def _fill_entries(self, candle: Candle) -> None:
        """Исполнить лимитные входы, пробитые свечей (поставлены раньше)."""
        still: list[PendingEntry] = []
        for pending in self.pendings:
            if pending.side == "Buy":
                hit = candle.low <= pending.price
            else:
                hit = candle.high >= pending.price
            if not hit:
                still.append(pending)
                continue
            qty = level_qty(pending.usd, pending.price, self.instrument)
            if qty < self.instrument.min_qty:
                continue
            self.cash -= pending.price * qty * self.fee_maker
            key = level_key(pending.side, pending.price)
            self.positions[key] = SimPosition(
                side=pending.side,
                entry=pending.price,
                qty=qty,
                step=pending.step,
                tp=tp_price_for(
                    pending.side, pending.price, pending.step, self.fee_maker
                ),
            )
            self.strategy.mark_entry_filled(key, pending.price, qty)
        self.pendings = still

    def _snapshot(self, candles: list[Candle], i: int) -> MarketSnapshot:
        """Срез бара i: окно SNAPSHOT_BARS, цена с учётом пробоя SL."""
        candle = candles[i]
        window = candles[max(0, i - SNAPSHOT_BARS + 1) : i + 1]
        price = candle.close
        plan = self.strategy.plan
        if plan is not None:
            mult = self.strategy.cfg.grid_sl_atr_mult
            sl_long = plan.lower - mult * plan.atr
            sl_short = plan.upper + mult * plan.atr
            if candle.low <= sl_long:
                price = sl_long
            elif candle.high >= sl_short:
                price = sl_short
        long_qty = sum(
            p.qty for p in self.positions.values() if p.side == "Buy"
        )
        short_qty = sum(
            p.qty for p in self.positions.values() if p.side == "Sell"
        )
        return MarketSnapshot(
            now=candle.open_time / 1000,
            candles=window,
            price=price,
            bid=price * 0.9999,
            ask=price * 1.0001,
            equity=self.equity(price),
            long_qty=long_qty,
            short_qty=short_qty,
            funding_pct=None,
            spread_pct=0.01,
            depth_notional=None,
            news_ok=True,
            manual_pause=False,
            tf_minutes=5,
            instrument=self.instrument,
        )

    def _sl_exit_price(
        self, position: SimPosition, candle: Candle,
        plan: GridPlan | None, reason: str,
    ) -> float:
        """Цена закрытия при reset: триггер SL, если свеча его пробила."""
        if plan is not None and reason.startswith("SL СЕТКИ"):
            mult = self.strategy.cfg.grid_sl_atr_mult
            sl_long = plan.lower - mult * plan.atr
            sl_short = plan.upper + mult * plan.atr
            if position.side == "Buy" and candle.low <= sl_long:
                return sl_long
            if position.side == "Sell" and candle.high >= sl_short:
                return sl_short
        return candle.close

    def _close_all(
        self, candle: Candle, plan: GridPlan | None, reason: str
    ) -> None:
        """Закрыть все позиции (reset/halt/trailing)."""
        for key, pos in list(self.positions.items()):
            exit_price = self._sl_exit_price(pos, candle, plan, reason)
            direction = 1.0 if pos.side == "Buy" else -1.0
            gross = (exit_price - pos.entry) * pos.qty * direction
            fee = exit_price * pos.qty * self.fee_taker
            self.cash += gross - fee
            self.trades.append(gross - fee)
            del self.positions[key]
        self.pendings.clear()

    def execute(self, decision: Decision, candle: Candle,
                plan: GridPlan | None) -> None:
        """Исполнить решение стратегии (зеркало main.py, без сети)."""
        if decision.cancel_entries or decision.cancel_sides:
            sides_set = (
                None if decision.cancel_entries else set(decision.cancel_sides)
            )
            keep: list[PendingEntry] = []
            for pending in self.pendings:
                if sides_set is None or pending.side in sides_set:
                    self.strategy.mark_entry_cancelled(
                        level_key(pending.side, pending.price)
                    )
                else:
                    keep.append(pending)
            self.pendings = keep

        if decision.close_all:
            self._close_all(candle, plan, decision.reason)

        kind = decision.kind
        if kind == "reset":
            self.resets += 1
        if kind == "halt":
            self.halts += 1
            self.halted = True
            return
        if kind == "build" and not self.build_note:
            self.build_note = decision.reason
        if kind == "wait" and not self.build_note:
            self.build_note = f"не построена: {decision.reason}"
        if kind in ("halt", "wait", "pause", "reset"):
            return

        plan_now = self.strategy.plan
        step = plan_now.step if plan_now else 0.0
        for entry in decision.entries:
            qty = level_qty(entry.usd, entry.level_price, self.instrument)
            if (
                qty < self.instrument.min_qty
                or entry.usd < self.instrument.min_notional
                or step <= 0
            ):
                continue
            self.pendings.append(
                PendingEntry(
                    side=entry.side,
                    price=entry.level_price,
                    usd=entry.usd,
                    step=step,
                )
            )
            self.strategy.register_entry(
                entry.side, entry.level_price, entry.usd, "sim"
            )

    def run(self, candles: list[Candle], trade_from: int) -> RunResult:
        """Пройти свечи начиная с trade_from (индекса стартовой даты)."""
        equity_curve: list[float] = []
        for i in range(trade_from, len(candles)):
            candle = candles[i]
            plan_before = self.strategy.plan

            self._fill_tps(candle)
            self._fill_entries(candle)

            snap = self._snapshot(candles, i)
            decision = self.strategy.next(snap)
            self.execute(decision, candle, plan_before)

            equity_curve.append(self.equity(candle.close))
            if self.halted:
                break

        wins = sum(1 for p in self.trades if p > 0)
        total = len(self.trades)
        final = equity_curve[-1] if equity_curve else DEPOSIT
        peak = 0.0
        max_dd = 0.0
        for eq in equity_curve:
            peak = max(peak, eq)
            if peak > 0:
                max_dd = max(max_dd, (peak - eq) / peak * 100.0)
        return RunResult(
            symbol="",
            period="",
            bars=max(0, len(candles) - trade_from),
            build_note=self.build_note,
            trades=total,
            wins=wins,
            pnl_usd=final - DEPOSIT,
            final_equity=final,
            max_dd_pct=max_dd,
            resets=self.resets,
            halts=self.halts,
            win_rate=wins / total * 100.0 if total else 0.0,
            pnl_pct=(final - DEPOSIT) / DEPOSIT * 100.0,
        )


async def run_one(
    config: Config,
    client: BybitClient,
    symbol: str,
    start_text: str,
    end_text: str,
) -> RunResult:
    """Один прогон: фильтры инструмента → свечи → симуляция."""
    filters = await client.get_instrument_filters(symbol)
    instrument = InstrumentInfo(
        tick_size=filters.tick_size,
        qty_step=filters.qty_step,
        min_qty=filters.min_qty,
        min_notional=filters.min_notional,
    )

    start_ms = parse_date(start_text)
    end_ms = parse_date(end_text, end=True)
    fetch_from = start_ms - WARMUP_DAYS * 86_400_000

    config.symbol = symbol
    candles = await fetch_candles(config, limit=10**9, start_ms=fetch_from,
                                  end_ms=end_ms)
    if not candles:
        raise ValueError(f"{symbol}: свечи не получены")

    trade_from = next(
        (i for i, c in enumerate(candles) if c.open_time >= start_ms), 0
    )

    cfg = load_grid_config()
    cfg.max_exposure_pct = MAX_EXPOSURE_PCT
    strategy = GridStrategy(cfg)
    sim = GridSim(
        strategy, instrument,
        fee_maker=cfg.fee_maker, fee_taker=cfg.fee_taker,
    )
    result = sim.run(candles, trade_from)
    result.symbol = symbol
    result.period = f"{start_text}–{end_text}"
    return result


def print_results(results: list[RunResult]) -> None:
    """Итоговая таблица прогонов."""
    print()
    print("=" * 100)
    print(
        f"  GRID FLAT BACKTEST | депозит ${DEPOSIT:.0f} | "
        f"exposure<={MAX_EXPOSURE_PCT:.0f}% | TF=5m | "
        f"fees maker 0.02% / taker 0.055%"
    )
    print("=" * 100)
    header = (
        f"{'Символ':<10} {'Период':<26} {'Баров':>6} {'Сделок':>6} "
        f"{'Win%':>6} {'PnL%':>8} {'Итог$':>8} {'MaxDD%':>7} "
        f"{'Resets':>6}"
    )
    print(header)
    print("-" * 100)
    for r in results:
        print(
            f"{r.symbol:<10} {r.period:<26} {r.bars:>6} {r.trades:>6} "
            f"{r.win_rate:>6.1f} {r.pnl_pct:>+8.2f} {r.final_equity:>8.2f} "
            f"{r.max_dd_pct:>7.2f} {r.resets:>6}"
        )
    print("-" * 100)
    for r in results:
        note = r.build_note.replace("\n", " ")[:90]
        print(f"  {r.symbol} {r.period}: {note}")
        if r.halts:
            print(f"    !! остановлен kill-switch'ем, halt={r.halts}")
    print("=" * 100)


async def main() -> None:
    """Точка входа: 4 прогона последовательно."""
    bot_dir = Path(__file__).resolve().parent
    load_bot_env(bot_dir)
    logging.getLogger().setLevel(logging.WARNING)
    config = Config()

    client = BybitClient(config)
    results: list[RunResult] = []
    try:
        for symbol, start, end in RUNS:
            print(f"Прогон {symbol} {start}–{end} ...", flush=True)
            try:
                results.append(
                    await run_one(config, client, symbol, start, end)
                )
            except Exception as exc:  # noqa: BLE001 — отчёт по каждому прогону
                print(f"  ОШИБКА {symbol}: {exc}")
    finally:
        client.close()

    print_results(results)


if __name__ == "__main__":
    asyncio.run(main())
