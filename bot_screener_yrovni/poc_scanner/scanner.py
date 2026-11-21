"""POCScanner: уровни по окнам, слияние, сигналы о подходе цены."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from bot_screener_yrovni.poc_scanner.config import (
    HOURS_PER_CANDLE,
    INTERVAL_FOUR_HOUR,
    POCConfig,
    WindowSpec,
    get_poc_age_days,
    parse_window_days,
)
from bot_screener_yrovni.poc_scanner.klines import KlineCache, RateLimiter
from bot_screener_yrovni.poc_scanner.profile import compute_profile
from bot_screener_yrovni.poc_scanner.signals import (
    ACTION_OBSERVE,
    ACTION_PLACE,
    POCSignal,
)
from bot_screener_yrovni.poc_scanner.store import LevelState, POCStore
from bot_screener_yrovni.poc_scanner.timeutil import (
    parse_iso,
    to_iso,
    utc_now,
    utc_now_iso,
)
from core.logger import get_logger

logger = get_logger(__name__)

WALL_MIN_WINDOWS = 3
MIN_PARTIAL_CANDLES = 2


def compute_atr(df: pd.DataFrame, period: int = 14) -> float:
    """Средний истинный диапазон по последним ``period`` свечам."""
    if len(df) < period + 1:
        return 0.0
    high = df["high"].to_numpy(dtype=float)
    low = df["low"].to_numpy(dtype=float)
    close = df["close"].to_numpy(dtype=float)
    prev_close = close[:-1]
    true_range = np.maximum(
        high[1:] - low[1:],
        np.maximum(np.abs(high[1:] - prev_close), np.abs(low[1:] - prev_close)),
    )
    return float(true_range[-period:].mean())


def _window_days(name: str) -> int:
    try:
        return parse_window_days(name)
    except ValueError:
        return 0


def _window_sort_key(name: str) -> tuple[int, int, str]:
    """Окна по возрастанию дней, затем календарные отрезки по имени."""
    days = _window_days(name)
    return (0, days, name) if days > 0 else (1, 0, name)


def _span_days(candles: pd.DataFrame, interval: str) -> int:
    """Сколько дней реально покрывают свечи (для частичных окон)."""
    if candles.empty:
        return 0
    hours = HOURS_PER_CANDLE[interval]
    delta = candles["ts"].iloc[-1] - candles["ts"].iloc[0]
    span = float(delta.total_seconds()) / 86_400
    return round(span + hours / 24)


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    """Дописать строку JSON в файл журнала (ошибка записи не роняет скан)."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except OSError as exc:
        logger.warning("не удалось записать %s: %s", path, exc)


