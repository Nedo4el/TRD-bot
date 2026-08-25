"""Индикаторы, написанные вручную (без сторонних TA-библиотек).

Все функции чистые: приняли списки чисел -> вернули список чисел.
Свечи везде идут от СТАРЫХ к НОВЫМ (как их отдаёт BybitClient).

Если данных меньше периода — возвращается пустой список [],
а стратегия в этом случае просто говорит "hold".
"""

from __future__ import annotations


def sma(values: list[float], period: int) -> list[float]:
    """Простая скользящая средняя (Simple Moving Average).

    Скользящее окно: на каждом шаге вычитаем уходящую цену
    и прибавляем новую — считается быстро даже на длинных рядах.

    Args:
        values: ряд цен от старых к новым.
        period: за сколько значений усреднять.

    Returns:
        Значения SMA; длина = len(values) - period + 1.
    """
    if period <= 0 or len(values) < period:
        return []
    result: list[float] = []
    window_sum = sum(values[:period])
    result.append(window_sum / period)
    for i in range(period, len(values)):
        window_sum += values[i] - values[i - period]
        result.append(window_sum / period)
    return result


def ema(values: list[float], period: int) -> list[float]:
    """Экспоненциальная скользящая средняя (Exponential MA).

    Первое значение — обычная SMA за period, дальше каждая новая точка:
    EMA = цена * k + предыдущая_EMA * (1 - k), где k = 2 / (period + 1).

    Args:
        values: ряд цен от старых к новым.
        period: период сглаживания.

    Returns:
        Значения EMA; длина = len(values) - period + 1.
    """
    if period <= 0 or len(values) < period:
        return []
    k = 2.0 / (period + 1)
    result: list[float] = [sum(values[:period]) / period]
    for price in values[period:]:
        result.append(price * k + result[-1] * (1 - k))
    return result


def rsi(closes: list[float], period: int = 14) -> list[float]:
    """Индекс относительной силы (RSI), сглаживание Уайлдера.

    RSI = 100 - 100 / (1 + RS), где RS = средний рост / средний падеж.
    0..100: ниже 30 — перепроданность, выше 70 — перекупленность.

    Args:
        closes: цены закрытия от старых к новым.
        period: период (классика — 14).

    Returns:
        Значения RSI; длина = len(closes) - period.
    """
    n = len(closes)
    if period <= 0 or n < period + 1:
        return []

    # Первый блок: средний рост и средний падеж за первые `period` свечей
    avg_gain = 0.0
    avg_loss = 0.0
    for i in range(1, period + 1):
        change = closes[i] - closes[i - 1]
        if change >= 0:
            avg_gain += change
        else:
            avg_loss -= change
    avg_gain /= period
    avg_loss /= period

    def _rsi(gain: float, loss: float) -> float:
        if loss == 0:
            return 100.0  # нет ни одного падения — максимум
        if gain == 0:
            return 0.0  # нет ни одного роста — минимум
        rs = gain / loss
        return 100.0 - 100.0 / (1.0 + rs)

    result: list[float] = [_rsi(avg_gain, avg_loss)]

    # Дальше — сглаживание Уайлдера: учитываем каждую новую свечу
    for i in range(period + 1, n):
        change = closes[i] - closes[i - 1]
        gain = change if change > 0 else 0.0
        loss = -change if change < 0 else 0.0
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        result.append(_rsi(avg_gain, avg_loss))
    return result


def bollinger(
    closes: list[float],
    period: int = 20,
    num_dev: float = 2.0,
) -> tuple[list[float], list[float], list[float]]:
    """Полосы Боллинджера вокруг SMA.

    Средняя линия = SMA(period).
    Верхняя/нижняя = SMA ± num_dev стандартных отклонений окна.

    Args:
        closes: цены закрытия от старых к новым.
        period: период средней линии (классика — 20).
        num_dev: ширина в сигмах (классика — 2).

    Returns:
        Кортеж (upper, middle, lower); длины = len(closes) - period + 1.
    """
    middle = sma(closes, period)
    upper: list[float] = []
    lower: list[float] = []
    for i in range(len(middle)):
        window = closes[i : i + period]
        mean = middle[i]
        # Стандартное отклонение по окну (среднеквадратичное)
        variance = sum((x - mean) ** 2 for x in window) / period
        sd = variance**0.5
        upper.append(mean + num_dev * sd)
        lower.append(mean - num_dev * sd)
    return upper, middle, lower


