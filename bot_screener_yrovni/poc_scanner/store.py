"""SQLite-хранилище POC-уровней, кэша свечей и журнала сигналов."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from bot_screener_yrovni.poc_scanner.config import get_poc_age_days
from bot_screener_yrovni.poc_scanner.timeutil import parse_iso, utc_now, utc_now_iso

SCHEMA_VERSION = 4

_SCHEMA = """
CREATE TABLE IF NOT EXISTS poc_levels (
    id INTEGER PRIMARY KEY,
    symbol TEXT NOT NULL,
    poc_price REAL NOT NULL,
    vah REAL,
    val REAL,
    total_volume_at_level REAL DEFAULT 0,
    source_windows TEXT,
    window_volumes TEXT,
    poc_age_days INTEGER DEFAULT 0,
    first_created_ts TEXT,
    last_calculated_ts TEXT,
    last_touch_ts TEXT,
    touch_count INTEGER DEFAULT 0,
    confirmed BOOLEAN DEFAULT 0,
    va_window TEXT DEFAULT '',
    segment_type TEXT DEFAULT 'rolling',
    segment_start_ts TEXT,
    segment_end_ts TEXT,
    partial_window BOOLEAN DEFAULT 0,
    UNIQUE(symbol, poc_price)
);

CREATE TABLE IF NOT EXISTS poc_signals_log (
    id INTEGER PRIMARY KEY,
    ts TEXT,
    symbol TEXT,
    poc_price REAL,
    confidence REAL,
    touch_count INTEGER,
    action TEXT,
    raw_json TEXT
);

CREATE TABLE IF NOT EXISTS klines (
    symbol TEXT NOT NULL,
    interval TEXT NOT NULL,
    open_time INTEGER NOT NULL,
    open REAL,
    high REAL,
    low REAL,
    close REAL,
    volume REAL,
    turnover REAL,
    PRIMARY KEY (symbol, interval, open_time)
);

