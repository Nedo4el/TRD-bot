"""Свечи Bybit v5: загрузка пачками, кэш в SQLite, лимит 10 запросов/с."""

from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen

import pandas as pd

from bot_screener_yrovni.poc_scanner.store import POCStore
from core.logger import get_logger

logger = get_logger(__name__)

BYBIT_KLINE_URL = "https://api.bybit.com/v5/market/kline"
CATEGORY = "linear"
MAX_LIMIT = 1000
INTERVAL_MS: dict[str, int] = {"60": 3_600_000, "240": 14_400_000}
FRAME_COLUMNS = ["ts", "open", "high", "low", "close", "volume", "turnover"]


class KlineError(RuntimeError):
    """Биржа не отдала свечи или ответил ошибкой."""


class RateLimiter:
    """Не чаще ``max_per_sec`` запросов в секунду (по умолчанию 10)."""

    def __init__(self, max_per_sec: float = 10.0) -> None:
        self._gap = 1.0 / max_per_sec if max_per_sec > 0 else 0.0
        self._last = 0.0

    def wait(self) -> None:
        """Выдержать паузу, если прошлый запрос был слишком недавно."""
        elapsed = time.monotonic() - self._last
        if elapsed < self._gap:
            time.sleep(self._gap - elapsed)
        self._last = time.monotonic()


def _get_json(url: str, params: dict[str, Any], timeout: float) -> dict[str, Any]:
    """GET с query-параметрами и разбором JSON-ответа."""
    full_url = f"{url}?{urlencode(params)}"
    try:
        with urlopen(full_url, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise KlineError(f"запрос свечей не удался: {exc}") from exc
    if not isinstance(payload, dict):
        raise KlineError("неожиданный ответ биржи: не объект JSON")
    if payload.get("retCode") != 0:
        raise KlineError(
            f"биржа вернула retCode={payload.get('retCode')}: {payload.get('retMsg')}"
        )
    return payload


def fetch_klines(
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    *,
    limiter: RateLimiter,
    timeout: float = 20.0,
) -> list[dict[str, Any]]:
    """Скачать свечи за диапазон, листая пачками по 1000 штук.

    Args:
        symbol: торговая пара.
        interval: ``"60"`` или ``"240"``.
        start_ms: начало диапазона, миллисекунды UTC.
        end_ms: конец диапазона, миллисекунды UTC.
        limiter: ограничитель частоты запросов.
        timeout: таймаут одного HTTP-запроса.

    Returns:
        Свечи по возрастанию времени: open_time/open/high/low/close/
        volume/turnover.

    Raises:
        KlineError: сеть или биржа ответили ошибкой.
    """
    step = INTERVAL_MS[interval]
    cursor_end = int(end_ms)
    result: list[dict[str, Any]] = []

    # Bybit отдаёт ПОСЛЕДНИЕ `limit` свечей диапазона, поэтому листаем
    # назад от конца: иначе на длинном окне придёт только свежий кусок.
    while cursor_end > start_ms:
        limiter.wait()
        payload = _get_json(
            BYBIT_KLINE_URL,
            {
                "category": CATEGORY,
                "symbol": symbol,
                "interval": interval,
                "start": int(start_ms),
                "end": cursor_end,
                "limit": MAX_LIMIT,
            },
            timeout,
        )
        rows = payload.get("result", {}).get("list") or []
        if not rows:
            break
        batch = [_row_to_candle(row) for row in rows]
        result.extend(batch)
        oldest = min(candle["open_time"] for candle in batch)
        if oldest <= start_ms:
            break
        next_end = oldest - step
        if next_end >= cursor_end:
            break
        cursor_end = next_end
        if len(batch) < MAX_LIMIT:
            break

    result.sort(key=lambda candle: candle["open_time"])
    return result


def _row_to_candle(row: list[str]) -> dict[str, Any]:
    """Строка ответа Bybit -> свеча."""
    return {
        "open_time": int(row[0]),
        "open": float(row[1]),
        "high": float(row[2]),
        "low": float(row[3]),
        "close": float(row[4]),
        "volume": float(row[5]),
        "turnover": float(row[6]),
    }


def _empty_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {column: pd.Series(dtype="float64") for column in FRAME_COLUMNS}
    )


def _to_frame(
    rows: list[dict[str, Any]],
    interval: str,
    now_ms: int,
) -> pd.DataFrame:
    """Свечи -> DataFrame, только закрытые свечи."""
    if not rows:
        return _empty_frame()
    step = INTERVAL_MS[interval]
    frame = pd.DataFrame(rows)
    frame = frame[frame["open_time"] + step <= now_ms]
    if frame.empty:
        return _empty_frame()
    frame = frame.copy()
    frame["ts"] = pd.to_datetime(frame["open_time"], unit="ms", utc=True)
    return frame[FRAME_COLUMNS].reset_index(drop=True)


class KlineCache:
    """История свечей: сначала SQLite, к бирже — только за недостающим."""

    def __init__(
        self,
        store: POCStore,
        limiter: RateLimiter | None = None,
        timeout: float = 20.0,
    ) -> None:
        self.store = store
        self.limiter = limiter or RateLimiter(10.0)
        self.timeout = timeout

    def load(
        self,
        symbol: str,
        interval: str,
        days: int,
        now: datetime | None = None,
    ) -> pd.DataFrame:
        """Свечи за ``days`` дней; из кэша, добирая хвост у биржи.

        Args:
            symbol: торговая пара.
            interval: ``"60"`` или ``"240"``.
            days: глубина истории в днях.
            now: текущий момент (для тестов), по умолчанию — системное время.

        Returns:
            DataFrame с колонками ts/open/high/low/close/volume/turnover,
            отсортированный по времени, без незакрытой свечи.
        """
        if interval not in INTERVAL_MS:
            raise ValueError(f"неизвестный интервал {interval!r}")
        now_ms = int((now.timestamp() if now is not None else time.time()) * 1000)
        step = INTERVAL_MS[interval]
        start_ms = now_ms - int(days) * 86_400_000

        first, last = self.store.open_time_bounds(symbol, interval)
        need_full = first is None or first > start_ms + step
        if need_full:
            self._download(symbol, interval, start_ms, now_ms)
        elif last is not None and last < now_ms - step:
            tail_from = max(start_ms, last - 2 * step)
            self._download(symbol, interval, tail_from, now_ms)

        rows = self.store.load_klines(symbol, interval, start_ms, now_ms)
        return _to_frame(rows, interval, now_ms)

    def _download(
        self,
        symbol: str,
        interval: str,
        start_ms: int,
        end_ms: int,
    ) -> None:
        candles = fetch_klines(
            symbol,
            interval,
            start_ms,
            end_ms,
            limiter=self.limiter,
            timeout=self.timeout,
        )
        if not candles:
            logger.warning("свечи не получены: %s %s с %s", symbol, interval, start_ms)
            return
        self.store.save_klines(symbol, interval, candles)
        logger.info("свечи: %s %s +%d (кэш обновлён)", symbol, interval, len(candles))
