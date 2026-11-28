"""Тесты открытых позиций дашборда: state, сверка с биржей, ручное закрытие."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.bybit_client import BybitClient, Position
from dashboard import positions as positions_module
from dashboard import server as server_module
from dashboard.positions import (
    StatePosition,
    close_robot_position,
    list_open_positions,
    state_positions,
)
from dashboard.robots import RobotStatus

ROBOT = "robot_trend"


def _setup_robot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    api_keys: bool = True,
) -> Path:
    """Робот с .env и STATE_FILE=data/trend_state.json внутри tmp_path."""
    monkeypatch.setattr(positions_module, "PROJECT_ROOT", tmp_path)
    robot_dir = tmp_path / ROBOT
    robot_dir.mkdir()
    keys = "BYBIT_API_KEY=k\nBYBIT_API_SECRET=s\n" if api_keys else ""
    (robot_dir / ".env").write_text(
        f"{keys}TESTNET=false\nCATEGORY=linear\nSTATE_FILE=data/trend_state.json\n",
        encoding="utf-8",
    )
    return robot_dir


def _write_state(
    tmp_path: Path,
    filename: str,
    *,
    symbol: str | None,
    position: dict[str, Any] | None,
) -> None:
    path = tmp_path / "data" / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "phase": "IN_POSITION" if position else "IDLE",
                "position": position,
                "symbol": symbol or "",
            }
        ),
        encoding="utf-8",
    )


def _position(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "entry_price": 10.0,
        "qty": 2.0,
        "side": "long",
        "stop_loss": 9.0,
        "take_profit": 12.0,
        "opened_at": 1_700_000_000.0,
    }
    data.update(overrides)
    return data


class _Filters:
    tick_size = 0.01
    qty_step = 1.0
    min_qty = 1.0
    min_notional = 0.0


class FakeClient:
    """Клиент Bybit: отдаёт заданные позиции, пишет ордера в self.orders."""

    def __init__(
        self,
        rows: list[Position] | None = None,
        *,
        error: Exception | None = None,
        flat: bool = True,
        open_orders: dict[str, list[dict[str, Any]]] | None = None,
    ) -> None:
        self.rows = rows or []
        self.error = error
        self.flat = flat
        self.orders: list[dict[str, Any]] = []
        self.cancelled: list[str] = []
        self.open_orders = open_orders or {}

    async def get_all_positions(self) -> list[Position]:
        if self.error is not None:
            raise self.error
        return list(self.rows)

    async def get_open_orders(self, symbol: str) -> list[dict[str, Any]]:
        return list(self.open_orders.get(symbol, []))

    async def get_positions(self, symbol: str) -> list[Position]:
        if self.error is not None:
            raise self.error
        return [row for row in self.rows if row.symbol == symbol]

    async def get_position(self, symbol: str) -> Position | None:
        if self.flat:
            return None
        return Position(
            symbol=symbol,
            side="Buy",
            size=1.0,
            avg_price=10.0,
            unrealised_pnl=0.0,
        )

    async def get_instrument_filters(self, symbol: str) -> _Filters:
        return _Filters()

    async def place_order(
        self,
        symbol: str,
        side: str,
        qty: float,
        order_type: str = "Market",
        price: float | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        self.orders.append({"symbol": symbol, "side": side, "qty": qty, **kwargs})
        self.rows = []
        return {}

    async def cancel_all_orders(self, symbol: str) -> dict[str, Any]:
        self.cancelled.append(symbol)
        return {}


def _patch_robots(
    monkeypatch: pytest.MonkeyPatch,
    client: FakeClient,
    *,
    symbols: list[str] | None = None,
    running: bool = True,
    states: dict[str, Any] | None = None,
) -> None:
    monkeypatch.setattr(
        positions_module,
        "get_available_robots",
        lambda: [ROBOT],
    )
    monkeypatch.setattr(
        positions_module,
        "get_all_robots_status",
        lambda: [RobotStatus(name=ROBOT, running=running, pid=1)],
    )
    monkeypatch.setattr(
        positions_module,
        "get_robot_symbols",
        lambda name: list(symbols or ["AAAUSDT"]),
    )
    if states is not None:
        monkeypatch.setattr(
            positions_module, "state_positions", lambda name: dict(states)
        )
    monkeypatch.setattr(positions_module, "_client", lambda name: client)


def test_state_positions_reads_per_symbol_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _setup_robot(tmp_path, monkeypatch)
    _write_state(
        tmp_path, "trend_state_AAAUSDT.json", symbol=None, position=_position()
    )
    _write_state(tmp_path, "trend_state_BBBUSDT.json", symbol="BBBUSDT", position=None)
    _write_state(
        tmp_path,
        "trend_state_OLD.bak.json",
        symbol="OLDUSDT",
        position=_position(),
    )
    _write_state(
        tmp_path,
        "trend_state.json",
        symbol="CCCUSDT",
        position=_position(side="short", entry_price=50.0),
    )

    found = state_positions(ROBOT)

    assert set(found) == {"AAAUSDT", "CCCUSDT"}
    assert found["AAAUSDT"].side == "long"
    assert found["AAAUSDT"].entry_price == 10.0
    assert found["AAAUSDT"].stop_loss == 9.0
    assert found["AAAUSDT"].opened_at == 1_700_000_000.0
    assert found["CCCUSDT"].side == "short"


def test_list_merges_state_with_exchange(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _setup_robot(tmp_path, monkeypatch)
    _write_state(
        tmp_path, "trend_state_AAAUSDT.json", symbol="AAAUSDT", position=_position()
    )
    client = FakeClient(
        [
            Position(
                symbol="AAAUSDT",
                side="Buy",
                size=2.0,
                avg_price=10.0,
                unrealised_pnl=-1.0,
                mark_price=9.5,
            ),
            Position(
                symbol="ZZZUSDT",
                side="Sell",
                size=1.0,
                avg_price=5.0,
                unrealised_pnl=0.1,
            ),
        ]
    )
    _patch_robots(monkeypatch, client, symbols=["AAAUSDT", "BBBUSDT"])

    async def run() -> tuple[list[dict[str, Any]], list[dict[str, str]], list[str]]:
        return await list_open_positions()

    rows, errors, notes = asyncio.run(run())

    assert errors == []
    assert notes == []
    assert len(rows) == 1  # ZZZUSDT не принадлежит роботу
    row = rows[0]
    assert row["robot"] == ROBOT
    assert row["symbol"] == "AAAUSDT"
    assert row["side"] == "long"
    assert row["entry_price"] == 10.0
    assert row["mark_price"] == 9.5
    assert row["pnl_pct"] == -5.0  # -1.0 / (10 * 2) * 100
    assert row["stop_loss"] == 9.0
    assert row["take_profit"] == 12.0
    assert row["opened_at"] == "2023-11-14T22:13:20+00:00"
    assert row["robot_running"] is True
    assert row["source"] == "exchange"


def test_list_falls_back_to_state_when_exchange_down(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _setup_robot(tmp_path, monkeypatch)
    _write_state(
        tmp_path, "trend_state_AAAUSDT.json", symbol="AAAUSDT", position=_position()
    )
    _patch_robots(monkeypatch, FakeClient(error=RuntimeError("boom")))

    async def run() -> tuple[list[dict[str, Any]], list[dict[str, str]], list[str]]:
        return await list_open_positions()

    rows, errors, notes = asyncio.run(run())

    assert len(rows) == 1
    assert rows[0]["source"] == "state"
    assert rows[0]["symbol"] == "AAAUSDT"
    assert notes == []
    assert errors and errors[0]["robot"] == ROBOT
    assert "boom" in errors[0]["error"]


def test_list_hides_stopped_robot_state_when_exchange_down(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """State остановленного робота — устаревший снимок, не открытая позиция.

    Фантом: позицию закрыли руками, а state не обновился; при недоступной
    бирже такая строка раньше висела в «Открытых позициях» с кнопкой 50%.
    """
    _setup_robot(tmp_path, monkeypatch)
    _write_state(
        tmp_path, "trend_state_AAAUSDT.json", symbol="AAAUSDT", position=_position()
    )
    _patch_robots(
        monkeypatch,
        FakeClient(error=RuntimeError("boom")),
        running=False,
    )

    async def run() -> tuple[list[dict[str, Any]], list[dict[str, str]], list[str]]:
        return await list_open_positions()

    rows, errors, notes = asyncio.run(run())

    assert rows == []
    assert errors and errors[0]["robot"] == ROBOT
    assert len(notes) == 1
    assert ROBOT in notes[0] and "AAAUSDT" in notes[0]


def test_list_attributes_position_by_order_prefix_and_order_levels(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Позицию приписывает префикс ордера, SL/TP — из условных ордеров."""
    monkeypatch.setattr(positions_module, "PROJECT_ROOT", tmp_path)
    for robot, prefix in (("robot_trend", "tr-"), ("robot_yrovni_D", "zk-")):
        robot_dir = tmp_path / robot
        robot_dir.mkdir()
        (robot_dir / ".env").write_text(
            "BYBIT_API_KEY=k\nBYBIT_API_SECRET=s\n"
            f"ORDER_LINK_ID_PREFIX={prefix}\n",
            encoding="utf-8",
        )
    position = Position(
        symbol="MMTUSDT",
        side="Buy",
        size=136.0,
        avg_price=0.1449,
        unrealised_pnl=-0.68,
        mark_price=0.1399,
    )
    orders = {
        "MMTUSDT": [
            {
                "orderLinkId": "zk-MMTUSDT-0.1449-long--stop-1",
                "reduceOnly": True,
                "triggerPrice": "0.1304",
                "price": "0",
                "orderType": "Market",
            },
            {
                "orderLinkId": "zk-MMTUSDT-0.1449-long--take-1",
                "reduceOnly": True,
                "triggerPrice": "",
                "price": "0.155",
                "orderType": "Limit",
            },
        ],
    }
    clients = {
        "robot_trend": FakeClient([position], open_orders=orders),
        "robot_yrovni_D": FakeClient([position], open_orders=orders),
    }
    monkeypatch.setattr(
        positions_module,
        "get_available_robots",
        lambda: ["robot_trend", "robot_yrovni_D"],
    )
    monkeypatch.setattr(
        positions_module,
        "get_all_robots_status",
        lambda: [RobotStatus(name="robot_trend", running=False, pid=0)],
    )
    monkeypatch.setattr(
        positions_module,
        "get_robot_symbols",
        lambda name: ["MMTUSDT"],
    )
    monkeypatch.setattr(positions_module, "state_positions", lambda name: {})
    monkeypatch.setattr(positions_module, "_client", lambda name: clients[name])

    rows, errors, notes = asyncio.run(list_open_positions())

    assert errors == [] and notes == []
    assert len(rows) == 1
    assert rows[0]["robot"] == "robot_yrovni_D"
    assert rows[0]["stop_loss"] == 0.1304
    assert rows[0]["take_profit"] == 0.155
    assert rows[0]["robot_running"] is False


