"""Журнал работ: что сделано и на чём остановились.

Один файл на день — ``docs/worklog/YYYY-MM-DD.md``, время в МСК.
Записи старше ``RETENTION_DAYS`` дней удаляются при каждой записи,
чтобы журнал не занимал место.

Использование::

    python tools/worklog.py "robot_coins: фолбэк utf-16, вкладка Роботы живая"
    python tools/worklog.py --show
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WORKLOG_DIR = PROJECT_ROOT / "docs" / "worklog"

# Полгода: видно, что делали в прошлом квартале, и это считанные килобайты.
RETENTION_DAYS = 180
MSK = timezone(timedelta(hours=3))


def entry_file(day: datetime) -> Path:
    """Файл журнала за конкретный день."""
    return WORKLOG_DIR / f"{day:%Y-%m-%d}.md"


def prune(now: datetime) -> list[Path]:
    """Удалить файлы журнала старше RETENTION_DAYS дней."""
    cutoff = (now - timedelta(days=RETENTION_DAYS)).date()
    removed: list[Path] = []
    if not WORKLOG_DIR.exists():
        return removed
    for path in WORKLOG_DIR.glob("*.md"):
        try:
            day = date.fromisoformat(path.stem)
        except ValueError:
            continue
        if day < cutoff:
            path.unlink(missing_ok=True)
            removed.append(path)
    return removed


def add(text: str, now: datetime | None = None) -> Path:
    """Дописать запись в журнал и почистить устаревшие файлы."""
    current = now or datetime.now(MSK)
    WORKLOG_DIR.mkdir(parents=True, exist_ok=True)
    path = entry_file(current)
    header = "" if path.exists() else f"# Журнал работ — {current:%Y-%m-%d}\n\n"
    line = f"- **{current:%H:%M} МСК** — {text.strip()}\n"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(header + line)
    prune(current)
    return path


def main(argv: list[str]) -> int:
    """CLI: записать строку или показать сегодняшний журнал."""
    if argv and argv[0] == "--show":
        path = entry_file(datetime.now(MSK))
        print(
            path.read_text(encoding="utf-8") if path.exists() else "сегодня записей нет"
        )
        return 0
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    print(f"записано: {add(' '.join(argv))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
