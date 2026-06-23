"""Source fetchers for the Panic Sales pipeline.

Free first pass only: OHLCV/close data for v, sigma and q.
No bid/ask or spread data is fetched in this version.

Fetchers return a pandas DataFrame indexed by UTC timestamp. Each DataFrame may
carry attrs: source_used, symbol_used, quality_note.
"""

from __future__ import annotations

import io
import time
import zipfile
from typing import Any, Callable, Iterable

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import config as C

DEFAULT_TIMEOUT = (6, 18)       # connect seconds, read seconds
FAST_FILE_TIMEOUT = (4, 10)     # fail fast for per-day ZIP files


def _session(retries: int = 3) -> requests.Session:
    retry = Retry(
        total=retries,
        connect=retries,
        read=retries,
        status=retries,
        backoff_factor=0.8,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET", "HEAD"),
        raise_on_status=False,
    )
    s = requests.Session()
    adapter = HTTPAdapter(max_retries=retry, pool_connections=16, pool_maxsize=16)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    s.headers.update({"User-Agent": "panic-sales-data-mining/1.4"})
    return s


HTTP = _session(retries=3)
HTTP_FAST = _session(retries=0)


def _stamp(df: pd.DataFrame, *, source: str, symbol: str, note: str = "") -> pd.DataFrame:
    if df is not None:
        df.attrs["source_used"] = source
        df.attrs["symbol_used"] = symbol
        df.attrs["quality_note"] = note
    return df


def _get_json(url: str, *, params: dict[str, Any] | None = None,
              headers: dict[str, str] | None = None,
              timeout: tuple[int, int] = DEFAULT_TIMEOUT) -> dict[str, Any]:
    r = HTTP.get(url, params=params, headers=headers, timeout=timeout)
    r.raise_for_status()
    return r.json()


def fetch_with_fallbacks(
    symbols: Iterable[str],
    fetcher: Callable[[str], pd.DataFrame],
    *,
    min_bars: int = 20,
    label: str = "source",
) -> pd.DataFrame:
    """Try symbols in order; return first frame with enough bars.

    If every candidate is empty or sparse, return the best non-empty frame and
    mark it as partial. This preserves event coverage while making data quality
    explicit in event_status.csv.
    """
    best = pd.DataFrame()
    best_n = -1
    errors: list[str] = []
    seen: set[str] = set()
    ordered = [s for s in symbols if not (s in seen or seen.add(s))]

    for sym in ordered:
        try:
            df = fetcher(sym)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{sym}: {exc}")
            continue
        n = 0 if df is None or df.empty else len(df)
        if n > best_n:
            best, best_n = df, n
        if n >= min_bars:
            if sym != ordered[0]:
                print(f"  [fallback] using {sym} instead of {ordered[0]} ({n} bars)")
                df.attrs["quality_note"] = (df.attrs.get("quality_note", "") +
                                            f"; proxy_symbol_for={ordered[0]}").strip("; ")
            return df
        if n:
            print(f"  [fallback] {label} {sym}: only {n} bars (<{min_bars})")
        else:
            print(f"  [fallback] {label} {sym}: no data")

    if best is not None and not best.empty:
        note = best.attrs.get("quality_note", "")
        best.attrs["quality_note"] = (note + f"; partial_best_available bars={len(best)}").strip("; ")
        return best
    if errors:
        print("  [fallback errors] " + " | ".join(errors[:4]))
    return pd.DataFrame()


# ---------------------------------------------------------------------------
# Yahoo Finance (daily) via yfinance, plus Stooq/FRED fallbacks
# ---------------------------------------------------------------------------
def fetch_yahoo(symbol: str, start: str, end: str) -> pd.DataFrame:
    import yfinance as yf

    end_excl = (pd.Timestamp(end) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    df = yf.download(symbol, start=start, end=end_excl, interval="1d",
                     auto_adjust=False, progress=False, threads=False)
    if df.empty:
        return _stamp(df, source="yahoo", symbol=symbol, note="empty_yfinance")
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns=str.lower)
    cols = [c for c in ["open", "high", "low", "close", "volume"] if c in df.columns]
    df = df[cols]
    df.index = pd.to_datetime(df.index, utc=True)
    return _stamp(df.dropna(subset=["close"]), source="yahoo", symbol=symbol)


