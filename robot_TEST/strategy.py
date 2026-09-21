"""TEST — Grid Flat (сетка в боковике).

POC фиксируется при старте. Сетка ±10% от POC.
Импульсный фильтр: если цена двигается ≥15% за 5 свечей — пауза.
TP = соседний уровень -1%. SL = граница коридора +3%.
Частичное закрытие 50% при возврате к POC. Трейлинг 2% после POC.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

from core.bybit_client import Candle
from core.strategies import BaseStrategy, Signal


# ─── Индикаторы ──────────────────────────────────────────────


def _ema(data: list[float], period: int) -> list[float]:
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


# ─── Конфиг ──────────────────────────────────────────────────


@dataclass
class TestConfig:
    # --- POC ---
    poc_lookback: int = 600        # 50 часов M5

    # --- Коридор ---
    range_pct: float = 20.0        # ±10% от POC

    # --- Импульс ---
    impulse_min_pct: float = 15.0  # импульс ≥15%
    impulse_window: int = 5        # за 5 свечей
    impulse_cooldown: int = 30     # пауза 30 свечей

    # --- Сетка (фиксированные уровни %) ---
    order_levels: list[float] = field(
        default_factory=lambda: [-6.0, -8.0, -10.0, 6.0, 8.0, 10.0]
    )
    stop_zone_pct: float = 5.0     # запрет ордеров ±5%
    stop_from_border_pct: float = 3.0  # SL = граница + 3%
    tp_offset_pct: float = 1.0     # TP = противоположный -1%

    # --- Позиции ---
    max_positions: int = 3         # макс 3 в сторону
    partial_close_pct: float = 50.0  # 50% на POC
    trailing_after_poc_pct: float = 2.0  # трейлинг 2%

    # --- Размер ---
    order_size: float = 100.0      # $100 на ордер


# ─── Состояния ───────────────────────────────────────────────


@dataclass
class GridPosition:
    direction: str   # "long" / "short"
    entry_price: float
    level_pct: float  # % уровень сетки
    entry_time: float
    candle_count: int = 0
    trailing_active: bool = False
    trailing_high: float = 0.0  # для long
    trailing_low: float = float('inf')  # для short


# ─── Стратегия ───────────────────────────────────────────────


class TestStrategy(BaseStrategy):
    name = "grid_flat"
    _min_warmup = 350

    def __init__(self, cfg: TestConfig | None = None):
        self.cfg = cfg or TestConfig()
        self._poc: float = 0.0
        self._active = False
        self._positions: list[GridPosition] = []
        self._cooldown_until: float = 0.0
        self._partial_done: bool = False
        self._poc_hit: bool = False

    def check_signal(self, candles: list[Candle]) -> Signal:
        n = len(candles)
        if n < self.cfg.poc_lookback + 50:
            return Signal(action="hold", reason="разогрев")

        current = candles[-1]
        current_price = current.close
        highs = [c.high for c in candles]
        lows = [c.low for c in candles]
        closes = [c.close for c in candles]

        # POC
        poc = self._calc_volume_poc(
            highs[-self.cfg.poc_lookback:],
            lows[-self.cfg.poc_lookback:],
            [c.volume for c in candles[-self.cfg.poc_lookback:]],
        )
        if self._poc == 0.0:
            self._poc = poc
        poc = self._poc

        # Коридор
        upper = poc * (1 + self.cfg.range_pct / 200)
        lower = poc * (1 - self.cfg.range_pct / 200)

        # Импульс-фильтр
        if n >= self.cfg.impulse_window + 1:
            window_start = closes[-self.cfg.impulse_window - 1]
            change_pct = abs(current_price - window_start) / window_start * 100
            if change_pct >= self.cfg.impulse_min_pct:
                self._cooldown_until = time.time() + self.cfg.impulse_cooldown * 5 * 60
                if self._active:
                    self._deactivate("ИМПУЛЬС")
                return Signal(
                    action="hold",
                    reason=f"ИМПУЛЬС {change_pct:.1f}% за {self.cfg.impulse_window} свечей",
                )

        # Пауза после импульса
        if time.time() < self._cooldown_until:
            remain = (self._cooldown_until - time.time()) / 60
            return Signal(action="hold", reason=f"ПАУЗА после импульса {remain:.0f} мин")

        # Частичное закрытие на POC
        if self._positions and not self._partial_done:
            for pos in self._positions[:]:
                if pos.direction == "long" and current_price >= poc:
                    self._partial_close(pos, "POC LONG")
                    self._partial_done = True
                    self._poc_hit = True
                    break
                elif pos.direction == "short" and current_price <= poc:
                    self._partial_close(pos, "POC SHORT")
                    self._partial_done = True
                    self._poc_hit = True
                    break

        # Трейлинг после POC
        if self._poc_hit:
            for pos in self._positions[:]:
                if pos.direction == "long":
                    trail_level = poc * (1 - self.cfg.trailing_after_poc_pct / 100)
                    if current_price > poc:
                        pos.trailing_active = True
                    if pos.trailing_active and current_price <= trail_level:
                        self._close_one(pos, f"ТРЕЙЛИНГ LONG {current_price:.4f} < {trail_level:.4f}")
                elif pos.direction == "short":
                    trail_level = poc * (1 + self.cfg.trailing_after_poc_pct / 100)
                    if current_price < poc:
                        pos.trailing_active = True
                    if pos.trailing_active and current_price >= trail_level:
                        self._close_one(pos, f"ТРЕЙЛИНГ SHORT {current_price:.4f} > {trail_level:.4f}")

        # TP / SL
        for pos in self._positions[:]:
            sl_border_long = lower - poc * self.cfg.stop_from_border_pct / 100
            sl_border_short = upper + poc * self.cfg.stop_from_border_pct / 100

            if pos.direction == "long":
                # TP = ближайший уровень выше × (1 - tp_offset)
                tp_price = poc * (1 + abs(pos.level_pct) / 100) * (1 - self.cfg.tp_offset_pct / 100)
                if current_price >= tp_price:
                    self._close_one(pos, f"TP LONG {current_price:.4f} >= {tp_price:.4f}")
                elif current.low <= sl_border_long:
                    self._close_one(pos, f"SL LONG {current.low:.4f} <= {sl_border_long:.4f}")

            elif pos.direction == "short":
                tp_price = poc * (1 - abs(pos.level_pct) / 100) * (1 + self.cfg.tp_offset_pct / 100)
                if current_price <= tp_price:
                    self._close_one(pos, f"TP SHORT {current_price:.4f} <= {tp_price:.4f}")
                elif current.high >= sl_border_short:
                    self._close_one(pos, f"SL SHORT {current.high:.4f} >= {sl_border_short:.4f}")

        # Временной выход — если позиция старше 200 свечей
        for pos in self._positions[:]:
            pos.candle_count += 1
            if pos.candle_count >= 200:
                self._close_one(pos, f"ТАЙМАУТ {pos.candle_count} свечей")

        # Поиск входа
        entry = self._find_entry(current_price, poc, upper, lower)
        if entry:
            return entry

        # Hold
        long_c = sum(1 for p in self._positions if p.direction == "long")
        short_c = sum(1 for p in self._positions if p.direction == "short")
        return Signal(
            action="hold",
            reason=f"POC={poc:.4f} | цена={current_price:.4f} | "
                   f"коридор=[{lower:.4f}, {upper:.4f}] | "
                   f"long={long_c} short={short_c}",
        )

    def _find_entry(
        self, price: float, poc: float, upper: float, lower: float,
    ) -> Signal | None:
        long_count = sum(1 for p in self._positions if p.direction == "long")
        short_count = sum(1 for p in self._positions if p.direction == "short")
        occupied_long = {p.level_pct for p in self._positions if p.direction == "long"}
        occupied_short = {p.level_pct for p in self._positions if p.direction == "short"}

        # LONG (отрицательные уровни)
        for lvl in self.cfg.order_levels:
            if lvl >= 0:
                continue
            if lvl in occupied_long:
                continue
            if long_count >= self.cfg.max_positions:
                break
            level_price = poc * (1 + lvl / 100)
            if price <= level_price:
                sl_price = lower - poc * self.cfg.stop_from_border_pct / 100
                # TP = противоположный уровень - offset
                tp_price = poc * (1 + abs(lvl) / 100) * (1 - self.cfg.tp_offset_pct / 100)
                self._positions.append(
                    GridPosition(direction="long", entry_price=price, level_pct=lvl, entry_time=time.time())
                )
                long_count += 1
                return Signal(
                    action="buy",
                    reason=f"GRID LONG {lvl:+.0f}% @{level_price:.4f} | POC={poc:.4f} | "
                           f"TP={tp_price:.4f} | SL={sl_price:.4f} | long={long_count}/{self.cfg.max_positions}",
                    stop_loss=sl_price,
                    take_profit=tp_price,
                )

        # SHORT (положительные уровни)
        for lvl in self.cfg.order_levels:
            if lvl <= 0:
                continue
            if lvl in occupied_short:
                continue
            if short_count >= self.cfg.max_positions:
                break
            level_price = poc * (1 + lvl / 100)
            if price >= level_price:
                sl_price = upper + poc * self.cfg.stop_from_border_pct / 100
                tp_price = poc * (1 - lvl / 100) * (1 + self.cfg.tp_offset_pct / 100)
                self._positions.append(
                    GridPosition(direction="short", entry_price=price, level_pct=lvl, entry_time=time.time())
                )
                short_count += 1
                return Signal(
                    action="sell",
                    reason=f"GRID SHORT {lvl:+.0f}% @{level_price:.4f} | POC={poc:.4f} | "
                           f"TP={tp_price:.4f} | SL={sl_price:.4f} | short={short_count}/{self.cfg.max_positions}",
                    stop_loss=sl_price,
                    take_profit=tp_price,
                )

        return None

    def _close_one(self, pos: GridPosition, reason: str) -> None:
        self._positions.remove(pos)

    def _partial_close(self, pos: GridPosition, reason: str) -> None:
        pos.entry_price = pos.entry_price  # Keep tracking half position
        pos.level_pct = pos.level_pct

    def _deactivate(self, reason: str) -> None:
        self._active = False
        self._positions.clear()
        self._partial_done = False
        self._poc_hit = False

    @staticmethod
    def _calc_volume_poc(
        highs: list[float], lows: list[float], volumes: list[float],
        n_buckets: int = 100,
    ) -> float:
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
