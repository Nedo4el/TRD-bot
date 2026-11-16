# Итог сессии — управление монетами роботов в дашборде

**Дата:** 14 ноября 2026, 03:45 МСК  
**Коммиты:** c8fec58 → 52f059b → 84c1835 → 152328a → 3761cbf → c72d65e → 7b7ae34 → c16595a

---

## Что сделано

### 1. Скринер тренда → EMA/ADX (robot_trend parity)
- `bot_screener_trend/scanner.py`, `printer.py`, `main.py` — логика переведена с HH/HL на EMA fast/slow/macro + ADX/DI.
- `dashboard/adapters.py` (`scan_trend`) и `dashboard/config.py` — скринер дашборда теперь использует те же параметры.
- Добавлен `tests/test_screener_trend.py`.

### 2. Инфраструктура дашборда
- `dashboard/fetcher.py` — глобальный темп запросов к Bybit (~8 req/s на процесс) + экспоненциальный backoff на 403/429.
- `dashboard/scheduler.py` + `dashboard/store.py` — сканы только вручную (авто-расписание убрано), `next_run_at = None`.

### 3. Trade Store — очистка истории
- `dashboard/trade_store.py` — метод `clear(robot=None) -> int`.
- `dashboard/server.py` — `POST /api/robots/statistics/clear`.
- `dashboard/static/index.html` — кнопка «Очистить историю» с подтверждением.
- `tests/test_trade_store.py` — тест очистки.

### 4. Управление монетами роботов (основная фича)
**Хранилище:** `dashboard/robot_coins.py`
- JSON `data/dashboard/robot_coins.json` — список монет на робота.
- Сид из `SYMBOL` в `.env` робота (если в JSON нет записи).
- Нормализация тикеров (uppercase, trim, dedup, удаление внутренних пробелов), лимит 20 монет.
- Явный пустой список `[]` не откатывается на сид.

**Процессы:** `dashboard/robots.py`
- На каждую монету — отдельный subprocess с изолированными `STATE_FILE`, `LOG_FILE`, `KILL_SWITCH` (суффикс `_SYMBOL`).
- Реестр `data/{robot}.procs.json` (symbol → pid), legacy `.pid` — первый живой pid.
- Windows-безопасная проверка живости через `OpenProcess` + `GetExitCodeProcess` (не убивает процесс).
- `start_robot` / `stop_robot` / `set_robot_symbols` (live apply: удалённые останавливаются, добавленные запускаются).
- `get_robot_status` возвращает `running`, `symbols`, `symbol_pids`.

**Кэш тикеров:** `dashboard/symbols.py`
- `SymbolsCache` — TTL 60 с, фильтр USDT-M, мин. оборот 1 млн, сортировка по обороту desc.

**API:** `dashboard/server.py`
- `GET /api/bybit/symbols` — список ликвидных пар.
- `GET /api/robots/{name}/symbols` — монеты робота + pids.
- `POST /api/robots/{name}/symbols` — `{"symbols":[]}` set / `{"add":[],"remove":[]}` add/remove, валидация тикеров (alnum, ≤24).
- `GET /api/robots` — расширен `symbols` и `symbol_pids`.
- `POST /api/robots/statistics/clear` — очистка истории сделок.
- `POST /api/robot/start|stop|status` — флаг робота (legacy, совместимость).

**UI:** `dashboard/static/index.html`
- Таблица роботов: колонка «Монеты» (badge preview + счётчик), клик по строке раскрывает чипы с ✕.
- Кнопка «🪙 Монеты» открывает модалку.
- Модалка: поиск, вкладки «Все Bybit / Выбранные / Из сигналов», клик по строке — toggle add/remove.
- Кнопки «▶ Запустить робота» / «⏹ Остановить» / «Закрыть», синхронизация с состоянием робота.
- Закрытие по клику на фон, сброс вкладки на «Все Bybit» при открытии.
- Мобильная ширина (390 px): нет горизонтального скролла, адаптивные `min-width`.

