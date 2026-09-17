"""Robot — Flat (боковик на основе POC).

Запуск:  py robot_flat/main.py
Остановка: Ctrl+C
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from core.config import Config, get_env_float, get_env_int, load_bot_env
from core.engine import TradingBot, setup_logging_and_validate
from robot_flat.strategy import FlatConfig, FlatStrategy


async def main() -> None:
    load_bot_env(Path(__file__).parent)

    cfg = FlatConfig(
        poc_lookback=get_env_int("POC_LOOKBACK", 60),
        range_pct=get_env_float("RANGE_PCT", 40.0),
        impulse_min_pct=get_env_float("IMPULSE_MIN_PCT", 15.0),
        impulse_window=get_env_int("IMPULSE_WINDOW", 5),
        impulse_cooldown=get_env_int("IMPULSE_COOLDOWN", 30),
    )

    config = Config()
    setup_logging_and_validate(config)

    strategy = FlatStrategy(cfg)
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
