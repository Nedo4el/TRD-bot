"""Хранение и расчёт статистики закрытых сделок роботов.

Сделки записываются в JSON-файл по роботам. Каждая запись — закрытая
позиция с ценой входа/выхода, стороной, PnL и временем закрытия.
Источник данных — логи и state-файлы роботов (записываются при закрытии
позиции через order_flow / state).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

MSK = timezone(timedelta(hours=3))

TRADES_DIR = Path(__file__).resolve().parents[1] / "data" / "dashboard" / "trades"


@dataclass(slots=True)
class TradeRecord:
    """Одна закрытая сделка."""

    robot: str
    symbol: str
    side: str  # "long" | "short"
    qty: float
    entry_price: float
    exit_price: float
    pnl: float  # в валюте котировки (USDT)
    pnl_pct: float  # в %
    closed_at: str  # ISO UTC
    order_id: str | None = None
    exit_reason: str = "server"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class TradeStats:
    """Агрегированная статистика по сделкам."""

    trades: int = 0
    wins: int = 0
    losses: int = 0
    breakeven: int = 0
    win_rate: float = 0.0
    net_pnl: float = 0.0
    gross_profit: float = 0.0
    gross_loss: float = 0.0
    profit_factor: float | None = None
    avg_pnl: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    best_trade: float = 0.0
    worst_trade: float = 0.0
    max_drawdown: float = 0.0
    max_win_streak: int = 0
    max_loss_streak: int = 0


class TradeStore:
    """Хранилище сделок по роботам (JSON на диск).

    Без кэша в памяти: роботы пишут сделки из своих процессов,
    поэтому читаем файл с диска при каждом запросе.
    """

    def __init__(self) -> None:
        TRADES_DIR.mkdir(parents=True, exist_ok=True)

    def _path(self, robot: str) -> Path:
        return TRADES_DIR / f"{robot}.json"

    def _load(self, robot: str) -> list[TradeRecord]:
        path = self._path(robot)
        records: list[TradeRecord] = []
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                for item in raw:
                    if isinstance(item, dict):
                        records.append(TradeRecord(**item))
            except (OSError, json.JSONDecodeError, TypeError):
                records = []
        return records

    def add(self, trade: TradeRecord) -> None:
        """Добавить закрытую сделку."""
        records = self._load(trade.robot)
        records.append(trade)
        self._persist(trade.robot, records)

    def _persist(self, robot: str, records: list[TradeRecord]) -> None:
        path = self._path(robot)
        temp = path.with_suffix(".tmp")
        try:
            temp.write_text(
                json.dumps(
                    [r.to_dict() for r in records], ensure_ascii=False, indent=2
                ),
                encoding="utf-8",
            )
            temp.replace(path)
        except OSError:
            pass

    def get_trades(
        self,
        robot: str | None = None,
        days: int | None = None,
    ) -> list[TradeRecord]:
        """Получить сделки, опционально по роботу и периоду."""
        if robot:
            records = self._load(robot)
        else:
            records = []
            for name in self._list_robots():
                records.extend(self._load(name))

        if days is not None:
            cutoff = datetime.now(timezone.utc) - timedelta(days=days)
            filtered = []
            for r in records:
                try:
                    dt = datetime.fromisoformat(r.closed_at.replace("Z", "+00:00"))
                    if dt >= cutoff:
                        filtered.append(r)
                except (ValueError, AttributeError):
                    continue
            records = filtered

        records.sort(key=lambda r: r.closed_at, reverse=True)
        return records

    def stats(self, robot: str | None = None, days: int | None = None) -> TradeStats:
        """Рассчитать статистику по сделкам."""
        trades = self.get_trades(robot, days)
        stats = TradeStats()
        stats.trades = len(trades)
        if not trades:
            return stats

        pnls = [t.pnl for t in trades]
        stats.net_pnl = sum(pnls)
        stats.avg_pnl = stats.net_pnl / stats.trades

        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        be = [p for p in pnls if p == 0]

        stats.wins = len(wins)
        stats.losses = len(losses)
        stats.breakeven = len(be)
        stats.win_rate = (stats.wins / stats.trades * 100.0) if stats.trades else 0.0

        stats.gross_profit = sum(wins) if wins else 0.0
        stats.gross_loss = sum(losses) if losses else 0.0

        if stats.gross_loss != 0:
            stats.profit_factor = abs(stats.gross_profit / stats.gross_loss)
        elif stats.gross_profit > 0:
            stats.profit_factor = float("inf")

        stats.avg_win = sum(wins) / len(wins) if wins else 0.0
        stats.avg_loss = sum(losses) / len(losses) if losses else 0.0
        stats.best_trade = max(pnls) if pnls else 0.0
        stats.worst_trade = min(pnls) if pnls else 0.0

        # Максимальная просадка (running peak)
        peak = 0.0
        max_dd = 0.0
        running = 0.0
        for p in pnls:
            running += p
            peak = max(peak, running)
            dd = peak - running
            max_dd = max(max_dd, dd)
        stats.max_drawdown = max_dd

        # Серии выигрышей/проигрышей
        win_streak = 0
        loss_streak = 0
        max_win = 0
        max_loss = 0
        for p in pnls:
            if p > 0:
                win_streak += 1
                loss_streak = 0
                max_win = max(max_win, win_streak)
            elif p < 0:
                loss_streak += 1
                win_streak = 0
                max_loss = max(max_loss, loss_streak)
            else:
                win_streak = 0
                loss_streak = 0
        stats.max_win_streak = max_win
        stats.max_loss_streak = max_loss

        return stats

    def _list_robots(self) -> list[str]:
        if not TRADES_DIR.exists():
            return []
        return sorted(p.stem for p in TRADES_DIR.glob("*.json"))

    def clear(self, robot: str | None = None) -> int:
        """Удалить историю сделок (всех или одного робота). Вернуть число записей."""
        robots = [robot] if robot else self._list_robots()
        removed = 0
        for name in robots:
            records = self._load(name)
            removed += len(records)
            path = self._path(name)
            if path.exists():
                try:
                    path.unlink()
                except OSError:
                    continue
        return removed

    def record_from_state(
        self,
        robot: str,
        symbol: str,
        side: str,
        entry_price: float,
        exit_price: float,
        qty: float,
        closed_at: str,
        order_id: str | None = None,
        exit_reason: str = "server",
    ) -> None:
        """Записать сделку из данных состояния робота."""
        pnl = (
            (exit_price - entry_price) * qty
            if side == "long"
            else (entry_price - exit_price) * qty
        )
        pnl_pct = (
            (exit_price - entry_price) / entry_price * 100.0
            if side == "long"
            else (entry_price - exit_price) / entry_price * 100.0
        )
        self.add(
            TradeRecord(
                robot=robot,
                symbol=symbol,
                side=side,
                qty=qty,
                entry_price=entry_price,
                exit_price=exit_price,
                pnl=round(pnl, 4),
                pnl_pct=round(pnl_pct, 4),
                closed_at=closed_at,
                order_id=order_id,
                exit_reason=exit_reason,
            )
        )

    def add_closed(
        self,
        robot: str,
        symbol: str,
        side: str,
        entry_price: float,
        qty: float,
        pnl: float,
        exit_reason: str = "server",
        closed_at: str | None = None,
    ) -> None:
        """Записать закрытую сделку с известным PnL (например, с биржи).

        exit_price восстанавливается из PnL: long — entry + pnl/qty,
        short — entry - pnl/qty.
        """
        if closed_at is None:
            closed_at = datetime.now(timezone.utc).isoformat()
        exit_price = entry_price
        pnl_pct = 0.0
        if qty > 0:
            exit_price = (
                entry_price + pnl / qty if side == "long" else entry_price - pnl / qty
            )
            notional = entry_price * qty
            if notional > 0:
                pnl_pct = pnl / notional * 100.0
        self.add(
            TradeRecord(
                robot=robot,
                symbol=symbol,
                side=side,
                qty=qty,
                entry_price=entry_price,
                exit_price=round(exit_price, 8),
                pnl=round(pnl, 4),
                pnl_pct=round(pnl_pct, 4),
                closed_at=closed_at,
                exit_reason=exit_reason,
            )
        )
