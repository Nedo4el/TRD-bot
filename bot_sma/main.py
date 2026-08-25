"""Точка входа бота SMA crossover.

Запуск:  python bot_sma/main.py
Остановка/перезапуск: python launcher.py bot_sma

Все настройки берутся из bot_sma/.env (скопируйте из .env.example).
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

from strategy import SmaCrossStrategy

from core.config import Config, get_env_int, load_bot_env
from core.engine import run_bot


async def main() -> None:
    """Собрать конфиг и стратегию, запустить движок."""
    load_bot_env(Path(__file__).parent)  # читаем bot_sma/.env
    config = Config()

    # --- Тонкая настройка стратегии (числа из .env) ---
    strategy = SmaCrossStrategy(
        fast_period=get_env_int("FAST_MA_PERIOD", 7),
        slow_period=get_env_int("SLOW_MA_PERIOD", 25),
    )

    await run_bot(config, strategy)


if __name__ == "__main__":
    asyncio.run(main())
