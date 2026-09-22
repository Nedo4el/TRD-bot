# TZ — Robot Grid Flat (нейтральная сетка вокруг POC)

Алгоритмическое задание. Цель: имплементация в `robot_grid_flat/`
(`strategy.py` — чистая логика, `main.py` — исполнение I/O).

---

## 1. Техническое задание (с формулами)

### 1.1 Идея

Альткоин-перпетуал в боковике: цена колеблется вокруг POC (Point of Control —
цена с максимальным объёмом за окно). Ставим **симметричную сетку лимитных
ордеров** вокруг POC: ниже POC — лонги, выше — шорты. Каждый уровень:
вход по лимиту, выход по TP на соседнем уровне. Риск ограничивается
экспозицией, стопом сетки и kill-switch.

Требование к бирже: **hedge mode** (позиции long и short живут одновременно,
`positionIdx` 1/2).

### 1.2 POC и Value Area (Volume Profile)

Окно: `POC_WINDOW_MIN=180` минут (на `TIMEFRAME=5` — 360 свечей),
`POC_BINS=100` бинов.

```
range   = [min(low_i), max(high_i)] по окну
bucket  = (high_global - low_global) / POC_BINS
```

Каждая свеча делит свой объём **поровну** по бинам, которые пересекает её
диапазон `[low_i, high_i]`:

```
start_i = floor((low_i  - low_global) / bucket)
end_i   = floor((high_i - low_global) / bucket)
volume[b] += volume_i / (end_i - start_i + 1)   для b ∈ [start_i, end_i]
```

```
POC = low_global + (argmax_b volume[b] + 0.5) * bucket
```

Value Area (70% объёма, `VALUE_AREA_PCT=70`) — расширяем от бина POC в сторону
бо́льшего соседнего объёма, пока `area ≥ 0.70 * total`:

```
target = 0.70 * total
lo = hi = poc_idx;  area = volume[poc_idx]
while area < target and (lo > 0 or hi < BINS-1):
    if volume[hi+1] >= volume[lo-1]:  hi += 1; area += volume[hi]
    else:                             lo -= 1; area += volume[lo]
VAL = low_global + lo * bucket
VAH = low_global + (hi + 1) * bucket
```

### 1.3 ATR и границы коридора

`ATR = ATR(14)` по свечам `TIMEFRAME` (Wilder).

```
lower = VAL - ATR_EXTEND_MULT * ATR        # ATR_EXTEND_MULT = 1.0
upper = VAH + ATR_EXTEND_MULT * ATR
```

Границы коридора — зона добора: входы только внутри `[lower, upper]`.

### 1.4 Шаг сетки

```
step_raw = max( 0.3 * ATR,                  # GRID_STEP_ATR_MULT
                 3 * tickSize,               # MIN_TICK_MULT
                 3 * fee_taker * POC )       # шаг окупает round-trip taker
step = ceil_to_tick(step_raw, tickSize)
```

Комиссионное ограничение — приоритетное: `step ≥ 3 * fee_taker * POC`
(0.00055*3 ≈ 0.165% на цену) гарантирует, что TP на один уровень выше входа
покрывает обе комиссии.

### 1.5 Уровни

```
k ∈ Z,  k ≠ 0,  price_k = round_to_tick(POC + k*step)
lower - tick/2 ≤ price_k ≤ upper + tick/2
```

- long-сторона: `price_k < POC`; short-сторона: `price_k > POC`; уровня
  ровно на POC нет.
- Число уровней `n ∈ [LEVELS_MIN=10, LEVELS_MAX=50]`:
  - `n > MAX` → увеличиваем step до `ceil_to_tick((upper-lower)/(MAX-1))`;
  - `n < MIN` → уменьшаем step до `max(range/(MIN-1), fee-floor)`;
  - если fee-floor не даёт 10 уровней — берём сколько есть
    (приоритет: защита от убытка на комиссии > MIN).
- Отсортированы по удалённости от POC (заполняем от центра).

### 1.6 Размер уровня (риск-менеджмент)

Сеточный стоп (см. 1.9):

```
SL_long  = lower - grid_sl_atr_mult * ATR       # grid_sl_atr_mult = 1.0
SL_short = upper + grid_sl_atr_mult * ATR
```

