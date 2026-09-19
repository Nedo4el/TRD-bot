"""Оптимизатор Flat стратегии: перебор параметров + walk-forward."""
from __future__ import annotations

import itertools
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

ALL_SYMBOLS = [
    "AKEUSDT", "BTRUSDT", "BRUSDT", "LSKUSDT", "NILUSDT", "LABUSDT",
]


# ============================================================
# Fetch candles
# ============================================================

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


# ============================================================
# Backtest engine
# ============================================================

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
    equity_curve: list = field(default_factory=list)


def _returns(curve):
    r = []
    for i in range(1, len(curve)):
        if curve[i - 1] > 0:
            r.append((curve[i] - curve[i - 1]) / curve[i - 1])
    return r


def _sharpe(curve, trades_per_year=105120):
    ret = _returns(curve)
    if len(ret) < 2:
        return 0.0
    avg = sum(ret) / len(ret)
    std = math.sqrt(sum((x - avg) ** 2 for x in ret) / len(ret))
    if std == 0:
        return 0.0
    return (avg / std) * math.sqrt(trades_per_year)


def _calc_poc(highs, lows, volumes, n_buckets=100):
    if not highs or not lows or not volumes:
        return 0.0
    global_low = min(lows)
    global_high = max(highs)
    if global_low == global_high:
        return global_low
    bucket_size = (global_high - global_low) / n_buckets
    profile = [0.0] * n_buckets
    for high, low, vol in zip(highs, lows, volumes):
        if low >= high or vol <= 0:
            continue
        start_bin = max(0, int((low - global_low) / bucket_size))
        end_bin = min(n_buckets - 1, int((high - global_low) / bucket_size))
        n_bins = end_bin - start_bin + 1
        vol_per_bin = vol / n_bins
        for b in range(start_bin, end_bin + 1):
            profile[b] += vol_per_bin
    best_idx = profile.index(max(profile))
    return global_low + (best_idx + 0.5) * bucket_size


@dataclass
class Params:
    poc_lookback: int = 600
    range_pct: float = 20.0
    order_levels: list[float] = field(default_factory=lambda: [-6.0, -8.0, -10.0, 6.0, 8.0, 10.0])
    order_sizes: dict[float, float] = field(default_factory=dict)
    stop_zone_pct: float = 5.0
    stop_from_border_pct: float = 3.0
    tp_offset_pct: float = 1.0
    max_positions: int = 3
    partial_close_pct: float = 50.0
    trailing_after_poc_pct: float = 2.0

    def to_label(self) -> str:
        levels_str = "/".join(f"{l:.0f}" for l in sorted(set(self.order_levels)))
        sizes = [self.order_sizes.get(l, 0) for l in sorted(set(self.order_levels))]
        sizes_str = "/".join(f"{s:.0f}" for s in sizes)
        return (
            f"POC={self.poc_lookback} R={self.range_pct:.0f}% "
            f"L=[{levels_str}] S=[{sizes_str}] "
            f"SL={self.stop_from_border_pct:.0f}% TP={self.tp_offset_pct:.1f}% "
            f"MaxPos={self.max_positions}"
        )


