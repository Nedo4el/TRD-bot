import asyncio, sys
from pathlib import Path
sys.path.insert(0, str(Path('.').resolve()))
from pybit.unified_trading import HTTP

session = HTTP(testnet=False)

resp = session.get_tickers(category="linear")
tickers = resp['result']['list']

usdt = [t for t in tickers if t['symbol'].endswith('USDT')]
usdt.sort(key=lambda t: float(t.get('volume24h', 0)), reverse=True)

for i, t in enumerate(usdt[:15]):
    vol = float(t.get('volume24h', 0))
    sym = t['symbol']
    print(f'{i+1:2d}. {sym:15s} Vol24h: ${vol:,.0f}')
