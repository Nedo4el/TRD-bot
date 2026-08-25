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
4. Печатает статистику и строит график equity curve.

Важно: это оценка на истории, а не гарантия прибыли.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, field
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


@dataclass
class Trade:
    """Одна завершённая сделка."""

    side: str  # "Buy" / "Sell"
    entry_price: float
    exit_price: float
    entry_time: int  # open_time свечи входа (unix, мс)
    exit_time: int  # open_time свечи выхода
    pnl_pct: float  # % от входа
    exit_reason: str  # "TP" / "SL"


@dataclass
class BacktestResult:
    """Полный результат бэктеста."""

    trades: list[Trade] = field(default_factory=list)
    equity_curve: list[float] = field(default_factory=list)
    equity_times: list[int] = field(default_factory=list)
    candles_used: list[Candle] = field(default_factory=list)

    @property
    def total_trades(self) -> int:
        return len(self.trades)

    @property
    def wins(self) -> int:
        return sum(1 for t in self.trades if t.pnl_pct > 0)

    @property
    def losses(self) -> int:
        return self.total_trades - self.wins

    @property
    def win_rate(self) -> float:
        return self.wins / self.total_trades * 100 if self.total_trades else 0.0

    @property
    def total_pnl(self) -> float:
        return sum(t.pnl_pct for t in self.trades)

    @property
    def avg_win(self) -> float:
        wins = [t.pnl_pct for t in self.trades if t.pnl_pct > 0]
        return sum(wins) / len(wins) if wins else 0.0

    @property
    def avg_loss(self) -> float:
        losses = [t.pnl_pct for t in self.trades if t.pnl_pct <= 0]
        return sum(losses) / len(losses) if losses else 0.0

    @property
    def profit_factor(self) -> float:
        gross_profit = sum(t.pnl_pct for t in self.trades if t.pnl_pct > 0)
        gross_loss = abs(sum(t.pnl_pct for t in self.trades if t.pnl_pct < 0))
        return gross_profit / gross_loss if gross_loss > 0 else float("inf")

    @property
    def max_drawdown(self) -> float:
        if not self.equity_curve:
            return 0.0
        peak = self.equity_curve[0]
        max_dd = 0.0
        for val in self.equity_curve:
            if val > peak:
                peak = val
            dd = (peak - val) / peak * 100 if peak > 0 else 0.0
            if dd > max_dd:
                max_dd = dd
        return max_dd

    @property
    def max_win_streak(self) -> int:
        return self._streak(True)

    @property
    def max_loss_streak(self) -> int:
        return self._streak(False)

    def _streak(self, win: bool) -> int:
        streak = 0
        best = 0
        for t in self.trades:
            if (t.pnl_pct > 0) == win:
                streak += 1
                best = max(best, streak)
            else:
                streak = 0
        return best


def run_backtest(
    candles: list[Candle],
    strategy: BaseStrategy,
    sl_pct_fallback: float,
    tp_pct_fallback: float,
) -> BacktestResult:
    """Эмулировать торговлю по свечам.

    Args:
        candles: свечи от старых к новым (последняя может быть незакрытой —
            она отбрасывается, как и в живом движке).
        strategy: объект стратегии (check_signal вызывается на каждой свече).
        sl_pct_fallback: SL в %, если стратегия не дала свою цену стопа.
        tp_pct_fallback: TP в %, аналогично.

    Returns:
        BacktestResult со сделками и equity curve.
    """
    closed = candles[:-1]  # незакрытая свеча не участвует
    result = BacktestResult()
    result.candles_used = closed
    equity = 100.0  # стартовый "баланс" в %
    in_position: str | None = None  # "Buy"/"Sell" или None
    entry_price = 0.0
    entry_time = 0
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
                exit_reason = "SL"
            elif hit_tp:
                exit_price = take_price
                exit_reason = "TP"
            else:
                result.equity_curve.append(equity)
                result.equity_times.append(bar.open_time)
                continue  # позиция ещё жива
            move = (
                (exit_price - entry_price) / entry_price * 100
                if in_position == "Buy"
                else (entry_price - exit_price) / entry_price * 100
            )
            equity += move
            result.trades.append(
                Trade(
                    side=in_position,
                    entry_price=entry_price,
                    exit_price=exit_price,
                    entry_time=entry_time,
                    exit_time=bar.open_time,
                    pnl_pct=move,
                    exit_reason=exit_reason,
                ),
            )
            in_position = None
            result.equity_curve.append(equity)
            result.equity_times.append(bar.open_time)
            continue

        # --- Вне позиции: входим по сигналу на закрытии свечи ---
        if signal.action not in ("buy", "sell"):
            result.equity_curve.append(equity)
            result.equity_times.append(bar.open_time)
            continue
        in_position = signal.action.capitalize()
        entry_price = bar.close
        entry_time = bar.open_time
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
        result.equity_curve.append(equity)
        result.equity_times.append(bar.open_time)

    return result


