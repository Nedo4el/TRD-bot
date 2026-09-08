"""Быстрый тест: ищет свечи > 3% на 10 минут."""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot_screener_impulse.scanner import ImpulseSignal, analyze_symbol
from bot_screener_klin.fetcher import Fetcher
from core.config import load_bot_env

load_bot_env(Path("bot_screener_impulse"))

MIN_RANGE_PCT = 3.0
RUN_SECONDS = 600
TIMEFRAME = "5"
LOOKBACK = 5


async def scan_once(
    fetcher: Fetcher,
    exclude: set[str],
    min_turnover: float,
) -> list[ImpulseSignal]:
    filtered = await fetcher.get_filtered_symbols(min_turnover)
    filtered = [(s, t) for s, t in filtered if s not in exclude]

    results: list[ImpulseSignal] = []

    batch_size = 10
    for i in range(0, len(filtered), batch_size):
        batch = filtered[i : i + batch_size]
        tasks = []
        for sym, turnover in batch:
            tasks.append(_check_one(fetcher, sym, turnover))
        batch_results = await asyncio.gather(*tasks)
        for r in batch_results:
            if r is not None:
                results.append(r)
        await asyncio.sleep(1)

    return results


async def _check_one(
    fetcher: Fetcher, symbol: str, turnover: float,
) -> ImpulseSignal | None:
    candles = await fetcher.get_klines(
        symbol=symbol, interval=TIMEFRAME, limit=LOOKBACK,
    )
    if not candles:
        return None
    return analyze_symbol(
        symbol, f"{TIMEFRAME}m", candles,
        min_range_pct=MIN_RANGE_PCT,
        turnover_24h=turnover,
    )


async def main() -> None:
    import os
    api_key = os.getenv("BYBIT_API_KEY", "")
    api_secret = os.getenv("BYBIT_API_SECRET", "")
    testnet = os.getenv("TESTNET", "true").lower() == "true"

    fetcher = Fetcher(api_key=api_key, api_secret=api_secret, testnet=testnet)

    raw_exclude = os.getenv("EXCLUDE_SYMBOLS", "BTCUSDT,ETHUSDT")
    exclude = {s.strip() for s in raw_exclude.split(",") if s.strip()}
    min_turnover = 500_000.0

    print("")
    print("=" * 90)
    print("  IMPULSE SCREENER: свечи > %.1f%% | TF=%sm | %d сек" % (
        MIN_RANGE_PCT, TIMEFRAME, RUN_SECONDS))
    print("=" * 90)
    print("")

    all_signals: list[ImpulseSignal] = []
    seen: set[str] = set()
    start = time.monotonic()
    scan_num = 0

    while time.monotonic() - start < RUN_SECONDS:
        scan_num += 1
        elapsed = time.monotonic() - start
        remaining = RUN_SECONDS - elapsed

        try:
            signals = await scan_once(fetcher, exclude, min_turnover)
            new = 0
            for s in signals:
                key = s.symbol
                if key not in seen:
                    seen.add(key)
                    all_signals.append(s)
                    new += 1
                    arrow = "UP " if s.direction == "LONG" else "DN "
                    print(
                        "  [%5.0fs] %s %-14s %s %6.2f%%  $%-12.4f turnover=$%.1fM" % (
                            elapsed, arrow, s.symbol, s.direction,
                            s.range_percent, s.price, s.turnover_24h / 1e6,
                        )
                    )

            print(
                "  --- Run #%d: scanned, new=%d, total=%d, remaining %.0fs ---" % (
                    scan_num, new, len(all_signals), remaining,
                )
            )

        except Exception as e:
            print("  ERROR: %s" % e)

        await asyncio.sleep(60)

    # Итоговая таблица
    print("")
    print("=" * 90)
    print("  TOTAL: %d impulses in %ds (%d runs)" % (
        len(all_signals), RUN_SECONDS, scan_num))
    print("=" * 90)

    if all_signals:
        all_signals.sort(key=lambda s: s.range_percent, reverse=True)
        print("")
        print("  #   Монета          Направл     Цена       Свеча%%     Оборот")
        print("  " + "-" * 60)
        for i, s in enumerate(all_signals, 1):
            arrow = "UP " if s.direction == "LONG" else "DN "
            print(
                "  %2d  %-14s  %s %s  $%-10.4f  %5.2f%%  $%.1fM" % (
                    i, s.symbol, arrow, s.direction,
                    s.price, s.range_percent, s.turnover_24h / 1e6,
                )
            )
    else:
        print("\n  No signals found.")

    print("")
    print("=" * 90)


if __name__ == "__main__":
    asyncio.run(main())
