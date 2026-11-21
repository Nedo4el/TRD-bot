# План: скринер «Уровни» (POC) + новый робот robot_yrovni

Статус: **черновик на утверждение**. Код не менялся — это документ, чтобы
продолжить работу другой моделью/сессией.

Дата: 2026-11-21. Автор: сессия планирования.

---

## 1. Что просил пользователь (дословно по смыслу)

1. Скринер «уровни»: **убрать окна 12ч и 24ч**, **добавить 3д, 5д, 10д** к уже
   существующим окнам.
2. Если уровни стоят близко друг к другу (**3–5 %**), приоритет у уровня,
   который **дольше наторговывался** (более длинное окно) — ордера будут там.
   Уровень = **POC по объёму за период**, а не экстремум.
3. Скринер **даёт команду роботу** уровни, если включён **автоматический
   режим** (если такого режима нет — добавить): робот выставляет **лимитные**
   ордера на покупку, если цена идёт сверху к уровню, и на продажу, если цена
   подходит снизу.
4. Скелет робота — **из `robot_trend`**: всё, что нужно для нормальной работы и
   связи с биржей (клиент, WS, состояние, recover, гейты, main).
5. Тейки/стопы/размеры ордеров — **потом**.

## 2. Что уже изучено (не перепроверять)

- `bot_screener_yrovni/scanner.py`
  - `WINDOWS: dict[str, tuple[str, int]]` — окна: `12h, 24h, 3d, 7d, 14d, 1m,
    3m, 6m, 1y` (интервал Bybit, число свечей). `3d` уже есть.
  - `interval_limits()` — сколько свечей качать на интервал (сейчас
    `60: 72`, `240: 180`, `D: 365`).
  - `windows_from_series()`, `build_volume_profile()`, `find_poc()`,
    `scan_symbol()`, `Level(kind, period, price, distance_pct, volume)`.
  - `EXTREME_WINDOWS = ("1m","3m","6m","1y")` → HIGH/LOW; ATH/ATL отдельно.
  - Уровни фильтруются по `proximity_pct` (10 % от текущей цены) и
    сортируются по модулю расстояния. **Схлопывания близких уровней нет.**
- Скринер запускается из двух мест:
  - `bot_screener_yrovni/main.py` (CLI, цикл `SCAN_INTERVAL=600`);
  - `dashboard/adapters.py: scan_yrovni()` (вкладка дашборда, интервал 300 с,
    `dashboard/config.py: ScreenerSpec("yrovni", ...)`).
  Оба используют один `scan_symbol()` → правки окон попадают в оба.
- `tests/test_screener_yrovni.py` жёстко фиксирует текущий список окон
  (`test_windows_cover_requested_periods`) и лимиты — **тест придётся править**.
- Робот уровней сейчас — пустышка: `robot_yrovni_D/strategy.py` возвращает
  `hold` (TODO), работает через старый `core.engine`. `.env` с `TESTNET=true`.
- Скелет `robot_trend/`: `config.py` (pydantic, `validate_for_live`),
  `state.py` (`TrendState`/`StateStore`, `check_symbol`, SCHEMA_VERSION),
  `risk.py` (чистые хелперы: `entry_qty`, `slippage_ok`, `cooldown_remaining`,
  `daily_stop_reason`, `should_kill`, `on_price`), `data_feed.py` (WS: ticker,
  kline, order/execution/position/wallet события, `last_price`, `is_connected`,
  `resync_needed`), `order_flow.py` (исполнение, гейты, recover, kill-switch,
  дневные счётчики), `main.py` (signal handlers, sync_time, hedge-check,
  WS wait, recover, graceful shutdown), `guard*.py` (CVD/absorption/OI).
- `core/bybit_client.py` уже умеет всё нужное: `place_order(..., order_type=
  "Limit", price=..., post_only=..., reduce_only=..., order_link_id=...)`,
  `cancel_order`, `cancel_all_orders`, `get_open_orders`, `get_order`,
  `set_stop_loss_take_profit`, `get_instrument_filters` (tick/qty step,
  min_qty, min_notional), `get_position_mode` (hedge запрещён).
- Дашборд поднимает **по процессу на монету** (`dashboard/robots.py`):
  `SYMBOL`, `STATE_FILE`, `LOG_FILE`, `KILL_SWITCH` подменяются на
  per-symbol пути; список монет — `data/dashboard/robot_coins.json`.
- Проверки в проекте: `uv run ruff check .`, `uv run ruff format .`,
  `uv run mypy robot_yrovni core`, `uv run pytest -q`.
  `mypy .` заблокирован старой ошибкой `bot_screener_yrovni/scanner.py`
  (не чинить в рамках этой задачи).

