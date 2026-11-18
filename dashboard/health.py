"""Здоровье роботов: процессы, ошибки в логах, рейт-лимит Bybit, stderr, state.

Панель отвечает на вопрос «робот работает или врёт»: считает ERROR/WARNING
за последний час по хвосту лога каждой монеты, отдельно считает ответы Bybit
10006 (Too many visits), показывает свежесть лога, размер stderr-файла
(в норме 0 байт) и фазу state-файла.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dashboard import robots as robots_module

logger = logging.getLogger(__name__)

_TAIL_BYTES = 256_000
_WINDOW_SEC = 3600.0
_LINE_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \| (\w+)\s*\| ([^|]+?) \| (.*)$"
)
_RATE_LIMIT_MARKERS = ("10006", "Too many visits")
_RATE_LIMIT_FOLLOWUP = ("API rate limit will reset", "Sleeping for")
_TRANSPORT_LOGGERS = ("websocket",)
_TRANSPORT_MARKERS = (
    "ping/pong",
    "Read timed out",
    "Connection reset",
    "Connection aborted",
    "WebSocket",
)
_ERROR_LEVELS = ("ERROR", "CRITICAL")
# Логи пишутся локальным временем (logging asctime) — переводим в epoch по нему.
_LOCAL_TZ = datetime.now(timezone.utc).astimezone().tzinfo or timezone.utc
_SEVERITY = {
    "errors": 0,
    "dead": 1,
    "kill": 2,
    "rate_limit": 3,
    "stopped": 4,
    "ok": 5,
}


def _iso(ts: float | None) -> str | None:
    """Unix-время (сек) → ISO UTC; 0/None — времени нет."""
    if not ts or ts <= 0:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


def _display(path: Path | None) -> str | None:
    """Путь относительно корня проекта (для показа в UI)."""
    if path is None:
        return None
    try:
        return str(path.relative_to(robots_module.PROJECT_ROOT))
    except ValueError:
        return str(path)


def _project_path(raw: str | None) -> Path | None:
    """Путь из .env робота (относительный — от корня проекта)."""
    text = (raw or "").strip()
    if not text:
        return None
    path = Path(text)
    return path if path.is_absolute() else robots_module.PROJECT_ROOT / path


def _tail_lines(path: Path) -> list[str]:
    """Последние строки файла (читаем хвост, не весь лог)."""
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - _TAIL_BYTES))
            data = handle.read()
    except OSError:
        return []
    lines = data.decode("utf-8", errors="replace").splitlines()
    if size > _TAIL_BYTES and lines:
        lines = lines[1:]  # первая строка могла обрезаться
    return lines


def _parse_line(line: str) -> tuple[float, str, str, str] | None:
    """Строка лога → (epoch, level, logger, message) или None."""
    match = _LINE_RE.match(line)
    if match is None:
        return None
    stamp, level, name, message = match.groups()
    try:
        moment = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S").replace(tzinfo=_LOCAL_TZ)
    except ValueError:
        return None
    return moment.timestamp(), level.upper(), name.strip(), message


def _is_rate_limit(message: str) -> bool:
    """Ответ Bybit «слишком много запросов» (pybit сам ретраит)."""
    return any(marker in message for marker in _RATE_LIMIT_MARKERS)


def _is_rate_limit_followup(message: str) -> bool:
    """Вторая строка того же ответа 10006 («sleeping and retrying»)."""
    return any(marker in message for marker in _RATE_LIMIT_FOLLOWUP)


def _is_transport(name: str, message: str) -> bool:
    """Сетевой шум WS/REST: реконнект, таймаут, обрыв — не ошибка логики."""
    if name in _TRANSPORT_LOGGERS:
        return True
    return any(marker in message for marker in _TRANSPORT_MARKERS)


def _log_stats(path: Path | None, now: float) -> dict[str, Any]:
    """Ошибки/лимиты за час, последняя строка и последняя ошибка лога."""
    stats: dict[str, Any] = {
        "path": _display(path),
        "exists": False,
        "size": 0,
        "updated_at": None,
        "last_line_at": None,
        "errors_1h": 0,
        "rate_limit_1h": 0,
        "transport_1h": 0,
        "warnings_1h": 0,
        "last_error": None,
    }
    if path is None or not path.is_file():
        return stats
    try:
        info = path.stat()
    except OSError:
        return stats
    stats["exists"] = True
    stats["size"] = info.st_size
    stats["updated_at"] = _iso(info.st_mtime)

    since = now - _WINDOW_SEC
    errors = 0
    rate_limit = 0
    transport = 0
    warnings = 0
    last_epoch = 0.0
    last_error: dict[str, str] | None = None
    for line in _tail_lines(path):
        parsed = _parse_line(line)
        if parsed is None:
            continue
        epoch, level, name, message = parsed
        last_epoch = max(last_epoch, epoch)
        if level in _ERROR_LEVELS:
            last_error = {
                "at": _iso(epoch) or "",
                "logger": name,
                "message": message[:200],
            }
            if epoch >= since:
                if _is_rate_limit(message):
                    rate_limit += 1
                elif _is_rate_limit_followup(message):
                    continue
                elif _is_transport(name, message):
                    transport += 1
                else:
                    errors += 1
        elif level == "WARNING" and epoch >= since:
            warnings += 1

    stats["errors_1h"] = errors
    stats["rate_limit_1h"] = rate_limit
    stats["transport_1h"] = transport
    stats["warnings_1h"] = warnings
    stats["last_error"] = last_error
    if last_epoch > 0:
        stats["last_line_at"] = _iso(last_epoch)
        stats["_last_epoch"] = last_epoch
    return stats


def _stderr_info(path: Path) -> dict[str, Any]:
    """Файл stderr процесса: в норме пустой, иначе там traceback."""
    info: dict[str, Any] = {
        "path": _display(path),
        "size": 0,
        "updated_at": None,
        "last_message": None,
    }
    if not path.is_file():
        return info
    try:
        stat = path.stat()
    except OSError:
        return info
    info["size"] = stat.st_size
    info["updated_at"] = _iso(stat.st_mtime)
    lines = [line.strip() for line in _tail_lines(path) if line.strip()]
    if lines:
        info["last_message"] = lines[-1][:200]
    return info


def _num(value: Any) -> float | None:
    """Число из JSON (или None)."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _state_info(path: Path | None) -> dict[str, Any] | None:
    """Фаза, дневной PnL, число сделок и kill из state-файла."""
    if path is None or not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    day_trades = _num(payload.get("day_trades"))
    return {
        "phase": str(payload.get("phase") or "—"),
        "has_position": isinstance(payload.get("position"), dict),
        "day_pnl": _num(payload.get("day_pnl")),
        "day_trades": int(day_trades) if day_trades is not None else 0,
        "kill": bool(payload.get("kill")),
        "last_close_at": _iso(_num(payload.get("last_close_at"))),
    }


