"""Бэктест любой из стратегий проекта на исторических данных.

Запуск (из корня проекта):
    python backtest.py --strategy flat
    python backtest.py --strategy yrovni --limit 1000
    python backtest.py --strategy trend --timeframe 5
    python backtest.py --strategy impulse --sl 0.2 --tp 0.4

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

from core.bybit_client import BybitClient, Candle
from core.config import Config, load_bot_env
from core.logger import setup_logging
from core.strategies import BaseStrategy, Signal

# Импорты стратегий — добавляйте по мере написания
from robot_flat.strategy import FlatStrategy

# from robot_yrovni_D.strategy import YrovniDStrategy
from robot_trend.strategy import TrendStrategy

# from robot_impulse.strategy import ImpulseStrategy
# from robot_krugloe.strategy import KrugloeStrategy


def make_strategy(name: str) -> BaseStrategy:
    """Создать стратегию по имени (параметры — из .env корня, если есть).

    Args:
        name: flat | trend | grid_flat | yrovni | impulse | zero.

    Returns:
        Готовый объект стратегии с настройками по умолчанию/.env.
    """
    if name == "trend":
        return TrendStrategy()
    if name == "flat":
        return FlatStrategy()
    if name == "grid_flat":
        raise NotImplementedError(
            "grid_flat — сеточная стратегия (robot_grid_flat), "
            "бэктест этого движка её не поддерживает"
        )
    raise ValueError(
        f"Стратегия '{name}' ещё не реализована. "
        f"Доступны: trend, flat"
    )


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
            peak = max(peak, val)
            dd = (peak - val) / peak * 100 if peak > 0 else 0.0
            max_dd = max(max_dd, dd)
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
    """Эмулировать торговлю по свечам (мульти-позиции).

    Args:
        candles: свечи от старых к новым.
        strategy: объект стратегии.
        sl_pct_fallback: SL в %, если стратегия не дала свою.
        tp_pct_fallback: TP в %, аналогично.

    Returns:
        BacktestResult со сделками и equity curve.
    """
    closed = candles[:-1]
    result = BacktestResult()
    result.candles_used = closed
    equity = 100.0

    @dataclass
    class OpenPos:
        side: str
        entry_price: float
        entry_time: int
        stop_price: float
        take_price: float

    open_positions: list[OpenPos] = []

    warmup = getattr(strategy, '_min_warmup', max(len(closed) // 4, 210))

    for i in range(warmup, len(closed)):
        lookback = getattr(strategy, '_max_lookback', len(closed))
        start = max(0, i - lookback)
        window = closed[start:i + 1]
        signal: Signal = strategy.check_signal(window)
        bar = window[-1]

        # --- Проверяем SL/TP для всех открытых позиций ---
        closed_this_bar = []
        for pos in open_positions:
            if pos.side == "Buy":
                hit_sl = bar.low <= pos.stop_price
                hit_tp = bar.high >= pos.take_price
            else:
                hit_sl = bar.high >= pos.stop_price
                hit_tp = bar.low <= pos.take_price

            if hit_sl:
                exit_price = pos.stop_price
                reason = "SL"
            elif hit_tp:
                exit_price = pos.take_price
                reason = "TP"
            else:
                continue

            move = (
                (exit_price - pos.entry_price) / pos.entry_price * 100
                if pos.side == "Buy"
                else (pos.entry_price - exit_price) / pos.entry_price * 100
            )
            equity += move
            result.trades.append(
                Trade(
                    side=pos.side,
                    entry_price=pos.entry_price,
                    exit_price=exit_price,
                    entry_time=pos.entry_time,
                    exit_time=bar.open_time,
                    pnl_pct=move,
                    exit_reason=reason,
                ),
            )
            closed_this_bar.append(pos)

        for pos in closed_this_bar:
            open_positions.remove(pos)

        # --- Входим по сигналу ---
        if signal.action in ("buy", "sell"):
            side = signal.action.capitalize()
            entry_price = bar.close
            entry_time = bar.open_time
            if signal.stop_loss is not None and signal.take_profit is not None:
                sp = signal.stop_loss
                tp = signal.take_profit
            else:
                if side == "Buy":
                    sp = entry_price * (1 - sl_pct_fallback / 100.0)
                    tp = entry_price * (1 + tp_pct_fallback / 100.0)
                else:
                    sp = entry_price * (1 + sl_pct_fallback / 100.0)
                    tp = entry_price * (1 - tp_pct_fallback / 100.0)
            open_positions.append(OpenPos(side=side, entry_price=entry_price,
                                          entry_time=entry_time, stop_price=sp, take_price=tp))

        result.equity_curve.append(equity)
        result.equity_times.append(bar.open_time)

    return result


async def fetch_candles(
    config: Config,
    limit: int,
    start_ms: int | None = None,
    end_ms: int | None = None,
) -> list[Candle]:
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
            end_ts = end_ms
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

        # Фильтрация по start_ms если задано
        if start_ms is not None:
            all_candles = [c for c in all_candles if c.open_time >= start_ms]

        return all_candles
    finally:
        client.close()


def print_stats(
    result: BacktestResult, strategy_name: str, symbol: str, timeframe: str
) -> None:
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


def plot_equity(
    result: BacktestResult, strategy_name: str, symbol: str, timeframe: str
) -> None:
    """График equity curve."""
    from datetime import datetime, timezone

    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    times = [
        datetime.fromtimestamp(t / 1000, tz=timezone.utc) for t in result.equity_times
    ]

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
        peak = max(peak, val)
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


def plot_trades(
    result: BacktestResult, strategy_name: str, symbol: str, timeframe: str
) -> None:
    """График цен со сделками: свечи + маркеры входов/выходов."""
    from datetime import datetime, timezone

    import matplotlib.dates as mdates
    import matplotlib.patches as mpatches
    import matplotlib.pyplot as plt

    candles = result.candles_used
    if not candles:
        return

    times = [
        datetime.fromtimestamp(c.open_time / 1000, tz=timezone.utc) for c in candles
    ]
    opens = [c.open for c in candles]
    highs = [c.high for c in candles]
    lows = [c.low for c in candles]
    closes = [c.close for c in candles]

    fig, ax = plt.subplots(figsize=(16, 8))
    fig.suptitle(f"Сделки: {strategy_name} | {symbol} | {timeframe}", fontsize=14)

    # Свечи
    width = (times[1] - times[0]) * 0.6 if len(times) > 1 else 0
    for i in range(len(times)):
        color = "#4CAF50" if closes[i] >= opens[i] else "#F44336"
        body_bottom = min(opens[i], closes[i])
        body_height = abs(closes[i] - opens[i])
        ax.bar(
            times[i],
            body_height,
            bottom=body_bottom,
            width=width,
            color=color,
            edgecolor=color,
            linewidth=0.5,
        )
        ax.vlines(times[i], lows[i], highs[i], color=color, linewidth=0.8)

    # Маркеры сделок
    for trade in result.trades:
        entry_dt = datetime.fromtimestamp(trade.entry_time / 1000, tz=timezone.utc)
        exit_dt = datetime.fromtimestamp(trade.exit_time / 1000, tz=timezone.utc)

        if trade.side == "Buy":
            ax.scatter(
                entry_dt,
                trade.entry_price,
                marker="^",
                color="#2196F3",
                s=120,
                zorder=5,
                edgecolors="black",
                linewidths=0.5,
            )
            exit_color = "#4CAF50" if trade.pnl_pct > 0 else "#F44336"
            ax.scatter(
                exit_dt,
                trade.exit_price,
                marker="v",
                color=exit_color,
                s=120,
                zorder=5,
                edgecolors="black",
                linewidths=0.5,
            )
            ax.plot(
                [entry_dt, exit_dt],
                [trade.entry_price, trade.exit_price],
                color=exit_color,
                linewidth=1,
                alpha=0.6,
                linestyle="--",
            )
        else:
            ax.scatter(
                entry_dt,
                trade.entry_price,
                marker="v",
                color="#FF9800",
                s=120,
                zorder=5,
                edgecolors="black",
                linewidths=0.5,
            )
            exit_color = "#4CAF50" if trade.pnl_pct > 0 else "#F44336"
            ax.scatter(
                exit_dt,
                trade.exit_price,
                marker="^",
                color=exit_color,
                s=120,
                zorder=5,
                edgecolors="black",
                linewidths=0.5,
            )
            ax.plot(
                [entry_dt, exit_dt],
                [trade.entry_price, trade.exit_price],
                color=exit_color,
                linewidth=1,
                alpha=0.6,
                linestyle="--",
            )

    ax.set_ylabel("Цена")
    ax.set_xlabel("Время")
    ax.grid(True, alpha=0.3)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d %b"))
    ax.tick_params(axis="x", rotation=30)

    legend_elements = [
        mpatches.Patch(facecolor="#4CAF50", label="Бычья свеча"),
        mpatches.Patch(facecolor="#F44336", label="Медвежья свеча"),
        plt.Line2D(
            [0],
            [0],
            marker="^",
            color="w",
            markerfacecolor="#2196F3",
            markersize=10,
            label="Вход Buy",
        ),
        plt.Line2D(
            [0],
            [0],
            marker="v",
            color="w",
            markerfacecolor="#FF9800",
            markersize=10,
            label="Вход Sell",
        ),
        plt.Line2D(
            [0],
            [0],
            marker="o",
            color="w",
            markerfacecolor="#4CAF50",
            markersize=10,
            label="Выход profit",
        ),
        plt.Line2D(
            [0],
            [0],
            marker="o",
            color="w",
            markerfacecolor="#F44336",
            markersize=10,
            label="Выход loss",
        ),
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
        choices=["trend", "flat", "grid_flat", "yrovni", "impulse", "zero"],
        help="какую стратегию тестировать",
    )
    parser.add_argument(
        "--limit", type=int, default=500, help="сколько свечей брать (пагинация >1000)"
    )
    parser.add_argument("--symbol", default=None, help="пара (по умолчанию из .env)")
    parser.add_argument("--timeframe", default=None, help="таймфрейм")
    parser.add_argument("--sl", type=float, default=None, help="SL %% (fallback)")
    parser.add_argument("--tp", type=float, default=None, help="TP %% (fallback)")
    parser.add_argument("--no-chart", action="store_true", help="не показывать график")
    parser.add_argument("--start-date", default=None, help="начальная дата (YYYY-MM-DD)")
    parser.add_argument("--end-date", default=None, help="конечная дата (YYYY-MM-DD)")
    parser.add_argument("--save-report", default=None, help="путь для сохранения отчёта (.txt)")
    args = parser.parse_args()

    setup_logging("logs/backtest.log", "WARNING")

    # Загружаем .env бота (robot_xxx/), а не корневой
    bot_dir = Path(__file__).parent / f"robot_{args.strategy}"
    if not bot_dir.exists():
        bot_dir = Path(__file__).parent
    load_bot_env(bot_dir)

    config = Config()
    if args.symbol:
        config.symbol = args.symbol
    if args.timeframe:
        config.timeframe = args.timeframe
    sl_pct = args.sl if args.sl is not None else config.stop_loss_pct
    tp_pct = args.tp if args.tp is not None else config.take_profit_pct
    config.validate()

    # Конвертация дат в timestamp
    from datetime import datetime, timezone
    start_ms = None
    end_ms = None
    if args.start_date:
        start_ms = int(datetime.strptime(args.start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)
    if args.end_date:
        end_dt = datetime.strptime(args.end_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        end_ms = int(end_dt.timestamp() * 1000)
        # Для end_date берём конец дня (23:59:59.999)
        end_ms = end_ms + 86400000 - 1

    # Если заданы даты — считаем limit автоматически
    limit = args.limit
    if start_ms and end_ms:
        tf_minutes = int(config.timeframe) if config.timeframe.isdigit() else 5
        days = (end_ms - start_ms) / 86400000
        limit = int(days * 24 * 60 / tf_minutes) + 100  # +100 запас
        print(f"Даты: {args.start_date} → {args.end_date} | {days:.0f} дней | ~{limit} свечей")

    strategy = make_strategy(args.strategy)
    candles = asyncio.run(fetch_candles(config, limit, start_ms=start_ms, end_ms=end_ms))
    result = run_backtest(candles, strategy, sl_pct, tp_pct)

    print_stats(result, strategy.name, config.symbol, config.timeframe)

    # Сохранение отчёта в файл
    if args.save_report:
        from datetime import datetime, timezone
        report_path = Path(args.save_report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(f"Backtest Report: {strategy.name} | {config.symbol} | {config.timeframe}\n")
            f.write(f"Date range: {args.start_date or 'N/A'} → {args.end_date or 'N/A'}\n")
            f.write(f"Candles: {len(candles)}\n")
            f.write("=" * 55 + "\n")
            f.write(f"  Trades:         {result.total_trades}\n")
            f.write(f"  Wins:           {result.wins} ({result.win_rate:.1f}%)\n")
            f.write(f"  Losses:         {result.losses}\n")
            f.write(f"  Avg Win:        {result.avg_win:+.3f}%\n")
            f.write(f"  Avg Loss:       {result.avg_loss:+.3f}%\n")
            f.write(f"  Profit Factor:  {result.profit_factor:.2f}\n")
            f.write(f"  Max Streak:     +{result.max_win_streak} / -{result.max_loss_streak}\n")
            f.write("-" * 55 + "\n")
            f.write(f"  Total PnL:      {result.total_pnl:+.2f}%\n")
            f.write(f"  Max Drawdown:   {result.max_drawdown:.2f}%\n")
            f.write("=" * 55 + "\n\n")
            f.write("Trades detail:\n")
            for i, t in enumerate(result.trades, 1):
                from datetime import datetime, timezone
                entry_dt = datetime.fromtimestamp(t.entry_time / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
                exit_dt = datetime.fromtimestamp(t.exit_time / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
                f.write(f"  {i:3d}. {t.side:4s} | {entry_dt} → {exit_dt} | entry={t.entry_price:.5f} exit={t.exit_price:.5f} | {t.pnl_pct:+.3f}% ({t.exit_reason})\n")
        print(f"\nОтчёт сохранён: {report_path}")

    if not args.no_chart and result.equity_curve:
        plot_equity(result, strategy.name, config.symbol, config.timeframe)
        plot_trades(result, strategy.name, config.symbol, config.timeframe)


if __name__ == "__main__":
    main()
