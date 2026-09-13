from __future__ import annotations

import os
from dataclasses import dataclass

from core.bybit_client import Candle
from core.indicators import (
    adx as calc_adx,
    atr as calc_atr,
    bollinger,
    ema as calc_ema,
    obv as calc_obv,
    sma as calc_sma,
    vwap as calc_vwap,
)
from core.strategies import BaseStrategy, Signal


def _env_float(name: str, default: float) -> float:
    val = os.getenv(name)
    if val is None or not val.strip():
        return default
    return float(val)


def _env_int(name: str, default: int) -> int:
    val = os.getenv(name)
    if val is None or not val.strip():
        return default
    return int(val)


def _env_bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class _Params:
    """Все параметры стратегии, загружаемые из .env один раз."""

    # ATR
    atr_period: int
    atr_min_pct: float
    atr_max_pct: float
    atr_sl_mult: float
    atr_tp_mult: float

    # Тренд
    ema_fast: int
    ema_slow: int
    ema_global: int
    adx_period: int
    adx_min: float
    di_filter: bool
    vwap_filter: bool
    structure_filter: bool
    swing_window: int

    # Волатильность
    bb_period: int
    bb_std: float
    bbw_min_percentile: int

    # Объём
    volume_ma_period: int
    volume_multiplier: float
    obv_filter: bool


def _load_params() -> _Params:
    return _Params(
        atr_period=_env_int("ATR_PERIOD", 14),
        atr_min_pct=_env_float("ATR_MIN_PCT", 0.0005),
        atr_max_pct=_env_float("ATR_MAX_PCT", 0.005),
        atr_sl_mult=_env_float("ATR_SL_MULT", 1.5),
        atr_tp_mult=_env_float("ATR_TP_MULT", 3.0),
        ema_fast=_env_int("EMA_FAST", 9),
        ema_slow=_env_int("EMA_SLOW", 21),
        ema_global=_env_int("EMA_GLOBAL", 200),
        adx_period=_env_int("ADX_PERIOD", 14),
        adx_min=_env_float("ADX_MIN", 20),
        di_filter=_env_bool("DI_FILTER", True),
        vwap_filter=_env_bool("VWAP_FILTER", True),
        structure_filter=_env_bool("STRUCTURE_FILTER", True),
        swing_window=_env_int("SWING_WINDOW", 3),
        bb_period=_env_int("BB_PERIOD", 20),
        bb_std=_env_float("BB_STD", 2.0),
        bbw_min_percentile=_env_int("BBW_MIN_PERCENTILE", 20),
        volume_ma_period=_env_int("VOLUME_MA_PERIOD", 20),
        volume_multiplier=_env_float("VOLUME_MULTIPLIER", 1.5),
        obv_filter=_env_bool("OBV_FILTER", True),
    )


def _find_swings(closes: list[float], window: int) -> list[tuple[int, str, float]]:
    """Найти свинг-хай и свинг-лоу.

    Свинг-хай: closes[i] — максимум среди window баров в обе стороны.
    Свинг-лоу: closes[i] — минимум среди window баров в обе стороны.

    Возвращает список (индекс, тип, цена) от старых к новым.
    Тип: "H" (high) или "L" (low).
    """
    n = len(closes)
    swings: list[tuple[int, str, float]] = []
    for i in range(window, n - window):
        local_high = max(closes[i - window : i + window + 1])
        local_low = min(closes[i - window : i + window + 1])
        if closes[i] == local_high:
            swings.append((i, "H", closes[i]))
        elif closes[i] == local_low:
            swings.append((i, "L", closes[i]))
    return swings


def _last_n(swings: list[tuple[int, str, float]], kind: str, n: int) -> list[float]:
    """Взять последние n цен свингов заданного типа."""
    return [p for _, t, p in swings if t == kind][-n:]


def _is_bullish_structure(swings: list[tuple[int, str, float]]) -> bool:
    """Проверить HH/HL паттерн (бычья структура)."""
    highs = _last_n(swings, "H", 2)
    lows = _last_n(swings, "L", 2)
    if len(highs) < 2 or len(lows) < 2:
        return False
    return highs[-1] > highs[-2] and lows[-1] > lows[-2]


