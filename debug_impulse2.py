"""Debug: check why confirmation fails."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bot_screener_impulse.main import _load_config
from bot_screener_impulse.scanner import ImpulseConfig, ImpulseSignal, _check_confirmation
from bot_screener_klin.fetcher import Fetcher
from core.logger import setup_logging
from core.metrics import ScreenerMetrics

async def debug():
    setup_logging("logs/screener_impulse.log", "INFO")
    cfg, _ = _load_config()
    
    fetcher = Fetcher(
        api_key=cfg["api_key"],
        api_secret=cfg["api_secret"],
        testnet=cfg["testnet"],
        metrics=ScreenerMetrics(),
    )
    
    filtered = await fetcher.get_filtered_symbols(cfg["min_turnover_24h"])
    exclude = set(cfg["exclude_symbols"])
    filtered = [(s, t) for s, t in filtered if s not in exclude]
    
    # Test LSKUSDT and AKEUSDT specifically
    for target_sym in ["LSKUSDT", "AKEUSDT"]:
        for sym, _ in filtered:
            if sym != target_sym:
                continue
            candles = await fetcher.get_klines(
                symbol=sym,
                interval=cfg["timeframe"],
                limit=cfg["lookback_bars"],
            )
            
            if not candles:
                print(f"{sym}: no candles")
                continue
            
            print(f"\n{'='*60}")
            print(f"{sym}: {len(candles)} candles")
            
            # Find all >= 4% moves
            for i, c in enumerate(candles):
                op = float(c["open"])
                cl = float(c["close"])
                hi = float(c["high"])
                lo = float(c["low"])
                if op <= 0:
                    continue
                body = cl - op
                move_pct = abs(body) / op * 100
                
                if move_pct >= 4.0:
                    direction = "LONG" if cl > op else "SHORT"
                    confirmed = _check_confirmation(candles, i, 2, direction)
                    
                    # Check what next candles look like
                    next_info = []
                    for j in range(1, 3):
                        idx = i + j
                        if idx < len(candles):
                            nc = candles[idx]
                            nlo = float(nc["low"])
                            nhi = float(nc["high"])
                            next_info.append(f"  [{idx}] L={nlo:.4f} H={nhi:.4f}")
                    
                    body_top = max(op, cl)
                    body_bottom = min(op, cl)
                    
                    print(f"\n  [{i}] {direction} {move_pct:.2f}% | open={op:.4f} close={cl:.4f}")
                    print(f"    body_bottom={body_bottom:.4f} body_top={body_top:.4f}")
                    print(f"    confirmed={confirmed}")
                    for info in next_info:
                        print(info)

asyncio.run(debug())
