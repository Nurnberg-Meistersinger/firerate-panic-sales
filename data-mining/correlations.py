"""Correlation analysis of FireRate signals.

Two views, both written to data/output/analysis/:

1. Within-event correlations. For each event with minute-resolution signals,
   compute pairwise Pearson and Spearman correlations of (s, v, sigma, q, d)
   time series during the event window. This answers: "during a specific
   stress episode, do these signals move together or independently?"

2. Across-event correlations. For each signal, take the peak_ratio across
   events from metrics.csv; compute correlations across signals. This answers:
   "do events where one signal peaks high also tend to have other signals
   peak high?"

Notes on the d signal: depth-at-top behaves inversely (stress = trough, not
peak). In within-event correlation it will show negative correlation with
v/sigma/q/s in raw form. We compute on raw values and let the sign speak.
In across-event correlation we use peak_ratio which is always >=1 by
construction (we report baseline/peak for drought signals), so direction is
hidden — the cross-signal sign is purely about whether ratios co-spike.

Usage:
  python correlations.py
"""

from __future__ import annotations

from itertools import combinations
from pathlib import Path

import pandas as pd

import config as C

SIGNALS = ["s", "v", "sigma", "q", "d", "s_eff"]
MIN_OBS_PAIR = 100         # minimum bars per pair to compute a correlation
MIN_OBS_PER_SIGNAL = 100   # minimum non-null bars for a signal to be considered


def detect_resolution_hours(idx: pd.Index) -> float:
    if len(idx) < 2:
        return 24.0
    delta = pd.Series(idx).diff().median()
    return delta.total_seconds() / 3600.0 if pd.notna(delta) else 24.0


def within_event_correlations(events: pd.DataFrame) -> pd.DataFrame:
    """One row per (event, signal pair) with Pearson and Spearman correlations."""
    rows = []
    for eid, row in events.iterrows():
        sig_path = C.SIGNALS_DIR / f"{eid}.parquet"
        if not sig_path.exists():
            continue
        sig = pd.read_parquet(sig_path)
        bar_h = detect_resolution_hours(sig.index)
        if bar_h > 1.0:  # need intraday resolution to get meaningful corr
            continue

        ws = pd.Timestamp(row["window_start"], tz="UTC")
        we = pd.Timestamp(row["window_end"], tz="UTC")
        win = sig.loc[ws:we]

        avail = [s for s in SIGNALS
                 if s in win.columns and win[s].notna().sum() >= MIN_OBS_PER_SIGNAL]
        if len(avail) < 2:
            continue

        for a, b in combinations(avail, 2):
            pair = win[[a, b]].dropna()
            if len(pair) < MIN_OBS_PAIR:
                continue
            rows.append({
                "event_id": eid,
                "asset_class": row["asset_class"],
                "signal_a": a,
                "signal_b": b,
                "n_obs": len(pair),
                "pearson": round(float(pair[a].corr(pair[b], method="pearson")), 3),
                "spearman": round(float(pair[a].rank().corr(pair[b].rank(), method="pearson")), 3),
            })
    return pd.DataFrame(rows)


def within_event_summary(within: pd.DataFrame) -> pd.DataFrame:
    """Median, range, and count of correlations per (signal_a, signal_b)."""
    if within.empty:
        return pd.DataFrame()
    grp = within.groupby(["signal_a", "signal_b"])
    summary = grp.agg(
        n_events=("event_id", "count"),
        median_pearson=("pearson", "median"),
        median_spearman=("spearman", "median"),
        min_spearman=("spearman", "min"),
        max_spearman=("spearman", "max"),
    ).reset_index()
    for col in ["median_pearson", "median_spearman", "min_spearman", "max_spearman"]:
        summary[col] = summary[col].round(3)
    return summary


def across_event_correlations(metrics: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Returns: pearson matrix, spearman matrix, coverage table."""
    # Prefer minute resolution where available; fall back to day
    metrics = metrics.copy()
    res_rank = {"minute": 0, "day": 1}
    metrics["_res_rank"] = metrics["resolution"].map(res_rank).fillna(2)
    metrics = (metrics.sort_values(["event_id", "signal", "_res_rank"])
               .drop_duplicates(subset=["event_id", "signal"], keep="first"))

    pivot = metrics.pivot_table(index="event_id", columns="signal",
                                values="peak_ratio", aggfunc="first")
    pivot = pivot.reindex(columns=[s for s in SIGNALS if s in pivot.columns])

    # Drop events with fewer than 3 signals
    pivot_full = pivot.dropna(thresh=3)

    pearson_mat = pivot_full.corr(method="pearson").round(3)
    # Spearman via ranks to avoid the scipy dependency.
    spearman_mat = pivot_full.rank().corr(method="pearson").round(3)

    coverage = pd.DataFrame({
        "signal": pivot.columns,
        "n_events_with_signal": pivot.notna().sum().values,
        "n_events_in_corr": pivot_full.notna().sum().values,
    })
    return pearson_mat, spearman_mat, coverage


def main() -> int:
    out_dir = C.OUTPUT_DIR / "analysis"
    out_dir.mkdir(parents=True, exist_ok=True)

    events = pd.read_csv(C.EVENTS_CSV, dtype=str).set_index("event_id")
    metrics = pd.read_csv(C.OUTPUT_DIR / "metrics.csv")

    # 1. Within-event
    within = within_event_correlations(events)
    within.to_csv(out_dir / "correlations_within_event.csv", index=False)
    summary = within_event_summary(within)
    summary.to_csv(out_dir / "correlations_within_event_summary.csv", index=False)

    # 2. Across-event
    pearson_mat, spearman_mat, coverage = across_event_correlations(metrics)
    pearson_mat.to_csv(out_dir / "correlations_across_events_pearson.csv")
    spearman_mat.to_csv(out_dir / "correlations_across_events_spearman.csv")
    coverage.to_csv(out_dir / "correlations_across_events_coverage.csv", index=False)

    # Console summary
    print("=== Within-event correlations: per-pair summary (Spearman) ===")
    print(f"Total events analysed: {within['event_id'].nunique() if not within.empty else 0}")
    print(f"Total pair-rows: {len(within)}")
    print()
    if not summary.empty:
        pretty = summary[["signal_a", "signal_b", "n_events",
                          "median_spearman", "min_spearman", "max_spearman"]]
        print(pretty.to_string(index=False))
    print()
    print("=== Across-event correlations: peak_ratio matrix (Spearman) ===")
    print(spearman_mat.to_string())
    print()
    print("=== Coverage ===")
    print(coverage.to_string(index=False))
    print()
    print(f"Outputs written to {out_dir}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
