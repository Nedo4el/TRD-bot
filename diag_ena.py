import asyncio, sys
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path('.').resolve()))

from core.config import Config, load_bot_env
from robot_TEST.strategy import TestStrategy, TestConfig
from backtest import run_backtest, fetch_candles, Trade

load_bot_env(Path('robot_TEST'))

async def main():
    config = Config()
    config.symbol = 'ENAUSDT'
    config.timeframe = '5'
    candles = await fetch_candles(config, 9000,
        start_ms=int(datetime(2026, 8, 21, tzinfo=timezone.utc).timestamp() * 1000),
        end_ms=int(datetime(2026, 9, 20, tzinfo=timezone.utc).timestamp() * 1000) + 86400000 - 1,
    )
    print(f'Candles: {len(candles)}')

    strategy = TestStrategy(TestConfig(poc_lookback=600))
    result = run_backtest(candles, strategy, 10.0, 10.0)

    print(f'\nTrades: {result.total_trades}')
    print(f'Wins: {result.wins} ({result.win_rate:.1f}%)')
    print(f'PnL: {result.total_pnl:+.2f}%')

    # Анализ сделок
    buys = [t for t in result.trades if t.side == 'Buy']
    sells = [t for t in result.trades if t.side == 'Sell']
    print(f'\nBuys: {len(buys)}, Sells: {len(sells)}')

    # PnL распределение
    pnls = [t.pnl_pct for t in result.trades]
    print(f'\nPnL min: {min(pnls):+.3f}%')
    print(f'PnL max: {max(pnls):+.3f}%')
    print(f'PnL avg: {sum(pnls)/len(pnls):+.3f}%')

    # Все убыточные сделки
    losses = [t for t in result.trades if t.pnl_pct <= 0]
    print(f'\nLosses: {len(losses)}')
    for t in losses[:20]:
        print(f'  {t.side} entry={t.entry_price:.4f} exit={t.exit_price:.4f} pnl={t.pnl_pct:+.3f}% reason={t.exit_reason}')

    # Проверка:有多少 сделок закрылись по TP vs SL
    tp_trades = [t for t in result.trades if t.exit_reason == 'TP']
    sl_trades = [t for t in result.trades if t.exit_reason == 'SL']
    print(f'\nTP: {len(tp_trades)} ({len(tp_trades)/len(result.trades)*100:.1f}%)')
    print(f'SL: {len(sl_trades)} ({len(sl_trades)/len(result.trades)*100:.1f}%)')

    # Проверка:.entry_price == bar.close для каждой сделки
    print(f'\nСредний entry: {sum(t.entry_price for t in result.trades)/len(result.trades):.4f}')
    print(f'Средний exit:  {sum(t.exit_price for t in result.trades)/len(result.trades):.4f}')

    # Симуляция с комиссиями
    FEE = 0.05  # 0.05% taker fee
    pnl_with_fees = sum(t.pnl_pct - FEE * 2 for t in result.trades)
    print(f'\nPnL без комиссий: {result.total_pnl:+.2f}%')
    print(f'PnL с комиссиями 0.05% x2: {pnl_with_fees:+.2f}%')

    # Equity curve анализ
    eq = result.equity_curve
    if eq:
        print(f'\nEquity start: {eq[0]:.2f}')
        print(f'Equity max:   {max(eq):.2f}')
        print(f'Equity min:   {min(eq):.2f}')
        print(f'Equity end:   {eq[-1]:.2f}')
        print(f'MaxDD: {result.max_drawdown:.2f}%')

asyncio.run(main())
