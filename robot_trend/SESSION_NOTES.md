# Robot Trend — Состояние на 2026-10-04

- **Мульти-ТФ, конфиг после сетки** (см. «Сетка мульти-ТФ»): **60м = режим**
  (EMA 20/50/200 + ADX ≥ 25), **15м = триггер** (флип Supertrend + объём ≥1.3×SMA20).
  Выход — флип ST на 15м. `TIMEFRAME=15`, `HTF_TIMEFRAME=60`, `HTF_WARMUP=250`.
  Выбрано по сетке на 5 альтах (LSK/BRU/ARK/KAVA/BAND, 90д): 5/5 прибыльных,
  ΣPnL +32.8%, win 58.7%, DD ≤5.3%.
- **Риск** (см. «Сессия 2026-10-03»): SL −2% / TP +5%, безубыток и трейлинг после +2%.
- Каркас технического слоя (сессия 2026-09-29) — без изменений.
- Результаты бэктеста старых стратегий (2026-09-13) — только для истории.

---

# Robot Trend — Сессия 2026-10-04 — МУЛЬТИ-ТФ: 30м режим / 5м триггер

## Логика
| ТФ | Что решает | Индикаторы |
|----|-----------|------------|
| **30м** | направление + сила тренда (куда входить) | EMA 20/50/200, ADX ≥ 25, +DI/−DI |
| **5м** | момент входа (когда входить) + выход | флип Supertrend, объём ≥ 1.3×SMA20 |

- **Вход**: триггер 5м только в направлении 30м (long ⇔ trend_up, short ⇔ trend_down).
- **Выход**: флип Supertrend на 5м — **не зависит** от 30м (страховка/slippage-gates
  и риск живут как раньше). SL −2% / TP +5% / BE / трейлинг — без изменений.
- Стороны: **Long + Short**; альт-профиль Supertrend (10/3.0) — уже по SYMBOL.

## Изменения по файлам
| Файл | Изменение |
|------|-----------|
| `robot_trend/config.py` | `HTF_TIMEFRAME` (30), `HTF_WARMUP` (250) + model-validator: htf > tf |
| `robot_trend/strategy.py` | `_Snapshot` = только 5м (close/st_dir/st_flip/vol_ratio); новый `_HTFSnapshot` = 30м (trend_up/down, adx, di); `decide(candles, htf_candles, position)`; `_entry(ltf, htf)`; `set_htf()` + `_closed_htf()` для бэктеста (без hindsight: закрытые 30м свечи по `open_time + 30м ≤ закрытие 5м свечи`); `TrendParams`: +`timeframe_min/htf_min/htf_warmup`, **удалён неиспользуемый `atr_period`** (ATR больше не считается — ушёл из снапшота) |
| `robot_trend/order_flow.py` | `_fetch_htf()` — REST 30м **только при решении** (1 запрос / 5 мин); ошибка/пусто 30м → решение откладывается (`_last_decided` не двигаем, повтор на следующем тике) |
| `core/strategies.py` | `BaseStrategy.set_htf()` — no-op по умолчанию (FlatStrategy не тронута) |
| `backtest.py` | `fetch_candles(..., timeframe=)`; `make_strategy(name, timeframe=, htf=)`; после загрузки 5м — догрузка 30м за тот же диапазон → `strategy.set_htf()`; CLI `--htf`; заголовок `5/30` |
| `robot_trend/main.py` | лог старта `tf=5 htf=30` |
| `robot_trend/.env`, `.env.example` | `HTF_TIMEFRAME=30`, `HTF_WARMUP=250`; комментарии стратегии (30м/5м); удалён `ATR_PERIOD` |
| `tests/test_robot_trend.py` | +7 тестов: валидация htf в конфиге/параметрах, `_htf_snapshot`, вход требует согласия 30м (long/short/ADX), выход без 30м, `_closed_htf` без hindsight, order_flow: передача 30м и отсрочка решения при ошибке fetch |

## Проверки
- `ruff check` по своим файлам — чисто; `ruff format` — чисто (backtest.py не форматировал:
  файл не был отформатирован и до сессии — мои строки оформлены);
  `mypy robot_trend robot_zakol core backtest.py` — 0 ошибок; `pytest` — **238 passed**.
