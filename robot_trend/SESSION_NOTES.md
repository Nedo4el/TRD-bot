# Robot Trend — Состояние на 2026-10-01

- **Стратегия написана** (см. «Сессия 2026-10-01»): EMA + ADX/DI + Supertrend +
  ATR + Volume, все параметры в `.env`.
- Каркас технического слоя (сессия 2026-09-29) — без изменений.
- Результаты бэктеста старых стратегий (2026-09-13) — только для истории.

---

# Robot Trend — Сессия 2026-10-01 — СТРАТЕГИЯ

## Состав (роли по ТЗ)

| # | Индикатор | Параметры (.env) | Роль |
|---|-----------|------------------|------|
| 1 | EMA | `EMA_FAST=20`, `EMA_SLOW=50`, `EMA_MACRO=200` | направление тренда (fast>slow и цена по ту сторону macro) |
| 2 | ADX (+DI/-DI) | `ADX_PERIOD=14`, `ADX_THRESHOLD=25` | сила тренда / фильтр флэта + направление |
| 3 | Supertrend | `ST_ATR_PERIOD=10`/`ST_FACTOR=3.0` (альты); `ST_ATR_PERIOD_MAJOR=55`/`ST_FACTOR_MAJOR=2.0` (BTC/ETH) | точка входа + трейлинг-стоп |
| 4 | ATR | `ATR_PERIOD=14`, `SL_ATR_MULT=2.0`, `TP_ATR_MULT=3.0` | стоп/тейк: SL = цена ∓ 2×ATR, TP = цена ± 3×ATR |
| 5 | Volume SMA | `VOL_PERIOD=20`, `VOL_MULT=1.3` | подтверждение пробоя (объём свечи ≥ 1.3×SMA20) |

- **Профиль Supertrend выбирается по SYMBOL**: BTC/ETH → 55/2.0, иначе 10/3.0
  (`TrendParams.from_env`, значения можно переопределить в `.env`).
- **Вход** — только на **флипе Supertrend** при одновременно выполненном
  фильтре EMA, ADX ≥ порога, правильном DI и объёме. Нет флипа — нет входа.
- **Выход** — разворот Supertrend против позиции; серверный SL/TP = страховка.
- **Размер позиции**: как раньше (`POSITION_PCT`), ATR влияет только на стоп/тейк.

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
- [ ] Проверить в live-логе стоп/тейк от ATR на реальном символе

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
