"""Торговая стратегия: пересечение скользящих средних (SMA crossover).

Логика классическая:
- если быстрая SMA пересекает медленную снизу вверх -> сигнал на покупку (long);
- если быстрая SMA пересекает медленную сверху вниз -> сигнал на продажу (short).

Функции чистые (без побочных эффектов) — их легко тестировать.
"""

from __future__ import annotations

from dataclasses import dataclass

from bybit_client import Candle


@dataclass
class Signal:
    """Сигнал стратегии на текущей свече."""

    action: str  # "buy" | "sell" | "hold"
    reason: str  # человекочитаемое пояснение для лога


def sma(values: list[float], period: int) -> list[float]:
    """Рассчитать простую скользящую среднюю по всему ряду.

    Args:
        values: ряд цен (закрытий свечей) от старых к новым.
        period: период усреднения.

    Returns:
        Список значений SMA длиной len(values) - period + 1.
        Первые period-1 значений (недостаточно данных) отсутствуют.
    """
    if period <= 0 or len(values) < period:
        return []
    result: list[float] = []
    window_sum = sum(values[:period])
    result.append(window_sum / period)
    for i in range(period, len(values)):
        # Скользящее окно: вычитаем уходящий элемент, добавляем новый
        window_sum += values[i] - values[i - period]
        result.append(window_sum / period)
    return result


def last_cross(
    candles: list[Candle], fast_period: int, slow_period: int
) -> tuple[float, float, str]:
    """Определить пересечение SMA на последней закрытой свече.

    Args:
        candles: свечи от старых к новым (должно быть достаточно для slow).
        fast_period: период быстрой SMA.
        slow_period: период медленной SMA.

    Returns:
        Кортеж (fast_now, slow_now, cross_type):
        fast_now, slow_now — текущие значения средних;
        cross_type — "golden" (быстрая пересекла вверх), "death" (вниз),
        или "none" (пересечения нет).
    """
    closes = [c.close for c in candles]
    fast = sma(closes, fast_period)
    slow = sma(closes, slow_period)

    # Выравниваем ряды по длине (сравниваем последние два значения обоих рядов)
    if len(fast) < 2 or len(slow) < 2:
        # Недостаточно данных даже для одного пересечения
        return (fast[-1] if fast else 0.0, slow[-1] if slow else 0.0, "none")

    # Была ли быстрая ниже медленной на прошлой свече и выше сейчас?
    prev_fast, prev_slow = fast[-2], slow[-2]
    now_fast, now_slow = fast[-1], slow[-1]

    if prev_fast <= prev_slow and now_fast > now_slow:
        return (now_fast, now_slow, "golden")  # пересечение вверх -> buy
    if prev_fast >= prev_slow and now_fast < now_slow:
        return (now_fast, now_slow, "death")  # пересечение вниз -> sell
    return (now_fast, now_slow, "none")


def generate_signal(
    candles: list[Candle], fast_period: int, slow_period: int
) -> Signal:
    """Сгенерировать торговый сигнал по свечам.

    Args:
        candles: свечи от старых к новым.
        fast_period: период быстрой SMA.
        slow_period: период медленной SMA.

    Returns:
        Сигнал: buy / sell / hold.
    """
    if len(candles) < slow_period + 2:
        return Signal("hold", "недостаточно свечей для расчёта индикаторов")

    fast_now, slow_now, cross = last_cross(candles, fast_period, slow_period)

    if cross == "golden":
        return Signal(
            "buy",
            f"золотое пересечение: SMA{fast_period}={fast_now:.2f} > "
            f"SMA{slow_period}={slow_now:.2f}",
        )
    if cross == "death":
        return Signal(
            "sell",
            f"мёртвое пересечение: SMA{fast_period}={fast_now:.2f} < "
            f"SMA{slow_period}={slow_now:.2f}",
        )
    return Signal(
        "hold",
        f"SMA{fast_period}={fast_now:.2f}, SMA{slow_period}={slow_now:.2f}",
    )
