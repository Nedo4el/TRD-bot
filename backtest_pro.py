"""Pro backtest: 90d, spread, commission, POC fixed, high WR + profitable."""
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

# ========== PARAMETERS ==========
POC_LOOKBACK = 600
RANGE_PCT = 20.0
ORDER_LEVELS = [-6.0, -8.0, -10.0, 6.0, 8.0, 10.0]
STOP_ZONE_PCT = 5.0
STOP_FROM_BORDER_PCT = 3.0
TP_OFFSET_PCT = 1.0
MAX_POSITIONS = 3
ORDER_SIZES = {-6.0: 20.0, -8.0: 15.0, -10.0: 15.0, 6.0: 20.0, 8.0: 15.0, 10.0: 15.0}


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


def _qty(level: float) -> float:
    return ORDER_SIZES.get(abs(level), 20.0)


def _ema(values: list[float], period: int) -> list[float]:
    result = [values[0]]
    k = 2 / (period + 1)
    for v in values[1:]:
        result.append(v * k + result[-1] * (1 - k))
    return result


def run_backtest(candles):
    if len(candles) < POC_LOOKBACK:
        return None

    half_range = RANGE_PCT / 2
    spread_h = SPREAD_PCT / 100 / 2
    equity = 100.0
    peak_eq = 100.0
    positions = []
    fl: set[float] = set()
    fs: set[float] = set()
    had_close = False
    trades = 0
    wins = 0
    sl_count = 0
    tp_count = 0

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
                ea = entry * (1 + spread_h)
                if lo <= sl:
                    exa = sl * (1 - spread_h)
                    p = (exa - ea) / ea * 100 - SPREAD_PCT - COMMISSION_PCT
                    equity += p * qty / 100
                    trades += 1
                    sl_count += 1
                    if p > 0:
                        wins += 1
                    closed.append(pos)
                    had_close = True
                elif h >= tp:
                    exa = tp * (1 - spread_h)
                    p = (exa - ea) / ea * 100 - SPREAD_PCT - COMMISSION_PCT
                    equity += p * qty / 100
                    trades += 1
                    tp_count += 1
                    if p > 0:
                        wins += 1
                    closed.append(pos)
                    had_close = True
            else:
                ea = entry * (1 - spread_h)
                if h >= sl:
                    exa = sl * (1 + spread_h)
                    p = (ea - exa) / ea * 100 - SPREAD_PCT - COMMISSION_PCT
                    equity += p * qty / 100
                    trades += 1
                    sl_count += 1
                    if p > 0:
                        wins += 1
                    closed.append(pos)
                    had_close = True
                elif lo <= tp:
                    exa = tp * (1 + spread_h)
                    p = (ea - exa) / ea * 100 - SPREAD_PCT - COMMISSION_PCT
                    equity += p * qty / 100
                    trades += 1
                    tp_count += 1
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

        buy_boundary = poc * (1 - half_range / 100)
        sell_boundary = poc * (1 + half_range / 100)

        if longs < MAX_POSITIONS:
            for level in sorted(ORDER_LEVELS, reverse=True):
                if level > 0 or level in fl:
                    continue
                op = poc * (1 + level / 100)
                if lo <= op <= h:
                    tp_level = abs(level) - TP_OFFSET_PCT
                    tp_p = poc * (1 + tp_level / 100)
                    sl_p = buy_boundary * (1 - STOP_ZONE_PCT / 100)
                    positions.append({"side": "long", "entry": op, "sl": sl_p, "tp": tp_p, "qty": _qty(level)})
                    fl.add(level)
                    break

        if shorts < MAX_POSITIONS:
            for level in sorted(ORDER_LEVELS):
                if level < 0 or level in fs:
                    continue
                op = poc * (1 + level / 100)
                if h >= op >= lo:
                    tp_level = level - TP_OFFSET_PCT
                    tp_p = poc * (1 - tp_level / 100)
                    sl_p = sell_boundary * (1 + STOP_ZONE_PCT / 100)
                    positions.append({"side": "short", "entry": op, "sl": sl_p, "tp": tp_p, "qty": _qty(level)})
                    fs.add(level)
                    break

    last_p = candles[-1].close
    for pos in positions:
        side = pos["side"]
        entry = pos["entry"]
        qty = pos["qty"]
        if side == "long":
            ea = entry * (1 + spread_h)
            p = (last_p - ea) / ea * 100 - SPREAD_PCT - COMMISSION_PCT
        else:
            ea = entry * (1 - spread_h)
            p = (ea - last_p) / ea * 100 - SPREAD_PCT - COMMISSION_PCT
        equity += p * qty / 100
        trades += 1
        if p > 0:
            wins += 1

    pnl = equity - 100.0
    wr = wins / trades * 100 if trades else 0
    dd = (peak_eq - equity) / peak_eq * 100 if peak_eq > 0 else 0

    return {
        "pnl": pnl, "wr": wr, "trades": trades, "wins": wins,
        "dd": dd, "sl": sl_count, "tp": tp_count,
    }