**Тесты:** `tests/test_dashboard.py` (+408 строк)
- `RobotCoins` persist/env seed/empty/limit.
- `_per_symbol_path`, `_symbol_env` изоляция.
- `start_robot`/`stop_robot`/`set_robot_symbols` с моками `Popen`/`is_running`.
- `SymbolsCache` filter/sort/TTL/refresh.
- HTTP endpoints: GET/POST symbols, invalid ticker 400, invalid JSON 400, unknown robot, `/api/bybit/symbols`, `/api/robots` expanded.

---

## Проверки

| Проверка | Результат |
|---|---|
| `pytest -q` | 387 passed (5 новых тестов) |
| `ruff check dashboard tests/test_dashboard.py` | clean |
| `ruff format --check dashboard tests/test_dashboard.py` | clean |
| `mypy dashboard tests/test_dashboard.py` | clean (16 файлов) |
| Живой сервер (порт 8787) | `/api/bybit/symbols` 200, `/api/robots` с `symbols`/`symbol_pids` |
| UI desktop (1440×900) | раскрытие, чипы, модалка, поиск, вкладки, toggle, backdrop close — 0 ошибок консоли |
| UI mobile (390×844) | без горизонтального скролла, вкладки работают |
| API add/remove ETHUSDT на остановленном `robot_trend` | 200, запись в `robot_coins.json` |
| API invalid ticker | 400 `неверные тикеры: BAD TICKER!` |
| API remove ETHUSDT | 200, возврат к `[BTCUSDT]` |

> **Важно:** торговые процессы **не запускались** — проверка старт/стоп шла с перехватом запросов (Playwright route), чтобы не открывать реальные позиции. Старый процесс дашборда (PID 14212, старт 01:25) возвращал 404 на новые маршруты — перезапущен, теперь эндпоинты 200.

---

## Известные ограничения / следующая сессия

- `data/dashboard/robot_coins.json` создаётся при первом `set`; до этого сид из `.env`. После коммита файл содержит только `robot_trend: ["BTCUSDT"]` (остальные роботы резолвятся из `.env`).
- `dashboard/trade_store.py` имеет pre-existing форматирование (одна строка) — не трогал, не в скоупе.
- В репозитории остались незакоммиченные изменения прошлых задач (`dashboard/adapters.py`, `config.py`, `fetcher.py`, `scheduler.py`, `store.py`, `tests/test_trade_store.py`, `bot_screener_trend/*`) — они вошли в коммиты выше.
- `.omo/run-continuation/*.json` — артефакты оркестратора, не закоммичены (остаются в working tree).
- `tests/test_screener_trend.py` создан, но не прогонялся изолированно (входит в общий `pytest`).
- Следующая задача: применить per-robot coins к остальным роботам (`robot_flat`, `robot_yrovni`, `robot_zakol`, `robot_impulse`) по эталону `robot_trend`.

---

## Файлы изменены в этой сессии (коммиты)

```
c8fec58 trend screener: EMA+ADX вместо HH/HL
52f059b dashboard: скринер тренда переведён на EMA/ADX
84c1835 dashboard: глобальный темп запросов Bybit + сканы только вручную
152328a trade store: очистка истории сделок по роботу
3761cbf robots: посимвольный запуск + список монет на робота
c72d65e dashboard API: монеты роботов, список Bybit, очистка статистики
7b7ae34 tests: покрытие монет роботов, API символов и флага робота
c16595a dashboard UI: выбор монет робота, запуск по сигналам, очистка статистики
```

---

*Сохранено автоматически в конце сессии.*
## Обновление 2026-11-15

- Legacy-флаг робота удалён: routes `/api/robot/start|stop|status` и `dashboard/robot_state.py` больше нет.
- Старт из окна «Новые сигналы»: `POST /api/robots/robot_trend/symbols {add:[...]}` -> `POST /api/robots/robot_trend/start` (пока привязан только тренд).
- Бейдж «Робот» и состояние окна читаются из `/api/robots`.
- `RobotCoins.load` читает и UTF-8 (с BOM), и UTF-16 — файл, сохранённый PowerShell, больше не роняет `/api/robots`.
