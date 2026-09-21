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

from core.config import Config, get_env_float, get_env_int, get_env_list, load_bot_env
from core.engine import TradingBot, setup_logging_and_validate
from robot_flat.strategy import FlatConfig, FlatStrategy


async def main() -> None:
    load_bot_env(Path(__file__).parent)

    cfg = FlatConfig(
        poc_lookback=get_env_int("POC_LOOKBACK", 600),
        range_pct=get_env_float("RANGE_PCT", 20.0),
        trend_ema_fast=get_env_int("TREND_EMA_FAST", 50),
        trend_ema_slow=get_env_int("TREND_EMA_SLOW", 200),
        trend_threshold=get_env_float("TREND_THRESHOLD", 2.0),
        order_levels=get_env_list("ORDER_LEVELS", [-6.0, -8.0, -10.0]),
        stop_zone_pct=get_env_float("STOP_ZONE_PCT", 5.0),
        stop_from_border_pct=get_env_float("STOP_FROM_BORDER_PCT", 3.0),
        tp_offset_pct=get_env_float("TP_OFFSET_PCT", 1.0),
        max_positions=get_env_int("MAX_POSITIONS", 3),
        partial_close_pct=get_env_float("PARTIAL_CLOSE_PCT", 50.0),
        trailing_after_poc_pct=get_env_float("TRAILING_AFTER_POC_PCT", 2.0),
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
