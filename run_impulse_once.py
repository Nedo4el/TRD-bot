"""Run impulse screener once."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bot_screener_impulse.main import _load_config, scan_once
from core.logger import setup_logging
from core.metrics import ScreenerMetrics

async def run():
    setup_logging("logs/screener_impulse.log", "INFO")
    cfg, impulse_cfg = _load_config()
    metrics = ScreenerMetrics()
    await scan_once(cfg, impulse_cfg, metrics)

asyncio.run(run())
