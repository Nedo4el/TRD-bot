"""Тесты сканера импульсов: rolling-окно, порог, направление."""

from __future__ import annotations

from bot_screener_impulse.scanner import (
    ImpulseConfig,
    ImpulseSignal,
    analyze_symbol,
)


def _candles(
    base_price: float = 100.0,
    moves: list[float] | None = None,
    start_ms: int = 0,
    step_ms: int = 60_000,
) -> list[dict]:
    """Генерирует свечи с заданными ценами закрытия."""
    if moves is None:
        moves = [base_price]
    out: list[dict] = []
    price = base_price
    ts = start_ms
    for move in moves:
        price = move
        out.append(
            {
                "open_time": ts,
                "open": price,
                "high": price * 1.001,
                "low": price * 0.999,
                "close": price,
                "volume": 100.0,
            }
        )
        ts += step_ms
    return out


def test_price_window_long_above_threshold() -> None:
    """Цена выросла на 5% за окно — сигнал LONG."""
    candles = _candles(moves=[100.0, 105.0], start_ms=0, step_ms=60_000)
    cfg = ImpulseConfig(min_move_pct=4.0, window_sec=60, impulse_type="price")
    sig = analyze_symbol("BTCUSDT", "1m", candles, cfg=cfg)
    assert sig is not None
    assert sig.direction == "LONG"
    assert sig.move_pct == 5.0
    assert sig.window_sec == 60


def test_price_window_short_above_threshold() -> None:
    """Цена упала на 5% за окно — сигнал SHORT."""
    candles = _candles(moves=[100.0, 95.0], start_ms=0, step_ms=60_000)
    cfg = ImpulseConfig(min_move_pct=4.0, window_sec=60, impulse_type="price")
    sig = analyze_symbol("BTCUSDT", "1m", candles, cfg=cfg)
    assert sig is not None
    assert sig.direction == "SHORT"
    assert sig.move_pct == 5.0


def test_price_window_below_threshold_no_signal() -> None:
    """Цена выросла на 3% при пороге 4% — сигнала нет."""
    candles = _candles(moves=[100.0, 103.0], start_ms=0, step_ms=60_000)
    cfg = ImpulseConfig(min_move_pct=4.0, window_sec=60, impulse_type="price")
    sig = analyze_symbol("BTCUSDT", "1m", candles, cfg=cfg)
    assert sig is None


def test_price_window_exactly_at_threshold_triggers() -> None:
    """Цена выросла ровно на 4% при пороге 4% — сигнал есть (семантика >=)."""
    candles = _candles(moves=[100.0, 104.0], start_ms=0, step_ms=60_000)
    cfg = ImpulseConfig(min_move_pct=4.0, window_sec=60, impulse_type="price")
    sig = analyze_symbol("BTCUSDT", "1m", candles, cfg=cfg)
    assert sig is not None
    assert sig.direction == "LONG"
    assert sig.move_pct == 4.0


def test_price_window_5_seconds() -> None:
    """Окно 5 секунд: свечи с шагом 1 сек, цена выросла на 6%."""
    candles = _candles(moves=[100.0, 106.0], start_ms=0, step_ms=1000)
    cfg = ImpulseConfig(min_move_pct=4.0, window_sec=5, impulse_type="price")
    sig = analyze_symbol("BTCUSDT", "1m", candles, cfg=cfg)
    assert sig is not None
    assert sig.direction == "LONG"
    assert sig.move_pct == 6.0
    assert sig.window_sec == 5


def test_candle_body_long() -> None:
    """Тело свечи: close > open на 5% — сигнал LONG (свеча не первая в истории)."""
    candles = [
        {
            "open_time": 0,
            "open": 100.0,
            "high": 100.5,
            "low": 99.5,
            "close": 100.0,
            "volume": 100.0,
        },
        {
            "open_time": 60_000,
            "open": 100.0,
            "high": 105.0,
            "low": 99.0,
            "close": 105.0,
            "volume": 100.0,
        },
        {
            "open_time": 120_000,
            "open": 105.0,
            "high": 106.0,
            "low": 104.0,
            "close": 105.5,
            "volume": 100.0,
        },
    ]
    cfg = ImpulseConfig(
        min_move_pct=4.0,
        confirmation_candles=0,
        window_sec=60,
        impulse_type="candle",
    )
    sig = analyze_symbol("BTCUSDT", "1m", candles, cfg=cfg)
    assert sig is not None
    assert sig.direction == "LONG"
    assert sig.move_pct == 5.0


def test_insufficient_candles_no_signal() -> None:
    """Недостаточно свечей — сигнала нет."""
    candles = _candles(moves=[100.0], start_ms=0, step_ms=60_000)
    cfg = ImpulseConfig(min_move_pct=4.0, window_sec=60, impulse_type="price")
    sig = analyze_symbol("BTCUSDT", "1m", candles, cfg=cfg)
    assert sig is None


def test_window_not_covered_by_history() -> None:
    """Окно 60с, но история только 10с — берём первую доступную свечу."""
    candles = _candles(moves=[100.0, 150.0], start_ms=0, step_ms=1000)
    cfg = ImpulseConfig(min_move_pct=4.0, window_sec=60, impulse_type="price")
    sig = analyze_symbol("BTCUSDT", "1m", candles, cfg=cfg)
    assert sig is not None
    assert sig.direction == "LONG"
    assert sig.move_pct == 50.0


def test_signal_has_required_fields() -> None:
    """Сигнал содержит все обязательные поля."""
    candles = _candles(moves=[100.0, 110.0], start_ms=0, step_ms=60_000)
    cfg = ImpulseConfig(min_move_pct=4.0, window_sec=60, impulse_type="price")
    sig = analyze_symbol("BTCUSDT", "1m", candles, cfg=cfg, turnover_24h=50_000_000.0)
    assert sig is not None
    assert isinstance(sig, ImpulseSignal)
    assert sig.symbol == "BTCUSDT"
    assert sig.price == 110.0
    assert sig.signal_time != ""
    assert sig.turnover_24h == 50_000_000.0
