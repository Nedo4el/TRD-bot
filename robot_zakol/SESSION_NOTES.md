# Robot Zakol — Сессия 2026-09-23

## Стратегия (итоговое состояние)
- TTL-лимитка **PostOnly buy @ −3%**, TTL 20с, re-place по min_price_change 0.3%
- **Только buy** (TP удалён)
- Выход: trail 2% от peak, BE trigger +0.5% / offset +0.2%, SL −2%
- Kill: **MAX_DRAWDOWN_PCT = 5%** от пика сессии (peak_session_pnl)
- Partial ≥80%, max concurrent 1, notional **$10** (POSITION_PCT=10% × депо $100)

## Файлы
- Live: `robot_zakol/{config,state,data_feed,risk,order_cycle,main}.py`
- Backtest: `robot_zakol/backtest/{config,data_loader,fill_model,strategy,backtest,metrics,report,__main__}.py`
- `.env` / `.env.example` — параметры

## Запуск бэктеста
```bash
.\.venv\Scripts\python.exe -m robot_zakol.backtest \
  --symbol BRUSDT --start-date 2026-03-20 --end-date 2026-08-07 \
  --deposit 100 --interval 1 --report reports/zakol_honest_br_0320_0807.txt
```
Режимы: ideal / realistic / pessimistic (fill-модель). Fidelity: 1S synthetic OHLC (60 sub-ticks/bar).

## Результаты сессии

### Scan 3 монеты (turnover ≥$50M, вне топ-15, 30d 24.08–22.09)
| Symbol | realistic trades | sum$ | Вердикт |
|--------|-----------------:|-----:|---------|
| ENAUSDT | 1 | −0.20 | нет входов (−3%/20с почти не бывает) |
| ADAUSDT | 0 | 0 | нет входов |
| AVAXUSDT | 0 | 0 | нет входов |

**Вывод:** turnover ≠ vol. Отбирать по **hit −3%/20s** (или p99 M1 range ≥3%).

### BRUSDT 20.03–07.08 (~140d) — ✅ лучший
| mode | trades | WR | PF | sum$ | maxDD% | kills |
|------|-------:|---:|---:|-----:|-------:|------:|
| ideal | 476 | 43.7% | 1.54 | +27.95 | 2.42 | 0 |
| realistic | 456 | 42.1% | 1.50 | **+26.24** | 2.57 | 0 |
| pessimistic | 456 | 42.1% | 1.45 | +24.01 | 2.67 | 0 |

- Equity $126, gap ideal→real −6% (queue_rej=136, po_rej=8)
- hit −3%/20s = **0.083%**, chg period +101%, bar p99=2.30%

### AKEUSDT 26.07–14.09 (~50d) — ❌ DD-kill
| mode | trades | WR | sum$ | kills |
|------|-------:|---:|-----:|------:|
| ideal | 716 | 44.4% | **+47.90** | 0 |
| realistic | 44 | **6.8%** | **−2.57** | **1 (DD-kill)** |
| pessimistic | 38 | 5.3% | −2.62 | 1 |

- queue_rej=**753** → adverse fill → серия SL (hold 1–5s, sl=93%) → kill на 5% DD
- hit −3%/20s = **0.332%** (выше BTR), chg +421%, bar p99=4.27%
- **Ловушка идеального бэктеста:** ideal +$48, честный fill −2.6$

### Spike scan AKEUSDT 3 дня (21–23.09)
- **123 спайка** ≥3% за ≤20с: LONG 70 / SHORT 53
- Время до 3%: p50=**15с**, p90=**20с** (граница TTL)
- До 50% ретрейса: p50=62с, p90=95с
- По дням: **21.09=109**, 22.09=12, 23.09=**2**
- По часам UTC: пик 02:00–13:00, max hour 05 (26)
- 23.09 по МСК: **04:29:52 SHORT −3.08%**, **04:31:53 LONG +3.24%**

## Настройки сессии
- **Время всегда показывать по МСК (UTC+3)** в ответах/отчётах (записано в `AGENTS.md`)
- В коде/логах хранить UTC, при выводе конвертировать в МСК
- Коммит — только по явной команде; `.env` в .gitignore