def test_list_state_file_wins_over_order_prefix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """State-файл робота приоритетнее префикса ордера."""
    monkeypatch.setattr(positions_module, "PROJECT_ROOT", tmp_path)
    for robot, prefix in (("robot_trend", "tr-"), ("robot_yrovni_D", "zk-")):
        robot_dir = tmp_path / robot
        robot_dir.mkdir()
        (robot_dir / ".env").write_text(
            "BYBIT_API_KEY=k\nBYBIT_API_SECRET=s\n"
            f"ORDER_LINK_ID_PREFIX={prefix}\n",
            encoding="utf-8",
        )
    position = Position(
        symbol="ENAUSDT",
        side="Buy",
        size=10.0,
        avg_price=1.0,
        unrealised_pnl=0.0,
    )
    orders = {
        "ENAUSDT": [
            {
                "orderLinkId": "zk-ENAUSDT-1.0-long--stop-1",
                "reduceOnly": True,
                "triggerPrice": "0.9",
            },
            {
                "orderLinkId": "tr-ENAUSDT-1.0-long--stop-2",
                "reduceOnly": True,
                "triggerPrice": "0.8",
            },
        ],
    }
    trend_state = StatePosition(
        symbol="ENAUSDT",
        side="long",
        entry_price=1.0,
        qty=10.0,
        stop_loss=0.85,
        take_profit=None,
        opened_at=1_700_000_000.0,
    )
    clients = {
        "robot_trend": FakeClient([position], open_orders=orders),
        "robot_yrovni_D": FakeClient([position], open_orders=orders),
    }
    monkeypatch.setattr(
        positions_module,
        "get_available_robots",
        lambda: ["robot_trend", "robot_yrovni_D"],
    )
    monkeypatch.setattr(
        positions_module,
        "get_all_robots_status",
        lambda: [RobotStatus(name="robot_trend", running=True, pid=1)],
    )
    monkeypatch.setattr(
        positions_module,
        "get_robot_symbols",
        lambda name: ["ENAUSDT"],
    )
    monkeypatch.setattr(
        positions_module,
        "state_positions",
        lambda name: {"ENAUSDT": trend_state} if name == "robot_trend" else {},
    )
    monkeypatch.setattr(positions_module, "_client", lambda name: clients[name])

    rows, errors, notes = asyncio.run(list_open_positions())

    assert errors == [] and notes == []
    assert len(rows) == 1
    assert rows[0]["robot"] == "robot_trend"
    assert rows[0]["stop_loss"] == 0.85


