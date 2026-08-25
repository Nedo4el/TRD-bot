"""Стратегия «Свинг-уровни»: пробой уровня и ретест.

Торговля не по индикаторам, а по уровням поддержки/сопротивления.

Как находятся уровни (свечи-«свинги»):
- Сопротивление: свеча с High выше, чем у 5 свечей слева и 5 справа.
- Поддержка:     свеча с Low ниже, чем у 5 свечей слева и 5 справа.
Сканируются последние 50 свечей; последние 5 уровней хранятся в списках
и перерисовываются на каждом шаге.

Вход в LONG (пробой вверх + ретест сверху):
1. Цена закрытием пробивает уровень сопротивления ВВЕРХ.
2. Потом цена ВОЗВРАЩАЕТСЯ к уровню (low касается уровня).
3. Свеча ретеста бычья (Close > Open) и закрылась над уровнем -> отскок.

Вход в SHORT — зеркально (пробой поддержки вниз, ретест снизу,
медвежья свеча Close < Open).

Если после пробоя ретест не случился за RETEST_MAX_BARS свечей —
сетка отменяется (уровень устарел).

Выход: SL = 0.2% и TP = 0.4% от входа (задаются в .env бота).
"""

from __future__ import annotations

from core.bybit_client import Candle
from core.strategies import BaseStrategy, Signal


def find_swings(
    candles: list[Candle],
    wing_size: int,
) -> tuple[list[tuple[int, float]], list[tuple[int, float]]]:
    """Найти локальные максимумы и минимумы (свинг-свечи).

    Args:
        candles: окно свечей от старых к новым.
        wing_size: сколько соседей слева/справа должны быть ниже/выше.

    Returns:
        Кортеж (resistances, supports); каждый список — пары
        (индекс_свечи, цена уровня), упорядочены от старых к новым.
    """
    n = len(candles)
    resistances: list[tuple[int, float]] = []
    supports: list[tuple[int, float]] = []

    # Перебираем только свечи, у которых есть wing соседей с обеих сторон
    for i in range(wing_size, n - wing_size):
        high = candles[i].high
        low = candles[i].low
        left = candles[i - wing_size : i]
        right = candles[i + 1 : i + wing_size + 1]

        # Локальный максимум: High выше всех соседей слева и справа
        if all(high > c.high for c in left) and all(high > c.high for c in right):
            resistances.append((i, high))
        # Локальный минимум: Low ниже всех соседей слева и справа
        if all(low < c.low for c in left) and all(low < c.low for c in right):
            supports.append((i, low))
    return resistances, supports


