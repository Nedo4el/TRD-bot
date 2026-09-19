from __future__ import annotations

from dataclasses import dataclass, field

from core.bybit_client import Candle
from core.strategies import BaseStrategy, Signal


@dataclass
class FlatConfig:
    """Параметры боковика после импульса."""

    poc_lookback: int = 600
    range_pct: float = 20.0
    impulse_min_pct: float = 15.0
    impulse_window: int = 5
    impulse_cooldown: int = 30

    # Сетка ордеров (от POC, %)
    order_levels: list[float] = field(default_factory=lambda: [-6.0, -8.0, -10.0, 6.0, 8.0, 10.0])
    # Стоп зона (±% от POC, запрет ордеров)
    stop_zone_pct: float = 5.0
    # Стоп от границы коридора (%)
    stop_from_border_pct: float = 3.0
    # Тейк: на 1% ближе к POC от противоположного ордера
    tp_offset_pct: float = 1.0
    # Максимум позиций в одну сторону
    max_positions: int = 3
    # Пересчёт POC каждые N свечей (0 = не пересчитывать)
    poc_recalc_every: int = 72  # 6 часов при M5
    # Частичное закрытие на POC (%)
    partial_close_pct: float = 50.0
    # Трейлинг TP после POC (%)
    trailing_after_poc_pct: float = 2.0
    # Размер ордера по уровням: -6/+6 → $20, -8/+8 → $15, -10/+10 → $15 (всего $100)
    order_sizes: dict[float, float] = field(default_factory=lambda: {
        -6.0: 20.0, -8.0: 15.0, -10.0: 15.0,
        6.0: 20.0, 8.0: 15.0, 10.0: 15.0,
    })


@dataclass
class PositionState:
    """Состояние открытой позиции."""

    direction: str  # "long" или "short"
    entry_price: float
    current_peak: float  # пик в сторону прибыли
    trailing_stop: float  # текущий трейлинг стоп


