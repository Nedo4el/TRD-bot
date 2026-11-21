# ТЗ: модуль POC-скринера (сохранено дословно 21.11.2026, 17:15 МСК)

Сохранено по просьбе пользователя: «сначало запомни текст в файл чтобы если
прервётся вспомнить что нужно было сделать».

> Ниже — текст задания БЕЗ изменений. Расхождения с прежним планом
> `.omo/plans/robot-yrovni-levels.md` перечислены в конце файла.

---

Ты — senior Python-разработчик торговых систем. Напиши модуль POC-скринера для крипто-бота (Bybit linear, альты, Python 3.10+, ccxt/bybit v5, pandas, numpy, SQLite).

ЗАДАЧА:
Скринер находит ВСЕ значимые POC-уровни (Point of Control — цена с максимальным объёмом) за разные исторические окна и отслеживает, когда текущая цена возвращается к старому POC. При возврате — шлёт сигнал роботу на выставление ордеров.

КЛЮЧЕВОЙ ПРИНЦИП (не путать!):
POC-уровень существует независимо от того, касалась ли его цена после формирования. Уровень валиден, пока на нём есть накопленный объём. Касание — это ТРИГГЕР для сигнала, а НЕ условие существования уровня. Цена может пройти мимо один раз, но если там был объём — это POC, и он остаётся значимым.

ОКНА POC (все считать одновременно):
- 3 дня, 5 дней, 7 дней, 14 дней, 30 дней, 180 дней, 365 дней, 730 дней

ДАННЫЕ:
- Источник: Bybit v5 REST /v5/market/kline (category=linear)
- 3-30 дней → 1h свечи; 180-730 дней → 4h свечи
- Кэшировать историю в SQLite/Parquet
- Формат DataFrame: [ts, open, high, low, close, volume]
- Rate limit: не более 10 req/sec

АЛГОРИТМ POC (Volume Profile):
1. Для каждого окна взять свечи за период.
2. Построить Volume Profile: разбить [min_low, max_high] на N=100 бинов, распределить объём каждой свечи по бинам пропорционально перекрытию [low, high] с бином.
3. POC = центр бина с максимальным объёмом.
4. Value Area (VA) = 68% объёма вокруг POC → VAH, VAL.
5. Вернуть: {window, poc, vah, val, total_volume, ts_calculated}.

ЛОГИКА POC-УРОВНЯ (исправлено — критично!):

1. Создание уровня:
   - При каждом расчёте POC (любое окно) — создать/обновить уровень в poc_history.
   - Уровень создаётся СРАЗУ, даже если цена никогда к нему не возвращалась.
   - Поля: poc_price, total_volume_at_level, source_windows[], first_created_ts,
     last_calculated_ts, touch_count (может быть 0!), confirmed.

2. Слияние уровней (MERGE_POC_PCT=0.01):
   - Если новый POC в пределах 1% от существующего → слить:
     * total_volume_at_level += новый объём
     * source_windows добавить окно
     * last_calculated_ts = now
     * пересчитать confirmed

3. Касание (touch) — только счётчик, не условие существования:
   - Если current_price в зоне ±TOUCH_PROXIMITY_PCT от poc_price:
     * touch_count += 1
     * last_touch_ts = now
     * сгенерировать сигнал POC_REACHED (если confirmed или volume >= порога)

4. Подтверждение (confirmed) — по объёму, НЕ по касаниям:
   - confirmed = (total_volume_at_level >= CONFIRM_VOLUME_THRESHOLD)
   - touch_count НЕ влияет на confirmed. Уровень с 0 касаний и $100M объёма —
     такой же валидный, как уровень с 10 касаниями и $100M.
   - touch_count используется только для confidence.

5. Confidence:
   confidence = min(1.0,
       (total_volume_at_level / CONFIRM_VOLUME_THRESHOLD) * 0.5 +
       (matched_windows_count / len(POC_WINDOWS)) * 0.3 +
       (poc_age_days / 365) * 0.1 +
       min(touch_count / 10, 1.0) * 0.1
   )
   Объём — главный фактор (50%), совпадение окон — второй (30%),
   возраст и касания — второстепенные.

6. Сигнал POC_REACHED генерируется, если:
   - current_price в зоне ±0.5% от poc_price
   - И (confirmed == true ИЛИ total_volume_at_level >= MIN_SIGNAL_VOLUME)
   - touch_count на момент сигнала может быть любым (включая 0 при первом подходе)

7. «Стена» (сильный уровень):
   - total_volume_at_level >= WALL_VOLUME_THRESHOLD ($200M)
   - ИЛИ matched_windows_count >= 3
   - Такие уровни торгуются максимальным размером.

ФОРМАТ СИГНАЛА (JSON, POST /api/signal в робот):
{
  "ts": "2026-11-21T12:00:00Z",
  "symbol": "SOLUSDT",
  "type": "POC_REACHED",
  "poc_price": 50.0,
  "poc_windows": ["7d", "30d", "180d"],
  "poc_age_days": 180,
  "total_volume_at_level": 80000000,
  "touch_count": 1,
  "is_wall": false,
  "proximity_pct": 0.003,
  "vah": 51.2, "val": 48.8,
  "confidence": 0.75,
  "action": "PLACE_LIMIT_ORDERS",
  "side_hint": "both",
  "order_params": {
     "buy_at": 48.8,
     "sell_at": 51.2,
     "stop_buy": 48.8 - 0.5*ATR,
     "stop_sell": 51.2 + 0.5*ATR
  }
}

