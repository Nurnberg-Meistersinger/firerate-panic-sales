"""Baseline spread evolution corpus.

Separate research stream from the 30 stress events. Question: how does
"normal" spread evolve for a fresh $10-100M mcap token during its first
6 months of trading? The answer feeds calibration of what baseline spread
FireRate should expect for a young COEN-like token.

Method:
1. events_baseline.csv lists 10 tokens listed on Binance during 2023-2024.
2. For each token, sample 12 days log-spaced across days 1..180 since listing.
3. For each sample day: pull bookTicker (real bid/ask) if available, fall back
   to aggTrades (s_eff price-range proxy). Also pull daily kline for volume.
4. Save per-token per-day aggregates to data/baseline/token_days.csv.

Usage:
  python baseline_corpus.py           # runs the full sample-fetch loop
  python baseline_corpus.py --list    # show corpus without fetching
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

import config as C
import fetchers as F


# Log-spaced sample days since listing
SAMPLE_DAYS = [1, 3, 7, 14, 21, 30, 45, 60, 90, 120, 150, 180]

BASELINE_CSV = C.ROOT / "events_baseline.csv"
OUT_DIR = C.DATA_DIR / "baseline"
OUT_DIR.mkdir(parents=True, exist_ok=True)
OUT_CSV = OUT_DIR / "token_days.csv"


def load_corpus() -> pd.DataFrame:
    return pd.read_csv(BASELINE_CSV, dtype=str)


def _sample_one_day(symbol: str, day: pd.Timestamp) -> dict:
    """Try bookTicker first (real spread), fall back to aggTrades (s_eff proxy).

    Returns a dict with:
      source: 'bookticker' | 'aggtrades' | 'none'
      bid, ask, bid_qty, ask_qty (if bookticker) OR s_eff, agg_volume, n_trades
      also always: median_spread_bps if computable
    """
    result: dict = {"source": "none", "median_spread_bps": None,
                    "volume_units": None, "n_updates_or_trades": None}

    # 1) Try bookTicker for real bid/ask
    bt_cache_dir = C.RAW_DIR / "bookticker_futures" / symbol
    bt_cache_dir.mkdir(parents=True, exist_ok=True)
    bt = F._aggregate_bookticker_day(symbol, day, bt_cache_dir)
    if not bt.empty:
        # Real spread from bid/ask
        mid = (bt["bid"] + bt["ask"]) / 2.0
        spread_bps = ((bt["ask"] - bt["bid"]) / mid.replace(0, pd.NA) * 1e4)
        result["source"] = "bookticker"
        result["median_spread_bps"] = float(spread_bps.median()) if not spread_bps.dropna().empty else None
        result["volume_units"] = float((bt["bid_qty"] + bt["ask_qty"]).sum())
        result["n_updates_or_trades"] = int(bt["n_updates"].sum())
        return result

    # 2) Fall back to aggTrades s_eff proxy
    at_cache_dir = C.RAW_DIR / "aggtrades" / symbol
    at_cache_dir.mkdir(parents=True, exist_ok=True)
    at = F._aggregate_aggtrades_day(symbol, day, at_cache_dir)
    if not at.empty:
        result["source"] = "aggtrades"
        result["median_spread_bps"] = float(at["s_eff"].median()) if not at["s_eff"].dropna().empty else None
        result["volume_units"] = float(at["agg_volume"].sum())
        result["n_updates_or_trades"] = int(at["n_trades"].sum())
        return result

    return result


def sample_corpus() -> pd.DataFrame:
    corpus = load_corpus()
    rows = []
    for _, tok in corpus.iterrows():
        tid = tok["token_id"]
        symbol = tok["symbol"]
        listing = pd.Timestamp(tok["listing_date"], tz="UTC")
        print(f"\n[{tid}] {tok['name']} ({symbol}) listed {tok['listing_date']}")

        for offset in SAMPLE_DAYS:
            day = listing + pd.Timedelta(days=offset - 1)  # day 1 = listing_date itself
            print(f"  day {offset:3d} → {day.date()}", flush=True)
            try:
                data = _sample_one_day(symbol, day)
            except Exception as exc:  # noqa: BLE001
                print(f"    failed: {exc}")
                data = {"source": "error", "median_spread_bps": None,
                        "volume_units": None, "n_updates_or_trades": None}
            rows.append({
                "token_id": tid,
                "name": tok["name"],
                "symbol": symbol,
                "sector": tok["sector"],
                "initial_mcap_usd_m": float(tok["initial_mcap_usd_m"]),
                "listing_date": tok["listing_date"],
                "day_offset": offset,
                "sample_date": day.strftime("%Y-%m-%d"),
                **data,
            })
    return pd.DataFrame(rows)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true",
                    help="Print the baseline corpus and exit")
    args = ap.parse_args()

    corpus = load_corpus()
    if args.list:
        print(corpus.to_string(index=False))
        return 0

    print(f"Sampling {len(corpus)} tokens × {len(SAMPLE_DAYS)} days = "
          f"{len(corpus) * len(SAMPLE_DAYS)} day-samples")
    df = sample_corpus()
    df.to_csv(OUT_CSV, index=False)
    print(f"\nWrote {len(df)} rows -> {OUT_CSV}")

    # Quick summary
    print("\n=== Source coverage ===")
    print(df.groupby(["token_id", "source"]).size().unstack(fill_value=0))
    print("\n=== Median spread by day_offset (across tokens) ===")
    med = df[df.median_spread_bps.notna()].groupby("day_offset")["median_spread_bps"].agg(["count", "median", "min", "max"])
    print(med.round(2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