async def fetch_candles(config: Config, limit: int) -> list[Candle]:
    """Загрузить свечи с Bybit через API с пагинацией.

    Bybit отдаёт максимум 1000 свечей за запрос.
    Пагинация: запрашиваем newer через end= (timestamp старшей свечи - 1мс).
    """
    client = BybitClient(config)
    PAGE = 1000
    try:
        all_candles: list[Candle] = []
        remaining = limit

        while remaining > 0:
            batch = min(remaining, PAGE)
            end_ts = None
            if all_candles:
                end_ts = all_candles[0].open_time - 1

            candles = await client.get_klines(
                config.symbol,
                interval=config.timeframe,
                limit=batch,
                end=end_ts,
            )
            if not candles:
                break

            all_candles = candles + all_candles
            remaining -= len(candles)

            if len(candles) < batch:
                break

        return all_candles
    finally:
        client.close()


def print_stats(result: BacktestResult, strategy_name: str, symbol: str, timeframe: str) -> None:
    """Расширенная статистика бэктеста."""
    print("=" * 55)
    print(f"  Стратегия: {strategy_name} | {symbol} | {timeframe}")
    print("=" * 55)
    print(f"  Сделок:         {result.total_trades}")
    print(f"  Выигрышных:     {result.wins} ({result.win_rate:.1f}%)")
    print(f"  Проигрышных:    {result.losses}")
    print(f"  Средний win:    {result.avg_win:+.3f}%")
    print(f"  Средний loss:   {result.avg_loss:+.3f}%")
    print(f"  Profit factor:  {result.profit_factor:.2f}")
    print(f"  Макс. серия:    +{result.max_win_streak} / -{result.max_loss_streak}")
    print("-" * 55)
    print(f"  Итоговый PnL:   {result.total_pnl:+.2f}%")
    print(f"  Макс. просадка: {result.max_drawdown:.2f}%")
    print("=" * 55)


def plot_equity(result: BacktestResult, strategy_name: str, symbol: str, timeframe: str) -> None:
    """График equity curve."""
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    from datetime import datetime, timezone

    times = [datetime.fromtimestamp(t / 1000, tz=timezone.utc) for t in result.equity_times]

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 8), height_ratios=[3, 1])
    fig.suptitle(f"Backtest: {strategy_name} | {symbol} | {timeframe}", fontsize=14)

    # Equity curve
    ax1.plot(times, result.equity_curve, linewidth=1.2, color="#2196F3")
    ax1.axhline(y=100, color="gray", linestyle="--", linewidth=0.8, alpha=0.5)
    ax1.set_ylabel("Equity %")
    ax1.set_title("Equity Curve")
    ax1.grid(True, alpha=0.3)
    ax1.xaxis.set_major_formatter(mdates.DateFormatter("%d %b"))
    ax1.tick_params(axis="x", rotation=30)

    # Drawdown
    peak = 100.0
    dd_series = []
    for val in result.equity_curve:
        if val > peak:
            peak = val
        dd_series.append(-(peak - val) / peak * 100 if peak > 0 else 0.0)
    ax2.fill_between(times, dd_series, 0, color="#F44336", alpha=0.4)
    ax2.set_ylabel("Drawdown %")
    ax2.set_title("Drawdown")
    ax2.grid(True, alpha=0.3)
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%d %b"))
    ax2.tick_params(axis="x", rotation=30)

    plt.tight_layout()
    plt.savefig("backtest_result.png", dpi=150, bbox_inches="tight")
    plt.show()
    print("\nГрафик сохранён: backtest_result.png")


