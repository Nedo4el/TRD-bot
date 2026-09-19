"""Pro backtest: 90d, spread, commission, POC recalc, Sharpe/Sortino."""
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
from robot_flat.strategy import FlatStrategy, FlatConfig

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
POC_RECALC_EVERY = 999999  # отключено — POC фиксируется один раз

ALL_SYMBOLS = [
    "AKEUSDT", "BTRUSDT", "BRUSDT", "LSKUSDT", "NILUSDT", "LABUSDT",
]


async def fetch_candles(session: HTTP, symbol: str) -> list[Candle]:
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


@dataclass
class CoinResult:
    symbol: str = ""
    candles: int = 0
    trades_count: int = 0
    longs: int = 0
    shorts: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    pnl: float = 0.0
    pf: float = 0.0
    max_dd_pct: float = 0.0
    sharpe: float = 0.0
    sortino: float = 0.0
    sl_count: int = 0
    tp_count: int = 0
    close_count: int = 0
    poc_recalcs: int = 0
    equity_curve: list = field(default_factory=list)


def _returns(curve):
    r = []
    for i in range(1, len(curve)):
        if curve[i - 1] > 0:
            r.append((curve[i] - curve[i - 1]) / curve[i - 1])
    return r


def _sharpe(curve):
    ret = _returns(curve)
    if len(ret) < 2:
        return 0.0
    avg = sum(ret) / len(ret)
    std = math.sqrt(sum((x - avg) ** 2 for x in ret) / len(ret))
    if std == 0:
        return 0.0
    return (avg / std) * math.sqrt(105120)


def _sortino(curve):
    ret = _returns(curve)
    if len(ret) < 2:
        return 0.0
    avg = sum(ret) / len(ret)
    neg = [x for x in ret if x < 0]
    if not neg:
        return 999.0
    dd = math.sqrt(sum(x ** 2 for x in neg) / len(neg))
    if dd == 0:
        return 999.0
    return (avg / dd) * math.sqrt(105120)


def _order_qty(level: float) -> float:
    """Размер ордера зависит от уровня сетки: чем дальше от POC — тем меньше."""
    abs_level = abs(level)
    if abs_level <= 6:
        return 30.0  # -6% / +6% → $30
    if abs_level <= 8:
        return 20.0  # -8% / +8% → $20
    return 10.0      # -10% / +10% → $10