Худшее расстояние до стопа (для размера берём консервативное — от POC):

```
worst_dist = max( (POC - SL_long)/POC , (SL_short - POC)/POC )
```

```
per_risk      = (equity * RISK_PER_TRADE_PCT/100) / worst_dist   # 1% на уровень
per_exposure  = (equity * MAX_EXPOSURE_PCT/100) / n              # 20% всего
per_cap       = ORDER_SIZE_CAP_USD   если > 0 иначе ∞            # явный потолок
per_leverage  = equity * LEVERAGE_CAP / n                        # 3x от депо

per_level_usd = min(per_risk, per_exposure, per_cap, per_leverage)
```

Стоп-гейт построения: `per_level_usd ≥ max(minNotional, minQty * upper)` —
иначе «сетка не построена: депозита не хватает».

Количество актива на уровень:

```
qty = floor( (per_level_usd / price) / qtyStep ) * qtyStep
```

### 1.7 Вход

- Только **post-only limit** (`timeInForce=PostOnly`) — бот исключительно
  maker; цена уровня строго ниже bid (для лонга) / выше ask (для шорта).
- Объём: `qty` из 1.6.
- Одна лимитка на уровень; уровень занят, пока позиция не закрыта по TP.
- `MAX_OPEN_LEVELS=10` исполненных позиций на сторону: при достижении —
  все входящие ордера этой стороны отменяются.

### 1.8 Выход (TP)

На каждую исполненную позицию — **limit TP** на один шаг к POC:

```
Buy:  TP = entry + step - 2 * fee_maker * entry
Sell: TP = entry - step + 2 * fee_maker * entry
```

`fee_maker=0.0002`, буфер 2*maker ≈ 0.04% закрывает вход+выход как maker
(step уже ≥ 3*taker*price ≈ 0.165% → нетто > 0). Округление до tick —
в `place_order`. `reduceOnly=true`.

### 1.9 Выход (сеточный стоп)

Один **stop-market conditional** на сторону (закрывает все позиции стороны):

```
long-позиции:  side=Sell, trigger = lower - 1 * ATR   (SL_long)
short-позиции: side=Buy,  trigger = upper + 1 * ATR   (SL_short)
qty            = суммарная позиция стороны
triggerBy      = MarkPrice, reduceOnly = true
```

Срабатывание стопа ⇒ полный сброс сетки (`reset`): отмена всех ордеров,
закрытие всего, новый цикл построения.

Дублируется в логике `GridStrategy._check_grid_sl` (решение принимается по
цене свечи, стоп-ордер — страховка на бирже).

### 1.10 Re-center (динамика сетки)

Перестроение (отмена всех входов, новый POC/шаг/уровни, входы заново) по
триггерам:

| Триггер | Условие |
|---|---|
| Выход за VA | цена > VAH или < VAL |
| Сдвиг POC | `\|POC_new − POC_old\| / POC_old > 0.5%` (`POC_RECENTER_PCT`) |
| Планово | каждые `RECENTER_INTERVAL_S=1800` с |

Ограничение: не перестраиваемся, пока POC «нестабилен» — сдвиг
`> poc_unstable_pct=0.5%` за `poc_unstable_bars=3` бара (иначе паника на
шуме). Во время нестабильности держим текущую сетку.

Перестроение не трогает исполненные позиции: старые TP остаются, новые
уровни достраиваются вокруг.

### 1.11 Гарды входа (не блокируют выходы)

| Гард | Условие | Действие |
|---|---|---|
| Ручная пауза | файл `robot_grid_flat/PAUSE` существует | пауза входов |
| Новости | `now ∈ NEWS_WINDOWS` (CPI/FOMC/листинг ±15 мин) | пауза входов |
| Импульс | `\|close[-1] − close[-1−5]\| / close[-1−5] ≥ 5%` | пауза `30` баров |
| Funding | `\|funding\| > 0.05%` | пауза входов |
| Спред | `(ask−bid)/mid > 0.05%` | пауза входов |
| Глубина | стакан ±0.1% < `5 × per_level_usd` | пауза входов |
| Тренд (anti-martingale) | EMA50/EMA200 расходятся > 2% **против** открытых позиций | доборные входы стороны отключены |

### 1.12 Kill-switch и трейлинг

