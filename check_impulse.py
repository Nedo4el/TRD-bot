import sys
sys.path.insert(0, ".")
from bot_screener_impulse.main import _load_config
cfg, icfg = _load_config()
print("min_turnover =", cfg["min_turnover_24h"])
