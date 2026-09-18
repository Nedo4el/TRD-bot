"""Бэктест robot_flat на 5 монетах, 30 дней, отчёт в файл."""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pybit.unified_trading import HTTP

from core.bybit_client import Candle
from robot_flat.strategy import FlatStrategy, FlatConfig

MSK = timezone(timedelta(hours=3))
API_KEY = "kOFDh0YrjjbkiC8VSp"
API_SECRET = "S2f2wUhMMDSqufEcgGzikwyZ1YH7yUsdyf5A"

# 30 дней × 24 часа × 12 (M5) = 8640 свечей
CANDLES_30D = 8640
PAGE_SIZE = 1000
TIMEFRAME = "5"

REPORT_DIR = Path(r"C:\Users\79095\Desktop\trd\Отчеты скринера")


async def fetch_candles_30d(session: HTTP, symbol: str) -> list[Candle]:
    """Загрузить 30 дней свечей M5 с пагинацией."""
    all_candles: list[Candle] = []
    seen_times: set[int] = set()
    remaining = CANDLES_30D
    # end_ts: upper bound for next request (exclusive boundary)
    boundary_ms: int | None = None

    while remaining > 0:
        batch = min(remaining, PAGE_SIZE)

        params = {
            "category": "linear",
            "symbol": symbol,
            "interval": TIMEFRAME,
            "limit": batch,
        }
        if boundary_ms is not None:
            params["end"] = boundary_ms

        resp = session.get_kline(**params)
        klines = resp["result"]["list"]

        if not klines:
            break

        new_count = 0
        oldest_in_batch = None
        for k in reversed(klines):
            ts = int(k[0])
            if ts not in seen_times:
                seen_times.add(ts)
                all_candles.append(
                    Candle(
                        open_time=ts,
                        open=float(k[1]),
                        high=float(k[2]),
                        low=float(k[3]),
                        close=float(k[4]),
                        volume=float(k[5]),
                    )
                )
                new_count += 1
                if oldest_in_batch is None or ts < oldest_in_batch:
                    oldest_in_batch = ts

        # Следующий batch: берём свечи ДО самого старого из текущего
        if oldest_in_batch is not None:
            interval_ms = int(TIMEFRAME) * 60_000
            boundary_ms = oldest_in_batch - interval_ms

        remaining -= new_count
        if new_count == 0 or len(klines) < batch:
            break

    return all_candles


