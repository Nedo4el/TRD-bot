"""Grid search: TP/SL ratio для 50% winrate profitability."""
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
    if abs_level <= 5:
        return 25.0
    if abs_level <= 8:
        return 15.0
    return 10.0


def run_backtest(candles, order_levels, range_pct, sl_from_poc, tp_mode, max_positions):
    """
    tp_mode:
      - "opposite": TP = opposite grid level - 1% (original)
      - "fixed_X":  TP = entry ± X% (fixed from entry)
    sl_from_poc: SL = poc ± (range_pct/2 + sl_from_poc)%
    """
    if len(candles) < POC_LOOKBACK:
        return 0.0, 0.0, 0, 0, 0

    half_range = range_pct / 2
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

        if longs < max_positions:
            for level in sorted(order_levels, reverse=True):
                if level > 0 or level in fl:
                    continue
                op = poc * (1 + level / 100)
                if lo <= op <= h:
                    if tp_mode == "opposite":
                        tpl = abs(level) - 1.0
                        tp_p = poc * (1 + tpl / 100)
                    elif tp_mode.startswith("fixed_"):
                        tp_pct = float(tp_mode.split("_")[1])
                        tp_p = op * (1 + tp_pct / 100)
                    else:
                        tp_p = op * 1.05
                    sl_p = poc * (1 - (half_range + sl_from_poc) / 100)
                    qty = _order_qty(level)
                    positions.append({"side": "long", "entry": op, "sl": sl_p, "tp": tp_p, "qty": qty})
                    fl.add(level)
                    break

        if shorts < max_positions:
            for level in sorted(order_levels):
                if level < 0 or level in fs:
                    continue
                op = poc * (1 + level / 100)
                if h >= op >= lo:
                    if tp_mode == "opposite":
                        tpl = level - 1.0
                        tp_p = poc * (1 - tpl / 100)
                    elif tp_mode.startswith("fixed_"):
                        tp_pct = float(tp_mode.split("_")[1])
                        tp_p = op * (1 - tp_pct / 100)
                    else:
                        tp_p = op * 0.95
                    sl_p = poc * (1 + (half_range + sl_from_poc) / 100)
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

    # Конфигурации:.levels, range, sl_from_poc, tp_mode, label
    configs = [
        # Оригинальная (для сравнения)
        ([-6, -8, -10, 6, 8, 10], 20.0, 3.0, "opposite", "ORIG: levels=-6/-8/-10 R=20 SL=13% TP=opposite-1%"),

        # TP > SL: коридор ±15%, SL=±10%, TP=фиксированный
        ([-4, -6, -8, 4, 6, 8], 15.0, 2.0, "fixed_5", "A: levels=-4/-6/-8 R=15 SL=10% TP=5%от входа"),
        ([-4, -6, -8, 4, 6, 8], 15.0, 2.0, "fixed_4", "B: levels=-4/-6/-8 R=15 SL=10% TP=4%от входа"),
        ([-4, -6, -8, 4, 6, 8], 15.0, 2.0, "fixed_3", "C: levels=-4/-6/-8 R=15 SL=10% TP=3%от входа"),

        # Ещё ближе: коридор ±12%, SL=±9%
        ([-3, -5, -7, 3, 5, 7], 12.0, 1.5, "fixed_4", "D: levels=-3/-5/-7 R=12 SL=9% TP=4%от входа"),
        ([-3, -5, -7, 3, 5, 7], 12.0, 1.5, "fixed_5", "E: levels=-3/-5/-7 R=12 SL=9% TP=5%от входа"),
        ([-3, -5, -7, 3, 5, 7], 12.0, 1.5, "fixed_3", "F: levels=-3/-5/-7 R=12 SL=9% TP=3%от входа"),

        # Широкий TP: ±15% коридор, SL=10%, TP=8%
        ([-4, -6, -8, 4, 6, 8], 15.0, 2.0, "fixed_8", "G: levels=-4/-6/-8 R=15 SL=10% TP=8%от входа"),
        ([-4, -6, -8, 4, -6, 8], 15.0, 2.0, "fixed_6", "H: levels=-4/-6/-8 R=15 SL=10% TP=6%от входа"),

        # Асимметрия: TP дальше, SL ближе
        ([-3, -5, -8, 3, 5, 8], 12.0, 1.0, "fixed_5", "I: levels=-3/-5/-8 R=12 SL=8% TP=5%от входа"),
        ([-3, -5, -8, 3, 5, 8], 12.0, 1.0, "fixed_6", "J: levels=-3/-5/-8 R=12 SL=8% TP=6%от входа"),

        # Максимальная асимметрия
        ([-3, -5, 3, 5], 10.0, 1.0, "fixed_5", "K: levels=-3/-5 R=10 SL=7% TP=5%от входа"),
        ([-3, -5, 3, 5], 10.0, 1.0, "fixed_7", "L: levels=-3/-5 R=10 SL=7% TP=7%от входа"),
        ([-3, -5, 3, 5], 10.0, 0.5, "fixed_5", "M: levels=-3/-5 R=10 SL=6% TP=5%от входа"),
        ([-3, -5, 3, 5], 10.0, 0.5, "fixed_6", "N: levels=-3/-5 R=10 SL=6% TP=6%от входа"),

        # Оригинальные уровни -6/-8/-10, но SL ближе
        ([-6, -8, -10, 6, 8, 10], 20.0, 1.0, "fixed_5", "O: levels=-6/-8/-10 R=20 SL=11% TP=5%от входа"),
        ([-6, -8, -10, 6, 8, 10], 20.0, 0.5, "fixed_5", "P: levels=-6/-8/-10 R=20 SL=10.5% TP=5%от входа"),
        ([-6, -8, -10, 6, 8, 10], 20.0, 0.0, "fixed_5", "Q: levels=-6/-8/-10 R=20 SL=10% TP=5%от входа"),
    ]

    print(f"\n  Конфигураций: {len(configs)}")

    results = []
    for levels, rng, sl_fp, tp_mode, label in configs:
        coin_pnls = []
        coin_wrs = []
        coin_dds = []
        for sym, candles in candles_dict.items():
            pnl, wr, trades, wins, dd = run_backtest(
                candles, levels, rng, sl_fp, tp_mode, 3
            )
            coin_pnls.append(pnl)
            coin_wrs.append(wr)
            coin_dds.append(dd)

        avg_pnl = sum(coin_pnls) / len(coin_pnls)
        avg_wr = sum(coin_wrs) / len(coin_wrs)
        avg_dd = sum(coin_dds) / len(coin_dds)
        profitable = sum(1 for p in coin_pnls if p > 0)

        # Score: прибыль важнее winrate
        score = avg_pnl * 3 + avg_wr * 1 - avg_dd * 0.5

        results.append({
            "label": label, "levels": levels, "range": rng, "sl_fp": sl_fp,
            "tp_mode": tp_mode,
            "avg_pnl": avg_pnl, "avg_wr": avg_wr, "avg_dd": avg_dd,
            "profitable": profitable, "score": score,
            "coin_pnls": coin_pnls, "coin_wrs": coin_wrs,
        })

    results.sort(key=lambda x: x["score"], reverse=True)

    # Print
    print("\n" + "=" * 120)
    print("  РЕЗУЛЬТАТЫ: TP > SL (прибыльность при 50% winrate)")
    print("=" * 120)
    print(f"  {'#':>3} {'PnL':>8} {'Win%':>6} {'DD':>7} {'Prof':>5} {'Score':>7}  Label")
    print("  " + "-" * 115)

    for rank, r in enumerate(results, 1):
        marker = " <<<" if r["avg_pnl"] > 0 else ""
        print(f"  {rank:3d} {r['avg_pnl']:+7.1f}% {r['avg_wr']:5.0f}% {r['avg_dd']:6.1f}% {r['profitable']:4d}/6 {r['score']:7.1f}  {r['label']}{marker}")

    # Top 5 details
    print("\n" + "=" * 120)
    print("  ТОП-5:")
    print("=" * 120)

    for rank, r in enumerate(results[:5], 1):
        print(f"\n  #{rank} Score={r['score']:.1f}")
        print(f"    {r['label']}")
        print(f"    PnL: {r['avg_pnl']:+.1f}% | Win%: {r['avg_wr']:.0f}% | MaxDD: {r['avg_dd']:.1f}% | Profitable: {r['profitable']}/6")
        for sym, pnl, wr in zip(ALL_SYMBOLS, r["coin_pnls"], r["coin_wrs"]):
            print(f"    {sym:12s} WR={wr:.0f}% PnL={pnl:+.1f}%")

    # Profitable configs
    profitable = [r for r in results if r["avg_pnl"] > 0]
    if profitable:
        print(f"\n  ПРИБЫЛЬНЫЕ КОНФИГУРАЦИИ: {len(profitable)}")
        for r in profitable:
            print(f"    {r['label']} → PnL={r['avg_pnl']:+.1f}% Win%={r['avg_wr']:.0f}%")
    else:
        print("\n  НЕТ ПРИБЫЛЬНЫХ КОНФИГУРАЦИЙ")

    # Save
    now_str = datetime.now(tz=MSK).strftime("%Y%m%d_%H%M")
    filepath = REPORT_DIR / f"gridsearch_profit50_{now_str}.txt"

    lines = [
        "=" * 120,
        "  GRID SEARCH: прибыльность при 50% winrate",
        "  Ключ: TP > SL по расстоянию от входа",
        "=" * 120,
        "",
        f"  Date: {datetime.now(tz=MSK).strftime('%d.%m.%Y %H:%M MSK')}",
        "",
    ]

    for rank, r in enumerate(results, 1):
        marker = " <<<" if r["avg_pnl"] > 0 else ""
        lines.append(f"  {rank:3d} {r['avg_pnl']:+7.1f}% {r['avg_wr']:5.0f}% {r['avg_dd']:6.1f}% {r['profitable']:4d}/6 {r['score']:7.1f}  {r['label']}{marker}")

    lines += ["", "=" * 120, "  TOP-5:", "=" * 120]
    for rank, r in enumerate(results[:5], 1):
        lines.append(f"\n  #{rank} Score={r['score']:.1f}")
        lines.append(f"    {r['label']}")
        lines.append(f"    PnL: {r['avg_pnl']:+.1f}% | Win%: {r['avg_wr']:.0f}% | MaxDD: {r['avg_dd']:.1f}% | Profitable: {r['profitable']}/6")
        for sym, pnl, wr in zip(ALL_SYMBOLS, r["coin_pnls"], r["coin_wrs"]):
            lines.append(f"    {sym:12s} WR={wr:.0f}% PnL={pnl:+.1f}%")

    lines.append("")
    lines.append("=" * 120)
    filepath.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n  Saved: {filepath}")


if __name__ == "__main__":
    main()