def run_backtest(candles: list[Candle], p: Params) -> CoinResult:
    res = CoinResult()
    if len(candles) < p.poc_lookback:
        return res

    half_range = p.range_pct / 2
    tp_off = p.tp_offset_pct
    spread_h = SPREAD_PCT / 2

    equity = 100.0
    peak_eq = 100.0
    positions = []
    fl = set()
    fs = set()
    had_close = False

    # POC один раз
    w = candles[:p.poc_lookback]
    poc = _calc_poc(
        [c.high for c in w], [c.low for c in w], [c.volume for c in w]
    )

    curve = [100.0]

    for i in range(p.poc_lookback, len(candles)):
        c = candles[i]
        h, lo = c.high, c.low

        if poc <= 0:
            curve.append(equity)
            continue

        if had_close and not positions:
            fl.clear()
            fs.clear()
        had_close = False

        # Закрытие позиций
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
                    p_pnl = (exa - ea) / ea * 100 - SPREAD_PCT - COMMISSION_PCT
                    equity += p_pnl * qty / 100
                    res.trades_count += 1
                    if p_pnl > 0:
                        res.wins += 1
                    else:
                        res.losses += 1
                    closed.append(pos)
                    had_close = True
                elif h >= tp:
                    exa = tp * (1 - spread_h / 100)
                    p_pnl = (exa - ea) / ea * 100 - SPREAD_PCT - COMMISSION_PCT
                    equity += p_pnl * qty / 100
                    res.trades_count += 1
                    if p_pnl > 0:
                        res.wins += 1
                    else:
                        res.losses += 1
                    closed.append(pos)
                    had_close = True
            else:
                ea = entry * (1 - spread_h / 100)
                if h >= sl:
                    exa = sl * (1 + spread_h / 100)
                    p_pnl = (ea - exa) / ea * 100 - SPREAD_PCT - COMMISSION_PCT
                    equity += p_pnl * qty / 100
                    res.trades_count += 1
                    if p_pnl > 0:
                        res.wins += 1
                    else:
                        res.losses += 1
                    closed.append(pos)
                    had_close = True
                elif lo <= tp:
                    exa = tp * (1 + spread_h / 100)
                    p_pnl = (ea - exa) / ea * 100 - SPREAD_PCT - COMMISSION_PCT
                    equity += p_pnl * qty / 100
                    res.trades_count += 1
                    if p_pnl > 0:
                        res.wins += 1
                    else:
                        res.losses += 1
                    closed.append(pos)
                    had_close = True

        for pp in closed:
            positions.remove(pp)

        curve.append(equity)
        peak_eq = max(peak_eq, equity)
        dd = (peak_eq - equity) / peak_eq * 100 if peak_eq > 0 else 0
        res.max_dd_pct = max(res.max_dd_pct, dd)

        if had_close:
            continue

        longs = sum(1 for pp in positions if pp["side"] == "long")
        shorts = sum(1 for pp in positions if pp["side"] == "short")
        levels = p.order_levels

        # LONG
        if longs < p.max_positions:
            for level in sorted(levels, reverse=True):
                if level > 0 or level in fl:
                    continue
                op = poc * (1 + level / 100)
                if lo <= op <= h:
                    tpl = abs(level) - tp_off
                    tp_p = poc * (1 + tpl / 100)
                    sl_p = poc * (1 - (half_range + p.stop_from_border_pct) / 100)
                    qty = p.order_sizes.get(level, 10.0)
                    positions.append({"side": "long", "entry": op, "sl": sl_p, "tp": tp_p, "qty": qty})
                    fl.add(level)
                    res.longs += 1
                    break

        # SHORT
        if shorts < p.max_positions:
            for level in sorted(levels):
                if level < 0 or level in fs:
                    continue
                op = poc * (1 + level / 100)
                if h >= op >= lo:
                    tpl = level - tp_off
                    tp_p = poc * (1 - tpl / 100)
                    sl_p = poc * (1 + (half_range + p.stop_from_border_pct) / 100)
                    qty = p.order_sizes.get(level, 10.0)
                    positions.append({"side": "short", "entry": op, "sl": sl_p, "tp": tp_p, "qty": qty})
                    fs.add(level)
                    res.shorts += 1
                    break

    # Закрытие оставшихся позиций
    last_p = candles[-1].close
    for pos in positions:
        side = pos["side"]
        entry = pos["entry"]
        qty = pos["qty"]
        if side == "long":
            ea = entry * (1 + spread_h / 100)
            p_pnl = (last_p - ea) / ea * 100 - SPREAD_PCT - COMMISSION_PCT
        else:
            ea = entry * (1 - spread_h / 100)
            p_pnl = (ea - last_p) / ea * 100 - SPREAD_PCT - COMMISSION_PCT
        equity += p_pnl * qty / 100
        res.trades_count += 1
        if p_pnl > 0:
            res.wins += 1
        else:
            res.losses += 1
    curve.append(equity)

    res.equity_curve = curve
    res.pnl = equity - 100.0
    res.sharpe = _sharpe(curve)
    res.win_rate = res.wins / res.trades_count * 100 if res.trades_count else 0
    win_pnl = res.pnl * res.win_rate / 100 if res.trades_count else 0
    loss_pnl = res.pnl - win_pnl
    res.pf = win_pnl / abs(loss_pnl) if loss_pnl < 0 else 999.0

    return res


