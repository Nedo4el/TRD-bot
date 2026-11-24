"""POCScanner: уровни по окнам, слияние, сигналы о подходе цены."""

from __future__ import annotations

import json
import math
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from bot_screener_yrovni.poc_scanner.clusters import (
    build_clusters,
    direction_for,
    nearest_cluster,
    zone_distance,
)
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
from bot_screener_yrovni.poc_scanner.signals import POCSignal
from bot_screener_yrovni.poc_scanner.store import LevelState, POCStore, ZoneState
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


def _window_days(name: str) -> int:
    try:
        return parse_window_days(name)
    except ValueError:
        return 0


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


def zone_contact_hours(
    frame: pd.DataFrame,
    level: float,
    zone_pct: float,
    moment: datetime,
) -> tuple[float, float] | None:
    """Пауза до подхода и время в зоне по закрытым свечам.

    Возвращает ``(quiet_hours, contact_hours)``: сколько часов цена была
    вне зоны до текущего подхода и сколько часов уже находится в зоне.
    ``quiet_hours`` — ``inf``, если в загруженной истории подходов не было.
    ``None`` — если истории нет или последняя свеча вне зоны.
    """
    if frame.empty or level <= 0:
        return None
    closes = frame["close"].astype(float)
    flags = (((closes - level).abs() / closes) <= zone_pct).to_numpy()
    if not bool(flags[-1]):
        return None
    start = len(flags) - 1
    while start > 0 and flags[start - 1]:
        start -= 1
    run_start = frame["ts"].iloc[start].to_pydatetime()
    contact_hours = max(0.0, (moment - run_start).total_seconds() / 3600.0)
    previous = start - 1
    while previous >= 0 and not flags[previous]:
        previous -= 1
    if previous < 0:
        return float("inf"), contact_hours
    last_contact = frame["ts"].iloc[previous].to_pydatetime()
    quiet_hours = max(0.0, (run_start - last_contact).total_seconds() / 3600.0)
    return quiet_hours, contact_hours


