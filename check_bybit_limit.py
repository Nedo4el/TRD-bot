"""Check how many M5 candles Bybit has for NILUSDT."""

import asyncio
from datetime import datetime, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.config import Config, load_bot_env
from core.bybit_client import BybitClient

load_bot_env(Path("robot_TEST"))


async def check():
    config = Config()
    client = BybitClient(config)

    end_ms = int(datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc).timestamp() * 1000)

    all_candles = []
    end = end_ms

    for page in range(50):
        candles = await client.get_klines("NILUSDT", "5", 1000, end=end)
        if not candles:
            break
        all_candles = candles + all_candles
        end = candles[0].open_time - 1
        if len(candles) < 1000:
            break

    client.close()

    if all_candles:
        d0 = datetime.fromtimestamp(all_candles[0].open_time / 1000, tz=timezone.utc)
        d1 = datetime.fromtimestamp(all_candles[-1].open_time / 1000, tz=timezone.utc)
        print(f"Total: {len(all_candles)} candles")
        d0s = d0.strftime("%Y-%m-%d %H:%M")
        d1s = d1.strftime("%Y-%m-%d %H:%M")
        print(f"Range: {d0s} -> {d1s}")
        print(f"Days: {(d1 - d0).days}")


asyncio.run(check())
