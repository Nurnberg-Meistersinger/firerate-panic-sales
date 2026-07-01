"""Econometric estimators of bid-ask spread and illiquidity from OHLC(V).

Three estimators applied to existing raw data for all events:

1. Corwin-Schultz (2012): spread estimator from consecutive high/low pairs.
   Formal derivation: high captures a buy at ask, low captures a sell at bid.
   Two-period variance separates spread from continuous volatility.
   Reference: "A Simple Way to Estimate Bid-Ask Spreads from Daily High and
   Low Prices", Journal of Finance 67(2), 2012.

2. Roll (1984): spread from returns autocovariance.
   Under bid-ask bounce, consecutive returns are negatively correlated,
   and Cov(r_t, r_{t-1}) = -s^2/4. So s = 2 * sqrt(-Cov).
   Reference: "A Simple Implicit Measure of the Effective Bid-Ask Spread
   in an Efficient Market", Journal of Finance 39(4), 1984.

3. Amihud (2002): illiquidity ratio.
   illiq_t = |r_t| / dollar_volume_t. Proxy for price impact of trade size.
   Reference: "Illiquidity and Stock Returns: Cross-Section and Time-Series
   Effects", Journal of Financial Markets 5(1), 2002.

Usage:
  python estimators.py
Outputs data/output/analysis/estimators_by_event.csv and comparison table.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

import config as C


OUT_DIR = C.OUTPUT_DIR / "analysis"
OUT_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Estimator implementations
# ---------------------------------------------------------------------------
def corwin_schultz(high: pd.Series, low: pd.Series) -> pd.Series:
    """Per-bar Corwin-Schultz spread using consecutive pairs of bars.

    Returns proportional spread (multiply by 1e4 for bps). First element NaN.
    Convention: negative alpha (which yields negative spread) clipped to 0.
    """
    h = pd.to_numeric(high, errors="coerce").astype(float)
    l = pd.to_numeric(low, errors="coerce").astype(float)

    mask = (h > 0) & (l > 0) & (h >= l)
    lnhl = pd.Series(np.nan, index=h.index)
    lnhl[mask] = np.log(h[mask] / l[mask])
    lnhl2 = lnhl ** 2

    beta = lnhl2.shift(1) + lnhl2                 # sum of two consecutive periods
    hmax = pd.concat([h.shift(1), h], axis=1).max(axis=1)
    lmin = pd.concat([l.shift(1), l], axis=1).min(axis=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        gamma = np.log(hmax / lmin) ** 2

    k = 3.0 - 2.0 * np.sqrt(2.0)
    with np.errstate(invalid="ignore"):
        alpha = ((np.sqrt(2.0 * beta) - np.sqrt(beta)) / k
                 - np.sqrt(gamma / k))
        spread = 2.0 * (np.exp(alpha) - 1.0) / (1.0 + np.exp(alpha))

    spread = spread.where(spread.notna(), np.nan)
    spread = spread.where(spread >= 0, 0.0)       # clip negatives per paper
    return spread


def roll_spread(returns: pd.Series, window: int = 60) -> pd.Series:
    """Rolling Roll (1984) spread estimator.

    spread = 2*sqrt(-cov(r_t, r_{t-1})) when cov < 0, else 0.
    Proportional; multiply by 1e4 for bps.
    """
    r = pd.to_numeric(returns, errors="coerce").astype(float)
    r_lag = r.shift(1)
    cov = r.rolling(window=window,
                    min_periods=max(window // 2, 10)).cov(r_lag)
    with np.errstate(invalid="ignore"):
        s = pd.Series(np.where(cov < 0, 2.0 * np.sqrt(-cov), 0.0),
                      index=r.index)
    return s.where(cov.notna(), np.nan)


def amihud_illiq(returns: pd.Series, price: pd.Series,
                 volume: pd.Series) -> pd.Series:
    """Amihud illiquidity ratio per bar: |return| / (price * volume)."""
    r = pd.to_numeric(returns, errors="coerce").astype(float).abs()
    p = pd.to_numeric(price, errors="coerce").astype(float)
    v = pd.to_numeric(volume, errors="coerce").astype(float)
    dv = (p * v).replace(0, np.nan)
    return r / dv


# ---------------------------------------------------------------------------
# Event-level metrics using the same baseline/peak scheme as signals.py
# ---------------------------------------------------------------------------
def _baseline_median(series: pd.Series, ws: pd.Timestamp,
                     days: int = 30) -> float:
    base = series.loc[ws - pd.Timedelta(days=days):ws].dropna()
    if base.empty:
        return np.nan
    med = float(base.median())
    if med > 0:
        return med
    nonzero = base[base > 0]
    if not nonzero.empty:
        return float(nonzero.median())
    return float(base.mean()) if not base.empty else np.nan


def _event_metric(series: pd.Series, ws: pd.Timestamp, we: pd.Timestamp,
                  name: str, resolution: str) -> dict | None:
    if series is None:
        return None
    full = series.dropna()
    if full.empty:
        return None
    win = full.loc[ws:we].dropna()
    if win.empty:
        return None
    baseline = _baseline_median(full, ws)
    if not np.isfinite(baseline) or baseline == 0:
        return None
    peak = float(win.max())
    ratio = peak / baseline
    return dict(estimator=name, baseline=round(baseline, 6),
                peak=round(peak, 6), peak_ratio=round(ratio, 3),
                resolution=resolution, n_bars_win=len(win))


def compute_for_event(eid: str, row: pd.Series) -> list[dict]:
    """Compute all three estimators from raw parquet, return metric rows."""
    raw_path = C.RAW_DIR / f"{eid}.parquet"
    if not raw_path.exists():
        return []
    raw = pd.read_parquet(raw_path)
    if len(raw) < 2 or "close" not in raw.columns:
        return []

    bar_h = (pd.Series(raw.index).diff().median().total_seconds() / 3600.0
             if len(raw) > 1 else 24.0)
    is_minute = bar_h < 1
    resolution = "minute" if is_minute else "day"
    roll_window = 60 if is_minute else 20

    close = pd.to_numeric(raw["close"], errors="coerce").astype(float)
    logret = np.log(close.where(close > 0)).diff()

    ws = pd.Timestamp(row["window_start"], tz="UTC")
    we = pd.Timestamp(row["window_end"], tz="UTC")

    out: list[dict] = []

    # 1. Corwin-Schultz
    if {"high", "low"}.issubset(raw.columns):
        cs = corwin_schultz(raw["high"], raw["low"]) * 1e4  # to bps
        m = _event_metric(cs, ws, we, "corwin_schultz_bps", resolution)
        if m:
            m["event_id"] = eid
            out.append(m)

    # 2. Roll
    roll = roll_spread(logret, window=roll_window) * 1e4  # to bps
    m = _event_metric(roll, ws, we, "roll_bps", resolution)
    if m:
        m["event_id"] = eid
        out.append(m)

    # 3. Amihud
    if "volume" in raw.columns:
        amihud = amihud_illiq(logret, close, raw["volume"])
        m = _event_metric(amihud, ws, we, "amihud_illiq", resolution)
        if m:
            m["event_id"] = eid
            out.append(m)

    return out


def main() -> int:
    events = pd.read_csv(C.EVENTS_CSV, dtype=str).set_index("event_id")
    rows: list[dict] = []
    for eid, row in events.iterrows():
        rows.extend(compute_for_event(eid, row))

    if not rows:
        print("no results")
        return 1

    df = pd.DataFrame(rows)
    df = df[["event_id", "resolution", "estimator", "baseline", "peak",
             "peak_ratio", "n_bars_win"]]
    df = df.sort_values(["event_id", "estimator"]).reset_index(drop=True)
    out_path = OUT_DIR / "estimators_by_event.csv"
    df.to_csv(out_path, index=False)
    print(f"Wrote {len(df)} rows -> {out_path}")

    # Comparison: for events where we have real s in metrics.csv, show
    # real s side-by-side with Corwin-Schultz and Roll estimates.
    try:
        metrics = pd.read_csv(C.OUTPUT_DIR / "metrics.csv")
    except FileNotFoundError:
        print("metrics.csv not found; skipping comparison")
        return 0

    real_s = (metrics[metrics.signal == "s"]
              [["event_id", "resolution", "baseline", "peak", "peak_ratio"]]
              .rename(columns={"baseline": "s_real_baseline",
                               "peak": "s_real_peak",
                               "peak_ratio": "s_real_ratio"}))
    cs = (df[df.estimator == "corwin_schultz_bps"]
          [["event_id", "resolution", "baseline", "peak", "peak_ratio"]]
          .rename(columns={"baseline": "cs_baseline",
                           "peak": "cs_peak",
                           "peak_ratio": "cs_ratio"}))
    roll = (df[df.estimator == "roll_bps"]
            [["event_id", "resolution", "baseline", "peak", "peak_ratio"]]
            .rename(columns={"baseline": "roll_baseline",
                             "peak": "roll_peak",
                             "peak_ratio": "roll_ratio"}))

    comp = real_s.merge(cs, on=["event_id", "resolution"], how="left")
    comp = comp.merge(roll, on=["event_id", "resolution"], how="left")
    comp_path = OUT_DIR / "estimators_vs_real_spread.csv"
    comp.to_csv(comp_path, index=False)
    print(f"Wrote comparison -> {comp_path}")
    print()
    print("=== Real spread vs estimator spreads (7 events with real s) ===")
    print(comp.round(2).to_string(index=False))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
