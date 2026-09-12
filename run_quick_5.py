"""Быстрый прогон 5 скринеров — по 1 скану + отчёты."""

from __future__ import annotations

import asyncio
import os
import sys
import time
from datetime import datetime, timedelta, timezone

MSK = timezone(timedelta(hours=3))
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_PROJECT_ROOT))

REPORTS_DIR = Path(r"C:\Users\79095\Desktop\trd\Отчеты скринера")

from core.config import get_env_bool, get_env_float, get_env_int, load_bot_env
from core.logger import get_logger, setup_logging
from core.metrics import ScreenerMetrics

logger = get_logger(__name__)

UZKIY_DIR = _PROJECT_ROOT / "bot_screener_uzkiy"
YROVNI_DIR = _PROJECT_ROOT / "bot_screener_yrovni"
KRUGLOE_DIR = _PROJECT_ROOT / "bot_screener_krugloe"
IMPULSE_DIR = _PROJECT_ROOT / "bot_screener_impulse"
ZAKONOMER_DIR = _PROJECT_ROOT / "bot_screener_zakonomer"

_SCREENING_ENV_KEYS = [
    "BYBIT_API_KEY", "BYBIT_API_SECRET", "TESTNET", "TIMEFRAME", "TIMEFRAMES",
    "SCAN_INTERVAL", "MIN_TURNOVER_24H", "LOOKBACK_BARS", "MIN_PROBABILITY",
    "EXCLUDE_SYMBOLS", "ANALYSIS_PERIOD", "VOLUME_LOOKBACK", "OBV_LOOKBACK",
    "BB_PERIOD", "BB_DEV", "RSI_PERIOD", "SMART_MONEY_LOOKBACK", "MAX_PRICE",
    "MIN_TOTAL_VOLUME_USD", "RANGE_MAX", "RANGE_MIN", "VOLUME_SPIKE",
    "BB_COMPRESSION", "MIN_VOLUME_USD", "MIN_TRADES", "MAX_SPREAD",
    "MIN_DAYS_LISTED", "MAX_VOLATILITY_3D", "MAX_DROP_7D", "MIN_PUMP_PROBABILITY",
    "WEIGHT_RANGE", "WEIGHT_VOLUME", "WEIGHT_OBV", "WEIGHT_BB",
    "WEIGHT_SMART_MONEY", "WEIGHT_OUTFLOW", "WEIGHT_RSI", "WEIGHT_LIQUIDITY",
    "EXTREMA_WINDOW", "SLOPE_UP", "SLOPE_DOWN", "SLOPE_FLAT",
    "COMPRESSION_RATIO", "TRIANGLE_LOOKBACK", "MIN_RANGE_PCT",
    "BB_LOOKBACK", "ATR_LOOKBACK", "ATR_DROP_THRESHOLD",
    "ADX_FALSE_THRESHOLD", "ADX_STRONG_THRESHOLD", "ATR_PERIOD", "ADX_PERIOD",
    "SPREAD_MAX_PCT", "MIN_SCORE", "PROXIMITY_PCT",
    "VOLUME_SMA_PERIOD", "VOLUME_SPIKE_MULTIPLIER", "CANDLE_WIDTH_MIN",
    "CANDLE_WIDTH_MAX", "DELTA_SMA_PERIOD", "DELTA_SPIKE_MULTIPLIER",
    "CONFIRMATION_CANDLES", "LOOKBACK_DAYS", "SPIKE_STD_MULTIPLIER",
]


def _clear_screener_env() -> None:
    for key in _SCREENING_ENV_KEYS:
        os.environ.pop(key, None)


def _dedup(signals: list, key_fn) -> list:
    seen: dict = {}
    for s in signals:
        seen[key_fn(s)] = s
    return list(seen.values())


# ── configs ──────────────────────────────────────────────

def _cfg_uzkiy():
    _clear_screener_env(); load_bot_env(UZKIY_DIR)
    from bot_screener_uzkiy.scanner import ScanConfig
    ex = [s.strip() for s in os.getenv("EXCLUDE_SYMBOLS", "BTCUSDT,ETHUSDT").split(",") if s.strip()]
    return {
        "api_key": os.getenv("BYBIT_API_KEY", ""), "api_secret": os.getenv("BYBIT_API_SECRET", ""),
        "testnet": get_env_bool("TESTNET", True), "timeframe": os.getenv("TIMEFRAME", "1"),
        "scan_interval": get_env_int("SCAN_INTERVAL", 60),
        "min_turnover_24h": get_env_float("MIN_TURNOVER_24H", 30_000_000),
        "lookback_bars": get_env_int("LOOKBACK_BARS", 60), "exclude_symbols": ex,
    }, ScanConfig()