- Живой прогон: `backtest.py --strategy trend --limit 1500 --no-chart` →
  «Старший ТФ: 30м, свечей: 300», заголовок `5/30`, сделка отработала (сигналы идут).

## Решения / компромиссы
- **30м грузится REST только в момент решения** — WS не нужен (kline 5м остаётся
  триггером пробуждения); старший ТФ не влияет на выходы/kill/cooldown.
- **Ранний бэктест**: входы возможны только после ~202 закрытых 30м свечей (~101 ч
  5м-истории) — до этого hold «мало свечей старшего ТФ».
- `HTF_WARMUP >= EMA_MACRO + 2` валидируется в `TrendParams` (иначе входов не будет).

## TODO
- [ ] Тюнинг на альтах: порог ADX 30м, длины EMA, `--htf/--timeframe` прогоны
- [ ] Проверить в live-логе вход `ST flip long, HTF ADX ..., vol ...` и BE/trail

## Сетка мульти-ТФ — ЗАВЕРШЕНА, конфиг применён (2026-10-04)

**Скрипт**: `C:\Users\79095\AppData\Local\Temp\opencode\grid_mtf.py`
(сетка: ADX {20,25,30,35} × EMA {20/50/200, 9/21/100} × ТФ {5/30, 15/60}
× 5 альтов LSKUSDT, BRUSDT, ARKMUSDT, KAVAUSDT, BANDUSDT, 90 дней, 80 прогонов,
27 мин; CSV: `data/grid_mtf_alts.csv`).

**Найденные и исправленные баги скрипта** (по ходу):
1. `FastTrend` кэшировал первый вызов `_htf_snapshot` (`[]` → `None` навсегда)
   → 0 входов. Фикс: ключ кэша = `(len(candles), candles[-1].open_time)`.
2. `cfg.symbol = sym` не выставлялся → все «альты» качали BTCUSDT из `.env`.
   Фикс: `cfg.symbol = sym` перед fetch.

**Результаты (ΣPnL по 5 альтам, 90д)**:

| ТФ | EMA | ADX | ΣPnL | Сделки | Win% | Прибыльных |
|----|-----|-----|------|--------|------|-----------|
| 15/60 | 9/21/100 | 20 | +36.7% | 97 | 52.8 | 4/5 |
| 15/60 | 20/50/200 | 20 | +35.4% | 91 | 54.8 | 4/5 |
| 5/30 | 9/21/100 | 35 | +35.1% | 140 | 37.1 | 3/5 |
| **15/60** | **20/50/200** | **25** | **+32.8%** | **65** | **58.7** | **5/5** |
| 15/60 | 20/50/200 | 30 | +28.3% | 40 | 63.1 | 4/5 |

**Вывод**: 15/60 доминирует (win 53-64% vs 35-41% у 5/30). Выбрано и
**применено в `.env` / `.env.example`**: `TIMEFRAME=15`, `HTF_TIMEFRAME=60`,
`ADX_THRESHOLD=25`, EMA 20/50/200 — единственная комбо с 5/5 прибыльных
(LSK +17.5 win 73% DD 1.7, BRU +6.7, BAND +6.2, KAVA +1.8, ARK +0.6; DD ≤5.3).

**Проверки**: ruff чисто, pytest 238 passed, смоук
`backtest.py --limit 3000` → заголовок `15/60`, «Старший ТФ: 60м» (BTC 31д:
8 сделок, −1.23% — BTC не в тюнинг-сете, механика ок).

**Замечания**:
- KAVAUSDT везде win 20-29% (плюс только за счёт редких крупных TP) —
  кандидат на исключение из ротации.
- `check_one.py` (LSK, 5/30 ADX20): 67 сделок, +22.24%, win 44.8% — референс.
- Скрипты: `grid_mtf.py`, `check_one.py`, `grid_detail.py`, `debug_mtf.py`
  во временной папке opencode.

---

# Robot Trend — Сессия 2026-10-03 — РИСК: SL 2% / TP 5% / BE + трейлинг; STOP = flat

