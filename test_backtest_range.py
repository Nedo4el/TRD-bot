"""Проверка временного диапазона бэктеста."""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pybit.unified_trading import HTTP

MSK = timezone(timedelta(hours=3))
API_KEY = "kOFDh0YrjjbkiC8VSp"
API_SECRET = "S2f2wUhMMDSqufEcgGzikwyZ1YH7yUsdyf5A"

SYMBOLS = ["BRUSDT", "APTUSDT", "SUIUSDT", "WLDUSDT", "ARBUSDT"]


async def main() -> None:
    session = HTTP(testnet=False, api_key=API_KEY, api_secret=API_SECRET)

    for symbol in SYMBOLS:
        resp = session.get_kline(
            category="linear", symbol=symbol, interval="1", limit=2000,
        )
        klines = resp["result"]["list"]

        oldest = datetime.fromtimestamp(int(klines[-1][0]) / 1000, tz=MSK)
        newest = datetime.fromtimestamp(int(klines[0][0]) / 1000, tz=MSK)
        hours_ago = (datetime.now(tz=MSK) - oldest).total_seconds() / 3600

        fmt = "%d.%m %H:%M"
        print(f"{symbol}:")
        print(f"  От: {oldest.strftime(fmt)}")
        print(f"  До: {newest.strftime(fmt)}")
        print(f"  Назад: {hours_ago:.1f} ч ({hours_ago / 24:.1f} дней)")
        print()


if __name__ == "__main__":
    asyncio.run(main())
