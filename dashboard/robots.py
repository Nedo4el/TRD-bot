"""Управление роботами: запуск, остановка, статус, список монет.

Каждый робот торгует набор монет (список живёт в ``dashboard.robot_coins``).
Для каждой монеты запускается отдельный процесс робота: так состояние,
логи и kill-switch не пересекаются между монетами.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values

from dashboard.robot_coins import RobotCoins, normalize_symbols

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Значение кода завершения процесса, который ещё работает (Windows).
_STILL_ACTIVE = 259
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

# --- Постепенный старт -------------------------------------------------------
# Процессы робота, поднятые в одну секунду, дают залп kline-запросов и ловят
# retCode 10006 (Too many visits). Поэтому первый символ стартует сразу, а
# остальные — равномерно внутри окна DASH_ROBOT_START_WINDOW_SEC (по умолчанию
# 30 с). 0 — поднимать все сразу; для длинных списков монет окно увеличить.
_PENDING_SUFFIX = ".pending.json"
_STAGGER_TICK_SEC = 3.0
_STATE_LOCK = threading.Lock()
_worker_started = False


@dataclass
class RobotStatus:
    """Статус одного робота."""

    name: str
    running: bool
    pid: int | None = None
    error: str | None = None
    symbols: list[str] = field(default_factory=list)
    symbol_pids: dict[str, int] = field(default_factory=dict)


def pidfile_for(robot_name: str) -> Path:
    """Путь к pid-файлу робота (legacy, первый живой процесс)."""
    return PROJECT_ROOT / "data" / f"{robot_name}.pid"


def procsfile_for(robot_name: str) -> Path:
    """Путь к реестру процессов робота (символ -> pid)."""
    return PROJECT_ROOT / "data" / f"{robot_name}.procs.json"


def is_running(pid: int) -> bool:
    """Жив ли процесс с таким PID.

    На Windows ``os.kill(pid, 0)`` вызывает TerminateProcess и убивает
    процесс, поэтому там проверка идёт через WinAPI.
    """
    if pid <= 0:
        return False
    if sys.platform == "win32":
        return _is_running_win(pid)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _is_running_win(pid: int) -> bool:
    """Проверка живости процесса на Windows (без убийства)."""
    import ctypes

    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == _STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


_robots_cache: list[str] | None = None


def get_available_robots() -> list[str]:
    """Список всех robot_* папок (список папок не меняется при старт/стоп)."""
    global _robots_cache
    if _robots_cache is None:
        _robots_cache = sorted(p.name for p in PROJECT_ROOT.glob("robot_*"))
    return _robots_cache


def get_robot_symbols(robot_name: str) -> list[str]:
    """Список торгуемых монет робота."""
    return RobotCoins.load().symbols_for(robot_name)


def _read_procs(robot_name: str) -> dict[str, int]:
    """Прочитать реестр процессов робота (символ -> pid)."""
    import json

    path = procsfile_for(robot_name)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    procs: dict[str, int] = {}
    for symbol, pid in raw.items():
        try:
            procs[str(symbol).upper()] = int(pid)
        except (TypeError, ValueError):
            continue
    return procs


def _write_procs(robot_name: str, procs: dict[str, int]) -> None:
    """Записать реестр процессов робота."""
    import json

    path = procsfile_for(robot_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(procs, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def pendingfile_for(robot_name: str) -> Path:
    """Путь к плану постепенного старта (символ -> момент запуска, unix)."""
    return PROJECT_ROOT / "data" / f"{robot_name}{_PENDING_SUFFIX}"


def _read_pending(robot_name: str) -> dict[str, int]:
    """Прочитать план постепенного старта робота."""
    import json

    path = pendingfile_for(robot_name)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    pending: dict[str, int] = {}
    for symbol, ready_at in raw.items():
        try:
            pending[str(symbol).upper()] = int(ready_at)
        except (TypeError, ValueError):
            continue
    return pending


def _write_pending(robot_name: str, pending: dict[str, int]) -> None:
    """Записать план постепенного старта робота."""
    import json

    path = pendingfile_for(robot_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(pending, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def stagger_window_sec() -> float:
    """Окно постепенного старта, сек (0 — поднимать все процессы сразу)."""
    raw = os.getenv("DASH_ROBOT_START_WINDOW_SEC", "30").strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 1200.0


def _per_symbol_path(raw: str, robot_name: str, symbol: str) -> str:
    """Вставить символ в имя файла: data/trend_state.json -> data/trend_state_BTCUSDT.json."""
    text = raw.strip()
    if not text:
        return text
    path = Path(text)
    if path.suffix:
        return str(path.with_name(f"{path.stem}_{symbol}{path.suffix}"))
    return f"{text}_{symbol}"


def _symbol_env(robot_name: str, symbol: str) -> dict[str, str]:
    """Окружение процесса робота для конкретной монеты."""
    env = os.environ.copy()
    env["SYMBOL"] = symbol
    env_file = PROJECT_ROOT / robot_name / ".env"
    raw: dict[str, str | None] = {}
    if env_file.exists():
        try:
            raw = dict(dotenv_values(env_file))
        except OSError:
            raw = {}
    env["STATE_FILE"] = _per_symbol_path(
        str(raw.get("STATE_FILE") or f"data/{robot_name}_state.json"),
        robot_name,
        symbol,
    )
    env["LOG_FILE"] = _per_symbol_path(
        str(raw.get("LOG_FILE") or f"logs/{robot_name}.log"), robot_name, symbol
    )
    env["KILL_SWITCH"] = _per_symbol_path(
        str(raw.get("KILL_SWITCH") or f"data/{robot_name}.kill"), robot_name, symbol
    )
    env["PROCESSED_FILE"] = _per_symbol_path(
        str(raw.get("PROCESSED_FILE") or f"data/{robot_name}_processed.json"),
        robot_name,
        symbol,
    )
    return env


def _spawn(robot_name: str, symbol: str) -> int:
    """Запустить процесс робота для монеты, вернуть pid.

    Запуск модулем (``-m``), а не путём к скрипту: тогда cwd попадает в
    ``sys.path`` и импорты ``core``/``dashboard`` не падают на старте.
    stderr уходит в ``logs/<robot>_<symbol>.stderr.log`` — иначе падение
    процесса видно только как «pid был и пропал», без причины.
    """
    log_dir = PROJECT_ROOT / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    stderr_path = log_dir / f"{robot_name}_{symbol}.stderr.log"
    with stderr_path.open("ab") as stderr:
        proc = subprocess.Popen(
            [sys.executable, "-m", f"{robot_name}.main"],
            cwd=str(PROJECT_ROOT),
            env=_symbol_env(robot_name, symbol),
            creationflags=subprocess.CREATE_NO_WINDOW,
            stdout=subprocess.DEVNULL,
            stderr=stderr,
        )
    return proc.pid


def _write_legacy_pid(robot_name: str, procs: dict[str, int]) -> None:
    """Обновить legacy pid-файл (первый живой процесс) для старых инструментов."""
    pidfile = pidfile_for(robot_name)
    alive = [pid for pid in procs.values() if is_running(pid)]
    if alive:
        pidfile.parent.mkdir(exist_ok=True)
        pidfile.write_text(str(alive[0]))
    else:
        pidfile.unlink(missing_ok=True)


def _stagger_tick() -> None:
    """Поднять процессы, у которых по плану уже наступило время старта."""
    now = time.time()
    for plan in sorted(PROJECT_ROOT.glob(f"data/*{_PENDING_SUFFIX}")):
        robot_name = plan.name[: -len(_PENDING_SUFFIX)]
        started: list[str] = []
        with _STATE_LOCK:
            pending = _read_pending(robot_name)
            if not pending:
                plan.unlink(missing_ok=True)
                continue
            due = [symbol for symbol, ready_at in pending.items() if ready_at <= now]
            if not due:
                continue
            procs = _read_procs(robot_name)
            for symbol in sorted(due):
                pid = procs.get(symbol)
                if pid and is_running(pid):
                    pending.pop(symbol, None)
                    continue
                try:
                    procs[symbol] = _spawn(robot_name, symbol)
                except (OSError, ValueError) as exc:
                    logger.error("Постепенный старт %s %s: %s", robot_name, symbol, exc)
                    continue
                pending.pop(symbol, None)
                started.append(symbol)
            _write_procs(robot_name, procs)
            _write_legacy_pid(robot_name, procs)
            if pending:
                _write_pending(robot_name, pending)
            else:
                plan.unlink(missing_ok=True)
        if started:
            logger.info("Робот %s: подняты %s", robot_name, ", ".join(started))


def _stagger_loop() -> None:
    while True:
        try:
            _stagger_tick()
        except Exception:
            logger.exception("Постепенный старт: сбой итерации")
        time.sleep(_STAGGER_TICK_SEC)


def start_stagger_worker() -> None:
    """Поднять фоновый поток постепенного старта (идемпотентно)."""
    global _worker_started
    with _STATE_LOCK:
        if _worker_started:
            return
        _worker_started = True
    threading.Thread(target=_stagger_loop, name="robot-stagger", daemon=True).start()


def _start_all(
    robot_name: str, symbols: list[str], procs: dict[str, int]
) -> tuple[bool, str]:
    """Поднять все процессы сразу (окно разноса выключено)."""
    started: list[str] = []
    try:
        for symbol in symbols:
            procs[symbol] = _spawn(robot_name, symbol)
            started.append(symbol)
    except (OSError, ValueError) as e:
        for symbol in started:
            _stop_symbol(robot_name, symbol, procs)
        _write_procs(robot_name, procs)
        return False, f"Ошибка запуска: {e}"
    _write_procs(robot_name, procs)
    _write_legacy_pid(robot_name, procs)
    logger.info("Робот %s запущен: %s", robot_name, ", ".join(started))
    return True, f"Робот {robot_name} запущен: {', '.join(started)}"


def start_robot(robot_name: str) -> tuple[bool, str]:
    """Запустить робота на всех его монетах. Возвращает (успех, сообщение).

    При включённом окне разноса первая монета стартует сразу, остальные —
    равномерно внутри окна. Монета, добавленная вручную, стартует немедленно.
    """
    robot_path = PROJECT_ROOT / robot_name / "main.py"
    if not robot_path.exists():
        return False, f"Робот {robot_name} не найден"

    symbols = get_robot_symbols(robot_name)
    if not symbols:
        return False, f"У робота {robot_name} не выбраны монеты"

    start_stagger_worker()
    with _STATE_LOCK:
        procs = _read_procs(robot_name)
        pending = _read_pending(robot_name)
        todo = [
            symbol
            for symbol in symbols
            if not (procs.get(symbol) and is_running(procs[symbol]))
            and symbol not in pending
        ]
        if not todo:
            if pending:
                return (
                    True,
                    f"Робот {robot_name} стартует: {', '.join(sorted(pending))}",
                )
            return True, f"Робот {robot_name} уже работает"

        window = stagger_window_sec()
        if window <= 0:
            return _start_all(robot_name, todo, procs)

        first, rest = todo[0], todo[1:]
        try:
            procs[first] = _spawn(robot_name, first)
        except (OSError, ValueError) as exc:
            return False, f"Ошибка запуска: {exc}"

        now = time.time()
        spacing = window / len(rest) if rest else 0.0
        pending = {s: int(now + spacing * (i + 1)) for i, s in enumerate(rest)}
        _write_procs(robot_name, procs)
        _write_legacy_pid(robot_name, procs)
        if not pending:
            logger.info("Робот %s запущен: %s", robot_name, first)
            return True, f"Робот {robot_name} запущен: {first}"
        _write_pending(robot_name, pending)
        logger.info(
            "Робот %s: старт %s, остальные %d — в течение %.0f мин",
            robot_name,
            first,
            len(pending),
            window / 60,
        )
        return (
            True,
            (
                f"Робот {robot_name} запущен: {first}; "
                f"остальные ({', '.join(sorted(pending))}) — "
                f"в течение {window / 60:.0f} мин"
            ),
        )


def _stop_symbol(robot_name: str, symbol: str, procs: dict[str, int]) -> bool:
    """Остановить процесс робота по одной монете."""
    pid = procs.pop(symbol, None)
    if not pid or not is_running(pid):
        return False
    try:
        os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and is_running(pid):
            time.sleep(0.1)
        if is_running(pid):
            sig = getattr(signal, "SIGKILL", signal.SIGTERM)
            os.kill(pid, sig)
    except OSError as e:
        logger.warning("Не удалось остановить %s %s: %s", robot_name, symbol, e)
        return False
    logger.info("Робот %s остановлен по монете %s", robot_name, symbol)
    return True


def stop_robot(robot_name: str, timeout: float = 2.0) -> tuple[bool, str]:
    """Остановить робота (все его монеты). Возвращает (успех, сообщение).

    Args:
        robot_name: Имя робота.
        timeout: Время ожидания перед SIGKILL (сек).
    """
    with _STATE_LOCK:
        procs = _read_procs(robot_name)
        pendingfile_for(robot_name).unlink(missing_ok=True)
        stopped: list[str] = []
        for symbol in list(procs):
            if _stop_symbol(robot_name, symbol, procs):
                stopped.append(symbol)
        _write_procs(robot_name, procs)
        pidfile_for(robot_name).unlink(missing_ok=True)
        procsfile_for(robot_name).unlink(missing_ok=True)
    if stopped:
        return True, f"Робот {robot_name} остановлен: {', '.join(stopped)}"
    if not procs:
        return False, f"Робот {robot_name} не запущен"
    return False, f"Процессы робота {robot_name} уже не активны"


def stop_all_robots(timeout: float = 2.0) -> dict[str, tuple[bool, str]]:
    """Остановить все запущенные роботы."""
    results = {}
    for robot in get_available_robots():
        results[robot] = stop_robot(robot, timeout=timeout)
    return results


def set_robot_symbols(robot_name: str, symbols: list[str]) -> tuple[bool, str]:
    """Задать список монет робота и применить его к работающему процессу.

    Удалённые монеты останавливаются, добавленные — запускаются.
    """
    if robot_name not in get_available_robots():
        return False, f"Робот {robot_name} не найден"
    normalized = normalize_symbols(symbols)
    coins = RobotCoins.load()
    old = coins.symbols_for(robot_name)
    coins.set(robot_name, normalized)

    with _STATE_LOCK:
        procs = _read_procs(robot_name)
        running = {sym: pid for sym, pid in procs.items() if is_running(pid)}
        pending = _read_pending(robot_name)
        if pending:
            kept = {sym: at for sym, at in pending.items() if sym in normalized}
            if kept:
                _write_pending(robot_name, kept)
            else:
                pendingfile_for(robot_name).unlink(missing_ok=True)
            pending = kept
        if not running:
            _write_procs(robot_name, procs)
            return (
                True,
                f"Монеты робота {robot_name} сохранены: {', '.join(normalized) or '—'}",
            )

        removed = [sym for sym in old if sym not in normalized]
        added = [
            sym for sym in normalized if sym not in running and sym not in pending
        ]
        stopped: list[str] = []
        for symbol in removed:
            if _stop_symbol(robot_name, symbol, procs):
                stopped.append(symbol)
        started: list[str] = []
        try:
            for symbol in added:
                procs[symbol] = _spawn(robot_name, symbol)
                started.append(symbol)
        except (OSError, ValueError) as e:
            logger.error("Не удалось запустить %s: %s", robot_name, e)

        _write_procs(robot_name, procs)
        _write_legacy_pid(robot_name, procs)
    parts = [f"Монеты робота {robot_name} обновлены"]
    if started:
        parts.append(f"запущено: {', '.join(started)}")
    if stopped:
        parts.append(f"остановлено: {', '.join(stopped)}")
    if stopped:
        parts.append("по остановленным монетам позиция остаётся на бирже под SL/TP")
    return True, "; ".join(parts)


def add_robot_symbol(robot_name: str, symbol: str) -> tuple[bool, str]:
    """Добавить монету в список робота."""
    symbols = get_robot_symbols(robot_name)
    if symbol in symbols:
        return True, f"{symbol} уже в списке робота {robot_name}"
    return set_robot_symbols(robot_name, [*symbols, symbol])


def remove_robot_symbol(robot_name: str, symbol: str) -> tuple[bool, str]:
    """Убрать монету из списка робота."""
    symbols = get_robot_symbols(robot_name)
    if symbol not in symbols:
        return True, f"{symbol} не в списке робота {robot_name}"
    return set_robot_symbols(robot_name, [s for s in symbols if s != symbol])


def get_robot_status(robot_name: str) -> RobotStatus:
    """Получить статус робота (по всем его монетам)."""
    symbols = get_robot_symbols(robot_name)
    procs = _read_procs(robot_name)
    alive: dict[str, int] = {}
    pruned = False
    for symbol, pid in list(procs.items()):
        if is_running(pid):
            alive[symbol] = pid
        else:
            procs.pop(symbol, None)
            pruned = True
    if pruned:
        _write_procs(robot_name, alive)
    if alive:
        _write_legacy_pid(robot_name, alive)
    else:
        pidfile_for(robot_name).unlink(missing_ok=True)
    return RobotStatus(
        name=robot_name,
        running=bool(alive),
        pid=next(iter(alive.values()), None),
        symbols=symbols,
        symbol_pids=alive,
    )


def get_all_robots_status() -> list[RobotStatus]:
    """Получить статус всех роботов."""
    start_stagger_worker()
    return [get_robot_status(robot) for robot in get_available_robots()]
