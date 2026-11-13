"""Скринер импульсов: резкое движение цены за rolling-окно.

Режимы работы:
- Опрос по REST (свечи) — периодический скан по расписанию.
- WebSocket ticker_stream — непрерывный мониторинг всех монет в реальном времени.

Запуск:  py bot_screener_impulse/main.py
Остановка: Ctrl+C
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))
_BOT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_BOT_DIR))

from bot_screener_impulse.printer import print_results
from bot_screener_impulse.scanner import ImpulseConfig, ImpulseSignal, analyze_symbol
from core.config import get_env_bool, get_env_float, get_env_int, load_bot_env
from core.logger import get_logger, setup_logging
from core.metrics import ScreenerMetrics

logger = get_logger(__name__)

MSK = timezone(timedelta(hours=3))


def _load_config() -> tuple[dict, ImpulseConfig]:
    load_bot_env(_BOT_DIR)

    raw_exclude = os.getenv("EXCLUDE_SYMBOLS", "BTCUSDT,ETHUSDT")
    exclude = [s.strip() for s in raw_exclude.split(",") if s.strip()]

    window_sec = get_env_int("WINDOW_SEC", 60)
    impulse_type = os.getenv("IMPULSE_TYPE", "price")
    continuous = get_env_bool("CONTINUOUS", True)

    env_cfg: dict = {
        "api_key": os.getenv("BYBIT_API_KEY", ""),
        "api_secret": os.getenv("BYBIT_API_SECRET", ""),
        "testnet": get_env_bool("TESTNET", True),
        "timeframe": os.getenv("TIMEFRAME", "1"),
        "scan_interval": get_env_int("SCAN_INTERVAL", 15),
        "min_turnover_24h": get_env_float("MIN_TURNOVER_24H", 30_000_000),
        "lookback_bars": get_env_int("LOOKBACK_BARS", 100),
        "exclude_symbols": exclude,
        "window_sec": window_sec,
        "impulse_type": impulse_type,
        "continuous": continuous,
    }

    impulse_cfg = ImpulseConfig(
        min_move_pct=get_env_float("MIN_MOVE_PCT", 4.0),
        confirmation_candles=get_env_int("CONFIRMATION_CANDLES", 0),
        window_sec=window_sec,
        impulse_type=impulse_type,
    )

    return env_cfg, impulse_cfg


async def _scan_one(
    fetcher: object,
    symbol: str,
    timeframe: str,
    lookback: int,
    impulse_cfg: ImpulseConfig,
    turnover_24h: float,
) -> ImpulseSignal | None:
    from bot_screener_klin.fetcher import Fetcher

    assert isinstance(fetcher, Fetcher)

    candles = await fetcher.get_klines(
        symbol=symbol,
        interval=timeframe,
        limit=lookback,
    )

    if not candles:
        return None

    return analyze_symbol(
        symbol=symbol,
        timeframe=timeframe,
        candles=candles,
        cfg=impulse_cfg,
        turnover_24h=turnover_24h,
    )


async def scan_once(
    cfg: dict, impulse_cfg: ImpulseConfig, metrics: ScreenerMetrics
) -> None:
    from bot_screener_impulse.printer import print_results
    from bot_screener_klin.fetcher import Fetcher

    fetcher = Fetcher(
        api_key=cfg["api_key"],
        api_secret=cfg["api_secret"],
        testnet=cfg["testnet"],
        metrics=metrics,
    )

    start = time.monotonic()

    logger.info("Получаю список символов...")
    filtered = await fetcher.get_filtered_symbols(cfg["min_turnover_24h"])
    total_symbols = len(await fetcher.get_all_linear_symbols())

    exclude = set(cfg["exclude_symbols"])
    filtered = [(s, t) for s, t in filtered if s not in exclude]

    logger.info(
        "Найдено %d символов с оборотом > $%sM (всего: %d, исключено: %d)",
        len(filtered),
        cfg["min_turnover_24h"] / 1_000_000,
        total_symbols,
        len(exclude),
    )

    tickers_map: dict[str, dict] = {}
    tickers = await fetcher.get_linear_tickers()
    for t in tickers:
        tickers_map[t.get("symbol", "")] = t

    results = []

    batch_size = 10
    for i in range(0, len(filtered), batch_size):
        batch = filtered[i : i + batch_size]
        tasks = [
            _scan_one(
                fetcher,
                sym,
                cfg["timeframe"],
                cfg["lookback_bars"],
                impulse_cfg,
                _get_turnover(tickers_map, sym),
            )
            for sym, _ in batch
        ]
        batch_results = await asyncio.gather(*tasks)

        for result in batch_results:
            if result is not None:
                logger.info(
                    "ИМПУЛЬС: %s %s move=%.2f%% window=%ss",
                    result.symbol,
                    result.direction,
                    result.move_pct,
                    result.window_sec,
                )
                results.append(result)

    results.sort(key=lambda r: r.move_pct, reverse=True)
    elapsed = time.monotonic() - start

    metrics.record_scan(elapsed, len(results), len(filtered))

    print_results(
        results=results,
        total_symbols=total_symbols,
        filtered_symbols=len(filtered),
        scan_time=elapsed,
    )


def _get_turnover(tickers_map: dict[str, dict], symbol: str) -> float:
    t = tickers_map.get(symbol, {})
    vol = t.get("turnover24h", "0")
    try:
        return float(vol)
    except (ValueError, TypeError):
        return 0.0


async def _ticker_callback(
    message: dict,
    price_history: dict[str, list[tuple[int, float]]],
    impulse_cfg: ImpulseConfig,
    results_queue: asyncio.Queue[ImpulseSignal],
    turnover_map: dict[str, float],
) -> None:
    """Обработка тика цены из WebSocket: rolling-окно и проверка порога."""
    try:
        data = message.get("data", {})
        if not isinstance(data, dict):
            return
        symbol = data.get("symbol", "")
        price = float(data.get("lastPrice", 0) or 0)
        if not symbol or price <= 0:
            return

        now_ms = int(time.time() * 1000)
        history = price_history.setdefault(symbol, [])
        history.append((now_ms, price))

        # Удаляем записи старше окна
        window_ms = impulse_cfg.window_sec * 1000
        cutoff_ms = now_ms - window_ms
        while history and history[0][0] < cutoff_ms:
            history.pop(0)

        if len(history) < 2:
            return

        base_price = history[0][1]
        if base_price <= 0:
            return

        move_pct = (price - base_price) / base_price * 100.0
        if abs(move_pct) < impulse_cfg.min_move_pct:
            return

        direction = "LONG" if move_pct > 0 else "SHORT"

        signal = ImpulseSignal(
            symbol=symbol,
            timeframe=str(impulse_cfg.window_sec) + "s",
            price=price,
            direction=direction,
            move_pct=round(abs(move_pct), 2),
            confirmed=True,
            turnover_24h=turnover_map.get(symbol, 0.0),
            signal_time=datetime.now(MSK).strftime("%H:%M:%S"),
            window_sec=impulse_cfg.window_sec,
        )
        await results_queue.put(signal)

        logger.info(
            "ИМПУЛЬС (WS): %s %s move=%.2f%% window=%ss",
            symbol,
            direction,
            signal.move_pct,
            impulse_cfg.window_sec,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Ошибка обработки тика импульса: %s", exc)


async def run_continuous(
    cfg: dict,
    impulse_cfg: ImpulseConfig,
    metrics: ScreenerMetrics,
    results_queue: asyncio.Queue[ImpulseSignal],
) -> None:
    """Непрерывный мониторинг через WebSocket ticker_stream."""
    from pybit.unified_trading import WebSocket

    ws = WebSocket(
        testnet=cfg["testnet"],
        channel_type="linear",
    )

    # Подписываемся на тикеры всех линейных пар
    symbols = await _fetch_universe_symbols(cfg)
    if not symbols:
        logger.warning("Не удалось получить список символов для WebSocket")
        return

    logger.info(
        "WebSocket мониторинг %d символов, окно=%ss",
        len(symbols),
        impulse_cfg.window_sec,
    )

    price_history: dict[str, list[tuple[int, float]]] = {}
    turnover_map: dict[str, float] = {}

    # Загружаем обороты для отображения
    try:
        tickers = await _fetch_tickers_map(cfg)
        for t in tickers:
            sym = t.get("symbol", "")
            vol = t.get("turnover24h", "0")
            try:
                turnover_map[sym] = float(vol)
            except (ValueError, TypeError):
                turnover_map[sym] = 0.0
    except Exception as exc:  # noqa: BLE001
        logger.warning("Не удалось загрузить обороты: %s", exc)

    def on_message(message: dict) -> None:
        asyncio.create_task(
            _ticker_callback(
                message,
                price_history,
                impulse_cfg,
                results_queue,
                turnover_map,
            )
        )

    ws.ticker_stream(symbol=symbols, callback=on_message)

    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        ws.exit()
        raise


async def _fetch_universe_symbols(cfg: dict) -> list[str]:
    from bot_screener_klin.fetcher import Fetcher

    fetcher = Fetcher(
        api_key=cfg["api_key"],
        api_secret=cfg["api_secret"],
        testnet=cfg["testnet"],
        metrics=ScreenerMetrics(),
    )
    filtered = await fetcher.get_filtered_symbols(cfg["min_turnover_24h"])
    exclude = set(cfg["exclude_symbols"])
    return [s for s, _ in filtered if s not in exclude]


async def _fetch_tickers_map(cfg: dict) -> list[dict]:
    from bot_screener_klin.fetcher import Fetcher

    fetcher = Fetcher(
        api_key=cfg["api_key"],
        api_secret=cfg["api_secret"],
        testnet=cfg["testnet"],
        metrics=ScreenerMetrics(),
    )
    return await fetcher.get_linear_tickers()


async def main() -> None:
    setup_logging("logs/screener_impulse.log", "INFO")
    env_cfg, impulse_cfg = _load_config()
    metrics = ScreenerMetrics()

    logger.info(
        "Скринер импульсов: type=%s, window=%ss, min_move=%.1f%%, min_turnover=$%sM, continuous=%s",
        impulse_cfg.impulse_type,
        impulse_cfg.window_sec,
        impulse_cfg.min_move_pct,
        env_cfg["min_turnover_24h"] / 1_000_000,
        env_cfg["continuous"],
    )

    results_queue: asyncio.Queue[ImpulseSignal] = asyncio.Queue()

    if env_cfg["continuous"]:
        ws_task = asyncio.create_task(
            run_continuous(env_cfg, impulse_cfg, metrics, results_queue)
        )

        # Обрабатываем сигналы из очереди
        try:
            while True:
                signal = await results_queue.get()
                print_results(
                    results=[signal],
                    total_symbols=0,
                    filtered_symbols=0,
                    scan_time=0.0,
                )
        except asyncio.CancelledError:
            ws_task.cancel()
            raise
    else:
        while True:
            try:
                await scan_once(env_cfg, impulse_cfg, metrics)
            except (OSError, ValueError):
                logger.exception("Ошибка сканирования")

            if metrics.scan_count % 10 == 0 and metrics.scan_count > 0:
                logger.info("МЕТРИКИ:\n%s", metrics.report())

            logger.info(
                "Следующее сканирование через %d сек...", env_cfg["scan_interval"]
            )
            await asyncio.sleep(env_cfg["scan_interval"])


if __name__ == "__main__":
    asyncio.run(main())
