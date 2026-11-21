# POC-скринер

Находит POC-уровни (Point of Control — цена с максимальным накопленным
объёмом) за 8 исторических окон, хранит их в SQLite и отдаёт сигнал роботу,
когда цена подходит к уровню вплотную.

**Ключевой принцип:** уровень существует независимо от того, касалась ли его
цена. Касание — триггер для сигнала, а не условие существования уровня.
Подтверждение (`confirmed`) определяется объёмом, а не числом касаний.

## Структура

```
bot_screener_yrovni/poc_scanner/
├── config.py     # POCConfig, окна, PER_SYMBOL_OVERRIDES, чтение .env
├── profile.py    # Volume Profile: POC, Value Area (VAH/VAL)
├── store.py      # SQLite: poc_levels, poc_signals_log, klines
├── klines.py     # Bybit v5 /market/kline + кэш + лимит 10 req/s
├── scanner.py    # POCScanner: расчёт, слияние, сигналы
├── signals.py    # POCSignal (JSON роботу)
├── timeutil.py   # ISO-время UTC
├── main.py       # цикл по закрытию 5m свечи, POST /api/signal
└── .env.example  # все параметры
```

## Запуск

Из корня проекта:

```powershell
# один проход по одному символу (проверка)
.venv\Scripts\python.exe -m bot_screener_yrovni.poc_scanner.main SOLUSDT --once

# постоянный цикл по списку
.venv\Scripts\python.exe -m bot_screener_yrovni.poc_scanner.main SOLUSDT ETHUSDT

# или список из POC_SYMBOLS в .env
.venv\Scripts\python.exe -m bot_screener_yrovni.poc_scanner.main
```

Первый запуск скачивает историю (для 730d на 4h — 4380 свечей, 5 запросов),
дальше каждые 5 минут добирается только хвост из кэша.

## Проверка на реальном SOLUSDT

```powershell
.venv\Scripts\python.exe -m bot_screener_yrovni.poc_scanner.main SOLUSDT --once
```

Что должно быть в выводе:

- `свечи: SOLUSDT 60 +N (кэш обновлён)` — история скачалась;
- строки `POC SOLUSDT 7d=... (VA ...) объём $...M окна 7d` по каждому окну;
- при подходе цены к уровню — `СИГНАЛ SOLUSDT POC=... confidence=... PLACE_LIMIT_ORDERS`;
- файлы `logs/poc_levels.jsonl` и `logs/poc_signals.jsonl`;
- таблицы в `data/poc_history.sqlite`:

```powershell
.venv\Scripts\python.exe -c "import sqlite3;c=sqlite3.connect('data/poc_history.sqlite');print(c.execute('select symbol,poc_price,total_volume_at_level,source_windows,confirmed,touch_count from poc_levels order by poc_price').fetchall())"
```

Если робот не запущен, POST вернёт `робот недоступен (...)` — это нормально,
сигнал всё равно записан в БД и JSONL.

## Смена окон

Окна задаются в `.env` (или `poc_scanner/.env`) одной строкой:

```
POC_WINDOWS=3d,5d,7d,14d,30d,180d,365d,730d
```

- формат `<дни>d`, порядок сохраняется;
- до 30 дней — часовые свечи, длиннее — четырёхчасовые;
- после смены окна старые уровни в БД остаются: они удаляются вручную
  (`DELETE FROM poc_levels WHERE symbol='SOLUSDT'`), иначе новые POC
  сливаются со старыми в пределах `MERGE_POC_PCT`.

## Параметры

| Переменная | По умолчанию | Смысл |
|---|---|---|
| `POC_WINDOWS` | `3d,5d,7d,14d,30d,180d,365d,730d` | окна расчёта |
| `POC_BINS` | 100 | корзин в профиле |
| `VA_PERCENT` | 0.68 | доля объёма в Value Area |
| `CONFIRM_VOLUME_THRESHOLD` | 50 000 000 | объём, подтверждающий уровень |
| `MIN_SIGNAL_VOLUME` | 10 000 000 | минимум объёма для сигнала |
| `WALL_VOLUME_THRESHOLD` | 200 000 000 | порог «стены» |
| `TOUCH_PROXIMITY_PCT` | 0.005 | зона касания/сигнала (±0.5 %) |
| `MERGE_POC_PCT` | 0.01 | слияние уровней (1 %) |
| `MIN_CONFIDENCE` | 0.6 | порог команды роботу |
| `MIN_POC_AGE_DAYS` | 7 | возраст уровня (длина окна): короче — окно не считается |
| `MAX_POC_AGE_DAYS` | 730 | старше — игнорировать |
| `POC_HISTORY_DB` | `data/poc_history.sqlite` | БД уровней и кэша свечей |
| `HISTORY_CACHE_DAYS` | 730 | глубина истории |
| `SIGNAL_ENDPOINT` | `http://127.0.0.1:8787/api/signal` | куда POST-ить сигнал |
| `TOUCH_COOLDOWN_SEC` | 3600 | не чаще одного касания в час |
| `SIGNAL_COOLDOWN_SEC` | 3600 | не чаще одного сигнала в час |
| `ATR_PERIOD` | 14 | период ATR для стопов |

Переопределения по символу (`PER_SYMBOL_OVERRIDES` в `config.py`):
`BTCUSDT` — $500M, `SOLUSDT` — $100M, `AVAXUSDT` — $30M.

## Схема БД

`poc_levels` — уровни (одна строка на уровень, уникальность `symbol` +
`poc_price`); `source_windows` — JSON-список окон, `window_volumes` — JSON
`{окно: объём}`; `poc_age_days` — возраст уровня (максимум по его окнам),
`first_created_ts` — начало самого старого окна (справочно, в фильтре не
участвует), `last_calculated_ts` обновляется каждым сканом;
`touch_count`/`last_touch_ts` — касания.

`poc_signals_log` — каждый выданный сигнал (`raw_json` — полное тело).

`klines` — кэш свечей (`PRIMARY KEY (symbol, interval, open_time)`).

Версия схемы — `PRAGMA user_version`; `migrate()` создаёт таблицы на пустой
БД и добавляет новые колонки к уже существующей.

## Отличия от исходного ТЗ (осознанные)

1. **Объём уровня не копится.** В ТЗ `total_volume_at_level += новый объём`,
   но при пересчёте каждые 5 минут это раздуло бы объём до бесконечности и
   все уровни стали бы «стенами». Хранится `{окно: объём}`, повторный расчёт
   того же окна **заменяет** значение, объёмы разных окон суммируются.
2. **`action` бывает `OBSERVE`.** Сигнал выдаётся по правилу ТЗ (зона ±0.5 %
   и объём), но команда `PLACE_LIMIT_ORDERS` ставится только при
   `confidence >= MIN_CONFIDENCE`, иначе `OBSERVE`.
3. **Кулдауны касаний и сигналов.** Иначе цена, стоящая в зоне, генерировала
   бы касание и сигнал каждые 5 минут.
4. **Свечи грузятся через stdlib `urllib`,** а не `ccxt`: ccxt в проекте нет,
   а Bybit v5 REST достаточно. pandas/numpy — уже в `requirements.txt`.
5. **Возраст уровня — длина окна, а не дата записи в БД.** Иначе на новом
   символе все уровни «моложе суток» и `MIN_POC_AGE_DAYS` отбрасывал бы их
   все: бот был бы слепым первые 7 дней. Окна короче `MIN_POC_AGE_DAYS`
   (`3d`, `5d` при дефолте 7) не считаются вовсе; возраст слитого уровня —
   максимум по его окнам. `first_created_ts` хранит начало самого старого
   окна и нужен только для отчётности.