def _cfg_yrovni():
    _clear_screener_env(); load_bot_env(YROVNI_DIR)
    ex = [s.strip() for s in os.getenv("EXCLUDE_SYMBOLS", "BTCUSDT,ETHUSDT").split(",") if s.strip()]
    return {
        "api_key": os.getenv("BYBIT_API_KEY", ""), "api_secret": os.getenv("BYBIT_API_SECRET", ""),
        "testnet": get_env_bool("TESTNET", True),
        "scan_interval": get_env_int("SCAN_INTERVAL", 300),
        "min_turnover_24h": get_env_float("MIN_TURNOVER_24H", 50_000_000),
        "num_bins": 100, "proximity_pct": get_env_float("PROXIMITY_PCT", 10.0),
        "exclude_symbols": ex,
    }


def _cfg_krugloe():
    _clear_screener_env(); load_bot_env(KRUGLOE_DIR)
    ex = [s.strip() for s in os.getenv("EXCLUDE_SYMBOLS", "BTCUSDT,ETHUSDT").split(",") if s.strip()]
    return {
        "api_key": os.getenv("BYBIT_API_KEY", ""), "api_secret": os.getenv("BYBIT_API_SECRET", ""),
        "testnet": get_env_bool("TESTNET", True), "timeframe": os.getenv("TIMEFRAME", "5"),
        "scan_interval": get_env_int("SCAN_INTERVAL", 300),
        "min_turnover_24h": get_env_float("MIN_TURNOVER_24H", 10_000_000),
        "proximity_pct": get_env_float("PROXIMITY_PCT", 5.0),
        "max_price": get_env_float("MAX_PRICE", 0), "exclude_symbols": ex,
    }


def _cfg_impulse():
    _clear_screener_env(); load_bot_env(IMPULSE_DIR)
    from bot_screener_impulse.scanner import ImpulseConfig
    ex = [s.strip() for s in os.getenv("EXCLUDE_SYMBOLS", "BTCUSDT,ETHUSDT").split(",") if s.strip()]
    env = {
        "api_key": os.getenv("BYBIT_API_KEY", ""), "api_secret": os.getenv("BYBIT_API_SECRET", ""),
        "testnet": get_env_bool("TESTNET", True), "timeframe": os.getenv("TIMEFRAME", "1"),
        "scan_interval": get_env_int("SCAN_INTERVAL", 15),
        "min_turnover_24h": get_env_float("MIN_TURNOVER_24H", 50_000_000),
        "lookback_bars": get_env_int("LOOKBACK_BARS", 100), "exclude_symbols": ex,
    }
    ic = ImpulseConfig(
        volume_sma_period=get_env_int("VOLUME_SMA_PERIOD", 50),
        volume_spike_multiplier=get_env_float("VOLUME_SPIKE_MULTIPLIER", 5.0),
        candle_width_min=get_env_float("CANDLE_WIDTH_MIN", 0.15),
        candle_width_max=get_env_float("CANDLE_WIDTH_MAX", 0.30),
        delta_sma_period=get_env_int("DELTA_SMA_PERIOD", 20),
        delta_spike_multiplier=get_env_float("DELTA_SPIKE_MULTIPLIER", 4.0),
        confirmation_candles=get_env_int("CONFIRMATION_CANDLES", 2),
    )
    return env, ic


