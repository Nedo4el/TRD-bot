"""Robot — Zero (нулевой).

Запуск:  python robot_0/main.py
Остановка: Ctrl+C
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from core.config import Config, load_bot_env
from core.engine import Engine
from core.logger import setup_logging
from robot_0.strategy import ZeroStrategy


async def main() -> None:
    setup_logging("logs/robot_0.log", "INFO")
    load_bot_env(Path(__file__).parent)

    config = Config()
    config.validate()

    strategy = ZeroStrategy()
    engine = Engine(config, strategy)
    await engine.run()


if __name__ == "__main__":
    asyncio.run(main())
