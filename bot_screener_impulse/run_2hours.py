"""Прогон импульс-скринера 2 часа → отчёт на рабочий стол."""

from __future__ import annotations

import asyncio
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

os.chdir(PROJECT_ROOT)

# Загружаем .env скринера
from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / "bot_screener_impulse" / ".env")

from bot_screener_impulse.scanner import analyze_symbol
from bot_screener_klin.fetcher import Fetcher


async def scan_all(
    fetcher: Fetcher,
    min_range_pct: float = 1.0,
    lookback: int = 50,
    min_turnover: float = 500_000,
    max_price: float = 100.0,
    scan_limit: int = 200,
) -> list[dict]:
    """Один прогон: получить свечи и найти импульсы."""
    raw_exclude = os.getenv("EXCLUDE_SYMBOLS", "")
    exclude = {s.strip() for s in raw_exclude.split(",") if s.strip()}

    filtered = await fetcher.get_filtered_symbols(min_turnover)
    filtered = [(s, t) for s, t in filtered if s not in exclude][:scan_limit]

    signals = []
    batch_size = 15
    for i in range(0, len(filtered), batch_size):
        batch = filtered[i : i + batch_size]
        tasks = []
        for sym, turnover in batch:
            tasks.append(_scan_one(fetcher, sym, turnover, lookback, min_range_pct))
        batch_results = await asyncio.gather(*tasks)
        for r in batch_results:
            if r and (max_price <= 0 or r["price"] <= max_price):
                signals.append(r)

    return signals


async def _scan_one(
    fetcher: Fetcher,
    symbol: str,
    turnover: float,
    lookback: int,
    min_range_pct: float,
) -> dict | None:
    """Скан одной монеты."""
    try:
        candles = await fetcher.get_klines(
            symbol=symbol, interval="5", limit=lookback,
        )
        if not candles or len(candles) < 2:
            return None

        sig = analyze_symbol(
            symbol=symbol,
            timeframe="5m",
            candles=candles,
            min_range_pct=min_range_pct,
            turnover_24h=turnover,
            freshness=5,
        )
        if not sig:
            return None

        return {
            "symbol": sig.symbol,
            "price": sig.price,
            "direction": sig.direction,
            "range_percent": sig.range_percent,
            "candles_ago": sig.candles_ago,
            "turnover_24h": sig.turnover_24h,
            "signal_time": sig.signal_time,
        }
    except Exception:
        return None


def format_report(
    all_signals: list[dict],
    start_time: datetime,
    end_time: datetime,
    scans_count: int,
) -> str:
    """Сформировать отчёт."""
    duration = end_time - start_time
    hours = duration.total_seconds() / 3600

    lines = []
    lines.append("=" * 70)
    lines.append("ОТЧЁТ: Импульс-скринер (90 минут)")
    lines.append("=" * 70)
    lines.append(f"Начало:    {start_time.strftime('%Y-%m-%d %H:%M:%S')} UTC")
    lines.append(f"Конец:     {end_time.strftime('%Y-%m-%d %H:%M:%S')} UTC")
    lines.append(f"Длительность: {hours:.1f} ч")
    lines.append(f"Прогонов:  {scans_count}")
    lines.append(f"Всего сигналов: {len(all_signals)}")
    lines.append("")

    if not all_signals:
        lines.append("Сигналов не найдено.")
        return "\n".join(lines)

    # Группировка по монете
    by_symbol: dict[str, list[dict]] = {}
    for s in all_signals:
        by_symbol.setdefault(s["symbol"], []).append(s)

    # Сортировка по кол-ву сигналов
    sorted_symbols = sorted(by_symbol.items(), key=lambda x: len(x[1]), reverse=True)

    lines.append("-" * 70)
    lines.append("СВОДКА ПО МОНЕТАМ")
    lines.append("-" * 70)
    lines.append(f"{'Монета':<16} {'Сигналов':>8} {'Макс %':>8} {'Направление':>12}")
    lines.append("-" * 70)

    for symbol, sigs in sorted_symbols:
        max_pct = max(s["range_percent"] for s in sigs)
        dirs = [s["direction"] for s in sigs]
        long_count = dirs.count("LONG")
        short_count = dirs.count("SHORT")
        dir_str = f"L:{long_count} S:{short_count}" if long_count and short_count else dirs[0]
        lines.append(f"{symbol:<16} {len(sigs):>8} {max_pct:>7.1f}% {dir_str:>12}")

    lines.append("-" * 70)
    lines.append("")

    # Детальный список
    lines.append("-" * 70)
    lines.append("ДЕТАЛЬНЫЙ СПИСОК (все сигналы)")
    lines.append("-" * 70)
    lines.append(
        f"{'Время':>8} {'Монета':<16} {'Цена':>10} {'Направл.':>10} "
        f"{'Свеча%':>8} {'Свежесть':>8} {'Оборот':>12}"
    )
    lines.append("-" * 70)

    for s in sorted(all_signals, key=lambda x: x["signal_time"], reverse=True):
        lines.append(
            f"{s['signal_time']:>8} {s['symbol']:<16} {s['price']:>10.4f} "
            f"{s['direction']:>10} {s['range_percent']:>7.1f}% "
            f"{s['candles_ago']:>5} св. {s['turnover_24h']:>11,.0f}$"
        )

    lines.append("-" * 70)
    lines.append("")
    lines.append(f"Уникальных монет с импульсами: {len(by_symbol)}")
    lines.append(f"Всего импульсов: {len(all_signals)}")
    lines.append("=" * 70)

    return "\n".join(lines)