## TODO
- [ ] BRUSDT дописать отчёт `zakol_honest_br_0320_0807.txt` (abort при re-run)
- [ ] Для live: отбор монет по hit −3%/20s, не по turnover
- [ ] AKE: проверить queue/slippage на publicTrade (сейчас synthetic 1S)
- [ ] Рассмотреть OFFSET −1…1.5% для liquid majors (иначе 0 fills)

---

# Сессия 2026-09-25 — ПЕРЕПИСЫВАНИЕ: брекет ±3% (long + short + TP)

## Что сделано
1. **`.env` обнулён** (старые параметры стёрты), затем задиктованы новые:
   - `OFFSET_PCT=0.03` — лимитки **одновременно** buy −3% и sell +3% (PostOnly)
   - `TTL_SEC=20`, `MIN_PRICE_CHANGE=0.003` (0.3% — только лог extend/re-place)
   - `STOP_PCT=0.03` (−3% от входа), **`TAKE_PCT=0.05` (+5% от входа)** — новый параметр
   - `POSITION_PCT=100` → ордер **$100** (=100% депо)
   - Нули = выключено: TRAIL, BE, MAX_LOSS, MAX_DRAWDOWN (kill OFF), PARTIAL=0 (открытие при первом филле)
   - `MAX_CONCURRENT=0` → **live заблокирован валидацией** («должен быть >= 1») — выставить 1 перед запуском
2. **Код переписан** (live + бэктест, 82 теста, ruff/mypy зелёные):
   - `order_cycle._place_bracket()`: две лимитки, fill любой стороны → отмена второй → позиция side=long/short
   - SL/TP side-aware, серверный `take_profit` передаётся; pnl для шорта зеркальный
   - `risk.py`: trail/BE/kill **выключаются при 0** (раньше trail=0 ломал стоп)
   - Бэктест берёт параметры из `.env` (`strategy_from_zakol`) — единый источник
   - `state.py`: `pending_buy` + `pending_sell` (старый JSON с `pending` игнорируется — чистый старт)
3. **Найден и исправлен баг** в `fill_model.on_tick`: инвертировано условие достижения уровня
   (buy филлился когда цена ВЫШЕ цели → фантомные +1798$). +2 регрессионных теста.

## Результат: SAGAUSDT 26.08–25.09 (30d), ордер $100
Отчёт: `reports/zakol_saga_bracket_0826_0925.txt`
| mode | trades | WR | PF | sum$ | maxDD% | tp/sl |
|------|-------:|---:|---:|-----:|-------:|-------|
| ideal | 173 | 42.8% | 1.24 | +72.21 | 49.8 | 43/57 |
| realistic | 156 | 41.7% | 1.17 | +47.05 | **70.8** | 42/58 |
| pessimistic | 154 | 42.2% | 1.18 | +48.58 | 61.4 | 42/58 |

- **Прибыль есть, но DD 50–70%** (ордер=100% депо, серия SL в тренде, kill off)
- Сравнение: старая buy-only стратегия на SAGA (ордер $10): +5.60$ → ×10 ≈ **+56$ при DD ~24%**
  → **старая лучше по risk/reward** (trail + слабее стоп). Новая просто перекрывает ±3% рывки.
- Идеи: вернуть trail/BE, POSITION_PCT 10–20%, включить MAX_DRAWDOWN kill, SL/TP асимметрия

## TODO (с этой сессии)
- [ ] Выставить `MAX_CONCURRENT=1` в `.env` для live
- [ ] Решить: trail/BE/kill включить? POSITION_PCT=100% слишком агрессивно?
- [ ] Прогнать новую стратегию на BRUSDT/AKE для сравнения со старой
- [ ] Live-запуск: отбор монет по hit −3%/20s (SAGA — кандидат, 390 спайков/мес)

---

# Сессия 2026-09-26 — LIVE-БЛОКЕРЫ ЗАКРЫТЫ (без реальных ордеров)

Ни одного реального ордера не выставлено — только код + тесты (117 passed, ruff/mypy зелёные).