def run_backtest(
    candles: list[Candle],
    strategy: FlatStrategy,
) -> dict:
    """Эмуляция торговли по сетке ордеров."""
    poc = strategy._fixed_poc
    if poc is None or poc <= 0:
        return {"trades": [], "pnl": 0.0, "max_dd": 0.0}

    half_range = strategy.cfg.range_pct / 2
    tp_offset = strategy.cfg.tp_offset_pct

    trades = []
    equity = 100.0
    peak_equity = 100.0
    max_dd = 0.0

    # Позиции: список словарей {side, entry, sl, tp, qty}
    positions = []
    # Уже исполненные уровни (чтобы не дублировать)
    filled_long_levels: set[float] = set()
    filled_short_levels: set[float] = set()
    # Флаг: были закрытия на предыдущей свече
    had_closes = False

    for i in range(600, len(candles)):
        c = candles[i]
        high = c.high
        low = c.low

        # Сбрасываем исполненные уровни если прошла свеча после закрытия
        if had_closes and not positions:
            filled_long_levels.clear()
            filled_short_levels.clear()
        had_closes = False

        # Проверяем SL/TP для открытых позиций
        closed = []
        for pos in positions:
            if pos["side"] == "long":
                if low <= pos["sl"]:
                    pnl = (pos["sl"] - pos["entry"]) / pos["entry"] * 100
                    equity += pnl * pos["qty"] / 100
                    trades.append({
                        "side": "long",
                        "entry": pos["entry"],
                        "exit": pos["sl"],
                        "pnl": pnl,
                        "reason": "SL",
                        "time": c.open_time,
                    })
                    closed.append(pos)
                    had_closes = True
                elif high >= pos["tp"]:
                    pnl = (pos["tp"] - pos["entry"]) / pos["entry"] * 100
                    equity += pnl * pos["qty"] / 100
                    trades.append({
                        "side": "long",
                        "entry": pos["entry"],
                        "exit": pos["tp"],
                        "pnl": pnl,
                        "reason": "TP",
                        "time": c.open_time,
                    })
                    closed.append(pos)
                    had_closes = True
            elif pos["side"] == "short":
                if high >= pos["sl"]:
                    pnl = (pos["entry"] - pos["sl"]) / pos["entry"] * 100
                    equity += pnl * pos["qty"] / 100
                    trades.append({
                        "side": "short",
                        "entry": pos["entry"],
                        "exit": pos["sl"],
                        "pnl": pnl,
                        "reason": "SL",
                        "time": c.open_time,
                    })
                    closed.append(pos)
                    had_closes = True
                elif low <= pos["tp"]:
                    pnl = (pos["entry"] - pos["tp"]) / pos["entry"] * 100
                    equity += pnl * pos["qty"] / 100
                    trades.append({
                        "side": "short",
                        "entry": pos["entry"],
                        "exit": pos["tp"],
                        "pnl": pnl,
                        "reason": "TP",
                        "time": c.open_time,
                    })
                    closed.append(pos)
                    had_closes = True

        for p in closed:
            positions.remove(p)

        # Не открываем новые позиции на свече где были закрытия
        if had_closes:
            continue

        # Максимум 3 позиции в одну сторону
        longs = sum(1 for p in positions if p["side"] == "long")
        shorts = sum(1 for p in positions if p["side"] == "short")

        # Ищем входы
        levels = strategy.cfg.order_levels

        # LONG: цена на уровнях -6%, -8%, -10%
        if longs < strategy.cfg.max_positions:
            for level in sorted(levels, reverse=True):
                if level > 0 or level in filled_long_levels:
                    continue
                order_price = poc * (1 + level / 100)
                if low <= order_price <= high:
                    tp_level = abs(level) - tp_offset
                    tp_price = poc * (1 + tp_level / 100)
                    sl_price = poc * (1 - (half_range + 3) / 100)
                    positions.append({
                        "side": "long",
                        "entry": order_price,
                        "sl": sl_price,
                        "tp": tp_price,
                        "qty": 33.0,
                    })
                    filled_long_levels.add(level)
                    break

        # SHORT: цена на уровнях +6%, +8%, +10%
        if shorts < strategy.cfg.max_positions:
            for level in sorted(levels):
                if level < 0 or level in filled_short_levels:
                    continue
                order_price = poc * (1 + level / 100)
                if high >= order_price >= low:
                    tp_level = level - tp_offset
                    tp_price = poc * (1 - tp_level / 100)
                    sl_price = poc * (1 + (half_range + 3) / 100)
                    positions.append({
                        "side": "short",
                        "entry": order_price,
                        "sl": sl_price,
                        "tp": tp_price,
                        "qty": 33.0,
                    })
                    filled_short_levels.add(level)
                    break

        # Drawdown
        peak_equity = max(peak_equity, equity)
        dd = (peak_equity - equity) / peak_equity * 100 if peak_equity > 0 else 0
        max_dd = max(max_dd, dd)

    # Закрываем оставшиеся позиции по текущей цене
    last_price = candles[-1].close
    for pos in positions:
        if pos["side"] == "long":
            pnl = (last_price - pos["entry"]) / pos["entry"] * 100
        else:
            pnl = (pos["entry"] - last_price) / pos["entry"] * 100
        equity += pnl * pos["qty"] / 100
        trades.append({
            "side": pos["side"],
            "entry": pos["entry"],
            "exit": last_price,
            "pnl": pnl,
            "reason": "CLOSE",
            "time": candles[-1].open_time,
        })

    return {
        "trades": trades,
        "pnl": equity - 100.0,
        "max_dd": max_dd,
        "final_equity": equity,
    }


