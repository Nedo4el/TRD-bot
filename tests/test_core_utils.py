"""Тесты core.utils: чтение JSON-файлов в UTF-8 (с BOM) и UTF-16."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.utils import read_json_file


def test_read_json_file_utf8_with_bom(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"symbol": "BTCUSDT"}), encoding="utf-8-sig")
    assert read_json_file(path) == {"symbol": "BTCUSDT"}


def test_read_json_file_utf16(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    payload = {"symbol": "ETHUSDT", "day_trades": 2}
    path.write_text(json.dumps(payload), encoding="utf-16")
    assert read_json_file(path) == payload


def test_read_json_file_garbage_raises(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text("{oops", encoding="utf-8")
    with pytest.raises(ValueError, match="UTF-8/UTF-16"):
        read_json_file(path)