## Закрытые пункты аудита
| # | Что | Где |
|---|-----|-----|
| а.1 | `MAX_CONCURRENT=1` в `.env` (валидация `< 1` оставлена строгой) | `.env` |
| а.2 | Отмена невостребованных waiter'ов после `asyncio.wait` — события филла больше не теряются | `order_cycle._watch_until_fill_or_deadline` |
| а.5 | `run()` в `try/finally` → `_on_shutdown`; `store.save()` после **первого** ордера; `main._amain` в `try/finally` + `_drain(runner)` | `order_cycle.run`, `main._amain/_drain` |
| а.3 | REST-фолбэк `get_order` в `_on_ttl`; сверка `get_open_orders` перед брекетом (`_cancel_stray_orders`) | `order_cycle` |
| б.3 | При неудачном снятии противоположной лимитки — перестановка с `reduceOnly=True` (не может перевернуть позицию) | `_guard_reduce_only` |
| б.4 | Любой `filled_qty > 0` к TTL = позиция со SL (как в бэктесте); `.env.example: PARTIAL_FILL_PCT=0` | `_on_ttl`, `_on_filled` |
| б.1 | Shutdown: cancel stopper → `_drain(runner)` → `feed.stop()` → `client.close()` → `store.save()` | `main._amain` |
| б.5 | `_enter_stopped` больше **не** зовёт `cancel_all_orders` (спасает SL/TP) + переустановка SL/TP; `recover()` при `kill` → `STOPPED`; фоновый kill по equity/DD в `_manage_position` | `order_cycle` |
| б.6 | `recover()` всегда заново ставит серверный SL/TP | `recover` |
| б.8 | `_size_gone` с допуском `qty_step/2`; pnl `exchange_exit` и закрытия вне бота — из `get_closed_pnl` (REST), не по текущей цене | `_size_gone`, `_exit_pnl`, `recover` |
| в.1 | Флаг `feed.resync_needed` после реконнекта WS → `_maybe_resync()` → `recover()`; нет ключей → алерт, не тихий skip | `data_feed`, `order_cycle` |
| в.2 | Подписки `wallet_stream` + `position_stream`, `feed.last_balance`; баланс реально используется: фоновый kill по equity (см. ниже) | `data_feed`, `order_cycle` |
| в.4 | `Notifier` инжектится в `OrderCycle` + `DataFeed`; алерты на fill / kill / cancel-ошибку / исключение в `run()` | `main`, `order_cycle` |
| в.6 | ~25 новых тестов на горячие пути (`_watch_*`, `_on_ttl`, TTL re-place, resync, shutdown, reduceOnly, partial) | `tests/test_robot_zakol.py` |

## Новые параметры конструктора `OrderCycle`
`qty_step` (допуск размера позиции) и `notifier` — передаются из `main.py` (фильтры инструмента уже запрашивались).

## Kill по балансу счёта (в.2, добивка)
`_maybe_equity_kill()` в каждой итерации `run()`:
- equity из `feed.last_balance` (wallet stream), фолбэк `get_balance()` не чаще раза в 30с;
- `st.start_equity` / `st.peak_equity` сидируются при первом наблюдении (`state.py`, JSON-совместимо);
- `should_kill(equity-start, peak-start, deposit=start)` → `_trigger_kill("equity")`;
- выключен целиком, если `MAX_LOSS_USD=0` и `MAX_DRAWDOWN_PCT=0`; **в `.env` теперь 10 / 0.05 — включён**.
Позиция контролируется и раньше: REST-снапшот раз в секунду в `_manage_position`.
+4 теста: сидирование, kill по DD, выключенные лимиты, REST-фолбэк.