# ============================================================
# Параметры для перебора
# ============================================================

def generate_variants() -> list[Params]:
    """20-30 вариантов параметров."""
    variants = []

    # Вариант 1: Базовый (как сейчас)
    variants.append(Params(
        poc_lookback=600, range_pct=20.0,
        order_levels=[-6, -8, -10, 6, 8, 10],
        order_sizes={-6: 30, -8: 20, -10: 10, 6: 30, 8: 20, 10: 10},
        stop_from_border_pct=3, tp_offset_pct=1.0, max_positions=3,
    ))

    # Вариант 2: Все равные $16.6
    variants.append(Params(
        poc_lookback=600, range_pct=20.0,
        order_levels=[-6, -8, -10, 6, 8, 10],
        order_sizes={-6: 17, -8: 17, -10: 16, 6: 17, 8: 17, 10: 16},
        stop_from_border_pct=3, tp_offset_pct=1.0, max_positions=3,
    ))

    # Вариант 3: Тяжёлые глубокие
    variants.append(Params(
        poc_lookback=600, range_pct=20.0,
        order_levels=[-6, -8, -10, 6, 8, 10],
        order_sizes={-6: 10, -8: 20, -10: 30, 6: 10, 8: 20, 10: 30},
        stop_from_border_pct=3, tp_offset_pct=1.0, max_positions=3,
    ))

    # Вариант 4: Узкий коридор ±15%, SL ближе
    variants.append(Params(
        poc_lookback=600, range_pct=15.0,
        order_levels=[-5, -7, -10, 5, 7, 10],
        order_sizes={-5: 25, -7: 25, -10: 25, 5: 25, 7: 25, 10: 25},
        stop_from_border_pct=2, tp_offset_pct=1.0, max_positions=3,
    ))

    # Вариант 5: Широкий коридор ±25%
    variants.append(Params(
        poc_lookback=600, range_pct=25.0,
        order_levels=[-7, -10, -13, 7, 10, 13],
        order_sizes={-7: 20, -10: 30, -13: 30, 7: 20, 10: 30, 13: 30},
        stop_from_border_pct=4, tp_offset_pct=1.5, max_positions=3,
    ))

    # Вариант 6: Мало уровней — только -8/+8
    variants.append(Params(
        poc_lookback=600, range_pct=20.0,
        order_levels=[-8, 8],
        order_sizes={-8: 50, 8: 50},
        stop_from_border_pct=3, tp_offset_pct=1.0, max_positions=2,
    ))

    # Вариант 7: 4 уровня — -5/-8/-10/-13
    variants.append(Params(
        poc_lookback=600, range_pct=20.0,
        order_levels=[-5, -8, -10, -13, 5, 8, 10, 13],
        order_sizes={-5: 15, -8: 15, -10: 15, -13: 15, 5: 15, 8: 15, 10: 15, 13: 15},
        stop_from_border_pct=3, tp_offset_pct=1.0, max_positions=4,
    ))

    # Вариант 8: POC окно 400
    variants.append(Params(
        poc_lookback=400, range_pct=20.0,
        order_levels=[-6, -8, -10, 6, 8, 10],
        order_sizes={-6: 30, -8: 20, -10: 10, 6: 30, 8: 20, 10: 10},
        stop_from_border_pct=3, tp_offset_pct=1.0, max_positions=3,
    ))

    # Вариант 9: POC окно 300
    variants.append(Params(
        poc_lookback=300, range_pct=20.0,
        order_levels=[-6, -8, -10, 6, 8, 10],
        order_sizes={-6: 30, -8: 20, -10: 10, 6: 30, 8: 20, 10: 10},
        stop_from_border_pct=3, tp_offset_pct=1.0, max_positions=3,
    ))

    # Вариант 10: TP=2%, агрессивный
    variants.append(Params(
        poc_lookback=600, range_pct=20.0,
        order_levels=[-6, -8, -10, 6, 8, 10],
        order_sizes={-6: 20, -8: 20, -10: 20, 6: 20, 8: 20, 10: 20},
        stop_from_border_pct=3, tp_offset_pct=2.0, max_positions=3,
    ))

    # Вариант 11: TP=0.5%, консервативный
    variants.append(Params(
        poc_lookback=600, range_pct=20.0,
        order_levels=[-6, -8, -10, 6, 8, 10],
        order_sizes={-6: 20, -8: 20, -10: 20, 6: 20, 8: 20, 10: 20},
        stop_from_border_pct=3, tp_offset_pct=0.5, max_positions=3,
    ))

    # Вариант 12: SL=2% от границы
    variants.append(Params(
        poc_lookback=600, range_pct=20.0,
        order_levels=[-6, -8, -10, 6, 8, 10],
        order_sizes={-6: 25, -8: 25, -10: 25, 6: 25, 8: 25, 10: 25},
        stop_from_border_pct=2, tp_offset_pct=1.0, max_positions=3,
    ))

    # Вариант 13: SL=5% от границы
    variants.append(Params(
        poc_lookback=600, range_pct=20.0,
        order_levels=[-6, -8, -10, 6, 8, 10],
        order_sizes={-6: 25, -8: 25, -10: 25, 6: 25, 8: 25, 10: 25},
        stop_from_border_pct=5, tp_offset_pct=1.0, max_positions=3,
    ))

    # Вариант 14: 2 позиции макс
    variants.append(Params(
        poc_lookback=600, range_pct=20.0,
        order_levels=[-6, -8, -10, 6, 8, 10],
        order_sizes={-6: 25, -8: 25, -10: 25, 6: 25, 8: 25, 10: 25},
        stop_from_border_pct=3, tp_offset_pct=1.0, max_positions=2,
    ))

    # Вариант 15: Только 3 уровня — -6/-8/-10
    variants.append(Params(
        poc_lookback=600, range_pct=20.0,
        order_levels=[-6, -8, -10, 6, 8, 10],
        order_sizes={-6: 33, -8: 33, -10: 34, 6: 33, 8: 33, 10: 34},
        stop_from_border_pct=3, tp_offset_pct=1.0, max_positions=3,
    ))

    # Вариант 16: Коридор ±12%, SL=2%
    variants.append(Params(
        poc_lookback=600, range_pct=12.0,
        order_levels=[-4, -6, -8, 4, 6, 8],
        order_sizes={-4: 20, -6: 25, -8: 30, 4: 20, 6: 25, 8: 30},
        stop_from_border_pct=2, tp_offset_pct=0.8, max_positions=3,
    ))

    # Вариант 17: Коридор ±30%, дальние уровни
    variants.append(Params(
        poc_lookback=600, range_pct=30.0,
        order_levels=[-10, -15, -20, 10, 15, 20],
        order_sizes={-10: 15, -15: 20, -20: 30, 10: 15, 15: 20, 20: 30},
        stop_from_border_pct=5, tp_offset_pct=2.0, max_positions=3,
    ))

    # Вариант 18: Асимметричные стопы
    variants.append(Params(
        poc_lookback=600, range_pct=20.0,
        order_levels=[-6, -8, -10, 6, 8, 10],
        order_sizes={-6: 20, -8: 20, -10: 20, 6: 20, 8: 20, 10: 20},
        stop_from_border_pct=3, tp_offset_pct=1.0, max_positions=3,
    ))

    # Вариант 19: POC=800, длинное окно
    variants.append(Params(
        poc_lookback=800, range_pct=20.0,
        order_levels=[-6, -8, -10, 6, 8, 10],
        order_sizes={-6: 30, -8: 20, -10: 10, 6: 30, 8: 20, 10: 10},
        stop_from_border_pct=3, tp_offset_pct=1.0, max_positions=3,
    ))

    # Вариант 20: POC=200, короткое окно
    variants.append(Params(
        poc_lookback=200, range_pct=20.0,
        order_levels=[-6, -8, -10, 6, 8, 10],
        order_sizes={-6: 30, -8: 20, -10: 10, 6: 30, 8: 20, 10: 10},
        stop_from_border_pct=3, tp_offset_pct=1.0, max_positions=3,
    ))

    # Вариант 21: Нет stop zone (0%), все ордера близко к POC
    variants.append(Params(
        poc_lookback=600, range_pct=20.0,
        order_levels=[-3, -5, -8, 3, 5, 8],
        order_sizes={-3: 15, -5: 20, -8: 25, 3: 15, 5: 20, 8: 25},
        stop_from_border_pct=2, tp_offset_pct=0.5, max_positions=3,
    ))

    # Вариант 22: Максимально агрессивный — 60 на уровень
    variants.append(Params(
        poc_lookback=600, range_pct=20.0,
        order_levels=[-6, -10, 6, 10],
        order_sizes={-6: 25, -10: 25, 6: 25, 10: 25},
        stop_from_border_pct=3, tp_offset_pct=1.5, max_positions=2,
    ))

    # Вариант 23: 5 уровней на сторону
    variants.append(Params(
        poc_lookback=600, range_pct=20.0,
        order_levels=[-4, -6, -8, -10, -12, 4, 6, 8, 10, 12],
        order_sizes={-4: 10, -6: 10, -8: 10, -10: 10, -12: 10, 4: 10, 6: 10, 8: 10, 10: 10, 12: 10},
        stop_from_border_pct=3, tp_offset_pct=1.0, max_positions=5,
    ))

    # Вариант 24: Коридор ±18%, TP=1.2
    variants.append(Params(
        poc_lookback=600, range_pct=18.0,
        order_levels=[-5, -7, -9, 5, 7, 9],
        order_sizes={-5: 20, -7: 20, -9: 20, 5: 20, 7: 20, 9: 20},
        stop_from_border_pct=3, tp_offset_pct=1.2, max_positions=3,
    ))

    # Вариант 25: Только ближние уровни -5/-7
    variants.append(Params(
        poc_lookback=600, range_pct=15.0,
        order_levels=[-5, -7, 5, 7],
        order_sizes={-5: 30, -7: 30, 5: 30, 7: 30},
        stop_from_border_pct=2, tp_offset_pct=1.0, max_positions=2,
    ))

    # Вариант 26: Дальние -10/-12/-15
    variants.append(Params(
        poc_lookback=600, range_pct=25.0,
        order_levels=[-10, -12, -15, 10, 12, 15],
        order_sizes={-10: 20, -12: 25, -15: 30, 10: 20, 12: 25, 15: 30},
        stop_from_border_pct=4, tp_offset_pct=2.0, max_positions=3,
    ))

    # Вариант 27: Смешанный — ближе больше, дальше меньше, TP=1.5
    variants.append(Params(
        poc_lookback=600, range_pct=20.0,
        order_levels=[-5, -8, -11, 5, 8, 11],
        order_sizes={-5: 35, -8: 25, -11: 10, 5: 35, 8: 25, 11: 10},
        stop_from_border_pct=3, tp_offset_pct=1.5, max_positions=3,
    ))

    # Вариант 28: Маленький коридор ±10%, быстрый TP
    variants.append(Params(
        poc_lookback=600, range_pct=10.0,
        order_levels=[-3, -5, -7, 3, 5, 7],
        order_sizes={-3: 20, -5: 25, -7: 30, 3: 20, 5: 25, 7: 30},
        stop_from_border_pct=2, tp_offset_pct=0.8, max_positions=3,
    ))

    # Вариант 29: Один уровень — -8%, $100
    variants.append(Params(
        poc_lookback=600, range_pct=20.0,
        order_levels=[-8, 8],
        order_sizes={-8: 50, 8: 50},
        stop_from_border_pct=3, tp_offset_pct=1.0, max_positions=1,
    ))

    # Вариант 30: POC=500, среднее окно
    variants.append(Params(
        poc_lookback=500, range_pct=20.0,
        order_levels=[-6, -8, -10, 6, 8, 10],
        order_sizes={-6: 30, -8: 20, -10: 10, 6: 30, 8: 20, 10: 10},
        stop_from_border_pct=3, tp_offset_pct=1.0, max_positions=3,
    ))

    return variants