def _is_bearish_structure(swings: list[tuple[int, str, float]]) -> bool:
    """Проверить LH/LL паттерн (медвежья структура)."""
    highs = _last_n(swings, "H", 2)
    lows = _last_n(swings, "L", 2)
    if len(highs) < 2 or len(lows) < 2:
        return False
    return highs[-1] < highs[-2] and lows[-1] < lows[-2]


def _percentile_rank(values: list[float], lookback: int) -> list[float]:
    """Ранг текущего значения в скользящем окне (0..100)."""
    result: list[float] = []
    for i in range(len(values)):
        start = max(0, i - lookback + 1)
        window = values[start : i + 1]
        count_below = sum(1 for v in window if v < values[i])
        result.append(count_below / len(window) * 100 if window else 50.0)
    return result


class TrendStrategy(BaseStrategy):
    """Трендовая стратегия: ATR + EMA + ADX + VWAP + Volume + Structure.

    Логика:
    1. ATR-фильтр: отсекает мёртвый рынок и хаос.
    2. EMA-фильтр: быстрая > медленная > глобальная = тренд вверх.
    3. ADX-фильтр: сила тренда выше порога.
    4. DI-фильтр: +DI > -DI для лонга.
    5. VWAP-фильтр: цена выше VWAP для лонга.
    6. Structure-фильтр: HH/HL для лонга.
    7. Volume-фильтр: объём выше среднего.
    8. BB squeeze: не торговать при сжатии.
    9. SL/TP считается от ATR.
    """

    name = "trend"

    def __init__(self) -> None:
        self._params = _load_params()

    def check_signal(self, candles: list[Candle]) -> Signal:
        p = self._params

        # Нужно достаточно свечей для самого длинного индикатора
        min_candles = max(p.ema_global, p.adx_period * 2 + 1, p.bb_period, 100)
        if len(candles) < min_candles:
            return Signal(
                action="hold",
                reason=f"недостаточно свечей: {len(candles)}/{min_candles}",
            )

        closes = [c.close for c in candles]
        highs = [c.high for c in candles]
        lows = [c.low for c in candles]
        volumes = [c.volume for c in candles]
        price = closes[-1]

        # --- 1. ATR-фильтр ---
        atr_values = calc_atr(highs, lows, closes, p.atr_period)
        if not atr_values:
            return Signal(action="hold", reason="ATR: мало данных")
        atr_val = atr_values[-1]
        atr_pct = atr_val / price

        if atr_pct < p.atr_min_pct:
            return Signal(action="hold", reason=f"ATR слишком низкий: {atr_pct:.4%}")
        if atr_pct > p.atr_max_pct:
            return Signal(action="hold", reason=f"ATR слишком высокий: {atr_pct:.4%}")

        # --- 2. EMA-фильтр тренда ---
        ema_f = calc_ema(closes, p.ema_fast)
        ema_s = calc_ema(closes, p.ema_slow)
        ema_g = calc_ema(closes, p.ema_global)

        if not ema_f or not ema_s or not ema_g:
            return Signal(action="hold", reason="EMA: мало данных")

        ef, es, eg = ema_f[-1], ema_s[-1], ema_g[-1]

        if ef > es > eg:
            direction = "long"
        elif ef < es < eg:
            direction = "short"
        else:
            return Signal(action="hold", reason="нет тренда: EMA не выстроены")

        # --- 3. ADX-фильтр ---
        adx_values = calc_adx(highs, lows, closes, p.adx_period)
        if not adx_values:
            return Signal(action="hold", reason="ADX: мало данных")
        adx_val = adx_values[-1]

        if adx_val < p.adx_min:
            return Signal(action="hold", reason=f"слабый тренд: ADX={adx_val:.1f} < {p.adx_min}")

        # --- 4. DI-фильтр ---
        if p.di_filter:
            # Вычисляем +DI/-DI из ADX-расчёта
            n = len(closes)
            trs: list[float] = [highs[0] - lows[0]]
            dm_plus: list[float] = [0.0]
            dm_minus: list[float] = [0.0]
            for i in range(1, n):
                tr = max(
                    highs[i] - lows[i],
                    abs(highs[i] - closes[i - 1]),
                    abs(lows[i] - closes[i - 1]),
                )
                trs.append(tr)
                up = highs[i] - highs[i - 1]
                down = lows[i - 1] - lows[i]
                dm_plus.append(up if up > down and up > 0 else 0.0)
                dm_minus.append(down if down > up and down > 0 else 0.0)

            period = p.adx_period
            atr_s = sum(trs[1 : period + 1]) / period
            dm_ps = sum(dm_plus[1 : period + 1]) / period
            dm_ms = sum(dm_minus[1 : period + 1]) / period
            for i in range(period + 1, n):
                atr_s = (atr_s * (period - 1) + trs[i]) / period
                dm_ps = (dm_ps * (period - 1) + dm_plus[i]) / period
                dm_ms = (dm_ms * (period - 1) + dm_minus[i]) / period

            dip = (dm_ps / atr_s * 100) if atr_s > 0 else 0.0
            dim = (dm_ms / atr_s * 100) if atr_s > 0 else 0.0

            if direction == "long" and dip <= dim:
                return Signal(action="hold", reason=f"+DI({dip:.1f}) <= -DI({dim:.1f})")
            if direction == "short" and dim <= dip:
                return Signal(action="hold", reason=f"-DI({dim:.1f}) <= +DI({dip:.1f})")

        # --- 5. VWAP-фильтр ---
        if p.vwap_filter:
            vwap_values = calc_vwap(highs, lows, closes, volumes)
            if vwap_values:
                vwap_now = vwap_values[-1]
                if direction == "long" and price < vwap_now:
                    return Signal(action="hold", reason=f"цена {price:.2f} < VWAP {vwap_now:.2f}")
                if direction == "short" and price > vwap_now:
                    return Signal(action="hold", reason=f"цена {price:.2f} > VWAP {vwap_now:.2f}")

        # --- 6. Structure-фильтр ---
        if p.structure_filter:
            swings = _find_swings(closes, p.swing_window)
            if len(swings) >= 4:
                if direction == "long" and not _is_bullish_structure(swings):
                    return Signal(action="hold", reason="нет бычьей структуры (HH/HL)")
                if direction == "short" and not _is_bearish_structure(swings):
                    return Signal(action="hold", reason="нет медвежьей структуры (LH/LL)")

        # --- 7. Volume-фильтр ---
        vol_ma = calc_sma(volumes, p.volume_ma_period)
        if vol_ma:
            vol_avg = vol_ma[-1]
            if volumes[-1] < vol_avg * p.volume_multiplier:
                return Signal(
                    action="hold",
                    reason=f"низкий объём: {volumes[-1]:.0f} < {vol_avg * p.volume_multiplier:.0f}",
                )

        # --- 8. OBV-фильтр ---
        if p.obv_filter:
            obv_values = calc_obv(closes, volumes)
            if len(obv_values) >= 10:
                obv_slope = obv_values[-1] - obv_values[-10]
                if direction == "long" and obv_slope < 0:
                    return Signal(action="hold", reason="OBV падает")
                if direction == "short" and obv_slope > 0:
                    return Signal(action="hold", reason="OBV растёт")

        # --- 9. BB squeeze ---
        bb_upper, bb_middle, bb_lower = bollinger(closes, p.bb_period, p.bb_std)
        if bb_upper and bb_middle and bb_lower:
            bbw = (bb_upper[-1] - bb_lower[-1]) / bb_middle[-1] if bb_middle[-1] > 0 else 0
            bbw_series = [
                (bb_upper[i] - bb_lower[i]) / bb_middle[i]
                if bb_middle[i] > 0
                else 0
                for i in range(len(bb_upper))
            ]
            bbw_pctrank = _percentile_rank(bbw_series, 100)
            if bbw_pctrank and bbw_pctrank[-1] < p.bbw_min_percentile:
                return Signal(action="hold", reason=f"BB squeeze:(percentile={bbw_pctrank[-1]:.0f})")

        # --- Сигнал ---
        sl = atr_val * p.atr_sl_mult
        tp = atr_val * p.atr_tp_mult

        if direction == "long":
            return Signal(
                action="buy",
                reason=(
                    f"тренд ↑: EMA({p.ema_fast}>{p.ema_slow}>{p.ema_global}), "
                    f"ADX={adx_val:.1f}, ATR%={atr_pct:.3%}"
                ),
                stop_loss=price - sl,
                take_profit=price + tp,
            )
        return Signal(
            action="sell",
            reason=(
                f"тренд ↓: EMA({p.ema_fast}<{p.ema_slow}<{p.ema_global}), "
                f"ADX={adx_val:.1f}, ATR%={atr_pct:.3%}"
            ),
            stop_loss=price + sl,
            take_profit=price - tp,
        )
