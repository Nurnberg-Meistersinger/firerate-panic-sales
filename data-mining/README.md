# Panic Sales: data-mining pipeline (first pass)

This folder is a ready-to-run extractor for the historical stress events in
section III of `[Research] Panic Sales.md`. It pulls free daily and minute
data, builds the four FireRate signals (s, v, sigma, q) and computes the
normalised stress metrics from section II.

It is designed to run **on your own machine**, where outbound network access
to the data sources is open. The Cowork sandbox blocks the exchange and data
APIs (every host returns a 403 tunnel error), so the pull cannot run there.
The compute core was verified on synthetic data inside the sandbox; only the
network-bound fetch must run locally.

## What runs without paying anything

| Source | Covers | Signals you get |
|---|---|---|
| Yahoo (`yfinance`) | All equities, commodities, some FX, daily | v, sigma, q |
| FRED | VIX, gold fix, FX rates, gilt yields, daily | v, sigma (q where present) |
| CryptoCompare | All crypto, daily back to 2010 | v, sigma, q |
| Binance data lake | Crypto 1-minute, 2017+ | v, sigma, q (high resolution) |

Spread (s) is **not** in the free first pass for equities and pre-2020
crypto. That is the known bottleneck and stays a second-pass item.

## Setup

```bash
cd "artifacts/data-mining"
python -m venv .venv && source .venv/bin/activate   # optional
pip install -r requirements.txt
```

Two free keys help but are optional:

```bash
export FRED_API_KEY=...            # https://fred.stlouisfed.org/docs/api/api_key.html
export CRYPTOCOMPARE_API_KEY=...   # https://min-api.cryptocompare.com (free tier)
```

## Run

```bash
python run.py --list                         # show the 29-row event catalogue
python run.py                                # all events, daily resolution
python run.py --resolution minute            # crypto pulled at 1-minute from Binance
python run.py --only cr-2020-covid eq-2008-lehman
```

Output lands in `data/`:

```
data/raw/<event_id>.parquet       raw OHLCV as pulled
data/signals/<event_id>.parquet   s/v/sigma/q time series
data/output/metrics.csv           one row per (event, signal): baseline,
                                   peak, peak_ratio, time_to_peak_h,
                                   duration_above_h, recovery_h, n_obs
```

Recommended order: run daily first for all 29 events (a few minutes), then
re-run `--resolution minute` for the 11 crypto events from 2017+ to get the
high-resolution time-to-peak that section VI.2 cares about.

## Second pass: spread and equity intraday

Both are now first-class fetchers in `fetchers.py`, wired into `run.py`.

**Dukascopy tick (free)** for spread on FX and metals:

```bash
python run.py --resolution minute --dukascopy --only \
    fx-2013-goldcrash fx-2015-snb fx-2016-brexit fx-2019-jpyflash fx-2022-ldi \
    --append
```

This pulls per-hour binary tick files directly over HTTPS (no Node required),
decodes LZMA + the 20-byte tick layout, resamples to 1-minute OHLCV with
mean bid/ask and tick-count as volume, and computes spread $s$ in bps of mid.

**Polygon.io intraday + NBBO (paid, ~29-79 USD/mo)** for equity events:

```bash
export POLYGON_API_KEY=...
python run.py --resolution minute --polygon-nbbo --only \
    eq-2010-flashcrash eq-2015-chinadev eq-2018-volmageddon --append
```

To use Polygon, also change `primary_source` for the relevant rows in
`events.csv` from `yahoo` to `polygon`. The fetcher pulls 1-minute aggs
and samples one NBBO quote per minute, returning bid/ask alongside OHLCV.

## Recompute metrics without re-pulling

If you change `signals.py` (methodology) and want updated `metrics.csv`
from the already-downloaded raw parquets:

```bash
python recompute.py
python recompute.py --only cr-2017-btctop cr-2020-covid
```

This reads everything from `data/raw/*.parquet`, re-derives signals, and
overwrites `data/output/metrics.csv` plus per-event signal files.

## Known data caveats (carried from section VIII of the doc)

- `cr-2023-curve` and `fx-2019-jpyflash` had wrong dates in the catalogue;
  `events.csv` uses the real event dates (2023-07-30 and 2019-01-03) and notes
  it in the `notes` column.
- `fx-2020-wti` has no usable spread (settlement-only); price and volume only.
- Crypto volume before ~2019 is contaminated by wash trading on some venues;
  the daily CryptoCompare aggregate is cleaner than single-venue pulls but
  still treat pre-2019 `q` with caution.
- 1987 has no intraday or spread; daily only.
