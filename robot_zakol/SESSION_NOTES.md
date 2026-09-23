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
