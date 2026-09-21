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

from core.config import Config, get_env_float, get_env_int, load_bot_env
from core.engine import TradingBot, setup_logging_and_validate
from robot_TEST.strategy import TestConfig, TestStrategy


async def main() -> None:
    load_bot_env(Path(__file__).parent)

    cfg = TestConfig(
        poc_lookback=get_env_int("POC_LOOKBACK", 300),
        corridor_pct=get_env_float("CORRIDOR_PCT", 7.5),
        stop_zone_pct=get_env_float("STOP_ZONE_PCT", 3.0),
        grid_levels=get_env_int("GRID_LEVELS", 12),
        er_period=get_env_int("ER_PERIOD", 10),
        er_threshold=get_env_float("ER_THRESHOLD", 0.30),
        ci_period=get_env_int("CI_PERIOD", 14),
        ci_threshold=get_env_float("CI_THRESHOLD", 60.0),
        adx_period=get_env_int("ADX_PERIOD", 14),
        adx_threshold=get_env_float("ADX_THRESHOLD", 20.0),
        vol_avg_fast=get_env_int("VOL_AVG_FAST", 20),
        vol_avg_slow=get_env_int("VOL_AVG_SLOW", 100),
        vol_ratio_threshold=get_env_float("VOL_RATIO_THRESHOLD", 0.60),
        adx_h1_period=get_env_int("ADX_H1_PERIOD", 14),
        adx_h1_threshold=get_env_float("ADX_H1_THRESHOLD", 25.0),
        time_exit_candles=get_env_int("TIME_EXIT_CANDLES", 3),
        atr_period=get_env_int("ATR_PERIOD", 14),
        atr_multiplier=get_env_float("ATR_MULTIPLIER", 2.0),
        atr_avg_period=get_env_int("ATR_AVG_PERIOD", 60),
        timeout_hours=get_env_int("TIMEOUT_HOURS", 24),
        cooldown_minutes=get_env_int("COOLDOWN_MINUTES", 60),
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
