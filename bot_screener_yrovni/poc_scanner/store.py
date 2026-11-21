"""SQLite-хранилище POC-уровней, кэша свечей и журнала сигналов."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bot_screener_yrovni.poc_scanner.config import parse_window_days
from bot_screener_yrovni.poc_scanner.timeutil import utc_now_iso

SCHEMA_VERSION = 1

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
    first_created_ts TEXT,
    last_calculated_ts TEXT,
    last_touch_ts TEXT,
    touch_count INTEGER DEFAULT 0,
    confirmed BOOLEAN DEFAULT 0,
    va_window TEXT DEFAULT '',
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
"""


@dataclass
class LevelState:
    """POC-уровень в хранилище.

    ``total_volume_at_level`` — сумма объёмов по окнам из ``window_volumes``,
    а не накопительный счётчик: повторный расчёт того же окна заменяет его
    объём, поэтому объём не растёт от каждого скана.
    """

    symbol: str
    poc_price: float
    vah: float = 0.0
    val: float = 0.0
    total_volume_at_level: float = 0.0
    source_windows: tuple[str, ...] = ()
    window_volumes: dict[str, float] = field(default_factory=dict)
    first_created_ts: str = ""
    last_calculated_ts: str = ""
    last_touch_ts: str | None = None
    touch_count: int = 0
    confirmed: bool = False
    va_window: str = ""
    id: int | None = None


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
            self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            self._conn.commit()
            return
        self._ensure_columns()

    def _ensure_columns(self) -> None:
        """Добавить колонки, появившиеся после первой версии схемы."""
        columns = {
            str(row["name"])
            for row in self._conn.execute("PRAGMA table_info(poc_levels)")
        }
        for name, ddl in (
            ("window_volumes", "TEXT"),
            ("va_window", "TEXT DEFAULT ''"),
        ):
            if name not in columns:
                self._conn.execute(f"ALTER TABLE poc_levels ADD COLUMN {name} {ddl}")
        self._conn.commit()

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
            return self._insert(candidate, confirm_threshold)
        return self._merge(target, candidate, confirm_threshold)

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
                source_windows, window_volumes, first_created_ts,
                last_calculated_ts, last_touch_ts, touch_count, confirmed,
                va_window
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                candidate.symbol,
                float(candidate.poc_price),
                float(candidate.vah),
                float(candidate.val),
                total,
                json.dumps(list(windows)),
                json.dumps(volumes, sort_keys=True),
                created,
                now,
                candidate.last_touch_ts,
                int(candidate.touch_count),
                int(total >= confirm_threshold),
                candidate.va_window,
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
            first_created_ts=created,
            last_calculated_ts=now,
            last_touch_ts=candidate.last_touch_ts,
            touch_count=int(candidate.touch_count),
            confirmed=total >= confirm_threshold,
            va_window=candidate.va_window,
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

        now = candidate.last_calculated_ts or utc_now_iso()
        confirmed = total >= confirm_threshold
        self._conn.execute(
            """
            UPDATE poc_levels SET
                vah = ?, val = ?, total_volume_at_level = ?,
                source_windows = ?, window_volumes = ?,
                last_calculated_ts = ?, confirmed = ?, va_window = ?
            WHERE id = ?
            """,
            (
                float(vah),
                float(val),
                total,
                json.dumps(list(windows)),
                json.dumps(volumes, sort_keys=True),
                now,
                int(confirmed),
                va_window,
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
            first_created_ts=target.first_created_ts,
            last_calculated_ts=now,
            last_touch_ts=target.last_touch_ts,
            touch_count=target.touch_count,
            confirmed=confirmed,
            va_window=va_window,
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
        try:
            return parse_window_days(name)
        except ValueError:
            return 0

    @classmethod
    def _sorted_windows(cls, windows: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted(windows, key=cls._window_days))

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
            first_created_ts=str(row["first_created_ts"] or ""),
            last_calculated_ts=str(row["last_calculated_ts"] or ""),
            last_touch_ts=(str(row["last_touch_ts"]) if row["last_touch_ts"] else None),
            touch_count=int(row["touch_count"] or 0),
            confirmed=bool(row["confirmed"]),
            va_window=str(row["va_window"] or ""),
            id=int(row["id"]),
        )
