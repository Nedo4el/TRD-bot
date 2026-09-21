"""TEST стратегия — Grid Flat (сетка в боковике по фильтрам).

Философия:
- Много маленьких сделок в подтверждённом флете.
- В трендах не торгуем — фильтры ER, CI, ADX, Volume.
- Геометрическая сетка 12 уровней, TP = соседний уровень.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

from core.bybit_client import Candle
from core.strategies import BaseStrategy, Signal


# ─── Индикаторы ──────────────────────────────────────────────


def _ema(data: list[float], period: int) -> list[float]:
    """EMA (возвращает список той же длины, NaN в начале)."""
    result = [0.0] * len(data)
    if len(data) < period:
        return result
    k = 2 / (period + 1)
    sma = sum(data[:period]) / period
    for i in range(period):
        result[i] = sma
    ema_val = sma
    for i in range(period, len(data)):
        ema_val = data[i] * k + ema_val * (1 - k)
        result[i] = ema_val
    return result


def _atr(highs: list[float], lows: list[float], closes: list[float], period: int) -> list[float]:
    """ATR (Average True Range)."""
    if len(closes) < 2:
        return [0.0] * len(closes)
    tr_list = [highs[0] - lows[0]]
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        tr_list.append(tr)
    # Wilder's smoothing
    result = [0.0] * len(closes)
    if len(tr_list) < period:
        return result
    atr_val = sum(tr_list[:period]) / period
    result[period - 1] = atr_val
    for i in range(period, len(tr_list)):
        atr_val = (atr_val * (period - 1) + tr_list[i]) / period
        result[i] = atr_val
    return result


def _adx(highs: list[float], lows: list[float], closes: list[float], period: int) -> list[float]:
    """ADX (Average Directional Index)."""
    n = len(closes)
    if n < period + 1:
        return [0.0] * n

    # +DM / -DM
    plus_dm = [0.0] * n
    minus_dm = [0.0] * n
    for i in range(1, n):
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        plus_dm[i] = up if (up > down and up > 0) else 0.0
        minus_dm[i] = down if (down > up and down > 0) else 0.0

    atr_vals = _atr(highs, lows, closes, period)

    # Smoothed +DM / -DM
    smoothed_plus = [0.0] * n
    smoothed_minus = [0.0] * n
    if n >= period:
        smoothed_plus[period - 1] = sum(plus_dm[1:period + 1]) if n > period else sum(plus_dm[:period])
        smoothed_minus[period - 1] = sum(minus_dm[1:period + 1]) if n > period else sum(minus_dm[:period])

    for i in range(period, n):
        smoothed_plus[i] = smoothed_plus[i - 1] - smoothed_plus[i - 1] / period + plus_dm[i]
        smoothed_minus[i] = smoothed_minus[i - 1] - smoothed_minus[i - 1] / period + minus_dm[i]

    # +DI / -DI
    plus_di = [0.0] * n
    minus_di = [0.0] * n
    for i in range(period - 1, n):
        if atr_vals[i] > 0:
            plus_di[i] = 100 * smoothed_plus[i] / atr_vals[i]
            minus_di[i] = 100 * smoothed_minus[i] / atr_vals[i]

    # DX
    dx = [0.0] * n
    for i in range(period - 1, n):
        di_sum = plus_di[i] + minus_di[i]
        if di_sum > 0:
            dx[i] = 100 * abs(plus_di[i] - minus_di[i]) / di_sum

    # ADX (Wilder's)
    result = [0.0] * n
    adx_start = 2 * period - 1
    if adx_start < n:
        adx_val = sum(dx[period - 1:adx_start]) / period
        result[adx_start] = adx_val
        for i in range(adx_start + 1, n):
            adx_val = (adx_val * (period - 1) + dx[i]) / period
            result[i] = adx_val

    return result


def _efficiency_ratio(closes: list[float], period: int) -> list[float]:
    """Efficiency Ratio (Kaufman ER)."""
    n = len(closes)
    result = [0.0] * n
    for i in range(period, n):
        direction = abs(closes[i] - closes[i - period])
        volatility = sum(abs(closes[j] - closes[j - 1]) for j in range(i - period + 1, i + 1))
        result[i] = direction / volatility if volatility > 0 else 0.0
    return result


def _choppiness_index(highs: list[float], lows: list[float], closes: list[float], period: int) -> list[float]:
    """Choppiness Index (CI)."""
    n = len(closes)
    result = [0.0] * n
    if n < period:
        return result
    atr_vals = _atr(highs, lows, closes, period)
    for i in range(period - 1, n):
        atr_sum = sum(atr_vals[i - period + 1:i + 1])
        highest = max(highs[i - period + 1:i + 1])
        lowest = min(lows[i - period + 1:i + 1])
        if highest - lowest > 0:
            result[i] = 100 * math.log10(atr_sum / (highest - lowest)) / math.log10(period)
    return result


# ─── Конфиг ──────────────────────────────────────────────────


@dataclass
class TestConfig:
    """Параметры стратегии Grid Flat."""

    # --- POC ---
    poc_lookback: int = 300  # 25 часов M5

    # --- Коридор ---
    corridor_pct: float = 7.5  # ±7.5% от POC
    stop_zone_pct: float = 3.0  # ±3% запрет ордеров

    # --- Сетка ---
    grid_levels: int = 12  # 6 в каждую сторону

    # --- Фильтры (M5) ---
    er_period: int = 10
    er_threshold: float = 0.30
    ci_period: int = 14
    ci_threshold: float = 60.0
    adx_period: int = 14
    adx_threshold: float = 20.0
    vol_avg_fast: int = 20
    vol_avg_slow: int = 100
    vol_ratio_threshold: float = 0.60

    # --- Фильтр H1 ---
    adx_h1_period: int = 14
    adx_h1_threshold: float = 25.0

    # --- Выходы ---
    time_exit_candles: int = 3  # свечи за границей коридора
    atr_period: int = 14
    atr_multiplier: float = 2.0  # ATR × 2 от средней
    atr_avg_period: int = 60
    timeout_hours: int = 24

    # --- Пауза ---
    cooldown_minutes: int = 60

    # --- Размер ордера ---
    order_size_pct: float = 100 / 13  # капитал / 13


# ─── Состояния позиции ───────────────────────────────────────


@dataclass
class GridPosition:
    """Позиция в сетке."""
    direction: str  # "long" / "short"
    entry_price: float
    level_idx: int  # индекс уровня в сетке (0 = дальний, 5 = ближний к POC)
    entry_time: float  # time.time()
    candle_count: int = 0  # свечей с момента входа


# ─── Стратегия ───────────────────────────────────────────────


class TestStrategy(BaseStrategy):
    """Grid Flat — сетка в боковике с фильтрами тренда."""

    name = "grid_flat"
    _max_lookback = 500
    _min_warmup = 350

    def __init__(self, cfg: TestConfig | None = None):
        self.cfg = cfg or TestConfig()
        self._fixed_poc: float | None = None
        self._grid: list[float] = []
        self._positions: list[GridPosition] = []
        self._active: bool = False
        self._last_sl_time: float = 0.0
        self._candles_since_exit: int = 0
        self._atr_avg: float = 0.0
        # Cache
        self._cache_n: int = 0
        self._cache_er: float = 0.0
        self._cache_ci: float = 0.0
        self._cache_adx: float = 0.0
        self._cache_vol: float = 0.0
        self._cache_atr: float = 0.0

    def check_signal(self, candles: list[Candle]) -> Signal:
        n = len(candles)
        max_needed = max(self.cfg.poc_lookback, self.cfg.vol_avg_slow, self.cfg.atr_avg_period + self.cfg.atr_period) + 50

        if n < max_needed:
            return Signal(action="hold", reason=f"мало свечей ({n}/{max_needed})")

        closes = [c.close for c in candles]
        highs = [c.high for c in candles]
        lows = [c.low for c in candles]
        volumes = [c.volume for c in candles]

        current_price = closes[-1]

        # === Фиксируем POC ===
        if self._fixed_poc is None:
            self._fixed_poc = self._calc_volume_poc(
                highs[-self.cfg.poc_lookback:],
                lows[-self.cfg.poc_lookback:],
                volumes[-self.cfg.poc_lookback:],
            )

        poc = self._fixed_poc
        if poc <= 0:
            return Signal(action="hold", reason="POC = 0")

        # === Индикаторы ===
        er_vals = _efficiency_ratio(closes, self.cfg.er_period)
        ci_vals = _choppiness_index(highs, lows, closes, self.cfg.ci_period)
        adx_vals = _adx(highs, lows, closes, self.cfg.adx_period)
        atr_vals = _atr(highs, lows, closes, self.cfg.atr_period)

        er_now = er_vals[-1]
        ci_now = ci_vals[-1]
        adx_now = adx_vals[-1]
        atr_now = atr_vals[-1]

        if n >= self.cfg.atr_avg_period + self.cfg.atr_period:
            self._atr_avg = sum(atr_vals[-self.cfg.atr_avg_period:]) / self.cfg.atr_avg_period

        vol_fast = sum(volumes[-self.cfg.vol_avg_fast:]) / self.cfg.vol_avg_fast if self.cfg.vol_avg_fast > 0 else 0
        vol_slow = sum(volumes[-self.cfg.vol_avg_slow:]) / self.cfg.vol_avg_slow if self.cfg.vol_avg_slow > 0 else 0
        vol_ratio = vol_fast / vol_slow if vol_slow > 0 else 1.0

        # === ATR-триггер ===
        if self._atr_avg > 0 and atr_now > self._atr_avg * self.cfg.atr_multiplier:
            if self._active:
                self._deactivate_grid()
                return Signal(
                    action="hold",
                    reason=(
                        f"ATR-ТРИГГЕР | ATR={atr_now:.4f} > "
                        f"{self._atr_avg:.4f}×{self.cfg.atr_multiplier} | "
                        f"сетка закрыта"
                    ),
                )

        # === Проверка фильтров ===
        filters_ok = (
            er_now < self.cfg.er_threshold
            and ci_now > self.cfg.ci_threshold
            and adx_now < self.cfg.adx_threshold
            and vol_ratio < self.cfg.vol_ratio_threshold
        )

        if not filters_ok:
            if self._active:
                self._deactivate_grid()
            return Signal(
                action="hold",
                reason=(
                    f"ФИЛТРЫ | ER={er_now:.3f} (<{self.cfg.er_threshold}) "
                    f"CI={ci_now:.1f} (>{self.cfg.ci_threshold}) "
                    f"ADX={adx_now:.1f} (<{self.cfg.adx_threshold}) "
                    f"Vol={vol_ratio:.2f} (<{self.cfg.vol_ratio_threshold}) | "
                    f"сетка НЕ активна"
                ),
            )

        # === Пауза после SL ===
        elapsed_since_sl = time.time() - self._last_sl_time
        if elapsed_since_sl < self.cfg.cooldown_minutes * 60:
            remaining = self.cfg.cooldown_minutes - elapsed_since_sl / 60
            return Signal(
                action="hold",
                reason=f"ПАУЗА после SL | осталось {remaining:.0f} мин",
            )

        # === Инициализация сетки ===
        if not self._active:
            self._build_grid(poc)
            self._active = True
            self._candles_since_exit = 0

        # === Проверка выходов ===
        exit_signal = self._check_exits(candles, current_price, highs, lows, poc)
        if exit_signal:
            return exit_signal

        # === Обновление candle_count у позиций ===
        for pos in self._positions:
            pos.candle_count += 1
            if pos.candle_count >= self.cfg.timeout_hours * 12:  # 12 свечей M5 = 1 час
                return self._close_position(pos, "ТАЙМ-АУТ 24ч")

        # === Временной выход: 3 свечи за границей ===
        upper = poc * (1 + self.cfg.corridor_pct / 100)
        lower = poc * (1 - self.cfg.corridor_pct / 100)
        if current_price > upper or current_price < lower:
            self._candles_since_exit += 1
            if self._candles_since_exit >= self.cfg.time_exit_candles:
                self._deactivate_grid()
                return Signal(
                    action="hold",
                    reason=(
                        f"ВРЕМЕННОЙ ВЫХОД | {self._candles_since_exit} свечей "
                        f"за границей | цена={current_price:.4f} "
                        f"коридор=[{lower:.4f}, {upper:.4f}]"
                    ),
                )
        else:
            self._candles_since_exit = 0

        # === Поиск входа по сетке ===
        entry = self._find_entry(current_price, poc)
        if entry:
            return entry

        # === Hold с информацией ===
        return Signal(
            action="hold",
            reason=(
                f"СЕТКА АКТИВНА | POC={poc:.4f} | цена={current_price:.4f} | "
                f"ER={er_now:.3f} CI={ci_now:.1f} ADX={adx_now:.1f} | "
                f"позиций={len(self._positions)}"
            ),
        )

    def _build_grid(self, poc: float) -> None:
        """Построить геометрическую сетку вокруг POC."""
        self._grid = []
        upper = poc * (1 + self.cfg.corridor_pct / 100)
        lower = poc * (1 - self.cfg.corridor_pct / 100)
        n = self.cfg.grid_levels // 2  # 6 уровней в сторону

        # Геометрический ratio
        ratio = (upper / lower) ** (1 / self.cfg.grid_levels)

        # LONG уровни (от дальнего к ближнему к POC)
        for i in range(n):
            level_price = poc * (ratio ** -(i + 1))  # ниже POC
            self._grid.append(level_price)

        # SHORT уровни (от ближнего к POC к дальнему)
        for i in range(n):
            level_price = poc * (ratio ** (i + 1))  # выше POC
            self._grid.append(level_price)

    def _find_entry(self, current_price: float, poc: float) -> Signal | None:
        """Найти вход по сетке."""
        long_count = sum(1 for p in self._positions if p.direction == "long")
        short_count = sum(1 for p in self._positions if p.direction == "short")
        occupied_long = {p.level_idx for p in self._positions if p.direction == "long"}
        occupied_short = {p.level_idx for p in self._positions if p.direction == "short"}

        # LONG (ниже POC)
        if long_count < 6:
            for idx in range(6):
                if idx in occupied_long:
                    continue
                level_price = self._grid[idx]
                if current_price <= level_price:
                    sl_price = poc * (1 - self.cfg.corridor_pct / 100) * 0.965
                    tp_price = self._grid[idx + 1] if idx + 1 < len(self._grid) else level_price * 1.009

                    self._positions.append(
                        GridPosition(
                            direction="long",
                            entry_price=current_price,
                            level_idx=idx,
                            entry_time=time.time(),
                        )
                    )
                    return Signal(
                        action="buy",
                        reason=(
                            f"GRID LONG @{level_price:.4f} | POC={poc:.4f} | "
                            f"цена={current_price:.4f} | TP={tp_price:.4f} | SL={sl_price:.4f} | "
                            f"long={long_count + 1}/6"
                        ),
                        stop_loss=sl_price,
                        take_profit=tp_price,
                    )

        # SHORT (выше POC)
        if short_count < 6:
            for idx in range(6, 12):
                if idx in occupied_short:
                    continue
                level_price = self._grid[idx]
                if current_price >= level_price:
                    sl_price = poc * (1 + self.cfg.corridor_pct / 100) * 1.035
                    tp_price = self._grid[idx - 1] if idx - 1 >= 0 else level_price * 0.991

                    self._positions.append(
                        GridPosition(
                            direction="short",
                            entry_price=current_price,
                            level_idx=idx,
                            entry_time=time.time(),
                        )
                    )
                    return Signal(
                        action="sell",
                        reason=(
                            f"GRID SHORT @{level_price:.4f} | POC={poc:.4f} | "
                            f"цена={current_price:.4f} | TP={tp_price:.4f} | SL={sl_price:.4f} | "
                            f"short={short_count + 1}/6"
                        ),
                        stop_loss=sl_price,
                        take_profit=tp_price,
                    )

        return None

    def _check_exits(
        self,
        candles: list[Candle],
        current_price: float,
        highs: list[float],
        lows: list[float],
        poc: float,
    ) -> Signal | None:
        """Проверить TP/SL для всех позиций."""
        if not self._positions:
            return None

        upper = poc * (1 + self.cfg.corridor_pct / 100)
        lower = poc * (1 - self.cfg.corridor_pct / 100)
        sl_long = lower * 0.965
        sl_short = upper * 1.035

        for pos in self._positions[:]:
            if pos.direction == "long":
                # TP = следующий уровень сетки (ближе к POC)
                tp_price = self._grid[pos.level_idx + 1] if pos.level_idx + 1 < len(self._grid) else None
                hit_tp = tp_price and current_price >= tp_price
                hit_sl = lows[-1] <= sl_long

                if hit_tp:
                    self._positions.remove(pos)
                    return Signal(
                        action="close_long",
                        reason=f"TP LONG | entry={pos.entry_price:.4f} → {tp_price:.4f}",
                        stop_loss=sl_long,
                        take_profit=tp_price,
                    )
                if hit_sl:
                    self._positions.remove(pos)
                    self._last_sl_time = time.time()
                    self._deactivate_grid()
                    return Signal(
                        action="close_long",
                        reason=f"SL LONG | цена={current_price:.4f} SL={sl_long:.4f}",
                        stop_loss=sl_long,
                    )

            elif pos.direction == "short":
                tp_price = self._grid[pos.level_idx - 1] if pos.level_idx - 1 >= 0 else None
                hit_tp = tp_price and current_price <= tp_price
                hit_sl = highs[-1] >= sl_short

                if hit_tp:
                    self._positions.remove(pos)
                    return Signal(
                        action="close_short",
                        reason=f"TP SHORT | entry={pos.entry_price:.4f} → {tp_price:.4f}",
                        stop_loss=sl_short,
                        take_profit=tp_price,
                    )
                if hit_sl:
                    self._positions.remove(pos)
                    self._last_sl_time = time.time()
                    self._deactivate_grid()
                    return Signal(
                        action="close_short",
                        reason=f"SL SHORT | цена={current_price:.4f} SL={sl_short:.4f}",
                        stop_loss=sl_short,
                    )

        return None

    def _close_position(self, pos: GridPosition, reason: str) -> Signal:
        """Закрыть позицию по рыночной причине."""
        self._positions.remove(pos)
        if pos.direction == "long":
            return Signal(action="close_long", reason=f"{reason} | LONG entry={pos.entry_price:.4f}")
        return Signal(action="close_short", reason=f"{reason} | SHORT entry={pos.entry_price:.4f}")

    def _deactivate_grid(self) -> None:
        """Деактивировать сетку."""
        self._active = False
        self._grid = []
        self._positions.clear()
        self._candles_since_exit = 0

    @staticmethod
    def _calc_volume_poc(
        highs: list[float], lows: list[float], volumes: list[float],
        n_buckets: int = 100,
    ) -> float:
        """Volume Profile POC — объём по бинам high/low."""
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
            start_bin = max(0, int((low - global_low) / bucket_size))
            end_bin = min(n_buckets - 1, int((high - global_low) / bucket_size))
            n_bins = end_bin - start_bin + 1
            vol_per_bin = vol / n_bins
            for b in range(start_bin, end_bin + 1):
                profile[b] += vol_per_bin
        best_idx = profile.index(max(profile))
        return global_low + (best_idx + 0.5) * bucket_size
