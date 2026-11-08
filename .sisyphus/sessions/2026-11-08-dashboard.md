# Сессия 2026-11-08 — дашборд: джиттер, ретраи, расписание, кодировка

Время: 08.11.2026, ~22:30 МСК.

## Что сделали

### 1. Джиттер на старте скринеров
- Файл: `dashboard/scheduler.py`
- `Scheduler.start()`: вместо `delay = index * 1.5` — `random.uniform(0.0, 2.0)` для каждого скринера.
- Распределяет стартовый залп, чтобы Bybit не выдал 403-бан сразу.

### 2. Ретраи 403/429
- Новый файл: `dashboard/fetcher.py` — `DashboardFetcher`.
- При rate-limit (403/429) — экспоненциальный backoff: 1с → 2с → 4с (до 3 попыток).
- `dashboard/adapters.py` переключён: `_new_fetcher()` и `scan_trend` / `scan_zakonomer` используют `DashboardFetcher`, а не fetcher'ы отдельных скринеров.
- Методы: `get_tickers`, `get_klines`, `get_spread`, `get_filtered_symbols`, `get_klines_range`.

### 3. Абсолютное расписание
- `dashboard/scheduler.py`: новый класс `_ScreenerClock`.
- `tick()`: если скан занял больше интервала — пропускает ближайшие слоты и ждёт ближайший.
- `Scheduler._clocks: dict[str, _ScreenerClock]` — по одному на каждый скринер.
- `_next_run()` теперь принимает clock и считает время до ближайшего слота.
- `_wait()` ждёт `clock.tick()` секунд, а не `spec.interval_sec`.

### 4. Кодировка логов (кракозябры)
- `run_dashboard.py`: при `win32` переключает `sys.stdout` / `sys.stderr` на UTF-8 через `reconfigure`.
- `dashboard.cmd`: `chcp 65001` + `set PYTHONIOENCODING=utf-8` перед запуском Python.

### 5. Коммит
- Коммит `86acb25`: `dashboard: джиттер, ретраи 403/429, абсолютное расписание, кодировка логов`.
- Включены: `dashboard/` (все файлы), `run_dashboard.py`, `dashboard.cmd`, `tests/test_dashboard.py`, `requirements.txt`, `bot_screener_klin/scanner.py`, `bot_screener_yrovni/scanner.py`.
- 16 файлов, +2699 строк.

### Тесты
- `tests/test_dashboard.py` — 18 тестов (было 16), все проходят.
- Новые: `test_scheduler_wait_uses_absolute_clock`, `test_fetcher_retries_rate_limit`, `test_fetcher_returns_empty_after_exhaustion`.

## Состояние проекта
- Последний коммит: `86acb25`.
- Рабочее дерево чистое (кроме `.sisyphus/sessions/`).
- ruff / mypy / pytest — без ошибок по `dashboard/`, `tests/test_dashboard.py`, `run_dashboard.py`.

## Полезные команды

```powershell
# запуск дашборда
.\.venv\Scripts\python.exe run_dashboard.py
# или dashboard.cmd

# тесты
uv run pytest tests/test_dashboard.py -q

# проверка API
Invoke-RestMethod http://127.0.0.1:8787/api/signals
```
