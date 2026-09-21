"""Stepwise filter relaxation - no top-15."""

import asyncio
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.config import Config, load_bot_env
from core.bybit_client import BybitClient
from robot_TEST.strategy import _efficiency_ratio, _choppiness_index, _adx

load_bot_env(Path("robot_TEST"))

# Топ-15 ИСКЛЮЧАЕМ
TOP15 = {
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
    "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "DOTUSDT", "LINKUSDT",
    "TRXUSDT", "MATICUSDT", "SHIBUSDT", "LTCUSDT", "TONUSDT",
}

SYMBOLS = [
    "NEARUSDT", "APTUSDT", "ARBUSDT", "OPUSDT", "SUIUSDT",
    "SEIUSDT", "TIAUSDT", "INJUSDT", "FETUSDT", "WLDUSDT",
    "RENDERUSDT", "FILUSDT", "ATOMUSDT", "ETCUSDT",
    "UNIUSDT", "AAVEUSDT", "MKRUSDT", "CRVUSDT",
    "WIFUSDT", "JUPUSDT", "BCHUSDT", "EOSUSDT", "XTZUSDT",
    "ALGOUSDT", "SANDUSDT", "MANAUSDT", "GALAUSDT", "AXSUSDT",
    "IMXUSDT", "PENDLEUSDT", "STXUSDT", "RUNEUSDT",
    "SXPUSDT", "MASKUSDT", "ENSUSDT", "LDOUSDT",
    "VETUSDT", "FTMUSDT", "HBARUSDT", "XLMUSDT",
    "FLUXUSDT", "KAVAUSDT", "COMPUSDT", "SNXUSDT",
    "DYDXUSDT", "GMXUSDT", "SUSHIUSDT", "1INCHUSDT",
    "BATUSDT", "CHZUSDT", "ENJUSDT", "GRTUSDT",
    "SKLUSDT", "STORJUSDT", "TOMOUSDT", "TRBUSDT",
    "UMAUSDT", "UNFIUSDT", "ZILUSDT",
    "CVCUSDT", "BLZUSDT",
]


async def scan():
    config = Config()
    data = {}  # symbol -> (er, ci, adx_m5, vol_ratio)

    for sym in SYMBOLS:
        if sym in TOP15:
            continue
        try:
            client = BybitClient(config)
            candles = await client.get_klines(sym, "5", 2000)
            client.close()
            if len(candles) < 300:
                continue

            closes = [c.close for c in candles]
            highs = [c.high for c in candles]
            lows = [c.low for c in candles]
            volumes = [c.volume for c in candles]

            er = _efficiency_ratio(closes, 10)
            ci = _choppiness_index(highs, lows, closes, 14)
            adx = _adx(highs, lows, closes, 14)

            vol_fast = sum(volumes[-20:]) / 20
            vol_slow = sum(volumes[-100:]) / 100
            vol_ratio = vol_fast / vol_slow if vol_slow > 0 else 1

            data[sym] = (er[-1], ci[-1], adx[-1], vol_ratio)
        except Exception:
            pass

    # Конфигурации фильтров (ослабление по шагам)
    configs = [
        ("ORIGINAL", {"er": 0.30, "ci": 60, "adx": 20, "vol": 0.60}),
        ("Step 1: ADX 20->25", {"er": 0.30, "ci": 60, "adx": 25, "vol": 0.60}),
        ("Step 2: ADX 20->30", {"er": 0.30, "ci": 60, "adx": 30, "vol": 0.60}),
        ("Step 3: CI 60->55", {"er": 0.30, "ci": 55, "adx": 30, "vol": 0.60}),
        ("Step 4: Vol 0.60->0.70", {"er": 0.30, "ci": 55, "adx": 30, "vol": 0.70}),
        ("Step 5: ER 0.30->0.35", {"er": 0.35, "ci": 55, "adx": 30, "vol": 0.70}),
    ]

    for name, thresholds in configs:
        print("=" * 75)
        print(f"  {name}")
        print(f"  ER<{thresholds['er']}  CI>{thresholds['ci']}  ADX<{thresholds['adx']}  Vol<{thresholds['vol']}")
        print("=" * 75)

        passed = []
        for sym, (er, ci, adx, vol) in data.items():
            flags = []
            if er < thresholds["er"]:
                flags.append("ER")
            if ci > thresholds["ci"]:
                flags.append("CI")
            if adx < thresholds["adx"]:
                flags.append("ADX")
            if vol < thresholds["vol"]:
                flags.append("Vol")

            total = len(flags)
            if total >= 3:
                passed.append((sym, er, ci, adx, vol, total, flags))

        if passed:
            passed.sort(key=lambda x: -x[5])
            for sym, er, ci, adx, vol, total, flags in passed:
                marker = " >>>" if total == 4 else ""
                print(f"  {sym:16s}  ER={er:.3f}  CI={ci:.1f}  ADX={adx:.1f}  Vol={vol:.3f}  [{'+'.join(flags)}]{marker}")
        else:
            print("  No coins (3/4 or 4/4)")
        print()


asyncio.run(scan())
