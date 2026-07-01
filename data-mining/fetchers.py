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
# Binance USDS-futures bookTicker (best bid/ask + sizes) — streaming aggregation
# ---------------------------------------------------------------------------
# Source: data.binance.vision daily ZIPs at
#   /data/futures/um/daily/bookTicker/<SYMBOL>/<SYMBOL>-bookTicker-<YYYY-MM-DD>.zip
# Each row: update_id, best_bid_price, best_bid_qty, best_ask_price,
#           best_ask_qty, transaction_time (ms), event_time (ms).
#
# Why futures and not spot: Binance Vision publishes bookTicker only for
# futures (UM perpetuals), not for spot. For most BTC/ETH/altcoin USDT pairs
# during 2020-2024 the perpetual is the venue of primary price discovery,
# its spread is typically tighter than spot, and stress dynamics lead spot
# by milliseconds-to-seconds. This makes futures bookTicker a strictly
# stronger source for our stress-signal calibration than spot would be.
#
# Files can be 50-300 MB compressed for a major symbol on a stressed day.
# We stream the CSV in chunks, aggregate to 1-minute means (bid, ask,
# bid_qty, ask_qty) plus update count, and cache only the per-day parquet
# so re-runs are instant. The raw ZIP is discarded after parsing.

_BOOKTICKER_COLS = ["update_id", "best_bid_price", "best_bid_qty",
                    "best_ask_price", "best_ask_qty",
                    "transaction_time", "event_time"]


def _aggregate_bookticker_day(symbol: str, day: pd.Timestamp,
                              cache_dir: pathlib.Path,
                              chunksize: int = 200_000) -> pd.DataFrame:
    """Download one day's bookTicker ZIP and aggregate to 1-min means.

    Returns DataFrame indexed by UTC minute with columns: bid, ask,
    bid_qty, ask_qty, n_updates. Empty DataFrame if file is missing.
    """
    day_str = day.strftime("%Y-%m-%d")
    cache_path = cache_dir / f"agg-{day_str}.parquet"
    marker_path = cache_dir / f"missing-{day_str}.flag"

    if cache_path.exists():
        try:
            cached = pd.read_parquet(cache_path)
            # Backfill UTC on caches written before the tz fix.
            if cached.index.tz is None:
                cached.index = cached.index.tz_localize("UTC")
            return cached
        except Exception:  # noqa: BLE001
            cache_path.unlink(missing_ok=True)

    if marker_path.exists():
        return pd.DataFrame()

    # USDS-futures (UM) bookTicker. Spot is not published on the public lake.
    url = (f"https://data.binance.vision/data/futures/um/daily/bookTicker/"
           f"{symbol}/{symbol}-bookTicker-{day_str}.zip")

    # Streaming download with progress + hard time budget per file.
    # The per-read timeout in requests resets on every byte; we add an
    # absolute deadline so a trickling connection cannot hang us.
    MAX_SECONDS_PER_FILE = 300  # 5 minutes hard cap
    print(f"  [bookticker] {symbol} {day_str}: downloading...", flush=True)
    try:
        r = HTTP_FAST.get(url, timeout=(5, 60), stream=True)
    except Exception as exc:  # noqa: BLE001
        print(f"  [bookticker] {symbol} {day_str}: connect failed: {exc}")
        return pd.DataFrame()

    if r.status_code == 404:
        marker_path.write_text("")
        r.close()
        return pd.DataFrame()
    if r.status_code != 200:
        print(f"  [bookticker] {symbol} {day_str}: HTTP {r.status_code}")
        r.close()
        return pd.DataFrame()

    total = int(r.headers.get("Content-Length", 0) or 0)
    chunks: list[bytes] = []
    downloaded = 0
    started = time.time()
    last_print = started
    try:
        for chunk in r.iter_content(chunk_size=4 * 1024 * 1024):
            if not chunk:
                continue
            chunks.append(chunk)
            downloaded += len(chunk)
            now = time.time()
            if now - started > MAX_SECONDS_PER_FILE:
                print(f"  [bookticker] {symbol} {day_str}: aborted after "
                      f"{int(now - started)}s ({downloaded / 1e6:.0f}/"
                      f"{total / 1e6:.0f} MB); skipping")
                r.close()
                return pd.DataFrame()
            if now - last_print >= 5.0:
                if total:
                    pct = downloaded / total * 100
                    print(f"    {symbol} {day_str}: {pct:5.1f}% "
                          f"({downloaded / 1e6:.0f}/{total / 1e6:.0f} MB)",
                          flush=True)
                else:
                    print(f"    {symbol} {day_str}: {downloaded / 1e6:.0f} MB"
                          f" so far", flush=True)
                last_print = now
    finally:
        r.close()

    content = b"".join(chunks)

    try:
        zf = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile:
        print(f"  [bookticker] {symbol} {day_str}: bad zip")
        return pd.DataFrame()

    inner = zf.namelist()[0]

    # Detect header: peek first bytes of inner file
    with zf.open(inner) as peek:
        head = peek.read(200)
    has_header = b"update_id" in head or b"best_bid_price" in head

    # Streaming aggregate across chunks
    sums: dict[pd.Timestamp, list[float]] = {}
    with zf.open(inner) as fh:
        reader_kwargs = {
            "chunksize": chunksize,
            "header": 0 if has_header else None,
            "names": None if has_header else _BOOKTICKER_COLS,
            "engine": "c",
        }
        for chunk in pd.read_csv(fh, **reader_kwargs):
            # Normalise column names
            chunk.columns = [c.strip() for c in chunk.columns]
            need = ["best_bid_price", "best_bid_qty", "best_ask_price",
                    "best_ask_qty", "transaction_time"]
            if not set(need).issubset(chunk.columns):
                continue
            ts = pd.to_datetime(chunk["transaction_time"], unit="ms",
                                utc=True, errors="coerce")
            mask = ts.notna()
            if not mask.any():
                continue
            minute = ts[mask].dt.floor("1min")
            grouped = (chunk.loc[mask].assign(_m=minute.values)
                       .groupby("_m", sort=False)
                       .agg(bid_sum=("best_bid_price", "sum"),
                            ask_sum=("best_ask_price", "sum"),
                            bidq_sum=("best_bid_qty", "sum"),
                            askq_sum=("best_ask_qty", "sum"),
                            cnt=("best_bid_price", "size")))
            for m, row in grouped.iterrows():
                if m not in sums:
                    sums[m] = [0.0, 0.0, 0.0, 0.0, 0]
                sums[m][0] += float(row["bid_sum"])
                sums[m][1] += float(row["ask_sum"])
                sums[m][2] += float(row["bidq_sum"])
                sums[m][3] += float(row["askq_sum"])
                sums[m][4] += int(row["cnt"])

    if not sums:
        marker_path.write_text("")
        return pd.DataFrame()

    rows = []
    for m in sorted(sums):
        bsum, asum, bqsum, aqsum, cnt = sums[m]
        if cnt == 0:
            continue
        rows.append({"ts": m, "bid": bsum / cnt, "ask": asum / cnt,
                     "bid_qty": bqsum / cnt, "ask_qty": aqsum / cnt,
                     "n_updates": cnt})
    if not rows:
        marker_path.write_text("")
        return pd.DataFrame()
    df = pd.DataFrame(rows).set_index("ts")
    # Groupby through .values had stripped tz; re-attach UTC.
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    df.to_parquet(cache_path)
    return df


