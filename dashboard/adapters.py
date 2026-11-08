"""Адаптеры запуска всех скринеров и нормализация результатов.

Каждый адаптер читает .env своего скринера, сканирует ограниченное
число самых ликвидных пар и возвращает список Signal. Сетевые и
локальные ошибки по отдельным символам не роняют весь прогон.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

from core.logger import get_logger
from dashboard.config import PROJECT_ROOT, ScreenerEnv, ScreenerSpec
from dashboard.fetcher import DashboardFetcher
from dashboard.models import Signal, make_signal

logger = get_logger(__name__)

Worker = Callable[[tuple[str, float]], Awaitable[list[Signal]]]

_STRONG = 10.0
_MEDIUM = 7.0
_WEAK = 5.0


def _now_iso() -> str:
    """Текущее время в UTC (ISO-8601)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _env(spec: ScreenerSpec) -> ScreenerEnv:
    """Прочитать .env скринера."""
    return ScreenerEnv(PROJECT_ROOT / spec.dir_name)


def _impulse_label(move_pct: float) -> str:
    """Словесная оценка силы импульса."""
    if move_pct >= _STRONG:
        return "очень сильный"
    if move_pct >= _MEDIUM:
        return "сильный"
    if move_pct >= _WEAK:
        return "средний"
    return "слабый"


def _new_fetcher(env: ScreenerEnv) -> DashboardFetcher:
    """Fetcher дашборда (общий, с ретраями на 403/429)."""
    return DashboardFetcher(
        api_key=env.string("BYBIT_API_KEY"),
        api_secret=env.string("BYBIT_API_SECRET"),
        testnet=env.boolean("TESTNET", False),
    )


async def _universe(
    fetcher: DashboardFetcher,
    env: ScreenerEnv,
    spec: ScreenerSpec,
    default_turnover: float,
) -> list[tuple[str, float]]:
    """Отфильтровать пары по обороту и обрезать до лимита."""
    min_turnover = env.number("MIN_TURNOVER_24H", default_turnover)
    exclude = set(env.names("EXCLUDE_SYMBOLS", ("BTCUSDT", "ETHUSDT")))
    filtered = await fetcher.get_filtered_symbols(min_turnover)
    return [(sym, tv) for sym, tv in filtered if sym not in exclude][: spec.max_symbols]


async def _batched(
    items: list[tuple[str, float]],
    size: int,
    worker: Worker,
) -> list[Signal]:
    """Выполнить worker пачками, переживая ошибки отдельных символов."""
    signals: list[Signal] = []
    for start in range(0, len(items), size):
        batch = items[start : start + size]
        results = await asyncio.gather(
            *(worker(item) for item in batch),
            return_exceptions=True,
        )
        for item, result in zip(batch, results, strict=True):
            if isinstance(result, BaseException):
                logger.warning("Скринер: ошибка по %s: %s", item[0], result)
                continue
            signals.extend(result)
    return signals


async def scan_impulse(spec: ScreenerSpec) -> list[Signal]:
    """Импульс: движение тела свечи >= MIN_MOVE_PCT с подтверждением."""
    from bot_screener_impulse.scanner import ImpulseConfig, analyze_symbol

    env = _env(spec)
    cfg = ImpulseConfig(
        min_move_pct=env.number("MIN_MOVE_PCT", 4.0),
        confirmation_candles=env.integer("CONFIRMATION_CANDLES", 2),
    )
    timeframe = env.string("TIMEFRAME", "1")
    lookback = env.integer("LOOKBACK_BARS", 100)
    fetcher = _new_fetcher(env)
    universe = await _universe(fetcher, env, spec, 30_000_000)

    async def work(item: tuple[str, float]) -> list[Signal]:
        symbol, turnover = item
        candles = await fetcher.get_klines(symbol, timeframe, lookback)
        result = analyze_symbol(
            symbol,
            timeframe,
            candles,
            cfg=cfg,
            turnover_24h=turnover,
        )
        if not result or not result.confirmed or result.move_pct <= 0:
            return []
        return [
            make_signal(
                screener=spec.name,
                title=spec.title,
                symbol=symbol,
                side=result.direction,
                price=result.price,
                score=min(100.0, result.move_pct * 10),
                label=_impulse_label(result.move_pct),
                turnover_24h=turnover,
                found_at=_now_iso(),
                detail={
                    "move_pct": round(result.move_pct, 2),
                    "timeframe": timeframe,
                },
            )
        ]

    return await _batched(universe, 10, work)


