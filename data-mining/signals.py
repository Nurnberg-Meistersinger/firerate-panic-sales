"""Signal construction and event metrics (mirrors section II of the doc).

From a raw OHLCV(+bid/ask) frame we derive the four FireRate market signals:
  s     bid-ask spread in bps of mid       (only if bid/ask present)
  v     price velocity, normalised price change per bar
  sigma realized volatility, rolling std of log returns
  q     trading volume

Per signal we compute baseline (30d median before the window), peak/baseline
ratio, time-to-peak inside the stress episode, duration above threshold, and
recovery time.

Methodology fixes versus the first cut:
1. Velocity uses log returns where the price is positive; for series with
   non-positive prints (WTI 2020) we fall back to normalised absolute price
   change relative to the rolling reference. The signal stays comparable
   across regimes.
2. The rolling sigma window is adaptive to bar spacing. A 20-bar window on
   daily data is a month; on minute data it would be 20 minutes. We pick a
   window equal to roughly one trading day (24 hours) at the detected bar
   spacing, clamped to [10, 240] bars.
3. Time-to-peak is computed inside the stress episode that contains the peak,
   not from the first breach anywhere in the window. The episode is bounded
   by signal returning to the recovery threshold (1.5x baseline). This
   prevents 60-day TTP numbers on multi-week windows.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

import config as C


# ---------------------------------------------------------------------------
# Signal construction
# ---------------------------------------------------------------------------
def _bar_hours_from_index(idx: pd.Index) -> float:
    """Median index spacing in hours; falls back to 24h for short series."""
    if len(idx) < 2:
        return 24.0
    delta = pd.Series(idx).diff().median()
    if pd.isna(delta):
        return 24.0
    return max(delta.total_seconds() / 3600.0, 1.0 / 60.0)


def _adaptive_sigma_window(idx: pd.Index) -> int:
    """Rolling window aiming at ~24h of bars, clamped to [10, 240]."""
    bar_h = _bar_hours_from_index(idx)
    target_bars = int(round(24.0 / bar_h)) if bar_h > 0 else 20
    return max(10, min(240, target_bars))


def build_signals(df: pd.DataFrame) -> pd.DataFrame:
    """Add s, v, sigma, q, d columns where the inputs allow."""
    out = pd.DataFrame(index=df.index)
    close = pd.to_numeric(df["close"], errors="coerce").astype(float)

    # Velocity: prefer log returns; for non-positive segments use normalised
    # absolute change against the previous valid positive close, so events
    # like negative-price WTI still produce a usable v signal.
    if (close > 0).all():
        logret = np.log(close).diff()
        out["v"] = logret.abs()
    else:
        prev = close.shift(1)
        ref = prev.where(prev > 0).ffill().abs()
        ref = ref.replace(0, np.nan)
        out["v"] = (close - prev).abs() / ref

    # Sigma: rolling std of returns; if any non-positive prints exist, use
    # the normalised-change series defined above for consistency with v.
    window = _adaptive_sigma_window(df.index)
    if (close > 0).all():
        rets = np.log(close).diff()
    else:
        prev = close.shift(1)
        ref = prev.where(prev > 0).ffill().abs().replace(0, np.nan)
        rets = (close - prev) / ref
    out["sigma"] = rets.rolling(window).std()
    out.attrs["sigma_window"] = window

    if "volume" in df.columns:
        vol = pd.to_numeric(df["volume"], errors="coerce").astype(float)
        # Yahoo FX symbols return volume=0 across the board. Drop those so
        # downstream code treats q as missing rather than as a zero baseline.
        if vol.abs().sum() > 0:
            out["q"] = vol

    if {"bid", "ask"}.issubset(df.columns):
        bid = pd.to_numeric(df["bid"], errors="coerce").astype(float)
        ask = pd.to_numeric(df["ask"], errors="coerce").astype(float)
        mid = (bid + ask) / 2.0
        spread_bps = (ask - bid) / mid.replace(0, np.nan) * 1e4
        out["s"] = spread_bps

    # Effective spread proxy from aggTrades price range. Kept as a SEPARATE
    # signal from `s`, not a fallback. Empirical check showed s_eff is
    # methodologically correlated with v (both measure intra-minute price
    # movement), so it is not a valid substitute for real bid-ask spread.
    # It remains useful as its own "intra-minute range" indicator and is
    # reported alongside s in metrics for events where real spread is
    # unavailable, but must not be combined with s in statistics.
    if "s_eff" in df.columns:
        out["s_eff"] = pd.to_numeric(df["s_eff"], errors="coerce").astype(float)

    # Depth at top of book = bid_qty + ask_qty (book thickness on level 1).
    # This is a "drought signal": during stress it drops, not rises.
    if {"bid_qty", "ask_qty"}.issubset(df.columns):
        bid_qty = pd.to_numeric(df["bid_qty"], errors="coerce").astype(float)
        ask_qty = pd.to_numeric(df["ask_qty"], errors="coerce").astype(float)
        depth = bid_qty + ask_qty
        # Zero depth is undefined; drop those rows for the d signal only.
        out["d"] = depth.where(depth > 0, np.nan)

    # Amihud (2002) illiquidity ratio: |return| / dollar_volume per bar.
    # Empirical impact-slope proxy that does not require L2 depth. Robust to
    # jump-dominated stress events (unlike Corwin-Schultz or Roll, which
    # assume continuous Brownian volatility and inflate on jumps). Verified
    # to give qualitatively correct rankings on our 26 events with volume.
    if "volume" in df.columns and (close > 0).all():
        vol = pd.to_numeric(df["volume"], errors="coerce").astype(float)
        logret = np.log(close).diff()
        dollar = (close * vol).replace(0, np.nan)
        illiq = logret.abs() / dollar
        out["illiq"] = illiq.where(illiq > 0, np.nan)

    return out


# ---------------------------------------------------------------------------
# Episode-local helpers
# ---------------------------------------------------------------------------
def _baseline(series: pd.Series, window_start: pd.Timestamp
              ) -> tuple[float, str]:
    """Median of the signal over the 30 days before the window opens.

    Returns (baseline_value, source_tag). source_tag is one of:
      "median"          normal case, strict median of pre-window sample
      "nonzero_median"  strict median was 0; used median of positives
      "mean"            no positives; fell back to mean
      "empty"           no pre-window data at all
      "undefined"       even mean was 0 or non-positive
    """
    base_start = window_start - pd.Timedelta(days=C.BASELINE_DAYS)
    base = series.loc[base_start:window_start].dropna()
    if base.empty:
        return np.nan, "empty"
    med = float(base.median())
    if med > 0:
        return med, "median"
    nonzero = base[base > 0]
    if not nonzero.empty:
        return float(nonzero.median()), "nonzero_median"
    m = float(base.mean())
    if m > 0:
        return m, "mean"
    return np.nan, "undefined"


def _episode_bounds(win: pd.Series, peak_ts: pd.Timestamp,
                    recovery_level: float, direction: str = "up"
                    ) -> tuple[pd.Timestamp, pd.Timestamp]:
    """Find the contiguous stress episode that contains the peak.

    direction="up":   stress = signal above recovery_level (high-stress)
    direction="down": stress = signal below recovery_level (drought)
    """
    if win.empty:
        return peak_ts, peak_ts

    if direction == "down":
        calm_mask = win >= recovery_level
    else:
        calm_mask = win <= recovery_level

    # Backward walk: stop when signal returns to recovery level
    before_mask = calm_mask.loc[:peak_ts]
    calm_before = before_mask[before_mask]
    start = calm_before.index[-1] if len(calm_before) else win.index[0]
    if start != peak_ts:
        loc = win.index.get_loc(start)
        if isinstance(loc, slice):
            loc = loc.stop - 1
        if loc + 1 < len(win):
            start = win.index[loc + 1]

    # Forward walk: stop when signal returns to recovery level
    after_mask = calm_mask.loc[peak_ts:]
    calm_after = after_mask[after_mask]
    end = calm_after.index[0] if len(calm_after) else win.index[-1]

    return start, end


# Signals that hit their stress extreme as a TROUGH (low value), not a PEAK.
# For these the breach/recovery comparisons flip: stress is "below baseline/k",
# calm is "above baseline/k".
DROUGHT_SIGNALS: frozenset[str] = frozenset({"d"})


# ---------------------------------------------------------------------------
# Event metrics
# ---------------------------------------------------------------------------
def event_metrics(signals: pd.DataFrame, window_start: str, window_end: str,
                  peak_direction: str = "up") -> pd.DataFrame:
    """Return one row per signal with the normalised stress metrics."""
    ws = pd.Timestamp(window_start, tz="UTC")
    we = pd.Timestamp(window_end, tz="UTC")
    rows: list[dict] = []

    for name in ["s", "v", "sigma", "q", "d", "s_eff", "illiq"]:
        if name not in signals.columns:
            continue
        full = signals[name].dropna()
        if full.empty:
            continue
        baseline, baseline_source = _baseline(full, ws)
        win = full.loc[ws:we]
        if win.empty or not np.isfinite(baseline) or baseline == 0:
            rows.append(dict(signal=name, baseline=baseline,
                             baseline_source=baseline_source,
                             peak=np.nan, peak_ratio=np.nan,
                             time_to_peak_h=np.nan,
                             duration_above_h=np.nan, recovery_h=np.nan,
                             episode_start=pd.NaT, episode_end=pd.NaT,
                             n_obs=len(win)))
            continue

        # Is this a drought-style signal (stress = trough) or panic-style (stress = peak)?
        is_drought = (
            (name in DROUGHT_SIGNALS) or
            (name == "q" and peak_direction == "down")
        )

        if is_drought:
            # Trough is the "peak of stress"
            peak_val = win.min()
            peak_ts = win.idxmin()
            ratio = baseline / peak_val if peak_val > 0 else np.nan
            breach_level = baseline / C.STRESS_MULTIPLE
            recovery_level = baseline / C.RECOVERY_MULTIPLE
            direction = "down"
        else:
            peak_val = win.max()
            peak_ts = win.idxmax()
            ratio = peak_val / baseline
            breach_level = C.STRESS_MULTIPLE * baseline
            recovery_level = C.RECOVERY_MULTIPLE * baseline
            direction = "up"

        bar_h = _bar_hours_from_index(win.index)

        # Episode-local metrics: scoped to the stress episode containing peak
        ep_start, ep_end = _episode_bounds(win, peak_ts, recovery_level,
                                           direction=direction)
        episode = win.loc[ep_start:ep_end]

        # First breach inside the episode (not in the whole window)
        if is_drought:
            breach = episode[episode <= breach_level]
        else:
            breach = episode[episode >= breach_level]

        if not breach.empty:
            t0 = breach.index[0]
            ttp_h = max(0.0, (peak_ts - t0).total_seconds() / 3600.0)
            if is_drought:
                above_mask = episode <= breach_level
            else:
                above_mask = episode >= breach_level
            dur_h = float(above_mask.sum()) * bar_h
            calm_after_peak = win.loc[peak_ts:]
            if is_drought:
                calm = calm_after_peak[calm_after_peak >= recovery_level]
            else:
                calm = calm_after_peak[calm_after_peak <= recovery_level]
            rec_h = ((calm.index[0] - peak_ts).total_seconds() / 3600.0
                     if not calm.empty else np.nan)
        else:
            ttp_h = dur_h = rec_h = np.nan

        rows.append(dict(
            signal=name,
            baseline=round(baseline, 6),
            baseline_source=baseline_source,
            peak=round(float(peak_val), 6),
            peak_ratio=round(float(ratio), 3) if np.isfinite(ratio) else np.nan,
            time_to_peak_h=round(ttp_h, 2) if np.isfinite(ttp_h) else np.nan,
            duration_above_h=round(dur_h, 2) if np.isfinite(dur_h) else np.nan,
            recovery_h=round(rec_h, 2) if np.isfinite(rec_h) else np.nan,
            episode_start=ep_start,
            episode_end=ep_end,
            n_obs=len(win),
        ))

    return pd.DataFrame(rows)


# Kept for callers outside the module
def _bar_hours(series: pd.Series) -> float:
    return _bar_hours_from_index(series.index)
