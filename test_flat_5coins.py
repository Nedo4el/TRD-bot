"""Быстрый тест robot_flat на 5 монетах."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pybit.unified_trading import HTTP

from core.bybit_client import Candle
from robot_flat.strategy import FlatConfig, FlatStrategy

SYMBOLS = ["XRPUSDT", "NEARUSDT", "DOGEUSDT", "SUIUSDT", "WLDUSDT"]

API_KEY = "kOFDh0YrjjbkiC8VSp"
API_SECRET = "S2f2wUhMMDSqufEcgGzikwyZ1YH7yUsdyf5A"


async def main() -> None:
    session = HTTP(
        testnet=False,
        api_key=API_KEY,
        api_secret=API_SECRET,
    )

    cfg = FlatConfig(poc_lookback=240, impulse_min_pct=15.0)

    for symbol in SYMBOLS:
        try:
            resp = session.get_kline(
                category="linear", symbol=symbol, interval="1", limit=300,
            )
            klines = resp["result"]["list"]

            candles = []
            for k in reversed(klines):
                candles.append(
                    Candle(
                        open_time=int(k[0]),
                        open=float(k[1]),
                        high=float(k[2]),
                        low=float(k[3]),
                        close=float(k[4]),
                        volume=float(k[5]),
                    )
                )

            strategy = FlatStrategy(cfg)
            signal = strategy.check_signal(candles)
            poc = strategy._fixed_poc
            price = candles[-1].close
            dev = (price - poc) / poc * 100

            print(f"{symbol}:")
            print(f"  Цена: ${price:.4f} | POC: ${poc:.4f} | откл: {dev:+.1f}%")
            print(f"  Сигнал: {signal.action} | {signal.reason}")
            print()

        except Exception as e:
            print(f"{symbol}: ОШИБКА - {e}")
            print()


if __name__ == "__main__":
    asyncio.run(main())