def fetch_binance_bookticker(symbol: str, start: str, end: str,
                             cache_root: pathlib.Path | None = None
                             ) -> pd.DataFrame:
    """Pull and aggregate Binance spot bookTicker into 1-minute frame.

    Returns DataFrame with UTC minute index and columns:
      bid, ask, bid_qty, ask_qty, n_updates
    Spread (s) and depth (d) signals are derived from these in signals.py.
    """
    if cache_root is None:
        cache_root = C.RAW_DIR / "bookticker_futures"
    cache_dir = cache_root / symbol
    cache_dir.mkdir(parents=True, exist_ok=True)

    days = pd.date_range(start, end, freq="D", tz="UTC")
    print(f"  [bookticker] {symbol}: {len(days)} days "
          f"from {pd.Timestamp(start).date()} to {pd.Timestamp(end).date()}")

    frames: list[pd.DataFrame] = []
    missing = 0
    start_t = time.time()
    for i, day in enumerate(days, 1):
        df = _aggregate_bookticker_day(symbol, day, cache_dir)
        if df.empty:
            missing += 1
        else:
            frames.append(df)
        # Per-day progress with rolling ETA: every 5 days or on the last day
        if i % 5 == 0 or i == len(days):
            elapsed = time.time() - start_t
            per_day = elapsed / i if i else 0
            eta_min = (len(days) - i) * per_day / 60
            print(f"  [bookticker] {symbol}: {i}/{len(days)} done "
                  f"(with_data={len(frames)}, missing={missing}, "
                  f"ETA ~{eta_min:.0f}m)")

    if not frames:
        return _stamp(pd.DataFrame(), source="binance-bookticker",
                      symbol=symbol, note=f"empty; missing={missing}")
    df = pd.concat(frames).sort_index()
    note = f"days_with_data={len(frames)}/{len(days)}; missing={missing}"
    print(f"  [bookticker] {symbol}: {note}")
    return _stamp(df, source="binance-bookticker", symbol=symbol, note=note)