def _cfg_zakonomer():
    _clear_screener_env(); load_bot_env(ZAKONOMER_DIR)
    from bot_screener_zakonomer.scanner import ScanConfig
    ex = [s.strip() for s in os.getenv("EXCLUDE_SYMBOLS", "BTCUSDT,ETHUSDT").split(",") if s.strip()]
    env = {
        "api_key": os.getenv("BYBIT_API_KEY", ""), "api_secret": os.getenv("BYBIT_API_SECRET", ""),
        "testnet": get_env_bool("TESTNET", True), "timeframe": os.getenv("TIMEFRAME", "1"),
        "scan_interval": get_env_int("SCAN_INTERVAL", 3600),
        "min_turnover_24h": get_env_float("MIN_TURNOVER_24H", 30_000_000),
        "lookback_days": get_env_int("LOOKBACK_DAYS", 30), "exclude_symbols": ex,
    }
    sc = ScanConfig(
        lookback_days=env["lookback_days"], interval=env["timeframe"],
        min_volume_usd=get_env_float("MIN_VOLUME_USD", 1_000_000),
        spike_std_multiplier=get_env_float("SPIKE_STD_MULTIPLIER", 3.0),
        min_turnover_24h=env["min_turnover_24h"], exclude_symbols=ex,
    )
    return env, sc


# ── capture helpers ──────────────────────────────────────

def _capture(mod_name: str):
    """Monkeypatch print_results, вернёт (original, captured_dict, main_mod)."""
    import importlib
    main_mod = importlib.import_module(f"{mod_name}.main")
    printer_mod = importlib.import_module(f"{mod_name}.printer")
    orig = printer_mod.print_results
    cap: dict = {"results": [], "meta": {}}

    def _hook(results, **kwargs):
        cap["results"] = list(results)
        cap["meta"] = kwargs

    printer_mod.print_results = _hook
    main_mod.print_results = _hook
    return orig, cap, main_mod, printer_mod


def _restore(orig, main_mod, printer_mod):
    printer_mod.print_results = orig
    main_mod.print_results = orig


# ── scanners (single scan) ──────────────────────────────

async def _scan_uzkiy():
    from bot_screener_uzkiy.main import scan_once
    cfg, sc = _cfg_uzkiy()
    m = ScreenerMetrics()
    orig, cap, mm, pm = _capture("bot_screener_uzkiy")
    try:
        await scan_once(cfg, sc, m)
    finally:
        _restore(orig, mm, pm)
    return cap["results"]


async def _scan_yrovni():
    from bot_screener_yrovni.main import scan_once
    cfg = _cfg_yrovni()
    m = ScreenerMetrics()
    orig, cap, mm, pm = _capture("bot_screener_yrovni")
    try:
        await scan_once(cfg, m)
    finally:
        _restore(orig, mm, pm)
    return cap["results"]


async def _scan_krugloe():
    from bot_screener_krugloe.main import scan_once
    cfg = _cfg_krugloe()
    m = ScreenerMetrics()
    orig, cap, mm, pm = _capture("bot_screener_krugloe")
    try:
        await scan_once(cfg, m)
    finally:
        _restore(orig, mm, pm)
    return cap["results"]


async def _scan_impulse():
    from bot_screener_impulse.main import scan_once
    cfg, ic = _cfg_impulse()
    m = ScreenerMetrics()
    orig, cap, mm, pm = _capture("bot_screener_impulse")
    try:
        await scan_once(cfg, ic, m)
    finally:
        _restore(orig, mm, pm)
    return cap["results"]


async def _scan_zakonomer():
    from bot_screener_zakonomer.main import scan_once
    cfg, sc = _cfg_zakonomer()
    m = ScreenerMetrics()
    orig, cap, mm, pm = _capture("bot_screener_zakonomer")
    try:
        await scan_once(cfg, sc, m)
    finally:
        _restore(orig, mm, pm)
    return cap["results"]


# ── report writer ────────────────────────────────────────