# ============================================================
# Score:复合评分
# ============================================================

def score_result(avg_pnl: float, avg_dd: float, avg_sharpe: float, profitable_pct: float) -> float:
    """Чем больше — тем лучше."""
    # Штрафуем просадку, бонус за прибыль и Sharpe
    dd_penalty = max(0, avg_dd - 10) * 2  # >10% просадка — штраф
    return avg_pnl * 2 + avg_sharpe * 5 + profitable_pct * 30 - dd_penalty


# ============================================================
# Walk-forward
# ============================================================

def walk_forward(candles_dict: dict[str, list[Candle]], p: Params, split_pct: float = 0.6) -> tuple[float, float]:
    """Walk-forward: оптимизация на первых split_pct, валидация на остальных.
    Returns: (train_pnl, test_pnl)
    """
    train_pnls = []
    test_pnls = []

    for sym, candles in candles_dict.items():
        split_idx = int(len(candles) * split_pct)
        train = candles[:split_idx]
        test = candles[split_idx:]

        if len(train) < p.poc_lookback or len(test) < p.poc_lookback:
            continue

        train_res = run_backtest(train, p)
        test_res = run_backtest(test, p)
        train_pnls.append(train_res.pnl)
        test_pnls.append(test_res.pnl)

    avg_train = sum(train_pnls) / len(train_pnls) if train_pnls else 0
    avg_test = sum(test_pnls) / len(test_pnls) if test_pnls else 0
    return avg_train, avg_test


