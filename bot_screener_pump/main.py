"""Accumulation скринер — поиск накопления крупных игроков.

Запуск:  python bot_screener_pump/main.py
Остановка: Ctrl+C
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))
_BOT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_BOT_DIR))

from bot_screener_pump.printer import print_results
from bot_screener_pump.scanner import (
    AccumulationConfig,
    AccumulationSignal,
    analyze_symbol,
)
from core.config import get_env_bool, get_env_float, get_env_int, load_bot_env
from core.logger import get_logger, setup_logging
from core.metrics import ScreenerMetrics

logger = get_logger(__name__)


def _get_trades_24h(tickers_map: dict[str, dict], symbol: str) -> int:
    """Получить количество сделок за 24ч из тикера (volume24h как прокси)."""
    t = tickers_map.get(symbol, {})
    vol = t.get("volume24h", "0")
    try:
        return int(float(vol))
    except (ValueError, TypeError):
        return 0


def _load_config() -> tuple[dict, AccumulationConfig]:
    """Загрузить конфигурацию из .env."""
    load_bot_env(_BOT_DIR)

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


async def _scan_one(
    fetcher: object,
    symbol: str,
    timeframe: str,
    lookback: int,
    acc_cfg: AccumulationConfig,
    spread_cache: dict[str, float],
    turnover_24h: float = 0.0,
    trades_24h: int = 0,
) -> AccumulationSignal | None:
    """Просканировать один символ."""
    from bot_screener_klin.fetcher import Fetcher
    from bot_screener_pump.scanner import passes_basic_filters

    assert isinstance(fetcher, Fetcher)

    candles = await fetcher.get_klines(
        symbol=symbol,
        interval=timeframe,
        limit=lookback,
    )

    if not candles:
        return None

    spread = spread_cache.get(symbol, 0.0)
    if spread == 0.0:
        spread = await fetcher.get_spread(symbol)
        spread_cache[symbol] = spread

    ok, reason = passes_basic_filters(
        candles=candles,
        cfg=acc_cfg,
    )
    if not ok:
        logger.debug("ФИЛЬТР %s: %s", symbol, reason)
        return None

    result = analyze_symbol(
        symbol=symbol,
        timeframe=timeframe,
        candles=candles,
        cfg=acc_cfg,
        spread_pct=spread,
        trades_24h=trades_24h,
        turnover_24h=turnover_24h,
    )

    if result.pump_probability > 0:
        logger.info(
            "АНАЛИЗ: %s [%s] P=%.1f%% status=%s",
            result.symbol,
            result.timeframe,
            result.pump_probability * 100,
            result.status,
        )

    return result


async def scan_once(
    env_cfg: dict,
    acc_cfg: AccumulationConfig,
    metrics: ScreenerMetrics,
) -> None:
    """Один прогон сканирования."""
    from bot_screener_klin.fetcher import Fetcher

    fetcher = Fetcher(
        api_key=env_cfg["api_key"],
        api_secret=env_cfg["api_secret"],
        testnet=env_cfg["testnet"],
        metrics=metrics,
    )

    start = time.monotonic()

    logger.info("Получаю список символов...")
    filtered = await fetcher.get_filtered_symbols(env_cfg["min_turnover_24h"])
    total_symbols = len(await fetcher.get_all_linear_symbols())

    exclude = set(env_cfg["exclude_symbols"])
    filtered = [(s, t) for s, t in filtered if s not in exclude]

    logger.info(
        "Найдено %d символов с оборотом > $%sM (всего: %d, исключено: %d)",
        len(filtered),
        env_cfg["min_turnover_24h"] / 1_000_000,
        total_symbols,
        len(exclude),
    )

    results: list[AccumulationSignal] = []
    spread_cache: dict[str, float] = {}

    tickers_map: dict[str, dict] = {}
    tickers = await fetcher.get_linear_tickers()
    for t in tickers:
        tickers_map[t.get("symbol", "")] = t

    batch_size = 10
    for i in range(0, len(filtered), batch_size):
        batch = filtered[i : i + batch_size]
        tasks = [
            _scan_one(
                fetcher,
                sym,
                env_cfg["timeframe"],
                env_cfg["lookback_bars"],
                acc_cfg,
                spread_cache,
                turnover_24h=turnover,
                trades_24h=_get_trades_24h(tickers_map, sym),
            )
            for sym, turnover in batch
        ]
        batch_results = await asyncio.gather(*tasks)

        for result in batch_results:
            if result and result.pump_probability >= acc_cfg.min_pump_probability:
                results.append(result)

    results.sort(key=lambda r: r.pump_probability, reverse=True)
    elapsed = time.monotonic() - start

    metrics.record_scan(elapsed, len(results), len(filtered))

    print_results(
        results=results,
        total_symbols=total_symbols,
        filtered_symbols=len(filtered),
        scan_time=elapsed,
        min_probability=acc_cfg.min_pump_probability,
    )


async def main() -> None:
    """Главный цикл скринера."""
    setup_logging("logs/screener_pump.log", "INFO")
    env_cfg, acc_cfg = _load_config()
    metrics = ScreenerMetrics()

    logger.info(
        "Accumulation-скринер запущен: timeframe=%s, interval=%ss, "
        "min_turnover=$%sM, analysis_period=%d",
        env_cfg["timeframe"],
        env_cfg["scan_interval"],
        env_cfg["min_turnover_24h"] / 1_000_000,
        acc_cfg.analysis_period,
    )

    while True:
        try:
            await scan_once(env_cfg, acc_cfg, metrics)
        except (OSError, ValueError):
            logger.exception("Ошибка сканирования")

        if metrics.scan_count % 5 == 0 and metrics.scan_count > 0:
            logger.info("МЕТРИКИ:\n%s", metrics.report())

        logger.info("Следующее сканирование через %d сек...", env_cfg["scan_interval"])
        await asyncio.sleep(env_cfg["scan_interval"])


if __name__ == "__main__":
    asyncio.run(main())
