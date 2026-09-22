"""Robot — Zakol.

Запуск:  py robot_zakol/main.py
Остановка: Ctrl+C
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from core.config import Config, get_env_int, load_bot_env
from core.engine import TradingBot, setup_logging_and_validate
from robot_zakol.strategy import ZakolConfig, ZakolStrategy


async def main() -> None:
    load_bot_env(Path(__file__).parent)

    cfg = ZakolConfig(
        ema_fast=get_env_int("EMA_FAST", 9),
        ema_slow=get_env_int("EMA_SLOW", 21),
        min_candles=get_env_int("MIN_CANDLES", 30),
    )

    config = Config()
    setup_logging_and_validate(config)

    strategy = ZakolStrategy(cfg)
    bot = TradingBot(config, strategy)
    try:
        await bot.run()
    except KeyboardInterrupt:
        pass
    finally:
        bot.client.close()
        await bot.state.save()


if __name__ == "__main__":
    asyncio.run(main())
