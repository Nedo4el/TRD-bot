"""Точка входа бота Импульс (моментум, быстрые входы).

Запуск:  python bot_impulse/main.py
Остановка: python launcher.py bot_impulse
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

_BOT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_BOT_DIR.parent))
sys.path.insert(0, str(_BOT_DIR))

from strategy import ImpulseStrategy

from core.config import Config, load_bot_env
from core.engine import run_bot


async def main() -> None:
    load_bot_env(Path(__file__).parent)
    config = Config()
    strategy = ImpulseStrategy()
    await run_bot(config, strategy)


if __name__ == "__main__":
    asyncio.run(main())
