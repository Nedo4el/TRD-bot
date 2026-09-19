"""Grid search: TP% x SL% для максимального winrate + прибыльности."""
from __future__ import annotations

import math
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.stdout.reconfigure(encoding="utf-8")

from pybit.unified_trading import HTTP
from core.bybit_client import Candle
from robot_flat.strategy import FlatStrategy

MSK = timezone(timedelta(hours=3))
API_KEY = "kOFDh0YrjjbkiC8VSp"
API_SECRET = "S2f2wUhMMDSqufEcgGzikwyZ1YH7yUsdyf5A"
REPORT_DIR = Path(r"C:\Users\79095\Desktop\trd\Отчеты скринера")

DAYS = 90
TIMEFRAME = "5"
CANDLES_TOTAL = DAYS * 24 * 12
PAGE_SIZE = 1000
SPREAD_PCT = 0.3
COMMISSION_PCT = 0.04

ALL_SYMBOLS = ["AKEUSDT", "BTRUSDT", "BRUSDT", "LSKUSDT", "NILUSDT", "LABUSDT"]

POC_LOOKBACK = 600
RANGE_PCT = 20.0
ORDER_LEVELS = [-6.0, -8.0, -10.0, 6.0, 8.0, 10.0]
STOP_FROM_BORDER = 3.0
MAX_POSITIONS = 3


def fetch_candles(session: HTTP, symbol: str) -> list[Candle]:
    all_candles: list[Candle] = []
    seen: set[int] = set()
    remaining = CANDLES_TOTAL
    boundary_ms: int | None = None
    while remaining > 0:
        batch = min(remaining, PAGE_SIZE)
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


def _order_qty(level: float) -> float:
    abs_level = abs(level)
    if abs_level <= 6:
        return 20.0
    if abs_level <= 8:
        return 15.0
    return 15.0


def run_backtest(candles, tp_pct, sl_pct):
    if len(candles) < POC_LOOKBACK:
        return 0.0, 0.0, 0, 0, 0

    half_range = RANGE_PCT / 2
    spread_h = SPREAD_PCT / 2

    equity = 100.0
    peak_eq = 100.0
    positions = []
    fl: set[float] = set()
    fs: set[float] = set()
    had_close = False
    trades = 0
    wins = 0

    w = candles[:POC_LOOKBACK]
    poc = FlatStrategy._calc_volume_poc(
        [x.high for x in w], [x.low for x in w], [x.volume for x in w]
    )

    for i in range(POC_LOOKBACK, len(candles)):
        c = candles[i]
        h, lo = c.high, c.low

        if poc <= 0:
            continue

        if had_close and not positions:
            fl.clear()
            fs.clear()
        had_close = False

        closed = []
        for pos in positions:
            side = pos["side"]
            entry = pos["entry"]
            sl = pos["sl"]
            tp = pos["tp"]
            qty = pos["qty"]
            if side == "long":
                ea = entry * (1 + spread_h / 100)
                if lo <= sl:
                    exa = sl * (1 - spread_h / 100)
                    p = (exa - ea) / ea * 100 - SPREAD_PCT - COMMISSION_PCT
                    equity += p * qty / 100
                    trades += 1
                    if p > 0:
                        wins += 1
                    closed.append(pos)
                    had_close = True
                elif h >= tp:
                    exa = tp * (1 - spread_h / 100)
                    p = (exa - ea) / ea * 100 - SPREAD_PCT - COMMISSION_PCT
                    equity += p * qty / 100
                    trades += 1
                    if p > 0:
                        wins += 1
                    closed.append(pos)
                    had_close = True
            else:
                ea = entry * (1 - spread_h / 100)
                if h >= sl:
                    exa = sl * (1 + spread_h / 100)
                    p = (ea - exa) / ea * 100 - SPREAD_PCT - COMMISSION_PCT
                    equity += p * qty / 100
                    trades += 1
                    if p > 0:
                        wins += 1
                    closed.append(pos)
                    had_close = True
                elif lo <= tp:
                    exa = tp * (1 + spread_h / 100)
                    p = (ea - exa) / ea * 100 - SPREAD_PCT - COMMISSION_PCT
                    equity += p * qty / 100
                    trades += 1
                    if p > 0:
                        wins += 1
                    closed.append(pos)
                    had_close = True

        for p in closed:
            positions.remove(p)

        peak_eq = max(peak_eq, equity)
        if had_close:
            continue

        longs = sum(1 for p in positions if p["side"] == "long")
        shorts = sum(1 for p in positions if p["side"] == "short")

        if longs < MAX_POSITIONS:
            for level in sorted(ORDER_LEVELS, reverse=True):
                if level > 0 or level in fl:
                    continue
                op = poc * (1 + level / 100)
                if lo <= op <= h:
                    tp_p = op * (1 + tp_pct / 100)
                    sl_p = poc * (1 - (half_range + sl_pct) / 100)
                    qty = _order_qty(level)
                    positions.append({"side": "long", "entry": op, "sl": sl_p, "tp": tp_p, "qty": qty})
                    fl.add(level)
                    break

        if shorts < MAX_POSITIONS:
            for level in sorted(ORDER_LEVELS):
                if level < 0 or level in fs:
                    continue
                op = poc * (1 + level / 100)
                if h >= op >= lo:
                    tp_p = op * (1 - tp_pct / 100)
                    sl_p = poc * (1 + (half_range + sl_pct) / 100)
                    qty = _order_qty(level)
                    positions.append({"side": "short", "entry": op, "sl": sl_p, "tp": tp_p, "qty": qty})
                    fs.add(level)
                    break

    last_p = candles[-1].close
    for pos in positions:
        side = pos["side"]
        entry = pos["entry"]
        qty = pos["qty"]
        if side == "long":
            ea = entry * (1 + spread_h / 100)
            p = (last_p - ea) / ea * 100 - SPREAD_PCT - COMMISSION_PCT
        else:
            ea = entry * (1 - spread_h / 100)
            p = (ea - last_p) / ea * 100 - SPREAD_PCT - COMMISSION_PCT
        equity += p * qty / 100
        trades += 1
        if p > 0:
            wins += 1

    pnl = equity - 100.0
    wr = wins / trades * 100 if trades else 0
    dd = (peak_eq - equity) / peak_eq * 100 if peak_eq > 0 else 0
    return pnl, wr, trades, wins, dd


