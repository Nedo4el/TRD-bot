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
