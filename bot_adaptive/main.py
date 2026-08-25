"""Точка входа бота «Адаптивный» (EMA5/13 + SMA200, стопы от ATR).

Запуск:  python bot_adaptive/main.py
Остановка/перезапуск: python launcher.py bot_adaptive

Все настройки берутся из bot_adaptive/.env.
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

from strategy import AdaptiveAtrStrategy

from core.config import Config, get_env_float, get_env_int, load_bot_env
from core.engine import run_bot


async def main() -> None:
    """Собрать конфиг и стратегию, запустить движок."""
    load_bot_env(Path(__file__).parent)  # читаем bot_adaptive/.env
    config = Config()

    # --- Тонкая настройка стратегии (числа из .env) ---
    strategy = AdaptiveAtrStrategy(
        trend_period=get_env_int("SMA_TREND_PERIOD", 200),
        ema_fast=get_env_int("EMA_FAST_PERIOD", 5),
        ema_slow=get_env_int("EMA_SLOW_PERIOD", 13),
        atr_period=get_env_int("ATR_PERIOD", 14),
        atr_sl_mult=get_env_float("ATR_SL_MULT", 1.5),
        atr_tp_mult=get_env_float("ATR_TP_MULT", 2.5),
        atr_avg_lookback=get_env_int("ATR_AVG_LOOKBACK", 1440),
        min_atr_pct=get_env_float("MIN_ATR_PCT", 0.1),
    )

    await run_bot(config, strategy)


if __name__ == "__main__":
    asyncio.run(main())
