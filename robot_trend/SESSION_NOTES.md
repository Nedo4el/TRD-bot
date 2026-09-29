# Robot Trend — Состояние на 2026-09-29

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
