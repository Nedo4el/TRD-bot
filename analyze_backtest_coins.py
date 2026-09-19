"""Анализ монет из бэктеста: волатильность, тренд, поведение цены относительно POC."""
from __future__ import annotations

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.stdout.reconfigure(encoding="utf-8")

from pybit.unified_trading import HTTP
from core.bybit_client import Candle
from robot_flat.strategy import FlatStrategy

API_KEY = "kOFDh0YrjjbkiC8VSp"
API_SECRET = "S2f2wUhMMDSqufEcgGzikwyZ1YH7yUsdyf5A"

SYMBOLS = ["AKEUSDT", "BTRUSDT", "BRUSDT", "LSKUSDT", "NILUSDT", "LABUSDT"]
DAYS = 90
TIMEFRAME = "5"
CANDLES_TOTAL = DAYS * 24 * 12  # 25920


def fetch_candles(session: HTTP, symbol: str) -> list[Candle]:
    all_candles: list[Candle] = []
    seen: set[int] = set()
    remaining = CANDLES_TOTAL
    boundary_ms: int | None = None
    while remaining > 0:
        batch = min(remaining, 1000)
        params = {"category": "linear", "symbol": symbol, "interval": TIMEFRAME, "limit": batch}
        if boundary_ms is not None:
            params["end"] = boundary_ms
        resp = session.get_kline(**params)
        klines = resp["result"]["list"]
        if not klines:
            break
        new_count = 0
        oldest_ts = None
        for k in reversed(klines):
            ts = int(k[0])
            if ts not in seen:
                seen.add(ts)
                all_candles.append(Candle(
                    open_time=ts, open=float(k[1]), high=float(k[2]),
                    low=float(k[3]), close=float(k[4]), volume=float(k[5]),
                ))
                new_count += 1
                if oldest_ts is None or ts < oldest_ts:
                    oldest_ts = ts
        if oldest_ts is not None:
            boundary_ms = oldest_ts - int(TIMEFRAME) * 60_000
        remaining -= new_count
        if new_count == 0 or len(klines) < batch:
            break
    all_candles.sort(key=lambda c: c.open_time)
    return all_candles


def calc_atr(candles: list[Candle], period: int = 14) -> list[float]:
    """ATR по массиву свечей."""
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i].high, candles[i].low, candles[i - 1].close
        tr = max(h - l, abs(h - pc), abs(l - pc))
        trs.append(tr)
    atrs = [0.0] * period
    if len(trs) < period:
        return atrs
    atr = sum(trs[:period]) / period
    atrs.append(atr)
    for i in range(period, len(trs)):
        atr = (atr * (period - 1) + trs[i]) / period
        atrs.append(atr)
    return atrs


