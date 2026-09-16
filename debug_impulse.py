"""Debug: check candle data and impulse detection."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bot_screener_impulse.main import _load_config
from bot_screener_klin.fetcher import Fetcher
from core.logger import setup_logging
from core.metrics import ScreenerMetrics

async def debug():
    setup_logging("logs/screener_impulse.log", "INFO")
    cfg, impulse_cfg = _load_config()
    
    fetcher = Fetcher(
        api_key=cfg["api_key"],
        api_secret=cfg["api_secret"],
        testnet=cfg["testnet"],
        metrics=ScreenerMetrics(),
    )
    
    # Get first few symbols
    filtered = await fetcher.get_filtered_symbols(cfg["min_turnover_24h"])
    exclude = set(cfg["exclude_symbols"])
    filtered = [(s, t) for s, t in filtered if s not in exclude]
    
    print(f"Symbols to scan: {len(filtered)}")
    
    # Check first 5 symbols
    for sym, _ in filtered[:5]:
        candles = await fetcher.get_klines(
            symbol=sym,
            interval=cfg["timeframe"],
            limit=cfg["lookback_bars"],
        )
        
        if not candles:
            print(f"{sym}: no candles")
            continue
        
        print(f"\n{sym}: {len(candles)} candles")
        
        # Find biggest moves
        moves = []
        for i, c in enumerate(candles):
            op = float(c["open"])
            cl = float(c["close"])
            if op <= 0:
                continue
            body = cl - op
            move_pct = abs(body) / op * 100
            moves.append((i, move_pct, "LONG" if cl > op else "SHORT"))
        
        # Top 5 moves
        moves.sort(key=lambda x: x[1], reverse=True)
        print(f"  Top 5 moves:")
        for idx, move, direction in moves[:5]:
            print(f"    [{idx}] {direction}: {move:.2f}%")
    
    # Try analyze first symbol with low threshold
    from bot_screener_impulse.scanner import ImpulseConfig, analyze_symbol
    
    sym, _ = filtered[0]
    candles = await fetcher.get_klines(
        symbol=sym,
        interval=cfg["timeframe"],
        limit=cfg["lookback_bars"],
    )
    
    # Test with 1% threshold
    cfg_low = ImpulseConfig(min_move_pct=1.0, confirmation_candles=0)
    result = analyze_symbol(sym, cfg["timeframe"], candles, cfg=cfg_low)
    print(f"\nTest with 1% threshold, no confirmation: {result}")
    
    # Test with 4% threshold, no confirmation
    cfg_4 = ImpulseConfig(min_move_pct=4.0, confirmation_candles=0)
    result4 = analyze_symbol(sym, cfg["timeframe"], candles, cfg=cfg_4)
    print(f"Test with 4% threshold, no confirmation: {result4}")

asyncio.run(debug())