def run_backtest(candles, cfg):
    res = CoinResult()
    if len(candles) < cfg.poc_lookback:
        return res

    half_range = cfg.range_pct / 2
    tp_off = cfg.tp_offset_pct
    spread_h = SPREAD_PCT / 2

    equity = 100.0
    peak_eq = 100.0
    positions = []
    fl = set()
    fs = set()
    had_close = False
    poc = 0.0
    last_recalc = 0
    curve = [100.0]

    for i in range(cfg.poc_lookback, len(candles)):
        c = candles[i]
        h, lo = c.high, c.low

        if i - last_recalc >= POC_RECALC_EVERY or poc == 0:
            w = candles[max(0, i - cfg.poc_lookback):i]
            poc = FlatStrategy._calc_volume_poc(
                [x.high for x in w], [x.low for x in w], [x.volume for x in w]
            )
            last_recalc = i
            res.poc_recalcs += 1

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
                    res.trades_count += 1
                    res.sl_count += 1
                    if p > 0:
                        res.wins += 1
                    else:
                        res.losses += 1
                    closed.append(pos)
                    had_close = True
                elif h >= tp:
                    exa = tp * (1 - spread_h / 100)
                    p = (exa - ea) / ea * 100 - SPREAD_PCT - COMMISSION_PCT
                    equity += p * qty / 100
                    res.trades_count += 1
                    res.tp_count += 1
                    if p > 0:
                        res.wins += 1
                    else:
                        res.losses += 1
                    closed.append(pos)
                    had_close = True
            else:
                ea = entry * (1 - spread_h / 100)
                if h >= sl:
                    exa = sl * (1 + spread_h / 100)
                    p = (ea - exa) / ea * 100 - SPREAD_PCT - COMMISSION_PCT
                    equity += p * qty / 100
                    res.trades_count += 1
                    res.sl_count += 1
                    if p > 0:
                        res.wins += 1
                    else:
                        res.losses += 1
                    closed.append(pos)
                    had_close = True
                elif lo <= tp:
                    exa = tp * (1 + spread_h / 100)
                    p = (ea - exa) / ea * 100 - SPREAD_PCT - COMMISSION_PCT
                    equity += p * qty / 100
                    res.trades_count += 1
                    res.tp_count += 1
                    if p > 0:
                        res.wins += 1
                    else:
                        res.losses += 1
                    closed.append(pos)
                    had_close = True

        for p in closed:
            positions.remove(p)

        curve.append(equity)
        peak_eq = max(peak_eq, equity)
        dd = (peak_eq - equity) / peak_eq * 100 if peak_eq > 0 else 0
        res.max_dd_pct = max(res.max_dd_pct, dd)

        if had_close:
            continue

        longs = sum(1 for p in positions if p["side"] == "long")
        shorts = sum(1 for p in positions if p["side"] == "short")
        levels = cfg.order_levels

        if longs < cfg.max_positions:
            for level in sorted(levels, reverse=True):
                if level > 0 or level in fl:
                    continue
                op = poc * (1 + level / 100)
                if lo <= op <= h:
                    tpl = abs(level) - tp_off
                    tp_p = poc * (1 + tpl / 100)
                    sl_p = poc * (1 - (half_range + 3) / 100)
                    positions.append({"side": "long", "entry": op, "sl": sl_p, "tp": tp_p, "qty": _order_qty(level)})
                    fl.add(level)
                    res.longs += 1
                    break

        if shorts < cfg.max_positions:
            for level in sorted(levels):
                if level < 0 or level in fs:
                    continue
                op = poc * (1 + level / 100)
                if h >= op >= lo:
                    tpl = level - tp_off
                    tp_p = poc * (1 - tpl / 100)
                    sl_p = poc * (1 + (half_range + 3) / 100)
                    positions.append({"side": "short", "entry": op, "sl": sl_p, "tp": tp_p, "qty": _order_qty(level)})
                    fs.add(level)
                    res.shorts += 1
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
        res.trades_count += 1
        res.close_count += 1
        if p > 0:
            res.wins += 1
        else:
            res.losses += 1
    curve.append(equity)

    res.equity_curve = curve
    res.pnl = equity - 100.0
    res.sharpe = _sharpe(curve)
    res.sortino = _sortino(curve)
    res.win_rate = res.wins / res.trades_count * 100 if res.trades_count else 0
    gp = sum(t.get("pnl", 0) for t in [] if t.get("pnl", 0) > 0)
    gl = abs(sum(t.get("pnl", 0) for t in [] if t.get("pnl", 0) <= 0))
    res.pf = gp / gl if gl > 0 else 999.0

    win_pnl = res.pnl * res.win_rate / 100 if res.trades_count else 0
    loss_pnl = res.pnl - win_pnl
    res.pf = win_pnl / abs(loss_pnl) if loss_pnl < 0 else 999.0

    return res