```
# kill-switch: убыток за 24ч
pnl_24h = (equity_now − equity_24h_ago) / equity_24h_ago * 100
pnl_24h ≤ −5%   →  halt (закрыть всё, остановить процесс)
```

```
# трейлинг профита (после +3% к депозиту сессии)
gain = (equity − session_base) / session_base * 100
gain ≥ 3%  →  trailing_armed = true
пока armed: giveback = (peak − equity) / peak * 100
giveback ≥ 1%  →  reset (зафиксировать, закрыть, новая сессия)
```

`session_base` фиксируется при построении/сбросе сетки.

### 1.13 Синхронизация с биржей (fill-sync)

Каждый цикл: `get_open_orders` → ордер уровня пропал из открытых →
`get_order` (история):

- `cumExecQty > 0` → уровень = позиция (частичное исполнение тоже),
  выставить TP;
- `Cancelled/Rejected` и `cumExecQty = 0` → уровень свободен;
- десинхрон: на бирже `qty = 0`, а локально позиции есть → сброс записей.

После каждого цикла состояние сериализуется в `grid_state.json`
(рестарт переживает).

---

## 2. Псевдокод основного цикла

```
init:
    load .env, Config, GridStrategy
    restore grid_state.json
    if halted: exit с ошибкой (требует ручного вмешательства)

loop every POLL_INTERVAL seconds:
    # --- срез рынка ---
    candles   = klines(TIMEFRAME, KLINE_LIMIT)
    book      = orderbook(50);  bid, ask = best
    depth     = min(сумма bid*qty в −0.1%, сумма ask*qty в +0.1%)
    price     = ws_price or mid
    equity    = totalEquity
    positions  = get_positions → long_qty, short_qty
    funding   = funding_rate_history[-1] * 100        # кэш 300 c
    spread    = (ask − bid) / mid * 100
    news_ok   = now ∉ NEWS_WINDOWS
    pause     = exists(PAUSE)

    # --- синхронизация ордеров ---
    if not simulation:
        open_ids = get_open_orders()
        for level in levels:
            if level.order_id ∉ open_ids:
                meta = get_order(level.order_id)
                if cumExecQty > 0: mark_entry_filled(level, avgPrice, cumExecQty)
                elif cancelled:    mark_entry_cancelled(level)
            if level.status == "position" and level.tp ∉ open_ids:
                if tp filled: mark_tp_filled(level)
                else:         clear_tp(level)          # поставим ниже
        for level in position without tp:
            place_limit(tp, reduceOnly)

    # --- решение (чистая функция) ---
    decision = strategy.next(snapshot)

    # --- исполнение ---
    if decision.cancel_entries or decision.cancel_sides:
        cancel входы (все / по сторонам)
    if decision.close_all:
        cancel_all_orders(); market-close обеих сторон
    match decision.kind:
        halt:   notify; break loop
        wait:   pass
        pause:  pass (входы уже отменены)
        reset:  pass (close_all сделано)
        build | rebuild | run:
            for e in decision.entries:
                if проходит по стакану и минимумам:
                    place_limit(postOnly, positionIdx)
                    register_entry(...)
            maintain_sl():
                для каждой стороны с позицией:
                    trigger = ∓ граница ∓ 1*ATR
                    если qty/trigger изменились: cancel старый стоп, place новый

    # --- persist ---
    save grid_state.json
    notify при смене состояния (build/rebuild/pause/reset/halt)
```

Стейт-машина `GridStrategy.next()` (приоритеты сверху вниз):

```
halted?                     → halt
kill-switch (−5%/24ч)?      → halt
trailing (откат 1% после +3%)? → reset
repair (сверка с биржей)
grid SL (цена ≤ lower−ATR / ≥ upper+ATR)? → reset
note impulse (обновить кулдаун)
entry guards (пауза/news/funding/spread/depth)?
    нет плана → wait | план есть → pause (cancel входы)
профиль/ATR недоступны?
    нет плана → wait | есть → run (держим)
push POC; POC нестабилен?
    нет плана → wait | есть → run (держим)
нет плана?                  → build (или wait, если нестабилен)
rebuild-триггер?            → rebuild (cancel входов + новые уровни)
иначе                       → run (добор свободных уровней, снятие
                                заблокированных/полных сторон)
```

