"""Управление роботами: запуск, остановка, статус."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class RobotStatus:
    """Статус одного робота."""

    name: str
    running: bool
    pid: int | None = None
    error: str | None = None


def pidfile_for(robot_name: str) -> Path:
    """Путь к pid-файлу робота."""
    return PROJECT_ROOT / "data" / f"{robot_name}.pid"


def is_running(pid: int) -> bool:
    """Жив ли процесс с таким PID."""
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


_robots_cache: list[str] | None = None


def get_available_robots() -> list[str]:
    """Список всех robot_* папок (список папок не меняется при старт/стоп)."""
    global _robots_cache
    if _robots_cache is None:
        _robots_cache = sorted(p.name for p in PROJECT_ROOT.glob("robot_*"))
    return _robots_cache


def start_robot(robot_name: str) -> tuple[bool, str]:
    """Запустить робота. Возвращает (успех, сообщение)."""
    robot_path = PROJECT_ROOT / robot_name / "main.py"
    if not robot_path.exists():
        return False, f"Робот {robot_name} не найден"

    pidfile = pidfile_for(robot_name)
    pidfile.parent.mkdir(exist_ok=True)

    try:
        proc = subprocess.Popen(
            [sys.executable, str(robot_path)],
            cwd=str(PROJECT_ROOT),
            creationflags=subprocess.CREATE_NO_WINDOW,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        pidfile.write_text(str(proc.pid))
        return True, f"Робот {robot_name} запущен (PID {proc.pid})"
    except (OSError, ValueError) as e:
        return False, f"Ошибка запуска: {e}"


def stop_robot(robot_name: str, timeout: float = 2.0) -> tuple[bool, str]:
    """Остановить робота. Возвращает (успех, сообщение).

    Args:
        robot_name: Имя робота.
        timeout: Время ожидания перед SIGKILL (сек).
    """
    pidfile = pidfile_for(robot_name)
    if not pidfile.exists():
        return False, f"Робот {robot_name} не запущен"

    try:
        pid = int(pidfile.read_text().strip())
    except (ValueError, OSError):
        pidfile.unlink(missing_ok=True)
        return False, "PID-файл повреждён"

    if is_running(pid):
        try:
            os.kill(pid, signal.SIGTERM)
            time.sleep(timeout)
            if is_running(pid):
                sig = getattr(signal, "SIGKILL", signal.SIGTERM)
                os.kill(pid, sig)
            pidfile.unlink(missing_ok=True)
            return True, f"Робот {robot_name} остановлен"
        except OSError as e:
            return False, f"Ошибка остановки: {e}"
    else:
        pidfile.unlink(missing_ok=True)
        return False, f"Процесс {pid} уже не активен"


def stop_all_robots(timeout: float = 2.0) -> dict[str, tuple[bool, str]]:
    """Остановить все запущенные роботы."""
    results = {}
    for robot in get_available_robots():
        results[robot] = stop_robot(robot, timeout=timeout)
    return results


def get_robot_status(robot_name: str) -> RobotStatus:
    """Получить статус робота."""
    pidfile = pidfile_for(robot_name)
    if not pidfile.exists():
        return RobotStatus(name=robot_name, running=False)

    try:
        pid = int(pidfile.read_text().strip())
        if is_running(pid):
            return RobotStatus(name=robot_name, running=True, pid=pid)
        else:
            pidfile.unlink(missing_ok=True)
            return RobotStatus(name=robot_name, running=False)
    except (ValueError, OSError) as e:
        return RobotStatus(name=robot_name, running=False, error=str(e))


def get_all_robots_status() -> list[RobotStatus]:
    """Получить статус всех роботов."""
    return [get_robot_status(robot) for robot in get_available_robots()]