async def scan_klin(spec: ScreenerSpec) -> list[Signal]:
    """Сжатие диапазона: клинья, треугольники, BB/ATR-компрессия."""
    from bot_screener_klin.scanner import scan_symbol

    env = _env(spec)
    timeframes = env.names("TIMEFRAMES", ("1", "3", "5"))
    lookback = env.integer("LOOKBACK_BARS", 50)
    min_score = env.integer("MIN_SCORE", 40)
    max_price = env.number("MAX_PRICE", 0.0)
    spread_max = env.number("SPREAD_MAX_PCT", 0.05)
    fetcher = _new_fetcher(env)
    universe = await _universe(fetcher, env, spec, 10_000_000)
    spread_cache: dict[str, float] = {}

    async def work(item: tuple[str, float]) -> list[Signal]:
        symbol, turnover = item
        found: list[Signal] = []
        for timeframe in timeframes:
            candles = await fetcher.get_klines(symbol, timeframe, lookback)
            if not candles:
                continue
            if max_price > 0 and candles[-1]["close"] > max_price:
                continue
            spread = spread_cache.get(symbol)
            if spread is None:
                spread = await fetcher.get_spread(symbol)
                spread_cache[symbol] = spread
            if spread > spread_max:
                continue
            result = scan_symbol(
                symbol,
                f"{timeframe}m",
                candles,
                turnover_24h=turnover,
                spread_pct=spread,
            )
            if not result.squeeze_type or result.score < min_score:
                continue
            found.append(
                make_signal(
                    screener=spec.name,
                    title=spec.title,
                    symbol=symbol,
                    side=result.direction or "NEUTRAL",
                    price=result.price,
                    score=result.score,
                    label=result.squeeze_type.value,
                    turnover_24h=turnover,
                    found_at=_now_iso(),
                    detail={
                        "timeframe": f"{timeframe}m",
                        "adx": round(result.adx_value, 1),
                        "atr_pct": round(result.atr_percent, 2),
                        "bb_width": round(result.bb_width, 2),
                        "stop": round(result.stop_loss, 8),
                        "take": round(result.take_profit, 8),
                    },
                )
            )
        return found

    return await _batched(universe, 10, work)


async def scan_uzkiy(spec: ScreenerSpec) -> list[Signal]:
    """Узкий диапазон: доля тихих свечей в выборке."""
    from bot_screener_uzkiy.scanner import ScanConfig, analyze_symbol

    env = _env(spec)
    cfg = ScanConfig()
    timeframe = env.string("TIMEFRAME", "1")
    lookback = env.integer("LOOKBACK_BARS", 100)
    fetcher = _new_fetcher(env)
    universe = await _universe(fetcher, env, spec, 30_000_000)

    async def work(item: tuple[str, float]) -> list[Signal]:
        symbol, turnover = item
        candles = await fetcher.get_klines(symbol, timeframe, lookback)
        result = analyze_symbol(
            symbol,
            timeframe,
            candles,
            cfg=cfg,
            turnover_24h=turnover,
        )
        if result.price <= 0:
            return []
        quiet_ratio = result.quiet_candles / max(1, result.total_candles)
        return [
            make_signal(
                screener=spec.name,
                title=spec.title,
                symbol=symbol,
                price=result.price,
                score=round(quiet_ratio * 100, 1),
                label=result.status,
                turnover_24h=turnover,
                found_at=_now_iso(),
                detail={
                    "timeframe": timeframe,
                    "avg_width_pct": round(result.avg_width_pct, 2),
                    "max_width_pct": round(result.max_width_pct, 2),
                    "quiet": f"{result.quiet_candles}/{result.total_candles}",
                },
            )
        ]

    return await _batched(universe, 10, work)


