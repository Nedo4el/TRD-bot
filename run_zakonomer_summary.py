"""ZAKONOMER — итоговый отчёт: суммарные показатели по дням/часам/градации.

Запуск:  python run_zakonomer_summary.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_PROJECT_ROOT))
_BOT_DIR = _PROJECT_ROOT / "bot_screener_zakonomer"
sys.path.insert(0, str(_BOT_DIR))

from bot_screener_zakonomer.main import _load_config, _scan_one
from bot_screener_zakonomer.scanner import ScanConfig, ScanResult, Spike
from core.config import get_env_float, get_env_int, load_bot_env
from core.logger import get_logger, setup_logging
from core.metrics import ScreenerMetrics

logger = get_logger(__name__)

MSK = timezone(timedelta(hours=3))
REPORTS_DIR = Path(r"C:\Users\79095\Desktop\trd\Отчеты скринера")


async def _run() -> None:
    setup_logging("logs/zakonomer_summary.log", "INFO")

    env_cfg, scan_cfg = _load_config()
    metrics = ScreenerMetrics()

    from bot_screener_zakonomer.fetcher import Fetcher

    fetcher = Fetcher(
        api_key=env_cfg["api_key"],
        api_secret=env_cfg["api_secret"],
        testnet=env_cfg["testnet"],
        metrics=metrics,
    )

    logger.info("Получаю список символов...")
    filtered = await fetcher.get_filtered_symbols(env_cfg["min_turnover_24h"])
    total_symbols = len(await fetcher.get_all_linear_symbols())
    exclude = set(env_cfg["exclude_symbols"])
    filtered = [(s, t) for s, t in filtered if s not in exclude]

    logger.info(
        "Найдено %d символов (исключено: %d)",
        len(filtered), len(exclude),
    )

    now_ms = int(time.time() * 1000)
    lookback_ms = env_cfg["lookback_days"] * 24 * 3600 * 1000
    start_ms = now_ms - lookback_ms

    # Собираем данные из hourly_patterns (ВСЕ spikes, не только top-10)
    all_spikes: list[Spike] = []
    results: list[ScanResult] = []

    # Агрегация по часам (из hourly_patterns всех символов)
    agg_hour_counts: Counter[int] = Counter()
    agg_weekday_counts: Counter[str] = Counter()
    agg_date_counts: Counter[str] = Counter()
    total_spikes_all = 0

    batch_size = 5
    for i in range(0, len(filtered), batch_size):
        batch = filtered[i: i + batch_size]
        tasks = [
            _scan_one(fetcher, sym, env_cfg["timeframe"], start_ms, now_ms, scan_cfg)
            for sym, _ in batch
        ]
        batch_results = await asyncio.gather(*tasks)

        for result in batch_results:
            if result and result.spikes_found > 0:
                results.append(result)
                all_spikes.extend(result.top_spikes)

                # Из hourly_patterns восстанавливаем totalCount для каждого часа
                for tp in result.hourly_patterns:
                    agg_hour_counts[tp.hour] += tp.count
                    total_spikes_all += tp.count

                # Из weekday_patterns
                for day_name, cnt in result.weekday_patterns:
                    agg_weekday_counts[day_name] += cnt

        done = min(i + batch_size, len(filtered))
        logger.info("Прогресс: %d/%d", done, len(filtered))

    logger.info("Сканирование завершено. Символов: %d, Всего spikes: %d",
                len(results), total_spikes_all)

    # Агрегация по датам (из top_spikes — лучшее что есть)
    for sp in all_spikes:
        dt = datetime.fromtimestamp(sp.open_time / 1000, tz=MSK)
        agg_date_counts[dt.strftime("%Y-%m-%d")] += 1

    # ===================== ФОРМИРОВАНИЕ ОТЧЁТА =====================
    now = datetime.now(MSK).strftime("%Y-%m-%d_%H-%M")

    total_spikes = total_spikes_all if total_spikes_all else 1

    weekday_order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    weekday_ru = {
        "Monday": "Понедельник", "Tuesday": "Вторник", "Wednesday": "Среда",
        "Thursday": "Четверг", "Friday": "Пятница", "Saturday": "Суббота", "Sunday": "Воскресенье",
    }

    # Градация по часам (percentiles)
    all_hour_vals = [agg_hour_counts.get(h, 0) for h in range(24)]
    sorted_vals = sorted(all_hour_vals)
    p25 = sorted_vals[5] if len(sorted_vals) > 5 else 0
    p50 = sorted_vals[11] if len(sorted_vals) > 11 else 0
    p75 = sorted_vals[17] if len(sorted_vals) > 17 else 0

    h_peak = [h for h in range(24) if all_hour_vals[h] > p75 and all_hour_vals[h] > 0]
    h_active = [h for h in range(24) if p50 < all_hour_vals[h] <= p75]
    h_moderate = [h for h in range(24) if p25 < all_hour_vals[h] <= p50]
    h_quiet = [h for h in range(24) if 0 < all_hour_vals[h] <= p25]
    h_empty = [h for h in range(24) if all_hour_vals[h] == 0]

    # Градация по дням
    day_vals = sorted(agg_date_counts.values())
    dp25 = day_vals[len(day_vals) // 4] if day_vals else 0
    dp50 = day_vals[len(day_vals) // 2] if day_vals else 0
    dp75 = day_vals[3 * len(day_vals) // 4] if day_vals else 0

    d_peak = [d for d, c in agg_date_counts.items() if c > dp75]
    d_active = [d for d, c in agg_date_counts.items() if dp50 < c <= dp75]
    d_moderate = [d for d, c in agg_date_counts.items() if dp25 < c <= dp50]
    d_quiet = [d for d, c in agg_date_counts.items() if c <= dp25 and c > 0]

    # Запись отчёта
    report_path = REPORTS_DIR / f"zakonomer_summary_{now}.txt"
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("=" * 80 + "\n")
        f.write("  ZAKONOMER — СВОДНЫЙ ОТЧЁТ АКТИВНОСТИ ВСПЛЕСКОВ\n")
        f.write(f"  Дата: {now} MSK | Период: {env_cfg['lookback_days']} дней\n")
        f.write(f"  Символов: {len(results)} из {len(filtered)} | Всего spikes: {total_spikes_all}\n")
        f.write("=" * 80 + "\n\n")

        # --- ЧАСЫ ---
        f.write("─" * 80 + "\n")
        f.write("  ПО ЧАСАМ (MSK)\n")
        f.write("─" * 80 + "\n\n")

        max_h = max(agg_hour_counts.values()) if agg_hour_counts else 1
        if max_h == 0:
            max_h = 1

        f.write(f"  {'Час':>5}  {'Бар':<12}  {'Кол-во':>6}  {'Доля':>6}  {'Градация'}\n")
        f.write("  " + "─" * 60 + "\n")

        for h in range(24):
            cnt = agg_hour_counts.get(h, 0)
            pct = cnt / total_spikes * 100 if total_spikes else 0

            if cnt == 0:
                bar = "░░░░░░░░░░"
                label = "Тихо"
            else:
                ratio = cnt / max_h
                if ratio >= 0.75:
                    bar = "██████████"
                    label = "ПИК"
                elif ratio >= 0.50:
                    bar = "▓▓▓▓▓▓░░░░"
                    label = "Активно"
                elif ratio >= 0.25:
                    bar = "▓▓▓▓░░░░░░"
                    label = "Умеренно"
                else:
                    bar = "▓▓░░░░░░░░"
                    label = "Слабо"

            f.write(f"  {h:02d}:00  {bar}  {cnt:5d}   {pct:5.1f}%   {label}\n")

        f.write(f"\n  Итого spikes по часам: {total_spikes}\n\n")

        # Градация часов
        f.write("  Градация часов:\n")
        if h_peak:
            f.write(f"    ПИК:       {', '.join(f'{h:02d}:00' for h in h_peak)}  ({len(h_peak)} ч.)\n")
        if h_active:
            f.write(f"    Активно:   {', '.join(f'{h:02d}:00' for h in h_active)}  ({len(h_active)} ч.)\n")
        if h_moderate:
            f.write(f"    Умеренно:  {', '.join(f'{h:02d}:00' for h in h_moderate)}  ({len(h_moderate)} ч.)\n")
        if h_quiet:
            f.write(f"    Тихо:      {', '.join(f'{h:02d}:00' for h in h_quiet)}  ({len(h_quiet)} ч.)\n")
        if h_empty:
            f.write(f"    Пусто:     {', '.join(f'{h:02d}:00' for h in h_empty)}  ({len(h_empty)} ч.)\n")

        # --- ДНИ НЕДЕЛИ ---
        f.write("\n" + "─" * 80 + "\n")
        f.write("  ПО ДНЯМ НЕДЕЛИ\n")
        f.write("─" * 80 + "\n\n")

        max_wd = max(agg_weekday_counts.values()) if agg_weekday_counts else 1
        if max_wd == 0:
            max_wd = 1

        f.write(f"  {'День':<14}  {'Бар':<16}  {'Кол-во':>6}  {'Доля':>6}\n")
        f.write("  " + "─" * 55 + "\n")

        for d in weekday_order:
            cnt = agg_weekday_counts.get(d, 0)
            pct = cnt / total_spikes * 100 if total_spikes else 0
            bar_len = int(cnt / max_wd * 15)
            bar = "█" * bar_len + "░" * (15 - bar_len)
            f.write(f"  {weekday_ru[d]:<14}  {bar}  {cnt:5d}   {pct:5.1f}%\n")

        # --- ДАТЫ ---
        f.write("\n" + "─" * 80 + "\n")
        f.write("  ПО ДАТАМ\n")
        f.write("─" * 80 + "\n\n")

        sorted_dates = sorted(agg_date_counts.items())
        max_d = max(agg_date_counts.values()) if agg_date_counts else 1
        if max_d == 0:
            max_d = 1

        f.write(f"  {'Дата':<12}  {'Бар':<16}  {'Кол-во':>6}  {'Градация'}\n")
        f.write("  " + "─" * 55 + "\n")

        for date_str, cnt in sorted_dates:
            pct = cnt / total_spikes * 100 if total_spikes else 0
            bar_len = int(cnt / max_d * 15)
            bar = "█" * bar_len + "░" * (15 - bar_len)

            if cnt > dp75:
                label = "ПИК"
            elif cnt > dp50:
                label = "Активно"
            elif cnt > dp25:
                label = "Умеренно"
            else:
                label = "Тихо"

            f.write(f"  {date_str}  {bar}  {cnt:5d}   {label}\n")

        f.write(f"\n  Всего дней с данными: {len(agg_date_counts)}\n\n")

        # Градация дней
        f.write("  Градация дней:\n")
        if d_peak:
            f.write(f"    ПИК:       {', '.join(d_peak)}  ({len(d_peak)} дн.)\n")
        if d_active:
            f.write(f"    Активно:   {', '.join(d_active)}  ({len(d_active)} дн.)\n")
        if d_moderate:
            f.write(f"    Умеренно:  {', '.join(d_moderate)}  ({len(d_moderate)} дн.)\n")
        if d_quiet:
            f.write(f"    Тихо:      {', '.join(d_quiet)}  ({len(d_quiet)} дн.)\n")

        # --- ИТОГО ---
        f.write("\n" + "=" * 80 + "\n")
        f.write("  ИТОГОВАЯ СВОДКА\n")
        f.write("=" * 80 + "\n\n")

        f.write(f"  Период:             {env_cfg['lookback_days']} дней\n")
        f.write(f"  Всего символов:     {len(results)}\n")
        f.write(f"  Всего spikes:       {total_spikes_all}\n")
        f.write(f"  Ср. spikes/день:    {total_spikes_all / max(len(agg_date_counts), 1):.1f}\n\n")

        f.write("  Самые активные часы (ПИК):\n")
        for h in sorted(h_peak, key=lambda x: agg_hour_counts.get(x, 0), reverse=True):
            cnt = agg_hour_counts.get(h, 0)
            pct = cnt / total_spikes * 100 if total_spikes else 0
            f.write(f"    {h:02d}:00 — {cnt} spikes ({pct:.1f}%)\n")

        f.write("\n  Самые активные дни (ПИК):\n")
        for d in sorted(d_peak, key=lambda x: agg_date_counts.get(x, 0), reverse=True)[:5]:
            cnt = agg_date_counts.get(d, 0)
            pct = cnt / total_spikes * 100 if total_spikes else 0
            f.write(f"    {d} — {cnt} spikes ({pct:.1f}%)\n")

        f.write("\n  Рекомендации:\n")
        f.write(f"    — Вход ДО {h_peak[0]:02d}:00 MSK если это пиковый час\n" if h_peak else "")
        f.write(f"    — Избегать {h_empty[0]:02d}:00 MSK — нет активности\n" if h_empty else "")
        f.write("    — Использовать как фильтр со скринерами UZKIY/YROVNI/IMPULSE\n")

        f.write("\n" + "=" * 80 + "\n")

    logger.info("Отчёт: %s", report_path)

    # Консольный вывод краткой сводки
    print(f"\n{'='*60}")
    print(f"  ZAKONOMER — КРАТКАЯ СВОДКА")
    print(f"{'='*60}")
    print(f"  Период: {env_cfg['lookback_days']} дней | Символов: {len(results)} | Spikes: {total_spikes_all}")
    print()
    print("  ЧАСЫ (ПИК):", ", ".join(f"{h:02d}:00" for h in h_peak) if h_peak else "нет данных")
    print("  ЧАСЫ (Тихо):", ", ".join(f"{h:02d}:00" for h in h_empty) if h_empty else "нет")
    print()
    print("  ДНИ (ПИК):", ", ".join(d_peak[:5]) if d_peak else "нет данных")
    print("  ДНИ (Тихо):", ", ".join(d_quiet[:5]) if d_quiet else "нет")
    print(f"\n  Отчёт: {report_path}")
    print(f"{'='*60}\n")


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
