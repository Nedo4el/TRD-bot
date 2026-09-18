"""Быстрый тест robot_flat на альтах — поиск боковиков."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pybit.unified_trading import HTTP

from core.bybit_client import Candle
from robot_flat.strategy import FlatConfig, FlatStrategy

API_KEY = "kOFDh0YrjjbkiC8VSp"
API_SECRET = "S2f2wUhMMDSqufEcgGzikwyZ1YH7yUsdyf5A"

# Исключаем топ-15 (стабильные монеты, не альты)
EXCLUDE = {
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT",
    "ADAUSDT", "SOLUSDT", "DOTUSDT", "MATICUSDT", "LINKUSDT",
    "LTCUSDT", "UNIUSDT", "ATOMUSDT", "NEARUSDT", "AVAXUSDT",
}


async def main() -> None:
    session = HTTP(
        testnet=False,
        api_key=API_KEY,
        api_secret=API_SECRET,
    )

    # Получаем все тикеры
    resp = session.get_tickers(category="linear")
    tickers = resp["result"]["list"]

    # Фильтруем альты: volume > $10M, цена $0.01-$5
    candidates = []
    for t in tickers:
        symbol = t["symbol"]
        if not symbol.endswith("USDT"):
            continue
        if symbol in EXCLUDE:
            continue
        volume_24h = float(t.get("turnover24h", 0))
        last_price = float(t.get("lastPrice", 0))
        if volume_24h > 10_000_000 and 0.01 < last_price < 5:
            candidates.append((symbol, last_price, volume_24h / 1_000_000))

    candidates.sort(key=lambda x: x[2], reverse=True)
    top = candidates[:15]

    print(f"Найдено {len(candidates)} альтов, тестирую топ-15:")
    for s, p, v in top:
        print(f"  {s}: ${p:.4f} | ${v:.1f}M")
    print()

    # Тестируем каждый
    cfg = FlatConfig(poc_lookback=240, impulse_min_pct=15.0)

    results = []
    for symbol, price, vol in top:
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
            current = candles[-1].close
            dev = (current - poc) / poc * 100

            # Определяем зону
            if abs(dev) < 5:
                zone = "СТОП ЗОНА"
            elif abs(dev) < 10:
                zone = "БОКОВИК"
            else:
                zone = "ТРЕНД"

            results.append((symbol, current, poc, dev, zone, signal))

        except Exception as e:
            print(f"{symbol}: ОШИБКА - {e}")

    # Выводим результаты
    print("=" * 80)
    print(f"{'Символ':<12} {'Цена':>10} {'POC':>10} {'Откл':>8} {'Зона':<12} {'Сигнал'}")
    print("=" * 80)

    for symbol, current, poc, dev, zone, signal in sorted(results, key=lambda x: abs(x[3])):
        print(f"{symbol:<12} ${current:>9.4f} ${poc:>9.4f} {dev:>+7.1f}% {zone:<12} {signal.action}")

    # Показываем лучшие кандидатуры на боковик
    flats = [r for r in results if r[4] == "БОКОВИК"]
    if flats:
        print()
        print("ЛУЧШИЕ КАНДИДАТЫ НА БОКОВИК (откл 5-10% от POC):")
        for symbol, current, poc, dev, zone, signal in sorted(flats, key=lambda x: abs(x[3])):
            print(f"  {symbol}: ${current:.4f} | POC=${poc:.4f} | откл={dev:+.1f}%")


if __name__ == "__main__":
    asyncio.run(main())