CREATE TABLE IF NOT EXISTS zone_state (
    symbol TEXT PRIMARY KEY,
    level_price REAL NOT NULL DEFAULT 0,
    direction TEXT NOT NULL DEFAULT '',
    in_zone INTEGER NOT NULL DEFAULT 0,
    episode INTEGER NOT NULL DEFAULT 0,
    updated_ts TEXT NOT NULL DEFAULT ''
);
"""


@dataclass
class LevelState:
    """POC-уровень в хранилище.

    ``total_volume_at_level`` — сумма объёмов по окнам из ``window_volumes``,
    а не накопительный счётчик: повторный расчёт того же окна заменяет его
    объём, поэтому объём не растёт от каждого скана.

    ``poc_age_days`` — возраст уровня: длина самого зрелого окна для
    ``segment_type='rolling'`` или давность конца отрезка для
    ``segment_type='calendar_month'``; ``first_created_ts`` — начало самого
    старого окна, по которому уровень посчитан сейчас. Оба поля справочные:
    фильтр возраста смотрит на ``poc_age_days``.
    """

    symbol: str
    poc_price: float
    vah: float = 0.0
    val: float = 0.0
    total_volume_at_level: float = 0.0
    source_windows: tuple[str, ...] = ()
    window_volumes: dict[str, float] = field(default_factory=dict)
    poc_age_days: int = 0
    first_created_ts: str = ""
    last_calculated_ts: str = ""
    last_touch_ts: str | None = None
    touch_count: int = 0
    confirmed: bool = False
    va_window: str = ""
    segment_type: str = "rolling"
    segment_start_ts: str = ""
    segment_end_ts: str = ""
    partial_window: bool = False
    id: int | None = None


@dataclass(frozen=True, slots=True)
class ZoneState:
    """Состояние зоны ближайшего кластера: эпизод входа и направление.

    Нужно, чтобы сигнал выдавался один раз за вход в зону (в том числе
    после рестарта скринера): ``in_zone`` снимается, когда цена выходит
    из зоны, а ``episode`` растёт на каждом новом входе.
    """

    symbol: str
    level_price: float = 0.0
    direction: str = ""
    in_zone: bool = False
    episode: int = 0
    updated_ts: str = ""


class POCStore:
    """Хранилище уровней и кэша свечей в одном SQLite-файле."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self.migrate()

    def close(self) -> None:
        """Закрыть соединение."""
        self._conn.close()

    def migrate(self) -> None:
        """Создать схему или дополнить её до текущей версии."""
        version = int(self._conn.execute("PRAGMA user_version").fetchone()[0])
        if version == 0:
            self._conn.executescript(_SCHEMA)
        self._ensure_columns()
        self._ensure_tables()
        if version < 2:
            self._backfill_age_days()
        if version < 3:
            self._backfill_segments()
        if version < SCHEMA_VERSION:
            self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self._conn.commit()

    def _ensure_columns(self) -> None:
        """Добавить колонки, появившиеся после первой версии схемы."""
        columns = {
            str(row["name"])
            for row in self._conn.execute("PRAGMA table_info(poc_levels)")
        }
        for name, ddl in (
            ("window_volumes", "TEXT"),
            ("va_window", "TEXT DEFAULT ''"),
            ("poc_age_days", "INTEGER DEFAULT 0"),
            ("segment_type", "TEXT DEFAULT 'rolling'"),
            ("segment_start_ts", "TEXT"),
            ("segment_end_ts", "TEXT"),
            ("partial_window", "BOOLEAN DEFAULT 0"),
        ):
            if name not in columns:
                self._conn.execute(f"ALTER TABLE poc_levels ADD COLUMN {name} {ddl}")
        self._conn.commit()

    def _ensure_tables(self) -> None:
        """Создать таблицы, появившиеся после первой версии схемы."""
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS zone_state (
                symbol TEXT PRIMARY KEY,
                level_price REAL NOT NULL DEFAULT 0,
                direction TEXT NOT NULL DEFAULT '',
                in_zone INTEGER NOT NULL DEFAULT 0,
                episode INTEGER NOT NULL DEFAULT 0,
                updated_ts TEXT NOT NULL DEFAULT ''
            )
            """
        )
        self._conn.commit()

    def _backfill_segments(self) -> None:
        """Проставить сегментные поля старым записям: они все rolling."""
        self._conn.execute(
            "UPDATE poc_levels SET segment_type = 'rolling' "
            "WHERE segment_type IS NULL OR segment_type = ''"
        )
        self._conn.execute(
            "UPDATE poc_levels SET segment_start_ts = first_created_ts "
            "WHERE segment_start_ts IS NULL OR segment_start_ts = ''"
        )
        self._conn.execute(
            "UPDATE poc_levels SET segment_end_ts = last_calculated_ts "
            "WHERE segment_end_ts IS NULL OR segment_end_ts = ''"
        )
        self._conn.execute(
            "UPDATE poc_levels SET partial_window = 0 WHERE partial_window IS NULL"
        )

    def _backfill_age_days(self) -> None:
        """Заполнить ``poc_age_days`` старых записей по их ``source_windows``."""
        rows = self._conn.execute(
            "SELECT id, source_windows FROM poc_levels"
        ).fetchall()
        for row in rows:
            windows = json.loads(row["source_windows"] or "[]")
            age = max((get_poc_age_days(str(name)) for name in windows), default=0)
            self._conn.execute(
                "UPDATE poc_levels SET poc_age_days = ? WHERE id = ?",
                (age, int(row["id"])),
            )

    # --- уровни ---------------------------------------------------------

    def levels(self, symbol: str) -> list[LevelState]:
        """Все уровни символа по возрастанию цены."""
        rows = self._conn.execute(
            "SELECT * FROM poc_levels WHERE symbol = ? ORDER BY poc_price",
            (symbol,),
        ).fetchall()
        return [self._row_to_level(row) for row in rows]

    def upsert_level(
        self,
        candidate: LevelState,
        merge_pct: float,
        confirm_threshold: float,
    ) -> LevelState:
        """Создать уровень или слить его с ближайшим в пределах ``merge_pct``.

        Слияние считается по относительному расстоянию цен: 0.01 = 1 %.
        Уровень-якорь сохраняет свою цену, объёмы окон суммируются,
        ``confirmed`` пересчитывается по порогу.
        """
        target: LevelState | None = None
        best = merge_pct
        for level in self.levels(candidate.symbol):
            if level.poc_price <= 0:
                continue
            relative = abs(candidate.poc_price - level.poc_price) / level.poc_price
            if relative <= best:
                best = relative
                target = level
        if target is None:
            level = self._insert(candidate, confirm_threshold)
        else:
            level = self._merge(target, candidate, confirm_threshold)
        self._detach_windows(
            candidate.symbol,
            level.id,
            candidate.source_windows,
            confirm_threshold,
        )
        return level

    def detach_window(
        self,
        symbol: str,
        label: str,
        confirm_threshold: float,
    ) -> int:
        """Снять метку окна со всех уровней; вернуть число затронутых записей."""
        return self._detach_windows(symbol, None, (label,), confirm_threshold)

    def _detach_windows(
        self,
        symbol: str,
        keep_id: int | None,
        labels: tuple[str, ...],
        confirm_threshold: float,
    ) -> int:
        """Снять метки окон с чужих уровней: метка живёт на одном уровне.

        Слияние уровней копирует метку на новый уровень, но со старого её
        никто не убирал — в БД оставались дубли окон (``7d`` на трёх уровнях,
        объём считался дважды). Уровень, у которого не осталось меток,
        удаляется: без окна он больше не существует.
        """
        moved = set(labels)
        if not moved:
            return 0
        touched = 0
        for level in self.levels(symbol):
            if level.id == keep_id or not moved.intersection(level.source_windows):
                continue
            remaining = tuple(
                name for name in level.source_windows if name not in moved
            )
            touched += 1
            if not remaining:
                self._conn.execute("DELETE FROM poc_levels WHERE id = ?", (level.id,))
                continue
            volumes = {
                name: volume
                for name, volume in level.window_volumes.items()
                if name in remaining
            }
            total = float(sum(volumes.values()))
            self._conn.execute(
                """
                UPDATE poc_levels SET
                    source_windows = ?, window_volumes = ?,
                    total_volume_at_level = ?, poc_age_days = ?,
                    va_window = ?, confirmed = ?
                WHERE id = ?
                """,
                (
                    json.dumps(list(self._sorted_windows(remaining))),
                    json.dumps(volumes, sort_keys=True),
                    total,
                    self._age_after_detach(level, remaining),
                    level.va_window if level.va_window in remaining else "",
                    int(total >= confirm_threshold),
                    level.id,
                ),
            )
        self._conn.commit()
        return touched

    @classmethod
    def _age_after_detach(cls, level: LevelState, remaining: tuple[str, ...]) -> int:
        """Возраст после снятия меток: максимум по оставшимся rolling-окнам."""
        rolling = [cls._window_days(name) for name in remaining]
        rolling = [days for days in rolling if days > 0]
        if rolling:
            return max(rolling)
        if level.segment_end_ts:
            try:
                ended = parse_iso(level.segment_end_ts)
            except ValueError:
                return int(level.poc_age_days)
            return max(0, (utc_now() - ended).days)
        return int(level.poc_age_days)

    def _insert(
        self,
        candidate: LevelState,
        confirm_threshold: float,
    ) -> LevelState:
        now = candidate.last_calculated_ts or utc_now_iso()
        created = candidate.first_created_ts or now
        volumes = dict(candidate.window_volumes)
        total = float(sum(volumes.values()))
        windows = self._sorted_windows(candidate.source_windows)
        cursor = self._conn.execute(
            """
            INSERT INTO poc_levels (
                symbol, poc_price, vah, val, total_volume_at_level,
                source_windows, window_volumes, poc_age_days, first_created_ts,
                last_calculated_ts, last_touch_ts, touch_count, confirmed,
                va_window, segment_type, segment_start_ts, segment_end_ts,
                partial_window
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                candidate.symbol,
                float(candidate.poc_price),
                float(candidate.vah),
                float(candidate.val),
                total,
                json.dumps(list(windows)),
                json.dumps(volumes, sort_keys=True),
                int(candidate.poc_age_days),
                created,
                now,
                candidate.last_touch_ts,
                int(candidate.touch_count),
                int(total >= confirm_threshold),
                candidate.va_window,
                candidate.segment_type or "rolling",
                candidate.segment_start_ts,
                candidate.segment_end_ts,
                int(candidate.partial_window),
            ),
        )
        self._conn.commit()
        return LevelState(
            symbol=candidate.symbol,
            poc_price=float(candidate.poc_price),
            vah=float(candidate.vah),
            val=float(candidate.val),
            total_volume_at_level=total,
            source_windows=windows,
            window_volumes=volumes,
            poc_age_days=int(candidate.poc_age_days),
            first_created_ts=created,
            last_calculated_ts=now,
            last_touch_ts=candidate.last_touch_ts,
            touch_count=int(candidate.touch_count),
            confirmed=total >= confirm_threshold,
            va_window=candidate.va_window,
            segment_type=candidate.segment_type or "rolling",
            segment_start_ts=candidate.segment_start_ts,
            segment_end_ts=candidate.segment_end_ts,
            partial_window=bool(candidate.partial_window),
            id=int(cursor.lastrowid or 0),
        )

    def _merge(
        self,
        target: LevelState,
        candidate: LevelState,
        confirm_threshold: float,
    ) -> LevelState:
        volumes = dict(target.window_volumes)
        volumes.update(candidate.window_volumes)
        total = float(sum(volumes.values()))
        windows = self._sorted_windows(
            tuple(set(target.source_windows) | set(candidate.source_windows))
        )

        vah, val, va_window = target.vah, target.val, target.va_window
        if self._window_days(candidate.va_window) > self._window_days(target.va_window):
            vah, val, va_window = candidate.vah, candidate.val, candidate.va_window

        age_days = max(int(target.poc_age_days), int(candidate.poc_age_days))
        created = self._oldest_ts(target.first_created_ts, candidate.first_created_ts)
        segment_type = (
            "rolling"
            if "rolling" in (target.segment_type, candidate.segment_type)
            else target.segment_type or "rolling"
        )
        segment_start = self._oldest_ts(
            target.segment_start_ts, candidate.segment_start_ts
        )
        segment_end = self._newest_ts(target.segment_end_ts, candidate.segment_end_ts)
        partial = bool(target.partial_window or candidate.partial_window)

        now = candidate.last_calculated_ts or utc_now_iso()
        confirmed = total >= confirm_threshold
        self._conn.execute(
            """
            UPDATE poc_levels SET
                vah = ?, val = ?, total_volume_at_level = ?,
                source_windows = ?, window_volumes = ?,
                poc_age_days = ?, first_created_ts = ?,
                last_calculated_ts = ?, confirmed = ?, va_window = ?,
                segment_type = ?, segment_start_ts = ?, segment_end_ts = ?,
                partial_window = ?
            WHERE id = ?
            """,
            (
                float(vah),
                float(val),
                total,
                json.dumps(list(windows)),
                json.dumps(volumes, sort_keys=True),
                age_days,
                created,
                now,
                int(confirmed),
                va_window,
                segment_type,
                segment_start,
                segment_end,
                int(partial),
                target.id,
            ),
        )
        self._conn.commit()
        return LevelState(
            symbol=target.symbol,
            poc_price=target.poc_price,
            vah=float(vah),
            val=float(val),
            total_volume_at_level=total,
            source_windows=windows,
            window_volumes=volumes,
            poc_age_days=age_days,
            first_created_ts=created,
            last_calculated_ts=now,
            last_touch_ts=target.last_touch_ts,
            touch_count=target.touch_count,
            confirmed=confirmed,
            va_window=va_window,
            segment_type=segment_type,
            segment_start_ts=segment_start,
            segment_end_ts=segment_end,
            partial_window=partial,
            id=target.id,
        )

    def record_touch(self, level_id: int, ts: str) -> None:
        """Увеличить счётчик касаний уровня."""
        self._conn.execute(
            """
            UPDATE poc_levels
            SET touch_count = touch_count + 1, last_touch_ts = ?
            WHERE id = ?
            """,
            (ts, level_id),
        )
        self._conn.commit()

    def prune_stale(
        self,
        symbol: str,
        keep_windows: set[str],
        older_than_sec: int = 3600,
    ) -> int:
        """Удалить rolling-уровни, чьи окна больше не считаются.

        Уровень удаляется, если все его источники — имена rolling-окон
        (``<дни>d``), ни одно не входит в ``keep_windows`` и запись не
        обновлялась дольше ``older_than_sec``. Уровни исторических отрезков
        (``2026-09``) не трогаются.
        """
        moment = utc_now()
        removed = 0
        for level in self.levels(symbol):
            sources = list(level.source_windows)
            if not sources:
                continue
            rolling = [name for name in sources if self._window_days(name) > 0]
            if len(rolling) != len(sources):
                continue
            if any(name in keep_windows for name in rolling):
                continue
            if not self._is_stale(level.last_calculated_ts, moment, older_than_sec):
                continue
            self._conn.execute("DELETE FROM poc_levels WHERE id = ?", (level.id,))
            removed += 1
        if removed:
            self._conn.commit()
        return removed

    # --- состояние зоны --------------------------------------------------

    def get_zone_state(self, symbol: str) -> ZoneState | None:
        """Состояние зоны символа или None, если сканов ещё не было."""
        row = self._conn.execute(
            "SELECT * FROM zone_state WHERE symbol = ?",
            (symbol,),
        ).fetchone()
        if row is None:
            return None
        return ZoneState(
            symbol=str(row["symbol"]),
            level_price=float(row["level_price"]),
            direction=str(row["direction"]),
            in_zone=bool(row["in_zone"]),
            episode=int(row["episode"]),
            updated_ts=str(row["updated_ts"] or ""),
        )

    def save_zone_state(self, state: ZoneState) -> None:
        """Записать состояние зоны (одна строка на символ)."""
        self._conn.execute(
            """
            INSERT INTO zone_state (
                symbol, level_price, direction, in_zone, episode, updated_ts
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET
                level_price = excluded.level_price,
                direction = excluded.direction,
                in_zone = excluded.in_zone,
                episode = excluded.episode,
                updated_ts = excluded.updated_ts
            """,
            (
                state.symbol,
                float(state.level_price),
                state.direction,
                int(state.in_zone),
                int(state.episode),
                state.updated_ts,
            ),
        )
        self._conn.commit()

    # --- журнал сигналов -------------------------------------------------

    def log_signal(self, payload: dict[str, Any]) -> None:
        """Записать сигнал в журнал (``poc_signals_log``)."""
        self._conn.execute(
            """
            INSERT INTO poc_signals_log (
                ts, symbol, poc_price, confidence, touch_count, action, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(payload.get("ts", "")),
                str(payload.get("symbol", "")),
                float(payload.get("poc_price", 0.0)),
                float(payload.get("confidence", 0.0)),
                int(payload.get("touch_count", 0)),
                str(payload.get("action", "")),
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
            ),
        )
        self._conn.commit()

    def last_signal_ts(self, symbol: str, poc_price: float) -> str | None:
        """Время последнего сигнала по уровню или None."""
        row = self._conn.execute(
            """
            SELECT ts FROM poc_signals_log
            WHERE symbol = ? AND ABS(poc_price - ?) < 1e-9
            ORDER BY id DESC LIMIT 1
            """,
            (symbol, float(poc_price)),
        ).fetchone()
        return str(row["ts"]) if row is not None else None

    # --- кэш свечей ------------------------------------------------------

    def save_klines(
        self,
        symbol: str,
        interval: str,
        rows: list[dict[str, Any]],
    ) -> None:
        """Сохранить свечи в кэш (повторная запись перезаписывает)."""
        if not rows:
            return
        self._conn.executemany(
            """
            INSERT OR REPLACE INTO klines (
                symbol, interval, open_time,
                open, high, low, close, volume, turnover
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    symbol,
                    interval,
                    int(row["open_time"]),
                    float(row["open"]),
                    float(row["high"]),
                    float(row["low"]),
                    float(row["close"]),
                    float(row["volume"]),
                    float(row.get("turnover", 0.0)),
                )
                for row in rows
            ],
        )
        self._conn.commit()

    def load_klines(
        self,
        symbol: str,
        interval: str,
        start_ms: int,
        end_ms: int,
    ) -> list[dict[str, Any]]:
        """Свечи символа за диапазон [start_ms, end_ms] по возрастанию времени."""
        rows = self._conn.execute(
            """
            SELECT open_time, open, high, low, close, volume, turnover
            FROM klines
            WHERE symbol = ? AND interval = ? AND open_time BETWEEN ? AND ?
            ORDER BY open_time
            """,
            (symbol, interval, int(start_ms), int(end_ms)),
        ).fetchall()
        return [dict(row) for row in rows]

    def open_time_bounds(
        self,
        symbol: str,
        interval: str,
    ) -> tuple[int | None, int | None]:
        """(самая старая, самая новая) свеча в кэше."""
        row = self._conn.execute(
            """
            SELECT MIN(open_time) AS first_ts, MAX(open_time) AS last_ts
            FROM klines WHERE symbol = ? AND interval = ?
            """,
            (symbol, interval),
        ).fetchone()
        if row is None or row["first_ts"] is None:
            return None, None
        return int(row["first_ts"]), int(row["last_ts"])

    # --- служебное -------------------------------------------------------

    @staticmethod
    def _window_days(name: str) -> int:
        if not name:
            return 0
        return get_poc_age_days(name)

    @classmethod
    def _sorted_windows(cls, windows: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted(windows, key=cls._sort_key))

    @staticmethod
    def _sort_key(name: str) -> tuple[int, int, str]:
        days = POCStore._window_days(name)
        return (0, days, name) if days > 0 else (1, 0, name)

    @staticmethod
    def _oldest_ts(first: str, second: str) -> str:
        """Более ранняя из двух ISO-дат; пустая строка означает «нет даты»."""
        stamps = [text for text in (first, second) if text]
        return min(stamps) if stamps else ""

    @staticmethod
    def _newest_ts(first: str, second: str) -> str:
        """Более поздняя из двух ISO-дат; пустая строка означает «нет даты»."""
        stamps = [text for text in (first, second) if text]
        return max(stamps) if stamps else ""

    @staticmethod
    def _is_stale(ts: str, now: datetime, older_than_sec: int) -> bool:
        """Запись не обновлялась дольше порога (пустая дата — устарела)."""
        if not ts:
            return True
        try:
            stamp = parse_iso(ts)
        except ValueError:
            return True
        return (now - stamp).total_seconds() >= older_than_sec

    @staticmethod
    def _row_to_level(row: sqlite3.Row) -> LevelState:
        windows = json.loads(row["source_windows"] or "[]")
        volumes = json.loads(row["window_volumes"] or "{}")
        return LevelState(
            symbol=str(row["symbol"]),
            poc_price=float(row["poc_price"]),
            vah=float(row["vah"] or 0.0),
            val=float(row["val"] or 0.0),
            total_volume_at_level=float(row["total_volume_at_level"] or 0.0),
            source_windows=tuple(str(name) for name in windows),
            window_volumes={str(k): float(v) for k, v in volumes.items()},
            poc_age_days=int(row["poc_age_days"] or 0),
            first_created_ts=str(row["first_created_ts"] or ""),
            last_calculated_ts=str(row["last_calculated_ts"] or ""),
            last_touch_ts=(str(row["last_touch_ts"]) if row["last_touch_ts"] else None),
            touch_count=int(row["touch_count"] or 0),
            confirmed=bool(row["confirmed"]),
            va_window=str(row["va_window"] or ""),
            segment_type=str(row["segment_type"] or "rolling"),
            segment_start_ts=str(row["segment_start_ts"] or ""),
            segment_end_ts=str(row["segment_end_ts"] or ""),
            partial_window=bool(row["partial_window"]),
            id=int(row["id"]),
        )
