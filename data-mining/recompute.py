"""Recompute metrics from already-downloaded raw parquet files.

Use this when you change signals.py methodology and want updated
metrics without re-pulling network data. It reads data/raw/*.parquet,
re-derives signals, and overwrites data/output/metrics.csv plus the
per-event signals parquets.

Usage:
  python recompute.py
  python recompute.py --only cr-2017-btctop cr-2020-covid
"""

from __future__ import annotations

import argparse
import sys

import pandas as pd

import config as C
import signals as S


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", help="event_id(s) to recompute")
    args = ap.parse_args()

    events = pd.read_csv(C.EVENTS_CSV, dtype=str).set_index("event_id")
    raw_dir = C.RAW_DIR
    parquets = sorted(raw_dir.glob("*.parquet"))
    if args.only:
        parquets = [p for p in parquets if p.stem in args.only]

    rows: list[pd.DataFrame] = []
    for p in parquets:
        eid = p.stem
        if eid not in events.index:
            print(f"[{eid}] not in events.csv; skipping")
            continue
        row = events.loc[eid]
        try:
            raw = pd.read_parquet(p)
        except Exception as exc:  # noqa: BLE001
            print(f"[{eid}] read failed: {exc}")
            continue

        # Detect resolution by bar spacing (minute vs day)
        bar_h = (pd.Series(raw.index).diff().median().total_seconds() / 3600.0
                 if len(raw) > 1 else 24.0)
        resolution = "minute" if bar_h < 1 else "day"

        sig = S.build_signals(raw)
        sig.to_parquet(C.SIGNALS_DIR / f"{eid}.parquet")
        m = S.event_metrics(sig, row["window_start"], row["window_end"],
                            row["peak_direction"])
        if m.empty:
            print(f"[{eid}] no metrics from {len(raw)} bars")
            continue
        m.insert(0, "event_id", eid)
        m.insert(1, "asset_class", row["asset_class"])
        m.insert(2, "resolution", resolution)
        m.insert(3, "n_bars_raw", len(raw))
        rows.append(m)
        print(f"[{eid}] {resolution} {len(raw)} bars -> "
              f"signals: {', '.join(m['signal'].tolist())}")

    if not rows:
        print("no metrics produced")
        return 1
    out = pd.concat(rows, ignore_index=True)
    out_path = C.OUTPUT_DIR / "metrics.csv"
    out.to_csv(out_path, index=False)
    print(f"\nWrote {len(out)} rows -> {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
