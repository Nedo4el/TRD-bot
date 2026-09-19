"""Fine-tune J config: SL/TP variants."""
from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.stdout.reconfigure(encoding="utf-8")

from pybit.unified_trading import HTTP
from core.bybit_client import Candle
from gridsearch_profit50 import fetch_candles, run_backtest, ALL_SYMBOLS

API_KEY = "kOFDh0YrjjbkiC8VSp"
API_SECRET = "S2f2wUhMMDSqufEcgGzikwyZ1YH7yUsdyf5A"


def main():
    session = HTTP(testnet=False, api_key=API_KEY, api_secret=API_SECRET)
    print("Loading...")
    cd = {}
    for s in ALL_SYMBOLS:
        cd[s] = fetch_candles(session, s)
        print(f"  {s}: {len(cd[s])}")

    cfgs = [
        ("J orig: SL=8% TP=6%", [-3,-5,-8,3,5,8], 12.0, 8.0, "fixed_6"),
        ("J2: SL=7% TP=6%",     [-3,-5,-8,3,5,8], 12.0, 7.0, "fixed_6"),
        ("J3: SL=6% TP=6%",     [-3,-5,-8,3,5,8], 12.0, 6.0, "fixed_6"),
        ("J4: SL=7% TP=7%",     [-3,-5,-8,3,5,8], 12.0, 7.0, "fixed_7"),
        ("J5: SL=6% TP=7%",     [-3,-5,-8,3,5,8], 12.0, 6.0, "fixed_7"),
        ("J6: SL=5% TP=6%",     [-3,-5,-8,3,5,8], 12.0, 5.0, "fixed_6"),
        ("J7: SL=5% TP=5%",     [-3,-5,-8,3,5,8], 12.0, 5.0, "fixed_5"),
        ("J8: SL=7% TP=8%",     [-3,-5,-8,3,5,8], 12.0, 7.0, "fixed_8"),
        ("J9: SL=6% TP=8%",     [-3,-5,-8,3,5,8], 12.0, 6.0, "fixed_8"),
        ("J10: SL=8% TP=7%",    [-3,-5,-8,3,5,8], 12.0, 8.0, "fixed_7"),
        ("J11: SL=8% TP=8%",    [-3,-5,-8,3,5,8], 12.0, 8.0, "fixed_8"),
    ]

    print()
    hdr = "  {:<30s} {:>8s} {:>6s} {:>7s} {:>5s}".format("Config", "PnL", "Win%", "DD", "Prof")
    print(hdr)
    print("  " + "-" * 60)

    for label, levels, rng, sl, tp in cfgs:
        ps, ws, ds = [], [], []
        for s, c in cd.items():
            p, w, _, _, d = run_backtest(c, levels, rng, sl, tp, 3)
            ps.append(p); ws.append(w); ds.append(d)
        ap = sum(ps)/len(ps)
        aw = sum(ws)/len(ws)
        ad = sum(ds)/len(ds)
        pf = sum(1 for x in ps if x > 0)
        m = " <<<" if ap > 0 else ""
        print("  {:<30s} {:>+7.1f}% {:>5.0f}% {:>6.1f}% {:>4d}/6{}".format(
            label, ap, aw, ad, pf, m))


if __name__ == "__main__":
    main()