def atr(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    period: int = 14,
) -> list[float]:
    """Средний истинный диапазон (ATR), сглаживание Уайлдера.

    Истинный диапазон свечи i:
        max(high - low, |high - close_prev|, |low - close_prev|)
    ATR — «средняя размах волатильности» в тех же единицах, что цена.

    Args:
        highs: максимумы свечей.
        lows: минимумы свечей.
        closes: цены закрытия.
        period: период (классика — 14).

    Returns:
        Значения ATR; длина = len(closes) - period.
    """
    n = len(closes)
    if period <= 0 or n < period + 1:
        return []

    # Истинные диапазоны для всех свечей (первая свеча — просто high-low)
    trs: list[float] = [highs[0] - lows[0]]
    for i in range(1, n):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)

    value = sum(trs[:period]) / period
    result: list[float] = [value]
    for i in range(period, n):
        value = (value * (period - 1) + trs[i]) / period
        result.append(value)
    return result


def adx(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    period: int = 14,
) -> list[float]:
    """Average Directional Index — сила тренда (0..100).

    ADX = 100 × сглажённый DX, где DX = |+DI - -DI| / (+DI + -DI).
    +DI / -DI считаются через Directional Movement (DM+ / DM-).

    Args:
        highs: максимумы свечей.
        lows: минимумы свечей.
        closes: цены закрытия.
        period: период (классика — 14).

    Returns:
        Значения ADX; длина = len(closes) - 2 * period.
    """
    n = len(closes)
    if period <= 0 or n < 2 * period + 1:
        return []

    # True Range + Directional Movement для каждой свечи
    trs: list[float] = []
    dm_plus: list[float] = []
    dm_minus: list[float] = []

    trs.append(highs[0] - lows[0])
    dm_plus.append(0.0)
    dm_minus.append(0.0)

    for i in range(1, n):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)

        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        if up > down and up > 0:
            dm_plus.append(up)
        else:
            dm_plus.append(0.0)
        if down > up and down > 0:
            dm_minus.append(down)
        else:
            dm_minus.append(0.0)

    # Первые ATR, DM+, DM- (средние за period)
    atr_val = sum(trs[1 : period + 1]) / period
    dm_p = sum(dm_plus[1 : period + 1]) / period
    dm_m = sum(dm_minus[1 : period + 1]) / period

    def _di(p: float, m: float) -> tuple[float, float]:
        if atr_val == 0:
            return 0.0, 0.0
        return (p / atr_val) * 100, (m / atr_val) * 100

    dip, dim = _di(dm_p, dm_m)
    dx = abs(dip - dim) / (dip + dim) * 100 if (dip + dim) > 0 else 0.0
    result: list[float] = [dx]

    # Сглаживание Уайлдера для ATR, DM+, DM- и ADX
    for i in range(period + 1, n):
        atr_val = (atr_val * (period - 1) + trs[i]) / period
        dm_p = (dm_p * (period - 1) + dm_plus[i]) / period
        dm_m = (dm_m * (period - 1) + dm_minus[i]) / period

        dip, dim = _di(dm_p, dm_m)
        dx = abs(dip - dim) / (dip + dim) * 100 if (dip + dim) > 0 else 0.0

        # ADX = сглажённый DX
        result.append((result[-1] * (period - 1) + dx) / period)

    return result


def crossed_up(prev_a: float, prev_b: float, now_a: float, now_b: float) -> bool:
    """Пересекла ли линия A линию B снизу вверх на последней свече."""
    return prev_a <= prev_b and now_a > now_b


def crossed_down(prev_a: float, prev_b: float, now_a: float, now_b: float) -> bool:
    """Пересекла ли линия A линию B сверху вниз на последней свече."""
    return prev_a >= prev_b and now_a < now_b