async def scan_krugloe(spec: ScreenerSpec) -> list[Signal]:
    """Круглые числа: близость цены к психологическому уровню."""
    from bot_screener_krugloe.scanner import scan_symbol

    env = _env(spec)
    timeframe = env.string("TIMEFRAME", "5")
    proximity = max(0.01, env.number("PROXIMITY_PCT", 5.0))
    max_price = env.number("MAX_PRICE", 0.0)
    fetcher = _new_fetcher(env)
    universe = await _universe(fetcher, env, spec, 10_000_000)

    async def work(item: tuple[str, float]) -> list[Signal]:
        symbol, turnover = item
        candles = await fetcher.get_klines(symbol, timeframe, 1)
        if not candles:
            return []
        result = scan_symbol(
            symbol,
            f"{timeframe}m",
            candles[-1]["close"],
            proximity_pct=proximity,
            max_price=max_price,
            turnover_24h=turnover,
        )
        if result is None:
            return []
        score = 100.0 * (1 - result.proximity_pct / proximity)
        return [
            make_signal(
                screener=spec.name,
                title=spec.title,
                symbol=symbol,
                price=result.price,
                score=round(max(0.0, min(100.0, score)), 1),
                label=f"уровень {result.level:g}",
                turnover_24h=turnover,
                found_at=_now_iso(),
                detail={
                    "level": result.level,
                    "distance_pct": result.proximity_pct,
                    "up": result.nearest_up,
                    "down": result.nearest_down,
                },
            )
        ]

    return await _batched(universe, 10, work)


async def scan_trend(spec: ScreenerSpec) -> list[Signal]:
    """Тренд: структура HH/HL (LONG) или LH/LL (SHORT)."""
    from bot_screener_trend.scanner import ScanConfig, analyze_symbol

    env = _env(spec)
    min_turnover = env.number("MIN_TURNOVER_24H", 10_000_000)
    exclude = env.names("EXCLUDE_SYMBOLS", ())
    cfg = ScanConfig(
        swing_window=env.integer("SWING_WINDOW", 5),
        ema_fast=env.integer("EMA_FAST", 20),
        ema_slow=env.integer("EMA_SLOW", 50),
        adx_min=env.number("ADX_MIN", 20.0),
        min_swings=env.integer("MIN_SWINGS", 3),
        min_turnover_24h=min_turnover,
        exclude_symbols=exclude,
    )
    timeframe = env.string("TIMEFRAME", "1")
    lookback = env.integer("LOOKBACK_BARS", 100)
    fetcher = _new_fetcher(env)
    universe = await _universe(fetcher, env, spec, min_turnover)

    async def work(item: tuple[str, float]) -> list[Signal]:
        symbol, turnover = item
        candles = await fetcher.get_klines(symbol, timeframe, limit=lookback)
        if not candles:
            return []
        candles.sort(key=lambda candle: candle["open_time"])
        result = analyze_symbol(
            symbol,
            candles,
            cfg=cfg,
            timeframe=timeframe,
            turnover_24h=turnover,
        )
        if result is None:
            return []
        side = "LONG" if result.trend == "UP" else "SHORT"
        label = "восходящий" if result.trend == "UP" else "нисходящий"
        return [
            make_signal(
                screener=spec.name,
                title=spec.title,
                symbol=symbol,
                side=side,
                price=result.price,
                score=min(100.0, float(result.score)),
                label=label,
                turnover_24h=turnover,
                found_at=_now_iso(),
                detail={
                    "timeframe": timeframe,
                    "hh": result.hh_count,
                    "hl": result.hl_count,
                    "lh": result.lh_count,
                    "ll": result.ll_count,
                    "adx": round(result.adx_value, 1),
                    "rsi": round(result.rsi_value, 1),
                },
            )
        ]

    return await _batched(universe, 5, work)


