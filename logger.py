"""Настройка логирования: вывод в консоль и в файл одновременно."""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

# Стандартный формат строки лога: время | уровень | имя логгера | сообщение
LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"

# Флаги-одноразовики: конфигурируем корневой логгер строго один раз,
# чтобы при перезапусках не плодить дублирующие хендлеры.
_configured = False


def setup_logging(log_file: str | Path = "logs/bot.log", level: str = "INFO") -> None:
    """Настроить корневой логгер (консоль + файл с ротацией).

    Args:
        log_file: путь к файлу лога (создаётся при необходимости).
        level: уровень логирования (DEBUG/INFO/WARNING/ERROR).

    Файл лога автоматически ротируется по достижении 5 МБ,
    хранится до 3 архивных копий — лог не разрастётся бесконечно.
    """
    global _configured
    if _configured:
        return

    # Создаём папку для логов, если её нет
    path = Path(log_file)
    path.parent.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(level.upper())

    # Форматтер, общий для всех хендлеров
    formatter = logging.Formatter(LOG_FORMAT, datefmt="%Y-%m-%d %H:%M:%S")

    # --- Консольный хендлер: видим лог прямо в терминале ---
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    root.addHandler(console)

    # --- Файловый хендлер: пишем в файл с ротацией ---
    file_handler = RotatingFileHandler(
        path,
        maxBytes=5 * 1024 * 1024,  # 5 МБ
        backupCount=3,  # до 3 архивов: bot.log.1, bot.log.2, bot.log.3
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    _configured = True


def get_logger(name: str) -> logging.Logger:
    """Получить логгер для конкретного модуля.

    Args:
        name: имя модуля (обычно __name__).

    Returns:
        Логгер с иерархическим именем, наследуется от корневого.
    """
    return logging.getLogger(name)