def _status(
    *,
    robot_running: bool,
    pid: int | None,
    alive: bool,
    stderr_size: int,
    kill: bool,
    errors_1h: int,
    rate_limit_1h: int,
) -> str:
    """Итоговый статус монеты (важное — выше по приоритету)."""
    if stderr_size > 0:
        return "errors"
    if kill:
        return "kill"
    if pid is None or not alive:
        return "dead" if robot_running else "stopped"
    if errors_1h > 0:
        return "errors"
    if rate_limit_1h > 0:
        return "rate_limit"
    return "ok"


def _row(
    robot: str,
    symbol: str,
    robot_running: bool,
    symbol_pids: dict[str, int],
    now: float,
) -> dict[str, Any]:
    """Строка здоровья по одной монете робота."""
    env = robots_module._symbol_env(robot, symbol)
    log_path = _project_path(env.get("LOG_FILE"))
    state_path = _project_path(env.get("STATE_FILE"))
    kill_path = _project_path(env.get("KILL_SWITCH"))
    stderr_path = robots_module.PROJECT_ROOT / "logs" / f"{robot}_{symbol}.stderr.log"

    pid = symbol_pids.get(symbol)
    alive = pid is not None and robots_module.is_running(pid)
    log = _log_stats(log_path, now)
    stderr = _stderr_info(stderr_path)
    state = _state_info(state_path)
    kill_file = bool(kill_path and kill_path.exists())
    kill = kill_file or bool(state and state.get("kill"))

    status = _status(
        robot_running=robot_running,
        pid=pid,
        alive=alive,
        stderr_size=stderr["size"],
        kill=kill,
        errors_1h=log["errors_1h"],
        rate_limit_1h=log["rate_limit_1h"],
    )
    log.pop("_last_epoch", None)
    return {
        "robot": robot,
        "symbol": symbol,
        "robot_running": robot_running,
        "pid": pid,
        "alive": alive,
        "status": status,
        "kill_file": kill_file,
        "log": log,
        "stderr": stderr,
        "state": state,
        "last_activity_at": log["last_line_at"] or stderr["updated_at"],
    }


def build_robot_health(now: float | None = None) -> dict[str, Any]:
    """Собрать здоровье всех роботов по их процессам, логам и state-файлам."""
    moment = now if now is not None else time.time()
    statuses = robots_module.get_all_robots_status()
    by_name = {status.name: status for status in statuses}
    robots = robots_module.get_available_robots()

    rows: list[dict[str, Any]] = []
    for robot in robots:
        status = by_name.get(robot)
        if status is None:
            status = robots_module.RobotStatus(name=robot, running=False)
        for symbol in sorted(robots_module.get_robot_symbols(robot)):
            rows.append(_row(robot, symbol, status.running, status.symbol_pids, moment))

    rows.sort(
        key=lambda row: (
            _SEVERITY.get(row["status"], 9),
            row["robot"],
            row["symbol"],
        )
    )
    last_activity = max(
        (row["last_activity_at"] for row in rows if row["last_activity_at"]),
        default=None,
    )
    summary = {
        "robots_total": len(robots),
        "robots_running": sum(1 for status in statuses if status.running),
        "processes": sum(len(status.symbol_pids) for status in statuses),
        "dead": sum(1 for row in rows if row["status"] == "dead"),
        "errors_1h": sum(row["log"]["errors_1h"] for row in rows),
        "rate_limit_1h": sum(row["log"]["rate_limit_1h"] for row in rows),
        "transport_1h": sum(row["log"]["transport_1h"] for row in rows),
        "warnings_1h": sum(row["log"]["warnings_1h"] for row in rows),
        "stderr_bytes": sum(row["stderr"]["size"] for row in rows),
        "last_activity_at": last_activity,
    }
    return {"generated_at": _iso(moment), "summary": summary, "rows": rows}
