"""Check filters for specific coins and date ranges."""

import asyncio
from datetime import datetime, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.config import Config, load_bot_env
from core.bybit_client import BybitClient, Candle
from robot_TEST.strategy import _efficiency_ratio, _choppiness_index, _adx

load_bot_env(Path("robot_TEST"))

COINS = [
    ("AKEUSDT", "2026-07-13", "2026-09-20"),
    ("BTRUSDT", "2026-08-10", "2026-09-20"),
    ("BRUSDT",  "2026-03-20", "2026-09-20"),
    ("LSKUSDT", "2026-09-10", "2026-09-20"),
    ("NILUSDT", "2026-06-10", "2026-09-20"),
    ("INJUSDT", "2026-06-22", "2026-09-20"),
    ("MITOUSDT", "2026-06-18", "2026-08-31"),
]

# 500 candles per page, fetch all needed
PAGE = 1000


def date_to_ms(d: str) -> int:
    return int(datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)


async def fetch_candles(config: Config, symbol: str, start_ms: int, end_ms: int) -> list[Candle]:
    client = BybitClient(config)
    all_candles = []
    end = end_ms + 86400000
    try:
        while True:
            candles = await client.get_klines(symbol, "5", PAGE, end=end)
            if not candles:
                break
            all_candles = candles + all_candles
            if candles[0].open_time <= start_ms:
                break
            end = candles[0].open_time - 1
            if len(candles) < PAGE:
                break
    finally:
        client.close()

    return [c for c in all_candles if c.open_time >= start_ms and c.open_time <= end_ms]


def check_window(closes, highs, lows, volumes, label):
    er = _efficiency_ratio(closes, 10)
    ci = _choppiness_index(highs, lows, closes, 14)
    adx_m5 = _adx(highs, lows, closes, 14)

    vol_fast = sum(volumes[-20:]) / 20 if len(volumes) >= 20 else 0
    vol_slow = sum(volumes[-100:]) / 100 if len(volumes) >= 100 else 0
    vol_ratio = vol_fast / vol_slow if vol_slow > 0 else 1

    e = er[-1]
    c = ci[-1]
    a = adx_m5[-1]
    v = vol_ratio

    flags = []
    if e < 0.30:
        flags.append("ER")
    if c > 60:
        flags.append("CI")
    if a < 20:
        flags.append("ADX")
    if v < 0.60:
        flags.append("Vol")

    return e, c, a, v, flags


async def main():
    config = Config()

    print("=" * 90)
    print("  FILTER CHECK: specific coins and periods (last 1000 candles = ~3.5 days M5)")
    print("=" * 90)

    for symbol, start_d, end_d in COINS:
        start_ms = date_to_ms(start_d)
        end_ms = date_to_ms(end_d)

        try:
            candles = await fetch_candles(config, symbol, start_ms, end_ms)
        except Exception as ex:
            print(f"\n{symbol}: ERROR {ex}")
            continue

        if len(candles) < 300:
            print(f"\n{symbol}: too few candles ({len(candles)})")
            continue

        closes = [c.close for c in candles]
        highs = [c.high for c in candles]
        lows = [c.low for c in candles]
        volumes = [c.volume for c in candles]

        print(f"\n{'=' * 90}")
        print(f"  {symbol}  |  {start_d} -> {end_d}  |  {len(candles)} candles")
        print(f"{'=' * 90}")

        # Check in 500-candle sliding windows (every 250 candles)
        window = 500
        step = 250
        results = []

        for i in range(0, len(candles) - window + 1, step):
            w_closes = closes[i:i + window]
            w_highs = highs[i:i + window]
            w_lows = lows[i:i + window]
            w_vols = volumes[i:i + window]

            e, c, a, v, flags = check_window(w_closes, w_highs, w_lows, w_vols, "")
            total = len(flags)

            # Get date range for this window
            t_start = candles[i].open_time // 1000
            t_end = candles[min(i + window - 1, len(candles) - 1)].open_time // 1000
            d_start = datetime.fromtimestamp(t_start, tz=timezone.utc).strftime("%Y-%m-%d")
            d_end = datetime.fromtimestamp(t_end, tz=timezone.utc).strftime("%Y-%m-%d")

            results.append((d_start, d_end, e, c, a, v, total, flags))

        # Print results
        for d_start, d_end, e, c, a, v, total, flags in results:
            marker = " <<<" if total >= 3 else ""
            print(f"  {d_start}..{d_end}  ER={e:.3f}  CI={c:.1f}  ADX={a:.1f}  Vol={v:.3f}  [{'+'.join(flags):12s}] {total}/4{marker}")

        # Summary: how many windows pass >= 3 filters
        good = [r for r in results if r[6] >= 3]
        print(f"  --- Summary: {len(good)}/{len(results)} windows pass >= 3/4 filters ---")


asyncio.run(main())