## Вторая волна — доработки по престарт-чеклисту (2026-09-26)
| # | Что сделано | Где |
|---|-------------|-----|
| B.5 | `take_pct=0` → отправляем явный `takeProfit=0` (снятие TP); при отказе — повтор только со SL | `bybit_client.set_stop_loss_take_profit`, `_apply_server_sl_tp` |
| D.4 | `_cancel_pending` **не чистит state** при неудаче; сверка `_order_is_open`; неснятая сторона остаётся в state после филла | `order_cycle._cancel_pending`, `_handle_filled` |
| D.5 | `state.symbol` + `schema_version` + `check_symbol()` (другой символ / схема новее кода → ValueError); запись при `save()` | `state.py`, `main.py` |
| E.1 | `_log_task_result` (done-callback) + лог исключений waiter'ов | `order_cycle` |
| E.3 | backoff 1→60с при неудачном cancel на TTL, фаза остаётся WORKING | `_on_ttl` |
| E.4 | `asyncio.Lock` на `_on_filled` (WS-таск vs `_on_ttl`) | `order_cycle` |
| F.3 | `MIN_PRICE_CHANGE` реально влияет: цена молчит → **extend** (те же лимитки, TTL продлён); иначе re-place | `_on_ttl` |
| A.3 | валидатор `POSITION_PCT ∈ (0, 100]` | `config.py` |
| 0.3 | биржа: equity **142.47 USDT** ≥ депо 100 | (read-only вызов) |

Биржевые фильтры BTCUSDT: tick 0.1, qty_step 0.001, **min_qty 0.001 BTC ≈ $84**, min_notional 5 USDT —
ордер $1 на BTCUSDT невозможен.

## Третья волна — A.2, A.5, SYMBOL=QUSDT (2026-09-26)
| # | Что сделано | Где |
|---|-------------|-----|
| 0.5 | `SYMBOL=QUSDT`; state хранит symbol и сверяет при старте | `.env`, `state.check_symbol` |
| A.2 | `validate_for_live` теперь требует `WS_ENABLED=true` (тестнет — warning); на старте `client.get_position_mode()` → **hedge = старт отменяется**, unknown = warning | `config.py`, `bybit_client.get_position_mode`, `main._amain` |
| A.5 | `feed.start()` → ждём `feed.is_connected()` до 10с (`WS_CONNECT_TIMEOUT`), иначе RuntimeError; флаги `_public_up/_private_up` в воркерах | `main._wait_ws_connected`, `data_feed` |
| — | `POSITION_PCT=6` → ордер **$6** (для QUSDT минимум: qty_step 10 × 0.034 ≈ $5, min_notional 5) | `.env` |
| — | Telegram/trail/BE — по решению пользователя **выключены** | `.env` |

Фильтры QUSDT: tick 1e-05, qty_step **10**, min_qty 10, min_notional 5 USDT, цена ~0.034.

## Live-проверка на QUSDT — тест-ордера и запуск (2026-09-27)
- Тест-ордера (реальные, оба сняты/отклонены):
  - «$1» = 30 QUSDT @ 0.03304 → **REJECTED `110094 minimum order value 5USDT`**: minNotionalValue=5 считается как `qty × цена лимитки`;
  - 160 QUSDT @ 0.03304 = **$5.29 → ACCEPTED**, снят.
  - В UI Bybit $1 «работает», потому что поле в USDT — это маржа: $1 × 10x = $10 номинала.
- `validate_for_live`: `CATEGORY` должен быть `linear` — **спот запрещён** (+тест `test_validate_for_live_rejects_spot`).
- Баг: цикл логировал/сохранял сырой qty (175.9), клиент же округлял вниз до `qtyStep` (170) → `_order_qty` теперь делает floor до `qty_step` и падает, если меньше шага (+2 теста).
- Live-запуск (окно терминала): `robot_zakol started symbol=QUSDT`, бракет Buy/Sell 170 QUSDT (notional $5.66/$6.01), TTL extend ×3 → re-place при delta>0.3%, позиций нет.
- Итог: **135 passed**, ruff/mypy чисто; в логе бота время UTC (06:46 UTC = 09:46 МСК).

## Что осталось (не входило в план)
- [ ] `get_executions(since=...)` в ресинке (сейчас: `get_open_orders` + `get_position` + `get_closed_pnl`)
- [ ] Хедж-режим: `positionIdx` в `set_stop_loss_take_profit` жёстко `0` (one-way)
- [ ] Утечка `feed.prices` в IDLE/WORKING (очередь растёт) — из аудита, не в плане
- [ ] Отбор монет по hit −3%/20s, отчёт по BRUSDT — TODO из 23–25.09
- [ ] **Per-symbol OFFSET — не нужен:** решили «для всех один» (TODO закрыт)
- [ ] `bot_screener_yrovni/scanner.py:137` — **синтаксическая ошибка блокирует `mypy .`** (не трогал, не мой файл)
- [ ] **Алерты не трогал по заказу:** в `robot_zakol/.env` нет `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` → только лог; уровень не везде ERROR (fill=INFO, cancel-fail=WARNING, kill/исключение=ERROR)

