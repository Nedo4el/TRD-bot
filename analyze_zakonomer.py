"""Анализ направления цены в пиковые часы (ZAKONOMER).

Для каждого всплеска объема определяет:
- Направление свечи (LONG/SHORT по телу)
- Куда пошла цена после всплеска (следующая свеча)
- Статистика по часам: % LONG vs SHORT
"""
from __future__ import annotations

import asyncio
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bot_screener_zakonomer.fetcher import Fetcher
from bot_screener_zakonomer.scanner import ScanConfig, ScanResult
from core.config import get_env_bool, get_env_float, get_env_int, load_bot_env
from core.logger import setup_logging
from core.metrics import ScreenerMetrics

MSK = timezone(timedelta(hours=3))
_BOT_DIR = Path(__file__).resolve().parent / "bot_screener_zakonomer"


def _load_config() -> dict:
    load_bot_env(_BOT_DIR)
    return {
        "api_key": __import__("os").getenv("BYBIT_API_KEY", ""),
        "api_secret": __import__("os").getenv("BYBIT_API_SECRET", ""),
        "testnet": get_env_bool("TESTNET", True),
        "timeframe": __import__("os").getenv("TIMEFRAME", "1"),
        "min_turnover_24h": get_env_float("MIN_TURNOVER_24H", 30_000_000),
        "lookback_days": get_env_int("LOOKBACK_DAYS", 100),
        "exclude_symbols": [
            s.strip()
            for s in __import__("os").getenv("EXCLUDE_SYMBOLS", "").split(",")
            if s.strip()
        ],
    }


def _candle_direction(c: dict) -> str:
    """Направление свечи по телу."""
    op = float(c["open"])
    cl = float(c["close"])
    return "LONG" if cl > op else "SHORT" if cl < op else "NEUTRAL"


def _body_pct(c: dict) -> float:
    """Процент изменения тела свечи."""
    op = float(c["open"])
    cl = float(c["close"])
    if op <= 0:
        return 0.0
    return abs(cl - op) / op * 100