---

## 3. Таблица параметров

| Параметр | Дефолт | Диапазон | Файл | Что делает |
|---|---:|---|---|---|
| `POC_WINDOW_MIN` | 180 | 60–600 | .env | окно профиля, мин |
| `POC_BINS` | 100 | 50–200 | .env | бинов профиля |
| `VALUE_AREA_PCT` | 70 | 60–90 | .env | доля объёма VA |
| `ATR_PERIOD` | 14 | 7–50 | GridConfig | период ATR |
| `ATR_EXTEND_MULT` | 1.0 | 0.5–2.0 | GridConfig | удлинение коридора за VA, ×ATR |
| `POC_UNSTABLE_PCT` | 0.5 | 0.2–1.0 | GridConfig | порог нестабильности POC, % |
| `POC_UNSTABLE_BARS` | 3 | 1–10 | GridConfig | окно нестабильности, баров |
| `GRID_STEP_ATR_MULT` | 0.3 | 0.1–0.6 | GridConfig | шаг = 0.3×ATR |
| `MIN_TICK_MULT` | 3 | 1–10 | GridConfig | шаг ≥ 3×tick |
| `FEE_TAKER` | 0.00055 | — | GridConfig | taker-комиссия Bybit |
| `FEE_MAKER` | 0.0002 | — | GridConfig | maker-комиссия (VIP0) |
| `STEP_FEE_MULT` | 3 | 2–5 | GridConfig | шаг ≥ 3×taker×цена |
| `LEVELS_MIN` | 10 | 4–20 | GridConfig | минимум уровней |
| `LEVELS_MAX` | 50 | 20–100 | GridConfig | максимум уровней |
| `RISK_PER_TRADE_PCT` | 1.0 | 0.25–2.0 | GridConfig | риск одного уровня, % депо |
| `MAX_EXPOSURE_PCT` | 20 | 5–50 | GridConfig | суммарная экспозиция, % |
| `LEVERAGE_CAP` | 3.0 | 1–5 | GridConfig | потолок кредитного плеча |
| `MAX_OPEN_LEVELS` | 10 | 3–20 | GridConfig | позиций на сторону |
| `GRID_SL_ATR_MULT` | 1.0 | 0.5–2.0 | GridConfig | стоп сетки = граница ±1×ATR |
| `TRAIL_TRIGGER_PCT` | 3.0 | 1–10 | GridConfig | взвод трейлинга, % к депо |
| `TRAIL_GIVEBACK_PCT` | 1.0 | 0.5–3 | GridConfig | откат с пика, % |
| `KILL_24H_PCT` | 5.0 | 2–10 | GridConfig | kill-switch за 24ч, % |
| `FUNDING_MAX_PCT` | 0.05 | 0.01–0.1 | GridConfig | макс \|funding\|, % |
| `MAX_SPREAD_PCT` | 0.05 | 0.01–0.2 | GridConfig | макс спред, % |
| `DEPTH_MULT` | 5.0 | 2–20 | GridConfig | глубина ≥ N×размера уровня |
| `IMPULSE_PCT` | 5.0 | 2–10 | GridConfig | порог импульса, % |
| `IMPULSE_WINDOW` | 5 | 3–20 | GridConfig | окно импульса, баров |
| `IMPULSE_COOLDOWN_BARS` | 30 | 5–100 | GridConfig | кулдаун после импульса |
| `RECENTER_INTERVAL_S` | 1800 | 300–7200 | .env | плановый re-center, с |
| `POC_RECENTER_PCT` | 0.5 | 0.2–2 | GridConfig | сдвиг POC для re-center, % |
| `TREND_EMA_FAST` | 50 | 20–100 | GridConfig | EMA fast (anti-martingale) |
| `TREND_EMA_SLOW` | 200 | 100–400 | GridConfig | EMA slow |
| `TREND_THRESHOLD` | 2.0 | 0.5–5 | GridConfig | расхождение EMA, % |
| `ORDER_SIZE_CAP_USD` | 0 | 0=off | GridConfig | явный кап уровня, $ |
| `POLL_INTERVAL` | 30 | 5–120 | .env | период цикла, с |
| `KLINE_LIMIT` | 500 | ≥ POC_WINDOW/TF+ATR | .env | сколько свечей тянуть |
| `HEDGE_MODE` | true | — | .env | хедж-режим Bybit |
| `SIMULATION_MODE` | true | — | .env | симуляция (ордера не шлём) |

