import json

with open(r'C:\Users\79095\.local\share\opencode\tool-output\tool_09eedcc280019YH5AmD59ds4vD', 'r') as f:
    data = json.load(f)

tickers = data.get('result', {}).get('list', [])
usdt_symbols = [t['symbol'] for t in tickers if t['symbol'].endswith('USDT') and t.get('contractType') == 'LinearPerpetual']

top30 = [
    'BTCUSDT','ETHUSDT','BNBUSDT','SOLUSDT','XRPUSDT','DOGEUSDT','ADAUSDT','TRXUSDT',
    'DOTUSDT','LINKUSDT','MATICUSDT','UNIUSDT','LTCUSDT','ATOMUSDT','FILUSDT','APTUSDT',
    'ARBUSDT','OPUSDT','NEARUSDT','AAVEUSDT','MKRUSDT','INJUSDT','SUIUSDT','TIAUSDT',
    'SEIUSDT','WLDUSDT','PEPEUSDT','FETUSDT','RNDRUSDT','GALAUSDT'
]

candidates = [s for s in usdt_symbols if s not in top30]
print(f'Total USDT linear perpetual: {len(usdt_symbols)}')
print(f'After top30 filter: {len(candidates)}')
print(f'Examples: {candidates[:30]}')

with open(r'D:\Vcode\TRD bot\bybit_symbols.txt', 'w') as f:
    for s in candidates:
        f.write(s + '\n')