def test_close_full_position_market_reduce_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _setup_robot(tmp_path, monkeypatch)
    client = FakeClient(
        [
            Position(
                symbol="AAAUSDT",
                side="Buy",
                size=2.0,
                avg_price=10.0,
                unrealised_pnl=0.0,
            )
        ]
    )
    _patch_robots(monkeypatch, client, states={})

    async def run() -> tuple[bool, str]:
        return await close_robot_position(ROBOT, "AAAUSDT")

    ok, message = asyncio.run(run())

    assert ok is True
    assert "закрыта" in message
    assert len(client.orders) == 1
    order = client.orders[0]
    assert order["symbol"] == "AAAUSDT"
    assert order["side"] == "Sell"  # закрытие long — продажа
    assert order["qty"] == 2.0
    assert order["reduce_only"] is True
    assert client.cancelled == ["AAAUSDT"]


def test_close_partial_rounds_to_qty_step(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _setup_robot(tmp_path, monkeypatch)
    client = FakeClient(
        [
            Position(
                symbol="AAAUSDT",
                side="Sell",
                size=11.0,
                avg_price=10.0,
                unrealised_pnl=0.0,
                mark_price=10.0,
            )
        ]
    )
    _patch_robots(monkeypatch, client, states={})

    async def run() -> tuple[bool, str]:
        return await close_robot_position(ROBOT, "AAAUSDT", 50.0)

    ok, message = asyncio.run(run())

    assert ok is True
    assert "50%" in message
    assert client.orders[0]["qty"] == 5.0  # floor(11 * 0.5 / 1) * 1
    assert client.orders[0]["side"] == "Buy"  # закрытие short — покупка
    assert client.cancelled == []


def test_close_partial_rejects_tiny_qty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _setup_robot(tmp_path, monkeypatch)
    client = FakeClient(
        [
            Position(
                symbol="AAAUSDT",
                side="Buy",
                size=1.0,
                avg_price=10.0,
                unrealised_pnl=0.0,
                mark_price=10.0,
            )
        ]
    )
    _patch_robots(monkeypatch, client, states={})

    async def run() -> tuple[bool, str]:
        return await close_robot_position(ROBOT, "AAAUSDT", 50.0)

    ok, message = asyncio.run(run())

    assert ok is False
    assert "минимум" in message
    assert client.orders == []


def test_close_rejects_foreign_symbol_and_missing_position(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _setup_robot(tmp_path, monkeypatch)
    client = FakeClient(rows=[])
    _patch_robots(monkeypatch, client, states={})

    async def run() -> tuple[tuple[bool, str], tuple[bool, str]]:
        foreign = await close_robot_position(ROBOT, "ZZZUSDT")
        missing = await close_robot_position(ROBOT, "AAAUSDT")
        return foreign, missing

    foreign, missing = asyncio.run(run())

    assert foreign[0] is False
    assert "не принадлежит" in foreign[1]
    assert missing[0] is False
    assert "нет" in missing[1]
    assert client.orders == []


def test_api_positions_endpoints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_list() -> tuple[
        list[dict[str, Any]], list[dict[str, str]], list[str]
    ]:
        return (
            [
                {
                    "robot": ROBOT,
                    "symbol": "AAAUSDT",
                    "side": "long",
                    "qty": 2.0,
                    "entry_price": 10.0,
                    "mark_price": 10.1,
                    "unrealized_pnl": 0.2,
                    "pnl_pct": 1.0,
                    "stop_loss": 9.0,
                    "take_profit": 12.0,
                    "opened_at": None,
                    "robot_running": True,
                    "source": "exchange",
                }
            ],
            [],
            [],
        )

    calls: list[tuple[str, str, float]] = []

    async def fake_close(
        robot: str, symbol: str, pct: float = 100.0
    ) -> tuple[bool, str]:
        calls.append((robot, symbol, pct))
        return True, "закрыто"

    monkeypatch.setattr(server_module, "list_open_positions", fake_list)
    monkeypatch.setattr(server_module, "close_robot_position", fake_close)

    async def run() -> None:
        resp = await server_module.api_positions(_plain_request(None))
        data = json.loads(bytes(resp.body))
        assert resp.status_code == 200
        assert data["ok"] is True
        assert data["positions"][0]["symbol"] == "AAAUSDT"
        assert data["notes"] == []

        resp = await server_module.api_positions_close(
            _plain_request({"robot": ROBOT, "symbol": "aaausdt", "pct": 50})
        )
        data = json.loads(bytes(resp.body))
        assert resp.status_code == 200
        assert data["ok"] is True
        assert calls == [(ROBOT, "AAAUSDT", 50.0)]

        resp = await server_module.api_positions_close(
            _plain_request({"robot": ROBOT, "symbol": "BAD SYMBOL"})
        )
        assert resp.status_code == 400

        resp = await server_module.api_positions_close(
            _plain_request({"symbol": "AAAUSDT"})
        )
        assert resp.status_code == 400

    asyncio.run(run())


def test_position_from_row_reads_mark_price() -> None:
    position = BybitClient._position_from_row(
        {
            "symbol": "BTCUSDT",
            "side": "Buy",
            "size": "0.5",
            "avgPrice": "60000",
            "unrealisedPnl": "12.5",
            "markPrice": "60025",
            "positionIdx": 0,
            "stopLoss": "",
            "takeProfit": "61000",
        }
    )

    assert position.symbol == "BTCUSDT"
    assert position.mark_price == 60025.0
    assert position.take_profit == 61000.0
    assert position.stop_loss is None


class _Request:
    """Стаб Starlette Request для прямых вызовов хендлеров."""

    def __init__(self, body: dict[str, Any] | None) -> None:
        self._body = body
        self.app = SimpleNamespace(state=SimpleNamespace())
        self.query_params: dict[str, str] = {}

    async def json(self) -> dict[str, Any]:
        if self._body is None:
            raise ValueError("no body")
        return self._body


def _plain_request(body: dict[str, Any] | None) -> Any:
    return _Request(body)
