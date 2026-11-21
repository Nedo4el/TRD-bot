# План: скринер «Уровни» (POC) + новый робот robot_yrovni

Статус: **решения пользователя получены 21.11.2026 17:06 МСК**, остался один
открытый пункт (Р9 — торговать ли POC длинных окон). Код пока не менялся.

---

## 1. Что просил пользователь

1. Скринер «уровни»: **убрать окна 12ч и 24ч**, **добавить 3д, 5д, 10д** к уже
   существующим окнам (7д и 14д не трогать).
2. Если уровни стоят близко друг к другу (**3–5 %**), приоритет у уровня,
   который **дольше наторговывался** (более длинное окно) — ордера будут там.
   Уровень = **POC по объёму за период**, а не экстремум.
3. Скринер **даёт команду роботу** уровни, если включён **автоматический
   режим** (если такого режима нет — добавить): робот выставляет **лимитные**
   ордера на покупку, если цена идёт сверху к уровню, и на продажу, если цена
   подходит снизу. Ручной режим тоже должен работать.
4. Скелет робота — **из `robot_trend`**: всё, что нужно для нормальной работы и
   связи с биржей (клиент, WS, состояние, recover, гейты, main).
5. Тейки/стопы/размеры ордеров — **потом**.

## 2. Решения пользователя (зафиксировано)

| № | Вопрос | Решение |
|---|---|---|
| 1 | Набор окон | **не трогать 7d/14d**: итог `3d, 5d, 7d, 10d, 14d, 1m, 3m, 6m, 1y` (убраны только 12h/24h, добавлены 5d/10d) |
| 2 | Порог схлопывания | **3.0 %** (`LEVEL_MERGE_PCT=3.0`) |
| 3 | Авторежим | **авто-добавление монет + ручной режим**: авто включено флагом, ручное редактирование списка работает всегда |
| 4 | Тип входа | **лимитка** (post-only, по цене уровня) |
| 5 | Какие POC торговать | **не решено** — пояснение и рекомендация в Р9 |
| 6 | Монеты для теста | **альты из прошлого прогона** — 17 символов, см. §7 |

## 3. Что уже изучено (не перепроверять)

- `bot_screener_yrovni/scanner.py`
  - `WINDOWS: dict[str, tuple[str, int]]` — окна: `12h, 24h, 3d, 7d, 14d, 1m,
    3m, 6m, 1y` (интервал Bybit, число свечей). `3d` уже есть.
  - `interval_limits()` — сколько свечей качать на интервал (сейчас
    `60: 72`, `240: 180`, `D: 365`).
  - `build_volume_profile(candles, num_bins=100)`, `find_poc(candles,
    num_bins=100) -> tuple[float, float] | None` (цена, объём).
  - `scan_symbol(symbol, series, *, all_time=None, num_bins=100,
    proximity_pct=10.0, max_price=0.0) -> VPSignal`.
  - `KIND_POC / KIND_HIGH / KIND_LOW / KIND_ATH / KIND_ATL`.
  - `EXTREME_WINDOWS = ("1m","3m","6m","1y")` → HIGH/LOW; ATH/ATL отдельно.
  - Уровни фильтруются по `proximity_pct` (10 % от цены) и сортируются по
    модулю расстояния. **Схлопывания близких уровней нет.**