---

## 4. Блок-схема риск-менеджмента (текстом)

```
                         ┌─────────────────────┐
                         │  Старт / каждый цикл │
                         └──────────┬──────────┘
                                    ▼
                   ┌────────────────────────────────┐
   нет ─────────── │ KILL-SWITCH: Δ24h ≤ −5%?       │
                   └───────────────┬────────────────┘
                          да       ▼
                   ┌───────────────────────────────┐
                   │ halt: cancel_all → market-    │
                   │ close → notify → выход        │
                   └───────────────────────────────┘

                                    ▼
                   ┌────────────────────────────────┐
   нет ─────────── │ ТРЕЙЛИНГ: armed ∧ откат ≥1%?   │
                   └───────────────┬────────────────┘
                          да       ▼
                   ┌───────────────────────────────┐
                   │ reset: закрыть всё,           │
                   │ новая сессия                   │
                   └───────────────────────────────┘

                                    ▼
                   ┌────────────────────────────────┐
   нет ─────────── │ СТОП СЕТКИ: цена ≤ lower−ATR   │
                   │ или ≥ upper+ATR?               │
                   └───────────────┬────────────────┘
                          да       ▼
                   ┌───────────────────────────────┐
                   │ reset (+ биржевой stop-market │
                   │ срабатывает независимо)        │
                   └───────────────────────────────┘

                                    ▼
        ┌──────────────────────────────────────────────┐
        │ ГАРДЫ ВХОДА (пауза/частичный запрет):        │
        │  PAUSE-файл | окно новостей | импульс 5%/5   │
        │  |funding|>0.05% | спред>0.05% | глубина     │
        │  <5×уровня | EMA-trend против позиций        │
        └──────────────────────┬───────────────────────┘
                               ▼
        ┌──────────────────────────────────────────────┐
        │ РАЗМЕР УРОВНЯ (до выставления):              │
        │  qty = floor(usd/price/qtyStep)*qtyStep      │
        │  usd = min(1% риска/worst_dist,              │
        │            20% экспозиции/n,                 │
        │            3×leverage/n, cap)                │
        │  usd < max(minNotional, minQty*price) → skip │
        └──────────────────────┬───────────────────────┘
                               ▼
        ┌──────────────────────────────────────────────┐
        │ ИСПОЛНЕНИЕ:                                  │
        │  вход только postOnly; TP = сосед − 2×maker; │
        │  SL-сетки = граница ∓1×ATR (stop-market,    │
        │  MarkPrice, reduceOnly, qty=позиция стороны) │
        │  MAX_OPEN_LEVELS на сторону → cancel входов  │
        └──────────────────────┬───────────────────────┘
                               ▼
        ┌──────────────────────────────────────────────┐
        │ ПОСЛЕ КАЖДОГО ЦИКЛА:                         │
        │  fill-sync ордеров → grid_state.json →       │
        │  notify о смене состояния                     │
        └──────────────────────────────────────────────┘
```

---

## 5. Обработка edge-cases