async def main():
    session = HTTP(testnet=False, api_key=API_KEY, api_secret=API_SECRET)

    now_str = datetime.now(tz=MSK).strftime("%Y%m%d_%H%M")
    date_str = datetime.now(tz=MSK).strftime("%d.%m.%Y %H:%M MSK")

    results = []
    for sym in ALL_SYMBOLS:
        print(f"  {sym}...", end=" ", flush=True)
        candles = fetch_candles(session, sym)
        print(f"{len(candles)} candles...", end=" ", flush=True)

        r = run_backtest(candles)
        if r is None:
            print("not enough data")
            continue
        r["symbol"] = sym
        r["candles"] = len(candles)
        results.append(r)
        print(f"OK WR={r['wr']:.0f}% PnL={r['pnl']:+.1f}%")

    if not results:
        print("No results")
        return

    half_range = RANGE_PCT / 2
    lines = [
        "=" * 80,
        "  BACKTEST PRO | 90 DAYS | M5 | SPREAD 0.3% | COMMISSION 0.04%",
        "=" * 80,
        "",
        f"  Date: {date_str}",
        "",
        "  Strategy:",
        f"    POC: Volume Profile (100 bins), window={POC_LOOKBACK} M5, fixed",
        f"    Grid: {ORDER_LEVELS}",
        f"    Sizes: $20/$15/$15 per level ($100 total)",
        f"    TP: opposite order - {TP_OFFSET_PCT}% closer to POC",
        f"    SL: boundary + {STOP_FROM_BORDER_PCT}% = ±{half_range + STOP_FROM_BORDER_PCT}%",
        f"    Stop zone: ±{STOP_ZONE_PCT}% | Corridor: ±{half_range}% | Max positions: {MAX_POSITIONS}",
        "",
        "  Costs:",
        f"    Spread: {SPREAD_PCT}% | Commission: {COMMISSION_PCT}%",
        "",
        "-" * 80,
    ]

    hdr = "  {:<14s} {:>5s} {:>5s} {:>5s} {:>4s} {:>4s} {:>7s} {:>5s} {:>6s}".format(
        "Coin", "Cand", "Trds", "Win%", "SL", "TP", "PnL", "PF", "MaxDD"
    )
    lines.append(hdr)
    lines.append("  " + "-" * 60)

    for r in results:
        pf = r["pnl"] / max(1, r["trades"] - r["wins"]) if r["trades"] > r["wins"] else 999.0
        pf_s = "inf" if pf > 100 else f"{pf:.1f}"
        lines.append(
            "  {:<14s} {:5d} {:5d} {:4.0f}% {:4d} {:4d} {:+6.1f}% {:>5s} {:5.1f}%".format(
                r["symbol"], r["candles"], r["trades"], r["wr"],
                r["sl"], r["tp"], r["pnl"], pf_s, r["dd"],
            )
        )

    lines.append("  " + "-" * 60)

    avg_pnl = sum(r["pnl"] for r in results) / len(results)
    avg_wr = sum(r["wr"] for r in results) / len(results)
    avg_dd = sum(r["dd"] for r in results) / len(results)
    profitable = sum(1 for r in results if r["pnl"] > 0)

    lines.append(f"  AVG             {'':5s} {'':5s} {avg_wr:.0f}% {'':4s} {'':4s} {avg_pnl:+6.1f}%        {avg_dd:.1f}%")
    lines.append("")
    lines.append(f"  Profitable: {profitable}/{len(results)} | Avg PnL: {avg_pnl:+.1f}% | Avg Win%: {avg_wr:.0f}%")
    lines.append("=" * 80)

    filepath = REPORT_DIR / f"backtest_90d_PRO_{now_str}.txt"
    filepath.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n  Saved: {filepath}")

    print("\n" + "\n".join(lines))


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
