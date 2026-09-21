"""Debug grid_flat strategy."""

import asyncio
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.config import Config, load_bot_env
from core.bybit_client import BybitClient
from robot_TEST.strategy import TestStrategy, TestConfig, _efficiency_ratio, _choppiness_index, _adx

load_bot_env(Path("robot_TEST"))


async def debug():
    config = Config()
    client = BybitClient(config)
    candles = await client.get_klines("NILUSDT", "5", 2000)
    client.close()

    closes = [c.close for c in candles]
    highs = [c.high for c in candles]
    lows = [c.low for c in candles]
    volumes = [c.volume for c in candles]

    min_candles = max(300, 200, 74) + 50
    print(f"Min candles: {min_candles}, Available: {len(candles)}")

    strategy = TestStrategy(TestConfig(poc_lookback=300))

    for i in range(min_candles, len(candles), 100):
        w_closes = closes[:i + 1]
        w_highs = highs[:i + 1]
        w_lows = lows[:i + 1]
        w_vols = volumes[:i + 1]

        e = _efficiency_ratio(w_closes, 10)[-1]
        c = _choppiness_index(w_highs, w_lows, w_closes, 14)[-1]
        a = _adx(w_highs, w_lows, w_closes, 14)[-1]
        vf = sum(w_vols[-20:]) / 20
        vs = sum(w_vols[-100:]) / 100
        v = vf / vs if vs > 0 else 1

        flags = []
        if e < 0.30:
            flags.append("ER")
        if c > 60:
            flags.append("CI")
        if a < 20:
            flags.append("ADX")
        if v < 0.60:
            flags.append("Vol")

        total = len(flags)
        marker = " <<<" if total >= 3 else ""
        print(
            f"  {i:5d}: ER={e:.3f} CI={c:.1f} ADX={a:.1f} Vol={v:.3f}"
            f" [{'+'.join(flags):12s}] {total}/4{marker}"
        )


asyncio.run(debug())
