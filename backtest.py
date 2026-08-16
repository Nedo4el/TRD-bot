"""Бэктест стратегии на исторических данных.

Как работает:
1. Загружает N свечей с Bybit через API.
2. Прогоняет стратегию по свечам, эмулируя вход/выход с SL/TP.
3. Печатает статистику: число сделок, win rate, итоговый PnL.

Важно: это оценка на исторических данных, а не гарантия прибыли.
Всегда проверяйте стратегию перед запуском на реальные деньги.
"""

from __future__ import annotations

import argparse
import asyncio

from bybit_client import BybitClient, Candle
from config import Config
from logger import setup_logging
from strategy import generate_signal


def run_backtest(
    candles: list[Candle],
    fast: int,
    slow: int,
    sl_pct: float,
    tp_pct: float,
) -> dict:
    """Эмулировать торговлю по свечам.

    Args:
        candles: свечи от старых к новым.
        fast: период быстрой SMA.
        slow: период медленной SMA.
        sl_pct: стоп-лосс в %.
        tp_pct: тейк-профит в %.

    Returns:
        Словарь со статистикой бэктеста.
    """
    trades = 0
    wins = 0
    pnl_pct = 0.0
    in_position: str | None = None  # "Buy"/"Sell" или None
    entry_price = 0.0

    # Проходим по свечам последовательно; сигнал считаем на каждой свече
    for i in range(slow + 2, len(candles)):
        window = candles[: i + 1]
        signal = generate_signal(window, fast, slow)
        price = candles[i].close

        if in_position is None:
            # Вне позиции: входим по сигналу
            if signal.action in ("buy", "sell"):
                in_position = signal.action.capitalize()
                entry_price = price
            continue

        # В позиции: проверяем срабатывание SL/TP на этой свече
        if in_position == "Buy":
            sl = entry_price * (1 - sl_pct / 100.0)
            tp = entry_price * (1 + tp_pct / 100.0)
        else:
            sl = entry_price * (1 + sl_pct / 100.0)
            tp = entry_price * (1 - tp_pct / 100.0)

        # Приоритет стоп-лосса (консервативный подход)
        hit_sl = price <= sl if in_position == "Buy" else price >= sl
        hit_tp = price >= tp if in_position == "Buy" else price <= tp

        if hit_sl:
            trade_pnl = -sl_pct
        elif hit_tp:
            trade_pnl = tp_pct
        else:
            continue  # позиция ещё жива, ждём следующую свечу

        trades += 1
        pnl_pct += trade_pnl
        if trade_pnl > 0:
            wins += 1
        in_position = None

    return {
        "trades": trades,
        "wins": wins,
        "win_rate": wins / trades * 100 if trades else 0.0,
        "pnl_pct": pnl_pct,
    }


async def fetch_candles(config: Config, limit: int) -> list[Candle]:
    """Загрузить свечи с Bybit через API.

    Args:
        config: конфигурация бота.
        limit: сколько свечей запросить.

    Returns:
        Список свечей от старых к новым.
    """
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
    """Точка входа бэктеста (запуск: python backtest.py)."""
    parser = argparse.ArgumentParser(description="Бэктест SMA-стратегии на Bybit")
    parser.add_argument("--limit", type=int, default=500, help="сколько свечей брать")
    args = parser.parse_args()

    setup_logging("logs/backtest.log", "INFO")
    config = Config()
    config.validate()

    candles = asyncio.run(fetch_candles(config, args.limit))
    result = run_backtest(
        candles,
        config.fast_ma_period,
        config.slow_ma_period,
        config.stop_loss_pct,
        config.take_profit_pct,
    )

    print("=" * 50)
    print(f"Пара: {config.symbol} | Таймфрейм: {config.timeframe}")
    print(f"Свечей проанализировано: {len(candles)}")
    print(
        f"SMA: {config.fast_ma_period}/{config.slow_ma_period} | "
        f"SL: {config.stop_loss_pct}% | TP: {config.take_profit_pct}%",
    )
    print("-" * 50)
    print(f"Сделок: {result['trades']}")
    print(f"Выигрышных: {result['wins']} ({result['win_rate']:.1f}%)")
    print(f"Итоговый результат: {result['pnl_pct']:+.2f}%")
    print("=" * 50)


if __name__ == "__main__":
    main()
