"""Massive.com REST API fetcher for equity minute aggregates.

Supports the aggregates endpoint which is available on Stocks Starter tier
and above. Does NOT support NBBO quotes (requires Stocks Developer + Quotes
add-on, ~$79 + $199).

For each equity event we pull 1-minute OHLCV for the event window from
Massive's /v2/aggs/ticker/{ticker}/range/1/minute/{from}/{to} endpoint,
handling pagination via `next_url`.

Special handling:
- Regular tickers (SPY, KRE, SIVB, etc.): plain ticker in the URL
- Indices (VIX, SPX, DJI, NDX): prefixed with `I:` per Massive convention
- Delisted tickers (SIVB, XIV): work at Massive since they retain history

Env vars:
- MASSIVE_API_KEY: required
- MASSIVE_API_BASE: optional, defaults to https://api.massive.com

Rate limiting: Starter tier is 5 requests/second. We sleep 250ms between
requests to stay safe.
"""

from __future__ import annotations

import os
import time
from typing import Iterable

import pandas as pd
import requests


API_BASE = os.environ.get("MASSIVE_API_BASE", "https://api.massive.com")
API_KEY = os.environ.get("MASSIVE_API_KEY", "")
SLEEP_BETWEEN_REQUESTS = 0.25
DEFAULT_TIMEOUT = 30


# Tickers that need the I: (index) prefix in Massive/Polygon conventions
INDEX_TICKERS = {"VIX", "SPX", "DJI", "NDX", "N225", "STOXX50E", "GSPC"}


def _normalise_ticker(symbol: str) -> str:
    """Translate common ^SYMBOL / SYMBOL forms into Massive's convention.

    Yahoo uses ^VIX, ^GSPC etc.; Massive uses I:VIX, I:SPX etc.
    """
    stripped = symbol.lstrip("^")
    if stripped in INDEX_TICKERS or symbol.startswith("^"):
        # For Yahoo indices we translate to Massive index format
        mapping = {"GSPC": "SPX", "DJI": "DJI", "IXIC": "NDX",
                   "N225": "N225", "STOXX50E": "STOXX50E", "VIX": "VIX"}
        return f"I:{mapping.get(stripped, stripped)}"
    return symbol


def fetch_minute_aggs(ticker: str, start_date: str, end_date: str,
                      api_key: str | None = None) -> pd.DataFrame:
    """Pull 1-minute OHLCV for a ticker over [start_date, end_date].

    Handles Massive's pagination via `next_url`. Returns DataFrame indexed
    by UTC timestamp with columns: open, high, low, close, volume, trades.
    """
    key = api_key or API_KEY
    if not key:
        raise RuntimeError("MASSIVE_API_KEY not set")

    massive_ticker = _normalise_ticker(ticker)
    url = (f"{API_BASE}/v2/aggs/ticker/{massive_ticker}/range/1/minute/"
           f"{start_date}/{end_date}")
    params = {"adjusted": "true", "sort": "asc", "limit": 50000,
              "apiKey": key}

    rows: list[dict] = []
    calls = 0
    while url:
        r = requests.get(url, params=params if calls == 0 else {"apiKey": key},
                         timeout=DEFAULT_TIMEOUT)
        calls += 1
        if r.status_code == 401 or r.status_code == 403:
            raise RuntimeError(
                f"Massive rejected request for {massive_ticker}: "
                f"HTTP {r.status_code} {r.text[:200]}")
        r.raise_for_status()
        payload = r.json()
        if payload.get("status") == "NOT_AUTHORIZED":
            raise RuntimeError(
                f"Massive tier does not include this data for {massive_ticker}: "
                f"{payload.get('message', 'no message')}")
        results = payload.get("results") or []
        for bar in results:
            rows.append({
                "ts": pd.Timestamp(bar["t"], unit="ms", tz="UTC"),
                "open": bar["o"], "high": bar["h"],
                "low": bar["l"], "close": bar["c"],
                "volume": bar.get("v", 0),
                "trades": bar.get("n", 0),
                "vwap": bar.get("vw"),
            })
        # Follow next_url for pagination if present
        url = payload.get("next_url")
        # next_url embeds cursor; drop our own params and reuse only apiKey
        params = None
        time.sleep(SLEEP_BETWEEN_REQUESTS)

    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows).set_index("ts").sort_index()
    df.attrs["source_used"] = "massive-api"
    df.attrs["symbol_used"] = massive_ticker
    df.attrs["n_requests"] = calls
    return df


