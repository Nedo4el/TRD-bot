"""Бэктест любой из стратегий проекта на исторических данных.

Запуск (из корня проекта):
    python backtest.py --strategy sma
    python backtest.py --strategy combo   --limit 1000
    python backtest.py --strategy adaptive --timeframe 5
    python backtest.py --strategy swings  --sl 0.2 --tp 0.4

Как работает:
1. Загружает N свечей с Bybit.
2. На каждой свече спрашивает у выбранной стратегии сигнал.
3. Эмулирует вход/выход с SL/TP (если стратегия дала свои уровни —
   используются они, иначе проценты --sl/--tp).
4. Печатает статистику: сделки, win rate, итоговый PnL.

Важно: это оценка на истории, а не гарантия прибыли.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from bot_adaptive.strategy import AdaptiveAtrStrategy
from bot_combo.strategy import ComboFilterStrategy
from bot_sma.strategy import SmaCrossStrategy
from bot_swings.strategy import SwingLevelsStrategy
from core.bybit_client import BybitClient, Candle
from core.config import Config, get_env_float, get_env_int, load_bot_env
from core.logger import setup_logging
from core.strategies import BaseStrategy, Signal


def make_strategy(name: str) -> BaseStrategy:
    """Создать стратегию по имени (параметры — из .env корня, если есть).

    Args:
        name: sma | combo | adaptive | swings.

    Returns:
        Готовый объект стратегии с настройками по умолчанию/.env.
    """
    if name == "sma":
        return SmaCrossStrategy(
            fast_period=get_env_int("FAST_MA_PERIOD", 7),
            slow_period=get_env_int("SLOW_MA_PERIOD", 25),
        )
    if name == "combo":
        return ComboFilterStrategy(
            ema_fast=get_env_int("EMA_FAST_PERIOD", 21),
            ema_slow=get_env_int("EMA_SLOW_PERIOD", 50),
            rsi_period=get_env_int("RSI_PERIOD", 14),
            rsi_oversold=get_env_float("RSI_OVERSOLD", 30.0),
            rsi_overbought=get_env_float("RSI_OVERBOUGHT", 70.0),
            bb_period=get_env_int("BB_PERIOD", 20),
            bb_deviation=get_env_float("BB_DEVIATION", 2.0),
        )
    if name == "adaptive":
        return AdaptiveAtrStrategy(
            trend_period=get_env_int("SMA_TREND_PERIOD", 200),
            ema_fast=get_env_int("EMA_FAST_PERIOD", 5),
            ema_slow=get_env_int("EMA_SLOW_PERIOD", 13),
            atr_period=get_env_int("ATR_PERIOD", 14),
            atr_sl_mult=get_env_float("ATR_SL_MULT", 1.5),
            atr_tp_mult=get_env_float("ATR_TP_MULT", 2.5),
            atr_avg_lookback=get_env_int("ATR_AVG_LOOKBACK", 1440),
            min_atr_pct=get_env_float("MIN_ATR_PCT", 0.1),
        )
    if name == "swings":
        return SwingLevelsStrategy(
            lookback=get_env_int("LOOKBACK_CANDLES", 50),
            wing_size=get_env_int("WING_SIZE", 5),
            max_levels=get_env_int("MAX_LEVELS", 5),
            retest_max_bars=get_env_int("RETEST_MAX_BARS", 20),
        )
    raise ValueError(f"Неизвестная стратегия: {name}")


def run_backtest(
    candles: list[Candle],
    strategy: BaseStrategy,
    sl_pct_fallback: float,
    tp_pct_fallback: float,
) -> dict:
    """Эмулировать торговлю по свечам.

    Args:
        candles: свечи от старых к новым (последняя может быть незакрытой —
            она отбрасывается, как и в живом движке).
        strategy: объект стратегии (check_signal вызывается на каждой свече).
        sl_pct_fallback: SL в %, если стратегия не дала свою цену стопа.
        tp_pct_fallback: TP в %, аналогично.

    Returns:
        Словарь со статистикой бэктеста.
    """
    closed = candles[:-1]  # незакрытая свеча не участвует
    trades = 0
    wins = 0
    pnl_pct = 0.0
    in_position: str | None = None  # "Buy"/"Sell" или None
    entry_price = 0.0
    stop_price = 0.0
    take_price = 0.0

    # Разогрев: пропускаем свечи, пока стратегии хватает данных
    warmup = max(len(closed) // 4, 210)

    for i in range(warmup, len(closed)):
        window = closed[: i + 1]
        signal: Signal = strategy.check_signal(window)
        bar = window[-1]

        if in_position is not None:
            # --- Позиция открыта: проверяем SL/TP внутри свечи ---
            # Приоритет стоп-лосса (консервативный подход)
            hit_sl = (
                bar.low <= stop_price
                if in_position == "Buy"
                else bar.high >= stop_price
            )
            hit_tp = (
                bar.high >= take_price
                if in_position == "Buy"
                else bar.low <= take_price
            )
            if hit_sl:
                exit_price = stop_price
            elif hit_tp:
                exit_price = take_price
            else:
                continue  # позиция ещё жива
            move = (
                (exit_price - entry_price) / entry_price * 100
                if in_position == "Buy"
                else (entry_price - exit_price) / entry_price * 100
            )
            trades += 1
            pnl_pct += move
            if move > 0:
                wins += 1
            in_position = None
            continue

        # --- Вне позиции: входим по сигналу на закрытии свечи ---
        if signal.action not in ("buy", "sell"):
            continue
        in_position = signal.action.capitalize()
        entry_price = bar.close
        if signal.stop_loss is not None and signal.take_profit is not None:
            stop_price = signal.stop_loss
            take_price = signal.take_profit
        else:
            if in_position == "Buy":
                stop_price = entry_price * (1 - sl_pct_fallback / 100.0)
                take_price = entry_price * (1 + tp_pct_fallback / 100.0)
            else:
                stop_price = entry_price * (1 + sl_pct_fallback / 100.0)
                take_price = entry_price * (1 - tp_pct_fallback / 100.0)

    return {
        "trades": trades,
        "wins": wins,
        "win_rate": wins / trades * 100 if trades else 0.0,
        "pnl_pct": pnl_pct,
    }


async def fetch_candles(config: Config, limit: int) -> list[Candle]:
    """Загрузить свечи с Bybit через API."""
    client = BybitClient(config)
    try:
        return await client.get_klines(
            config.symbol,
            interval=config.timeframe,
            limit=limit,
        )
    finally:
        client.close()


def main() -> None:
    """Точка входа бэктеста."""
    parser = argparse.ArgumentParser(description="Бэктест стратегий на Bybit")
    parser.add_argument(
        "--strategy",
        required=True,
        choices=["sma", "combo", "adaptive", "swings"],
        help="какую стратегию тестировать",
    )
    parser.add_argument("--limit", type=int, default=500, help="сколько свечей брать")
    parser.add_argument("--symbol", default=None, help="пара (по умолчанию из .env)")
    parser.add_argument("--timeframe", default=None, help="таймфрейм")
    parser.add_argument("--sl", type=float, default=None, help="SL %% (fallback)")
    parser.add_argument("--tp", type=float, default=None, help="TP %% (fallback)")
    args = parser.parse_args()

    setup_logging("logs/backtest.log", "WARNING")
    load_bot_env(Path(__file__).parent)  # общий .env из корня (если есть)

    config = Config()
    if args.symbol:
        config.symbol = args.symbol
    if args.timeframe:
        config.timeframe = args.timeframe
    sl_pct = args.sl if args.sl is not None else config.stop_loss_pct
    tp_pct = args.tp if args.tp is not None else config.take_profit_pct
    config.validate()

    strategy = make_strategy(args.strategy)
    candles = asyncio.run(fetch_candles(config, args.limit))
    result = run_backtest(candles, strategy, sl_pct, tp_pct)

    print("=" * 50)
    print(
        f"Стратегия: {strategy.name} | Пара: {config.symbol} | "
        f"Таймфрейм: {config.timeframe}",
    )
    print(f"Свечей проанализировано: {len(candles)}")
    print(f"Fallback стопы: SL={sl_pct}% | TP={tp_pct}%")
    print("-" * 50)
    print(f"Сделок: {result['trades']}")
    print(f"Выигрышных: {result['wins']} ({result['win_rate']:.1f}%)")
    print(f"Итоговый результат: {result['pnl_pct']:+.2f}%")
    print("=" * 50)


if __name__ == "__main__":
    main()
