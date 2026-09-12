"""Запуск 5 скринеров на 1 час + отчёты.

Скринеры: impulse, krugloe, uzkiy, yrovni, zakonomer
Запуск: python run_5_screeners_1h.py
Остановка: Ctrl+C
"""

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
RUN_DURATION = 3600  # 1 час

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
    "CONFIRMATION_CANDLES",
    "LOOKBACK_DAYS", "SPIKE_STD_MULTIPLIER",
]


def _clear_screener_env() -> None:
    for key in _SCREENING_ENV_KEYS:
        os.environ.pop(key, None)


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


def _load_zakonomer_config() -> tuple[dict, object]:
    _clear_screener_env()
    load_bot_env(ZAKONOMER_DIR)
    from bot_screener_zakonomer.scanner import ScanConfig

    raw_exclude = os.getenv("EXCLUDE_SYMBOLS", "BTCUSDT,ETHUSDT")
    exclude = [s.strip() for s in raw_exclude.split(",") if s.strip()]

    env_cfg = {
        "api_key": os.getenv("BYBIT_API_KEY", ""),
        "api_secret": os.getenv("BYBIT_API_SECRET", ""),
        "testnet": get_env_bool("TESTNET", True),
        "timeframe": os.getenv("TIMEFRAME", "1"),
        "scan_interval": get_env_int("SCAN_INTERVAL", 3600),
        "min_turnover_24h": get_env_float("MIN_TURNOVER_24H", 30_000_000),
        "lookback_days": get_env_int("LOOKBACK_DAYS", 30),
        "exclude_symbols": exclude,
    }

    scan_cfg = ScanConfig(
        lookback_days=env_cfg["lookback_days"],
        interval=env_cfg["timeframe"],
        min_volume_usd=get_env_float("MIN_VOLUME_USD", 1_000_000),
        spike_std_multiplier=get_env_float("SPIKE_STD_MULTIPLIER", 3.0),
        min_turnover_24h=env_cfg["min_turnover_24h"],
        exclude_symbols=exclude,
    )

    return env_cfg, scan_cfg


MAX_RETRIES = 3
RETRY_BASE_DELAY = 30


async def _retry_call(coro_factory, name: str, max_retries: int = MAX_RETRIES):
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
    if delay > 0:
        logger.info("Задержка %.0f сек перед стартом...", delay)
        await asyncio.sleep(delay)
    await fn(all_results)


async def _run_uzkiy(all_results: dict) -> None:
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


async def _run_zakonomer(all_results: dict) -> None:
    from bot_screener_zakonomer.main import scan_once
    from bot_screener_zakonomer.scanner import ScanResult

    env_cfg, scan_cfg = _load_zakonomer_config()
    metrics = ScreenerMetrics()
    signals: list[ScanResult] = []

    logger.info("[ZAKONOMER] Запуск (interval=%ss, lookback=%dd)",
                env_cfg["scan_interval"], env_cfg["lookback_days"])

    start = time.monotonic()
    while time.monotonic() - start < RUN_DURATION:
        try:
            import bot_screener_zakonomer.printer as printer_mod
            import bot_screener_zakonomer.main as main_mod

            original_print = printer_mod.print_results
            captured: dict = {"results": [], "meta": {}}

            def _capture_print(results, **kwargs):
                captured["results"] = list(results)
                captured["meta"] = kwargs

            printer_mod.print_results = _capture_print
            main_mod.print_results = _capture_print
            try:
                await _retry_call(
                    lambda: scan_once(env_cfg, scan_cfg, metrics), "ZAKONOMER"
                )
            finally:
                printer_mod.print_results = original_print
                main_mod.print_results = original_print

            if captured["results"]:
                signals.extend(captured["results"])

        except Exception:
            logger.exception("[ZAKONOMER] Ошибка сканирования")

        remaining = RUN_DURATION - (time.monotonic() - start)
        if remaining > 0:
            await asyncio.sleep(min(env_cfg["scan_interval"], remaining))

    all_results["zakonomer"] = signals
    logger.info("[ZAKONOMER] Завершено. Сигналов: %d", len(signals))


def _dedup_signals(signals: list, key_fn) -> list:
    """Оставить только последний сигнал на каждый ключ (symbol/timeframe и т.д.)."""
    seen: dict = {}
    for s in signals:
        key = key_fn(s)
        seen[key] = s
    return list(seen.values())


