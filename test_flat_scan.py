"""Тест robot_flat — сканирование боковика."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.config import Config, get_env_float, get_env_int, load_bot_env
from core.bybit_client import BybitClient
from robot_flat.strategy import FlatConfig, FlatStrategy

async def scan():
    load_bot_env(Path("robot_flat"))

    cfg = FlatConfig(
        atr_period=get_env_int("ATR_PERIOD", 14),
        atr_lookback=get_env_int("ATR_LOOKBACK", 60),
        atr_decrease_pct=get_env_float("ATR_DECREASE_PCT", 0.7),
        range_pct=get_env_float("RANGE_PCT", 15.0),
        min_candles=get_env_int("MIN_CANDLES", 100),
        bb_squeeze_threshold=get_env_float("BB_SQUEEZE_THRESHOLD", 30.0),
    )

    config = Config()
    client = BybitClient(config)
    strategy = FlatStrategy(cfg)

    symbol = config.symbol
    timeframe = config.timeframe

    print(f"Символ: {symbol} | TF: {timeframe}m | Параметры: ATR={cfg.atr_period}, lookback={cfg.atr_lookback}, decrease={cfg.atr_decrease_pct}, range={cfg.range_pct}%")
    print(f"{'='*80}")
    print()

    # Сканируем несколько раз с интервалом
    for scan_num in range(1, 6):
        try:
            candles = await client.get_klines(symbol=symbol, interval=timeframe, limit=200)
            if not candles:
                print(f"[{scan_num}] Нет данных")
                continue

            signal = strategy.check_signal(candles)

            price = candles[-1].close
            dt_str = ""
            if hasattr(candles[-1], 'open_time') and candles[-1].open_time:
                from datetime import datetime, timezone, timedelta
                dt = datetime.fromtimestamp(candles[-1].open_time / 1000, tz=timezone(timedelta(hours=3)))
                dt_str = dt.strftime("%d.%m %H:%M")

            is_flat = "БОКОВИК" in signal.reason

            status = "[BOCOVIK]" if is_flat else "[TREND]"
            print(f"[{scan_num}] {dt_str} | {price:.4f} | {status}")
            print(f"    {signal.reason}")
            print()

        except Exception as e:
            print(f"[{scan_num}] Ошибка: {e}")

        if scan_num < 5:
            await asyncio.sleep(30)

    client.close()

asyncio.run(scan())
