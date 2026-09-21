"""Robot TEST — Песочница для тестирования стратегий.

Запуск:  py robot_TEST/main.py
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
from robot_TEST.strategy import TestConfig, TestStrategy


async def main() -> None:
    load_bot_env(Path(__file__).parent)

    cfg = TestConfig(
        lookback=get_env_int("LOOKBACK", 100),
    )

    config = Config()
    setup_logging_and_validate(config)

    strategy = TestStrategy(cfg)
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
