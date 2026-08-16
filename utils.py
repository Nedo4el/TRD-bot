"""Вспомогательные функции: повторные попытки, backoff, хелперы."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Any, ParamSpec, TypeVar

from logger import get_logger

logger = get_logger(__name__)

# Типы для аннотаций декораторов
P = ParamSpec("P")
T = TypeVar("T")


def exponential_backoff(attempt: int, base: float = 1.0, cap: float = 60.0) -> float:
    """Рассчитать паузу для повторной попытки.

    Задержка растёт экспоненциально: base * 2^attempt, но не больше cap.
    Пример: 1с, 2с, 4с, 8с, ..., максимум 60с.

    Args:
        attempt: номер текущей попытки (0 — первая).
        base: начальная задержка в секундах.
        cap: максимальная задержка в секундах.

    Returns:
        Пауза в секундах перед следующей попыткой.
    """
    return float(min(base * (2**attempt), cap))


def retry(
    max_retries: int = 3,
    base_delay: float = 1.0,
    exceptions: tuple[type[Exception], ...] = (Exception,),
) -> Callable[[Callable[P, T]], Callable[P, T]]:
    """Декоратор: повторять вызов функции при исключениях.

    Args:
        max_retries: сколько раз повторить после первого отказа.
        base_delay: начальная пауза между попытками (растёт экспоненциально).
        exceptions: какие исключения считаются «временными сбоями».
    """

    def decorator(func: Callable[P, T]) -> Callable[P, T]:
        @wraps(func)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as exc:
                    if attempt >= max_retries:
                        logger.error(
                            "Функция %s исчерпала попытки: %s",
                            func.__name__,
                            exc,
                        )
                        raise
                    delay = exponential_backoff(attempt, base_delay)
                    logger.warning(
                        "Сбой %s (попытка %d/%d): %s. Повтор через %.1fс",
                        func.__name__,
                        attempt + 1,
                        max_retries,
                        exc,
                        delay,
                    )
                    time.sleep(delay)
            raise AssertionError("unreachable")  # pragma: no cover

        return wrapper

    return decorator


async def run_in_thread(
    func: Callable[..., T],
    *args: Any,
    **kwargs: Any,
) -> T:
    """Выполнить синхронную функцию в отдельном потоке.

    pybit — синхронная библиотека (блокирующий HTTP). Чтобы не блокировать
    главный цикл asyncio, оборачиваем каждый вызов API в поток.

    Args:
        func: синхронная функция для вызова.
        args, kwargs: аргументы функции.

    Returns:
        Результат функции.
    """
    return await asyncio.to_thread(func, *args, **kwargs)


async def async_retry(
    coro_factory: Callable[[], Awaitable[T]],
    max_retries: int = 3,
    base_delay: float = 1.0,
    exceptions: tuple[type[Exception], ...] = (Exception,),
) -> T:
    """Повторить асинхронную операцию при сбоях.

    Args:
        coro_factory: фабрика корутин — вызывается заново при каждой попытке
            (важно: нельзя передавать уже созданную корутину).
        max_retries: сколько раз повторить после первого отказа.
        base_delay: начальная пауза (экспоненциальный backoff).
        exceptions: какие исключения считаются временными.

    Returns:
        Результат операции.
    """
    for attempt in range(max_retries + 1):
        try:
            return await coro_factory()
        except exceptions as exc:
            if attempt >= max_retries:
                logger.error("Операция исчерпала попытки: %s", exc)
                raise
            delay = exponential_backoff(attempt, base_delay)
            logger.warning(
                "Сбой операции (попытка %d/%d): %s. Повтор через %.1fс",
                attempt + 1,
                max_retries,
                exc,
                delay,
            )
            await asyncio.sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover


def clamp(value: float, low: float, high: float) -> float:
    """Ограничить число диапазоном [low, high]."""
    return max(low, min(value, high))