## 3. Решения, которые предлагаю (нужно «ок» пользователя)

### Р1. Итоговый набор окон

Рекомендую: `3d, 5d, 10d, 1m, 3m, 6m, 1y` — то есть кроме удаления 12ч/24ч
**убрать ещё 7d и 14d**: они попадают ровно между 5d и 10d/1m и после правила
«близкие уровни схлопываем» всё равно будут выброшены как дубли.

Буквальное чтение задачи (оставить 7d и 14d) тоже рабочее — тогда набор
`3d, 5d, 7d, 10d, 14d, 1m, 3m, 6m, 1y`, но уровней-дублей станет больше.

Интервалы данных: `3d/5d → 60m (72/120 свечей)`, `10d → 240m (60 свечей)`.
Лимиты загрузки: `60: 120`, `240: 180`, `D: 365` (сейчас `60: 72`).

### Р2. Приоритет близких уровней

Новая чистая функция в `bot_screener_yrovni/scanner.py`:

```python
merge_close_levels(levels, merge_pct, current_price) -> list[Level]
```

- Кластеризация по цене: два уровня в одном кластере, если
  `abs(p1 - p2) / current_price * 100 <= merge_pct`.
- В кластере остаётся уровень с **самым длинным окном**
  (`WINDOW_HOURS`: 1y > 6m > 3m > 1m > 10d > 5d > 3d).
- Тай-брейк при равном окне — больший накопленный объём.
- Порог: `LEVEL_MERGE_PCT=4.0` (пользователь сказал 3–5 %; 4 — середина,
  вынести в `.env`).
- Схлопываются **только POC**. HIGH/LOW/ATH/ATL остаются информационными и
  в торговле не участвуют (у них нет объёма; пользователь сказал «уровень =
  POC по объёму»).

### Р3. Как скринер «даёт команду» роботу

Единый файл-канал (не HTTP, не БД): `data/yrovni_levels.json`.

```json
{
  "updated_at": "2026-11-21T10:05:00+00:00",
  "scan_id": 1841,
  "merge_pct": 4.0,
  "symbols": {
    "XAUUSDT": {
      "price": 4210.5,
      "levels": [
        {"kind": "POC", "period": "10d", "price": 4180.0,
         "distance_pct": -0.72, "volume": 123456789.0, "hours": 240}
      ]
    }
  }
}
```

- Пишет скринер (и CLI, и дашборд) через новый модуль
  `bot_screener_yrovni/levels_feed.py::write_levels(...)`.
- Робот читает файл на каждом тике (кэш по `mtime`), берёт только свой
  `SYMBOL`.
- **Свежесть**: если файл старше `LEVEL_MAX_AGE_SEC=1800` — робот снимает
  свои лимитки и не торгует (уровень мог уехать).
- Скринер — единственный источник истины: робот **не считает POC сам**.

### Р4. Логика ордеров (главное)

Для выбранного уровня `L` и текущей цены `P`:

| Условие | Действие |
|---|---|
| `L < P` (цена сверху, уровень — поддержка) | `Buy Limit` по цене `L` |
| `L > P` (цена снизу, уровень — сопротивление) | `Sell Limit` по цене `L` |

Инварианты (проверять в коде, иначе ордер исполнится как тейкер):
- `Buy Limit` всегда **ниже** текущей цены, `Sell Limit` — **выше**;
- расстояние до уровня `>= MIN_LEVEL_DIST_PCT=0.15 %` (иначе лимитка в
  стакане и мгновенный филл);
- `qty` — от `min_qty`/`qty_step`/`min_notional` инструмента.

Зона подхода: ордер ставится, когда `|P - L| / P * 100 <= APPROACH_PCT=1.0`.
Цена ушла дальше `1.5 × APPROACH_PCT` — ордер снимается. Уровень уехал
больше `LEVEL_REPLACE_PCT=0.3 %` — ордер переставляется.

Тип: `post_only=True` (maker, без тейкерской комиссии и проскальзывания).
Риск: при гэпе через уровень ордер не исполнится — принимаем осознанно
(альтернатива — Market по касанию, но это уже «проскальзывание по сигналу»).

Один символ = один процесс = **максимум одна позиция и одна активная
лимитка**. После филла: снять остаток, поставить SL/TP (временно, см. Р6),
кулдаун `COOLDOWN_SEC=300` перед следующей попыткой.

### Р5. Авторежим

Два независимых флага (чтобы можно было включить ленту уровней без
автозапуска процессов):

- `LEVELS_FEED_ENABLED=true` — скринер пишет `data/yrovni_levels.json`.
- `AUTO_ADD_SYMBOLS=false` — скринер сам добавляет монету в список робота
  (`data/dashboard/robot_coins.json` → `robot_yrovni`), если её там нет.