# ---------------------------------------------------------------------------
# Binance spot aggTrades — derived effective spread proxy
# ---------------------------------------------------------------------------
# Path: /data/spot/daily/aggTrades/<SYMBOL>/<SYMBOL>-aggTrades-<YYYY-MM-DD>.zip
# CSV (with or without header):
#   agg_trade_id, price, quantity, first_trade_id, last_trade_id,
#   transact_time (ms), is_buyer_maker, is_best_match
#
# aggTrades is the broadest historical source on Binance Vision for crypto:
# full coverage back to 2017 for major USDT pairs. We use it ONLY when bid/ask
# is not available (i.e. for events before mid-2023 when bookTicker archive
# starts). The proxy we compute is the per-minute price RANGE divided by mid,
# in bps:
#   s_eff = (max_price - min_price) / mid * 1e4
# This is an upward-biased estimator of true bid-ask spread because it also
# captures intra-minute price drift. For our calibration purpose what matters
# is the peak/baseline ratio, which is preserved as long as the bias is
# stable across calm and stress regimes — and it is, because both regimes
# include drift; stress just amplifies both spread and drift simultaneously.

_AGGTRADES_COLS = ["agg_id", "price", "qty", "first_id", "last_id",
                   "ts", "is_buyer_maker", "is_best_match"]


def _aggregate_aggtrades_day(symbol: str, day: pd.Timestamp,
                             cache_dir: pathlib.Path,
                             chunksize: int = 200_000) -> pd.DataFrame:
    day_str = day.strftime("%Y-%m-%d")
    cache_path = cache_dir / f"agg-{day_str}.parquet"
    marker_path = cache_dir / f"missing-{day_str}.flag"

    if cache_path.exists():
        try:
            cached = pd.read_parquet(cache_path)
            if cached.index.tz is None:
                cached.index = cached.index.tz_localize("UTC")
            return cached
        except Exception:  # noqa: BLE001
            cache_path.unlink(missing_ok=True)

    if marker_path.exists():
        return pd.DataFrame()

    url = (f"https://data.binance.vision/data/spot/daily/aggTrades/"
           f"{symbol}/{symbol}-aggTrades-{day_str}.zip")

    MAX_SECONDS_PER_FILE = 180  # aggTrades zips are smaller than bookTicker
    print(f"  [aggtrades] {symbol} {day_str}: downloading...", flush=True)
    try:
        r = HTTP_FAST.get(url, timeout=(5, 60), stream=True)
    except Exception as exc:  # noqa: BLE001
        print(f"  [aggtrades] {symbol} {day_str}: connect failed: {exc}")
        return pd.DataFrame()

    if r.status_code == 404:
        marker_path.write_text("")
        r.close()
        return pd.DataFrame()
    if r.status_code != 200:
        print(f"  [aggtrades] {symbol} {day_str}: HTTP {r.status_code}")
        r.close()
        return pd.DataFrame()

    total = int(r.headers.get("Content-Length", 0) or 0)
    chunks: list[bytes] = []
    downloaded = 0
    started = time.time()
    last_print = started
    try:
        for chunk in r.iter_content(chunk_size=4 * 1024 * 1024):
            if not chunk:
                continue
            chunks.append(chunk)
            downloaded += len(chunk)
            now = time.time()
            if now - started > MAX_SECONDS_PER_FILE:
                print(f"  [aggtrades] {symbol} {day_str}: aborted after "
                      f"{int(now - started)}s; skipping")
                r.close()
                return pd.DataFrame()
            if now - last_print >= 10.0:
                if total:
                    pct = downloaded / total * 100
                    print(f"    {symbol} {day_str}: {pct:5.1f}% "
                          f"({downloaded / 1e6:.0f}/{total / 1e6:.0f} MB)",
                          flush=True)
                last_print = now
    finally:
        r.close()
    content = b"".join(chunks)

    try:
        zf = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile:
        print(f"  [aggtrades] {symbol} {day_str}: bad zip")
        return pd.DataFrame()

    inner = zf.namelist()[0]
    with zf.open(inner) as peek:
        head = peek.read(200)
    has_header = b"agg_trade_id" in head or b"price" in head[:50].lower()

    # Per-minute accumulators: minute_ts -> [pmax, pmin, qsum, count]
    sums: dict[pd.Timestamp, list[float]] = {}
    with zf.open(inner) as fh:
        kwargs = {
            "chunksize": chunksize,
            "header": 0 if has_header else None,
            "names": None if has_header else _AGGTRADES_COLS,
            "engine": "c",
        }
        for chunk in pd.read_csv(fh, **kwargs):
            chunk.columns = [c.strip() for c in chunk.columns]
            # Normalise timestamp column name
            if "transact_time" in chunk.columns:
                chunk = chunk.rename(columns={"transact_time": "ts"})
            elif "timestamp" in chunk.columns:
                chunk = chunk.rename(columns={"timestamp": "ts"})
            if not {"price", "qty", "ts"}.issubset(chunk.columns):
                # Try renaming common alternates
                if "quantity" in chunk.columns:
                    chunk = chunk.rename(columns={"quantity": "qty"})
                if not {"price", "qty", "ts"}.issubset(chunk.columns):
                    continue
            ts = pd.to_datetime(chunk["ts"], unit="ms", utc=True,
                                errors="coerce")
            mask = ts.notna()
            if not mask.any():
                continue
            minute = ts[mask].dt.floor("1min")
            sub = chunk.loc[mask].assign(_m=minute.values)
            grouped = sub.groupby("_m", sort=False).agg(
                pmax=("price", "max"),
                pmin=("price", "min"),
                qsum=("qty", "sum"),
                cnt=("price", "size"),
            )
            for m, row in grouped.iterrows():
                if m not in sums:
                    sums[m] = [float(row["pmax"]), float(row["pmin"]),
                               float(row["qsum"]), int(row["cnt"])]
                else:
                    if row["pmax"] > sums[m][0]:
                        sums[m][0] = float(row["pmax"])
                    if row["pmin"] < sums[m][1]:
                        sums[m][1] = float(row["pmin"])
                    sums[m][2] += float(row["qsum"])
                    sums[m][3] += int(row["cnt"])

    if not sums:
        marker_path.write_text("")
        return pd.DataFrame()

    rows = []
    for m in sorted(sums):
        pmax, pmin, qsum, cnt = sums[m]
        mid = (pmax + pmin) / 2.0
        s_eff = ((pmax - pmin) / mid * 1e4) if mid > 0 else None
        rows.append({"ts": m, "s_eff": s_eff,
                     "agg_volume": qsum, "n_trades": cnt})
    df = pd.DataFrame(rows).set_index("ts")
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    df.to_parquet(cache_path)
    return df