- Скринер запускается из двух мест:
  - `bot_screener_yrovni/main.py` (CLI, цикл `SCAN_INTERVAL=600`);
  - `dashboard/adapters.py: scan_yrovni()` (вкладка дашборда, 300 с,
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

## 4. Изменения по файлам

### Скринер
1. `bot_screener_yrovni/scanner.py`
   - `WINDOWS` → `{3d: (60,72), 5d: (60,120), 7d: (240,42), 10d: (240,60),
     14d: (240,84), 1m: (240,180), 3m: (D,90), 6m: (D,180), 1y: (D,365)}`
     (12h/24h удалены, 5d/10d добавлены, 7d/14d не тронуты);
   - `WINDOW_HOURS = {3d:72, 5d:120, 7d:168, 10d:240, 14d:336, 1m:720,
     3m:2160, 6m:4320, 1y:8760}`;
   - `merge_close_levels(levels, merge_pct, current_price)`:
     кластеризация по `abs(p1-p2)/current_price*100 <= merge_pct`, в кластере
     остаётся уровень с наибольшим `hours`, тай-брейк — больший объём;
   - `Level` получает `hours: float = 0.0`;
   - в `scan_symbol(..., merge_pct=0.0)` применять схлопывание после сбора
     уровней, до сортировки;
   - схлопываются **только POC** (HIGH/LOW/ATH/ATL остаются информационными).
2. `bot_screener_yrovni/levels_feed.py` (новый) — `write_levels(signals,
   path, merge_pct)`, атомарная запись (tmp + replace), время UTC в ISO.
3. `bot_screener_yrovni/main.py` — читать `LEVELS_FEED_ENABLED`,
   `LEVEL_MERGE_PCT`, `LEVELS_FILE`; после скана писать файл.
4. `dashboard/adapters.py: scan_yrovni` — то же самое (через тот же
   `levels_feed`), плюс авто-добавление монет (см. Р5).
5. `bot_screener_yrovni/.env` / `.env.example` — новые параметры:
   `LEVEL_MERGE_PCT=3.0`, `LEVELS_FEED_ENABLED=true`,
   `LEVELS_FILE=data/yrovni_levels.json`, `AUTO_ADD_SYMBOLS=true`,
   `AUTO_ADD_MAX_COINS=25`.
6. `tests/test_screener_yrovni.py` — обновить список окон и лимиты, добавить
   тесты `merge_close_levels` (кластер, приоритет по длине окна, тай-брейк
   по объёму, граница 3 %).

### Робот `robot_yrovni/` (новый каталог)
```
config.py        YrovniConfig: техника robot_trend + LEVELS_FILE,
                 LEVEL_MAX_AGE_SEC, APPROACH_PCT, LEVEL_REPLACE_PCT,
                 MIN_LEVEL_DIST_PCT, POST_ONLY, MAX_ORDERS_PER_SYMBOL,
                 COOLDOWN_SEC, SL_PCT, TAKE_PCT, LEVELS_MAX_HOURS
state.py         YrovniState + OpenPosition + PendingOrder(price, side, qty,
                 order_id, level_price, period, placed_at)
levels.py        LevelFeed: read() с кэшем по mtime, валидация схемы и
                 свежести, выбор цели (ближайший приоритетный уровень)
risk.py          копия robot_trend/risk.py
data_feed.py     копия robot_trend/data_feed.py (order/exec события НЕ
                 выбрасывать — нужны для филла лимитки)
order_flow.py    цикл: levels → цель → reconcile лимиток → филл → SL/TP →
                 cooldown; recover(state↔биржа) с учётом открытых ордеров
main.py          копия robot_trend/main.py
.env / .env.example
SESSION_NOTES.md
```

## 5. Канал «скринер → робот»

Единый файл `data/yrovni_levels.json`:

```json
{
  "updated_at": "2026-11-21T14:05:00+00:00",
  "scan_id": 1841,
  "merge_pct": 3.0,
  "symbols": {
    "XAUUSDT": {
      "price": 3567.82,
      "levels": [
        {"kind": "POC", "period": "7d", "price": 3542.12,
         "distance_pct": -0.72, "volume": 123456789.0, "hours": 168}
      ]
    }
  }
}
```

- Пишет скринер (и CLI, и дашборд) через `levels_feed.write_levels()`.
- Робот читает на каждом тике (кэш по `mtime`), берёт только свой `SYMBOL`.
- Файл старше `LEVEL_MAX_AGE_SEC=1800` → робот снимает лимитки и не торгует.
- Скринер — единственный источник истины: робот **не считает POC сам**.

## 6. Логика ордеров

Для уровня `L` и цены `P`:

| Условие | Действие |
|---|---|
| `L < P` (цена сверху) | `Buy Limit` по цене `L` |
| `L > P` (цена снизу) | `Sell Limit` по цене `L` |

Инварианты (проверяются в коде):
- `Buy Limit` строго **ниже** цены, `Sell Limit` строго **выше**;
- `|P - L| / P * 100 >= MIN_LEVEL_DIST_PCT=0.15` (иначе мгновенный филл);
- `post_only=True`;
- `qty` по `min_qty`/`qty_step`/`min_notional` инструмента.

Зона подхода `APPROACH_PCT=1.0 %`: вне неё ордер не ставится; цена ушла
дальше `1.5 × APPROACH_PCT` — снять. Уровень сдвинулся больше
`LEVEL_REPLACE_PCT=0.3 %` — переставить.

Один символ = один процесс = **одна позиция и одна активная лимитка**.
После филла: снять остаток, поставить SL/TP, кулдаун `COOLDOWN_SEC=300`.

## 7. Монеты для теста (из прошлого прогона)

Последний прогон скринера «уровни» (бэкап `data/dashboard/latest.backup-
20261119-140234.json`) дал 17 сигналов:

`HYPEUSDT, QQQUSDT, XAUUSDT, ZECUSDT, ENAUSDT, SPCXUSDT, CLUSDT, SUIUSDT,
XAGUSDT, NEARUSDT, SNDKUSDT, 1000PEPEUSDT, NVDAUSDT, SOXLUSDT, KORUUSDT,
BARDUSDT, AKEUSDT`

Это и есть стартовый список `robot_yrovni` (все, кроме BTC/ETH).

**Важное следствие смены окон** (проверено живьём 21.11 ~17:00 МСК на текущих
данных, окна `3d/5d/7d/10d/14d/1m/3m/6m/1y`, merge 3 %, зона 1 %):

| Символ | Ближайший приоритетный уровень | В зоне 1 % |
|---|---|---|
| QQQUSDT | 3d 619.11 (-0.69 %) | да |
| XAUUSDT | 7d 3542.12 (-0.72 %) | да |
| SOXLUSDT | 3d 32.96 (+0.39 %) | да |
| CLUSDT | 7d 83.04 (-1.13 %) | нет |
| BARDUSDT | 7d 0.09216 (-1.78 %) | нет |
| NEARUSDT | 3d 2.81 (-3.45 %) | нет |
| ZECUSDT | 3d 762 (-4.82 %) | нет |
| ENAUSDT | 1y 0.0918 (-6.83 %) | нет |
| SPCXUSDT | 1y 139.72 (+6.57 %) | нет |
| AKEUSDT | 1m 0.00522 (-7.24 %) | нет |
| XAGUSDT | 5d 45.38 (+8.65 %) | нет |
| 1000PEPEUSDT | 1y 0.003815 (+9.44 %) | нет |
| HYPEUSDT | 14d 67.30 (+9.82 %) | нет |
| SUIUSDT | 3d 0.7154 (+9.96 %) | нет |
| SNDKUSDT, NVDAUSDT, KORUUSDT | нет POC в пределах 10 % | нет |

Вывод: после удаления 12ч/24ч **сигналов стало в разы меньше** — это
ожидаемо (внутридневные POC стояли вплотную к цене, длинные стоят дальше).
Робот будет чаще стоять без ордеров, чем торговать. Это нормальный режим
лимитной стратегии, но не ждать потока сделок.

## 8. Робот: что берём из robot_trend

**Берём (копия, без импортов):** `config.py` (`TrendConfig → YrovniConfig`),
`state.py` (+`PendingOrder`), `risk.py` (как есть), `data_feed.py` (order/exec
события **не выбрасывать**), `main.py`, `notifier`, `TradeStore`.

**НЕ берём в v1:** `guard.py`, `guard_feed.py` (CVD/absorption/OI — трендовая
логика выходов, к уровням не относится).

**Переписываем:** `order_flow.py` — вместо Market по сигналу стратегии:
лимитка по уровню, отмена/перестановка, ожидание филла. Каркас цикла
(`run/_tick/_wait_wake/_on_shutdown/recover`) сохраняется.

## 9. Открытый пункт: торговать ли POC длинных окон

Проблема: POC — это центр **одной** корзины из 100, на которые разбит
диапазон окна. Чем длиннее окно, тем шире корзина и тем «размытее» уровень.
Замер на текущих данных (ширина одной корзины в % от цены):

| Окно | XAUUSDT | ENAUSDT | 1000PEPEUSDT |
|---|---|---|---|
| 3d | 0.075 % | 0.245 % | 0.253 % |
| 5d | 0.109 % | 0.304 % | 0.253 % |
| 7d | 0.111 % | 0.341 % | 0.260 % |
| 10d | 0.142 % | 0.675 % | 0.529 % |
| 14d | 0.182 % | 0.706 % | 0.705 % |
| 1m | 0.182 % | 1.307 % | 0.722 % |
| 3m | 0.334 % | 2.086 % | 0.722 % |
| 6m | 0.334 % | 2.289 % | 0.992 % |
| 1y | 0.484 % | 2.369 % | 1.449 % |

То есть на ENA/мем-коинах «уровень 1m» — это пятно шириной 1.3 %, а зона
подхода 1 %: ордер по сути ставится «где-то в раёне POC», а не на уровне.

Варианты:
- **А (рекомендую):** торговать POC окон `3d…14d`; `1m/3m/6m/1y` — только
  контекст и приоритет в скринере, ордера по ним не ставим.
- **Б:** торговать все окна, но зону подхода для длинного окна расширять до
  ширины его корзины (сложнее, размывает понятие «уровень»).
- **В:** торговать все окна с текущей зоной 1 % (просто, но будут входы «около
  уровня» на длинных окнах).

Технически это один флаг `LEVELS_MAX_HOURS` (А = 336, В = 8760).

## 10. Шаги работ

1. Скринер: окна + `WINDOW_HOURS` + `merge_close_levels` + тесты.
2. `levels_feed.py` + запись из CLI и из дашборда + `.env`.
3. Авто-добавление монет в список `robot_yrovni` (флаг + лимит количества).
4. `robot_yrovni/`: config/state/levels/risk/data_feed/order_flow/main.
5. `robot_yrovni/.env` + `.env.example` (временный `SL_PCT=0.02`, помечен
   «ПОТОМ ЗАПОЛНИМ»).
6. Тесты `tests/test_robot_yrovni.py` (FakeClient/FakeFeed, tmp_path:
   лимитка ставится/снимается/переставляется, Buy ниже цены, Sell выше,
   stale-файл → ордеров нет).
7. Проверки: `ruff check` → `ruff format` → `mypy robot_yrovni core` →
   `pytest -q` → смоук на TESTNET одной монетой (XAUUSDT).
8. `SESSION_NOTES.md` + запись в `docs/worklog/`.

## 11. Риски

| Риск | Решение |
|---|---|
| Лимитка исполнится мгновенно как тейкер | `Buy < P`, `Sell > P`, `post_only`, `MIN_LEVEL_DIST_PCT` |
| POC «плывёт» при скольжении окна | переустановка при сдвиге `> LEVEL_REPLACE_PCT`, TTL файла |
| Длинные окна = размытый уровень | Р9 (вариант А) |
| Нет SL (пользователь отложил) | временный `SL_PCT=0.02`, помечен |
| Гэп через уровень | осознанно не догоняем Market'ом |
| Дубли ордеров после рестарта | `recover()` + `orderLinkId` с префиксом `yv-` |
| 20+ процессов × лимитки | 1 активный ордер на символ, поллинг только при позиции/лимитке |
| Скринер упал/не пишет файл | робот стоит без ордеров (fail-safe) + WARNING |

## 12. Как продолжить (для следующей модели/сессии)

- Читать этот файл + `robot_trend/SESSION_NOTES.md` + `.omo/plans/
  robot-trend-skeleton.md`.
- Ключевые точки: `bot_screener_yrovni/scanner.py:26` (`WINDOWS`),
  `dashboard/adapters.py:474` (`scan_yrovni`), `robot_trend/order_flow.py`
  (`_tick`, `_enter`, `_on_shutdown`), `dashboard/robots.py` (per-symbol env).
- Договорённости проекта: `.env` не коммитить (`**/.env`), тесты — pytest,
  стиль — ruff/88 символов, в логах UTC, пользователю — МСК.
- Проверки перед сдачей: `uv run ruff check .`, `uv run ruff format .`,
  `uv run mypy robot_yrovni core`, `uv run pytest -q`.
