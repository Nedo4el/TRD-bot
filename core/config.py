"""Загрузка конфигурации бота из переменных окружения (.env).

Каждый бот читает СВОЙ .env из своей папки (bot_xxx/.env).
Если в папке бота .env нет — берётся общий .env из корня проекта.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# Корень проекта — на два уровня выше этого файла (core/ -> корень).
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_bot_env(bot_dir: Path) -> None:
    """Загрузить .env бота, а если его нет — общий .env из корня.

    Вызывается в main.py каждого бота ДО создания Config.
    override=False — уже существующие переменные ОС не перезаписываем.

    Args:
        bot_dir: папка бота (обычно Path(__file__).parent).
    """
    bot_env = bot_dir / ".env"
    if bot_env.exists():
        load_dotenv(bot_env, override=False)
    else:
        load_dotenv(PROJECT_ROOT / ".env", override=False)


def get_env_str(name: str, default: str) -> str:
    """Прочитать строковую переменную окружения."""
    return os.getenv(name, default)


def get_env_bool(name: str, default: bool) -> bool:
    """Прочитать переменную окружения как boolean (true/false/1/0)."""
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def get_env_float(name: str, default: float) -> float:
    """Прочитать переменную окружения как число с плавающей точкой."""
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return float(value)


def get_env_int(name: str, default: int) -> int:
    """Прочитать переменную окружения как целое число."""
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return int(value)


@dataclass
class Config:
    """Общие настройки бота (одинаковые для всех стратегий).

    Параметры конкретной стратегии (периоды индикаторов и т.п.)
    задаются отдельно в main.py бота — тоже из его .env.
    """

    # --- Bybit API ---
    api_key: str = field(default_factory=lambda: os.getenv("BYBIT_API_KEY", ""))
    api_secret: str = field(
        default_factory=lambda: os.getenv("BYBIT_API_SECRET", ""),
    )
    testnet: bool = field(default_factory=lambda: get_env_bool("TESTNET", True))

    # --- Режимы работы ---
    # simulation=True: ордера не отправляются, только логируются.
    simulation_mode: bool = field(
        default_factory=lambda: get_env_bool("SIMULATION_MODE", True),
    )

    # --- Торговые параметры ---
    category: str = field(default_factory=lambda: os.getenv("CATEGORY", "linear"))
    symbol: str = field(default_factory=lambda: os.getenv("SYMBOL", ""))
    timeframe: str = field(default_factory=lambda: os.getenv("TIMEFRAME", "15"))
    poll_interval: int = field(
        default_factory=lambda: get_env_int("POLL_INTERVAL", 60),
    )
    kline_limit: int = field(
        default_factory=lambda: get_env_int("KLINE_LIMIT", 200),
    )
    # Максимум REST-запросов в секунду (защита от блокировки по лимитам API)
    requests_per_second: float = field(
        default_factory=lambda: get_env_float("REQUESTS_PER_SECOND", 10.0),
    )

    # --- Риск-менеджмент ---
    position_pct: float = field(
        default_factory=lambda: get_env_float("POSITION_PCT", 10.0),
    )
    # Фиксированный размер лота. 0 = выключено (считаем от POSITION_PCT).
    fixed_qty: float = field(default_factory=lambda: get_env_float("FIXED_QTY", 0.0))
    stop_loss_pct: float = field(
        default_factory=lambda: get_env_float("STOP_LOSS_PCT", 2.0),
    )
    take_profit_pct: float = field(
        default_factory=lambda: get_env_float("TAKE_PROFIT_PCT", 4.0),
    )

    # --- WebSocket ---
    ws_enabled: bool = field(default_factory=lambda: get_env_bool("WS_ENABLED", True))

    # --- Telegram уведомления (опционально) ---
    telegram_bot_token: str = field(
        default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN", ""),
    )
    telegram_chat_id: str = field(
        default_factory=lambda: os.getenv("TELEGRAM_CHAT_ID", ""),
    )

    # --- Логирование и состояние ---
    log_level: str = field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO"))
    log_file: str = field(default_factory=lambda: os.getenv("LOG_FILE", "logs/bot.log"))
    state_file: str = field(
        default_factory=lambda: os.getenv("STATE_FILE", "data/state.json"),
    )

    # --- Инфраструктура ---
    # Папки для логов и данных (создаются при первом использовании)
    logs_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "logs")
    data_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "data")

    @property
    def state_path(self) -> Path:
        """Абсолютный путь к файлу состояния."""
        return PROJECT_ROOT / self.state_file

    def validate(self) -> None:
        """Проверить обязательные настройки до запуска бота.

        Выбрасывает ValueError с понятным описанием, если чего-то не хватает.
        """
        if not self.api_key or not self.api_secret:
            raise ValueError(
                "BYBIT_API_KEY и BYBIT_API_SECRET обязательны. "
                "Скопируйте .env.example в .env и заполните ключи.",
            )
        if not self.symbol:
            raise ValueError(
                "SYMBOL не задан. Укажите инструмент в .env, например: SYMBOL=ETHUSDT",
            )
        if self.category not in ("spot", "linear"):
            raise ValueError("CATEGORY должен быть 'spot' или 'linear'.")
        if not (0 < self.position_pct <= 100):
            raise ValueError("POSITION_PCT должен быть в диапазоне (0, 100].")
        if self.fixed_qty < 0:
            raise ValueError(
                "FIXED_QTY должен быть >= 0 (0 = считать от POSITION_PCT)."
            )
        if self.stop_loss_pct <= 0 or self.take_profit_pct <= 0:
            raise ValueError("STOP_LOSS_PCT и TAKE_PROFIT_PCT должны быть > 0.")
