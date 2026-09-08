"""Анализ импульса AKEUSDT 05.08.26 05:22-05:49 MSK (1m)."""

from pybit.unified_trading import HTTP
from datetime import datetime, timezone, timedelta


def sma(values: list[float], period: int) -> list[float | None]:
    result: list[float | None] = []
    for i in range(len(values)):
        if i < period - 1:
            result.append(None)
        else:
            window = values[i - period + 1 : i + 1]
            result.append(sum(window) / period)
    return result


def estimate_delta(candle: dict) -> float:
    op, cl, hi, lo, vol = candle["open"], candle["close"], candle["high"], candle["low"], candle["volume"]
    rng = hi - lo
    if rng <= 0:
        return 0.0
    body = cl - op
    return vol * (body / rng)


def main() -> None:
    http = HTTP(testnet=False)

    msk = timezone(timedelta(hours=3))
    impulse_start_msk = datetime(2026, 8, 5, 5, 22, 0, tzinfo=msk)
    impulse_end_msk = datetime(2026, 8, 5, 5, 49, 59, tzinfo=msk)
    impulse_start = int(impulse_start_msk.timestamp() * 1000)
    impulse_end = int(impulse_end_msk.timestamp() * 1000)

    fetch_start = impulse_start - 200 * 60 * 1000

    resp = http.get_kline(
        category="linear", symbol="AKEUSDT", interval="1",
        start=fetch_start, end=impulse_end, limit=1000,
    )
    rows = resp["result"]["list"]
    rows.reverse()

    candles = []
    for r in rows:
        ts = int(r[0])
        dt = datetime.fromtimestamp(ts / 1000, tz=msk)
        candles.append({
            "time": dt.strftime("%H:%M"),
            "ts": ts,
            "open": float(r[1]),
            "high": float(r[2]),
            "low": float(r[3]),
            "close": float(r[4]),
            "volume": float(r[5]),
        })

    # Находим индекс начала окна
    start_idx = 0
    for i, c in enumerate(candles):
        if c["ts"] >= impulse_start:
            start_idx = i
            break

    volumes = [c["volume"] for c in candles]
    deltas = [estimate_delta(c) for c in candles]
    vol_sma50 = sma(volumes, 50)
    abs_deltas = [abs(d) for d in deltas]
    delta_sma20 = sma(abs_deltas, 20)

    print(f"Total candles fetched: {len(candles)}")
    print(f"Impulse window index: {start_idx} ({candles[start_idx]['time']} MSK)")
    print()

    hdr = f"{'Time':>6} {'Open':>11} {'High':>11} {'Low':>11} {'Close':>11} {'Volume':>12} {'SMA50':>12} {'Vol/SMA':>8} {'Width%':>7} {'Delta':>12} {'Delta/SMA':>10}"
    print(hdr)
    print("-" * len(hdr))

    for i in range(max(0, start_idx - 10), len(candles)):
        if candles[i]["ts"] > impulse_end:
            break
        c = candles[i]
        sma_v = vol_sma50[i]
        vol_ratio = c["volume"] / sma_v if sma_v and sma_v > 0 else 0
        width = (c["high"] - c["low"]) / c["close"] * 100 if c["close"] > 0 else 0
        d = deltas[i]
        sma_d = delta_sma20[i]
        delta_ratio = abs(d) / sma_d if sma_d and sma_d > 0 else 0

        marker = " <<<" if c["ts"] >= impulse_start and vol_ratio >= 3.0 else ""
        in_window = "*" if c["ts"] >= impulse_start else " "

        print(
            f"{in_window}{c['time']:>5} {c['open']:>11.7f} {c['high']:>11.7f} {c['low']:>11.7f} "
            f"{c['close']:>11.7f} {c['volume']:>12.0f} {sma_v or 0:>12.0f} {vol_ratio:>8.1f} "
            f"{width:>7.3f} {d:>12.0f} {delta_ratio:>10.1f}{marker}"
        )

    # Сводка по импульсным свечам
    print()
    print("=" * 80)
    print("ИМПУЛЬСНЫЕ СВЕЧИ (Vol/SMA50 >= 3.0):")
    print("=" * 80)

    for i in range(start_idx, len(candles)):
        if candles[i]["ts"] > impulse_end:
            break
        c = candles[i]
        sma_v = vol_sma50[i]
        vol_ratio = c["volume"] / sma_v if sma_v and sma_v > 0 else 0
        if vol_ratio < 3.0:
            continue

        width = (c["high"] - c["low"]) / c["close"] * 100 if c["close"] > 0 else 0
        d = deltas[i]
        sma_d = delta_sma20[i]
        delta_ratio = abs(d) / sma_d if sma_d and sma_d > 0 else 0
        direction = "LONG" if c["close"] > c["open"] else "SHORT"

        print(f"  {c['time']} MSK | {direction:>5} | Vol/SMA: {vol_ratio:.1f}x | Width: {width:.3f}% | Delta/SMA: {delta_ratio:.1f}x")

    # Рекомендации
    print()
    print("=" * 80)
    print("РЕКОМЕНДУЕМЫЕ ПАРАМЕТРЫ ImpulseConfig:")
    print("=" * 80)

    # Собираем все vol_ratio и width в окне
    ratios = []
    widths = []
    for i in range(start_idx, len(candles)):
        if candles[i]["ts"] > impulse_end:
            break
        c = candles[i]
        sma_v = vol_sma50[i]
        vol_ratio = c["volume"] / sma_v if sma_v and sma_v > 0 else 0
        width = (c["high"] - c["low"]) / c["close"] * 100 if c["close"] > 0 else 0
        ratios.append(vol_ratio)
        widths.append(width)

    if ratios:
        max_vol = max(ratios)
        avg_impulse_vol = sum(r for r in ratios if r >= 3.0) / max(1, sum(1 for r in ratios if r >= 3.0))
        max_width = max(widths)
        avg_impulse_width = sum(w for w, r in zip(widths, ratios) if r >= 3.0) / max(1, sum(1 for r in ratios if r >= 3.0))

        print(f"  volume_spike_multiplier:  min={min(ratios if ratios else [0]):.1f}  avg_impulse={avg_impulse_vol:.1f}  max={max_vol:.1f}")
        print(f"  candle_width_min:         min={min(widths if widths else [0]):.3f}  avg_impulse={avg_impulse_width:.3f}  max={max_width:.3f}")
        print(f"  candle_width_max:         рекомендация = max_width * 1.2 = {max_width * 1.2:.3f}")
        print(f"  delta_spike_multiplier:   (смотри delta_ratio колонку)")


if __name__ == "__main__":
    main()
