"""Сбор метрик производительности и результатов торговли.

Позволяет оценить эффективность бота:
- сколько сделок совершено и каков итоговый PnL;
- сколько ошибок и переподключений было;
- среднее время ответа API.

Метрики накапливаются в памяти и периодически выводятся в лог.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Self

logger = logging.getLogger(__name__)


@dataclass
class Metrics:
    """Счётчики и статистика работы бота."""

    trades_opened: int = 0  # всего открытых сделок
    trades_closed: int = 0  # всего закрытых сделок
    total_pnl: float = 0.0  # суммарная прибыль/убыток в USDT
    api_calls: int = 0  # количество REST-запросов
    api_errors: int = 0  # ошибок API
    ws_reconnects: int = 0  # переподключений WebSocket
    api_response_times: list[float] = field(default_factory=list)  # сек

    def record_api_call(self, duration: float) -> None:
        """Зафиксировать успешный вызов API и его длительность."""
        self.api_calls += 1
        # Храним только последние 100 замеров — не даём списку расти
        if len(self.api_response_times) >= 100:
            self.api_response_times.pop(0)
        self.api_response_times.append(duration)

    def record_api_error(self) -> None:
        """Зафиксировать ошибку API."""
        self.api_errors += 1

    def record_trade_open(self) -> None:
        """Зафиксировать открытие сделки."""
        self.trades_opened += 1

    def record_trade_close(self, pnl: float) -> None:
        """Зафиксировать закрытие сделки с итоговым PnL."""
        self.trades_closed += 1
        self.total_pnl += pnl

    def record_ws_reconnect(self) -> None:
        """Зафиксировать переподключение WebSocket."""
        self.ws_reconnects += 1

    def avg_api_time(self) -> float:
        """Среднее время ответа API (сек), 0 если замеров не было."""
        if not self.api_response_times:
            return 0.0
        return sum(self.api_response_times) / len(self.api_response_times)

    def report(self) -> str:
        """Сформировать текстовый отчёт по метрикам (для лога)."""
        win_rate = (
            f"{(self.trades_closed / self.trades_opened * 100):.1f}%"
            if self.trades_opened
            else "—"
        )
        return (
            f"Сделок открыто: {self.trades_opened} | закрыто: {self.trades_closed}\n"
            f"Win rate (закрытых/открытых): {win_rate}\n"
            f"Итоговый PnL: {self.total_pnl:+.4f} USDT\n"
            f"API: {self.api_calls} вызовов, {self.api_errors} ошибок, "
            f"среднее {self.avg_api_time() * 1000:.0f} мс\n"
            f"WebSocket переподключений: {self.ws_reconnects}"
        )


class MetricsTimer:
    """Контекстный менеджер замера времени выполнения API-вызова."""

    def __init__(self, metrics: Metrics) -> None:
        self.metrics = metrics
        self._started: float | None = None

    def __enter__(self) -> Self:
        self._started = time.monotonic()
        return self

    def __exit__(self, *exc_info: object) -> None:
        assert self._started is not None
        duration = time.monotonic() - self._started
        self.metrics.record_api_call(duration)


@dataclass
class ScreenerMetrics:
    """Метрики скринера: время сканирования, сигналы, latency API."""

    scan_count: int = 0
    total_signals: int = 0
    total_symbols_scanned: int = 0
    last_scan_duration: float = 0.0
    last_signals_count: int = 0
    last_symbols_count: int = 0
    api_calls: int = 0
    api_errors: int = 0
    api_latencies: list[float] = field(default_factory=list)
    _scan_durations: list[float] = field(default_factory=list)

    def record_api_call(self, duration: float) -> None:
        self.api_calls += 1
        if len(self.api_latencies) >= 200:
            self.api_latencies.pop(0)
        self.api_latencies.append(duration)

    def record_api_error(self) -> None:
        self.api_errors += 1

    def record_scan(self, duration: float, signals: int, symbols: int) -> None:
        self.scan_count += 1
        self.last_scan_duration = duration
        self.last_signals_count = signals
        self.last_symbols_count = symbols
        self.total_signals += signals
        self.total_symbols_scanned += symbols
        if len(self._scan_durations) >= 50:
            self._scan_durations.pop(0)
        self._scan_durations.append(duration)

    def avg_api_latency_ms(self) -> float:
        if not self.api_latencies:
            return 0.0
        return (sum(self.api_latencies) / len(self.api_latencies)) * 1000

    def p95_api_latency_ms(self) -> float:
        if not self.api_latencies:
            return 0.0
        sorted_lat = sorted(self.api_latencies)
        idx = int(len(sorted_lat) * 0.95)
        return sorted_lat[min(idx, len(sorted_lat) - 1)] * 1000

    def avg_scan_duration(self) -> float:
        if not self._scan_durations:
            return 0.0
        return sum(self._scan_durations) / len(self._scan_durations)

    def report(self) -> str:
        avg_scan = self.avg_scan_duration()
        avg_lat = self.avg_api_latency_ms()
        p95_lat = self.p95_api_latency_ms()
        avg_signals = self.total_signals / self.scan_count if self.scan_count else 0
        return (
            f"Сканов: {self.scan_count} | "
            f"Сигналов: {self.total_signals} (ср. {avg_signals:.1f}/скан) | "
            f"Пар просканировано: {self.total_symbols_scanned}\n"
            f"Последний скан: {self.last_scan_duration:.1f} сек, "
            f"{self.last_symbols_count} пар, {self.last_signals_count} сигналов\n"
            f"Среднее время скана: {avg_scan:.1f} сек | "
            f"API: {self.api_calls} вызовов, {self.api_errors} ошибок\n"
            f"API latency: среднее {avg_lat:.0f} мс, p95 {p95_lat:.0f} мс"
        )


class ScreenerMetricsTimer:
    """Контекстный менеджер для замера API-вызова в скринере."""

    def __init__(self, metrics: ScreenerMetrics) -> None:
        self.metrics = metrics
        self._started: float | None = None

    def __enter__(self) -> Self:
        self._started = time.monotonic()
        return self

    def __exit__(self, *exc_info: object) -> None:
        assert self._started is not None
        duration = time.monotonic() - self._started
        self.metrics.record_api_call(duration)