def main():
    session = HTTP(testnet=False, api_key=API_KEY, api_secret=API_SECRET)

    print("  Загрузка данных...")
    candles_dict: dict[str, list[Candle]] = {}
    for sym in ALL_SYMBOLS:
        print(f"    {sym}...", end=" ", flush=True)
        candles = fetch_candles(session, sym)
        candles_dict[sym] = candles
        print(f"{len(candles)} candles")

    # Grid search: TP x SL
    tp_values = [0.5, 1.0, 1.5, 2.0, 2.5, 3.0]
    sl_values = [3.0, 5.0, 8.0, 10.0, 13.0]

    print(f"\n  Grid search: {len(tp_values)} TP x {len(sl_values)} SL = {len(tp_values)*len(sl_values)} комбинаций")

    results = []
    for tp in tp_values:
        for sl in sl_values:
            coin_pnls = []
            coin_wrs = []
            coin_dds = []
            for sym, candles in candles_dict.items():
                pnl, wr, trades, wins, dd = run_backtest(candles, tp, sl)
                coin_pnls.append(pnl)
                coin_wrs.append(wr)
                coin_dds.append(dd)

            avg_pnl = sum(coin_pnls) / len(coin_pnls)
            avg_wr = sum(coin_wrs) / len(coin_wrs)
            avg_dd = sum(coin_dds) / len(coin_dds)
            profitable = sum(1 for p in coin_pnls if p > 0)

            # Score: winrate + pnl - penalty for DD
            score = avg_wr + avg_pnl * 2 - avg_dd * 0.5

            results.append({
                "tp": tp, "sl": sl,
                "avg_pnl": avg_pnl, "avg_wr": avg_wr, "avg_dd": avg_dd,
                "profitable": profitable, "score": score,
                "coin_pnls": coin_pnls, "coin_wrs": coin_wrs,
            })

    # Sort by score
    results.sort(key=lambda x: x["score"], reverse=True)

    # Print full grid
    print("\n" + "=" * 100)
    print("  ПОЛНАЯ ТАБЛИЦА TP% x SL%")
    print("=" * 100)
    print(f"  {'TP':>5} {'SL':>5} | {'PnL':>8} {'Win%':>6} {'DD':>7} {'Prof':>5} {'Score':>7} | ", end="")
    for sym in ALL_SYMBOLS:
        print(f"{sym[:6]:>7}", end=" ")
    print()
    print("  " + "-" * 95)

    for r in results:
        marker = " <<<" if r == results[0] else ""
        print(f"  {r['tp']:5.1f} {r['sl']:5.1f} | {r['avg_pnl']:+7.1f}% {r['avg_wr']:5.0f}% {r['avg_dd']:6.1f}% {r['profitable']:4d}/6 {r['score']:7.1f} | ", end="")
        for pnl in r["coin_pnls"]:
            print(f"{pnl:+6.1f}%", end=" ")
        print(marker)

    # Top 5
    print("\n" + "=" * 100)
    print("  ТОП-5 ЛУЧШИХ КОМБИНАЦИЙ")
    print("=" * 100)

    for rank, r in enumerate(results[:5], 1):
        print(f"\n  #{rank} TP={r['tp']}% SL={r['sl']}% → Score={r['score']:.1f}")
        print(f"    Win%: {r['avg_wr']:.0f}% | PnL: {r['avg_pnl']:+.1f}% | MaxDD: {r['avg_dd']:.1f}% | Profitable: {r['profitable']}/6")
        for sym, pnl, wr in zip(ALL_SYMBOLS, r["coin_pnls"], r["coin_wrs"]):
            print(f"    {sym:12s} WR={wr:.0f}% PnL={pnl:+.1f}%")

    # Save
    now_str = datetime.now(tz=MSK).strftime("%Y%m%d_%H%M")
    filepath = REPORT_DIR / f"gridsearch_TP_SL_{now_str}.txt"

    lines = [
        "=" * 100,
        "  GRID SEARCH: TP% x SL% | 90 DAYS | M5 | SPREAD 0.3% | COMMISSION 0.04%",
        "=" * 100,
        "",
        f"  Date: {datetime.now(tz=MSK).strftime('%d.%m.%Y %H:%M MSK')}",
        f"  POC: 600 M5, fixed | Grid: -6/-8/-10 | Sizes: $20/$15/$15 (total $100/side)",
        "",
        "-" * 100,
        "  FULL GRID:",
        "-" * 100,
        f"  {'TP':>5} {'SL':>5} | {'PnL':>8} {'Win%':>6} {'DD':>7} {'Prof':>5} {'Score':>7} | ",
    ]
    for sym in ALL_SYMBOLS:
        lines[-1] += f"{sym[:6]:>7} "
    lines.append("  " + "-" * 95)

    for r in results:
        marker = " <<<" if r == results[0] else ""
        row = f"  {r['tp']:5.1f} {r['sl']:5.1f} | {r['avg_pnl']:+7.1f}% {r['avg_wr']:5.0f}% {r['avg_dd']:6.1f}% {r['profitable']:4d}/6 {r['score']:7.1f} | "
        for pnl in r["coin_pnls"]:
            row += f"{pnl:+6.1f}% "
        lines.append(row + marker)

    lines += ["", "-" * 100, "  TOP-5:", "-" * 100]
    for rank, r in enumerate(results[:5], 1):
        lines.append(f"\n  #{rank} TP={r['tp']}% SL={r['sl']}% → Score={r['score']:.1f}")
        lines.append(f"    Win%: {r['avg_wr']:.0f}% | PnL: {r['avg_pnl']:+.1f}% | MaxDD: {r['avg_dd']:.1f}% | Profitable: {r['profitable']}/6")
        for sym, pnl, wr in zip(ALL_SYMBOLS, r["coin_pnls"], r["coin_wrs"]):
            lines.append(f"    {sym:12s} WR={wr:.0f}% PnL={pnl:+.1f}%")

    lines += ["", "=" * 100]
    filepath.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n  Saved: {filepath}")


if __name__ == "__main__":
    main()
