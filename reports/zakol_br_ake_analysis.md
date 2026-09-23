# robot_zakol — BRUSDT и AKEUSDT (указанные окна)

**Депо:** $100 | **Notional:** $10/сделка | **Fidelity:** 1S synthetic  
**Параметры:** OFFSET −3%, TTL 20s, SL −2%, trail 2%, BE +0.5%/+0.2%, kill DD 5%

---

## Сводка (все 3 режима)

### BRUSDT 2026-03-20 → 2026-08-07 (~140d, 203k M1)

| mode | trades | WR | PF | sum$ | equity$ | maxDD% | Sharpe | kills |
|------|-------:|---:|---:|-----:|--------:|-------:|-------:|------:|
| ideal | 476 | 43.7% | 1.54 | **+27.95** | 127.95 | 2.42 | 3.15 | 0 |
| realistic | 456 | 42.1% | 1.50 | **+26.24** | 126.24 | 2.57 | 2.92 | 0 |
| pessimistic | 456 | 42.1% | 1.45 | **+24.01** | 124.01 | 2.67 | 2.68 | 0 |

**Fill (realistic):** po_rej=8, queue_rej=136, ttl_cancels≈608k, re_places≈61k  
**Exits (realistic):** trail 37.9%, sl 57.9%, be ~4%  
**Volatility:** chg30…period **+101%**, bar p99=**2.30%**, hit −3%/20s=**0.083%**

### AKEUSDT 2026-07-26 → 2026-09-14 (~50d, 73k M1)

| mode | trades | WR | PF | sum$ | equity$ | maxDD% | Sharpe | kills |
|------|-------:|---:|---:|-----:|--------:|-------:|-------:|------:|
| ideal | 716 | 44.4% | 1.62 | **+47.90** | 147.90 | 3.43 | 4.35 | 0 |
| realistic | 44 | 6.8% | 0.69 | **−2.57** | 97.43 | 4.99 | −0.64 | **1 (DD-kill)** |
| pessimistic | 38 | 5.3% | 0.64 | **−2.62** | 97.38 | 5.01 | −0.68 | **1 (DD-kill)** |

**Fill (realistic):** po_rej=0, queue_rej=**753**, ttl≈29k, re≈11k — бот **остановлен kill'ом** до конца окна  
**Exits (realistic):** trail 6.8%, **sl 93.2%**  
**Volatility:** chg **+421%**, bar p99=**4.27%**, hit −3%/20s=**0.332%** (выше BTR!)

Отчёт AKE: `reports/zakol_honest_ake_0726_0914.txt`

---

## Анализ

### BRUSDT — рабочий, стабильный
1. **realistic ≈ ideal** (gap −$1.7 = 6%): queue_rej всего 136, po_rej=8 — fill-модель почти не съедает edge.
2. **456 сделок / 140d ≈ 3.3/день**, WR 42%, PF 1.50, DD 2.6% — укладывается в kill 5%.
3. Sharpe 2.9, Recovery 9.4 — лучший из всех прогонов на $100.
4. **pessimistic тоже +$24** — устойчиво к slippage.
5. kill=0 — риск-контроль не сработал (DD не дошёл до 5%).

### AKEUSDT — ideal красивый, realistic сдох
1. **Volatility рекордная** (hit 0.332% > BTR 0.31%), chg +421% — ideal даёт +48$ за 50 дней.
2. **Но realistic: только 44 сделки и DD-kill.** Причина — **queue_rej=753** при входе: лимитка −3% часто «прыгает мимо» (прошла уровень) или очередь не даёт fill в окно; те fill, что проходят — **adverse selection** (заполняется на падающем knives), WR падает 44%→7%, sl=93%.
3. **Каскад SL:** hold_ms=1000–5000 — цена после −3% за 20с продолжает падать, −2% SL долбит сериями → session DD → **kill на 5%** (equity 97.43 = −2.57, maxDD 4.99% упёрся в лимит).
4. ideal при этом **не убивался** — потому что fill без очереди даёт лучшую цену и больше trail-выходов.
5. **pessimistic = realistic** (queue уже съел всё, slippage −0.05$ дополнительно).

### Сравнение трёх «рабочих» кандидатов

| | BTR (bench) | BRUSDT | AKEUSDT |
|--|------------:|-------:|--------:|
| window | 19–30d | **140d** | 50d |
| realistic trades | 25–39 | **456** | 44 (killed) |
| realistic sum$ | +0.55…+2.21 | **+26.24** | **−2.57** |
| gap ideal→real | small | **−6%** | **−$50 (kill)** |
| hit −3%/20s | 0.31% | 0.08% | 0.33% |
| DD-kill | 0 | 0 | **1** |

---

## Вывод

1. **BRUSDT — лучший кандидат из этой пары:** длинное окно, realistic **+$26 (126$ equity)**, малый gap fill-модели, kill не срабатывал, pessimistic +$24.
2. **AKEUSDT — ловушка идеального бэктеста:** ideal +$48 выглядит отлично, но на честном fill → **queue rejects + серия SL → DD-kill за ~неделю**, итог −2.6$. Высокая volatility **сама по себе не спасает** — нужна ещё **однородность** dump'ов (не continuous bleed).
3. **Фильтр отбора:** hit −3%/20s **и** отсутствие длинных multi-minute bleed после fill. AKE проходит первый фильтр (0.33%), но проваливает второй (WR 7% после queue).
4. Для live: **BR — брать**, AKE — только после проверки queue/slippage на publicTrade (synthetic 1S может занижать/завышать queue_rej) либо с ужесточённым kill / меньшим notional.

## Команды

```bash
.\.venv\Scripts\python.exe -m robot_zakol.backtest --symbol BRUSDT --start-date 2026-03-20 --end-date 2026-08-07 --deposit 100 --interval 1 --report reports/zakol_honest_br_0320_0807.txt
.\.venv\Scripts\python.exe -m robot_zakol.backtest --symbol AKEUSDT --start-date 2026-07-26 --end-date 2026-09-14 --deposit 100 --interval 1 --report reports/zakol_honest_ake_0726_0914.txt
```