async def scan_pump(spec: ScreenerSpec) -> list[Signal]:
    """Накопление: вероятность памп-движения по набору факторов."""
    from bot_screener_pump.scanner import (
        AccumulationConfig,
        analyze_symbol,
        passes_basic_filters,
    )

    env = _env(spec)
    cfg = AccumulationConfig(
        analysis_period=env.integer("ANALYSIS_PERIOD", 100),
        volume_lookback=env.integer("VOLUME_LOOKBACK", 20),
        obv_divergence_lookback=env.integer("OBV_LOOKBACK", 30),
        bb_period=env.integer("BB_PERIOD", 20),
        bb_dev=env.number("BB_DEV", 2.0),
        rsi_period=env.integer("RSI_PERIOD", 14),
        smart_money_lookback=env.integer("SMART_MONEY_LOOKBACK", 7),
        max_price=env.number("MAX_PRICE", 0.03),
        min_total_volume_usd=env.number("MIN_TOTAL_VOLUME_USD", 50_000_000),
        range_max=env.number("RANGE_MAX", 999.0),
        range_min=env.number("RANGE_MIN", 0.0),
        volume_spike=env.number("VOLUME_SPIKE", 0.0),
        bb_compression=env.number("BB_COMPRESSION", 999.0),
        min_volume_usd=env.number("MIN_VOLUME_USD", 0.0),
        min_trades=env.integer("MIN_TRADES", 0),
        max_spread=env.number("MAX_SPREAD", 999.0),
        min_days_listed=env.integer("MIN_DAYS_LISTED", 0),
        max_volatility_3d=env.number("MAX_VOLATILITY_3D", 999.0),
        max_drop_7d=env.number("MAX_DROP_7D", 999.0),
        min_pump_probability=env.number("MIN_PUMP_PROBABILITY", 0.0),
        weight_range=env.number("WEIGHT_RANGE", 0.0),
        weight_volume=env.number("WEIGHT_VOLUME", 0.0),
        weight_obv=env.number("WEIGHT_OBV", 0.0),
        weight_bb=env.number("WEIGHT_BB", 0.0),
        weight_smart_money=env.number("WEIGHT_SMART_MONEY", 0.0),
        weight_outflow=env.number("WEIGHT_OUTFLOW", 0.0),
        weight_rsi=env.number("WEIGHT_RSI", 0.0),
        weight_liquidity=env.number("WEIGHT_LIQUIDITY", 0.0),
    )
    threshold = max(0.3, cfg.min_pump_probability)
    timeframe = env.string("TIMEFRAME", "1D")
    lookback = env.integer("LOOKBACK_BARS", 100)
    fetcher = _new_fetcher(env)
    universe = await _universe(fetcher, env, spec, 1_000_000)
    spread_cache: dict[str, float] = {}

    async def work(item: tuple[str, float]) -> list[Signal]:
        symbol, turnover = item
        candles = await fetcher.get_klines(symbol, timeframe, lookback)
        if not candles:
            return []
        passed, _ = passes_basic_filters(candles=candles, cfg=cfg)
        if not passed:
            return []
        spread = spread_cache.get(symbol)
        if spread is None:
            spread = await fetcher.get_spread(symbol)
            spread_cache[symbol] = spread
        result = analyze_symbol(
            symbol,
            timeframe,
            candles,
            cfg=cfg,
            spread_pct=spread,
            turnover_24h=turnover,
        )
        if result.pump_probability < threshold:
            return []
        return [
            make_signal(
                screener=spec.name,
                title=spec.title,
                symbol=symbol,
                price=result.price,
                score=round(result.pump_probability * 100, 1),
                label=result.status,
                turnover_24h=turnover,
                found_at=_now_iso(),
                detail={
                    "timeframe": timeframe,
                    "range_pct": round(result.range_pct, 2),
                    "spread_pct": round(result.spread_pct, 3),
                },
            )
        ]

    return await _batched(universe, 10, work)


