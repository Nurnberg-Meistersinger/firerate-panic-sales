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

SIGNALS = ["s", "v", "sigma", "q", "d", "s_eff", "illiq"]
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


def _bootstrap_median_ci(values: pd.Series, n_boot: int = 2000,
                         seed: int = 42) -> tuple[float, float]:
    """Percentile bootstrap 95% CI for the median of `values`.

    Returns (lower_95, upper_95). With small n (<10) the CI is intentionally
    wide, which is the honest signal to the reader.
    """
    import numpy as np
    arr = values.dropna().values
    if len(arr) == 0:
        return float("nan"), float("nan")
    rng = np.random.default_rng(seed)
    boots = rng.choice(arr, size=(n_boot, len(arr)), replace=True)
    medians = np.median(boots, axis=1)
    return (float(np.percentile(medians, 2.5)),
            float(np.percentile(medians, 97.5)))


def within_event_summary(within: pd.DataFrame) -> pd.DataFrame:
    """Median, range, and count of correlations per (signal_a, signal_b),
    with 95% bootstrap CIs so small-n findings show their uncertainty."""
    if within.empty:
        return pd.DataFrame()
    grp = within.groupby(["signal_a", "signal_b"])

    rows = []
    for (a, b), sub in grp:
        lo, hi = _bootstrap_median_ci(sub["spearman"])
        rows.append({
            "signal_a": a, "signal_b": b,
            "n_events": len(sub),
            "median_pearson": round(float(sub["pearson"].median()), 3),
            "median_spearman": round(float(sub["spearman"].median()), 3),
            "spearman_ci95_lo": round(lo, 3),
            "spearman_ci95_hi": round(hi, 3),
            "min_spearman": round(float(sub["spearman"].min()), 3),
            "max_spearman": round(float(sub["spearman"].max()), 3),
        })
    return pd.DataFrame(rows)


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

    # Bootstrap 95% CI for each cell of the spearman matrix. Resample events
    # (rows) with replacement 2000 times, recompute correlations, take
    # 2.5%/97.5% percentiles. Small-n events (<10) will show wide CIs.
    import numpy as np
    n_events = len(pivot_full)
    rng = np.random.default_rng(42)
    n_boot = 2000
    boot_mats = np.zeros((n_boot, len(pivot_full.columns),
                          len(pivot_full.columns)))
    for i in range(n_boot):
        idx = rng.integers(0, n_events, size=n_events)
        sample = pivot_full.iloc[idx].rank().corr(method="pearson").values
        boot_mats[i] = sample
    spearman_ci_lo = pd.DataFrame(
        np.nanpercentile(boot_mats, 2.5, axis=0).round(3),
        index=pivot_full.columns, columns=pivot_full.columns)
    spearman_ci_hi = pd.DataFrame(
        np.nanpercentile(boot_mats, 97.5, axis=0).round(3),
        index=pivot_full.columns, columns=pivot_full.columns)
    # Combine into single readable frame with "point [lo, hi]" strings
    spearman_with_ci = pd.DataFrame(
        [[f"{spearman_mat.iat[i,j]:.2f} [{spearman_ci_lo.iat[i,j]:.2f}, "
          f"{spearman_ci_hi.iat[i,j]:.2f}]"
          for j in range(len(spearman_mat.columns))]
         for i in range(len(spearman_mat.index))],
        index=spearman_mat.index, columns=spearman_mat.columns)

    coverage = pd.DataFrame({
        "signal": pivot.columns,
        "n_events_with_signal": pivot.notna().sum().values,
        "n_events_in_corr": pivot_full.notna().sum().values,
    })
    return pearson_mat, spearman_mat, coverage, spearman_with_ci


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
    pearson_mat, spearman_mat, coverage, spearman_with_ci = across_event_correlations(metrics)
    pearson_mat.to_csv(out_dir / "correlations_across_events_pearson.csv")
    spearman_mat.to_csv(out_dir / "correlations_across_events_spearman.csv")
    spearman_with_ci.to_csv(out_dir / "correlations_across_events_spearman_ci95.csv")
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
