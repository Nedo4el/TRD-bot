"""Тест robot_flat — POC-based боковик."""
import asyncio
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.config import Config, get_env_float, get_env_int, load_bot_env
from core.bybit_client import BybitClient
from robot_flat.strategy import FlatConfig, FlatStrategy

MSK = timezone(timedelta(hours=3))

async def scan():
    load_bot_env(Path("robot_flat"))

    cfg = FlatConfig(
        poc_lookback=get_env_int("POC_LOOKBACK", 60),
        range_pct=get_env_float("RANGE_PCT", 40.0),
    )

    config = Config()
    client = BybitClient(config)
    strategy = FlatStrategy(cfg)

    symbols = ["LSKUSDT", "ZECUSDT", "HYPEUSDT", "BRUSDT", "AKEUSDT", "BTCUSDT", "ETHUSDT"]

    print(f"POC={cfg.poc_lookback} свечей, диапазон=+/-{cfg.range_pct/2:.0f}%")
    print(f"{'='*90}")
    print()

    for symbol in symbols:
        try:
            candles = await client.get_klines(symbol=symbol, interval="1", limit=200)
            if not candles:
                print(f"{symbol}: нет данных")
                continue

            candles.sort(key=lambda c: c.open_time)
            signal = strategy.check_signal(candles)

            price = candles[-1].close
            dt = datetime.fromtimestamp(candles[-1].open_time / 1000, tz=MSK)

            is_flat = "БОКОВИК" in signal.reason
            status = "[BOCOVIK]" if is_flat else "[TREND]"

            print(f"{symbol} | {dt.strftime('%d.%m %H:%M')} | {price:.4f} | {status}")
            print(f"  {signal.reason}")
            print()

        except Exception as e:
            print(f"{symbol}: ошибка - {e}")
            print()

        await asyncio.sleep(1)

    client.close()

asyncio.run(scan())
