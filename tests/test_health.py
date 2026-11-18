"""Тесты панели здоровья: разбор логов, лимит Bybit, stderr, state, API."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from dashboard import health as health_module
from dashboard import robots as robots_module
from dashboard import server as server_module
from dashboard.health import _log_stats, _parse_line, _state_info, build_robot_health
from dashboard.robots import RobotStatus

ROBOT = "robot_trend"
NOW = 1_800_000_000.0


def _stamp(offset: float) -> str:
    """Время в формате лога (локальное) на offset секунд раньше NOW."""
    local = datetime.fromtimestamp(NOW - offset, timezone.utc).astimezone()
    return local.strftime("%Y-%m-%d %H:%M:%S")


def _line(offset: float, level: str, logger: str, message: str) -> str:
    return f"{_stamp(offset)} | {level:<8} | {logger} | {message}"


def _setup_robot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Корень проекта в tmp: .env робота с per-symbol логом и state."""
    monkeypatch.setattr(robots_module, "PROJECT_ROOT", tmp_path)
    robot_dir = tmp_path / ROBOT
    robot_dir.mkdir()
    (robot_dir / ".env").write_text(
        "CATEGORY=linear\n"
        "STATE_FILE=data/trend_state.json\n"
        "LOG_FILE=logs/trend.log\n"
        "KILL_SWITCH=data/trend.kill\n",
        encoding="utf-8",
    )
    (tmp_path / "logs").mkdir()
    (tmp_path / "data").mkdir()
    return tmp_path


def _patch_robots(
    monkeypatch: pytest.MonkeyPatch,
    *,
    symbols: list[str],
    running: bool = True,
    pids: dict[str, int] | None = None,
    alive_pid: int = 111,
) -> None:
    monkeypatch.setattr(robots_module, "get_available_robots", lambda: [ROBOT])
    monkeypatch.setattr(
        robots_module,
        "get_all_robots_status",
        lambda: [
            RobotStatus(
                name=ROBOT,
                running=running,
                pid=next(iter((pids or {}).values()), None),
                symbols=symbols,
                symbol_pids=dict(pids or {}),
            )
        ],
    )
    monkeypatch.setattr(robots_module, "get_robot_symbols", lambda name: symbols)
    monkeypatch.setattr(robots_module, "is_running", lambda pid: pid == alive_pid)


def test_log_stats_counts_window_and_limits(tmp_path: Path) -> None:
    path = tmp_path / "trend_AAAUSDT.log"
    path.write_text(
        "\n".join(
            [
                _line(7200, "ERROR", "pybit", "Too many visits. ErrCode: 10006"),
                _line(300, "ERROR", "websocket", "ping/pong timed out - goodbye"),
                _line(120, "ERROR", "pybit", "Too many visits. ErrCode: 10006"),
                _line(
                    119,
                    "ERROR",
                    "pybit",
                    "API rate limit will reset at 10:00:00. Sleeping for 2000 ms.",
                ),
                _line(60, "ERROR", "robot_trend.order_flow", "boom"),
                _line(30, "WARNING", "robot_trend.data_feed", "WS down"),
                "Traceback (most recent call last):",
            ]
        ),
        encoding="utf-8",
    )

    stats = _log_stats(path, NOW)

    assert stats["exists"] is True
    assert stats["errors_1h"] == 1
    assert stats["rate_limit_1h"] == 1
    assert stats["transport_1h"] == 1
    assert stats["warnings_1h"] == 1
    assert stats["last_error"]["message"] == "boom"
    assert stats["last_error"]["logger"] == "robot_trend.order_flow"
    assert stats["last_line_at"] is not None
    assert stats["path"] == str(path)


def test_log_stats_missing_file(tmp_path: Path) -> None:
    stats = _log_stats(tmp_path / "nope.log", NOW)

    assert stats["exists"] is False
    assert stats["size"] == 0
    assert stats["last_line_at"] is None
    assert stats["last_error"] is None
    assert stats["errors_1h"] == 0


def test_parse_line_skips_non_log_lines() -> None:
    assert _parse_line("Traceback (most recent call last):") is None
    assert _parse_line("") is None
    parsed = _parse_line(_line(10, "INFO", "robot_trend", "текст | с чертой"))
    assert parsed is not None
    _, level, name, message = parsed
    assert level == "INFO"
    assert name == "robot_trend"
    assert message == "текст | с чертой"