def fetch_daily_aggs(ticker: str, start_date: str, end_date: str,
                     api_key: str | None = None) -> pd.DataFrame:
    """Same as fetch_minute_aggs but daily bars. Useful for 30-day baseline
    padding when we only need coarse pre-event history."""
    key = api_key or API_KEY
    if not key:
        raise RuntimeError("MASSIVE_API_KEY not set")

    massive_ticker = _normalise_ticker(ticker)
    url = (f"{API_BASE}/v2/aggs/ticker/{massive_ticker}/range/1/day/"
           f"{start_date}/{end_date}")
    params = {"adjusted": "true", "sort": "asc", "limit": 50000,
              "apiKey": key}
    r = requests.get(url, params=params, timeout=DEFAULT_TIMEOUT)
    if r.status_code in (401, 403):
        raise RuntimeError(
            f"Massive rejected daily request for {massive_ticker}: "
            f"HTTP {r.status_code} {r.text[:200]}")
    r.raise_for_status()
    payload = r.json()
    if payload.get("status") == "NOT_AUTHORIZED":
        raise RuntimeError(
            f"Massive tier does not include daily data for {massive_ticker}: "
            f"{payload.get('message', 'no message')}")
    results = payload.get("results") or []
    if not results:
        return pd.DataFrame()
    rows = [{
        "ts": pd.Timestamp(bar["t"], unit="ms", tz="UTC"),
        "open": bar["o"], "high": bar["h"],
        "low": bar["l"], "close": bar["c"],
        "volume": bar.get("v", 0),
        "trades": bar.get("n", 0),
    } for bar in results]
    df = pd.DataFrame(rows).set_index("ts").sort_index()
    df.attrs["source_used"] = "massive-api"
    df.attrs["symbol_used"] = massive_ticker
    return df


def fetch_event_window(ticker: str, window_start: str, window_end: str,
                       baseline_days: int = 30) -> pd.DataFrame:
    """Fetch minute-resolution data for the event window plus baseline pad.

    Returns a DataFrame with (baseline_days + window_length) days of minute
    OHLCV. Uses minute aggs throughout; the 30-day pad is heavy on requests
    but gives us a proper baseline sample for peak_ratio calculation.
    """
    ws = pd.Timestamp(window_start) - pd.Timedelta(days=baseline_days)
    we = pd.Timestamp(window_end)
    return fetch_minute_aggs(ticker,
                             ws.strftime("%Y-%m-%d"),
                             we.strftime("%Y-%m-%d"))


if __name__ == "__main__":
    # Smoke test: pull 2 days of SPY around the COVID crash trigger
    import sys
    key = os.environ.get("MASSIVE_API_KEY")
    if not key:
        print("MASSIVE_API_KEY not set")
        sys.exit(1)

    print("=== SPY minute aggs, 2020-03-12 to 2020-03-13 ===")
    df = fetch_minute_aggs("SPY", "2020-03-12", "2020-03-13")
    print(f"rows: {len(df)}")
    print(df.head(3))
    print(df.tail(3))

    print("\n=== VIX daily aggs (translated to I:VIX), same window ===")
    vdf = fetch_daily_aggs("^VIX", "2020-03-01", "2020-03-31")
    print(f"rows: {len(vdf)}")
    print(vdf.head(3))
