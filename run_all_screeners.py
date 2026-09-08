"""Запуск всех скринеров на 1 час + сохранение отчётов.

Запуск: python run_all_screeners.py
Остановка: Ctrl+C
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_PROJECT_ROOT))

REPORTS_DIR = Path(r"C:\Users\79095\Desktop\trd\Отчеты скринера")
RUN_DURATION = 3600  # 1 час

from core.config import get_env_bool, get_env_float, get_env_int, load_bot_env
from core.logger import get_logger, setup_logging
from core.metrics import ScreenerMetrics

logger = get_logger(__name__)

# ============================================================
# Конфигурация каждого скринера
# ============================================================

PUMP_DIR = _PROJECT_ROOT / "bot_screener_pump"
KLIN_DIR = _PROJECT_ROOT / "bot_screener_klin"
UZKIY_DIR = _PROJECT_ROOT / "bot_screener_uzkiy"
YROVNI_DIR = _PROJECT_ROOT / "bot_screener_yrovni"
KRUGLOE_DIR = _PROJECT_ROOT / "bot_screener_krugloe"
IMPULSE_DIR = _PROJECT_ROOT / "bot_screener_impulse"


# Ключи env, которые нужно чистить между скринерами чтобы избежать утечки
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
    "CONFIRMATION_CANDLES",
]


def _clear_screener_env() -> None:
    """Удалить env vars скринеров, чтобы load_bot_env корректно загружал новые."""
    for key in _SCREENING_ENV_KEYS:
        os.environ.pop(key, None)


def _load_env(bot_dir: Path) -> dict:
    """Загрузить .env скринера."""
    _clear_screener_env()
    load_bot_env(bot_dir)
    return {
        "api_key": os.getenv("BYBIT_API_KEY", ""),
        "api_secret": os.getenv("BYBIT_API_SECRET", ""),
        "testnet": get_env_bool("TESTNET", True),
    }


def _load_pump_config() -> tuple[dict, object]:
    _clear_screener_env()
    load_bot_env(PUMP_DIR)
    from bot_screener_pump.scanner import AccumulationConfig

    raw_exclude = os.getenv("EXCLUDE_SYMBOLS", "BTCUSDT,ETHUSDT")
    exclude = [s.strip() for s in raw_exclude.split(",") if s.strip()]

    env_cfg = {
        "api_key": os.getenv("BYBIT_API_KEY", ""),
        "api_secret": os.getenv("BYBIT_API_SECRET", ""),
        "testnet": get_env_bool("TESTNET", True),
        "timeframe": os.getenv("TIMEFRAME", "1D"),
        "scan_interval": get_env_int("SCAN_INTERVAL", 3600),
        "min_turnover_24h": get_env_float("MIN_TURNOVER_24H", 1_000_000),
        "lookback_bars": get_env_int("LOOKBACK_BARS", 100),
        "min_probability": get_env_float("MIN_PROBABILITY", 0.0),
        "exclude_symbols": exclude,
    }

    acc_cfg = AccumulationConfig(
        analysis_period=get_env_int("ANALYSIS_PERIOD", 100),
        volume_lookback=get_env_int("VOLUME_LOOKBACK", 20),
        obv_divergence_lookback=get_env_int("OBV_LOOKBACK", 30),
        bb_period=get_env_int("BB_PERIOD", 20),
        bb_dev=get_env_float("BB_DEV", 2.0),
        rsi_period=get_env_int("RSI_PERIOD", 14),
        smart_money_lookback=get_env_int("SMART_MONEY_LOOKBACK", 7),
        max_price=get_env_float("MAX_PRICE", 0.03),
        min_total_volume_usd=get_env_float("MIN_TOTAL_VOLUME_USD", 50_000_000),
        range_max=get_env_float("RANGE_MAX", 999.0),
        range_min=get_env_float("RANGE_MIN", 0.0),
        volume_spike=get_env_float("VOLUME_SPIKE", 0.0),
        bb_compression=get_env_float("BB_COMPRESSION", 999.0),
        min_volume_usd=get_env_float("MIN_VOLUME_USD", 0.0),
        min_trades=get_env_int("MIN_TRADES", 0),
        max_spread=get_env_float("MAX_SPREAD", 999.0),
        min_days_listed=get_env_int("MIN_DAYS_LISTED", 0),
        max_volatility_3d=get_env_float("MAX_VOLATILITY_3D", 999.0),
        max_drop_7d=get_env_float("MAX_DROP_7D", 999.0),
        min_pump_probability=get_env_float("MIN_PUMP_PROBABILITY", 0.0),
        weight_range=get_env_float("WEIGHT_RANGE", 0.0),
        weight_volume=get_env_float("WEIGHT_VOLUME", 0.0),
        weight_obv=get_env_float("WEIGHT_OBV", 0.0),
        weight_bb=get_env_float("WEIGHT_BB", 0.0),
        weight_smart_money=get_env_float("WEIGHT_SMART_MONEY", 0.0),
        weight_outflow=get_env_float("WEIGHT_OUTFLOW", 0.0),
        weight_rsi=get_env_float("WEIGHT_RSI", 0.0),
        weight_liquidity=get_env_float("WEIGHT_LIQUIDITY", 0.0),
    )

    return env_cfg, acc_cfg


def _load_klin_config() -> tuple[dict, object]:
    _clear_screener_env()
    load_bot_env(KLIN_DIR)
    from bot_screener_klin.scanner import PatternConfig

    raw_timeframes = os.getenv("TIMEFRAMES", "1,3,5")
    timeframes = [t.strip() for t in raw_timeframes.split(",") if t.strip()]
    raw_exclude = os.getenv("EXCLUDE_SYMBOLS", "BTCUSDT,ETHUSDT")
    exclude = [s.strip() for s in raw_exclude.split(",") if s.strip()]

    pattern_cfg = PatternConfig(
        extrema_window=get_env_int("EXTREMA_WINDOW", 5),
        slope_up=get_env_float("SLOPE_UP", 0.3),
        slope_down=get_env_float("SLOPE_DOWN", -0.3),
        slope_flat=get_env_float("SLOPE_FLAT", 0.1),
        compression_ratio=get_env_float("COMPRESSION_RATIO", 0.75),
        triangle_lookback=get_env_int("TRIANGLE_LOOKBACK", 30),
        min_range_pct=get_env_float("MIN_RANGE_PCT", 10.0),
        bb_lookback=get_env_int("BB_LOOKBACK", 20),
        atr_lookback=get_env_int("ATR_LOOKBACK", 10),
        atr_drop_threshold=get_env_float("ATR_DROP_THRESHOLD", 0.3),
        adx_false_threshold=get_env_int("ADX_FALSE_THRESHOLD", 20),
        adx_strong_threshold=get_env_int("ADX_STRONG_THRESHOLD", 25),
        bb_period=get_env_int("BB_PERIOD", 20),
        bb_dev=get_env_float("BB_DEV", 2.0),
        atr_period=get_env_int("ATR_PERIOD", 14),
        adx_period=get_env_int("ADX_PERIOD", 14),
    )

    env_cfg = {
        "api_key": os.getenv("BYBIT_API_KEY", ""),
        "api_secret": os.getenv("BYBIT_API_SECRET", ""),
        "testnet": get_env_bool("TESTNET", True),
        "timeframes": timeframes,
        "scan_interval": get_env_int("SCAN_INTERVAL", 10),
        "min_turnover_24h": get_env_float("MIN_TURNOVER_24H", 10_000_000),
        "lookback_bars": get_env_int("LOOKBACK_BARS", 50),
        "spread_max_pct": get_env_float("SPREAD_MAX_PCT", 0.05),
        "min_score": get_env_int("MIN_SCORE", 40),
        "max_price": get_env_float("MAX_PRICE", 0),
        "exclude_symbols": exclude,
    }

    return env_cfg, pattern_cfg


def _load_uzkiy_config() -> tuple[dict, object]:
    _clear_screener_env()
    load_bot_env(UZKIY_DIR)
    from bot_screener_uzkiy.scanner import ScanConfig

    raw_exclude = os.getenv("EXCLUDE_SYMBOLS", "BTCUSDT,ETHUSDT")
    exclude = [s.strip() for s in raw_exclude.split(",") if s.strip()]

    env_cfg = {
        "api_key": os.getenv("BYBIT_API_KEY", ""),
        "api_secret": os.getenv("BYBIT_API_SECRET", ""),
        "testnet": get_env_bool("TESTNET", True),
        "timeframe": os.getenv("TIMEFRAME", "1"),
        "scan_interval": get_env_int("SCAN_INTERVAL", 60),
        "min_turnover_24h": get_env_float("MIN_TURNOVER_24H", 30_000_000),
        "lookback_bars": get_env_int("LOOKBACK_BARS", 60),
        "exclude_symbols": exclude,
    }

    return env_cfg, ScanConfig()


def _load_yrovni_config() -> dict:
    _clear_screener_env()
    load_bot_env(YROVNI_DIR)
    raw_exclude = os.getenv("EXCLUDE_SYMBOLS", "BTCUSDT,ETHUSDT")
    exclude = [s.strip() for s in raw_exclude.split(",") if s.strip()]

    return {
        "api_key": os.getenv("BYBIT_API_KEY", ""),
        "api_secret": os.getenv("BYBIT_API_SECRET", ""),
        "testnet": get_env_bool("TESTNET", True),
        "scan_interval": get_env_int("SCAN_INTERVAL", 300),
        "min_turnover_24h": get_env_float("MIN_TURNOVER_24H", 50_000_000),
        "num_bins": 100,
        "proximity_pct": get_env_float("PROXIMITY_PCT", 10.0),
        "exclude_symbols": exclude,
    }


def _load_krugloe_config() -> dict:
    _clear_screener_env()
    load_bot_env(KRUGLOE_DIR)
    raw_exclude = os.getenv("EXCLUDE_SYMBOLS", "BTCUSDT,ETHUSDT")
    exclude = [s.strip() for s in raw_exclude.split(",") if s.strip()]

    return {
        "api_key": os.getenv("BYBIT_API_KEY", ""),
        "api_secret": os.getenv("BYBIT_API_SECRET", ""),
        "testnet": get_env_bool("TESTNET", True),
        "timeframe": os.getenv("TIMEFRAME", "5"),
        "scan_interval": get_env_int("SCAN_INTERVAL", 300),
        "min_turnover_24h": get_env_float("MIN_TURNOVER_24H", 10_000_000),
        "proximity_pct": get_env_float("PROXIMITY_PCT", 5.0),
        "max_price": get_env_float("MAX_PRICE", 0),
        "exclude_symbols": exclude,
    }


def _load_impulse_config() -> tuple[dict, object]:
    _clear_screener_env()
    load_bot_env(IMPULSE_DIR)
    from bot_screener_impulse.scanner import ImpulseConfig

    raw_exclude = os.getenv("EXCLUDE_SYMBOLS", "BTCUSDT,ETHUSDT")
    exclude = [s.strip() for s in raw_exclude.split(",") if s.strip()]

    env_cfg = {
        "api_key": os.getenv("BYBIT_API_KEY", ""),
        "api_secret": os.getenv("BYBIT_API_SECRET", ""),
        "testnet": get_env_bool("TESTNET", True),
        "timeframe": os.getenv("TIMEFRAME", "1"),
        "scan_interval": get_env_int("SCAN_INTERVAL", 15),
        "min_turnover_24h": get_env_float("MIN_TURNOVER_24H", 50_000_000),
        "lookback_bars": get_env_int("LOOKBACK_BARS", 100),
        "exclude_symbols": exclude,
    }

    impulse_cfg = ImpulseConfig(
        volume_sma_period=get_env_int("VOLUME_SMA_PERIOD", 50),
        volume_spike_multiplier=get_env_float("VOLUME_SPIKE_MULTIPLIER", 5.0),
        candle_width_min=get_env_float("CANDLE_WIDTH_MIN", 0.15),
        candle_width_max=get_env_float("CANDLE_WIDTH_MAX", 0.30),
        delta_sma_period=get_env_int("DELTA_SMA_PERIOD", 20),
        delta_spike_multiplier=get_env_float("DELTA_SPIKE_MULTIPLIER", 4.0),
        confirmation_candles=get_env_int("CONFIRMATION_CANDLES", 2),
    )

    return env_cfg, impulse_cfg


# ============================================================
# Сканеры (каждый возвращает свой список результатов)
# ============================================================

MAX_RETRIES = 3
RETRY_BASE_DELAY = 30  # секунд


async def _retry_call(coro_factory, name: str, max_retries: int = MAX_RETRIES):
    """Вызвать coroutine с retry при 403/RateLimit."""
    for attempt in range(max_retries + 1):
        try:
            return await coro_factory()
        except Exception as e:
            err_str = str(e)
            is_rate = "403" in err_str or "rate limit" in err_str.lower()
            if is_rate and attempt < max_retries:
                delay = RETRY_BASE_DELAY * (attempt + 1)
                logger.warning("[%s] Rate limit (попытка %d/%d), жду %d сек...",
                               name, attempt + 1, max_retries, delay)
                await asyncio.sleep(delay)
            else:
                raise


async def _staggered_start(fn, all_results: dict, delay: float) -> None:
    """Запустить скринер с задержкой для снижения нагрузки на API."""
    if delay > 0:
        logger.info("Задержка %.0f сек перед стартом...", delay)
        await asyncio.sleep(delay)
    await fn(all_results)


async def _run_pump(all_results: dict) -> None:
    """Сканер pump."""
    from bot_screener_pump.main import scan_once
    from bot_screener_pump.scanner import AccumulationSignal

    env_cfg, acc_cfg = _load_pump_config()
    metrics = ScreenerMetrics()
    signals: list[AccumulationSignal] = []

    logger.info("[PUMP] Запуск (interval=%ss)", env_cfg["scan_interval"])

    start = time.monotonic()
    while time.monotonic() - start < RUN_DURATION:
        try:
            import bot_screener_pump.printer as printer_mod
            import bot_screener_pump.main as main_mod

            original_print = printer_mod.print_results
            captured: dict = {"results": [], "meta": {}}

            def _capture_print(results, **kwargs):
                captured["results"] = list(results)
                captured["meta"] = kwargs

            printer_mod.print_results = _capture_print
            main_mod.print_results = _capture_print
            try:
                await _retry_call(
                    lambda: scan_once(env_cfg, acc_cfg, metrics), "PUMP"
                )
            finally:
                printer_mod.print_results = original_print
                main_mod.print_results = original_print

            if captured["results"]:
                signals.extend(captured["results"])

        except Exception:
            logger.exception("[PUMP] Ошибка сканирования")

        remaining = RUN_DURATION - (time.monotonic() - start)
        if remaining > 0:
            await asyncio.sleep(min(env_cfg["scan_interval"], remaining))

    all_results["pump"] = signals
    logger.info("[PUMP] Завершено. Сигналов: %d", len(signals))


async def _run_klin(all_results: dict) -> None:
    """Сканер klin (сужение)."""
    from bot_screener_klin.main import scan_once
    from bot_screener_klin.scanner import ScanResult

    env_cfg, pattern_cfg = _load_klin_config()
    metrics = ScreenerMetrics()
    signals: list[ScanResult] = []

    logger.info("[KLIN] Запуск (interval=%ss)", env_cfg["scan_interval"])

    start = time.monotonic()
    while time.monotonic() - start < RUN_DURATION:
        try:
            import bot_screener_klin.printer as printer_mod
            import bot_screener_klin.main as main_mod

            original_print = printer_mod.print_results
            captured: dict = {"results": [], "meta": {}}

            def _capture_print(results, **kwargs):
                captured["results"] = list(results)
                captured["meta"] = kwargs

            printer_mod.print_results = _capture_print
            main_mod.print_results = _capture_print
            try:
                await _retry_call(
                    lambda: scan_once(env_cfg, pattern_cfg, metrics), "KLIN"
                )
            finally:
                printer_mod.print_results = original_print
                main_mod.print_results = original_print

            if captured["results"]:
                signals.extend(captured["results"])

        except Exception:
            logger.exception("[KLIN] Ошибка сканирования")

        remaining = RUN_DURATION - (time.monotonic() - start)
        if remaining > 0:
            await asyncio.sleep(min(env_cfg["scan_interval"], remaining))

    all_results["klin"] = signals
    logger.info("[KLIN] Завершено. Сигналов: %d", len(signals))


async def _run_uzkiy(all_results: dict) -> None:
    """Сканер uzkiy (узкий)."""
    from bot_screener_uzkiy.main import scan_once
    from bot_screener_uzkiy.scanner import ScanResult

    env_cfg, scan_cfg = _load_uzkiy_config()
    metrics = ScreenerMetrics()
    signals: list[ScanResult] = []

    logger.info("[UZKIY] Запуск (interval=%ss)", env_cfg["scan_interval"])

    start = time.monotonic()
    while time.monotonic() - start < RUN_DURATION:
        try:
            import bot_screener_uzkiy.printer as printer_mod
            import bot_screener_uzkiy.main as main_mod

            original_print = printer_mod.print_results
            captured: dict = {"results": [], "meta": {}}

            def _capture_print(results, **kwargs):
                captured["results"] = list(results)
                captured["meta"] = kwargs

            printer_mod.print_results = _capture_print
            main_mod.print_results = _capture_print
            try:
                await _retry_call(
                    lambda: scan_once(env_cfg, scan_cfg, metrics), "UZKIY"
                )
            finally:
                printer_mod.print_results = original_print
                main_mod.print_results = original_print

            if captured["results"]:
                signals.extend(captured["results"])

        except Exception:
            logger.exception("[UZKIY] Ошибка сканирования")

        remaining = RUN_DURATION - (time.monotonic() - start)
        if remaining > 0:
            await asyncio.sleep(min(env_cfg["scan_interval"], remaining))

    all_results["uzkiy"] = signals
    logger.info("[UZKIY] Завершено. Сигналов: %d", len(signals))


async def _run_yrovni(all_results: dict) -> None:
    """Сканер yrovni (POC)."""
    from bot_screener_yrovni.main import scan_once
    from bot_screener_yrovni.scanner import VPSignal

    cfg = _load_yrovni_config()
    metrics = ScreenerMetrics()
    signals: list[VPSignal] = []

    logger.info("[YROVNI] Запуск (interval=%ss)", cfg["scan_interval"])

    start = time.monotonic()
    while time.monotonic() - start < RUN_DURATION:
        try:
            import bot_screener_yrovni.printer as printer_mod
            import bot_screener_yrovni.main as main_mod

            original_print = printer_mod.print_results
            captured: dict = {"results": [], "meta": {}}

            def _capture_print(results, **kwargs):
                captured["results"] = list(results)
                captured["meta"] = kwargs

            printer_mod.print_results = _capture_print
            main_mod.print_results = _capture_print
            try:
                await _retry_call(
                    lambda: scan_once(cfg, metrics), "YROVNI"
                )
            finally:
                printer_mod.print_results = original_print
                main_mod.print_results = original_print

            if captured["results"]:
                signals.extend(captured["results"])

        except Exception:
            logger.exception("[YROVNI] Ошибка сканирования")

        remaining = RUN_DURATION - (time.monotonic() - start)
        if remaining > 0:
            await asyncio.sleep(min(cfg["scan_interval"], remaining))

    all_results["yrovni"] = signals
    logger.info("[YROVNI] Завершено. Сигналов: %d", len(signals))


async def _run_krugloe(all_results: dict) -> None:
    """Сканер krugloe (круглые числа)."""
    from bot_screener_krugloe.main import scan_once
    from bot_screener_krugloe.scanner import RoundSignal

    env_cfg = _load_krugloe_config()
    metrics = ScreenerMetrics()
    signals: list[RoundSignal] = []

    logger.info("[KRUGLOE] Запуск (interval=%ss)", env_cfg["scan_interval"])

    start = time.monotonic()
    while time.monotonic() - start < RUN_DURATION:
        try:
            import bot_screener_krugloe.printer as printer_mod
            import bot_screener_krugloe.main as main_mod

            original_print = printer_mod.print_results
            captured: dict = {"results": [], "meta": {}}

            def _capture_print(results, **kwargs):
                captured["results"] = list(results)
                captured["meta"] = kwargs

            printer_mod.print_results = _capture_print
            main_mod.print_results = _capture_print
            try:
                await _retry_call(
                    lambda: scan_once(env_cfg, metrics), "KRUGLOE"
                )
            finally:
                printer_mod.print_results = original_print
                main_mod.print_results = original_print

            if captured["results"]:
                signals.extend(captured["results"])

        except Exception:
            logger.exception("[KRUGLOE] Ошибка сканирования")

        remaining = RUN_DURATION - (time.monotonic() - start)
        if remaining > 0:
            await asyncio.sleep(min(env_cfg["scan_interval"], remaining))

    all_results["krugloe"] = signals
    logger.info("[KRUGLOE] Завершено. Сигналов: %d", len(signals))


async def _run_impulse(all_results: dict) -> None:
    """Сканер impulse (импульсы)."""
    from bot_screener_impulse.main import scan_once
    from bot_screener_impulse.scanner import ImpulseSignal

    env_cfg, impulse_cfg = _load_impulse_config()
    metrics = ScreenerMetrics()
    signals: list[ImpulseSignal] = []

    logger.info("[IMPULSE] Запуск (interval=%ss)", env_cfg["scan_interval"])

    start = time.monotonic()
    while time.monotonic() - start < RUN_DURATION:
        try:
            import bot_screener_impulse.printer as printer_mod
            import bot_screener_impulse.main as main_mod

            original_print = printer_mod.print_results
            captured: dict = {"results": [], "meta": {}}

            def _capture_print(results, **kwargs):
                captured["results"] = list(results)
                captured["meta"] = kwargs

            printer_mod.print_results = _capture_print
            main_mod.print_results = _capture_print
            try:
                await _retry_call(
                    lambda: scan_once(env_cfg, impulse_cfg, metrics), "IMPULSE"
                )
            finally:
                printer_mod.print_results = original_print
                main_mod.print_results = original_print

            if captured["results"]:
                signals.extend(captured["results"])

        except Exception:
            logger.exception("[IMPULSE] Ошибка сканирования")

        remaining = RUN_DURATION - (time.monotonic() - start)
        if remaining > 0:
            await asyncio.sleep(min(env_cfg["scan_interval"], remaining))

    all_results["impulse"] = signals
    logger.info("[IMPULSE] Завершено. Сигналов: %d", len(signals))


# ============================================================
# Генерация отчётов
# ============================================================

def _generate_reports(all_results: dict) -> None:
    """Сформировать txt-отчёты по каждому скринеру."""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M")

    # --- PUMP ---
    signals = all_results.get("pump", [])
    with open(REPORTS_DIR / f"pump_{now}.txt", "w", encoding="utf-8") as f:
        f.write("=" * 80 + "\n")
        f.write("  ACCUMULATION SCREENER — Отчёт\n")
        f.write(f"  Дата: {now} UTC | Сигналов: {len(signals)}\n")
        f.write("=" * 80 + "\n\n")
        if not signals:
            f.write("  Сигналов не найдено за период.\n\n")
        else:
            for i, s in enumerate(signals, 1):
                f.write(f"  {i}. {s.symbol} [{s.timeframe}]\n")
                f.write(f"     Цена: {s.price:.4f}\n")
                f.write(f"     Вероятность: {s.pump_probability:.1%}\n")
                f.write(f"     Статус: {s.status}\n")
                f.write(f"     Диапазон: {s.range_pct}%\n")
                f.write(f"     B/S ratio: {s.buy_sell_ratio:.2f}\n")
                f.write(f"     F(Диапазон)={s.f_range:.2f} F(Объем)={s.f_volume:.2f} "
                        f"F(OBV)={s.f_obv:.2f} F(BB)={s.f_bb:.2f} "
                        f"F(Smart)={s.f_smart_money:.2f}\n")
                f.write(f"     Время: {s.signal_time}\n\n")

        f.write("\n--- КОММЕНТАРИЙ ---\n")
        if not signals:
            f.write("Сигналов нет. Это может означать:\n")
            f.write("1. Рынок в нейтральной фазе — нет явного накопления.\n")
            f.write("2. Параметры фильтрации слишком строгие.\n")
            f.write("3. Период 1 час мал для поиска паттернов накопления.\n")
        else:
            f.write(f"Найдено {len(signals)} сигналов. Рекомендую:\n")
            f.write("- Проверить топ-3 по вероятности вручную.\n")
            f.write("- Обратить внимание на статус КРИТИЧЕСКИЙ/СИЛЬНЫЙ.\n")
            f.write("- Не входить сразу — ждать подтверждения на H1/H4.\n")
        f.write("\n" + "=" * 80 + "\n")

    # --- KLIN ---
    signals = all_results.get("klin", [])
    with open(REPORTS_DIR / f"klin_{now}.txt", "w", encoding="utf-8") as f:
        f.write("=" * 80 + "\n")
        f.write("  СКРИНЕР СУЖЕНИЯ ДИАПАЗОНА — Отчёт\n")
        f.write(f"  Дата: {now} UTC | Сигналов: {len(signals)}\n")
        f.write("=" * 80 + "\n\n")
        if not signals:
            f.write("  Сигналов не найдено за период.\n\n")
        else:
            for i, s in enumerate(signals, 1):
                f.write(f"  {i}. {s.symbol} [{s.timeframe}]\n")
                f.write(f"     Цена: {s.price:.4f}\n")
                f.write(f"     Тип сужения: {s.squeeze_type.value if s.squeeze_type else 'N/A'}\n")
                f.write(f"     Направление: {s.direction or 'N/A'}\n")
                f.write(f"     ATR: {s.atr_current:.4f} ({s.atr_percent:.2f}%)\n")
                f.write(f"     BB: {s.bb_width:.2f}% | ADX: {s.adx_value:.1f}\n")
                f.write(f"     Score: {s.score}\n")
                f.write(f"     Spread: {s.spread_pct:.3f}%\n")
                f.write(f"     Время: {s.signal_time}\n\n")

        f.write("\n--- КОММЕНТАРИЙ ---\n")
        if not signals:
            f.write("Сжатий не найдено. Возможно:\n")
            f.write("1. Рынок в трендовой фазе — нет консолидации.\n")
            f.write("2. min_score слишком высокий.\n")
            f.write("3. Спреды слишком высокие для скальпинга.\n")
        else:
            f.write(f"Найдено {len(signals)} сжатий. Рекомендую:\n")
            f.write("- Сфокусироваться на сигналах с ADX > 25 (сильный тренд после сжатия).\n")
            f.write("- Проверить спред — чем ниже, тем лучше для скальпинга.\n")
            f.write("- Ждать breakout из сужения, а не входить внутрь.\n")
        f.write("\n" + "=" * 80 + "\n")

    # --- UZKIY ---
    signals = all_results.get("uzkiy", [])
    with open(REPORTS_DIR / f"uzkiy_{now}.txt", "w", encoding="utf-8") as f:
        f.write("=" * 80 + "\n")
        f.write("  СКРИНЕР УЗКОГО ДИАПАЗОНА — Отчёт\n")
        f.write(f"  Дата: {now} UTC | Сигналов: {len(signals)}\n")
        f.write("=" * 80 + "\n\n")
        if not signals:
            f.write("  Сигналов не найдено за период.\n\n")
        else:
            for i, s in enumerate(signals, 1):
                f.write(f"  {i}. {s.symbol} [{s.timeframe}]\n")
                f.write(f"     Цена: {s.price:.4f}\n")
                f.write(f"     Малые свечи: {s.small_candles}/{s.total_candles}\n")
                f.write(f"     Выбросы: {s.outliers}\n")
                f.write(f"     Ср. диапазон: {s.avg_range_pct:.2f}%\n")
                f.write(f"     Оборот: ${s.turnover_24h / 1_000_000:.0f}M\n")
                f.write(f"     Статус: {s.status}\n")
                f.write(f"     Время: {s.signal_time}\n\n")

        f.write("\n--- КОММЕНТАРИЙ ---\n")
        if not signals:
            f.write("Сжатий не найдено. Возможно:\n")
            f.write("1. Рынок слишком волатилен — нет узких диапазонов.\n")
            f.write("2. Оборот ниже порога.\n")
        else:
            f.write(f"Найдено {len(signals)} сигналов. Рекомендую:\n")
            f.write("- Внимание на % малых свечей > 50% — это подтверждение сжатия.\n")
            f.write("- Проверять выбросы — если выбросов 0, рынок \"спит\".\n")
            f.write("- Использовать как фильтр перед входом по другому скринеру.\n")
        f.write("\n" + "=" * 80 + "\n")

    # --- YROVNI ---
    signals = all_results.get("yrovni", [])
    with open(REPORTS_DIR / f"yrovni_{now}.txt", "w", encoding="utf-8") as f:
        f.write("=" * 80 + "\n")
        f.write("  VOLUME PROFILE SCREENER — POC + Дневные уровни — Отчёт\n")
        f.write(f"  Дата: {now} UTC | Сигналов: {len(signals)}\n")
        f.write("=" * 80 + "\n\n")
        if not signals:
            f.write("  Сигналов не найдено за период.\n\n")
        else:
            for i, s in enumerate(signals, 1):
                f.write(f"  {i}. {s.symbol} — цена: {s.current_price:.4f}\n")
                if s.poc_levels:
                    f.write(f"     POC уровни:\n")
                    for lvl in s.poc_levels:
                        f.write(f"       {lvl.period}: POC={lvl.poc_price:.4f} "
                                f"(объём={lvl.poc_volume:.0f}, расст={lvl.distance_pct}%)\n")
                if s.daily_levels:
                    f.write(f"     Дневные уровни:\n")
                    for lvl in s.daily_levels:
                        f.write(f"       {lvl.level_type}: {lvl.price:.4f} (расст={lvl.distance_pct}%)\n")
                f.write(f"     Время: {s.scan_time}\n\n")

        f.write("\n--- КОММЕНТАРИЙ ---\n")
        if not signals:
            f.write("Уровней не найдено. Возможно:\n")
            f.write("1. Оборот ниже порога $50M.\n")
            f.write("2. Цена далеко от POC/дневных уровней.\n")
        else:
            f.write(f"Найдено {len(signals)} символов с уровнями. Рекомендую:\n")
            f.write("- POC — зона максимального объёма. Хорошо работают как поддержка/сопротивление.\n")
            f.write("- Дневные open/close — психологические уровни.\n")
            f.write("- Использовать для тейк-профитов или стопов, а не для входов.\n")
        f.write("\n" + "=" * 80 + "\n")

    # --- KRUGLOE ---
    signals = all_results.get("krugloe", [])
    with open(REPORTS_DIR / f"krugloe_{now}.txt", "w", encoding="utf-8") as f:
        f.write("=" * 80 + "\n")
        f.write("  СКРИНЕР КРУГЛЫХ ЧИСЕЛ — Отчёт\n")
        f.write(f"  Дата: {now} UTC | Сигналов: {len(signals)}\n")
        f.write("=" * 80 + "\n\n")
        if not signals:
            f.write("  Сигналов не найдено за период.\n\n")
        else:
            for i, s in enumerate(signals, 1):
                f.write(f"  {i}. {s.symbol} [{s.timeframe}]\n")
                f.write(f"     Цена: {s.price:.4f}\n")
                f.write(f"     Ближайшее круглое: {s.level:.4f}\n")
                f.write(f"     Расстояние: {s.proximity_pct:.2f}%\n")
                f.write(f"     Оборот: ${s.turnover_24h / 1_000_000:.0f}M\n")
                f.write(f"     Время: {s.signal_time}\n\n")

        f.write("\n--- КОММЕНТАРИЙ ---\n")
        if not signals:
            f.write("Круглых чисел рядом нет. Возможно:\n")
            f.write("1. Цена далеко от психологических уровней.\n")
            f.write("2. proximity_pct слишком мал.\n")
        else:
            f.write(f"Найдено {len(signals)} близостей. Рекомендую:\n")
            f.write("- Круглые числа — зоны отскока. Хороши для лимитных ордеров.\n")
            f.write("- Чем ближе цена к круглому уровню, тем выше шанс отскока.\n")
            f.write("- Не использовать как единственный сигнал — только в связке.\n")
        f.write("\n" + "=" * 80 + "\n")

    # --- IMPULSE ---
    signals = all_results.get("impulse", [])
    with open(REPORTS_DIR / f"impulse_{now}.txt", "w", encoding="utf-8") as f:
        f.write("=" * 80 + "\n")
        f.write("  IMPULSE SCREENER — Тиковый объем + Дельта + Ширина — Отчёт\n")
        f.write(f"  Дата: {now} UTC | Сигналов: {len(signals)}\n")
        f.write("=" * 80 + "\n\n")
        if not signals:
            f.write("  Импульсов не найдено за период.\n\n")
        else:
            for i, s in enumerate(signals, 1):
                f.write(f"  {i}. {s.symbol} [{s.timeframe}]\n")
                f.write(f"     Цена: {s.price:.4f}\n")
                f.write(f"     Направление: {s.direction}\n")
                f.write(f"     Volume: {s.volume_ratio:.1f}x (от SMA)\n")
                f.write(f"     Ширина свечи: {s.candle_width:.3f}%\n")
                f.write(f"     Delta: {s.delta_ratio:.1f}x (от SMA)\n")
                f.write(f"     Подтверждено: {'Да' if s.confirmed else 'Нет'}\n")
                f.write(f"     Оборот: ${s.turnover_24h / 1_000_000:.0f}M\n")
                f.write(f"     Время: {s.signal_time}\n\n")

        f.write("\n--- КОММЕНТАРИЙ ---\n")
        if not signals:
            f.write("Импульсов не найдено. Возможно:\n")
            f.write("1. Рынок спокойный — нет резких движений.\n")
            f.write("2. Порог volume_spike_multiplier слишком высокий.\n")
            f.write("3. Фильтр подтверждения отсеивает все сигналы.\n")
        else:
            f.write(f"Найдено {len(signals)} импульсов. Рекомендую:\n")
            f.write("- Импульс = факт. Не входить сразу — ждать откат или пробой.\n")
            f.write("- Проверять направление (LONG/SHORT) и подтверждение.\n")
            f.write("- Использовать как индикатор турбулентности, а не сигнал для входа.\n")
        f.write("\n" + "=" * 80 + "\n")

    logger.info("Отчёты сохранены в %s", REPORTS_DIR)


# ============================================================
# Main
# ============================================================

async def main() -> None:
    """Запуск всех скринеров на 1 час."""
    setup_logging("logs/run_all_screeners.log", "INFO")
    logger.info("=" * 60)
    logger.info("ЗАПУСК ВСЕХ СКРИНЕРОВ НА 1 ЧАС")
    logger.info("Отчёты: %s", REPORTS_DIR)
    logger.info("=" * 60)

    all_results: dict = {}

    start = time.monotonic()

    # Запускаем с задержками чтобы не trip rate limit
    STAGGER_DELAY = 30  # секунд между стартами (Bybit rate limit ~2 req/s)

    tasks = []
    for i, (name, fn) in enumerate([
        ("pump", _run_pump),
        ("klin", _run_klin),
        ("uzkiy", _run_uzkiy),
        ("yrovni", _run_yrovni),
        ("krugloe", _run_krugloe),
        ("impulse", _run_impulse),
    ]):
        task = asyncio.create_task(_staggered_start(fn, all_results, i * STAGGER_DELAY), name=name)
        tasks.append(task)

    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        logger.info("Задачи отменены")
    except Exception:
        logger.exception("Ошибка в основном цикле")

    elapsed = time.monotonic() - start
    logger.info("Общее время: %.0f сек", elapsed)

    # Генерация отчётов
    logger.info("Генерирую отчёты...")
    _generate_reports(all_results)

    logger.info("ГОТОВО!")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nОстановлен пользователем.")
