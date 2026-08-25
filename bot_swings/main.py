"""Точка входа бота «Свинг-уровни» (пробой + ретест).

Запуск:  python bot_swings/main.py
Остановка/перезапуск: python launcher.py bot_swings

Все настройки берутся из bot_swings/.env.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# Позволяем запускать файл напрямую И через python -m:
# добавляем в путь корень проекта (для core/) и папку бота (для strategy)
_BOT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_BOT_DIR.parent))
sys.path.insert(0, str(_BOT_DIR))

from strategy import SwingLevelsStrategy

from core.config import Config, get_env_int, load_bot_env
from core.engine import run_bot


async def main() -> None:
    """Собрать конфиг и стратегию, запустить движок."""
    load_bot_env(Path(__file__).parent)  # читаем bot_swings/.env
    config = Config()

    # --- Тонкая настройка стратегии (числа из .env) ---
    strategy = SwingLevelsStrategy(
        lookback=get_env_int("LOOKBACK_CANDLES", 50),
        wing_size=get_env_int("WING_SIZE", 5),
        max_levels=get_env_int("MAX_LEVELS", 5),
        retest_max_bars=get_env_int("RETEST_MAX_BARS", 20),
    )

    await run_bot(config, strategy)


if __name__ == "__main__":
    asyncio.run(main())