async def analyze(cfg: dict) -> None:
    setup_logging("logs/screener_zakonomer.log", "INFO")

    fetcher = Fetcher(
        api_key=cfg["api_key"],
        api_secret=cfg["api_secret"],
        testnet=cfg["testnet"],
        metrics=ScreenerMetrics(),
    )

    scan_cfg = ScanConfig(
        lookback_days=cfg["lookback_days"],
        interval=cfg["timeframe"],
        min_volume_usd=get_env_float("MIN_VOLUME_USD", 1_000_000),
        spike_std_multiplier=get_env_float("SPIKE_STD_MULTIPLIER", 3.0),
        min_turnover_24h=cfg["min_turnover_24h"],
        exclude_symbols=cfg["exclude_symbols"],
    )

    print("Получаю список символов...")
    filtered = await fetcher.get_filtered_symbols(cfg["min_turnover_24h"])
    total_symbols = len(await fetcher.get_all_linear_symbols())
    exclude = set(cfg["exclude_symbols"])
    filtered = [(s, t) for s, t in filtered if s not in exclude]

    print(f"Найдено {len(filtered)} символов с оборотом > ${cfg['min_turnover_24h']/1e6:.0f}M")

    now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
    lookback_ms = cfg["lookback_days"] * 24 * 3600 * 1000
    start_ms = now_ms - lookback_ms

    # Сбор данных по всем символам
    # hour -> { "long": N, "short": N, "total": N, "avg_body_pct": float }
    hour_stats: dict[int, dict] = defaultdict(lambda: {"long": 0, "short": 0, "total": 0, "body_pcts": []})
    all_spikes = []

    batch_size = 5
    for i in range(0, len(filtered), batch_size):
        batch = filtered[i : i + batch_size]
        tasks = []
        for sym, _ in batch:
            tasks.append(_fetch_and_analyze(fetcher, sym, cfg["timeframe"], start_ms, now_ms, scan_cfg))

        batch_results = await asyncio.gather(*tasks)

        for spikes in batch_results:
            for sp in spikes:
                hour_stats[sp["hour"]]["total"] += 1
                hour_stats[sp["hour"]]["body_pcts"].append(sp["body_pct"])
                if sp["direction"] == "LONG":
                    hour_stats[sp["hour"]]["long"] += 1
                else:
                    hour_stats[sp["hour"]]["short"] += 1
                all_spikes.append(sp)

        done = min(i + batch_size, len(filtered))
        print(f"Прогресс: {done}/{len(filtered)}")

    # Вывод результатов
    print()
    print("=" * 100)
    print("  ZAKONOMER — НАПРАВЛЕНИЕ ЦЕНЫ В ПИКОВЫЕ ЧАСЫ")
    print(f"  Период: {cfg['lookback_days']} дней | 1m свечи | Всего спайков: {len(all_spikes)}")
    print("=" * 100)
    print()

    if not all_spikes:
        print("  Спайков не найдено.")
        return

    # Общая статистика
    total_long = sum(1 for s in all_spikes if s["direction"] == "LONG")
    total_short = sum(1 for s in all_spikes if s["direction"] == "SHORT")
    total = len(all_spikes)

    print(f"  ОБЩАЯ СТАТИСТИКА:")
    print(f"    LONG:  {total_long:4d} ({total_long/total*100:.1f}%)")
    print(f"    SHORT: {total_short:4d} ({total_short/total*100:.1f}%)")
    print()

    # Таблица по часам
    print("  ПО ЧАСАМ (MSK):")
    print(f"  {'Час':>4} | {'Всего':>6} | {'LONG':>6} | {'SHORT':>6} | {'% LONG':>7} | {'% SHORT':>8} | {'Ср. body%':>10}")
    print(f"  {'─' * 70}")

    sorted_hours = sorted(hour_stats.items(), key=lambda x: x[1]["total"], reverse=True)

    for hour, stats in sorted_hours:
        h_total = stats["total"]
        h_long = stats["long"]
        h_short = stats["short"]
        avg_body = sum(stats["body_pcts"]) / len(stats["body_pcts"]) if stats["body_pcts"] else 0
        pct_long = h_long / h_total * 100 if h_total > 0 else 0
        pct_short = h_short / h_total * 100 if h_total > 0 else 0

        # Определяем доминирующее направление
        dominant = "▲" if h_long > h_short else "▼" if h_short > h_long else "="

        print(f"  {hour:02d}:00 | {h_total:6d} | {h_long:6d} | {h_short:6d} | {pct_long:6.1f}% | {pct_short:7.1f}% | {avg_body:9.2f}% {dominant}")

    print()

    # Топ-5 пиковых часов по количеству спайков
    print("  ТОП-5 ПИКОВЫХ ЧАСОВ:")
    for hour, stats in sorted_hours[:5]:
        h_total = stats["total"]
        h_long = stats["long"]
        h_short = stats["short"]
        pct_long = h_long / h_total * 100 if h_total > 0 else 0
        avg_body = sum(stats["body_pcts"]) / len(stats["body_pcts"]) if stats["body_pcts"] else 0

        direction = "LONG преобладает" if h_long > h_short else "SHORT преобладает" if h_short > h_long else "РАВНОВЕСИЕ"
        print(f"    {hour:02d}:00 MSK — {h_total} спайков, {direction} ({pct_long:.0f}% LONG), ср. body {avg_body:.2f}%")

    print()

    # Корреляция: большой volume spike -> куда идет цена?
    print("  КОРРЕЛЯЦИЯ: РАЗМЕР SPIKE vs НАПРАВЛЕНИЕ")
    # Разбиваем спайки по квартилям объема
    sorted_by_vol = sorted(all_spikes, key=lambda x: x["volume_usdt"])
    q1 = len(sorted_by_vol) // 4
    q2 = len(sorted_by_vol) // 2
    q3 = 3 * len(sorted_by_vol) // 4

    quartiles = [
        ("Q1 (маленький)", sorted_by_vol[:q1]),
        ("Q2 (средний)", sorted_by_vol[q1:q2]),
        ("Q3 (крупный)", sorted_by_vol[q2:q3]),
        ("Q4 (огромный)", sorted_by_vol[q3:]),
    ]

    print(f"  {'Квартиль':>20} | {'Всего':>6} | {'% LONG':>7} | {'Ср. body%':>10}")
    print(f"  {'─' * 60}")

    for label, spikes in quartiles:
        if not spikes:
            continue
        s_total = len(spikes)
        s_long = sum(1 for s in spikes if s["direction"] == "LONG")
        pct_long = s_long / s_total * 100
        avg_body = sum(s["body_pct"] for s in spikes) / s_total
        print(f"  {label:>20} | {s_total:6d} | {pct_long:6.1f}% | {avg_body:9.2f}%")

    print()
    print("=" * 100)


async def _fetch_and_analyze(
    fetcher: Fetcher,
    symbol: str,
    timeframe: str,
    start_ms: int,
    end_ms: int,
    cfg: ScanConfig,
) -> list[dict]:
    """Получить свечи и вернуть спайки с направлением."""
    candles = await fetcher.get_klines_range(
        symbol=symbol,
        interval=timeframe,
        start_ms=start_ms,
        end_ms=end_ms,
        limit=1000,
    )

    if not candles or len(candles) < 100:
        return []

    candles.sort(key=lambda c: c["open_time"])

    # Объем в USDT
    volumes_usdt = []
    for c in candles:
        if c.get("turnover", 0) > 0:
            volumes_usdt.append(c["turnover"])
        else:
            volumes_usdt.append(c["volume"] * c["close"])

    if not volumes_usdt:
        return []

    mean_vol = sum(volumes_usdt) / len(volumes_usdt)
    std_vol = (sum((v - mean_vol) ** 2 for v in volumes_usdt) / len(volumes_usdt)) ** 0.5
    threshold = max(mean_vol + cfg.spike_std_multiplier * std_vol, cfg.min_volume_usd)

    spikes = []
    for i, c in enumerate(candles):
        vol = volumes_usdt[i]
        if vol > threshold:
            dt = datetime.fromtimestamp(c["open_time"] / 1000, tz=MSK)
            direction = _candle_direction(c)
            body = _body_pct(c)

            spikes.append({
                "symbol": symbol,
                "hour": dt.hour,
                "volume_usdt": vol,
                "direction": direction,
                "body_pct": body,
                "price": float(c["close"]),
            })

    return spikes


if __name__ == "__main__":
    asyncio.run(analyze(_load_config()))
