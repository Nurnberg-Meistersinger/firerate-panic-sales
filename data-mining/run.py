"""Orchestrator for the Panic Sales free first pass.

This version intentionally excludes bid/ask and spread data. It aims for
maximum event coverage using free OHLCV/close sources and writes explicit
coverage/quality reports next to metrics.csv.

Usage:
  python run.py
  python run.py --only cr-2022-ftx eq-2008-lehman
  python run.py --resolution minute --append --only cr-2022-ftx
  python run.py --list

Output:
  data/raw/<event_id>.parquet       raw OHLCV/close pull
  data/signals/<event_id>.parquet   v/sigma/q signal series
  data/output/metrics.csv           one row per (event, signal)
  data/output/event_status.csv      one row per event with source/symbol/status
  data/output/missing.csv           events with no usable metrics
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass

import pandas as pd

import config as C
import fetchers as F
import signals as S


CRYPTO_FALLBACKS: dict[str, list[str]] = {
    # BCH hash war is now two events. ABC and SV legs tracked separately to
    # avoid silently picking one as proxy for the other.
    "cr-2018-bchfork-abc": ["BCHABCUSDT", "BCHUSDT"],
    "cr-2018-bchfork-sv": ["BCHSVUSDT", "BSVUSDT"],
    # LUNA data around the collapse can halt/split; BTCUSDT keeps market-stress coverage.
    "cr-2022-luna": ["LUNAUSDT", "LUNCUSDT", "BTCUSDT"],
    # USDC depeg: the event IS the stablecoin moving away from 1.00. Do NOT
    # fall back to BTC/ETH on day resolution because that would mask a depeg
    # with a non-depeg series. Minute pass has USDCUSDT data; if day USDC is
    # too sparse we accept "no_metrics" for that resolution.
    "cr-2023-usdc": ["USDCUSDT"],
    "cr-2023-curve": ["CRVUSDT", "BTCUSDT"],
}

# Optional daily close fallbacks for manual/second-pass FX events. These are
# not spread/bid-ask data. They are daily OHLCV/close proxies only.
MANUAL_DAILY_FALLBACKS: dict[str, tuple[str, str]] = {
    # These are daily OHLC/close proxies only; no bid/ask/spread.
    # Yahoo FX symbols are more reliable here than Stooq for these windows.
    "fx-2013-goldcrash": ("yahoo", "GC=F"),
    "fx-2015-snb": ("yahoo", "EURCHF=X"),
    "fx-2016-brexit": ("yahoo", "GBPUSD=X"),
    "fx-2019-jpyflash": ("yahoo", "USDJPY=X"),
    "fx-2022-ldi": ("yahoo", "GBPUSD=X"),
}


@dataclass
class FetchStatus:
    event_id: str
    event_name: str
    asset_class: str
    requested_source: str
    requested_symbol: str
    source_used: str = ""
    symbol_used: str = ""
    resolution: str = "day"
    raw_bars: int = 0
    signals: str = ""
    status: str = "missing"
    quality_note: str = ""


def load_events() -> pd.DataFrame:
    return pd.read_csv(C.EVENTS_CSV, dtype=str)


def _pull_start(row: pd.Series) -> str:
    start = row["window_start"]
    return (pd.Timestamp(start) - pd.Timedelta(days=C.BASELINE_DAYS + 5)).strftime("%Y-%m-%d")


def _min_bars(resolution: str, user_min_bars: int | None) -> int:
    if user_min_bars is not None:
        return user_min_bars
    return 120 if resolution == "minute" else 20


def fetch_one(row: pd.Series, args: argparse.Namespace) -> pd.DataFrame:
    src = row["primary_source"]
    sym = row["symbol"]
    eid = row["event_id"]
    start, end = row["window_start"], row["window_end"]
    pull_start = _pull_start(row)
    min_bars = _min_bars(args.resolution, args.min_bars)

    if src == "yahoo":
        return F.fetch_yahoo_with_fallback(sym, pull_start, end, min_bars=min_bars)

    if src == "fred":
        return F.fetch_fred(sym, pull_start, end)

    if src == "cryptocompare":
        return F.fetch_cryptocompare(sym, pull_start, end, resolution="day")

    if src == "binance_s3":
        symbols = CRYPTO_FALLBACKS.get(eid, [sym])
        if args.resolution == "minute":
            source = "api" if args.crypto_minute_source == "binance-api" else "s3"
            return F.fetch_with_fallbacks(
                symbols,
                lambda s: F.fetch_binance_minute(s, pull_start, end, source=source),
                min_bars=min_bars,
                label="crypto-minute",
            )

        if args.crypto_daily_source == "cryptocompare":
            return F.fetch_with_fallbacks(
                symbols,
                lambda s: F.fetch_cryptocompare(s, pull_start, end, resolution="day"),
                min_bars=min_bars,
                label="crypto-daily",
            )
        source = "s3" if args.crypto_daily_source == "binance-s3" else "api"
        return F.fetch_with_fallbacks(
            symbols,
            lambda s: F.fetch_binance_daily(s, pull_start, end, source=source),
            min_bars=min_bars,
            label="crypto-daily",
        )

    if src == "dukascopy":
        if args.dukascopy:
            # Real tick pull with bid/ask. Resample to chosen resolution.
            # Use a shorter baseline lookback than the equity 30-day default:
            # FX events are short and minute resolution makes 14d of baseline
            # already ~20k bars per signal, which is plenty for the median.
            resample = "1min" if args.resolution == "minute" else "1D"
            try:
                return F.fetch_dukascopy_tick(
                    sym, pull_start, end, resample=resample,
                    lookback_days=args.dukascopy_lookback_days,
                )
            except Exception as exc:  # noqa: BLE001
                print(f"  [dukascopy] {sym} failed: {exc}; falling back to daily proxy")
        if not args.manual_daily_fallbacks:
            print(f"  [skip] {eid}: manual source skipped in free no-spread pass")
            return pd.DataFrame()
        fallback = MANUAL_DAILY_FALLBACKS.get(eid)
        if not fallback:
            print(f"  [skip] {eid}: no daily fallback configured")
            return pd.DataFrame()
        fb_source, fb_symbol = fallback
        print(f"  [daily fallback] {src}/{sym} -> {fb_source}/{fb_symbol} (no bid/ask/spread)")
        if fb_source == "yahoo":
            return F.fetch_yahoo_with_fallback(fb_symbol, pull_start, end, min_bars=min_bars)
        if fb_source == "stooq":
            return F.fetch_stooq(fb_symbol, pull_start, end)

    if src == "polygon":
        return F.fetch_polygon_intraday(sym, pull_start, end,
                                        include_nbbo=args.polygon_nbbo)

    print(f"  [skip] {eid}: unknown source {src}")
    return pd.DataFrame()


def _merge_bookticker(row: pd.Series, raw: pd.DataFrame,
                      args: argparse.Namespace) -> pd.DataFrame:
    """Pull Binance bookTicker for the same window and join into raw.

    Only runs for crypto + minute resolution + when raw has data. Adds
    bid/ask/bid_qty/ask_qty columns aligned to raw's minute timestamps.
    """
    if args.resolution != "minute":
        return raw
    if row["asset_class"] != "crypto":
        return raw
    if raw.empty:
        return raw

    # Use the symbol that was actually used in the main fetch (proxy-aware)
    symbol = raw.attrs.get("symbol_used") or row["symbol"]
    if not symbol or symbol.endswith("USD") and not symbol.endswith("USDT"):
        # bookTicker availability is mostly for USDT pairs on Binance Vision
        pass

    start = raw.index.min().strftime("%Y-%m-%d")
    end = raw.index.max().strftime("%Y-%m-%d")

    try:
        bt = F.fetch_binance_bookticker(symbol, start, end)
    except Exception as exc:  # noqa: BLE001
        print(f"  [bookticker] {symbol}: failed: {exc}")
        return raw

    if bt is None or bt.empty:
        print(f"  [bookticker] {symbol}: no data in window")
        return raw

    # Align both to minute index, left join on raw
    raw_keys = raw.index.floor("min")
    bt_keys = bt.index.floor("min")
    bt_aligned = bt[~bt_keys.duplicated(keep="last")].copy()
    bt_aligned.index = bt_keys[~bt_keys.duplicated(keep="last")]

    merged = raw.copy()
    merged.index = raw_keys
    # Drop any potential dup minutes in raw (rare but safe)
    merged = merged[~merged.index.duplicated(keep="last")]
    cols = [c for c in ["bid", "ask", "bid_qty", "ask_qty"] if c in bt_aligned.columns]
    merged = merged.join(bt_aligned[cols], how="left")

    coverage = float(merged["bid"].notna().mean()) if "bid" in merged.columns else 0.0
    merged.attrs = dict(raw.attrs)
    merged.attrs["bookticker_symbol"] = symbol
    merged.attrs["bookticker_coverage"] = coverage
    existing_note = merged.attrs.get("quality_note", "") or ""
    merged.attrs["quality_note"] = (existing_note +
                                    f"; bookticker_cov={coverage:.1%}").strip("; ")
    print(f"  [bookticker] {symbol}: coverage {coverage:.1%} of {len(merged)} minute bars")
    return merged


def _merge_aggtrades(row: pd.Series, raw: pd.DataFrame,
                     args: argparse.Namespace) -> pd.DataFrame:
    """Pull Binance aggTrades and merge a per-minute effective-spread proxy.

    Adds s_eff (bps), agg_volume, n_trades columns. signals.py uses s_eff
    as a fallback for the s signal when real bid/ask is not present.
    """
    if args.resolution != "minute":
        return raw
    if row["asset_class"] != "crypto":
        return raw
    if raw.empty:
        return raw

    symbol = raw.attrs.get("symbol_used") or row["symbol"]
    start = raw.index.min().strftime("%Y-%m-%d")
    end = raw.index.max().strftime("%Y-%m-%d")

    try:
        at = F.fetch_binance_aggtrades(symbol, start, end)
    except Exception as exc:  # noqa: BLE001
        print(f"  [aggtrades] {symbol}: failed: {exc}")
        return raw

    if at is None or at.empty:
        print(f"  [aggtrades] {symbol}: no data in window")
        return raw

    raw_keys = raw.index.floor("min")
    at_keys = at.index.floor("min")
    at_aligned = at[~at_keys.duplicated(keep="last")].copy()
    at_aligned.index = at_keys[~at_keys.duplicated(keep="last")]

    merged = raw.copy()
    merged.index = raw_keys
    merged = merged[~merged.index.duplicated(keep="last")]
    cols = [c for c in ["s_eff", "agg_volume", "n_trades"]
            if c in at_aligned.columns]
    merged = merged.join(at_aligned[cols], how="left")

    coverage = (float(merged["s_eff"].notna().mean())
                if "s_eff" in merged.columns else 0.0)
    merged.attrs = dict(raw.attrs)
    merged.attrs["aggtrades_symbol"] = symbol
    merged.attrs["aggtrades_coverage"] = coverage
    existing_note = merged.attrs.get("quality_note", "") or ""
    merged.attrs["quality_note"] = (existing_note +
                                    f"; aggtrades_cov={coverage:.1%}").strip("; ")
    print(f"  [aggtrades] {symbol}: coverage {coverage:.1%} of "
          f"{len(merged)} minute bars")
    return merged


def _append_metrics(new_metrics: pd.DataFrame, append: bool) -> pd.DataFrame:
    out_path = C.OUTPUT_DIR / "metrics.csv"
    if append and out_path.exists():
        old = pd.read_csv(out_path)
        out = pd.concat([old, new_metrics], ignore_index=True)
        dedupe_cols = [c for c in ["event_id", "signal", "resolution"] if c in out.columns]
        out = out.drop_duplicates(subset=dedupe_cols, keep="last")
        out = out.sort_values(["event_id", "signal"]).reset_index(drop=True)
    else:
        out = new_metrics
    out.to_csv(out_path, index=False)
    print(f"\nWrote {len(out)} rows -> {out_path}")
    return out


def _write_status(status_rows: list[FetchStatus], append: bool) -> None:
    status_path = C.OUTPUT_DIR / "event_status.csv"
    missing_path = C.OUTPUT_DIR / "missing.csv"
    new_status = pd.DataFrame([s.__dict__ for s in status_rows])
    if append and status_path.exists():
        old = pd.read_csv(status_path)
        out = pd.concat([old, new_status], ignore_index=True)
        out = out.drop_duplicates(subset=["event_id", "resolution"], keep="last")
        out = out.sort_values(["event_id", "resolution"]).reset_index(drop=True)
    else:
        out = new_status
    out.to_csv(status_path, index=False)
    out[out["status"] != "ok"].to_csv(missing_path, index=False)
    print(f"Wrote status -> {status_path}")
    print(f"Wrote missing -> {missing_path}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", help="event_id(s) to run")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--resolution", choices=["day", "minute"], default="day")
    ap.add_argument(
        "--crypto-daily-source",
        choices=["binance-api", "binance-s3", "binance", "cryptocompare"],
        default="binance-api",
        help="For crypto day mode. 'binance' is an alias for 'binance-api'.",
    )
    ap.add_argument(
        "--crypto-minute-source",
        choices=["binance-api", "binance-s3", "binance"],
        default="binance-api",
        help="For crypto minute mode. 'binance' is an alias for 'binance-api'.",
    )
    ap.add_argument("--min-bars", type=int, default=None,
                    help="Minimum bars before accepting a fallback candidate. Defaults: day=20, minute=120.")
    ap.add_argument("--manual-daily-fallbacks", action="store_true",
                    help="Use daily close/OHLCV proxies for dukascopy/manual FX events. No bid/ask/spread.")
    ap.add_argument("--bookticker", action="store_true",
                    help="For crypto+minute, also pull Binance bookTicker and merge "
                         "bid/ask/bid_qty/ask_qty by minute. Unlocks s and d signals.")
    ap.add_argument("--aggtrades", action="store_true",
                    help="For crypto+minute, pull Binance aggTrades and merge "
                         "an effective-spread proxy s_eff. Use for events before "
                         "the bookTicker archive cutoff (~mid-2023).")
    ap.add_argument("--dukascopy", action="store_true",
                    help="Pull real Dukascopy tick with bid/ask for FX/metal events. "
                         "Free; first-class spread (s) signal. Heavy I/O.")
    ap.add_argument("--dukascopy-lookback-days", type=int, default=None,
                    help="If set, trims Dukascopy pull to this many days before window_end. "
                         "Default = no trim (full pull_start..window_end, matches BASELINE_DAYS). "
                         "Use 7-14 for quick smoke tests.")
    ap.add_argument("--polygon-nbbo", action="store_true",
                    help="When primary_source=polygon, sample NBBO for spread. Costs more API calls.")
    ap.add_argument("--append", action="store_true",
                    help="Append/update output CSVs instead of overwriting them.")
    args = ap.parse_args()

    if args.crypto_daily_source == "binance":
        args.crypto_daily_source = "binance-api"
    if args.crypto_minute_source == "binance":
        args.crypto_minute_source = "binance-api"

    events = load_events()
    if args.list:
        print(events[["event_id", "event_date", "name", "asset_class", "primary_source", "symbol"]].to_string(index=False))
        return 0
    if args.only:
        events = events[events["event_id"].isin(args.only)]
        if events.empty:
            print("No matching event_id.")
            return 1

    all_metrics: list[pd.DataFrame] = []
    statuses: list[FetchStatus] = []

    for _, row in events.iterrows():
        eid = row["event_id"]
        print(f"[{eid}] {row['name']} ({row['primary_source']})")
        st = FetchStatus(
            event_id=eid,
            event_name=row["name"],
            asset_class=row["asset_class"],
            requested_source=row["primary_source"],
            requested_symbol=row["symbol"],
            resolution=args.resolution,
        )
        try:
            raw = fetch_one(row, args)
        except Exception as exc:  # noqa: BLE001
            st.status = "fetch_failed"
            st.quality_note = str(exc)
            print(f"  fetch failed: {exc}")
            statuses.append(st)
            continue

        if raw is None or raw.empty:
            print("  no data")
            statuses.append(st)
            continue

        # Optional: merge Binance bookTicker for spread + depth signals
        if args.bookticker:
            raw = _merge_bookticker(row, raw, args)

        # Optional: merge Binance aggTrades for s_eff (proxy spread)
        if args.aggtrades:
            raw = _merge_aggtrades(row, raw, args)

        # Avoid log(0)/negative warnings in v/sigma; keep only usable OHLC rows.
        if "close" in raw.columns:
            before = len(raw)
            raw = raw[pd.to_numeric(raw["close"], errors="coerce") > 0]
            if len(raw) < before:
                note = raw.attrs.get("quality_note", "")
                raw.attrs["quality_note"] = (note + f"; dropped_nonpositive_close={before-len(raw)}").strip("; ")
        if raw.empty:
            print("  no usable positive-close data")
            statuses.append(st)
            continue

        raw.to_parquet(C.RAW_DIR / f"{eid}.parquet")
        sig = S.build_signals(raw)
        sig.to_parquet(C.SIGNALS_DIR / f"{eid}.parquet")

        m = S.event_metrics(sig, row["window_start"], row["window_end"], row["peak_direction"])
        if m.empty:
            st.status = "no_metrics"
            st.raw_bars = len(raw)
            st.source_used = raw.attrs.get("source_used", "")
            st.symbol_used = raw.attrs.get("symbol_used", "")
            st.quality_note = raw.attrs.get("quality_note", "")
            print(f"  no metrics ({len(raw)} bars)")
            statuses.append(st)
            continue

        source_used = raw.attrs.get("source_used", row["primary_source"])
        symbol_used = raw.attrs.get("symbol_used", row["symbol"])
        quality_note = raw.attrs.get("quality_note", "")
        m.insert(0, "event_id", eid)
        m.insert(1, "asset_class", row["asset_class"])
        m.insert(2, "resolution", args.resolution)
        m.insert(3, "source_used", source_used)
        m.insert(4, "symbol_used", symbol_used)
        all_metrics.append(m)

        got = ", ".join(m["signal"].tolist())
        st.status = "ok"
        st.raw_bars = len(raw)
        st.source_used = source_used
        st.symbol_used = symbol_used
        st.signals = got
        st.quality_note = quality_note
        statuses.append(st)
        print(f"  signals: {got or 'none'}  ({len(raw)} bars; {source_used}/{symbol_used})")
        if quality_note:
            print(f"  note: {quality_note}")

    if all_metrics:
        new_out = pd.concat(all_metrics, ignore_index=True)
        _append_metrics(new_out, args.append)
    else:
        print("\nNo metrics produced.")
    _write_status(statuses, args.append)
    return 0


if __name__ == "__main__":
    sys.exit(main())