Рекомендую начать с `LEVELS_FEED_ENABLED=true`, `AUTO_ADD_SYMBOLS=false`:
робот торгует фиксированным списком, который пользователь наполняет сам
(или включает авто-добавление, когда обкатаем). Автозапуск **процессов**
оставляем дашборду (`POST /api/robots/robot_yrovni/start`) — не спавнить
процессы из воркера сканера.

### Р6. Что берём из robot_trend, а что нет

**Берём (копия, без импортов из robot_trend):**
`config.py` (переименовать `TrendConfig → YrovniConfig`, убрать
трендовые поля), `state.py` (добавить `PendingOrder` — активная лимитка),
`risk.py` (как есть), `data_feed.py` (как есть, но **order/execution
события не выбрасывать** — они нужны для филла лимитки), `main.py` (как
есть), `notifier`, `TradeStore`.

**НЕ берём в v1:** `guard.py`, `guard_feed.py` (CVD/absorption/OI — 20 КБ
логики под трендовые выходы). Пользователь просил «скелет и связь с
биржей», а не guard. Добавим отдельной задачей, когда будут SL/TP.

**Переписываем:** `order_flow.py` — вместо «market по сигналу стратегии»
нужен «лимитка по уровню + отмена/перестановка + ожидание филла».
Структура цикла (`run/_tick/_wait_wake/_on_shutdown/recover`) остаётся.

### Р7. Имя и судьба старого робота

Новый каталог — `robot_yrovni/`. `robot_yrovni_D/` **не трогаем** (как
поступали с legacy-ботами в плане `robot-trend-skeleton.md`), потом удалим
отдельным коммитом вместе со страницей в `app.py:1539`.

### Р8. Тейки/стопы/размеры (пользователь: «потом»)

Чтобы робот не открывал позиции без защиты, на v1:
- `POSITION_PCT=3`, `QTY=0` (размер как в robot_trend: `risk.entry_qty`);
- `SL_PCT=0.02` — **временный** стоп, помечен «ПОТОМ ЗАПОЛНИМ»;
- `TAKE_PCT=0` — тейк не ставим (выход по стопу/вручную), помечено.

## 4. Изменения по файлам (что именно делать)

### Скринер
1. `bot_screener_yrovni/scanner.py`
   - `WINDOWS` → `{3d: (60,72), 5d: (60,120), 10d: (240,60), 1m: (240,180),
     3m: (D,90), 6m: (D,180), 1y: (D,365)}`;
   - добавить `WINDOW_HOURS = {3d:72, 5d:120, 10d:240, 1m:720, 3m:2160,
     6m:4320, 1y:8760}`;
   - `merge_close_levels(levels, merge_pct, current_price)`;
   - в `scan_symbol(..., merge_pct=0.0)` применять схлопывание после сбора
     уровней, до сортировки; `Level` получает `hours: float = 0.0`.
2. `bot_screener_yrovni/levels_feed.py` (новый) — `write_levels(signals,
   path, merge_pct)`, атомарная запись (tmp + replace), ISO-время UTC.
3. `bot_screener_yrovni/main.py` — читать `LEVELS_FEED_ENABLED`,
   `LEVEL_MERGE_PCT`, `LEVELS_FILE`; после скана писать файл.
4. `dashboard/adapters.py: scan_yrovni` — то же самое (через тот же
   `levels_feed`), плюс `AUTO_ADD_SYMBOLS` (см. Р5).
5. `bot_screener_yrovni/.env` / `.env.example` — новые параметры:
   `LEVEL_MERGE_PCT=4.0`, `LEVELS_FEED_ENABLED=true`,
   `LEVELS_FILE=data/yrovni_levels.json`, `AUTO_ADD_SYMBOLS=false`.
6. `tests/test_screener_yrovni.py` — обновить список окон и лимиты, добавить
   тесты `merge_close_levels` (кластер, приоритет по длине окна, тай-брейк
   по объёму, граница `merge_pct`).

### Робот `robot_yrovni/` (новый каталог)
```
config.py        YrovniConfig: техника robot_trend + LEVELS_FILE,
                 LEVEL_MAX_AGE_SEC, APPROACH_PCT, LEVEL_REPLACE_PCT,
                 MIN_LEVEL_DIST_PCT, POST_ONLY, MAX_ORDERS_PER_SYMBOL,
                 COOLDOWN_SEC, SL_PCT, TAKE_PCT
state.py         YrovniState + OpenPosition + PendingOrder(price, side, qty,
                 order_id, level_price, period, placed_at)
levels.py        LevelFeed: read() с кэшем по mtime, валидация схемы и
                 свежести, выбор цели (ближайший приоритетный уровень)
risk.py          копия robot_trend/risk.py
data_feed.py     копия robot_trend/data_feed.py (order/exec события НЕ
                 выбрасывать)
order_flow.py    цикл: levels → цель → reconcile лимиток → филл → SL/TP →
                 cooldown; recover(state↔биржа) с учётом открытых ордеров
main.py          копия robot_trend/main.py
.env / .env.example
SESSION_NOTES.md
```