def test_state_info_reads_phase_and_counters(tmp_path: Path) -> None:
    path = tmp_path / "trend_state_AAAUSDT.json"
    path.write_text(
        json.dumps(
            {
                "phase": "IN_POSITION",
                "position": {"side": "long", "qty": 1.0},
                "day_pnl": -1.25,
                "day_trades": 3,
                "kill": True,
                "last_close_at": NOW - 60,
            }
        ),
        encoding="utf-8",
    )

    state = _state_info(path)

    assert state is not None
    assert state["phase"] == "IN_POSITION"
    assert state["has_position"] is True
    assert state["day_pnl"] == -1.25
    assert state["day_trades"] == 3
    assert state["kill"] is True


def test_build_robot_health_rows_and_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _setup_robot(tmp_path, monkeypatch)
    (tmp_path / "logs" / "trend_AAAUSDT.log").write_text(
        _line(100, "ERROR", "pybit", "Too many visits. ErrCode: 10006") + "\n",
        encoding="utf-8",
    )
    (tmp_path / "data" / "trend_state_AAAUSDT.json").write_text(
        json.dumps({"phase": "IDLE", "position": None, "day_pnl": 1.5}),
        encoding="utf-8",
    )
    _patch_robots(
        monkeypatch,
        symbols=["AAAUSDT", "BBBUSDT"],
        pids={"AAAUSDT": 111},
    )

    payload = build_robot_health(now=NOW)

    rows = payload["rows"]
    assert [row["symbol"] for row in rows] == ["BBBUSDT", "AAAUSDT"]
    by_symbol = {row["symbol"]: row for row in rows}

    aaa = by_symbol["AAAUSDT"]
    assert aaa["status"] == "rate_limit"
    assert aaa["pid"] == 111
    assert aaa["alive"] is True
    assert aaa["log"]["rate_limit_1h"] == 1
    assert aaa["state"]["phase"] == "IDLE"
    assert aaa["state"]["day_pnl"] == 1.5

    bbb = by_symbol["BBBUSDT"]
    assert bbb["status"] == "dead"
    assert bbb["pid"] is None

    summary = payload["summary"]
    assert summary["robots_running"] == 1
    assert summary["processes"] == 1
    assert summary["dead"] == 1
    assert summary["rate_limit_1h"] == 1
    assert summary["errors_1h"] == 0


def test_build_robot_health_priorities(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _setup_robot(tmp_path, monkeypatch)
    (tmp_path / "logs" / "trend_AAAUSDT.log").write_text(
        _line(60, "ERROR", "robot_trend.order_flow", "boom") + "\n",
        encoding="utf-8",
    )
    (tmp_path / "logs" / f"{ROBOT}_AAAUSDT.stderr.log").write_text(
        "Traceback (most recent call last):\nValueError: boom\n",
        encoding="utf-8",
    )
    _patch_robots(monkeypatch, symbols=["AAAUSDT"], pids={"AAAUSDT": 111})

    payload = build_robot_health(now=NOW)
    row = payload["rows"][0]

    assert row["status"] == "errors"
    assert row["log"]["errors_1h"] == 1
    assert row["stderr"]["size"] > 0
    assert "ValueError" in row["stderr"]["last_message"]


def test_build_robot_health_kill_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _setup_robot(tmp_path, monkeypatch)
    (tmp_path / "data" / "trend_AAAUSDT.kill").write_text("", encoding="utf-8")
    _patch_robots(monkeypatch, symbols=["AAAUSDT"], pids={"AAAUSDT": 111})

    payload = build_robot_health(now=NOW)
    row = payload["rows"][0]

    assert row["kill_file"] is True
    assert row["status"] == "kill"


def test_status_stopped_when_robot_not_running(tmp_path: Path) -> None:
    status = health_module._status(
        robot_running=False,
        pid=None,
        alive=False,
        stderr_size=0,
        kill=False,
        errors_1h=0,
        rate_limit_1h=0,
    )

    assert status == "stopped"


def test_api_robot_health_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "generated_at": "2026-11-18T09:00:00+00:00",
        "summary": {"processes": 1, "errors_1h": 0},
        "rows": [],
    }
    monkeypatch.setattr(server_module, "build_robot_health", lambda: payload)

    async def run() -> None:
        response = await server_module.api_robot_health(_plain_request(None))
        data = json.loads(bytes(response.body))
        assert response.status_code == 200
        assert data["ok"] is True
        assert data["summary"]["processes"] == 1
        assert data["rows"] == []

    asyncio.run(run())


class _Request:
    """Стаб Starlette Request для прямого вызова хендлера."""

    def __init__(self, body: dict[str, Any] | None) -> None:
        self._body = body
        self.app = SimpleNamespace(state=SimpleNamespace())

    async def json(self) -> dict[str, Any]:
        if self._body is None:
            raise ValueError("no body")
        return self._body


def _plain_request(body: dict[str, Any] | None) -> Any:
    return _Request(body)
