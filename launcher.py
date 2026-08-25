"""Лаунчер: повторный запуск — остановит бота, одиночный — запустит.

Использование:
    python launcher.py              # запустить bot_sma (по умолчанию)
    python launcher.py bot_combo    # запустить конкретного бота
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent


def pidfile_for(bot_name: str) -> Path:
    """Путь к pid-файлу конкретного бота."""
    return PROJECT_ROOT / "data" / f"{bot_name}.pid"


def is_running(pid: int) -> bool:
    """Жив ли процесс с таким PID (без фактической отправки сигнала)."""
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def stop(pidfile: Path) -> None:
    """Остановить бота по pid-файлу."""
    if not pidfile.exists():
        print("Бот не запущен.")
        return

    pid = int(pidfile.read_text().strip())
    if is_running(pid):
        print(f"Останавливаю бота (PID {pid})...")
        os.kill(pid, signal.SIGTERM)
        time.sleep(2)
        if is_running(pid):
            # SIGKILL есть только на Unix; на Windows повторяем SIGTERM
            sig = getattr(signal, "SIGKILL", signal.SIGTERM)
            os.kill(pid, sig)
        print("Бот остановлен.")
    else:
        print(f"Процесс {pid} уже не активен.")
    pidfile.unlink(missing_ok=True)


def start(bot_name: str, pidfile: Path) -> None:
    """Запустить бота в фоне и записать его PID."""
    pidfile.parent.mkdir(exist_ok=True)
    proc = subprocess.Popen(
        [sys.executable, str(PROJECT_ROOT / bot_name / "main.py")],
        cwd=str(PROJECT_ROOT),
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    pidfile.write_text(str(proc.pid))
    print(f"Бот {bot_name} запущен (PID {proc.pid}).")


def main() -> None:
    """Точка входа: имя бота — первый аргумент."""
    bot_name = sys.argv[1] if len(sys.argv) > 1 else "bot_sma"
    if not (PROJECT_ROOT / bot_name / "main.py").exists():
        available = sorted(p.name for p in PROJECT_ROOT.glob("bot_*"))
        print(f"Бот '{bot_name}' не найден. Доступны: {', '.join(available)}")
        raise SystemExit(1)

    pidfile = pidfile_for(bot_name)
    if pidfile.exists():
        pid = int(pidfile.read_text().strip())
        if is_running(pid):
            stop(pidfile)
            return
        # PID-файл остался от мёртвого процесса — чистим и стартуем
        pidfile.unlink(missing_ok=True)
    start(bot_name, pidfile)


if __name__ == "__main__":
    main()