async def main():
    """Основной цикл: сканировать 2 часа."""
    api_key = os.getenv("BYBIT_API_KEY", "")
    api_secret = os.getenv("BYBIT_API_SECRET", "")
    testnet = os.getenv("TESTNET", "true").lower() == "true"

    fetcher = Fetcher(api_key=api_key, api_secret=api_secret, testnet=testnet)

    min_range_pct = 3.0
    lookback = 50
    min_turnover = 10_000_000
    scan_interval = 15
    max_price = 100.0
    scan_limit = 200

    duration_sec = 90 * 60  # 90 минут
    start_time = datetime.now(timezone.utc)
    end_time_scan = time.time() + duration_sec

    print(f"Старт: {start_time.strftime('%Y-%m-%d %H:%M:%S')} UTC")
    print(f"Длительность: 90 минут")
    print(f"Интервал: {scan_interval} сек")
    print(f"Мин. импульс: {min_range_pct}%")
    print(f"Мин. оборот: ${min_turnover:,.0f}")
    print(f"Монет: до {scan_limit}")
    print()

    all_signals: list[dict] = []
    scans_count = 0
    seen_keys: set[str] = set()

    while time.time() < end_time_scan:
        scans_count += 1
        now = datetime.now(timezone.utc).strftime("%H:%M:%S")
        print(f"[{now}] Прогон #{scans_count}...", end=" ", flush=True)

        try:
            signals = await scan_all(
                fetcher,
                min_range_pct=min_range_pct,
                lookback=lookback,
                min_turnover=min_turnover,
                max_price=max_price,
                scan_limit=scan_limit,
            )

            # Дедупликация: монета + направление + время
            new_count = 0
            for s in signals:
                key = f"{s['symbol']}_{s['direction']}_{s['signal_time']}"
                if key not in seen_keys:
                    seen_keys.add(key)
                    all_signals.append(s)
                    new_count += 1

            print(f"найдено: {len(signals)}, новых: {new_count}, всего: {len(all_signals)}")
        except Exception as e:
            print(f"ошибка: {e}")

        # Пауза до следующего прогона
        remaining = end_time_scan - time.time()
        if remaining <= 0:
            break
        wait = min(scan_interval, remaining)
        print(f"  Пауза {wait:.0f} сек...")
        await asyncio.sleep(wait)

    end_time = datetime.now(timezone.utc)

    # Сохраняем отчёт
    report = format_report(all_signals, start_time, end_time, scans_count)

    desktop = Path.home() / "Desktop"
    filename = f"impulse_report_{start_time.strftime('%Y%m%d_%H%M')}.txt"
    report_path = desktop / filename

    report_path.write_text(report, encoding="utf-8")
    print(f"\nОтчёт сохранён: {report_path}")
    print()
    print(report)


if __name__ == "__main__":
    asyncio.run(main())
