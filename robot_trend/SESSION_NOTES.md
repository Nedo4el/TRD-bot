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