## Параметры (по требованию)
| Параметр | Значение | Env |
|----------|----------|-----|
| SL | −2% от входа | `SL_PCT=0.02` |
| TP | +5% от входа | `TP_PCT=0.05` |
| Активация BE и трейлинга | прибыль +2% | `BE_TRIGGER=0.02` |
| Безубыток | стоп = цена входа | `BE_OFFSET=0` |
| Трейлинг | стоп = пик − 2% | `TRAIL_PCT=0.02` |

- Стоп двигается **только в сторону прибыли** (монотонность), выключается
  трейлингом `TRAIL_PCT=0` / BE `BE_TRIGGER=0`.
- `SL_ATR_MULT`/`TP_ATR_MULT` удалены; ATR (`ATR_PERIOD`) остался только
  для снапшота/Supertrend.

## Что изменено
| Файл | Изменение |
|------|-----------|
| `robot_trend/strategy.py` | `TrendParams`: `sl_pct/tp_pct/be_trigger_pct/be_offset_pct/trail_pct` вместо `sl_atr_mult/tp_atr_mult`; `_stop_take()` — проценты от close свечи сигнала |
| `robot_trend/risk.py` | `RiskDecision` + `on_price()` — BE/trail по цене (long/short, монотонно) |
| `robot_trend/state.py` | `OpenPosition`: `peak_price`, `be_active` (старый JSON читается, дефолты) |
| `robot_trend/order_flow.py` | `_update_risk()` в `_tick` после проверки «позиция жива»: серверный SL обновляется только при изменении |
| `backtest.py` | BE/trail-механика: свеча сначала «бьёт» по старому стопу, обновление — после (без hindsight); для стратегий без `params` поведение прежнее |
| `robot_trend/.env`, `.env.example` | блок «Риск» вместо ATR-множителей |
| `tests/test_robot_trend.py` | правки %-ожиданий + 5 тестов `on_price`; `CountingStrategy` — `super().__init__()` |

## Остановка = flat (та же сессия)
- `_on_shutdown()` (любой выход из `run()`: kill-switch, SIGTERM, исключение
  в цикле): закрыть позицию по рынку → `cancel_all_orders`. Если закрытие
  не подтвердилось — заявки НЕ снимаем, SL/TP остаются защитой.
- `st.kill` → фаза `STOPPED` в конце shutdown (раньше финализация ставила IDLE).

## Проверки (2026-10-03)
- `ruff check` / `ruff format --check` по своим файлам — чисто;
  `mypy robot_trend robot_zakol core` — 0 ошибок; `pytest` — **231 passed**
  (+3 shutdown-теста: закрытие/flat/неудача закрытия).

---

# Robot Trend — Сессия 2026-10-01 — СТРАТЕГИЯ

## Состав (роли по ТЗ)

| # | Индикатор | Параметры (.env) | Роль |
|---|-----------|------------------|------|
| 1 | EMA | `EMA_FAST=20`, `EMA_SLOW=50`, `EMA_MACRO=200` | направление тренда (fast>slow и цена по ту сторону macro) |
| 2 | ADX (+DI/-DI) | `ADX_PERIOD=14`, `ADX_THRESHOLD=25` | сила тренда / фильтр флэта + направление |
| 3 | Supertrend | `ST_ATR_PERIOD=10`/`ST_FACTOR=3.0` (альты); `ST_ATR_PERIOD_MAJOR=55`/`ST_FACTOR_MAJOR=2.0` (BTC/ETH) | точка входа + трейлинг-стоп |
| 4 | ATR | `ATR_PERIOD=14` | волатильность (снапшот); SL/TP теперь в % (см. сессию 2026-10-03) |
| 5 | Volume SMA | `VOL_PERIOD=20`, `VOL_MULT=1.3` | подтверждение пробоя (объём свечи ≥ 1.3×SMA20) |

- **Профиль Supertrend выбирается по SYMBOL**: BTC/ETH → 55/2.0, иначе 10/3.0
  (`TrendParams.from_env`, значения можно переопределить в `.env`).
