"""Анализ LSKUSDT за период 13.09.26 00:00 - 14.09.26 12:00."""
import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bot_screener_zakonomer.fetcher import Fetcher
from core.config import get_env_bool, get_env_float, load_bot_env
from core.logger import setup_logging
from core.metrics import ScreenerMetrics

MSK = timezone(timedelta(hours=3))
_BOT_DIR = Path(__file__).resolve().parent / "bot_screener_zakonomer"


async def analyze():
    setup_logging("logs/screener_zakonomer.log", "INFO")
    load_bot_env(_BOT_DIR)

    import os
    fetcher = Fetcher(
        api_key=os.getenv("BYBIT_API_KEY", ""),
        api_secret=os.getenv("BYBIT_API_SECRET", ""),
        testnet=get_env_bool("TESTNET", True),
        metrics=ScreenerMetrics(),
    )

    # Период: 13.09.26 00:00 MSK - 14.09.26 12:00 MSK
    start_dt = datetime(2026, 9, 13, 0, 0, 0, tzinfo=MSK)
    end_dt = datetime(2026, 9, 14, 12, 0, 0, tzinfo=MSK)

    start_ms = int(start_dt.timestamp() * 1000)
    end_ms = int(end_dt.timestamp() * 1000)

    print(f"Период: {start_dt.strftime('%d.%m.%Y %H:%M')} - {end_dt.strftime('%d.%m.%Y %H:%M')} MSK")
    print(f"Загружаю данные LSKUSDT (1m)...")

    candles = await fetcher.get_klines_range(
        symbol="LSKUSDT",
        interval="1",
        start_ms=start_ms,
        end_ms=end_ms,
        limit=2000,
    )

    if not candles:
        print("Данные не получены!")
        return

    candles.sort(key=lambda c: c["open_time"])
    print(f"Получено свечей: {len(candles)}")

    # Анализ
    print()
    print("=" * 100)
    print("  АНАЛИЗ LSKUSDT — 13.09.26 00:00 по 14.09.26 12:00 MSK")
    print("=" * 100)
    print()

    # Базовые данные
    opens = [float(c["open"]) for c in candles]
    highs = [float(c["high"]) for c in candles]
    lows = [float(c["low"]) for c in candles]
    closes = [float(c["close"]) for c in candles]
    volumes = [float(c["volume"]) for c in candles]

    # Объем в USDT
    volumes_usdt = []
    for c in candles:
        if c.get("turnover", 0) > 0:
            volumes_usdt.append(c["turnover"])
        else:
            volumes_usdt.append(c["volume"] * c["close"])

    start_price = opens[0]
    end_price = closes[-1]
    high_price = max(highs)
    low_price = min(lows)
    total_change = (end_price - start_price) / start_price * 100

    print(f"  СТАТИСТИКА ЦЕНЫ:")
    print(f"    Открытие:      {start_price:.4f}")
    print(f"    Закрытие:      {end_price:.4f}")
    print(f"    Максимум:      {high_price:.4f}")
    print(f"    Минимум:       {low_price:.4f}")
    print(f"    Изменение:     {total_change:+.2f}%")
    print(f"    Диапазон:      {(high_price - low_price) / start_price * 100:.2f}%")
    print()

    # Поминутный разбор: каждый час
    print("  ПОЧАСОВОЙ РАЗБОР (MSK):")
    print(f"  {'Час':>5} | {'Откр.':>8} | {'Закр.':>8} | {'Макс.':>8} | {'Мин.':>8} | {'Изм.%':>7} | {'Vol USDT':>12} | {'Свечей':>7}")
    print(f"  {'─' * 90}")

    # Группируем по часам
    hourly = {}
    for c in candles:
        dt = datetime.fromtimestamp(c["open_time"] / 1000, tz=MSK)
        hour_key = dt.hour
        if hour_key not in hourly:
            hourly[hour_key] = {"opens": [], "highs": [], "lows": [], "closes": [], "volumes": []}
        hourly[hour_key]["opens"].append(float(c["open"]))
        hourly[hour_key]["highs"].append(float(c["high"]))
        hourly[hour_key]["lows"].append(float(c["low"]))
        hourly[hour_key]["closes"].append(float(c["close"]))
        hourly[hour_key]["volumes"].append(c.get("turnover", float(c["volume"]) * float(c["close"])))

    for hour in sorted(hourly.keys()):
        h = hourly[hour]
        h_open = h["opens"][0]
        h_close = h["closes"][-1]
        h_high = max(h["highs"])
        h_low = min(h["lows"])
        h_change = (h_close - h_open) / h_open * 100 if h_open > 0 else 0
        h_vol = sum(h["volumes"])

        direction = "▲" if h_change > 0 else "▼" if h_change < 0 else "="
        print(f"  {hour:02d}:00 | {h_open:8.4f} | {h_close:8.4f} | {h_high:8.4f} | {h_low:8.4f} | {h_change:+6.2f}% | ${h_vol:>10,.0f} | {len(h['opens']):>5} {direction}")

    print()

    # Крупные свечи (body > 2%)
    print("  КРУПНЫЕ СВЕЧИ (body > 2%):")
    print(f"  {'Время':>12} | {'Направл.':>8} | {'Откр.':>8} | {'Закр.':>8} | {'Body%':>7} | {'Vol USDT':>12}")
    print(f"  {'─' * 75}")

    big_candles = []
    for c in candles:
        op = float(c["open"])
        cl = float(c["close"])
        if op <= 0:
            continue
        body = abs(cl - op) / op * 100
        if body > 2:
            dt = datetime.fromtimestamp(c["open_time"] / 1000, tz=MSK)
            vol = c.get("turnover", float(c["volume"]) * float(c["close"]))
            direction = "LONG" if cl > op else "SHORT"
            big_candles.append((dt, direction, op, cl, body, vol))

    big_candles.sort(key=lambda x: x[4], reverse=True)

    for dt, direction, op, cl, body, vol in big_candles[:20]:
        print(f"  {dt.strftime('%d.%m %H:%M'):>12} | {direction:>8} | {op:8.4f} | {cl:8.4f} | {body:+6.2f}% | ${vol:>10,.0f}")

    print()

    # Volume spikes
    mean_vol = sum(volumes_usdt) / len(volumes_usdt)
    std_vol = (sum((v - mean_vol) ** 2 for v in volumes_usdt) / len(volumes_usdt)) ** 0.5
    threshold = mean_vol + 3 * std_vol

    print(f"  VOLUME SPIKES (> mean+3σ = ${threshold:,.0f}):")
    print(f"  {'Время':>12} | {'Цена':>8} | {'Направл.':>8} | {'Vol USDT':>12} | {'x avg':>6}")
    print(f"  {'─' * 65}")

    spikes = []
    for i, c in enumerate(candles):
        vol = volumes_usdt[i]
        if vol > threshold:
            dt = datetime.fromtimestamp(c["open_time"] / 1000, tz=MSK)
            op = float(c["open"])
            cl = float(c["close"])
            direction = "LONG" if cl > op else "SHORT" if cl < op else "NEUTRAL"
            spikes.append((dt, cl, direction, vol, vol / mean_vol))

    spikes.sort(key=lambda x: x[3], reverse=True)

    for dt, price, direction, vol, x_avg in spikes[:15]:
        print(f"  {dt.strftime('%d.%m %H:%M'):>12} | {price:8.4f} | {direction:>8} | ${vol:>10,.0f} | {x_avg:.1f}x")

    print()
    print("=" * 100)


if __name__ == "__main__":
    asyncio.run(analyze())