async def scan_yrovni(spec: ScreenerSpec) -> list[Signal]:
    """Volume Profile: POC по периодам и дневные уровни."""
    from bot_screener_yrovni.scanner import POC_PERIODS, scan_symbol

    env = _env(spec)
    num_bins = 100
    proximity = env.number("PROXIMITY_PCT", 10.0)
    fetcher = _new_fetcher(env)
    universe = await _universe(fetcher, env, spec, 50_000_000)

    async def work(item: tuple[str, float]) -> list[Signal]:
        symbol, turnover = item
        candles_by_period: dict[str, list[dict]] = {}
        for period, (interval, num_candles) in POC_PERIODS.items():
            candles = await fetcher.get_klines(symbol, interval, num_candles)
            if candles:
                candles_by_period[period] = candles
        hourly = await fetcher.get_klines(symbol, "60", 48)
        if not candles_by_period or not hourly:
            return []
        result = scan_symbol(
            symbol,
            candles_by_period,
            hourly,
            num_bins=num_bins,
            proximity_pct=proximity,
        )
        if not result.poc_levels and not result.daily_levels:
            return []
        distances = [level.distance_pct for level in result.poc_levels]
        distances += [level.distance_pct for level in result.daily_levels]
        closest = min(distances)
        score = max(0.0, 100.0 - closest * 10)
        label = (
            f"POC {result.poc_levels[0].period} {result.poc_levels[0].distance_pct:g}%"
            if result.poc_levels
            else f"уровень {result.daily_levels[0].level_type}"
        )
        return [
            make_signal(
                screener=spec.name,
                title=spec.title,
                symbol=symbol,
                price=result.current_price,
                score=round(min(100.0, score), 1),
                label=label,
                turnover_24h=turnover,
                found_at=_now_iso(),
                detail={
                    "poc": [
                        {
                            "period": level.period,
                            "price": level.poc_price,
                            "distance_pct": level.distance_pct,
                        }
                        for level in result.poc_levels[:4]
                    ],
                    "daily": [
                        {
                            "type": level.level_type,
                            "price": level.price,
                            "distance_pct": level.distance_pct,
                        }
                        for level in result.daily_levels[:4]
                    ],
                },
            )
        ]

    return await _batched(universe, 5, work)


async def scan_zakonomer(spec: ScreenerSpec) -> list[Signal]:
    """Закономерность: частота прострелов >= SPIKE_PCT за период."""
    from bot_screener_zakonomer.scanner import SpikeConfig, analyze_symbol

    env = _env(spec)
    min_turnover = env.number("MIN_TURNOVER_24H", 30_000_000)
    exclude = env.names("EXCLUDE_SYMBOLS", ())
    lookback_days = env.integer("LOOKBACK_DAYS", 30)
    timeframe = env.string("TIMEFRAME", "1")
    cfg = SpikeConfig(
        spike_pct=env.number("SPIKE_PCT", 0.03),
        window_sec=env.integer("SPIKE_WINDOW_SEC", 20),
        cooldown_sec=env.integer("SPIKE_COOLDOWN_SEC", 60),
        lookback_days=lookback_days,
        interval=timeframe,
        min_turnover_24h=min_turnover,
        exclude_symbols=exclude,
    )
    fetcher = _new_fetcher(env)
    universe = await _universe(fetcher, env, spec, min_turnover)
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - lookback_days * 24 * 3600 * 1000

    async def work(item: tuple[str, float]) -> list[Signal]:
        symbol, turnover = item
        candles = await fetcher.get_klines_range(
            symbol=symbol,
            interval=timeframe,
            start_ms=start_ms,
            end_ms=now_ms,
            limit=1000,
        )
        if not candles:
            return []
        stats = analyze_symbol(symbol, candles, cfg=cfg)
        if stats is None or stats.total <= 0:
            return []
        if stats.longs > stats.shorts:
            side = "LONG"
        elif stats.shorts > stats.longs:
            side = "SHORT"
        else:
            side = "NEUTRAL"
        last_spike = stats.spikes[-1] if stats.spikes else None
        return [
            make_signal(
                screener=spec.name,
                title=spec.title,
                symbol=symbol,
                side=side,
                price=last_spike.price if last_spike else 0.0,
                score=min(100.0, stats.total * 5.0),
                label=f"{stats.total} спайков",
                turnover_24h=turnover,
                found_at=_now_iso(),
                detail={
                    "longs": stats.longs,
                    "shorts": stats.shorts,
                    "days": lookback_days,
                    "max_bar_range_pct": round(
                        stats.max_bar_range_pct,
                        2,
                    ),
                    "last_spike": (
                        {
                            "ts_ms": last_spike.ts_ms,
                            "direction": last_spike.direction,
                            "move_pct": round(last_spike.move_pct, 2),
                        }
                        if last_spike
                        else None
                    ),
                },
            )
        ]

    return await _batched(universe, 5, work)


ADAPTERS: dict[str, Callable[[ScreenerSpec], Awaitable[list[Signal]]]] = {
    "impulse": scan_impulse,
    "klin": scan_klin,
    "uzkiy": scan_uzkiy,
    "krugloe": scan_krugloe,
    "trend": scan_trend,
    "pump": scan_pump,
    "yrovni": scan_yrovni,
    "zakonomer": scan_zakonomer,
}