def analyze(symbol: str, candles: list[Candle]) -> dict:
    closes = [c.close for c in candles]
    highs = [c.high for c in candles]
    lows = [c.low for c in candles]
    volumes = [c.volume for c in candles]

    # 1. Общий диапазон цены
    price_min = min(lows)
    price_max = max(highs)
    total_range_pct = (price_max - price_min) / price_min * 100 if price_min > 0 else 0

    # 2. Trend strength — линейная регрессия по close
    n = len(closes)
    x_mean = (n - 1) / 2
    y_mean = sum(closes) / n
    num = sum((i - x_mean) * (closes[i] - y_mean) for i in range(n))
    den = sum((i - x_mean) ** 2 for i in range(n))
    slope = num / den if den > 0 else 0
    trend_pct = slope * n / y_mean * 100 if y_mean > 0 else 0  # total trend move %

    # 3. ATR
    atrs = calc_atr(candles, 14)
    atr_now = atrs[-1] if atrs else 0
    atr_start = atrs[14] if len(atrs) > 14 else 0
    atr_change = (atr_now - atr_start) / atr_start * 100 if atr_start > 0 else 0
    atr_pct = atr_now / y_mean * 100 if y_mean > 0 else 0

    # 4. POC analysis — сколько времени цена проводит в коридоре ±10%
    poc = FlatStrategy._calc_volume_poc(highs[-600:], lows[-600:], volumes[-600:])
    deviations = [(c - poc) / poc * 100 for c in closes[-600:]]
    in_corridor = sum(1 for d in deviations if abs(d) <= 10) / len(deviations) * 100
    in_stop_zone = sum(1 for d in deviations if abs(d) <= 5) / len(deviations) * 100

    # 5. Сколько раз цена пересекает уровни -6/-8/-10 и +6/+8/+10
    levels_crossed = {l: 0 for l in [-10, -8, -6, 6, 8, 10]}
    for d in deviations:
        for level in levels_crossed:
            if abs(d - level) < 0.5:  #within 0.5% of level
                levels_crossed[level] += 1

    # 6. Частота импульсов (>15% за 5 свечей)
    impulse_count = 0
    for i in range(5, len(candles)):
        seg_h = max(highs[i - 5:i + 1])
        seg_l = min(lows[i - 5:i + 1])
        if seg_l > 0 and (seg_h - seg_l) / seg_l * 100 >= 15:
            impulse_count += 1
    impulse_pct = impulse_count / (len(candles) - 5) * 100

    # 7. Distribution of moves from POC
    abs_devs = [abs(d) for d in deviations]
    avg_dev = sum(abs_devs) / len(abs_devs)
    max_dev = max(abs_devs)
    above_13 = sum(1 for d in abs_devs if d > 13) / len(abs_devs) * 100  # SL zone

    # 8. Volume profile — насколько POC стабилен
    # Считаем POC на разных окнах и смотрим разброс
    poc_values = []
    window = 600
    step = 72  # 6 часов
    for i in range(window, len(candles), step):
        w_candles = candles[i - window:i]
        p = FlatStrategy._calc_volume_poc(
            [c.high for c in w_candles],
            [c.low for c in w_candles],
            [c.volume for c in w_candles],
        )
        poc_values.append(p)
    poc_stability = 0.0
    if len(poc_values) > 1:
        poc_mean = sum(poc_values) / len(poc_values)
        poc_std = math.sqrt(sum((p - poc_mean) ** 2 for p in poc_values) / len(poc_values))
        poc_stability = poc_std / poc_mean * 100 if poc_mean > 0 else 0

    return {
        "symbol": symbol,
        "candles": len(candles),
        "price_range_pct": total_range_pct,
        "trend_pct": trend_pct,
        "atr_pct": atr_pct,
        "atr_change": atr_change,
        "poc": poc,
        "in_corridor_pct": in_corridor,
        "in_stop_zone_pct": in_stop_zone,
        "levels_crossed": levels_crossed,
        "impulse_pct": impulse_pct,
        "avg_deviation": avg_dev,
        "max_deviation": max_dev,
        "above_13pct": above_13,
        "poc_stability": poc_stability,
    }


