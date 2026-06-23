"""Central configuration for the Panic Sales data-mining pipeline.

All comments and identifiers are in English on purpose (project rule 2.3).
"""

import os
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent
EVENTS_CSV = ROOT / "events.csv"

DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw"            # untouched source pulls (parquet)
SIGNALS_DIR = DATA_DIR / "signals"   # per-event signal series (parquet)
OUTPUT_DIR = DATA_DIR / "output"     # metrics tables (csv/parquet)

for _d in (RAW_DIR, SIGNALS_DIR, OUTPUT_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# API keys (read from environment; free tiers work without most of these)
# ---------------------------------------------------------------------------
FRED_API_KEY = os.environ.get("FRED_API_KEY", "")            # free, instant signup
CRYPTOCOMPARE_API_KEY = os.environ.get("CRYPTOCOMPARE_API_KEY", "")  # free up to 100k calls/mo
POLYGON_API_KEY = os.environ.get("POLYGON_API_KEY", "")      # paid, second pass only

# ---------------------------------------------------------------------------
# Methodology constants (mirror section II of the research doc)
# ---------------------------------------------------------------------------
BASELINE_DAYS = 30          # pre-event baseline window, calendar days
STRESS_MULTIPLE = 2.0       # signal is "out of normal" above 2x baseline
RECOVERY_MULTIPLE = 1.5     # signal is "back to normal" below 1.5x baseline
VOL_WINDOW = 20             # rolling window for realized volatility (bars)

# Binance public data lake (no key required)
BINANCE_VISION = "https://data.binance.vision/data/spot/daily/klines"

# CryptoCompare REST base
CRYPTOCOMPARE_BASE = "https://min-api.cryptocompare.com/data"
