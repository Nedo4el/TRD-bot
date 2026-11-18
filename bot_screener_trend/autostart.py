"""Авто-запуск robot_trend по сигналам скринера.

Скринер нашёл тренд по правилам робота — монета добавляется в список
robot_trend и процесс поднимается тем же путём, что кнопка в дашборде
(``dashboard.robots``): отдельный процесс на каждую монету.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from bot_screener_trend.scanner import ScanResult
from dashboard.robot_coins import MAX_SYMBOLS_PER_ROBOT

logger = logging.getLogger(__name__)

ROBOT_NAME = "robot_trend"


@dataclass(frozen=True)
class AutoStartConfig:
    """Настройки авто-запуска робота по сигналам."""

    enabled: bool = False
    robot_name: str = ROBOT_NAME
    max_per_scan: int = 3


def pick_symbols(
    results: list[ScanResult],
    existing: list[str],
    per_scan: int,
    total_limit: int = MAX_SYMBOLS_PER_ROBOT,
) -> list[str]:
    """Выбрать монеты для запуска: топ по score, которых ещё нет у робота.

    Args:
        results: найденные сигналы.
        existing: монеты, уже закреплённые за роботом.
        per_scan: максимум запусков за одно сканирование.
        total_limit: лимит монет на робота (защита от «выбрать всё»).

    Returns:
        Список тикеров в порядке силы сигнала.
    """
    free = max(0, total_limit - len(existing))
    limit = min(per_scan, free)
    if limit <= 0:
        return []

    picked: list[str] = []
    for result in sorted(results, key=lambda r: r.score, reverse=True):
        if result.symbol in existing or result.symbol in picked:
            continue
        picked.append(result.symbol)
        if len(picked) >= limit:
            break
    return picked


def start_picked(robot: str, symbols: list[str]) -> list[str]:
    """Добавить монеты роботу и запустить процессы. Возвращает добавленные."""
    if not symbols:
        return []

    from dashboard import robots as dashboard_robots

    started: list[str] = []
    for symbol in symbols:
        ok, message = dashboard_robots.add_robot_symbol(robot, symbol)
        if ok:
            started.append(symbol)
            logger.info("autostart: %s — %s", symbol, message)
        else:
            logger.warning("autostart: %s не добавлен — %s", symbol, message)

    if started:
        ok, message = dashboard_robots.start_robot(robot)
        if ok:
            logger.info("autostart: %s", message)
        else:
            logger.warning("autostart: робот не стартовал — %s", message)
    return started