# ============================================================
# Main
# ============================================================

def main():
    print("=" * 100)
    print("  ОПТИМИЗАТОР FLAT СТРАТЕГИИ | 90 DAYS | M5 | SPREAD 0.3% | COMMISSION 0.04%")
    print("=" * 100)

    session = HTTP(testnet=False, api_key=API_KEY, api_secret=API_SECRET)

    # Загружаем данные
    print("\n  Загрузка данных...")
    candles_dict: dict[str, list[Candle]] = {}
    for sym in ALL_SYMBOLS:
        print(f"    {sym}...", end=" ", flush=True)
        candles = fetch_candles(session, sym)
        candles_dict[sym] = candles
        print(f"{len(candles)} candles")

    variants = generate_variants()
    print(f"\n  Вариантов для тестирования: {len(variants)}")

    # Тестируем все варианты
    print("\n  Запуск тестирования...")
    results = []

    for idx, p in enumerate(variants):
        label = p.to_label()
        print(f"  [{idx+1:2d}/{len(variants)}] {label}", end="")

        coin_pnls = []
        coin_dds = []
        coin_sharpes = []
        profitable = 0
        total_coins = 0

        for sym, candles in candles_dict.items():
            res = run_backtest(candles, p)
            coin_pnls.append(res.pnl)
            coin_dds.append(res.max_dd_pct)
            coin_sharpes.append(res.sharpe)
            if res.pnl > 0:
                profitable += 1
            total_coins += 1

        avg_pnl = sum(coin_pnls) / len(coin_pnls)
        avg_dd = sum(coin_dds) / len(coin_dds)
        avg_sharpe = sum(coin_sharpes) / len(coin_sharpes)
        prof_pct = profitable / total_coins * 100

        s = score_result(avg_pnl, avg_dd, avg_sharpe, prof_pct)

        # Walk-forward
        wf_train, wf_test = walk_forward(candles_dict, p)

        results.append({
            "params": p,
            "label": label,
            "avg_pnl": avg_pnl,
            "avg_dd": avg_dd,
            "avg_sharpe": avg_sharpe,
            "profitable_pct": prof_pct,
            "score": s,
            "wf_train": wf_train,
            "wf_test": wf_test,
            "coin_pnls": coin_pnls,
        })

        print(f" → PnL={avg_pnl:+.1f}% DD={avg_dd:.1f}% Sh={avg_sharpe:.1f} Score={s:.1f} WF={wf_train:+.1f}%→{wf_test:+.1f}%")

    # Сортируем по Score
    results.sort(key=lambda x: x["score"], reverse=True)

    # Топ-5
    print("\n" + "=" * 100)
    print("  ТОП-5 ЛУЧШИХ КОМБИНАЦИЙ")
    print("=" * 100)

    for rank, r in enumerate(results[:5], 1):
        p = r["params"]
        print(f"\n  #{rank} Score={r['score']:.1f}")
        print(f"    {r['label']}")
        print(f"    PnL: {r['avg_pnl']:+.1f}% | MaxDD: {r['avg_dd']:.1f}% | Sharpe: {r['avg_sharpe']:.1f} | Profitable: {r['profitable_pct']:.0f}%")
        print(f"    Walk-forward: train={r['wf_train']:+.1f}% → test={r['wf_test']:+.1f}%")
        coins_str = " | ".join(f"{sym}={pnl:+.1f}%" for sym, pnl in zip(ALL_SYMBOLS, r["coin_pnls"]))
        print(f"    По монетам: {coins_str}")

    # Все результаты
    print("\n" + "=" * 100)
    print("  ВСЕ РЕЗУЛЬТАТЫ (от лучшего к худшему)")
    print("=" * 100)
    print(f"  {'#':>3} {'Score':>7} {'PnL':>8} {'DD':>7} {'Sharpe':>7} {'Prof%':>6} {'WF_train':>9} {'WF_test':>9}  Label")
    print("  " + "-" * 110)
    for rank, r in enumerate(results, 1):
        print(f"  {rank:3d} {r['score']:7.1f} {r['avg_pnl']:+7.1f}% {r['avg_dd']:6.1f}% {r['avg_sharpe']:7.1f} {r['profitable_pct']:5.0f}% {r['wf_train']:+8.1f}% {r['wf_test']:+8.1f}%  {r['label']}")

    # Сохраняем
    now_str = datetime.now(tz=MSK).strftime("%Y%m%d_%H%M")
    filepath = REPORT_DIR / f"optimize_flat_{now_str}.txt"

    lines = [
        "=" * 100,
        "  ОПТИМИЗАТОР FLAT | 90 DAYS | M5 | SPREAD 0.3% | COMMISSION 0.04%",
        "=" * 100,
        "",
        f"  Date: {datetime.now(tz=MSK).strftime('%d.%m.%Y %H:%M MSK')}",
        f"  Variants tested: {len(variants)}",
        f"  Symbols: {', '.join(ALL_SYMBOLS)}",
        "",
        "-" * 100,
        "  ТОП-5 ЛУЧШИХ КОМБИНАЦИЙ:",
        "-" * 100,
    ]

    for rank, r in enumerate(results[:5], 1):
        p = r["params"]
        lines.append(f"\n  #{rank} Score={r['score']:.1f}")
        lines.append(f"    {r['label']}")
        lines.append(f"    PnL: {r['avg_pnl']:+.1f}% | MaxDD: {r['avg_dd']:.1f}% | Sharpe: {r['avg_sharpe']:.1f} | Profitable: {r['profitable_pct']:.0f}%")
        lines.append(f"    Walk-forward: train={r['wf_train']:+.1f}% → test={r['wf_test']:+.1f}%")
        coins_str = " | ".join(f"{sym}={pnl:+.1f}%" for sym, pnl in zip(ALL_SYMBOLS, r["coin_pnls"]))
        lines.append(f"    По монетам: {coins_str}")

    lines += ["", "-" * 100, "  ВСЕ РЕЗУЛЬТАТЫ:", "-" * 100, ""]
    lines.append(f"  {'#':>3} {'Score':>7} {'PnL':>8} {'DD':>7} {'Sharpe':>7} {'Prof%':>6} {'WF_t':>9} {'WF_v':>9}  Label")
    lines.append("  " + "-" * 110)
    for rank, r in enumerate(results, 1):
        lines.append(f"  {rank:3d} {r['score']:7.1f} {r['avg_pnl']:+7.1f}% {r['avg_dd']:6.1f}% {r['avg_sharpe']:7.1f} {r['profitable_pct']:5.0f}% {r['wf_train']:+8.1f}% {r['wf_test']:+8.1f}%  {r['label']}")

    lines.append("")
    lines.append("=" * 100)

    filepath.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n  Saved: {filepath}")


if __name__ == "__main__":
    main()