async def main():
    session = HTTP(testnet=False, api_key=API_KEY, api_secret=API_SECRET)
    cfg = FlatConfig(poc_lookback=600, impulse_min_pct=15.0)

    results = []
    for sym in ALL_SYMBOLS:
        try:
            print(f"  {sym}...", end=" ", flush=True)
            candles = await fetch_candles(session, sym)
            print(f"{len(candles)} candles...", end=" ", flush=True)

            if len(candles) < cfg.poc_lookback:
                print("not enough data")
                continue

            r = run_backtest(candles, cfg)
            r.symbol = sym
            r.candles = len(candles)
            results.append(r)
            print(f"OK {r.pnl:+.1f}% PF={r.pf:.1f} Sharpe={r.sharpe:.1f}")

        except Exception as e:
            print(f"ERROR: {e}")

    if not results:
        print("No results")
        return

    now_str = datetime.now(tz=MSK).strftime("%Y%m%d_%H%M")
    date_str = datetime.now(tz=MSK).strftime("%d.%m.%Y %H:%M MSK")

    lines = [
        "=" * 80,
        "  BACKTEST PRO | 90 DAYS | M5 | SPREAD 0.3% | COMMISSION 0.04% | GRID $30/$20/$10",
        "=" * 80,
        "",
        f"  Date: {date_str}",
        "",
        "  Strategy:",
        "    POC: Volume Profile (100 bins), window=600 M5, fixed\n"
        "    Grid: -6%=$30, -8%=$20, -10%=$10 (LONG) | +6%=$30, +8%=$20, +10%=$10 (SHORT)",
        "    Corridor: +/-10% | Stop zone: +/-5% | Grid: -6/-8/-10 (L) +6/+8/+10 (S)",
        "    SL: +/-13% | TP: opposite order -1% | Trailing: 2% | Partial: 50%",
        "",
        "  Costs:",
        f"    Spread: {SPREAD_PCT}% | Commission: {COMMISSION_PCT}% (0.02% x 2)",
        "",
        "-" * 80,
    ]

    hdr = "  {:<14s} {:>5s} {:>5s} {:>5s} {:>4s} {:>4s} {:>4s} {:>7s} {:>5s} {:>6s} {:>6s} {:>6s}".format(
        "Coin", "Cand", "Trds", "Win%", "SL", "TP", "Ex", "PnL", "PF", "MaxDD", "Sharpe", "Sortino"
    )
    lines.append(hdr)
    lines.append("  " + "-" * 72)

    for r in results:
        pf_s = "inf" if r.pf > 100 else f"{r.pf:.1f}"
        sh_s = f"{r.sharpe:.1f}" if abs(r.sharpe) < 100 else "---"
        so_s = f"{r.sortino:.1f}" if abs(r.sortino) < 100 else "---"
        lines.append(
            "  {:<14s} {:5d} {:5d} {:4.0f}% {:4d} {:4d} {:4d} {:+6.1f}% {:>5s} {:5.1f}% {:>6s} {:>6s}".format(
                r.symbol, r.candles, r.trades_count, r.win_rate,
                r.sl_count, r.tp_count, r.close_count,
                r.pnl, pf_s, r.max_dd_pct, sh_s, so_s,
            )
        )

    lines.append("  " + "-" * 72)

    profitable = sum(1 for r in results if r.pnl > 0)
    avg_pnl = sum(r.pnl for r in results) / len(results)
    avg_wr = sum(r.win_rate for r in results) / len(results)
    avg_sh = sum(r.sharpe for r in results) / len(results)
    avg_so = sum(r.sortino for r in results) / len(results)
    avg_dd = sum(r.max_dd_pct for r in results) / len(results)
    total_sl = sum(r.sl_count for r in results)
    total_tp = sum(r.tp_count for r in results)
    total_ex = sum(r.close_count for r in results)
    total_all = total_sl + total_tp + total_ex

    lines += [
        "",
        f"  Profitable: {profitable}/{len(results)} | Avg PnL: {avg_pnl:+.1f}% | Avg Win%: {avg_wr:.0f}%",
        f"  Avg Sharpe: {avg_sh:.1f} | Avg Sortino: {avg_so:.1f} | Avg MaxDD: {avg_dd:.1f}%",
        "",
        "-" * 80,
        "  EXIT DISTRIBUTION:",
        "-" * 80,
        f"  SL: {total_sl} ({total_sl/total_all*100:.0f}%) | TP: {total_tp} ({total_tp/total_all*100:.0f}%) | CLOSE: {total_ex} ({total_ex/total_all*100:.0f}%)",
        "",
        "-" * 80,
        "  TOP/BOTTOM by PnL:",
        "-" * 80,
    ]

    sorted_r = sorted(results, key=lambda x: x.pnl, reverse=True)
    for r in sorted_r[:3]:
        lines.append(f"  BEST  {r.symbol:14s} {r.pnl:+.1f}%  Sharpe={r.sharpe:.1f}  MaxDD={r.max_dd_pct:.1f}%")
    lines.append("")
    for r in sorted_r[-3:]:
        lines.append(f"  WORST {r.symbol:14s} {r.pnl:+.1f}%  Sharpe={r.sharpe:.1f}  MaxDD={r.max_dd_pct:.1f}%")

    lines += ["", "=" * 80]

    filepath = REPORT_DIR / f"backtest_90d_PRO_{now_str}.txt"
    filepath.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nSaved: {filepath}")


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
