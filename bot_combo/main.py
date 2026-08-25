"""Точка входа бота «Комбинированный фильтр» (EMA + RSI + Bollinger).

Запуск:  python bot_combo/main.py
Остановка/перезапуск: python launcher.py bot_combo

Все настройки берутся из bot_combo/.env.
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

from strategy import ComboFilterStrategy

from core.config import Config, get_env_float, get_env_int, load_bot_env
from core.engine import run_bot


async def main() -> None:
    """Собрать конфиг и стратегию, запустить движок."""
    load_bot_env(Path(__file__).parent)  # читаем bot_combo/.env
    config = Config()

    # --- Тонкая настройка стратегии (числа из .env) ---
    strategy = ComboFilterStrategy(
        ema_fast=get_env_int("EMA_FAST_PERIOD", 21),
        ema_slow=get_env_int("EMA_SLOW_PERIOD", 50),
        rsi_period=get_env_int("RSI_PERIOD", 14),
        rsi_oversold=get_env_float("RSI_OVERSOLD", 30.0),
        rsi_overbought=get_env_float("RSI_OVERBOUGHT", 70.0),
        bb_period=get_env_int("BB_PERIOD", 20),
        bb_deviation=get_env_float("BB_DEVIATION", 2.0),
    )

    await run_bot(config, strategy)


if __name__ == "__main__":
    asyncio.run(main())