def plot_trades(result: BacktestResult, strategy_name: str, symbol: str, timeframe: str) -> None:
    """График цен со сделками: свечи + маркеры входов/выходов."""
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    import matplotlib.patches as mpatches
    from datetime import datetime, timezone

    candles = result.candles_used
    if not candles:
        return

    times = [datetime.fromtimestamp(c.open_time / 1000, tz=timezone.utc) for c in candles]
    opens = [c.open for c in candles]
    highs = [c.high for c in candles]
    lows = [c.low for c in candles]
    closes = [c.close for c in candles]

    fig, ax = plt.subplots(figsize=(16, 8))
    fig.suptitle(f"Сделки: {strategy_name} | {symbol} | {timeframe}", fontsize=14)

    # Свечи ( Naked-style: зелёные бычьи, красные медвежьи)
    width = (times[1] - times[0]) * 0.6 if len(times) > 1 else 0
    for i in range(len(times)):
        color = "#4CAF50" if closes[i] >= opens[i] else "#F44336"
        # Тело свечи
        body_bottom = min(opens[i], closes[i])
        body_height = abs(closes[i] - opens[i])
        ax.bar(times[i], body_height, bottom=body_bottom, width=width, color=color, edgecolor=color, linewidth=0.5)
        # Тени
        ax.vlines(times[i], lows[i], highs[i], color=color, linewidth=0.8)

    # Маркеры сделок
    for trade in result.trades:
        entry_dt = datetime.fromtimestamp(trade.entry_time / 1000, tz=timezone.utc)
        exit_dt = datetime.fromtimestamp(trade.exit_time / 1000, tz=timezone.utc)

        if trade.side == "Buy":
            # Вход: зелёный треугольник вверх
            ax.scatter(entry_dt, trade.entry_price, marker="^", color="#2196F3", s=120, zorder=5, edgecolors="black", linewidths=0.5)
            # Выход: маркер по результату
            exit_color = "#4CAF50" if trade.pnl_pct > 0 else "#F44336"
            ax.scatter(exit_dt, trade.exit_price, marker="v", color=exit_color, s=120, zorder=5, edgecolors="black", linewidths=0.5)
            # Линия сделки
            ax.plot([entry_dt, exit_dt], [trade.entry_price, trade.exit_price],
                    color=exit_color, linewidth=1, alpha=0.6, linestyle="--")
        else:
            # Вход: красный треугольник вниз
            ax.scatter(entry_dt, trade.entry_price, marker="v", color="#FF9800", s=120, zorder=5, edgecolors="black", linewidths=0.5)
            # Выход
            exit_color = "#4CAF50" if trade.pnl_pct > 0 else "#F44336"
            ax.scatter(exit_dt, trade.exit_price, marker="^", color=exit_color, s=120, zorder=5, edgecolors="black", linewidths=0.5)
            ax.plot([entry_dt, exit_dt], [trade.entry_price, trade.exit_price],
                    color=exit_color, linewidth=1, alpha=0.6, linestyle="--")

    ax.set_ylabel("Цена")
    ax.set_xlabel("Время")
    ax.grid(True, alpha=0.3)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d %b"))
    ax.tick_params(axis="x", rotation=30)

    # Легенда
    legend_elements = [
        mpatches.Patch(facecolor="#4CAF50", label="Бычья свеча"),
        mpatches.Patch(facecolor="#F44336", label="Медвежья свеча"),
        plt.Line2D([0], [0], marker="^", color="w", markerfacecolor="#2196F3", markersize=10, label="Вход Buy"),
        plt.Line2D([0], [0], marker="v", color="w", markerfacecolor="#FF9800", markersize=10, label="Вход Sell"),
        plt.Line2D([0], [0], marker="o", color="w", markerfacecolor="#4CAF50", markersize=10, label="Выход profit"),
        plt.Line2D([0], [0], marker="o", color="w", markerfacecolor="#F44336", markersize=10, label="Выход loss"),
    ]
    ax.legend(handles=legend_elements, loc="upper left", fontsize=9)

    plt.tight_layout()
    plt.savefig("backtest_trades.png", dpi=150, bbox_inches="tight")
    plt.show()
    print("График сделок сохранён: backtest_trades.png")


def main() -> None:
    """Точка входа бэктеста."""
    parser = argparse.ArgumentParser(description="Бэктест стратегий на Bybit")
    parser.add_argument(
        "--strategy",
        required=True,
        choices=["sma", "combo", "adaptive", "swings"],
        help="какую стратегию тестировать",
    )
    parser.add_argument("--limit", type=int, default=500, help="сколько свечей брать (пагинация >1000)")
    parser.add_argument("--symbol", default=None, help="пара (по умолчанию из .env)")
    parser.add_argument("--timeframe", default=None, help="таймфрейм")
    parser.add_argument("--sl", type=float, default=None, help="SL %% (fallback)")
    parser.add_argument("--tp", type=float, default=None, help="TP %% (fallback)")
    parser.add_argument("--no-chart", action="store_true", help="не показывать график")
    args = parser.parse_args()

    setup_logging("logs/backtest.log", "WARNING")
    load_bot_env(Path(__file__).parent)

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

    print_stats(result, strategy.name, config.symbol, config.timeframe)

    if not args.no_chart and result.equity_curve:
        plot_equity(result, strategy.name, config.symbol, config.timeframe)
        plot_trades(result, strategy.name, config.symbol, config.timeframe)


if __name__ == "__main__":
    main()