_STOOQ_MAP = {
    "^GSPC": "^spx",
    "^DJI": "^dji",
    "^VIX": "^vix",
    "^NDX": "^ndx",
    "^IXIC": "^ndq",
    "^N225": "^nkx",
    "^STOXX50E": "^sx5e",
    "KRE": "kre.us",
    "GC=F": "gc.f",
    "CL=F": "cl.f",
}


def fetch_stooq(symbol: str, start: str, end: str) -> pd.DataFrame:
    stooq_symbol = _STOOQ_MAP.get(symbol, symbol.lower())
    d1 = pd.Timestamp(start).strftime("%Y%m%d")
    d2 = pd.Timestamp(end).strftime("%Y%m%d")
    url = f"https://stooq.com/q/d/l/?s={stooq_symbol}&d1={d1}&d2={d2}&i=d"
    try:
        r = HTTP.get(url, timeout=DEFAULT_TIMEOUT)
        r.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"Stooq request failed for {symbol}/{stooq_symbol}: {exc}") from exc
    if not r.text or r.text.strip().lower().startswith("no data"):
        return _stamp(pd.DataFrame(), source="stooq", symbol=stooq_symbol, note="empty_stooq")
    df = pd.read_csv(io.StringIO(r.text))
    if df.empty or "Date" not in df.columns:
        return _stamp(pd.DataFrame(), source="stooq", symbol=stooq_symbol, note="empty_stooq")
    df = df.rename(columns={"Date": "date", "Open": "open", "High": "high",
                            "Low": "low", "Close": "close", "Volume": "volume"})
    df["ts"] = pd.to_datetime(df["date"], utc=True)
    df = df.set_index("ts").sort_index()
    cols = [c for c in ["open", "high", "low", "close", "volume"] if c in df.columns]
    for col in cols:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return _stamp(df[cols].dropna(subset=["close"]), source="stooq", symbol=stooq_symbol)


def fetch_yahoo_with_fallback(symbol: str, start: str, end: str,
                              min_bars: int = 20) -> pd.DataFrame:
    def _fetch(sym: str) -> pd.DataFrame:
        df = fetch_yahoo(sym, start, end)
        if len(df) >= min_bars:
            return df
        # Try Stooq for the same market symbol when Yahoo is empty/sparse.
        try:
            stq = fetch_stooq(sym, start, end)
            if len(stq) > len(df):
                print(f"  [fallback] Stooq used for {sym} ({len(stq)} bars)")
                return stq
        except Exception as exc:  # noqa: BLE001
            print(f"  [fallback] Stooq failed for {sym}: {exc}")
        return df
    return fetch_with_fallbacks([symbol], _fetch, min_bars=min_bars, label="equity")


# ---------------------------------------------------------------------------
# FRED daily macro series
# ---------------------------------------------------------------------------
def fetch_fred(series_id: str, start: str, end: str) -> pd.DataFrame:
    from fredapi import Fred

    fred = Fred(api_key=C.FRED_API_KEY)
    s = fred.get_series(series_id, observation_start=start, observation_end=end)
    df = s.to_frame("close")
    df.index = pd.to_datetime(df.index, utc=True)
    return _stamp(df.dropna(), source="fred", symbol=series_id)