- **Вход** — только на **флипе Supertrend** при одновременно выполненном
  фильтре EMA, ADX ≥ порога, правильном DI и объёме. Нет флипа — нет входа.
- **Выход** — разворот Supertrend против позиции; серверный SL/TP = страховка.
- **Размер позиции**: как раньше (`POSITION_PCT`).

## Что изменено
| Файл | Изменение |
|------|-----------|
| `core/indicators.py` | `adx_di()` (ADX и +DI/−DI одной проходкой, `adx()` — обёртка), `supertrend()` → `(line, direction)` |
| `robot_trend/strategy.py` | `TrendParams` (env + валидация), `_snapshot()` (EMA/ADX/ST/ATR/volume), `decide()`, `check_signal()` (buy/sell/close_*), `_min_warmup=300`, `_max_lookback=600` |
| `robot_trend/main.py` | `TrendStrategy(TrendParams.from_env(symbol))` |
| `backtest.py` | `make_strategy("trend")` → `TrendParams.from_env()` (параметры из `robot_trend/.env`) |
| `robot_trend/.env`, `.env.example` | блок «СТРАТЕГИЯ» вместо «ПОТОМ ЗАПОЛНИМ» |
| `tests/` | стаб-тесты заменены (+15): вход/выход/фильтры/профиль ST, `supertrend`, `adx_di` |

## Проверки (2026-10-01)
- `ruff check`/`ruff format --check` по своим файлам — чисто;
  `mypy robot_trend core` — 0 ошибок; `pytest` — **217 passed**.
- `python backtest.py --strategy trend --limit 1200 --no-chart` (BTCUSDT M5) —
  3 сделки, −0.91%: сигналы работают, **параметры не тюнились**.

## TODO
- [ ] Тюнинг/тест параметров на разных монетах и ТФ (backtest.py --strategy trend)
- [ ] Проверить в live-логе стоп/тейк и BE/trail (risk: ...) на реальном символе

---

# Robot Trend — Состояние на 2026-09-29 (история)

- **Каркас «технический слой» построен с нуля** (см. сессию ниже). Стратегия —
  стаб, сделок нет. Все места «ПОТОМ ЗАПОЛНИМ» помечены в коде и `.env`.
- Старый `strategy.py` (ATR + EMA + ADX + DI + VWAP + Volume + OBV + BB squeeze
  + Structure) больше не существует — заменён стабом (legacy-результаты внизу
  только для истории).
- Прошлые результаты бэктеста (2026-09-13) — только для истории.

---

# Robot Trend — Сессия 2026-09-29 (каркас без стратегии)

## Что сделано
- `robot_trend/` обнулён (`main.py`, `strategy.py`, `.env`) и перестроен в
  стиле `robot_zakol`: самодостаточный бот, живой zakol не трогали.
- Файлы:
  | Файл | Назначение |
  |------|------------|
  | `config.py` | `TrendConfig` (pydantic/env) + валидации + `validate_for_live()` |
  | `state.py` | JSON-state: позиция, kill, день/счётчики, symbol+schema |
  | `risk.py` | чистые хелперы: `entry_qty`, `slippage_ok`, `cooldown_remaining`, `daily_stop_reason`, `should_kill`, `position_pnl` |
  | `data_feed.py` | копия zakol-фида + подписка **kline** (закрытые свечи) |
  | `order_flow.py` | техника: вход Market → серверный SL/TP, выход, recover, kill-switch, гейты, дедуп свечей |
  | `strategy.py` | **СТАБ**: `check_signal()` = hold (для `backtest.py`), `decide()` = None |
  | `main.py` | вход: time-sync → фильтры → hedge-check → recover → WS → цикл |
  | `.env` / `.env.example` | только технические параметры |

## Цикл (order_flow)
1. Каждая итерация: `_roll_day` (UTC) → kill (файл + просадка сессии) →
   WS-ресинк → drain очередей → проверка «позицию не закрыла биржа?» →
   REST-свечи → **дедуп по последней закрытой свече** → `strategy.decide()`.