## Доработки по аудиту надёжности (2026-09-29) — ✅ реализовано в тот же день
См. раздел ниже «Сессия 2026-09-29»: RECONNECT/HEARTBEAT, RECV_WINDOW+TIME_SYNC,
KILL_SWITCH, ORDER_LINK_ID_PREFIX, FUNDING_AWARE + новые SLIPPAGE/COOLDOWN/DAILY.

---

# Сессия 2026-09-29 — НАДЁЖНОСТЬ: 8 фич, все зелёные

**Проверки:** `ruff check` + `ruff format --check` + `mypy robot_zakol core` чисто,
**154 passed**. (`mypy .` по-прежнему блокируется `bot_screener_yrovni/scanner.py:137`
— синтаксис, не наш файл.) Live-ордера не выставлялись.

## Что добавлено (всё из `.env`, ничего не захардкожено)

| Параметр | Как работает | Где |
|----------|--------------|-----|
| `SLIPPAGE_PCT=0.005` | после cancel-раунда сверяем тик фида с ценой расчёта: ушло дальше допуска → лимитки считаются по свежей цене (0 = выкл). Маркет-выхода в боте нет (выходы серверные) — `risk.slippage_ok()` готов для него | `order_cycle._fresh_price`, `risk.slippage_ok` |
| `COOLDOWN_SEC=15` | после каждого закрытия (стоп/тейк/exchange) пауза перед новым брекетом; таймер `state.last_close_at` (unix, переживает рестарт) | `_entry_blocked`, `risk.cooldown_remaining` |
| `DAILY_LOSS_LIMIT=0` / `MAX_TRADES_PER_DAY=0` | дневной стоп-кран: блокирует **входы**, бот не останавливает, позицию не трогает; счётчики `day/day_pnl/day_trades` (граница суток — **UTC**), сбрасываются в `_roll_day` | `_entry_blocked`, `risk.daily_stop_reason` |
| `RECONNECT_SEC=2` | база экспоненциального backoff WS (потолок max(base,60)); `HEARTBEAT_SEC=1` — период проверки `is_connected()` (был хардкод 1с) | `data_feed` |
| `RECV_WINDOW=10000` / `TIME_SYNC=true` | окно подписи в `.env` (был хардкод в 2 местах); сверка `/v5/market/time` на старте + при retCode 10002 → сдвиг применяется подменой `pybit._helpers.generate_timestamp` (лечит 10002, а не только репортит). Рекурсия защищена флагом `_syncing_time` | `core/bybit_client.sync_time/_is_time_error` |
| `KILL_SWITCH=data/zakol.kill` | создать файл → `state.kill` → STOPPED (в любой фазе, проверка в начале итерации `run()`); позиция остаётся под серверным SL/TP. Пустое значение = выкл. | `order_cycle._kill_switch_hit` |
| `ORDER_LINK_ID_PREFIX=zk-` | префикс `orderLinkId` (валидация длины 1..17: Bybit лимит 36 = префикс + 16 hex + "-ro") | `order_cycle._order_link_id` |
| `FUNDING_AWARE=true` / `FUNDING_WINDOW_SEC=60` | перед входом — `get_tickers.nextFundingTime`; в окне ≤N сек до фандинга вход откладывается (проверка только в IDLE, открытую позицию не трогает) | `order_cycle._funding_left` |

## Поведение гейта входа (порядок в `_entry_blocked`)
`kill-switch файл` (в начале `run()`) → `день UTC` → `daily stop` → `cooldown` → `фандинг`.
Каждый запрет логируется/уведомляется **один раз** (при смене причины), цикл спит ≤5с.

## State (JSON) — новые поля, миграция не нужна (старые файлы читаются)
`last_close_at`, `day`, `day_pnl`, `day_trades` (`state.py`, `SCHEMA_VERSION` не менялся:
все поля с дефолтами).

