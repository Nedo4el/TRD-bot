"""Запуск POC-скринера: цикл по закрытию 5-минутной свечи и POST сигналов.

Примеры:
    python -m bot_screener_yrovni.poc_scanner.main SOLUSDT --once
    python -m bot_screener_yrovni.poc_scanner.main SOLUSDT ETHUSDT
    python -m bot_screener_yrovni.poc_scanner.main SOLUSDT --prune
"""

from __future__ import annotations

import json
import os
import signal as signal_module
import sys
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from bot_screener_yrovni.poc_scanner.config import POCConfig
from bot_screener_yrovni.poc_scanner.scanner import POCScanner
from bot_screener_yrovni.poc_scanner.signals import POCSignal
from core.logger import get_logger, setup_logging

logger = get_logger(__name__)

POLL_SEC = 300
CLOSE_GRACE_SEC = 5
ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


def symbols_from_env() -> list[str]:
    """Символы из аргументов командной строки или из ``POC_SYMBOLS``."""
    from_args = [arg.upper() for arg in sys.argv[1:] if not arg.startswith("--")]
    if from_args:
        return from_args
    return [
        part.strip().upper()
        for part in os.getenv("POC_SYMBOLS", "").split(",")
        if part.strip()
    ]


def wait_for_5m_close() -> None:
    """Спать до следующей границы 5 минут плюс небольшая задержка."""
    now = time.time()
    target = (int(now) // POLL_SEC + 1) * POLL_SEC + CLOSE_GRACE_SEC
    delay = max(1.0, target - now)
    logger.info("следующий скан через %.0f с", delay)
    time.sleep(delay)


def post_signal(
    signal: POCSignal,
    endpoint: str,
    timeout: float = 5.0,
) -> bool:
    """Отправить сигнал роботу. Недоступный робот не роняет цикл."""
    body = json.dumps(signal.to_dict(), ensure_ascii=False).encode("utf-8")
    request = Request(
        endpoint,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            logger.info("сигнал %s отправлен: HTTP %s", signal.symbol, response.status)
            return True
    except HTTPError as exc:
        logger.warning("робот ответил HTTP %s на сигнал %s", exc.code, signal.symbol)
    except (URLError, TimeoutError) as exc:
        logger.warning("робот недоступен (%s): %s", endpoint, exc)
    return False


def run_once(scanners: list[POCScanner]) -> int:
    """Один проход по всем символам; вернуть число отправленных сигналов."""
    sent = 0
    for scanner in scanners:
        try:
            found = scanner.refresh()
        except Exception:
            logger.exception("%s: скан упал", scanner.symbol)
            continue
        if found is None:
            logger.info("%s: сигналов нет", scanner.symbol)
            continue
        if post_signal(
            found,
            scanner.config.signal_endpoint,
            scanner.config.signal_timeout_sec,
        ):
            sent += 1
    return sent


def prune(scanners: list[POCScanner]) -> int:
    """Удалить уровни, чьи окна больше не считаются (после смены POC_WINDOWS)."""
    total = 0
    for scanner in scanners:
        removed = scanner.store.prune_stale(
            scanner.symbol,
            set(scanner.config.windows),
        )
        logger.info("%s: удалено устаревших уровней: %d", scanner.symbol, removed)
        total += removed
    return total


def main() -> None:
    """Собрать сканеры и крутить цикл (или один проход с ``--once``)."""
    setup_logging("logs/poc_scanner.log")
    once = any(arg.lower() == "--once" for arg in sys.argv[1:])
    prune_only = any(arg.lower() == "--prune" for arg in sys.argv[1:])
    symbols = symbols_from_env()
    if not symbols:
        logger.error("не заданы символы: укажите их аргументами или в POC_SYMBOLS")
        raise SystemExit(2)

    scanners = [
        POCScanner(POCConfig.from_env(symbol, ENV_FILE), symbol) for symbol in symbols
    ]
    logger.info("POC-скринер: %s", ", ".join(symbols))

    stop: dict[str, bool] = {"flag": False}

    def _handle(_signum: int, _frame: Any) -> None:
        stop["flag"] = True
        logger.info("остановка: дорабатываю текущий проход")

    signal_module.signal(signal_module.SIGINT, _handle)
    if hasattr(signal_module, "SIGTERM"):
        signal_module.signal(signal_module.SIGTERM, _handle)

    try:
        if prune_only:
            prune(scanners)
            return
        if once:
            run_once(scanners)
            return
        while not stop["flag"]:
            wait_for_5m_close()
            if stop["flag"]:
                break
            run_once(scanners)
    finally:
        for scanner in scanners:
            scanner.close()


if __name__ == "__main__":
    main()
