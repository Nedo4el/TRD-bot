"""Конфигурация robot_yrovni_D: POC-лимитки, стоп/BE/тейк/трейлинг.

Все параметры читаются из ``.env`` бота. Параметры сигналов (окна, зона,
кластеры) живут в POC-скринере — робот их только потребляет.

Per-symbol override (высокая волатильность альтов)::

    SYMBOL_OVERRIDES={"AKEUSDT": {"stop_initial_pct": 0.15, "level_offset_pct": 0.03}}
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from core.config import load_bot_env

BOT_DIR = Path(__file__).resolve().parent

DEFAULT_ORDER_SPLIT: tuple[float, ...] = (0.33, 0.34, 0.33)


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return float(raw)


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return int(raw)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_split(name: str, default: tuple[float, ...]) -> tuple[float, ...]:
    """Доли трёх лимиток через запятую: ORDER_SPLIT=0.33,0.34,0.33."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    parts = [part.strip() for part in raw.split(",") if part.strip()]
    if not parts:
        return default
    return tuple(float(part) for part in parts)


def _env_overrides(name: str) -> dict[str, dict[str, float]]:
    """JSON per-symbol override: {\"AKEUSDT\": {\"stop_initial_pct\": 0.15}}."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, dict):
        return {}
    result: dict[str, dict[str, float]] = {}
    for symbol, values in payload.items():
        if not isinstance(values, dict):
            continue
        clean: dict[str, float] = {}
        for key, value in values.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            clean[str(key)] = float(value)
        if clean:
            result[str(symbol).upper()] = clean
    return result


class LevelConfig(BaseModel):
    """Параметры стратегии POC-уровней (дефолты — из ТЗ)."""

    model_config = ConfigDict(extra="ignore")

    # --- Bybit ---
    api_key: str = Field(default_factory=lambda: os.getenv("BYBIT_API_KEY", ""))
    api_secret: str = Field(default_factory=lambda: os.getenv("BYBIT_API_SECRET", ""))
    testnet: bool = Field(default_factory=lambda: _env_bool("TESTNET", True))
    category: str = Field(default_factory=lambda: os.getenv("CATEGORY", "linear"))
    dry_run: bool = Field(default_factory=lambda: _env_bool("DRY_RUN", True))

    # --- Депозит и размер ---
    deposit_usd: float = Field(
        default_factory=lambda: _env_float("DEPOSIT_USD", 100.0),
    )
    position_pct: float = Field(
        default_factory=lambda: _env_float("POSITION_PCT", 6.0),
    )
    order_split: tuple[float, ...] = Field(
        default_factory=lambda: _env_split("ORDER_SPLIT", DEFAULT_ORDER_SPLIT),
    )

    # --- Вход ---
    level_offset_pct: float = Field(
        default_factory=lambda: _env_float("LEVEL_OFFSET_PCT", 0.02),
    )
    entry_ttl_sec: float = Field(
        default_factory=lambda: _env_float("ENTRY_TTL_SEC", 43200.0),
    )
    cancel_if_distance_pct: float = Field(
        default_factory=lambda: _env_float("CANCEL_IF_DISTANCE_PCT", 0.05),
    )

    # --- Стоп ---
    stop_initial_pct: float = Field(
        default_factory=lambda: _env_float("STOP_INITIAL_PCT", 0.10),
    )
    be_trigger_pct: float = Field(
        default_factory=lambda: _env_float("BE_TRIGGER_PCT", 0.01),
    )
    be_offset_pct: float = Field(
        default_factory=lambda: _env_float("BE_OFFSET_PCT", 0.0),
    )
    cancel_limits_on_be: bool = Field(
        default_factory=lambda: _env_bool("CANCEL_LIMITS_ON_BE", True),
    )

    # --- Тейк и трейлинг ---
    take_pct: float = Field(default_factory=lambda: _env_float("TAKE_PCT", 0.07))
    trail_pct: float = Field(default_factory=lambda: _env_float("TRAIL_PCT", 0.03))
    trail_activate_pct: float = Field(
        default_factory=lambda: _env_float("TRAIL_ACTIVATE_PCT", 0.07),
    )

    # --- Общие фильтры ---
    min_confidence: float = Field(
        default_factory=lambda: _env_float("MIN_CONFIDENCE", 0.6),
    )
    max_concurrent: int = Field(
        default_factory=lambda: _env_int("MAX_CONCURRENT", 1),
    )
    cooldown_sec: float = Field(
        default_factory=lambda: _env_float("COOLDOWN_SEC", 900.0),
    )
    max_hold_sec: float = Field(
        default_factory=lambda: _env_float("MAX_HOLD_SEC", 86400.0),
    )
    max_atr_pct: float = Field(
        default_factory=lambda: _env_float("MAX_ATR_PCT", 0.02),
    )
    max_distance_from_poc_pct: float = Field(
        default_factory=lambda: _env_float("MAX_DISTANCE_FROM_POC_PCT", 0.03),
    )
    funding_window_sec: float = Field(
        default_factory=lambda: _env_float("FUNDING_WINDOW_SEC", 300.0),
    )
    atr_period: int = Field(default_factory=lambda: _env_int("ATR_PERIOD", 14))
    atr_timeframe: str = Field(
        default_factory=lambda: os.getenv("ATR_TIMEFRAME", "5"),
    )

    # --- Логирование (JSONL) ---
    log_file: str = Field(
        default_factory=lambda: os.getenv("LOG_FILE", "logs/strategy.jsonl"),
    )
    log_rotation_mb: float = Field(
        default_factory=lambda: _env_float("LOG_ROTATION_MB", 50.0),
    )
    log_keep_days: int = Field(
        default_factory=lambda: _env_int("LOG_KEEP_DAYS", 7),
    )
    tick_snapshot_sec: float = Field(
        default_factory=lambda: _env_float("TICK_SNAPSHOT_SEC", 60.0),
    )

    # --- Источники сигналов ---
    # Символ этого процесса (задаёт дашборд при per-symbol запуске).
    # Пусто — процесс один на все монеты: держит HTTP-приёмник и берёт
    # все сигналы дашборда. Непусто — только своя монета, без HTTP-приёмника
    # (поллинг дашборда), чтобы процессы не дрались за SIGNAL_PORT.
    symbol: str = Field(
        default_factory=lambda: os.getenv("SYMBOL", "").strip().upper(),
    )
    signal_host: str = Field(
        default_factory=lambda: os.getenv("SIGNAL_HOST", "127.0.0.1"),
    )
    signal_port: int = Field(default_factory=lambda: _env_int("SIGNAL_PORT", 8789))
    # 0 — поллинг дашборда выключен (работаем только по входящим POST).
    dashboard_url: str = Field(
        default_factory=lambda: os.getenv("DASHBOARD_URL", "http://127.0.0.1:8787"),
    )
    signal_poll_sec: float = Field(
        default_factory=lambda: _env_float("SIGNAL_POLL_SEC", 15.0),
    )
    poll_timeout_sec: float = Field(
        default_factory=lambda: _env_float("POLL_TIMEOUT_SEC", 5.0),
    )

    # --- Самодиагностика при старте ---
    startup_check: bool = Field(
        default_factory=lambda: _env_bool("STARTUP_CHECK", True),
    )
    startup_cleanup: bool = Field(
        default_factory=lambda: _env_bool("STARTUP_CLEANUP", True),
    )
    # Разрешить заново активные сигналы дашборда, до уровня которых цена
    # ещё не дошла: сетку могли снять по TTL, а вход не отработан.
    startup_rearm: bool = Field(
        default_factory=lambda: _env_bool("STARTUP_REARM", True),
    )

    # --- Инфраструктура ---
    tick_sec: float = Field(default_factory=lambda: _env_float("TICK_SEC", 5.0))
    requests_per_second: float = Field(
        default_factory=lambda: _env_float("REQUESTS_PER_SECOND", 10.0),
    )
    kill_switch_file: str = Field(
        default_factory=lambda: os.getenv(
            "KILL_SWITCH",
            "data/robot_yrovni_D.kill",
        ),
    )
    processed_file: str = Field(
        default_factory=lambda: os.getenv(
            "PROCESSED_FILE",
            "data/robot_yrovni_D_processed.json",
        ),
    )
    order_link_prefix: str = Field(
        default_factory=lambda: os.getenv("ORDER_LINK_ID_PREFIX", "zk-"),
    )
    symbol_overrides: dict[str, dict[str, float]] = Field(
        default_factory=lambda: _env_overrides("SYMBOL_OVERRIDES"),
    )
    log_level: str = Field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO"))
    bot_log_file: str = Field(
        default_factory=lambda: os.getenv("BOT_LOG_FILE", "logs/robot_yrovni_D.log"),
    )

    # ------------------------------ проверки ------------------------------

    @field_validator(
        "level_offset_pct",
        "stop_initial_pct",
        "be_trigger_pct",
        "take_pct",
        "trail_pct",
        "trail_activate_pct",
        "cancel_if_distance_pct",
        "max_distance_from_poc_pct",
    )
    @classmethod
    def _fraction_in_range(cls, value: float) -> float:
        if not (0 < value < 1):
            raise ValueError("доля должна быть в интервале (0, 1)")
        return value

    @field_validator("be_offset_pct", "max_atr_pct")
    @classmethod
    def _zero_allowed_fraction(cls, value: float) -> float:
        if not (0 <= value < 1):
            raise ValueError("доля должна быть в [0, 1)")
        return value

    @field_validator("min_confidence")
    @classmethod
    def _confidence_range(cls, value: float) -> float:
        if not (0 <= value <= 1):
            raise ValueError("MIN_CONFIDENCE должен быть в [0, 1]")
        return value

    @field_validator("position_pct")
    @classmethod
    def _position_pct_range(cls, value: float) -> float:
        if not (0 < value <= 100):
            raise ValueError("POSITION_PCT должен быть в (0, 100]")
        return value

    @field_validator("order_split")
    @classmethod
    def _split_sums_to_one(cls, value: tuple[float, ...]) -> tuple[float, ...]:
        if len(value) != 3:
            raise ValueError("ORDER_SPLIT должен содержать ровно 3 доли")
        if any(part <= 0 for part in value):
            raise ValueError("ORDER_SPLIT: каждая доля должна быть > 0")
        if abs(sum(value) - 1.0) > 0.01:
            raise ValueError("ORDER_SPLIT должен в сумме давать 1.0")
        return value

    @field_validator("entry_ttl_sec", "tick_sec", "tick_snapshot_sec")
    @classmethod
    def _positive_seconds(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("значение в секундах должно быть > 0")
        return value

    @field_validator("cooldown_sec", "max_hold_sec", "funding_window_sec")
    @classmethod
    def _non_negative_seconds(cls, value: float) -> float:
        if value < 0:
            raise ValueError("значение в секундах должно быть >= 0")
        return value

    @field_validator("max_concurrent")
    @classmethod
    def _max_concurrent_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("MAX_CONCURRENT должен быть >= 1")
        return value

    @field_validator("log_keep_days")
    @classmethod
    def _keep_days_non_negative(cls, value: int) -> int:
        if value < 0:
            raise ValueError("LOG_KEEP_DAYS должен быть >= 0")
        return value

    @field_validator("log_rotation_mb")
    @classmethod
    def _rotation_positive(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("LOG_ROTATION_MB должен быть > 0")
        return value

    @field_validator("category")
    @classmethod
    def _linear_only(cls, value: str) -> str:
        if value != "linear":
            raise ValueError("CATEGORY должен быть linear (perpetual)")
        return value

    # ------------------------------ хелперы ------------------------------

    def validate_for_live(self) -> None:
        """Проверить обязательные поля перед запуском без DRY_RUN."""
        if not self.api_key or not self.api_secret:
            raise ValueError("BYBIT_API_KEY и BYBIT_API_SECRET обязательны")
        if self.deposit_usd <= 0:
            raise ValueError("DEPOSIT_USD должен быть > 0")

    def for_symbol(self, symbol: str) -> LevelConfig:
        """Конфиг с per-symbol override для символа (без мутации общего)."""
        overrides = self.symbol_overrides.get(symbol.upper())
        if not overrides:
            return self
        known = type(self).model_fields
        update: dict[str, Any] = {
            key: value for key, value in overrides.items() if key in known
        }
        if not update:
            return self
        return self.model_copy(update=update)

    def order_notional(self) -> float:
        """Notional одной лимитки в USDT (депозит * POSITION_PCT / 3)."""
        total = self.deposit_usd * (self.position_pct / 100.0)
        return total / len(self.order_split)


def load_config() -> LevelConfig:
    """Загрузить ``.env`` бота и собрать конфигурацию."""
    load_bot_env(BOT_DIR)
    return LevelConfig()
