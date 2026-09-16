"""Анализ боковика LSKUSDT после импульса."""
import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bot_screener_zakonomer.fetcher import Fetcher
from core.config import get_env_bool, load_bot_env
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

    start_dt = datetime(2026, 9, 13, 7, 0, 0, tzinfo=MSK)
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

    opens = [float(c["open"]) for c in candles]
    highs = [float(c["high"]) for c in candles]
    lows = [float(c["low"]) for c in candles]
    closes = [float(c["close"]) for c in candles]

    print()
    print("=" * 100)
    print("  АНАЛИЗ БОКОВИКА LSKUSDT — 13.09.26 07:00 по 14.09.26 12:00 MSK")
    print("=" * 100)
    print()

    # Общая статистика
    start_price = closes[0]
    end_price = closes[-1]
    high_price = max(highs)
    low_price = min(lows)
    avg_price = sum(closes) / len(closes)

    print(f"  ПАРАМЕТРЫ БОКОВИКА:")
    print(f"    Период:          {len(candles)} свечей ({len(candles)/60:.1f} часов)")
    print(f"    Старт цена:      {start_price:.4f}")
    print(f"    Конец цена:      {end_price:.4f}")
    print(f"    Максимум:        {high_price:.4f}")
    print(f"    Минимум:         {low_price:.4f}")
    print(f"    Средняя цена:    {avg_price:.4f}")
    print(f"    Диапазон:        {(high_price - low_price) / avg_price * 100:.2f}%")
    print(f"    Изменение:       {(end_price - start_price) / start_price * 100:+.2f}%")
    print()

    # ATR (Average True Range) по 14 свечам
    trs = []
    for i in range(1, len(candles)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i-1]),
            abs(lows[i] - closes[i-1])
        )
        trs.append(tr)

    atr_14 = sum(trs[:14]) / 14 if len(trs) >= 14 else sum(trs) / len(trs)
    atr_pct = atr_14 / avg_price * 100

    # Bollinger Bands (20 периодов)
    bb_period = 20
    bb_sma = sum(closes[:bb_period]) / bb_period
    bb_std = (sum((c - bb_sma) ** 2 for c in closes[:bb_period]) / bb_period) ** 0.5
    bb_upper = bb_sma + 2 * bb_std
    bb_lower = bb_sma - 2 * bb_std
    bb_width = (bb_upper - bb_lower) / bb_sma * 100

    # Подсчет свечей в диапазоне
    range_pct = (high_price - low_price) / avg_price * 100
    in_range = sum(1 for c in closes if low_price + (high_price - low_price) * 0.2 <= c <= high_price - (high_price - low_price) * 0.2)

    print(f"  ТЕХНИЧЕСКИЕ ПАРАМЕТРЫ:")
    print(f"    ATR(14):         {atr_14:.4f} ({atr_pct:.2f}%)")
    print(f"    BB Width:        {bb_width:.2f}%")
    print(f"    BB Upper:        {bb_upper:.4f}")
    print(f"    BB Lower:        {bb_lower:.4f}")
    print(f"    Свечей в узкой зоне: {in_range}/{len(candles)} ({in_range/len(candles)*100:.1f}%)")
    print()

    # Поиск фаз боковика
    print("  ФАЗЫ БОКОВИКА:")
    print(f"  {'Фаза':>5} | {'Начало':>12} | {'Конец':>12} | {'Длит.':>6} | {'Цена':>8} | {'Диапазон%':>9} | {'ATR%':>6}")
    print(f"  {'─' * 80}")

    # Разбиваем на сегменты по 30 минут
    seg_len = 30  # минут
    for seg_start in range(0, len(candles), seg_len):
        seg_end = min(seg_start + seg_len, len(candles))
        seg_candles = candles[seg_start:seg_end]

        seg_opens = [float(c["open"]) for c in seg_candles]
        seg_highs = [float(c["high"]) for c in seg_candles]
        seg_lows = [float(c["low"]) for c in seg_candles]
        seg_closes = [float(c["close"]) for c in seg_candles]

        seg_high = max(seg_highs)
        seg_low = min(seg_lows)
        seg_avg = sum(seg_closes) / len(seg_closes)
        seg_range = (seg_high - seg_low) / seg_avg * 100 if seg_avg > 0 else 0

        seg_trs = []
        for i in range(1, len(seg_candles)):
            tr = max(
                seg_highs[i] - seg_lows[i],
                abs(seg_highs[i] - seg_closes[i-1]),
                abs(seg_lows[i] - seg_closes[i-1])
            )
            seg_trs.append(tr)
        seg_atr = sum(seg_trs) / len(seg_trs) if seg_trs else 0
        seg_atr_pct = seg_atr / seg_avg * 100 if seg_avg > 0 else 0

        dt_start = datetime.fromtimestamp(seg_candles[0]["open_time"] / 1000, tz=MSK)
        dt_end = datetime.fromtimestamp(seg_candles[-1]["open_time"] / 1000, tz=MSK)

        print(f"  {seg_start//seg_len + 1:>5} | {dt_start.strftime('%d.%m %H:%M'):>12} | {dt_end.strftime('%d.%m %H:%M'):>12} | {len(seg_candles):>4}m | {seg_avg:8.4f} | {seg_range:8.2f}% | {seg_atr_pct:5.2f}%")

    print()

    # Ключевые параметры для поиска
    print("  ═══════════════════════════════════════════════════════════════")
    print("  ПАРАМЕТРЫ ДЛЯ ПОИСКА ПОХОЖИХ БОКОВИКОВ:")
    print("  ═══════════════════════════════════════════════════════════════")
    print(f"    1. Диапазон цены:      {range_pct:.2f}% (макс.� мин.)/ср.цена")
    print(f"    2. ATR(14):            {atr_pct:.2f}% от средней цены")
    print(f"    3. BB Width:           {bb_width:.2f}%")
    print(f"    4. Свечей в узкой зоне: {in_range/len(candles)*100:.1f}%")
    print(f"    5. Длительность:       {len(candles)/60:.1f} часов")
    print(f"    6. Контекст:           После импульса (пампа)")
    print()
    print("  ФОРМУЛА ПОИСКА:")
    print("    - Цена в пределах ±15% от средней за N свечей")
    print("    - ATR снижается (волатильность падает)")
    print("    - BB сжимается (BB Width < порога)")
    print("    - Объёмы снижаются")
    print("    - После резкого движения (impulse)")
    print("  ═══════════════════════════════════════════════════════════════")
    print()


if __name__ == "__main__":
    asyncio.run(analyze())
