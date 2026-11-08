"""Запуск веб-дашборда скринеров: python run_dashboard.py."""

from __future__ import annotations

import sys

if sys.platform == "win32":
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")

from core.logger import setup_logging
from dashboard.config import DashboardConfig
from dashboard.server import run


def main() -> None:
    """Настроить логирование и поднять сервер."""
    setup_logging(log_file="logs/dashboard.log")
    run(DashboardConfig.from_env())


if __name__ == "__main__":
    main()