def fetch_binance_aggtrades(symbol: str, start: str, end: str,
                            cache_root: pathlib.Path | None = None
                            ) -> pd.DataFrame:
    """Pull and aggregate Binance spot aggTrades into 1-minute proxy frame.

    Columns: s_eff (effective spread in bps from price range), agg_volume,
    n_trades. Built to be merged into the existing minute klines and read
    by signals.py as a fallback for the s signal when bid/ask is missing.
    """
    if cache_root is None:
        cache_root = C.RAW_DIR / "aggtrades"
    cache_dir = cache_root / symbol
    cache_dir.mkdir(parents=True, exist_ok=True)

    days = pd.date_range(start, end, freq="D", tz="UTC")
    print(f"  [aggtrades] {symbol}: {len(days)} days "
          f"from {pd.Timestamp(start).date()} to {pd.Timestamp(end).date()}")

    frames: list[pd.DataFrame] = []
    missing = 0
    start_t = time.time()
    for i, day in enumerate(days, 1):
        df = _aggregate_aggtrades_day(symbol, day, cache_dir)
        if df.empty:
            missing += 1
        else:
            frames.append(df)
        if i % 10 == 0 or i == len(days):
            elapsed = time.time() - start_t
            per_day = elapsed / i if i else 0
            eta_min = (len(days) - i) * per_day / 60
            print(f"  [aggtrades] {symbol}: {i}/{len(days)} done "
                  f"(with_data={len(frames)}, missing={missing}, "
                  f"ETA ~{eta_min:.0f}m)")

    if not frames:
        return _stamp(pd.DataFrame(), source="binance-aggtrades",
                      symbol=symbol, note=f"empty; missing={missing}")
    df = pd.concat(frames).sort_index()
    note = f"days_with_data={len(frames)}/{len(days)}; missing={missing}"
    print(f"  [aggtrades] {symbol}: {note}")
    return _stamp(df, source="binance-aggtrades", symbol=symbol, note=note)


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


# ---------------------------------------------------------------------------
# Massive.com REST API (Stocks Starter tier — aggregates only, no NBBO)
# ---------------------------------------------------------------------------
def fetch_massive_intraday(symbol: str, start: str, end: str) -> pd.DataFrame:
    """Wrapper over fetch_massive.fetch_minute_aggs conforming to our schema.

    Returns DataFrame indexed by UTC timestamp with columns:
        open, high, low, close, volume, trades, vwap
    Stocks Starter tier does NOT include NBBO quotes, so bid/ask columns are
    absent. Downstream signals.py will fall back to Roll/Corwin-Schultz
    spread estimators + Amihud illiquidity for the s and s_eff proxies.
    """
    import fetch_massive as FM

    df = FM.fetch_minute_aggs(symbol, start, end)
    if df.empty:
        return _stamp(df, source="massive-api", symbol=symbol, note="empty")
    note = (f"aggs_only n_requests={df.attrs.get('n_requests', 1)} "
            f"resolved_symbol={df.attrs.get('symbol_used', symbol)}")
    return _stamp(df, source="massive-api", symbol=symbol, note=note)