КОМАНДА РОБОТУ:
При confidence >= MIN_CONFIDENCE (0.6):
- Buy лимит на VAL, sell лимит на VAH (или у POC ± offset).
- Стоп за границей VA + 0.5×ATR.
- Тейк — следующий значимый POC или противоположная граница VA.

ПАРАМЕТРЫ В КОНФИГ:
POC_WINDOWS = ["3d", "5d", "7d", "14d", "30d", "180d", "365d", "730d"]
POC_BINS = 100
VA_PERCENT = 0.68
CONFIRM_VOLUME_THRESHOLD = 50000000    # $50M — уровень подтверждён объёмом
MIN_SIGNAL_VOLUME = 10000000           # $10M — мин. объём для сигнала
WALL_VOLUME_THRESHOLD = 200000000      # $200M — «стена»
TOUCH_PROXIMITY_PCT = 0.005            # 0.5% — зона касания/сигнала
MERGE_POC_PCT = 0.01                   # 1% — слияние уровней
MIN_CONFIDENCE = 0.6
MIN_POC_AGE_DAYS = 7                   # игнорировать POC младше 7 дней
MAX_POC_AGE_DAYS = 730                 # старше 2 лет — не учитывать
POC_HISTORY_DB = "data/poc_history.sqlite"
HISTORY_CACHE_DAYS = 730

PER-SYMBOL OVERRIDES (для низколиквидных альтов):
BTCUSDT: CONFIRM_VOLUME_THRESHOLD=500000000
SOLUSDT: CONFIRM_VOLUME_THRESHOLD=100000000
AVAXUSDT: CONFIRM_VOLUME_THRESHOLD=30000000
(остальные — default)

АРХИТЕКТУРА:
- class POCScanner:
    - __init__(config, symbol)
    - load_history() -> DataFrame
    - calculate_poc(df, window) -> dict
    - upsert_poc_level(poc_data)  # создать/слить/обновить
    - check_price_proximity(current_price) -> list[Signal]
    - combine_signals(signals) -> Signal
- class POCSignal: dataclass с полями выше
- main loop: на закрытии 5m свечи → check_price_proximity → если сигнал, POST в дашборд/робота.

SQLITE-СХЕМА:
CREATE TABLE poc_levels (
    id INTEGER PRIMARY KEY,
    symbol TEXT NOT NULL,
    poc_price REAL NOT NULL,
    vah REAL, val REAL,
    total_volume_at_level REAL DEFAULT 0,
    source_windows TEXT,           -- JSON: ["7d","30d"]
    first_created_ts TEXT,
    last_calculated_ts TEXT,
    last_touch_ts TEXT,
    touch_count INTEGER DEFAULT 0,
    confirmed BOOLEAN DEFAULT 0,
    UNIQUE(symbol, poc_price)
);
CREATE TABLE poc_signals_log (
    id INTEGER PRIMARY KEY,
    ts TEXT, symbol TEXT, poc_price REAL,
    confidence REAL, touch_count INTEGER,
    action TEXT, raw_json TEXT
);

ТРЕБОВАНИЯ К КОДУ:
- Полный рабочий код, не псевдокод.
- Кэширование klines (не дёргать API каждый раз).
- Логирование каждого найденного POC и каждого сигнала в JSONL.
- Юнит-тесты:
  * POC на $50 создан 180 дней назад с объёмом $80M → уровень confirmed, touch_count=0.
  * Цена возвращается к $50 → сигнал POC_REACHED с confidence > 0.6.
  * POC на $50 и $50.3 из разных окон → сливаются в один уровень.
  * POC с объёмом $5M → не confirmed, сигнал не генерируется.
- README: запуск, проверка на реальном SOLUSDT, смена окон.

ОГРАНИЧЕНИЯ:
- Не открывать позиции самому — только сигнал роботу.
- Не считать POC на свечах младше 1h.
- Для альтов с низкой ликвидностью MIN_STRENGTH_USD поднять (см. PER-SYMBOL OVERRIDES).
- Учесть разные интервалы свечей для разных окон (1h vs 4h) — пересчёт в единый формат.

ВЫВОД:
1. Структура проекта (файлы).
2. Код POCScanner + POCSignal.
3. SQLite-схема + миграция.
4. Пример main.py с циклом.
5. Пример конфига.
6. Юнит-тесты (все 4 кейса).
7. README.

---

## Расхождения с прежним планом (решение пользователя: делать по этому ТЗ)

| Пункт | Прежний план | Новое ТЗ |
|---|---|---|
| Окна | `3d, 5d, 7d, 10d, 14d, 1m, 3m, 6m, 1y` | `3d, 5d, 7d, 14d, 30d, 180d, 365d, 730d` (10d и 1m/3m убраны, добавлены 30d/365d/730d) |
| Слияние | 3 % от цены | 1 % от цены |
| Передача роботу | файл `data/yrovni_levels.json` | HTTP `POST /api/signal` + SQLite-история |
| Точка входа | POC-цена | VAL/VAH (границы Value Area 68 %) |
| Хранение | без БД | SQLite `poc_history.sqlite` + JSONL-логи |
| Касания | не отслеживались | `touch_count`, `last_touch_ts`, confidence |
| Монеты | 17 из прошлого прогона | любые (по символу на процесс) |