2. Вход: гейты `kill → день UTC → cooldown → фандинг → слippаж`,
   qty из `POSITION_PCT` (или `QTY`), Market, ожидание позиции,
   серверный SL/TP (**без SL вход запрещён**; SL не встал → аварийное
   закрытие). Позиция фиксируется в state до выставки SL.
3. Выход: Market → подтверждение → PnL (биржа, иначе расчёт) → счётчики
   дня/сессии/кулдаун.
4. Kill-switch: файл `KILL_SWITCH` или просадка (`MAX_LOSS_USD`,
   `MAX_DRAWDOWN_PCT`) → стойкий `kill=true`, фаза STOPPED, цикл завершается.
5. `recover()`: state ↔ биржа — закрытая биржей → фиксация; позиция без
   state → принимаем с её SL/TP; сироты-лимитки → `cancel_all_orders`.

## ПОТОМ ЗАПОЛНИМ (точки подключения стратегии)
1. `strategy.py::decide()` — логика входа/выхода (сейчас всегда None).
2. `.env` — блок `СТРАТЕГИЯ — ПОТОМ ЗАПОЛНИМ` (ATR/фильтры и т.п.).
3. `order_flow._on_candles()` — единственная точка вызова `decide()`
   (техника вокруг уже работает и покрыта тестами).

## Параметры (.env)
- Техника: SYMBOL/TIMEFRAME/DEPOSIT_USD/QTY/POSITION_PCT/CANDLE_WARMUP/
  POLL_SEC/ORDER_LINK_ID_PREFIX(`tr-`)/RECONNECT/HEARTBEAT/RECV_WINDOW/
  TIME_SYNC/WS_ENABLED.
- Защита: KILL_SWITCH=`data/trend.kill`, MAX_LOSS_USD=10,
  MAX_DRAWDOWN_PCT=0.05, SLIPPAGE_PCT=0.005, COOLDOWN_SEC=15,
  DAILY_LOSS_LIMIT=0 (выкл), MAX_TRADES_PER_DAY=0 (выкл),
  FUNDING_AWARE=true.

## Проверки (2026-09-29)
- `ruff check/format` — чисто; `mypy robot_trend robot_zakol core` — 0 ошибок;
  `pytest` — **202 passed** (+48 новых `tests/test_robot_trend.py`).
- `python backtest.py --strategy trend --limit 300` — не падает, 0 сделок
  (стаб hold), `load_config().validate_for_live()` — OK.

---

# Robot Trend — Состояние на 2026-09-25

- **Робот обнулён.** Новая стратегия ещё не написана.
- Старый `strategy.py` (ATR + EMA + ADX + DI + VWAP + Volume + OBV + BB squeeze + Structure) — legacy, не используется как основа, пока не напишем новую.
- Прошлые результаты бэктеста (2026-09-13) ниже — только для истории.

---

# Robot Trend — Сессия 2026-09-13

## Стратегия: TrendStrategy
- Файл: `robot_trend/strategy.py`
- Индикаторы: ATR + EMA(9/21/200) + ADX + DI + VWAP + Volume + OBV + BB Squeeze + Structure(HH/HL)
- SL/TP от ATR

## Лучшие параметры (.env)
```
ADX_MIN=22
ATR_SL_MULT=2.0
ATR_TP_MULT=3.0
VOLUME_MULTIPLIER=1.0
```

## Результаты бэктеста (5000 свечей, 5m)

| Монета | Сделок | Win Rate | PF | PnL | Max DD |
|--------|--------|----------|-----|------|--------|
| BTCUSDT | 42 | 50.0% | 1.71 | +4.35% | 3.04% |
| PUMPFUNUSDT | 13 | 46.2% | 1.34 | +2.02% | 2.62% |
| STEEMUSDT | 33 | 48.5% | 1.29 | +2.65% | 2.26% |
| CVCUSDT | 24 | 29.2% | 0.71 | -2.70% | 5.05% |

## Торговать: PUMPFUNUSDT и STEEMUSDT (не из топ-15, оборот >$30M, цена <$1)

## TODO
- [ ] Добавить трейлинг-стоп
- [ ] Добавить time-exit (закрытие через 60 мин)
- [ ] Протестировать на других ТФ (1m, 15m)
