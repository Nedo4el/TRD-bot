"""Тесты TradeStore: запись сделок, фильтры, агрегированная статистика."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from dashboard.trade_store import TradeRecord, TradeStore


@pytest.fixture()
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TradeStore:
    """TradeStore с изолированной папкой данных."""
    import dashboard.trade_store as ts

    monkeypatch.setattr(ts, "TRADES_DIR", tmp_path / "trades")
    return TradeStore()


def _rec(
    robot: str = "robot_trend",
    symbol: str = "BTCUSDT",
    pnl: float = 10.0,
    closed_at: str = "2026-11-13T10:00:00+00:00",
    side: str = "long",
) -> TradeRecord:
    return TradeRecord(
        robot=robot,
        symbol=symbol,
        side=side,
        qty=0.1,
        entry_price=100.0,
        exit_price=110.0,
        pnl=pnl,
        pnl_pct=10.0,
        closed_at=closed_at,
    )


def test_add_and_get_roundtrip(store: TradeStore) -> None:
    store.add(_rec())
    trades = store.get_trades(robot="robot_trend")
    assert len(trades) == 1
    assert trades[0].symbol == "BTCUSDT"
    assert trades[0].pnl == 10.0


def test_persist_to_disk(store: TradeStore, tmp_path: Path) -> None:
    store.add(_rec())
    # Новый инстанс читает с диска
    store2 = TradeStore()
    trades = store2.get_trades(robot="robot_trend")
    assert len(trades) == 1


def test_clear_all_and_single_robot(store: TradeStore) -> None:
    store.add(_rec(robot="robot_trend"))
    store.add(_rec(robot="robot_flat"))
    store.add(_rec(robot="robot_flat", symbol="ETHUSDT"))

    removed = store.clear(robot="robot_flat")
    assert removed == 2
    assert store.get_trades(robot="robot_flat") == []
    assert len(store.get_trades(robot="robot_trend")) == 1

    removed = store.clear()
    assert removed == 1
    assert store.get_trades() == []
    assert store._list_robots() == []


def test_record_from_state_long_pnl(store: TradeStore) -> None:
    store.record_from_state(
        robot="robot_trend",
        symbol="BTCUSDT",
        side="long",
        entry_price=100.0,
        exit_price=110.0,
        qty=2.0,
        closed_at="2026-11-13T10:00:00+00:00",
    )
    trades = store.get_trades(robot="robot_trend")
    assert trades[0].pnl == pytest.approx(20.0)
    assert trades[0].pnl_pct == pytest.approx(10.0)


def test_record_from_state_short_pnl(store: TradeStore) -> None:
    store.record_from_state(
        robot="robot_trend",
        symbol="BTCUSDT",
        side="short",
        entry_price=100.0,
        exit_price=90.0,
        qty=2.0,
        closed_at="2026-11-13T10:00:00+00:00",
    )
    trades = store.get_trades(robot="robot_trend")
    assert trades[0].pnl == pytest.approx(20.0)
    assert trades[0].pnl_pct == pytest.approx(10.0)


def test_stats_basic(store: TradeStore) -> None:
    store.add(_rec(pnl=10.0, closed_at="2026-11-13T10:00:00+00:00"))
    store.add(_rec(pnl=-5.0, closed_at="2026-11-13T11:00:00+00:00"))
    store.add(_rec(pnl=20.0, closed_at="2026-11-13T12:00:00+00:00"))

    s = store.stats(robot="robot_trend")
    assert s.trades == 3
    assert s.wins == 2
    assert s.losses == 1
    assert s.win_rate == pytest.approx(66.666, abs=0.1)
    assert s.net_pnl == pytest.approx(25.0)
    assert s.gross_profit == pytest.approx(30.0)
    assert s.gross_loss == pytest.approx(-5.0)
    assert s.profit_factor == pytest.approx(6.0)
    assert s.avg_pnl == pytest.approx(25.0 / 3)
    assert s.avg_win == pytest.approx(15.0)
    assert s.avg_loss == pytest.approx(-5.0)
    assert s.best_trade == pytest.approx(20.0)
    assert s.worst_trade == pytest.approx(-5.0)


def test_stats_max_drawdown(store: TradeStore) -> None:
    # +10, +10, -30, +5 → peak 20, trough -10 → dd 30
    store.add(_rec(pnl=10.0, closed_at="2026-11-13T10:00:00+00:00"))
    store.add(_rec(pnl=10.0, closed_at="2026-11-13T11:00:00+00:00"))
    store.add(_rec(pnl=-30.0, closed_at="2026-11-13T12:00:00+00:00"))
    store.add(_rec(pnl=5.0, closed_at="2026-11-13T13:00:00+00:00"))

    s = store.stats(robot="robot_trend")
    assert s.max_drawdown == pytest.approx(30.0)


def test_stats_streaks(store: TradeStore) -> None:
    # Серия: W, W, W, L, L → max_win=3, max_loss=2
    pnls = [1.0, 1.0, 1.0, -1.0, -1.0]
    for i, p in enumerate(pnls):
        store.add(
            _rec(pnl=p, closed_at=f"2026-11-13T1{i}:00:00+00:00")
        )
    s = store.stats(robot="robot_trend")
    assert s.max_win_streak == 3
    assert s.max_loss_streak == 2


def test_stats_breakeven(store: TradeStore) -> None:
    store.add(_rec(pnl=0.0))
    s = store.stats(robot="robot_trend")
    assert s.trades == 1
    assert s.breakeven == 1
    assert s.wins == 0
    assert s.losses == 0
    assert s.win_rate == 0.0


def test_stats_empty(store: TradeStore) -> None:
    s = store.stats(robot="robot_trend")
    assert s.trades == 0
    assert s.net_pnl == 0.0
    assert s.profit_factor is None


def test_stats_profit_factor_no_losses(store: TradeStore) -> None:
    store.add(_rec(pnl=10.0))
    s = store.stats(robot="robot_trend")
    assert s.profit_factor == float("inf")


def test_days_filter(store: TradeStore) -> None:
    now = datetime.now(timezone.utc)
    old = (now - timedelta(days=10)).isoformat()
    recent = (now - timedelta(days=1)).isoformat()
    store.add(_rec(pnl=10.0, closed_at=old))
    store.add(_rec(pnl=-5.0, closed_at=recent))

    all_trades = store.get_trades(robot="robot_trend")
    assert len(all_trades) == 2

    week = store.get_trades(robot="robot_trend", days=7)
    assert len(week) == 1
    assert week[0].pnl == pytest.approx(-5.0)

    s = store.stats(robot="robot_trend", days=7)
    assert s.trades == 1
    assert s.net_pnl == pytest.approx(-5.0)


def test_all_robots_filter(store: TradeStore) -> None:
    store.add(_rec(robot="robot_trend", pnl=10.0))
    store.add(_rec(robot="robot_flat", pnl=-5.0))

    trades = store.get_trades()  # все роботы
    assert len(trades) == 2

    s = store.stats()
    assert s.trades == 2
    assert s.net_pnl == pytest.approx(5.0)


def test_trades_sorted_newest_first(store: TradeStore) -> None:
    store.add(_rec(closed_at="2026-11-13T10:00:00+00:00"))
    store.add(_rec(closed_at="2026-11-13T12:00:00+00:00"))
    store.add(_rec(closed_at="2026-11-13T11:00:00+00:00"))

    trades = store.get_trades(robot="robot_trend")
    assert trades[0].closed_at == "2026-11-13T12:00:00+00:00"
    assert trades[-1].closed_at == "2026-11-13T10:00:00+00:00"


def test_add_closed_long(store: TradeStore) -> None:
    store.add_closed(
        robot="robot_trend",
        symbol="BTCUSDT",
        side="long",
        entry_price=100.0,
        qty=2.0,
        pnl=20.0,
        exit_reason="server",
    )
    trades = store.get_trades(robot="robot_trend")
    assert len(trades) == 1
    assert trades[0].exit_price == pytest.approx(110.0)
    assert trades[0].pnl == pytest.approx(20.0)
    assert trades[0].pnl_pct == pytest.approx(10.0)
    assert trades[0].exit_reason == "server"
    assert trades[0].closed_at  # заполнено автоматически


def test_add_closed_short(store: TradeStore) -> None:
    store.add_closed(
        robot="robot_flat",
        symbol="ETHUSDT",
        side="short",
        entry_price=100.0,
        qty=2.0,
        pnl=20.0,
    )
    trades = store.get_trades(robot="robot_flat")
    assert trades[0].exit_price == pytest.approx(90.0)
    assert trades[0].pnl_pct == pytest.approx(10.0)


def test_add_closed_negative_pnl(store: TradeStore) -> None:
    store.add_closed(
        robot="robot_trend",
        symbol="BTCUSDT",
        side="long",
        entry_price=100.0,
        qty=1.0,
        pnl=-5.0,
    )
    trades = store.get_trades(robot="robot_trend")
    assert trades[0].exit_price == pytest.approx(95.0)
    assert trades[0].pnl_pct == pytest.approx(-5.0)