class FlatStrategy(BaseStrategy):
    """Боковик после импульса: POC-based коридор + сетка ордеров + трейлинг."""

    name = "flat"

    def __init__(self, cfg: FlatConfig | None = None):
        self.cfg = cfg or FlatConfig()
        self._positions: list[PositionState] = []
        self._fixed_poc: float | None = None

    def check_signal(self, candles: list[Candle]) -> Signal:
        if len(candles) < self.cfg.poc_lookback:
            return Signal(action="hold", reason=f"мало свечей ({len(candles)}/{self.cfg.poc_lookback})")

        # Фиксируем POC при первом вызове — больше не пересчитываем
        if self._fixed_poc is None:
            window = candles[-self.cfg.poc_lookback:]
            self._fixed_poc = self._calc_volume_poc(
                [c.high for c in window],
                [c.low for c in window],
                [c.volume for c in window],
            )

        poc = self._fixed_poc

        if poc <= 0:
            return Signal(action="hold", reason="POC = 0")

        closes = [c.close for c in candles]
        highs = [c.high for c in candles]
        lows = [c.low for c in candles]
        volumes = [c.volume for c in candles]

        current_price = closes[-1]
        high = highs[-1]
        low = lows[-1]
        deviation = (current_price - poc) / poc * 100
        half_range = self.cfg.range_pct / 2

        # Проверяем трейлинг стоп для открытых позиций
        trailing_signal = self._check_trailing_stop(
            current_price, high, low, poc, half_range
        )
        if trailing_signal:
            return trailing_signal

        # ТРЕНД: цена за пределами коридора
        if abs(deviation) > half_range:
            direction = "ВВЕРХ" if deviation > 0 else "ВНИЗ"
            return Signal(
                action="hold",
                reason=(
                    f"ТРЕНД {direction} | POC={poc:.4f} | цена={current_price:.4f} | "
                    f"откл={deviation:+.1f}% > ±{half_range:.0f}%"
                ),
            )

        # Ищем лучший ордер в стакане
        best_order = self._find_best_order(
            current_price, poc, deviation, half_range
        )

        if best_order:
            return best_order

        # СТОП ЗОНА: цена рядом с POC — ждём движения
        if abs(deviation) < self.cfg.stop_zone_pct:
            return Signal(
                action="hold",
                reason=(
                    f"СТОП ЗОНА | POC={poc:.4f} | цена={current_price:.4f} | "
                    f"откл={deviation:+.1f}% < ±{self.cfg.stop_zone_pct:.0f}% | "
                    f"сетка: buy@-6/-8/-10% sell@+6/+8/+10%"
                ),
            )

        # Нет подходящих ордеров — ждём
        return Signal(
            action="hold",
            reason=(
                f"БОКОВИК | POC={poc:.4f} | цена={current_price:.4f} | "
                f"откл={deviation:+.1f}% (диапазон ±{half_range:.0f}%) | "
                f"стоп зона ±{self.cfg.stop_zone_pct:.0f}%"
            ),
        )

    def _check_trailing_stop(
        self,
        current_price: float,
        high: float,
        low: float,
        poc: float,
        half_range: float,
    ) -> Signal | None:
        """Проверить трейлинг стоп для открытых позиций."""
        if not self._positions:
            return None

        for pos in self._positions[:]:
            if pos.direction == "long":
                # Для LONG: пик растёт вверх, стоп поднимается
                if high > pos.current_peak:
                    pos.current_peak = high
                    pos.trailing_stop = high * (1 - self.cfg.trailing_after_poc_pct / 100)

                # Проверяем удар стопа
                if low <= pos.trailing_stop:
                    self._positions.remove(pos)
                    return Signal(
                        action="close_long",
                        reason=(
                            f"ТРЕЙЛИНГ LONG | стоп={pos.trailing_stop:.4f} | "
                            f"цена={current_price:.4f} | пик={pos.current_peak:.4f}"
                        ),
                        stop_loss=pos.trailing_stop,
                    )

                # Частичное закрытие на POC
                deviation = (current_price - poc) / poc * 100
                if abs(deviation) < 1.0 and len(self._positions) > 0:
                    return Signal(
                        action="close_long_50",
                        reason=(
                            f"ЧАСТИЧНОЕ ЗАКРЫТИЕ LONG 50% | POC={poc:.4f} | "
                            f"цена={current_price:.4f}"
                        ),
                    )

            elif pos.direction == "short":
                # Для SHORT: пик растёт вниз, стоп опускается
                if low < pos.current_peak:
                    pos.current_peak = low
                    pos.trailing_stop = low * (1 + self.cfg.trailing_after_poc_pct / 100)

                # Проверяем удар стопа
                if high >= pos.trailing_stop:
                    self._positions.remove(pos)
                    return Signal(
                        action="close_short",
                        reason=(
                            f"ТРЕЙЛИНГ SHORT | стоп={pos.trailing_stop:.4f} | "
                            f"цена={current_price:.4f} | пик={pos.current_peak:.4f}"
                        ),
                        stop_loss=pos.trailing_stop,
                    )

                # Частичное закрытие на POC
                deviation = (current_price - poc) / poc * 100
                if abs(deviation) < 1.0 and len(self._positions) > 0:
                    return Signal(
                        action="close_short_50",
                        reason=(
                            f"ЧАСТИЧНОЕ ЗАКРЫТИЕ SHORT 50% | POC={poc:.4f} | "
                            f"цена={current_price:.4f}"
                        ),
                    )

        return None

    def _find_best_order(
        self,
        current_price: float,
        poc: float,
        deviation: float,
        half_range: float,
    ) -> Signal | None:
        """Найти лучший ордер в стакане с учётом приоритета TP."""

        buy_boundary = poc * (1 - half_range / 100)
        sell_boundary = poc * (1 + half_range / 100)

        # Проверяем LONG ордера (цена ниже POC)
        if deviation < 0:
            for level in sorted(self.cfg.order_levels, reverse=True):
                if level > 0:
                    continue
                order_price = poc * (1 + level / 100)

                if current_price <= order_price:
                    # TP = противоположный ордер на 1% ближе к POC
                    tp_level = abs(level) - self.cfg.tp_offset_pct
                    tp_price = poc * (1 + tp_level / 100)

                    # SL от границы коридора 3%
                    sl_price = buy_boundary * (1 - self.cfg.stop_zone_pct / 100)

                    # Добавляем позицию в состояние
                    self._positions.append(
                        PositionState(
                            direction="long",
                            entry_price=current_price,
                            current_peak=current_price,
                            trailing_stop=sl_price,
                        )
                    )

                    return Signal(
                        action="buy",
                        reason=(
                            f"LONG @{level:+.0f}% | POC={poc:.4f} | "
                            f"цена={current_price:.4f} | "
                            f"TP={tp_price:.4f} | SL={sl_price:.4f}"
                        ),
                        stop_loss=sl_price,
                        take_profit=tp_price,
                    )

        # Проверяем SHORT ордера (цена выше POC)
        if deviation > 0:
            for level in sorted(self.cfg.order_levels):
                if level < 0:
                    continue
                order_price = poc * (1 + level / 100)

                if current_price >= order_price:
                    # TP = противоположный ордер на 1% ближе к POC
                    tp_level = level - self.cfg.tp_offset_pct
                    tp_price = poc * (1 - tp_level / 100)

                    # SL от границы коридора 3%
                    sl_price = sell_boundary * (1 + self.cfg.stop_zone_pct / 100)

                    # Добавляем позицию в состояние
                    self._positions.append(
                        PositionState(
                            direction="short",
                            entry_price=current_price,
                            current_peak=current_price,
                            trailing_stop=sl_price,
                        )
                    )

                    return Signal(
                        action="sell",
                        reason=(
                            f"SHORT @{level:+.0f}% | POC={poc:.4f} | "
                            f"цена={current_price:.4f} | "
                            f"TP={tp_price:.4f} | SL={sl_price:.4f}"
                        ),
                        stop_loss=sl_price,
                        take_profit=tp_price,
                    )

        return None

    def _detect_impulse(
        self,
        closes: list[float],
        highs: list[float],
        lows: list[float],
    ) -> tuple[int, float, float] | None:
        """Найти импульс: ≥ impulse_min_pct за impulse_window свечей.

        Ищет импульс, который завершился НЕ позже impulse_cooldown свечей назад,
        чтобы была стабилизация после него.

        Returns:
            Кортеж (impulse_end_idx, high, low) или None.
        """
        window = self.cfg.impulse_window
        cooldown = self.cfg.impulse_cooldown
        search_end = len(closes) - cooldown
        if search_end < window + 1:
            return None

        for end_idx in range(search_end - 1, window - 1, -1):
            start_idx = end_idx - window
            segment_highs = highs[start_idx : end_idx + 1]
            segment_lows = lows[start_idx : end_idx + 1]

            high = max(segment_highs)
            low = min(segment_lows)

            if low <= 0:
                continue

            move_pct = (high - low) / low * 100

            if move_pct >= self.cfg.impulse_min_pct:
                return (end_idx, high, low)

        return None

    @staticmethod
    def _calc_volume_poc(
        highs: list[float], lows: list[float], volumes: list[float],
        n_buckets: int = 100,
    ) -> float:
        """Volume Profile POC — объём распределяется по всем бинам, которые пересекает свеча.

        Каждая свеча распределяет свой объём пропорционально по бинам
        от low до high (volume / кол-во бинов, которых касается свеча).
        """
        if not highs or not lows or not volumes:
            return 0.0

        global_low = min(lows)
        global_high = max(highs)
        if global_low == global_high:
            return global_low

        bucket_size = (global_high - global_low) / n_buckets
        profile = [0.0] * n_buckets

        for high, low, vol in zip(highs, lows, volumes):
            if low >= high or vol <= 0:
                continue
            # Определяем какие бины пересекает свеча
            start_bin = max(0, int((low - global_low) / bucket_size))
            end_bin = min(n_buckets - 1, int((high - global_low) / bucket_size))
            n_bins = end_bin - start_bin + 1
            vol_per_bin = vol / n_bins
            for b in range(start_bin, end_bin + 1):
                profile[b] += vol_per_bin

        best_idx = profile.index(max(profile))
        return global_low + (best_idx + 0.5) * bucket_size

    @staticmethod
    def _calc_poc(closes: list[float], volumes: list[float]) -> float:
        """Point of Control — цена с максимальным объёмом (legacy, по close)."""
        if not closes or not volumes:
            return 0.0

        mn, mx = min(closes), max(closes)
        if mn == mx:
            return mn

        n_buckets = 50
        bucket_size = (mx - mn) / n_buckets
        buckets_vol = [0.0] * n_buckets

        for price, vol in zip(closes, volumes):
            idx = min(int((price - mn) / bucket_size), n_buckets - 1)
            buckets_vol[idx] += vol

        best_idx = buckets_vol.index(max(buckets_vol))
        return mn + (best_idx + 0.5) * bucket_size