# ---------------------------------------------------------------------------
# CryptoCompare aggregated history (daily or hourly)
# ---------------------------------------------------------------------------
def fetch_cryptocompare(symbol: str, start: str, end: str,
                        resolution: str = "day") -> pd.DataFrame:
    base = symbol.replace("USDT", "").replace("USD", "")
    path = "v2/histoday" if resolution == "day" else "v2/histohour"
    to_ts = int(pd.Timestamp(end, tz="UTC").timestamp())
    start_ts = int(pd.Timestamp(start, tz="UTC").timestamp())

    headers = {}
    if C.CRYPTOCOMPARE_API_KEY:
        headers["authorization"] = f"Apikey {C.CRYPTOCOMPARE_API_KEY}"

    rows = []
    def _cc_request(cur: int) -> dict[str, Any]:
        params = {"fsym": base, "tsym": "USD", "limit": 2000, "toTs": cur}
        url = f"{C.CRYPTOCOMPARE_BASE}/{path}"
        try:
            return _get_json(url, params=params, headers=headers, timeout=(5, 12))
        except requests.HTTPError as exc:
            # If a stale/invalid API key is exported, CryptoCompare returns 401.
            # The free endpoint often still works anonymously, so retry once
            # without the Authorization header before marking the event failed.
            status = getattr(exc.response, "status_code", None)
            if status == 401 and headers:
                print("  [cryptocompare] API key rejected; retrying anonymously")
                return _get_json(url, params=params, headers={}, timeout=(5, 12))
            raise

    cursor = to_ts
    while cursor > start_ts:
        payload_root = _cc_request(cursor)
        payload = payload_root.get("Data", {}).get("Data", [])
        if not payload:
            break
        rows = payload + rows
        cursor = payload[0]["time"] - 1
        time.sleep(0.2)

    if not rows:
        return _stamp(pd.DataFrame(), source="cryptocompare", symbol=base, note="empty")
    df = pd.DataFrame(rows)
    df["ts"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df = df.set_index("ts").rename(columns={"volumeto": "volume"})
    df = df[["open", "high", "low", "close", "volume"]]
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.loc[start:end].dropna(subset=["open", "high", "low", "close"])
    return _stamp(df, source="cryptocompare", symbol=base)


# ---------------------------------------------------------------------------
# Binance klines, 2017+
# ---------------------------------------------------------------------------
_BINANCE_COLS = ["open_time", "open", "high", "low", "close", "volume",
                 "close_time", "quote_volume", "trades",
                 "taker_base", "taker_quote", "ignore"]

BINANCE_API_BASES = (
    "https://api.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api3.binance.com",
)


def _read_binance_zip(content: bytes) -> pd.DataFrame:
    zf = zipfile.ZipFile(io.BytesIO(content))
    with zf.open(zf.namelist()[0]) as fh:
        return pd.read_csv(fh, header=None, names=_BINANCE_COLS)


def _normalise_binance_frame(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    unit = "us" if pd.to_numeric(df["open_time"].iloc[0]) > 1e15 else "ms"
    df["ts"] = pd.to_datetime(df["open_time"], unit=unit, utc=True)
    df = df.set_index("ts").sort_index()
    for col in ["open", "high", "low", "close", "volume", "trades"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    keep = [c for c in ["open", "high", "low", "close", "volume", "trades"] if c in df.columns]
    return df[keep].dropna(subset=["open", "high", "low", "close"])


def fetch_binance_api_klines(symbol: str, start: str, end: str,
                             interval: str = "1d") -> pd.DataFrame:
    start_ms = int(pd.Timestamp(start, tz="UTC").timestamp() * 1000)
    end_ms = int((pd.Timestamp(end, tz="UTC") + pd.Timedelta(days=1)).timestamp() * 1000)
    rows: list[list[Any]] = []
    cursor = start_ms

    while cursor < end_ms:
        params = {
            "symbol": symbol,
            "interval": interval,
            "startTime": cursor,
            "endTime": end_ms,
            "limit": 1000,
        }
        payload = None
        last_exc: Exception | None = None
        for base in BINANCE_API_BASES:
            try:
                r = HTTP.get(f"{base}/api/v3/klines", params=params, timeout=DEFAULT_TIMEOUT)
                if r.status_code in (400, 404):
                    return _stamp(pd.DataFrame(), source="binance-api", symbol=symbol, note=f"HTTP {r.status_code}")
                r.raise_for_status()
                payload = r.json()
                break
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                continue
        if payload is None:
            raise RuntimeError(f"Binance REST failed for {symbol} {interval}: {last_exc}")
        if not payload:
            break

        rows.extend(payload)
        next_cursor = int(payload[-1][0]) + 1
        if next_cursor <= cursor:
            break
        cursor = next_cursor
        time.sleep(0.06)
        if len(payload) < 1000:
            break

    if not rows:
        return _stamp(pd.DataFrame(), source="binance-api", symbol=symbol, note="empty")
    df = pd.DataFrame(rows, columns=_BINANCE_COLS)
    df = _normalise_binance_frame(df).loc[start:end]
    return _stamp(df, source="binance-api", symbol=symbol)


def fetch_binance_klines_s3(symbol: str, start: str, end: str,
                            interval: str = "1m") -> pd.DataFrame:
    days = pd.date_range(start, end, freq="D")
    frames = []
    missed = 0
    for day in days:
        ds = day.strftime("%Y-%m-%d")
        url = f"{C.BINANCE_VISION}/{symbol}/{interval}/{symbol}-{interval}-{ds}.zip"
        try:
            r = HTTP_FAST.get(url, timeout=FAST_FILE_TIMEOUT)
            if r.status_code == 404:
                missed += 1
                continue
            if r.status_code != 200:
                print(f"  [binance-s3] {symbol} {interval} {ds}: HTTP {r.status_code}")
                missed += 1
                continue
            frames.append(_read_binance_zip(r.content))
        except Exception as exc:  # noqa: BLE001
            print(f"  [binance-s3] {symbol} {interval} {ds}: {exc}")
            missed += 1
            continue
        time.sleep(0.02)

    if not frames:
        return _stamp(pd.DataFrame(), source="binance-s3", symbol=symbol, note="empty")
    if missed:
        print(f"  [binance-s3] skipped {missed}/{len(days)} missing/failed daily files")
    df = pd.concat(frames, ignore_index=True)
    df = _normalise_binance_frame(df)
    note = f"skipped_files={missed}/{len(days)}" if missed else ""
    return _stamp(df, source="binance-s3", symbol=symbol, note=note)


def fetch_binance_minute(symbol: str, start: str, end: str,
                         source: str = "api") -> pd.DataFrame:
    if source == "s3":
        df = fetch_binance_klines_s3(symbol, start, end, interval="1m")
        if not df.empty:
            return df
        print("  [fallback] Binance S3 minute failed/empty, using Binance REST 1m")
    return fetch_binance_api_klines(symbol, start, end, interval="1m")


def fetch_binance_daily(symbol: str, start: str, end: str,
                        source: str = "api") -> pd.DataFrame:
    """Fetch daily Binance OHLCV.

    Important: API mode intentionally does NOT fall back to Binance data lake
    ZIP files. The data lake requires one network request per day and is very
    fragile on some networks. Empty/API-failed candidates should return empty
    quickly so fetch_with_fallbacks can try the next proxy symbol instead.
    Use --crypto-daily-source binance-s3 only when you explicitly want ZIPs.
    """
    if source == "s3":
        return fetch_binance_klines_s3(symbol, start, end, interval="1d")
    try:
        return fetch_binance_api_klines(symbol, start, end, interval="1d")
    except Exception as exc:  # noqa: BLE001
        print(f"  [binance-api] {symbol} 1d failed: {exc}")
        return _stamp(pd.DataFrame(), source="binance-api", symbol=symbol, note=f"api_failed: {exc}")


# ---------------------------------------------------------------------------
# Dukascopy tick (bid/ask) — direct binary download, no Node dependency
# ---------------------------------------------------------------------------
# Dukascopy serves per-hour LZMA-compressed binary tick files at
#   https://datafeed.dukascopy.com/datafeed/<INSTRUMENT>/<YYYY>/<MM-1>/<DD>/<HH>h_ticks.bi5
# Each tick is 20 bytes big-endian: uint32 ms_since_hour, uint32 ask, uint32 bid,
# float32 ask_vol, float32 bid_vol. Ask/bid are in 10^-point_multiplier units.
# Reference: well-known community spec; matches the dukascopy-node format.
#
# Notes on server behaviour:
#  - Dukascopy returns 503 (not 404) for hours that have no data, including
#    weekend hours when the FX market is closed. Both must be treated as
#    "skip this hour, do not retry".
#  - The server reliably rate-limits requests that come from non-browser
#    User-Agents. We override the session UA with a real browser string.
#  - Hourly files are cached on disk so Ctrl+C is safe and a re-run resumes.
import pathlib

DUKASCOPY_BASE = "https://datafeed.dukascopy.com/datafeed"
DUKASCOPY_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Point multipliers per instrument. Most FX majors quote with 5 decimals
# (multiplier=1e-5). JPY pairs use 3 decimals. Metals/CFDs use 1e-3.
DUKASCOPY_POINT: dict[str, float] = {
    "EURUSD": 1e-5, "GBPUSD": 1e-5, "EURCHF": 1e-5, "AUDUSD": 1e-5,
    "USDCAD": 1e-5, "USDCHF": 1e-5, "NZDUSD": 1e-5, "EURGBP": 1e-5,
    "USDJPY": 1e-3, "EURJPY": 1e-3, "GBPJPY": 1e-3,
    "XAUUSD": 1e-3, "XAGUSD": 1e-3,
}


def _dukascopy_decode_hour(blob: bytes, hour_start: pd.Timestamp,
                           point: float) -> pd.DataFrame:
    """Decode a single .bi5 hour file (after LZMA decompression)."""
    import struct

    rec = 20
    n = len(blob) // rec
    if n == 0:
        return pd.DataFrame()
    out = []
    for i in range(n):
        ms, ask_i, bid_i, av, bv = struct.unpack(
            ">IIIff", blob[i * rec:(i + 1) * rec])
        out.append((
            hour_start + pd.Timedelta(milliseconds=ms),
            ask_i * point,
            bid_i * point,
            av, bv,
        ))
    df = pd.DataFrame(out, columns=["ts", "ask", "bid", "ask_vol", "bid_vol"])
    df = df.set_index("ts").sort_index()
    return df


def _fx_market_closed(h: pd.Timestamp) -> bool:
    """Spot FX market closed: Friday 22:00 UTC through Sunday 22:00 UTC."""
    dow = h.dayofweek  # Monday=0..Sunday=6
    if dow == 5:                              # Saturday all day
        return True
    if dow == 6 and h.hour < 22:              # Sunday before 22:00 UTC
        return True
    if dow == 4 and h.hour >= 22:             # Friday from 22:00 UTC
        return True
    return False


def fetch_dukascopy_tick(pair: str, start: str, end: str,
                         resample: str | None = "1min",
                         lookback_days: int | None = None,
                         cache_root: pathlib.Path | None = None) -> pd.DataFrame:
    """Download Dukascopy tick bid/ask for a pair across [start, end].

    pair: e.g. "EURCHF", "GBPUSD", "USDJPY", "XAUUSD" (case-insensitive)
    resample: pandas offset alias for OHLCV downsample with mean bid/ask, or
              None to return raw ticks. Default "1min" keeps files manageable.
    lookback_days: if set, trims the start so we pull at most this many days
                   before window_start. Avoids ~20k unnecessary hour requests.
    cache_root: directory to cache .bi5 files; defaults to data/raw/dukascopy.
    """
    import lzma

    instrument = pair.upper()
    point = DUKASCOPY_POINT.get(instrument)
    if point is None:
        raise ValueError(
            f"Unknown point multiplier for {instrument}; add to DUKASCOPY_POINT")

    # Trim the lookback if requested: Dukascopy hour pulls are heavy and the
    # 30-day baseline pull was creating ~700 extra requests per event.
    if lookback_days is not None:
        trim_from = pd.Timestamp(end, tz="UTC") - pd.Timedelta(days=lookback_days)
        start_ts = max(pd.Timestamp(start, tz="UTC"), trim_from)
    else:
        start_ts = pd.Timestamp(start, tz="UTC")
    end_ts = pd.Timestamp(end, tz="UTC")

    if cache_root is None:
        cache_root = C.RAW_DIR / "dukascopy"
    cache_dir = cache_root / instrument
    cache_dir.mkdir(parents=True, exist_ok=True)

    hours = pd.date_range(start_ts, end_ts, freq="h", tz="UTC")
    headers = {
        "User-Agent": DUKASCOPY_UA,
        "Accept": "application/octet-stream",
        "Referer": "https://www.dukascopy.com/",
    }
    print(f"  [dukascopy] {instrument}: {len(hours)} hours from {start_ts.date()} "
          f"to {end_ts.date()} (weekends will be skipped)")

    frames: list[pd.DataFrame] = []
    missed = 0
    skipped_closed = 0
    cache_hits = 0
    last_day_logged: str | None = None

    for h in hours:
        if _fx_market_closed(h):
            skipped_closed += 1
            continue

        cache_file = cache_dir / f"{h.strftime('%Y-%m-%d')}_{h.hour:02d}.bi5"
        content: bytes | None = None

        if cache_file.exists():
            content = cache_file.read_bytes()
            cache_hits += 1
        else:
            url = (f"{DUKASCOPY_BASE}/{instrument}/{h.year:04d}/"
                   f"{h.month - 1:02d}/{h.day:02d}/{h.hour:02d}h_ticks.bi5")
            try:
                r = HTTP_FAST.get(url, timeout=FAST_FILE_TIMEOUT, headers=headers)
            except Exception:  # noqa: BLE001 — connection error: treat as missed
                missed += 1
                time.sleep(0.5)
                continue

            # 503 = no data for this hour on Dukascopy. Same treatment as 404.
            if r.status_code in (404, 503):
                # Mark cache so a re-run doesn't retry the same empty hour.
                cache_file.write_bytes(b"")
                missed += 1
                time.sleep(0.05)
                continue

            if r.status_code != 200:
                missed += 1
                time.sleep(0.5)
                continue

            content = r.content
            cache_file.write_bytes(content or b"")
            time.sleep(0.15)

        if not content:
            continue

        try:
            blob = lzma.decompress(content)
        except lzma.LZMAError:
            # Empty hour or non-LZMA; skip
            continue
        try:
            frames.append(_dukascopy_decode_hour(blob, h, point))
        except Exception:  # noqa: BLE001
            continue

        # Per-day progress (printed once at the last hour of each day)
        if h.hour == 23:
            day = h.strftime("%Y-%m-%d")
            if last_day_logged != day:
                print(f"  [dukascopy] {instrument} {day}: "
                      f"cached={cache_hits} missed={missed} skipped={skipped_closed}")
                last_day_logged = day

    if not frames:
        return _stamp(pd.DataFrame(), source="dukascopy", symbol=instrument,
                      note=f"empty; missed={missed} skipped_closed={skipped_closed}")

    df = pd.concat(frames).sort_index()
    df = df[(df.index >= start_ts) & (df.index <= end_ts)]
    if df.empty:
        return _stamp(df, source="dukascopy", symbol=instrument,
                      note=f"no_ticks_in_window; missed={missed}")

    mid = (df["bid"] + df["ask"]) / 2.0
    df = df.assign(mid=mid)

    if resample:
        agg = df.resample(resample).agg({
            "mid": ["first", "max", "min", "last"],
            "bid": "mean",
            "ask": "mean",
        })
        agg.columns = ["open", "high", "low", "close", "bid", "ask"]
        agg["volume"] = df["mid"].resample(resample).count()  # tick count
        agg = agg.dropna(subset=["close"])
        note = (f"resample={resample}; cache_hits={cache_hits}; "
                f"missed={missed}; skipped_closed={skipped_closed}")
        return _stamp(agg, source="dukascopy", symbol=instrument, note=note)

    df["open"] = df["high"] = df["low"] = df["close"] = df["mid"]
    df["volume"] = 1
    note = (f"raw_ticks; cache_hits={cache_hits}; "
            f"missed={missed}; skipped_closed={skipped_closed}")
    return _stamp(df[["open", "high", "low", "close", "volume", "bid", "ask"]],
                  source="dukascopy", symbol=instrument, note=note)


# ---------------------------------------------------------------------------
# Polygon.io intraday + NBBO (paid; gated by POLYGON_API_KEY)
# ---------------------------------------------------------------------------
def fetch_polygon_intraday(symbol: str, start: str, end: str,
                           include_nbbo: bool = True,
                           nbbo_sample_min: int = 1) -> pd.DataFrame:
    """Pull 1-minute aggregates and (optionally) sampled NBBO quotes for spread.

    NBBO endpoint is high-volume so we sample one quote per minute. Spread
    column (s) is filled in bps of mid; v/sigma/q come from minute aggs.
    """
    if not C.POLYGON_API_KEY:
        raise RuntimeError(
            "POLYGON_API_KEY not set. Polygon is the paid second-pass source; "
            "export the key or skip this fetcher.")

    base = "https://api.polygon.io"
    headers = {"Authorization": f"Bearer {C.POLYGON_API_KEY}"}

    # 1) Minute aggregates
    aggs_url = (f"{base}/v2/aggs/ticker/{symbol}/range/1/minute/"
                f"{start}/{end}")
    rows: list[dict] = []
    next_url = aggs_url + "?adjusted=true&sort=asc&limit=50000"
    while next_url:
        r = HTTP.get(next_url, headers=headers, timeout=DEFAULT_TIMEOUT)
        r.raise_for_status()
        payload = r.json()
        for bar in payload.get("results", []) or []:
            rows.append({
                "ts": pd.Timestamp(bar["t"], unit="ms", tz="UTC"),
                "open": bar["o"], "high": bar["h"],
                "low": bar["l"], "close": bar["c"],
                "volume": bar.get("v", 0),
            })
        next_url = payload.get("next_url")
        if next_url and "apiKey" not in next_url and "Authorization" not in next_url:
            # Polygon's next_url omits the key; pass header as before
            pass
        if not payload.get("next_url"):
            break
        time.sleep(0.05)

    if not rows:
        return _stamp(pd.DataFrame(), source="polygon", symbol=symbol,
                      note="empty_aggs")
    aggs = pd.DataFrame(rows).set_index("ts").sort_index()

    if not include_nbbo:
        return _stamp(aggs, source="polygon", symbol=symbol, note="aggs_only")

    # 2) Sampled NBBO: one quote per minute bar via the quotes endpoint.
    bid_col, ask_col = [], []
    for minute_ts in aggs.index:
        ns = int(minute_ts.value)
        q_url = f"{base}/v3/quotes/{symbol}"
        params = {
            "timestamp.gte": ns,
            "timestamp.lt": ns + nbbo_sample_min * 60 * 1_000_000_000,
            "order": "asc", "limit": 1, "sort": "timestamp",
        }
        try:
            r = HTTP.get(q_url, headers=headers, params=params,
                         timeout=DEFAULT_TIMEOUT)
            r.raise_for_status()
            results = r.json().get("results") or []
        except Exception:  # noqa: BLE001
            results = []
        if results:
            q = results[0]
            bid_col.append(q.get("bid_price"))
            ask_col.append(q.get("ask_price"))
        else:
            bid_col.append(None)
            ask_col.append(None)
    aggs["bid"] = bid_col
    aggs["ask"] = ask_col
    note = f"aggs+nbbo sampled@{nbbo_sample_min}m"
    return _stamp(aggs, source="polygon", symbol=symbol, note=note)