class POCScanner:
    """Считает POC по окнам, ведёт историю уровней и выдаёт сигналы."""

    def __init__(
        self,
        config: POCConfig,
        symbol: str | None = None,
        *,
        store: POCStore | None = None,
        cache: KlineCache | None = None,
    ) -> None:
        self.symbol = (symbol or config.symbol or "").upper()
        if not self.symbol:
            raise ValueError("POCScanner: не задан символ")
        self.config = (
            config
            if config.symbol == self.symbol
            else replace(config, symbol=self.symbol)
        )
        self.store = store if store is not None else POCStore(self.config.db_path)
        self.cache = (
            cache if cache is not None else KlineCache(self.store, RateLimiter(10.0))
        )
        self._series: dict[str, pd.DataFrame] = {}
        self._short_history_warned: set[str] = set()

    def close(self) -> None:
        """Закрыть соединение с БД."""
        self.store.close()

    # --- история ---------------------------------------------------------

    def load_history(
        self,
        interval: str = "60",
        now: datetime | None = None,
        days: int | None = None,
    ) -> pd.DataFrame:
        """Свечи символа за глубину окон интервала или явно заданную ``days``."""
        depth = days if days is not None else self.config.days_for_interval(interval)
        if depth <= 0:
            return pd.DataFrame()
        frame = self.cache.load(self.symbol, interval, depth, now)
        self._series[interval] = frame
        return frame

    # --- расчёт ----------------------------------------------------------

    def calculate_poc(
        self,
        df: pd.DataFrame,
        window: str,
    ) -> dict[str, Any] | None:
        """POC и Value Area по последним свечам окна ``window``.

        Если истории меньше, чем нужно окну, профиль считается по фактическим
        свечам: ``partial=True``, а возраст — реальный охват истории.

        Returns:
            {window, poc, vah, val, total_volume, ts_calculated, candles,
            interval, segment_type, segment_start, segment_end, age_days,
            partial} или None, если профиль не построился.
        """
        spec = self.config.spec(window)
        candles = df.tail(spec.candles)
        if candles.empty:
            return None
        result = compute_profile(
            candles,
            num_bins=self.config.bins,
            va_percent=self.config.va_percent,
        )
        if result is None:
            return None
        partial = len(candles) < spec.candles
        age_days = (
            _span_days(candles, spec.interval) if partial else get_poc_age_days(window)
        )
        return {
            "window": window,
            "poc": result.poc,
            "vah": result.vah,
            "val": result.val,
            "total_volume": result.total_volume,
            "ts_calculated": utc_now_iso(),
            "candles": len(candles),
            "interval": spec.interval,
            "segment_type": "rolling",
            "segment_start": to_iso(candles["ts"].iloc[0].to_pydatetime()),
            "segment_end": to_iso(candles["ts"].iloc[-1].to_pydatetime()),
            "age_days": age_days,
            "partial": partial,
        }

    def calculate_calendar_poc(
        self,
        df: pd.DataFrame,
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """POC по историческим отрезкам: календарный месяц или блоки 30 дней.

        Возвращает до ``TOP_N_SEGMENTS`` последних отрезков с объёмом не ниже
        ``SEGMENT_MIN_VOLUME_USD``. Возраст отрезка — сколько дней прошло
        с его последней свечи.
        """
        if df.empty or self.config.segment_mode == "off":
            return []
        moment = now or utc_now()
        keys = self._segment_keys(df, self.config.segment_mode)
        frame = df.assign(_segment=keys)
        first_key, last_key = keys[0], keys[-1]
        results: list[dict[str, Any]] = []
        for segment, part in frame.groupby("_segment", sort=True):
            if len(part) < MIN_PARTIAL_CANDLES:
                continue
            result = compute_profile(
                part,
                num_bins=self.config.bins,
                va_percent=self.config.va_percent,
            )
            if result is None:
                continue
            if result.total_volume < self.config.segment_min_volume:
                continue
            segment_end = part["ts"].iloc[-1].to_pydatetime()
            results.append(
                {
                    "window": str(segment),
                    "poc": result.poc,
                    "vah": result.vah,
                    "val": result.val,
                    "total_volume": result.total_volume,
                    "ts_calculated": utc_now_iso(),
                    "candles": len(part),
                    "interval": INTERVAL_FOUR_HOUR,
                    "segment_type": (
                        "calendar_month"
                        if self.config.segment_mode == "calendar_month"
                        else "fixed_30d"
                    ),
                    "segment_start": to_iso(part["ts"].iloc[0].to_pydatetime()),
                    "segment_end": to_iso(segment_end),
                    "age_days": max(0, (moment - segment_end).days),
                    "partial": str(segment) in (first_key, last_key),
                }
            )
        return results[-self.config.top_n_segments :]

    @staticmethod
    def _segment_keys(df: pd.DataFrame, mode: str) -> list[str]:
        """Ключ отрезка для каждой свечи: ``2026-09`` или дата начала блока."""
        stamps = df["ts"]
        if mode == "fixed_30d":
            block_seconds = 30 * 86_400
            last = stamps.iloc[-1]
            blocks = ((last - stamps).dt.total_seconds() // block_seconds).astype(int)
            starts = stamps - pd.to_timedelta(blocks * block_seconds, unit="s")
            return [str(stamp.strftime("%Y-%m-%d")) for stamp in starts]
        return [str(value) for value in stamps.dt.strftime("%Y-%m")]

    def upsert_poc_level(self, poc_data: dict[str, Any]) -> LevelState:
        """Создать уровень POC или слить с уже известным в пределах 1 %."""
        window = str(poc_data["window"])
        segment_type = str(poc_data.get("segment_type", "rolling"))
        age_days = int(poc_data.get("age_days") or get_poc_age_days(window))
        candidate = LevelState(
            symbol=self.symbol,
            poc_price=float(poc_data["poc"]),
            vah=float(poc_data["vah"]),
            val=float(poc_data["val"]),
            source_windows=(window,),
            window_volumes={window: float(poc_data["total_volume"])},
            poc_age_days=age_days,
            first_created_ts=str(poc_data.get("segment_start") or ""),
            last_calculated_ts=str(poc_data.get("ts_calculated") or utc_now_iso()),
            va_window=window,
            segment_type=segment_type,
            segment_start_ts=str(poc_data.get("segment_start") or ""),
            segment_end_ts=str(poc_data.get("segment_end") or ""),
            partial_window=bool(poc_data.get("partial", False)),
        )
        level = self.store.upsert_level(
            candidate,
            merge_pct=self.config.merge_poc_pct,
            confirm_threshold=self.config.confirm_volume_threshold,
        )
        _append_jsonl(
            self.config.poc_log,
            {
                "ts": level.last_calculated_ts,
                "symbol": level.symbol,
                "window": window,
                "poc_price": round(level.poc_price, 10),
                "vah": round(level.vah, 10),
                "val": round(level.val, 10),
                "total_volume_at_level": round(level.total_volume_at_level, 2),
                "source_windows": list(level.source_windows),
                "poc_age_days": level.poc_age_days,
                "first_created_ts": level.first_created_ts,
                "confirmed": level.confirmed,
                "touch_count": level.touch_count,
                "segment_type": level.segment_type,
                "segment_start_ts": level.segment_start_ts,
                "segment_end_ts": level.segment_end_ts,
                "partial_window": level.partial_window,
            },
        )
        wall = " WALL" if self._is_wall(level) else ""
        if segment_type == "rolling":
            logger.info(
                "POC %s %s=%.10g (VA %.10g..%.10g) объём $%.1fM окна %s%s",
                self.symbol,
                window,
                level.poc_price,
                level.val,
                level.vah,
                level.total_volume_at_level / 1_000_000,
                ",".join(level.source_windows),
                wall,
            )
        else:
            logger.info(
                "%s POC %s: price=%.10g, vol=$%.1fM, age=%dd%s",
                segment_type,
                window,
                candidate.poc_price,
                float(poc_data["total_volume"]) / 1_000_000,
                age_days,
                wall,
            )
        if (
            segment_type != "rolling"
            and level.segment_type == "rolling"
            and len(level.source_windows) > 1
        ):
            logger.info(
                "merged calendar %s POC %.10g into rolling level (age updated to %dd)",
                window,
                level.poc_price,
                level.poc_age_days,
            )
        return level

    # --- сигналы ---------------------------------------------------------

    def check_price_proximity(
        self,
        current_price: float,
        atr: float = 0.0,
        now: datetime | None = None,
    ) -> list[POCSignal]:
        """Сигналы по уровням, к которым цена подошла вплотную.

        Касание увеличивает ``touch_count`` не чаще, чем раз в
        ``TOUCH_COOLDOWN_SEC``; сигнал по одному уровню — не чаще
        ``SIGNAL_COOLDOWN_SEC``.
        """
        moment = now or utc_now()
        now_iso = to_iso(moment)
        signals: list[POCSignal] = []

        for level in self.store.levels(self.symbol):
            if level.poc_price <= 0 or current_price <= 0:
                continue
            age_days = self._level_age_days(level)
            if not (
                self.config.min_poc_age_days <= age_days <= self.config.max_poc_age_days
            ):
                continue
            proximity = abs(current_price - level.poc_price) / level.poc_price
            if proximity > self.config.touch_proximity_pct:
                continue

            touch_count = level.touch_count
            if self._touch_allowed(level, moment):
                if level.id is not None:
                    self.store.record_touch(level.id, now_iso)
                touch_count += 1

            strong = level.confirmed or (
                level.total_volume_at_level >= self.config.min_signal_volume
            )
            if not strong or not self._signal_allowed(level, moment):
                continue

            confidence = self._confidence(level, age_days, touch_count)
            signal = POCSignal(
                ts=now_iso,
                symbol=self.symbol,
                poc_price=level.poc_price,
                poc_windows=list(level.source_windows),
                poc_age_days=age_days,
                total_volume_at_level=level.total_volume_at_level,
                touch_count=touch_count,
                is_wall=self._is_wall(level),
                proximity_pct=proximity,
                vah=level.vah,
                val=level.val,
                confidence=confidence,
                action=(
                    ACTION_PLACE
                    if confidence >= self.config.min_confidence
                    else ACTION_OBSERVE
                ),
                side_hint="both",
                order_params=self._order_params(level, atr),
            )
            payload = signal.to_dict()
            self.store.log_signal(payload)
            _append_jsonl(self.config.signal_log, payload)
            logger.info(
                "СИГНАЛ %s POC=%.10g proximity=%.3f%% confidence=%.3f %s",
                self.symbol,
                signal.poc_price,
                signal.proximity_pct * 100,
                signal.confidence,
                signal.action,
            )
            signals.append(signal)
        return signals

    def combine_signals(
        self,
        signals: list[POCSignal],
    ) -> POCSignal | None:
        """Свести сигналы символа в один: сильнейший + окна соседних уровней."""
        if not signals:
            return None
        best = max(signals, key=lambda item: item.confidence)
        windows: set[str] = set(best.poc_windows)
        for signal in signals:
            if signal is best or best.poc_price <= 0:
                continue
            relative = abs(signal.poc_price - best.poc_price) / best.poc_price
            if relative <= self.config.merge_poc_pct:
                windows.update(signal.poc_windows)
        best.poc_windows = sorted(windows, key=_window_sort_key)
        return best

    def refresh(
        self,
        current_price: float | None = None,
        now: datetime | None = None,
    ) -> POCSignal | None:
        """Пересчитать все окна, обновить уровни и вернуть сигнал (или None)."""
        moment = now or utc_now()
        for interval in self.config.intervals:
            try:
                self.load_history(interval, moment)
            except Exception as exc:  # noqa: BLE001 — сеть не должна ронять цикл
                logger.warning(
                    "история %s %s недоступна: %s", self.symbol, interval, exc
                )

        statuses: list[str] = []
        for spec in self.config.specs:
            age_days = get_poc_age_days(spec.name)
            if age_days < self.config.min_poc_age_days:
                statuses.append(f"{spec.name}=skip(age={age_days})")
                continue
            frame = self._series.get(spec.interval)
            if frame is None or len(frame) < MIN_PARTIAL_CANDLES:
                statuses.append(f"{spec.name}=skip(no_history)")
                continue
            if len(frame) < spec.candles:
                self._warn_short_history(spec, len(frame))
            poc_data = self.calculate_poc(frame, spec.name)
            if poc_data is None:
                statuses.append(f"{spec.name}=skip(no_profile)")
                continue
            self.upsert_poc_level(poc_data)
            status = f"{spec.name}=ok(age={poc_data['age_days']})"
            if poc_data["partial"]:
                status = f"{spec.name}=ok(age={poc_data['age_days']},partial)"
            statuses.append(status)
        logger.info("%s: POC levels loaded: %s", self.symbol, ", ".join(statuses))
        self._refresh_segments(moment)

        price = current_price if current_price is not None else self._last_price()
        if price <= 0:
            logger.warning("%s: нет цены для проверки близости", self.symbol)
            return None
        frame_1h = self._series.get("60")
        atr = (
            compute_atr(frame_1h, self.config.atr_period)
            if frame_1h is not None and not frame_1h.empty
            else 0.0
        )
        return self.combine_signals(self.check_price_proximity(price, atr, moment))

    # --- вспомогательное -------------------------------------------------

    def _last_price(self) -> float:
        for interval in ("60", "240"):
            frame = self._series.get(interval)
            if frame is not None and not frame.empty:
                return float(frame["close"].iloc[-1])
        return 0.0

    def _warn_short_history(self, spec: WindowSpec, available: int) -> None:
        """Предупредить один раз, что окну не хватает истории (partial)."""
        if spec.name in self._short_history_warned:
            return
        self._short_history_warned.add(spec.name)
        logger.warning(
            "%s: окну %s нужно %d свечей, в истории %d — считаю по фактической "
            "истории (partial_window)",
            self.symbol,
            spec.name,
            spec.candles,
            available,
        )

    def _refresh_segments(self, moment: datetime) -> None:
        """Пересчитать POC по историческим отрезкам (месяцы или блоки 30 дней)."""
        if self.config.segment_mode == "off":
            return
        frame = self._series.get(INTERVAL_FOUR_HOUR)
        if frame is None or frame.empty:
            try:
                frame = self.load_history(
                    INTERVAL_FOUR_HOUR,
                    moment,
                    days=self.config.history_cache_days,
                )
            except Exception as exc:  # noqa: BLE001 — сеть не должна ронять цикл
                logger.warning(
                    "история %s %s недоступна: %s",
                    self.symbol,
                    INTERVAL_FOUR_HOUR,
                    exc,
                )
                return
        segments = self.calculate_calendar_poc(frame, moment)
        if not segments:
            return
        logger.info(
            "%s: POC segments loaded: %s",
            self.symbol,
            ", ".join(
                f"{item['window']} (vol=${item['total_volume'] / 1_000_000:.1f}M)"
                for item in segments
            ),
        )
        for item in segments:
            self.upsert_poc_level(item)

    @staticmethod
    def _level_age_days(level: LevelState) -> int:
        """Возраст уровня в днях: из поля таблицы или по его окнам."""
        if level.poc_age_days > 0:
            return level.poc_age_days
        return max(
            (get_poc_age_days(name) for name in level.source_windows),
            default=0,
        )

    def _touch_allowed(self, level: LevelState, now: datetime) -> bool:
        if not level.last_touch_ts:
            return True
        try:
            last = parse_iso(level.last_touch_ts)
        except ValueError:
            return True
        return (now - last).total_seconds() >= self.config.touch_cooldown_sec

    def _signal_allowed(self, level: LevelState, now: datetime) -> bool:
        last_text = self.store.last_signal_ts(self.symbol, level.poc_price)
        if last_text is None:
            return True
        try:
            last = parse_iso(last_text)
        except ValueError:
            return True
        return (now - last).total_seconds() >= self.config.signal_cooldown_sec

    def _confidence(
        self,
        level: LevelState,
        age_days: int,
        touch_count: int,
    ) -> float:
        """Вес уровня: объём 50 %, совпадение окон 30 %, возраст и касания по 10 %."""
        threshold = self.config.confirm_volume_threshold
        volume_part = (
            level.total_volume_at_level / threshold * 0.5 if threshold > 0 else 0.0
        )
        windows_total = max(len(self.config.windows), 1)
        windows_part = len(level.source_windows) / windows_total * 0.3
        age_part = age_days / 365 * 0.1
        touch_part = min(touch_count / 10, 1.0) * 0.1
        return min(1.0, volume_part + windows_part + age_part + touch_part)

    def _is_wall(self, level: LevelState) -> bool:
        return (
            level.total_volume_at_level >= self.config.wall_volume_threshold
            or len(level.source_windows) >= WALL_MIN_WINDOWS
        )

    @staticmethod
    def _order_params(level: LevelState, atr: float) -> dict[str, float]:
        params = {"buy_at": level.val, "sell_at": level.vah}
        if atr > 0:
            params["stop_buy"] = level.val - 0.5 * atr
            params["stop_sell"] = level.vah + 0.5 * atr
        return params