## 5. Логика тика (псевдокод для реализации)

```
_tick():
    roll_day(); kill_switch()
    if feed.resync_needed: recover()
    lv = level_feed.read()                      # None — нет файла
    if lv is None or lv.age > LEVEL_MAX_AGE_SEC:
        await cancel_own_orders(); return
    target = level_feed.pick(lv, SYMBOL, price) # None — нет уровня в зоне
    if target is None:
        await cancel_own_orders(); return
    desired = build_order(target, price)        # Buy ниже / Sell выше
    await reconcile(desired)                    # снять лишние, поставить одну
    if position: manage_sl_tp(); return
    await check_fill()                          # WS order event или REST
```

`reconcile` обязан идемпотентно сравнивать «что стоит на бирже»
(`get_open_orders`) с «что должно стоять» и не плодить дубли.

## 6. Шаги работ

1. Скринер: окна + `WINDOW_HOURS` + `merge_close_levels` + тесты.
2. `levels_feed.py` + запись из CLI и из дашборда + `.env`.
3. `robot_yrovni/`: config/state/levels/risk/data_feed/order_flow/main.
4. `robot_yrovni/.env` (техника + временные SL) + `.env.example`.
5. Тесты `tests/test_robot_yrovni.py` (по образцу `test_robot_trend.py`:
   FakeClient/FakeFeed, tmp_path, лимитка ставится/снимается/переставляется,
   Buy ниже цены, Sell выше, stale-файл → ордеров нет).
6. Проверки: `ruff check` → `ruff format` → `mypy robot_yrovni core` →
   `pytest -q` → живой смоук на TESTNET одной монетой.
7. `SESSION_NOTES.md` + запись в `docs/worklog/`.

## 7. Риски и как их снимаем

| Риск | Решение |
|---|---|
| Лимитка исполнится мгновенно как тейкер | инвариант `Buy < P`, `Sell > P`, `post_only=True`, `MIN_LEVEL_DIST_PCT` |
| POC «плывёт» при скольжении окна | переустановка при сдвиге `> LEVEL_REPLACE_PCT`, TTL файла |
| Уровень 1y — грубая корзина (широкий диапазон / 100) | принять в v1; потом `num_bins` от диапазона/ATR |
| Нет SL (пользователь отложил) | временный `SL_PCT=0.02`, помечен «ПОТОМ ЗАПОЛНИМ» |
| Гэп через уровень | осознанно не догоняем Market'ом |
| Дубли ордеров после рестарта | `recover()` + `orderLinkId` с префиксом `yv-` |
| 20 процессов × лимитки = нагрузка на API | 1 активный ордер на символ, поллинг ордеров только при позиции/лимитке |
| Скринер упал/не пишет файл | робот стоит без ордеров (fail-safe), пишет WARNING раз в N минут |

## 8. Открытые вопросы к пользователю (нужны ответы до кода)

1. **Окна**: оставить 7d/14d (буквально) или выкинуть, как рекомендую?
2. **Порог схлопывания**: 3 %, 4 % или 5 % (`LEVEL_MERGE_PCT`)?
3. **Авторежим**: только лента уровней, или ещё и авто-добавление монет в
   список робота (с автостартом процессов из дашборда)?
4. **Тип входа**: post-only лимитка (рекомендую) или Market по касанию?
5. **Список монет робота на старте**: какие 3–5 монет берём для теста?
6. **Временный SL 2 %** — ок, или другой процент до того, как настроим риск?

## 9. Как продолжить (для следующей модели/сессии)

- Читать этот файл + `robot_trend/SESSION_NOTES.md` + `.omo/plans/
  robot-trend-skeleton.md`.
- Ключевые точки: `bot_screener_yrovni/scanner.py:26` (`WINDOWS`),
  `dashboard/adapters.py:474` (`scan_yrovni`), `robot_trend/order_flow.py`
  (`_tick`, `_enter`, `_on_shutdown`), `dashboard/robots.py` (per-symbol env).
- Договорённости проекта: `.env` в git не коммитить (`**/.env`), тесты — pytest,
  стиль — ruff/88 символов, время в логах UTC, пользователю — МСК.
- Проверки перед сдачей: `uv run ruff check .`, `uv run ruff format .`,
  `uv run mypy robot_yrovni core`, `uv run pytest -q`.
