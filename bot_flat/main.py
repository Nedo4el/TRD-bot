"""Точка входа бота Боковик (рип-сайдинг, консолидации).

Запуск:  python bot_flat/main.py
Остановка: python launcher.py bot_flat
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

_BOT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_BOT_DIR.parent))
sys.path.insert(0, str(_BOT_DIR))

from strategy import FlatStrategy

from core.config import Config, load_bot_env
from core.engine import run_bot


async def main() -> None:
    load_bot_env(Path(__file__).parent)
    config = Config()
    strategy = FlatStrategy()
    await run_bot(config, strategy)


if __name__ == "__main__":
    asyncio.run(main())