| # | Ситуация | Реакция |
|---|---|---|
| 1 | Мало свечей на старте (warmup) | `wait`: «разогрев: мало свечей», без ордеров |
| 2 | POC прыгает каждый бар | гейт нестабильности (0.5%/3 бара): держим построенную сетку / ждём |
| 3 | Депозит не вмещает 10 уровней | `wait` с конкретной суммой и подсказкой (уменьшить LEVELS_MIN / увеличить депо) |
| 4 | Шаг по комиссии даёт <10 уровней | строим с меньшим числом + warning в reason (комиссия приоритетнее MIN) |
| 5 | Цена ушла за VAH/VAL | rebuild: отмена входов, новый профиль, исполненные позиции не трогаем |
| 6 | Пробой коридора ±1×ATR | reset: полный сброс (дублируется биржевым stop-market) |
| 7 | Ордер частично исполнён и отменён | `cumExecQty>0` → позиция на фактическом avgPrice/qty, остальное свободно |
| 8 | TP-ордер пропал с биржи | рассинхрон: если исполнен — закрыт; нет — выставить заново |
| 9 | Ликвидация/ручное закрытие на бирже | repair: фактический qty=0 → запись уровня стирается; частичный десинхрон — warning |
| 10 | Рестарт процесса | `grid_state.json`: план, уровни, SL, кулдауны, trailing; если `halted=true` — отказ старта до ручного вмешательства |
| 11 | Одновременный гэп через оба стопа | один stop-market на сторону; обе стороны = полный сброс; в худшем случае гэп покрывает только equity, kill-switch отсекает дальше |
| 12 | `post-only` принял бы как taker | фильтр до выставления: price_level < bid (long) / > ask (short) |
| 13 | Ордер уровня = 0 после фильтров (qty<minQty) | skip уровня + warning |
| 14 | Новости/спред/funding/глубина | пауза только ВХОДОВ; TP/SL/cancel продолжают работать |
| 15 | Сторона занята (`MAX_OPEN_LEVELS`) или заблокирована трендом | cancel входящих ордеров этой стороны, исполненные держим до TP/SL |
| 16 | Сбой REST / 5 ордеров подряд | exponential backoff, после 5 — `reconnect()`, цикл не падает |
| 17 | Повреждённый `grid_state.json` | warning, чистый старт |
| 18 | Симуляция (`SIMULATION_MODE=true`) | ордера не отправляются, fill-sync выключен, всё логируется как `[SIMULATION]` |
| 19 | One-way режим биржи вместо hedge | `HEDGE_MODE=false` → `positionIdx` не шлётся; сетка будет работать только с одной стороной — в ТЗ считаем **hedge обязательным** |
| 20 | WS отвалился | цена берётся из REST mid; WS переподнимается с backoff |

---

## 6. Рекомендации сверх ТЗ

1. **Тестнет перед боем.** `TESTNET=true, SIMULATION_MODE=false` на неделю:
   проверить fill/post-only/conditional на реальных ответах API.
2. **Проверить hedge mode в UI Bybit** до старта: Account → position mode →
   Hedge. С `HEDGE_MODE=true` при one-way биржа вернёт ошибку positionIdx.
3. **Funding-окна.** Funding начисляется каждые 8ч; при удержании позиций
   через границу — заложить `FUNDING_MAX_PCT` с запасом (альты часто
   ±0.01–0.05%). Опционально: пауза за 5 мин до расчёта.
4. **TON/лента новостей.** `NEWS_WINDOWS` заполнять вручную под CPI/FOMC
   (Bybit calendar); можно дополнить авто-парсером календаря позже.
5. **Кап по непрерывной работе.** Добавить max-длительность сессии (например,
   7 дней) с принудительным reset — профиль рынка меняется медленно, но
   «застрявший» боковик не бесконечен.
6. **Частичный TP.** Опция закрывать 50% объёма на первом шаге и держать
   остаток до 2 шагов — повышает expectancy на трендовых ретестах POC.
7. **Метрики.** Считать maker-fill ratio и среднее время жизни уровня:
   если `post-only` часто не исполнится (цена проскочила) — поднимать шаг.
8. **Диверсификация.** Запускать 2–3 символа с разъединённым equity-бюджетом
   (каждый — свой `.env` и свой `grid_state.json`), корреляция снизит
   риск одновременного пробоя.
9. **Backtest сетки.** `backtest.py --strategy grid_flat` сейчас ожидаемо
   падает с `NotImplementedError`: сетке нужен отдельный симулятор лимитных
   очередей (fill-or-kill по свечам, комиссии maker/taker, гэпы через стоп) —
   приоритетный следующий шаг после стабилизации live-логики.
10. **Лог-структура.** События `build/rebuild/pause/reset/halt` уже уходят в
    Telegram; добавить дневной сводный отчёт (сделки, PnL, число сбросов).

---

## Статус имплементации

| Компонент | Файл | Статус |
|---|---|---|
| Логика/стейт-машина | `strategy.py` | готово |
| Исполнитель | `main.py` | готово |
| Параметры | `.env`, `GridConfig` | готово |
| Тесты | `tests/test_robot_grid_flat.py` | готово |
| Свой бэктест лимитной очереди | — | рекомендация (п. 9) |