def format_report(
    symbol: str,
    result: dict,
    candles: list[Candle],
    poc: float,
) -> str:
    """Сформировать подробный отчёт."""
    trades = result["trades"]
    pnl = result["pnl"]
    max_dd = result["max_dd"]
    final_eq = result.get("final_equity", 100.0)

    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    longs = [t for t in trades if t["side"] == "long"]
    shorts = [t for t in trades if t["side"] == "short"]

    # Время
    first_dt = datetime.fromtimestamp(candles[0].open_time / 1000, tz=MSK)
    last_dt = datetime.fromtimestamp(candles[-1].open_time / 1000, tz=MSK)
    days = (last_dt - first_dt).total_seconds() / 86400

    lines = []
    lines.append("=" * 70)
    lines.append(f"  БЭКТЕСТ: {symbol} | robot_flat | M5 | {days:.1f} дней")
    lines.append("=" * 70)
    lines.append("")
    lines.append("  ПАРАМЕТРЫ:")
    lines.append(f"    POC:              ${poc:.4f}")
    lines.append(f"    Коридор:          ±10% от POC")
    lines.append(f"    Стоп зона:        ±5% от POC")
    lines.append(f"    Сетка LONG:       -6%, -8%, -10%")
    lines.append(f"    Сетка SHORT:      +6%, +8%, +10%")
    lines.append(f"    SL:               ±13% от POC")
    lines.append(f"    TP:               противоположный ордер -1%")
    lines.append(f"    Трейлинг:         2%")
    lines.append("")
    lines.append("  ПЕРИОД:")
    lines.append(f"    От:               {first_dt.strftime('%d.%m.%Y %H:%M')}")
    lines.append(f"    До:               {last_dt.strftime('%d.%m.%Y %H:%M')}")
    lines.append(f"    Свечей:           {len(candles)}")
    lines.append("")
    lines.append("-" * 70)
    lines.append("  СТАТИСТИКА:")
    lines.append(f"    Сделок:           {len(trades)}")
    lines.append(f"    Выигрышных:       {len(wins)} ({len(wins)/len(trades)*100:.1f}%)" if trades else "    Выигрышных:       0")
    lines.append(f"    Проигрышных:      {len(losses)}")
    lines.append(f"    LONG:             {len(longs)}")
    lines.append(f"    SHORT:            {len(shorts)}")

    if wins:
        avg_win = sum(t["pnl"] for t in wins) / len(wins)
        lines.append(f"    Средний win:      {avg_win:+.3f}%")
    if losses:
        avg_loss = sum(t["pnl"] for t in losses) / len(losses)
        lines.append(f"    Средний loss:     {avg_loss:+.3f}%")

    gross_profit = sum(t["pnl"] for t in wins) if wins else 0
    gross_loss = abs(sum(t["pnl"] for t in losses)) if losses else 0
    pf = gross_profit / gross_loss if gross_loss > 0 else float("inf")
    lines.append(f"    Profit factor:    {pf:.2f}")
    lines.append(f"    Итоговый PnL:     {pnl:+.2f}%")
    lines.append(f"    Макс. просадка:   {max_dd:.2f}%")
    lines.append(f"    Финальный баланс: {final_eq:.2f}%")
    lines.append("")
    lines.append("-" * 70)
    lines.append("  СДЕЛКИ:")
    lines.append(f"  {'#':>3} {'Сторона':<6} {'Вход':>10} {'Выход':>10} {'PnL':>8} {'Причина':<6}")
    lines.append("  " + "-" * 50)

    for i, t in enumerate(trades, 1):
        dt = datetime.fromtimestamp(t["time"] / 1000, tz=MSK)
        time_str = dt.strftime("%d.%m %H:%M")
        side = "LONG" if t["side"] == "long" else "SHORT"
        lines.append(
            f"  {i:>3} {side:<6} ${t['entry']:>9.4f} ${t['exit']:>9.4f} "
            f"{t['pnl']:>+7.3f}% {t['reason']:<6} {time_str}"
        )

    lines.append("")
    lines.append("=" * 70)

    return "\n".join(lines)


async def main() -> None:
    session = HTTP(testnet=False, api_key=API_KEY, api_secret=API_SECRET)

    symbols = ["AKEUSDT", "BTRUSDT", "BRUSDT", "LSKUSDT"]
    # niulai — проверим وجود
    niulai_candidates = ["NIULAUSDT", "NILUSDT", "NIUUSDT"]

    # Проверяем niulai
    resp = session.get_tickers(category="linear")
    available = {t["symbol"] for t in resp["result"]["list"]}
    niulai_symbol = None
    for c in niulai_candidates:
        if c in available:
            niulai_symbol = c
            break

    if niulai_symbol:
        symbols.append(niulai_symbol)
        print(f"Niulai найден: {niulai_symbol}")
    else:
        print(f"Niulai не найден на Bybit. Доступные похожие:")
        for s in sorted(available):
            if "NIU" in s or "NIL" in s:
                print(f"  {s}")

    print(f"\nТестирую: {symbols}")
    print()

    cfg = FlatConfig(poc_lookback=600, impulse_min_pct=15.0)
    now_str = datetime.now(tz=MSK).strftime("%Y%m%d_%H%M")

    for symbol in symbols:
        try:
            print(f"Загружаю {symbol}...")
            candles = await fetch_candles_30d(session, symbol)
            candles.sort(key=lambda c: c.open_time)
            print(f"  Загружено {len(candles)} свечей")

            if len(candles) < 600:
                print(f"  Мало данных, пропускаю")
                continue

            strategy = FlatStrategy(cfg)
            strategy.check_signal(candles)  # фиксирует POC
            poc = strategy._fixed_poc

            result = run_backtest(candles, strategy)
            report = format_report(symbol, result, candles, poc)

            # Сохраняем отчёт
            filename = f"backtest_{symbol}_{now_str}.txt"
            filepath = REPORT_DIR / filename
            filepath.write_text(report, encoding="utf-8")

            print(f"  POC: ${poc:.4f}")
            print(f"  Сделок: {len(result['trades'])}")
            print(f"  PnL: {result['pnl']:+.2f}%")
            print(f"  Отчёт: {filepath}")
            print()

        except Exception as e:
            print(f"  ОШИБКА: {e}")
            print()

    print("Готово!")


if __name__ == "__main__":
    asyncio.run(main())
