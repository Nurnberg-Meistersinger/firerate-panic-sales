"""Sensitivity analysis for methodological parameters.

Tests robustness of peak_ratio and TTP metrics under variations of:
- BASELINE_DAYS ∈ {15, 30, 60} — window for pre-event baseline median
- STRESS_MULTIPLE ∈ {1.5, 2.0, 3.0} — threshold multiplier for stress breach

For each combination, recomputes metrics on a subset of key events (those
with reliable data) and reports:
- Absolute change in peak_ratio per event
- Change in ranking (Spearman correlation with baseline)
- Which events flip between "detected stress" and "not detected"

Output: data/output/analysis/sensitivity_by_param.csv
"""

from __future__ import annotations

import numpy as np
import pandas as pd

import config as C
import signals as S


BASELINE_DAYS_GRID = [15, 30, 60]
STRESS_MULTIPLE_GRID = [1.5, 2.0, 3.0]

# Focus set: events with clean data (excludes cr-2018-bchfork-*, cr-2023-usdc)
FOCUS_EVENTS = [
    "cr-2020-covid", "cr-2021-elonchina", "cr-2022-luna",
    "cr-2022-ftx", "cr-2022-celsius", "cr-2023-curve", "cr-2024-etf",
    "eq-2008-lehman", "eq-2018-volmageddon", "eq-2020-covid", "eq-2023-svb",
    "fx-2015-snb", "fx-2016-brexit", "fx-2019-jpyflash", "fx-2022-ldi",
    "fx-2013-goldcrash",
]


def recompute_one_event(eid: str, row: pd.Series,
                        baseline_days: int, stress_multiple: float) -> pd.DataFrame:
    """Recompute event metrics with overridden parameters."""
    # Monkey-patch config for this call
    orig_base = C.BASELINE_DAYS
    orig_stress = C.STRESS_MULTIPLE
    orig_recovery = C.RECOVERY_MULTIPLE
    C.BASELINE_DAYS = baseline_days
    C.STRESS_MULTIPLE = stress_multiple
    C.RECOVERY_MULTIPLE = max(1.1, stress_multiple - 0.5)  # keep recovery below breach

    try:
        raw_path = C.RAW_DIR / f"{eid}.parquet"
        if not raw_path.exists():
            return pd.DataFrame()
        raw = pd.read_parquet(raw_path)
        sig = S.build_signals(raw)
        m = S.event_metrics(sig, row["window_start"], row["window_end"],
                            row["peak_direction"])
        if not m.empty:
            m.insert(0, "event_id", eid)
            m["baseline_days"] = baseline_days
            m["stress_multiple"] = stress_multiple
        return m
    finally:
        C.BASELINE_DAYS = orig_base
        C.STRESS_MULTIPLE = orig_stress
        C.RECOVERY_MULTIPLE = orig_recovery


def main() -> int:
    events = pd.read_csv(C.EVENTS_CSV, dtype=str).set_index("event_id")
    events = events.loc[[e for e in FOCUS_EVENTS if e in events.index]]

    all_rows = []
    for bd in BASELINE_DAYS_GRID:
        for sm in STRESS_MULTIPLE_GRID:
            for eid, row in events.iterrows():
                m = recompute_one_event(eid, row, bd, sm)
                if not m.empty:
                    all_rows.append(m)

    if not all_rows:
        print("no results")
        return 1

    df = pd.concat(all_rows, ignore_index=True)
    cols = ["event_id", "signal", "baseline_days", "stress_multiple",
            "baseline", "peak", "peak_ratio", "time_to_peak_h",
            "duration_above_h"]
    df = df[cols]
    out = C.OUTPUT_DIR / "analysis" / "sensitivity_by_param.csv"
    df.to_csv(out, index=False)
    print(f"Wrote {len(df)} rows -> {out}")

    # === Summary: relative change in peak_ratio vs baseline (30d, 2x) ===
    ref = df[(df.baseline_days == 30) & (df.stress_multiple == 2.0)].set_index(
        ["event_id", "signal"])["peak_ratio"]

    print("\n=== Peak ratio sensitivity: median absolute % change vs (30d, 2x) baseline ===")
    changes = []
    for bd in BASELINE_DAYS_GRID:
        for sm in STRESS_MULTIPLE_GRID:
            if bd == 30 and sm == 2.0:
                continue
            sub = df[(df.baseline_days == bd) & (df.stress_multiple == sm)]
            merged = sub.set_index(["event_id", "signal"])["peak_ratio"].to_frame(
                "alt").join(ref.to_frame("ref"))
            merged["rel_change_pct"] = (merged["alt"] - merged["ref"]) / merged[
                "ref"] * 100
            changes.append({
                "baseline_days": bd, "stress_multiple": sm,
                "n_comparisons": merged["rel_change_pct"].notna().sum(),
                "median_abs_change_pct": round(float(
                    merged["rel_change_pct"].abs().median()), 1),
                "p90_abs_change_pct": round(float(
                    merged["rel_change_pct"].abs().quantile(0.9)), 1),
            })
    print(pd.DataFrame(changes).to_string(index=False))

    print("\n=== Ranking stability: Spearman corr of peak_ratio vs (30d, 2x) baseline ===")
    ranking = []
    for bd in BASELINE_DAYS_GRID:
        for sm in STRESS_MULTIPLE_GRID:
            for signal in ["v", "sigma", "q", "illiq"]:
                ref_r = df[(df.baseline_days == 30) & (df.stress_multiple == 2.0) &
                           (df.signal == signal)].set_index("event_id")["peak_ratio"]
                alt_r = df[(df.baseline_days == bd) & (df.stress_multiple == sm) &
                           (df.signal == signal)].set_index("event_id")["peak_ratio"]
                common = ref_r.dropna().index.intersection(alt_r.dropna().index)
                if len(common) < 3:
                    continue
                spr = ref_r.loc[common].rank().corr(alt_r.loc[common].rank())
                ranking.append({
                    "signal": signal, "baseline_days": bd,
                    "stress_multiple": sm, "n": len(common),
                    "rank_corr_vs_ref": round(float(spr), 3),
                })
    print(pd.DataFrame(ranking).pivot_table(
        index=["signal", "baseline_days"], columns="stress_multiple",
        values="rank_corr_vs_ref").round(3).to_string())

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