def departure_pct(
    frame: pd.DataFrame,
    level: float,
    since: datetime,
) -> float | None:
    """Максимальный уход цены от уровня с момента ``since``.

    Считается по закрытиям свечей: ``max(|close - level| / level)``.
    Уровень, от которого цена ни разу не уходила, — это не исторический
    объёмный уровень, а текущая зона торговли.

    Returns:
        Доля ухода (0.2 = 20 %) или None, если после ``since`` свечей нет.
    """
    if frame.empty or level <= 0:
        return None
    closes = frame.loc[frame["ts"] >= since, "close"].astype(float)
    if closes.empty:
        return None
    return float(((closes - level).abs() / level).max())


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

    def check_clusters(
        self,
        current_price: float,
        now: datetime | None = None,
    ) -> POCSignal | None:
        """Сигнал, когда цена вошла в зону ближайшего кластера.

        Мониторится только ближайший к цене кластер. Сигнал выдаётся один
        раз за вход в зону: пока цена не вышла из зоны и не вернулась,
        повторного сигнала нет (состояние переживает рестарт).
        """
        moment = now or utc_now()
        now_iso = to_iso(moment)
        if current_price <= 0:
            return None
        levels: list[LevelState] = []
        skipped: list[str] = []
        for level in self.store.levels(self.symbol):
            if not self._is_tradeable(level):
                continue
            if self.config.min_departure_pct > 0:
                departure = self._level_departure(level)
                if departure is not None and departure < self.config.min_departure_pct:
                    skipped.append(f"{level.poc_price:.6g}({departure * 100:.0f}%)")
                    continue
            levels.append(level)
        if skipped:
            logger.info(
                "%s: уровни без ухода цены на %.0f%% — пропуск: %s",
                self.symbol,
                self.config.min_departure_pct * 100,
                ", ".join(skipped),
            )
        target = nearest_cluster(
            build_clusters(levels, self.config.cluster_merge_pct),
            current_price,
        )
        if target is None:
            return None

        distance = zone_distance(current_price, target.level)
        state = self.store.get_zone_state(self.symbol)
        if distance > self.config.zone_pct:
            if state is not None and state.in_zone:
                self.store.save_zone_state(
                    ZoneState(
                        symbol=self.symbol,
                        level_price=target.level,
                        direction=state.direction,
                        in_zone=False,
                        episode=state.episode,
                        updated_ts=now_iso,
                    )
                )
                logger.info(
                    "%s: цена вышла из зоны кластера %.10g — сигнал снова разрешён",
                    self.symbol,
                    target.level,
                )
            return None

        direction = direction_for(current_price, target.level)
        if direction is None:
            return None
        same_episode = (
            state is not None
            and state.in_zone
            and state.direction == direction
            and target.level > 0
            and abs(state.level_price - target.level) / target.level
            < self.config.cluster_merge_pct
        )
        if same_episode:
            return None

        contact = self._contact_stats(target.level, moment)
        if contact is not None:
            quiet_hours, contact_hours = contact
            if contact_hours > self.config.zone_fresh_hours:
                logger.info(
                    "%s: цена у уровня %.10g уже %.1fч — вход не свежий, сигнала нет",
                    self.symbol,
                    target.level,
                    contact_hours,
                )
                return None
            if quiet_hours < self.config.zone_quiet_hours:
                logger.info(
                    "%s: подход к уровню %.10g был %.1fч назад (< %.0fч) — "
                    "слабый сигнал, пропуск",
                    self.symbol,
                    target.level,
                    quiet_hours,
                    self.config.zone_quiet_hours,
                )
                return None

        episode = (state.episode if state is not None else 0) + 1
        self.store.save_zone_state(
            ZoneState(
                symbol=self.symbol,
                level_price=target.level,
                direction=direction,
                in_zone=True,
                episode=episode,
                updated_ts=now_iso,
            )
        )
        signal = POCSignal(
            ts=now_iso,
            symbol=self.symbol,
            direction=direction,
            entry_price=target.level,
            price=current_price,
            distance_pct=distance,
            zone_pct=self.config.zone_pct,
            signal_id=f"{self.symbol}:{target.level:.10g}:{direction}:{episode}",
            members=list(target.members),
            windows=list(target.windows),
            volume=target.volume,
        )
        payload = signal.to_dict()
        self.store.log_signal(payload)
        _append_jsonl(self.config.signal_log, payload)
        logger.info(
            "СИГНАЛ %s %s entry=%.10g (цена %.10g, %.2f%%) кластер из %d уровней: %s",
            self.symbol,
            direction,
            target.level,
            current_price,
            distance * 100,
            target.size,
            ",".join(target.windows),
        )
        return signal

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
        price_days = max(
            2,
            math.ceil(
                (self.config.zone_quiet_hours + self.config.zone_fresh_hours) / 24
            ),
        )
        try:
            self.load_history(self.config.price_interval, moment, days=price_days)
        except Exception as exc:  # noqa: BLE001 — цена не должна ронять цикл
            logger.warning(
                "цена %s %s недоступна: %s",
                self.symbol,
                self.config.price_interval,
                exc,
            )

        winners = self._span_winners()
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
            span = min(len(frame), spec.candles)
            winner = winners.get((spec.interval, span))
            if winner is None or winner.name != spec.name:
                statuses.append(f"{spec.name}=skip(duplicate_span={span})")
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
        return self.check_clusters(price, moment)

    # --- вспомогательное -------------------------------------------------

    def _last_price(self) -> float:
        """Цена последней закрытой свечи: 5m (п.6), затем 1h и 4h."""
        frame = self._price_frame()
        if frame is None:
            return 0.0
        return float(frame["close"].iloc[-1])

    def _price_frame(self) -> pd.DataFrame | None:
        """Кадр цены для фильтра подхода: 5m, затем 1h и 4h."""
        for interval in (self.config.price_interval, "60", "240"):
            frame = self._series.get(interval)
            if frame is not None and not frame.empty:
                return frame
        return None

    def _contact_stats(
        self,
        level: float,
        moment: datetime,
    ) -> tuple[float, float] | None:
        """Пауза до подхода и время в зоне; None — данных нет, фильтр не проверен."""
        frame = self._price_frame()
        if frame is None:
            logger.warning(
                "%s: нет истории цены — фильтр подхода не проверен", self.symbol
            )
            return None
        return zone_contact_hours(frame, level, self.config.zone_pct, moment)

    def _level_start(self, level: LevelState) -> datetime:
        """Начало окна уровня: дата отрезка или возраст в днях назад."""
        stamp = level.segment_start_ts or level.first_created_ts
        if stamp:
            try:
                return parse_iso(stamp)
            except ValueError:
                pass
        return utc_now() - timedelta(days=self._level_age_days(level))

    def _level_departure(self, level: LevelState) -> float | None:
        """Максимальный уход цены от уровня внутри его окна.

        Берётся самый подробный кадр, покрывающий начало окна: 1h, затем 4h.
        Если ни один кадр не доходит до начала, уход считается по тому, что
        есть (лучшая оценка). ``None`` — истории цены нет, фильтр не проверен.
        """
        since = self._level_start(level)
        best: float | None = None
        for interval in ("60", "240"):
            frame = self._series.get(interval)
            if frame is None or frame.empty:
                continue
            value = departure_pct(frame, level.poc_price, since)
            if value is None:
                continue
            if frame["ts"].iloc[0].to_pydatetime() <= since:
                return value
            best = value
        return best

    def _span_winners(self) -> dict[tuple[str, int], WindowSpec]:
        """По одному окну на каждый фактический охват истории.

        Частичные окна с одинаковым охватом дают одинаковый POC (SOXSUSDT:
        180d/365d/730d на 128 днях) — считаем только самое длинное из них,
        иначе один и тот же оборот суммируется несколько раз.
        """
        winners: dict[tuple[str, int], WindowSpec] = {}
        for spec in self.config.specs:
            if get_poc_age_days(spec.name) < self.config.min_poc_age_days:
                continue
            frame = self._series.get(spec.interval)
            if frame is None or len(frame) < MIN_PARTIAL_CANDLES:
                continue
            key = (spec.interval, min(len(frame), spec.candles))
            current = winners.get(key)
            if current is None or spec.days > current.days:
                winners[key] = spec
        return winners

    def _is_tradeable(self, level: LevelState) -> bool:
        """Уровень участвует в кластерах, если его возраст в допуске."""
        if level.poc_price <= 0:
            return False
        age_days = self._level_age_days(level)
        return self.config.min_poc_age_days <= age_days <= self.config.max_poc_age_days

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

    def _is_wall(self, level: LevelState) -> bool:
        return (
            level.total_volume_at_level >= self.config.wall_volume_threshold
            or len(level.source_windows) >= WALL_MIN_WINDOWS
        )