def _generate_reports(all_results: dict) -> None:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(MSK).strftime("%Y-%m-%d_%H-%M")

    # Лог дедупликации
    for name in ["uzkiy", "yrovni", "krugloe", "impulse", "zakonomer"]:
        raw = len(all_results.get(name, []))
        logger.info("[%s] До дедупликации: %d сигналов", name.upper(), raw)

    # --- UZKIY ---
    signals = _dedup_signals(all_results.get("uzkiy", []), lambda s: s.symbol)
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
    signals = _dedup_signals(all_results.get("yrovni", []), lambda s: s.symbol)
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
    signals = _dedup_signals(all_results.get("krugloe", []), lambda s: s.symbol)
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
    signals = _dedup_signals(all_results.get("impulse", []), lambda s: f"{s.symbol}_{s.timeframe}")
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

    # --- ZAKONOMER ---
    signals = _dedup_signals(all_results.get("zakonomer", []), lambda s: s.symbol)
    with open(REPORTS_DIR / f"zakonomer_{now}.txt", "w", encoding="utf-8") as f:
        f.write("=" * 80 + "\n")
        f.write("  ZAKONOMER — ГРАФИК АКТИВНОСТИ ВСПЛЕСКОВ ОБЪЁМА\n")
        f.write(f"  Дата: {now} UTC | Символов: {len(signals)}\n")
        f.write("=" * 80 + "\n\n")
        if not signals:
            f.write("  Паттернов не найдено за период.\n\n")
            f.write("  Причины:\n")
            f.write("  1. Оборот ниже порога.\n")
            f.write("  2. Всплесков объема нет — рынок спокойный.\n")
            f.write("  3. lookback_days слишком мал.\n")
        else:
            for i, s in enumerate(signals, 1):
                f.write(f"  {i}. {s.symbol}\n")
                f.write(f"     Свечей: {s.total_candles} | Всплесков: {s.spikes_found} | Порог: ${s.threshold_usd:,.0f}\n\n")

                hour_map = {p.hour: p for p in s.hourly_patterns}
                max_count = max((p.count for p in s.hourly_patterns), default=1)

                f.write("     Часы (MSK):\n")
                for h in range(24):
                    tp = hour_map.get(h)
                    count = tp.count if tp else 0
                    pct = tp.pct if tp else 0.0

                    if count == 0:
                        bar = "░░░░░░░░░░"
                        label = "Тихо"
                    else:
                        ratio = count / max_count
                        if ratio >= 0.75:
                            bar = "██████████"
                            label = "ПИК"
                        elif ratio >= 0.50:
                            bar = "▓▓▓▓▓▓░░░░"
                            label = "Активно"
                        elif ratio >= 0.25:
                            bar = "▓▓▓▓░░░░░░"
                            label = "Умеренно"
                        else:
                            bar = "▓▓░░░░░░░░"
                            label = "Слабо"

                    f.write(f"       {h:02d}:00  {bar}  {count:2d}x ({pct:4.1f}%)  {label}\n")

                all_counts = [hour_map[h].count if h in hour_map else 0 for h in range(24)]
                sorted_counts = sorted(all_counts)
                p25 = sorted_counts[5]
                p50 = sorted_counts[11]
                p75 = sorted_counts[17]

                quiet = [h for h in range(24) if all_counts[h] <= p25 and all_counts[h] > 0]
                moderate = [h for h in range(24) if p25 < all_counts[h] <= p50]
                active = [h for h in range(24) if p50 < all_counts[h] <= p75]
                peak = [h for h in range(24) if all_counts[h] > p75]

                f.write("\n     Градация:\n")
                if peak:
                    f.write(f"       ПИК:       {', '.join(f'{h:02d}:00' for h in peak)}\n")
                if active:
                    f.write(f"       Активно:   {', '.join(f'{h:02d}:00' for h in active)}\n")
                if moderate:
                    f.write(f"       Умеренно:  {', '.join(f'{h:02d}:00' for h in moderate)}\n")
                if quiet:
                    f.write(f"       Тихо:      {', '.join(f'{h:02d}:00' for h in quiet)}\n")

                if s.weekday_patterns:
                    f.write("\n     Дни недели:\n")
                    max_wd = max(c for _, c in s.weekday_patterns) if s.weekday_patterns else 1
                    for d, c in s.weekday_patterns:
                        bar_len = int(c / max_wd * 15)
                        bar = "█" * bar_len + "░" * (15 - bar_len)
                        f.write(f"       {d:12s}  {bar}  {c}x\n")

                if s.top_spikes:
                    f.write("\n     Топ-3 всплеска:\n")
                    for sp in s.top_spikes[:3]:
                        from datetime import datetime as _dt, timedelta as _td, timezone as _tz
                        _msk = _tz(_td(hours=3))
                        _dt_obj = _dt.fromtimestamp(sp.open_time / 1000, tz=_msk)
                        f.write(f"       {_dt_obj.strftime('%Y-%m-%d %H:%M')} MSK  ${sp.volume_usdt:,.0f}  price={sp.price:.4f}\n")

                f.write(f"\n     Время сканирования: {s.signal_time}\n")
                f.write("\n" + "─" * 80 + "\n\n")

            f.write("--- КОММЕНТАРИЙ ---\n")
            f.write(f"Найдено {len(signals)} символов с паттернами спайков.\n\n")
            f.write("Градация по часам (MSK):\n")
            f.write("  ПИК       — время наибольшей активности. Планировать вход ДО этого времени.\n")
            f.write("  Активно   — высокая вероятность всплеска. Хорошо для скальпинга.\n")
            f.write("  Умеренно  — средняя активность. Подходит для лимитных ордеров.\n")
            f.write("  Тихо      — минимальная активность. Не входить — рынок спит.\n\n")
            f.write("Рекомендации:\n")
            f.write("  - Проверять топ-3 символа вручную перед входом.\n")
            f.write("  - Не входить в «тихие» часы — ждать активности.\n")
            f.write("  - Использовать как фильтр в связке с другими скринерами.\n")

        f.write("\n" + "=" * 80 + "\n")

    logger.info("Отчёты сохранены в %s", REPORTS_DIR)


async def main() -> None:
    setup_logging("logs/run_5_screeners_1h.log", "INFO")
    logger.info("=" * 60)
    logger.info("ЗАПУСК 5 СКРИНЕРОВ НА 1 ЧАС")
    logger.info("Скринеры: uzkiy, yrovni, krugloe, impulse, zakonomer")
    logger.info("Отчёты: %s", REPORTS_DIR)
    logger.info("=" * 60)

    all_results: dict = {}
    start = time.monotonic()

    STAGGER_DELAY = 30

    tasks = []
    for i, (name, fn) in enumerate([
        ("uzkiy", _run_uzkiy),
        ("yrovni", _run_yrovni),
        ("krugloe", _run_krugloe),
        ("impulse", _run_impulse),
        ("zakonomer", _run_zakonomer),
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

    logger.info("Генерирую отчёты...")
    _generate_reports(all_results)

    logger.info("ГОТОВО!")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nОстановлен пользователем.")