def main():
    session = HTTP(testnet=False, api_key=API_KEY, api_secret=API_SECRET)

    results = []
    for sym in SYMBOLS:
        print(f"  {sym}...", end=" ", flush=True)
        candles = fetch_candles(session, sym)
        print(f"{len(candles)} candles...", end=" ", flush=True)
        a = analyze(sym, candles)
        results.append(a)
        print("OK")

    # Compare profitable vs unprofitable
    profitable = {"LSKUSDT", "BTRUSDT"}
    losing = {"AKEUSDT", "BRUSDT", "NILUSDT", "LABUSDT"}

    print("\n" + "=" * 100)
    print("  СРАВНИТЕЛЬНЫЙ АНАЛИЗ: ПРИБЫЛЬНЫЕ vs УБЫТОЧНЫЕ")
    print("=" * 100)

    hdr = "  {:<12s} {:>8s} {:>8s} {:>8s} {:>8s} {:>8s} {:>8s} {:>8s} {:>8s} {:>8s}".format(
        "Coin", "Range%", "Trend%", "ATR%", "ATRΔ%", "POCstab", "Corr%", "StopZ%", "Impul%", "AvgDev"
    )
    print(hdr)
    print("  " + "-" * 92)

    for r in results:
        tag = "✓" if r["symbol"] in profitable else "✗"
        print("  {:<12s} {:7.1f}% {:7.1f}% {:7.2f}% {:7.1f}% {:7.2f}% {:7.1f}% {:7.1f}% {:7.2f}% {:7.2f}%  {}".format(
            r["symbol"],
            r["price_range_pct"],
            r["trend_pct"],
            r["atr_pct"],
            r["atr_change"],
            r["poc_stability"],
            r["in_corridor_pct"],
            r["in_stop_zone_pct"],
            r["impulse_pct"],
            r["avg_deviation"],
            tag,
        ))

    print("\n" + "-" * 100)
    print("  УРОВНИ (сколько раз цена была на уровне):")
    print("-" * 100)
    hdr2 = "  {:<12s} {:>8s} {:>8s} {:>8s} {:>8s} {:>8s} {:>8s}".format(
        "Coin", "-10%", "-8%", "-6%", "+6%", "+8%", "+10%"
    )
    print(hdr2)
    print("  " + "-" * 68)
    for r in results:
        lc = r["levels_crossed"]
        print("  {:<12s} {:8d} {:8d} {:8d} {:8d} {:8d} {:8d}".format(
            r["symbol"], lc[-10], lc[-8], lc[-6], lc[6], lc[8], lc[10]
        ))

    print("\n" + "-" * 100)
    print("  ДЕВИАЦИЯ ОТ POC:")
    print("-" * 100)
    for r in results:
        tag = "✓" if r["symbol"] in profitable else "✗"
        print("  {:<12s} avg={:.1f}%  max={:.1f}%  >13%={:.1f}%  POC稳定性={:.2f}%  {}".format(
            r["symbol"], r["avg_deviation"], r["max_deviation"],
            r["above_13pct"], r["poc_stability"], tag
        ))

    # Summary
    print("\n" + "=" * 100)
    print("  ВЫВОДЫ:")
    print("=" * 100)

    prof = [r for r in results if r["symbol"] in profitable]
    lose = [r for r in results if r["symbol"] in losing]

    avg_prof_trend = sum(r["trend_pct"] for r in prof) / len(prof)
    avg_lose_trend = sum(r["trend_pct"] for r in lose) / len(lose)
    avg_prof_atr = sum(r["atr_pct"] for r in prof) / len(prof)
    avg_lose_atr = sum(r["atr_pct"] for r in lose) / len(lose)
    avg_prof_corr = sum(r["in_corridor_pct"] for r in prof) / len(prof)
    avg_lose_corr = sum(r["in_corridor_pct"] for r in lose) / len(lose)
    avg_prof_stab = sum(r["poc_stability"] for r in prof) / len(prof)
    avg_lose_stab = sum(r["poc_stability"] for r in lose) / len(lose)
    avg_prof_dev = sum(r["avg_deviation"] for r in prof) / len(prof)
    avg_lose_dev = sum(r["avg_deviation"] for r in lose) / len(lose)
    avg_prof_imp = sum(r["impulse_pct"] for r in prof) / len(prof)
    avg_lose_imp = sum(r["impulse_pct"] for r in lose) / len(lose)

    print(f"  Прибыльные (LSK, BTR):")
    print(f"    Тренд:      {avg_prof_trend:+.1f}%")
    print(f"    ATR:        {avg_prof_atr:.2f}%")
    print(f"    В коридоре: {avg_prof_corr:.1f}%")
    print(f"    POC стаб.:  {avg_prof_stab:.2f}%")
    print(f"    Ср.откл:    {avg_prof_dev:.1f}%")
    print(f"    Импульсы:   {avg_prof_imp:.2f}%")
    print()
    print(f"  Убыточные (AKE, BR, NIL, LAB):")
    print(f"    Тренд:      {avg_lose_trend:+.1f}%")
    print(f"    ATR:        {avg_lose_atr:.2f}%")
    print(f"    В коридоре: {avg_lose_corr:.1f}%")
    print(f"    POC стаб.:  {avg_lose_stab:.2f}%")
    print(f"    Ср.откл:    {avg_lose_dev:.1f}%")
    print(f"    Импульсы:   {avg_lose_imp:.2f}%")


if __name__ == "__main__":
    main()
