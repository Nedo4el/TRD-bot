"""Общие фикстуры: тесты не должны писать в боевые папки данных."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_trades_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Увести журнал сделок в tmp.

    ``TradeStore()`` создают сами роботы (order_flow / order_cycle) со
    путём по умолчанию, поэтому подменяем путь в модуле: иначе каждый
    прогон тестов дописывал фиктивные BTCUSDT-сделки в статистику
    дашборда (data/dashboard/trades).
    """
    from dashboard import trade_store

    trades_dir = tmp_path / "trades"
    trades_dir.mkdir()
    monkeypatch.setattr(trade_store, "TRADES_DIR", trades_dir)