def _write_reports(results: dict) -> None:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(MSK).strftime("%Y-%m-%d_%H-%M")

    # ── UZKIY ──
    sigs = _dedup(results.get("uzkiy", []), lambda s: s.symbol)
    with open(REPORTS_DIR / f"uzkiy_{now}.txt", "w", encoding="utf-8") as f:
        f.write("=" * 80 + "\n")
        f.write("  СКРИНЕР УЗКОГО ДИАПАЗОНА\n")
        f.write(f"  Дата: {now} UTC | Сигналов: {len(sigs)}\n")
        f.write("=" * 80 + "\n\n")
        if not sigs:
            f.write("  Сигналов нет.\n")
        else:
            for i, s in enumerate(sigs, 1):
                f.write(f"  {i}. {s.symbol} [{s.timeframe}]  цена={s.price:.4f}\n")
                f.write(f"     Тихие свечи: {s.quiet_candles}/{s.total_candles}\n")
                f.write(f"     Ср.ширина: {s.avg_width_pct:.2f}% | Оборот: ${s.turnover_24h/1e6:.0f}M | {s.status}\n\n")
        f.write("=" * 80 + "\n")

    # ── YROVNI ──
    sigs = _dedup(results.get("yrovni", []), lambda s: s.symbol)
    with open(REPORTS_DIR / f"yrovni_{now}.txt", "w", encoding="utf-8") as f:
        f.write("=" * 80 + "\n")
        f.write("  VOLUME PROFILE — POC + Дневные уровни\n")
        f.write(f"  Дата: {now} UTC | Сигналов: {len(sigs)}\n")
        f.write("=" * 80 + "\n\n")
        if not sigs:
            f.write("  Сигналов нет.\n")
        else:
            for i, s in enumerate(sigs, 1):
                f.write(f"  {i}. {s.symbol}  цена={s.current_price:.4f}\n")
                for lvl in (s.poc_levels or []):
                    f.write(f"     POC {lvl.period}: {lvl.poc_price:.4f} (объём={lvl.poc_volume:.0f}, расст={lvl.distance_pct}%)\n")
                for lvl in (s.daily_levels or []):
                    f.write(f"     {lvl.level_type}: {lvl.price:.4f} (расст={lvl.distance_pct}%)\n")
                f.write("\n")
        f.write("=" * 80 + "\n")

    # ── KRUGLOE ──
    sigs = _dedup(results.get("krugloe", []), lambda s: s.symbol)
    with open(REPORTS_DIR / f"krugloe_{now}.txt", "w", encoding="utf-8") as f:
        f.write("=" * 80 + "\n")
        f.write("  СКРИНЕР КРУГЛЫХ ЧИСЕЛ\n")
        f.write(f"  Дата: {now} UTC | Сигналов: {len(sigs)}\n")
        f.write("=" * 80 + "\n\n")
        if not sigs:
            f.write("  Сигналов нет.\n")
        else:
            for i, s in enumerate(sigs, 1):
                f.write(f"  {i}. {s.symbol} [{s.timeframe}]  цена={s.price:.4f}\n")
                f.write(f"     Круглое: {s.level:.4f} | Расстояние: {s.proximity_pct:.2f}% | Оборот: ${s.turnover_24h/1e6:.0f}M\n\n")
        f.write("=" * 80 + "\n")

    # ── IMPULSE ──
    sigs = _dedup(results.get("impulse", []), lambda s: f"{s.symbol}_{s.timeframe}")
    with open(REPORTS_DIR / f"impulse_{now}.txt", "w", encoding="utf-8") as f:
        f.write("=" * 80 + "\n")
        f.write("  IMPULSE — Объём + Дельта + Ширина\n")
        f.write(f"  Дата: {now} UTC | Сигналов: {len(sigs)}\n")
        f.write("=" * 80 + "\n\n")
        if not sigs:
            f.write("  Импульсов нет.\n")
        else:
            for i, s in enumerate(sigs, 1):
                f.write(f"  {i}. {s.symbol} [{s.timeframe}]  {s.direction}  цена={s.price:.4f}\n")
                f.write(f"     Vol: {s.volume_ratio:.1f}x | Width: {s.candle_width:.3f}% | Delta: {s.delta_ratio:.1f}x\n")
                f.write(f"     Подтверждено: {'Да' if s.confirmed else 'Нет'} | Оборот: ${s.turnover_24h/1e6:.0f}M\n\n")
        f.write("=" * 80 + "\n")

    # ── ZAKONOMER ──
    sigs = _dedup(results.get("zakonomer", []), lambda s: s.symbol)
    with open(REPORTS_DIR / f"zakonomer_{now}.txt", "w", encoding="utf-8") as f:
        f.write("=" * 80 + "\n")
        f.write("  ZAKONOMER — Активность всплесков по часам\n")
        f.write(f"  Дата: {now} UTC | Символов: {len(sigs)}\n")
        f.write("=" * 80 + "\n\n")
        if not sigs:
            f.write("  Паттернов нет.\n")
        else:
            for i, s in enumerate(sigs, 1):
                f.write(f"  {i}. {s.symbol}\n")
                f.write(f"     Свечей: {s.total_candles} | Всплесков: {s.spikes_found} | Порог: ${s.threshold_usd:,.0f}\n\n")

                hour_map = {p.hour: p for p in s.hourly_patterns}
                max_cnt = max((p.count for p in s.hourly_patterns), default=1)

                f.write("     Часы (MSK):\n")
                for h in range(24):
                    tp = hour_map.get(h)
                    cnt = tp.count if tp else 0
                    pct = tp.pct if tp else 0.0
                    if cnt == 0:
                        bar, label = "░░░░░░░░░░", "Тихо"
                    else:
                        r = cnt / max_cnt
                        if r >= 0.75:   bar, label = "██████████", "ПИК"
                        elif r >= 0.50: bar, label = "▓▓▓▓▓▓░░░░", "Активно"
                        elif r >= 0.25: bar, label = "▓▓▓▓░░░░░░", "Умеренно"
                        else:           bar, label = "▓▓░░░░░░░░", "Слабо"
                    f.write(f"       {h:02d}:00  {bar}  {cnt:2d}x ({pct:4.1f}%)  {label}\n")

                # Градация
                all_c = [hour_map[h].count if h in hour_map else 0 for h in range(24)]
                sc = sorted(all_c)
                p25, p50, p75 = sc[5], sc[11], sc[17]
                quiet = [h for h in range(24) if all_c[h] <= p25 and all_c[h] > 0]
                moderate = [h for h in range(24) if p25 < all_c[h] <= p50]
                active = [h for h in range(24) if p50 < all_c[h] <= p75]
                peak = [h for h in range(24) if all_c[h] > p75]

                f.write("\n     Градация:\n")
                if peak:     f.write(f"       ПИК:       {', '.join(f'{h:02d}:00' for h in peak)}\n")
                if active:   f.write(f"       Активно:   {', '.join(f'{h:02d}:00' for h in active)}\n")
                if moderate: f.write(f"       Умеренно:  {', '.join(f'{h:02d}:00' for h in moderate)}\n")
                if quiet:    f.write(f"       Тихо:      {', '.join(f'{h:02d}:00' for h in quiet)}\n")

                # Дни недели
                if s.weekday_patterns:
                    f.write("\n     Дни недели:\n")
                    max_wd = max(c for _, c in s.weekday_patterns) if s.weekday_patterns else 1
                    for d, c in s.weekday_patterns:
                        bl = int(c / max_wd * 15)
                        f.write(f"       {d:12s}  {'█'*bl}{'░'*(15-bl)}  {c}x\n")

                # Топ-3
                if s.top_spikes:
                    f.write("\n     Топ-3 всплеска:\n")
                    for sp in s.top_spikes[:3]:
                        dt = datetime.fromtimestamp(sp.open_time / 1000, tz=MSK)
                        f.write(f"       {dt.strftime('%Y-%m-%d %H:%M')} MSK  ${sp.volume_usdt:,.0f}  price={sp.price:.4f}\n")
                f.write("\n" + "─" * 80 + "\n\n")

            f.write("--- КОММЕНТАРИЙ ---\n")
            f.write("Градация (MSK):\n")
            f.write("  ПИК — наибольшая активность. Входить ДО этого времени.\n")
            f.write("  Активно — высокий шанс всплеска. Хорошо для скальпинга.\n")
            f.write("  Умеренно — лимитные ордера.\n")
            f.write("  Тихо — не входить.\n")
        f.write("\n" + "=" * 80 + "\n")

    logger.info("Отчёты: %s", REPORTS_DIR)


# ── main ─────────────────────────────────────────────────

async def main() -> None:
    setup_logging("logs/quick_scan.log", "INFO")
    logger.info("БЫСТРЫЙ ПРОГОН 5 СКРИНЕРОВ")

    results = {}

    for name, fn in [
        ("uzkiy", _scan_uzkiy),
        ("yrovni", _scan_yrovni),
        ("krugloe", _scan_krugloe),
        ("impulse", _scan_impulse),
        ("zakonomer", _scan_zakonomer),
    ]:
        logger.info("[%s] Сканирование...", name.upper())
        t0 = time.monotonic()
        try:
            data = await fn()
            results[name] = data
            logger.info("[%s] Готово: %d сигналов (%.1fs)", name.upper(), len(data), time.monotonic() - t0)
        except Exception:
            logger.exception("[%s] Ошибка", name.upper())
            results[name] = []

    _write_reports(results)
    logger.info("ГОТОВО!")


if __name__ == "__main__":
    asyncio.run(main())
