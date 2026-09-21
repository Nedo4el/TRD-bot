import asyncio, sys, time
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path('.').resolve()))

from core.config import Config, load_bot_env
from core.bybit_client import BybitClient
from robot_TEST.strategy import TestStrategy, TestConfig
from backtest import run_backtest

load_bot_env(Path('robot_TEST'))

SYMBOLS = [
    'BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'XRPUSDT', 'DOGEUSDT',
    'ADAUSDT', 'AVAXUSDT', 'DOTUSDT', 'LINKUSDT', 'TONUSDT',
]
START = '2026-06-23'
END = '2026-09-21'
REPORT_DIR = Path(r'C:\Users\79095\Desktop\trd\Отчеты скринера')

from pybit.unified_trading import HTTP

session = HTTP(testnet=False)

def fetch_candles_sync(symbol: str, limit: int, end_ms: int | None = None):
    all_candles = []
    remaining = limit
    end_param = end_ms

    while remaining > 0:
        batch = min(remaining, 1000)
        params = {
            'category': 'linear',
            'symbol': symbol,
            'interval': '5',
            'limit': batch,
        }
        if end_param:
            params['end'] = end_param

        for attempt in range(5):
            try:
                resp = session.get_kline(**params)
                break
            except Exception as e:
                wait = 3 * (attempt + 1)
                print(f'    Rate limit, waiting {wait}s...')
                time.sleep(wait)
        else:
            print(f'    FAILED after retries')
            break

        data = resp.get('result', {}).get('list', [])
        if not data:
            break

        from core.bybit_client import Candle
        for row in reversed(data):
            c = Candle(
                open_time=float(row[0]),
                open=float(row[1]),
                high=float(row[2]),
                low=float(row[3]),
                close=float(row[4]),
                volume=float(row[5]),
            )
            all_candles.insert(0, c)

        remaining -= len(data)
        end_param = float(data[0][0]) - 1

        if len(data) < batch:
            break

        time.sleep(1.5)

    return all_candles

def main():
    start_ms = int(datetime.strptime(START, '%Y-%m-%d').replace(tzinfo=timezone.utc).timestamp() * 1000)
    end_ms = int(datetime.strptime(END, '%Y-%m-%d').replace(tzinfo=timezone.utc).timestamp() * 1000) + 86400000 - 1
    days = (end_ms - start_ms) / 86400000
    limit = int(days * 24 * 60 / 5) + 100

    results = []

    for i, sym in enumerate(SYMBOLS):
        print(f'\n[{i+1}/10] {sym} ...')
        try:
            candles = fetch_candles_sync(sym, limit, end_ms=end_ms)
            print(f'  Candles: {len(candles)}')

            if len(candles) < 400:
                print(f'  SKIP: too few candles')
                results.append({'symbol': sym, 'trades': 0, 'pnl': 0, 'max_dd': 0, 'win_rate': 0, 'pf': 0, 'error': 'too few candles'})
                time.sleep(3)
                continue

            strategy = TestStrategy(TestConfig(poc_lookback=300))
            result = run_backtest(candles, strategy, 10.0, 10.0)

            row = {
                'symbol': sym,
                'trades': result.total_trades,
                'wins': result.wins,
                'win_rate': result.win_rate,
                'pnl': result.total_pnl,
                'max_dd': result.max_drawdown,
                'avg_win': result.avg_win,
                'avg_loss': result.avg_loss,
                'pf': result.profit_factor,
            }
            results.append(row)

            rpt_path = REPORT_DIR / f'grid_flat_{sym}_90d_{datetime.now().strftime("%Y%m%d")}.txt'
            rpt_path.parent.mkdir(parents=True, exist_ok=True)
            with open(rpt_path, 'w', encoding='utf-8') as f:
                f.write(f'Grid Flat Backtest: {sym} | 5m | {START} -> {END}\n')
                f.write(f'Candles: {len(candles)}\n')
                f.write('=' * 50 + '\n')
                f.write(f'  Trades:    {result.total_trades}\n')
                f.write(f'  Wins:      {result.wins} ({result.win_rate:.1f}%)\n')
                f.write(f'  Losses:    {result.losses}\n')
                f.write(f'  Avg Win:   {result.avg_win:+.3f}%\n')
                f.write(f'  Avg Loss:  {result.avg_loss:+.3f}%\n')
                f.write(f'  PF:        {result.profit_factor:.2f}\n')
                f.write(f'  PnL:       {result.total_pnl:+.2f}%\n')
                f.write(f'  Max DD:    {result.max_drawdown:.2f}%\n')
            print(f'  Trades={result.total_trades} PnL={result.total_pnl:+.2f}% WR={result.win_rate:.0f}% DD={result.max_drawdown:.2f}%')

        except Exception as e:
            print(f'  ERROR: {e}')
            results.append({'symbol': sym, 'trades': 0, 'pnl': 0, 'max_dd': 0, 'win_rate': 0, 'pf': 0, 'error': str(e)})

        time.sleep(3)

    print('\n\n========== GRID FLAT 90d ==========')
    print(f'{"Symbol":<15s} {"Trades":>6s} {"Win%":>7s} {"PnL":>9s} {"MaxDD":>8s} {"PF":>7s}')
    print('-' * 55)
    profitable = 0
    total_pnl = 0
    cnt = 0
    for r in results:
        if r.get('error'):
            print(f'{r["symbol"]:<15s} SKIP ({r["error"][:30]})')
        else:
            tag = ' *' if r['pnl'] > 0 else ''
            print(f'{r["symbol"]:<15s} {r["trades"]:>6d} {r["win_rate"]:>6.1f}% {r["pnl"]:>+8.2f}% {r["max_dd"]:>7.2f}% {r["pf"]:>7.2f}{tag}')
            if r['pnl'] > 0:
                profitable += 1
            total_pnl += r['pnl']
            cnt += 1
    print('-' * 55)
    print(f'Profitable: {profitable}/{cnt}')
    if cnt:
        print(f'Average PnL: {total_pnl/cnt:+.2f}%')

if __name__ == '__main__':
    main()