class SwingLevelsStrategy(BaseStrategy):
    """Пробой свинг-уровня + возврат (ретест) + свечной фильтр."""

    name = "swings"

    def __init__(
        self,
        lookback: int = 50,
        wing_size: int = 5,
        max_levels: int = 5,
        retest_max_bars: int = 20,
    ) -> None:
        """Все параметры настраиваются в .env бота (см. main.py)."""
        self.lookback = lookback
        self.wing_size = wing_size
        self.max_levels = max_levels
        self.retest_max_bars = retest_max_bars
        # --- «Глобальные» списки последних найденных уровней ---
        self.resistance_levels: list[float] = []
        self.support_levels: list[float] = []
        # Ожидающие ретеста сетапы: уровень + время свечи пробоя
        self._long_setup: dict | None = None
        self._short_setup: dict | None = None

    # ------------------------------------------------------------------
    def _update_levels(self, candles: list[Candle]) -> None:
        """Пересканировать историю и перерисовать списки уровней."""
        resistances, supports = find_swings(candles, self.wing_size)
        # Берём самые свежие уровни (списки уже упорядочены по времени)
        self.resistance_levels = [price for _, price in resistances[-self.max_levels :]]
        self.support_levels = [price for _, price in supports[-self.max_levels :]]

    def _detect_breakouts(self, prev: Candle, last: Candle) -> None:
        """Заметить свежий пробой уровня и запомнить сетап на ретест."""
        # Пробой сопротивления вверх: вчера закрылись под уровнем,
        # сегодня закрылись над ним
        for level in self.resistance_levels:
            if prev.close <= level < last.close:
                self._long_setup = {
                    "level": level,
                    "breakout_time": last.open_time,
                }
                break
        # Пробой поддержки вниз
        for level in self.support_levels:
            if prev.close >= level > last.close:
                self._short_setup = {
                    "level": level,
                    "breakout_time": last.open_time,
                }
                break

    def _bars_since(self, candles: list[Candle], since_time: int) -> int | None:
        """Сколько закрытых свечей прошло с момента пробоя."""
        for passed, candle in enumerate(reversed(candles[:-1])):
            # candles[:-1] — без последней (это кандидат на свечу ретеста)
            if candle.open_time == since_time:
                return passed
        return None  # свеча пробоя уже вне окна — сетап устарел

    def _check_long_retest(self, candles: list[Candle], last: Candle) -> Signal | None:
        """Проверить ретест сопротивления снизу... точнее сверху вниз."""
        assert self._long_setup is not None
        level = self._long_setup["level"]

        # Ретест ищем только ПОСЛЕ свечи пробоя (не на ней самой)
        if self._long_setup["breakout_time"] == last.open_time:
            return None

        bars_passed = self._bars_since(candles, self._long_setup["breakout_time"])
        if bars_passed is None or bars_passed > self.retest_max_bars:
            self._long_setup = None  # просрочили — отменяем сетап
            return None

        touched_level = last.low <= level  # цена вернулась к уровню
        bounced_up = last.close > level  # и не провалилась под него
        bullish_candle = last.close > last.open  # свеча бычья

        if touched_level and bounced_up and bullish_candle:
            self._long_setup = None
            return Signal(
                "buy",
                f"ретест сопротивления {level:.8g} сверху: "
                f"касание + бычье закрытие над уровнем",
            )
        return None

    def _check_short_retest(self, candles: list[Candle], last: Candle) -> Signal | None:
        """Проверить ретест поддержки снизу вверх."""
        assert self._short_setup is not None
        level = self._short_setup["level"]

        if self._short_setup["breakout_time"] == last.open_time:
            return None

        bars_passed = self._bars_since(candles, self._short_setup["breakout_time"])
        if bars_passed is None or bars_passed > self.retest_max_bars:
            self._short_setup = None
            return None

        touched_level = last.high >= level  # цена вернулась к уровню
        bounced_down = last.close < level  # и не пробилась над ним
        bearish_candle = last.close < last.open  # свеча медвежья

        if touched_level and bounced_down and bearish_candle:
            self._short_setup = None
            return Signal(
                "sell",
                f"ретест поддержки {level:.8g} снизу: "
                f"касание + медвежье закрытие под уровнем",
            )
        return None

    # ------------------------------------------------------------------
    def check_signal(self, candles: list[Candle]) -> Signal:
        """Полный цикл: уровни -> пробой -> ожидание ретеста -> вход."""
        need = self.lookback + self.wing_size + 3
        if len(candles) < need:
            return Signal("hold", "недостаточно свечей для поиска свингов")

        window = candles[-self.lookback :]
        prev, last = candles[-2], candles[-1]

        # Шаг 1. Обновляем карту уровней по последним 50 свечам
        self._update_levels(window)

        # Шаг 2. Ищем свежие пробои (регистрируют сетап на следующий свечи)
        self._detect_breakouts(prev, last)

        # Шаг 3. Проверяем ретесты ранее зарегистрированных сетапов
        if self._long_setup is not None:
            signal = self._check_long_retest(candles, last)
            if signal is not None:
                return signal
        if self._short_setup is not None:
            signal = self._check_short_retest(candles, last)
            if signal is not None:
                return signal

        waiting = []
        if self._long_setup is not None:
            waiting.append(f"ждём ретест сопротивления {self._long_setup['level']:.8g}")
        if self._short_setup is not None:
            waiting.append(f"ждём ретест поддержки {self._short_setup['level']:.8g}")
        reason = (
            "; ".join(waiting)
            if waiting
            else (
                f"уровней в памяти: R={len(self.resistance_levels)}, "
                f"S={len(self.support_levels)}; активных сетапов нет"
            )
        )
        return Signal("hold", reason)