## Тесты (новые, ~20)
slippage/cooldown/daily-стоп (математика + гейт), roll-day, close → счётчики дня,
kill-switch через `run()` (STOPPED, ордеров нет), funding-окно, пересчёт лимиток по
slippage + префикс link-id, `_is_time_error`, `sync_time` со сдвигом,
`_amain` → time_sync + WS-параметры, `DataFeed` reconnect/heartbeat.

## Решения/компромиссы
- **Дневной кран считает закрытия, не открытия** (сделка = закрытая позиция).
- **Дневная граница UTC** (в ответах/отчётах показывать МСК — правило AGENTS.md).
- **Сдвиг часов патчит приватный helper pybit** — единственный способ лечить 10002
  без своего REST-клиента; идемпотентно, включается только при `TIME_SYNC=true`.
- Значения в `.env`: cooldown 15с включён; `DAILY_LOSS_LIMIT`/`MAX_TRADES_PER_DAY`
  оставлены 0 (выкл) — числа должен выбрать владелец.
- Маркет-выхода в боте нет → `SLIPPAGE_PCT` работает на re-place; при добавлении
  маркет-выхода использовать `risk.slippage_ok()`.

## Осталось (не входило в план)
См. список «Что осталось (не входило в план)» в разделе от 27.09 — ничего не закрыто
и ничего не добавлено.

---

# Сессия 2026-10-03 — LONGXIAUSDT: только лонг, лимитка −7%

## Параметры (.env)
| Параметр | Было | Стало |
|---|---|---|
| `SYMBOL` | QUSDT | **LONGXIAUSDT** (~$0.045, qty_step 1, min_notional 5, tick 0.0001) |
| `LONG_ONLY` | — | **true** (новый параметр) |
| `OFFSET_PCT` | 0.03 | **0.07** (buy −7%, sell больше не ставится) |
| `STOP_PCT` | 0.03 | 0.03 (без изменений) |
| `TAKE_PCT` | 0.05 | **0** (фикс. тейк выкл) |
| `TRAIL_PCT` | 0 | **0.02** (трейлинг-тейк 2% от пика) |
| `BE_TRIGGER` / `BE_OFFSET` | 0 / 0 | **0.01 / 0** (безубыток после +1% = цена входа) |
| `POSITION_PCT` | 6 | 6 ($6 ≥ min_notional) |

## Код
- `LONG_ONLY=true`: `_place_bracket()` ставит только buy-лимитку (order_cycle);
  бэктест — `_place()` не ставит `pending_sell` (backtest/config.py, backtest.py).
- Выходы: серверный SL −3%, BE +1% → стоп на входе, трейлинг 2% ниже пика; TP выкл.
- Проверки: `ruff`/`mypy robot_zakol core` чисто, **219 passed** (+2:
  `test_place_bracket_long_only`, `test_backtest_long_only_never_opens_short`).

## Остановка = flat (2026-10-03)
- `_on_shutdown()` (любой выход из `run()`: kill-файл, SIGTERM, исключение):
  снять входные лимитки → закрыть позицию по рынку → `cancel_all_orders`.
  Если закрытие не подтвердилось за 10с — вернуть серверный SL/TP,
  заявки не снимаем (SL/TP остаётся защитой на бирже).
- `_enter_stopped()` больше не переустанавливает SL/TP (фолбэк теперь в
  `_shutdown_close`); `_close_position()` не дублирует KILL, если `kill` уже
  стоит (иначе был второй ERROR-лог при stop после kill-файла).
- Хелперы: `_shutdown_close`, `_wait_closed` (poll позиции), `_cancel_rest`.
- `_cfg()` в тестах задаёт `kill_switch_file=""` — тесты не зависят от
  реального `data/zakol.kill` на диске (иначе `run()` падал в STOPPED).
- Проверки: `ruff`/`mypy` чисто, **231 passed** (+3 shutdown-теста,
  `test_enter_stopped_keeps_sl_tp` → `test_enter_stopped_cancels_pending_only`).

## TODO
- [ ] Live-запуск на LONGXIAUSDT: сверить фильтры инструмента, qty floor, мин. notional
- [ ] Отбор монет по hit −3%/20s / бэктест новой сетки (−7%) на LONGXIA и старых кандидатах
