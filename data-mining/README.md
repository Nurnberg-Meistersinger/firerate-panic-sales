# FireRate data-mining pipeline

Extracts data for two research streams:

1. **Main dataset** — 30 historical stress events from `[Research] Panic Sales.md` §III. For each event we compute the FireRate signals ($s$, $v$, $\sigma$, $q$, $d$, $s_{eff}$, `illiq`) and stress metrics (baseline, peak, peak_ratio, time_to_peak, duration, recovery).
2. **Baseline corpus** — 10 fresh ($10-100M mcap) tokens listed in 2023-2024. For each we sample 12 log-spaced days (1, 3, 7 ... 180 after listing) and collect real spread. Reference for FireRate baseline calibration under a fresh COEN-like token. Details in `[Research] Baseline Spread Evolution.md`.

The pipeline runs locally (Cowork sandbox blocks exchange APIs).

## Files

**Event catalogues:**
- `events.csv` — 30 stress events, one row each
- `events_baseline.csv` — 10 fresh tokens for the baseline corpus

**Orchestrators:**
- `run.py` — main pipeline: raw data fetch + signal derivation + metrics
- `baseline_corpus.py` — sampled fetch by day-since-listing for the baseline corpus
- `recompute.py` — recompute metrics without re-fetching (when `signals.py` changes)
- `correlations.py` — within-event and across-event correlation matrices with bootstrap CIs
- `estimators.py` — econometric spread estimators (Corwin-Schultz, Roll, Amihud)
- `sensitivity.py` — parameter sensitivity analysis (BASELINE_DAYS, STRESS_MULTIPLE)

**Core:**
- `fetchers.py` — all data sources (Yahoo, FRED, CryptoCompare, Binance klines/bookTicker/aggTrades, Dukascopy tick, Polygon stub)
- `signals.py` — signal construction from OHLCV/bid-ask and episode-local metric computation
- `config.py` — constants (baseline_days, stress_multiple, API key paths)

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

Free keys are optional:

```bash
export FRED_API_KEY=...            # https://fred.stlouisfed.org/docs/api/api_key.html
export CRYPTOCOMPARE_API_KEY=...   # https://min-api.cryptocompare.com (free tier)
```

Without a key CryptoCompare works anonymously (100k calls/month limit). FRED requires a key.

## Free sources and what they cover

| Source | Coverage | Signals | Flag |
|---|---|---|---|
| Yahoo (`yfinance`) | equity, commodity, some FX, daily | $v$, $\sigma$, $q$ | default |
| FRED | VIX, gold fix, FX rates, gilt yields, daily | $v$, $\sigma$ | default |
| CryptoCompare | crypto since 2010, daily/hourly | $v$, $\sigma$, $q$ | `--crypto-daily-source cryptocompare` |
| Binance klines (S3 or API) | crypto since 2017, 1-min | $v$, $\sigma$, $q$ | `--resolution minute` |
| Binance bookTicker (futures) | crypto since mid-2023 | $s$ (real), $d$ | `--bookticker` |
| Binance aggTrades (spot) | crypto since 2017 | $s_{eff}$ (proxy) | `--aggtrades` |
| Dukascopy tick | FX majors + XAUUSD since 2003 | $s$ (real) | `--dukascopy` |

Amihud illiquidity (`illiq`) is computed automatically from OHLCV wherever volume is available.

Paid-only:
- Polygon.io / Massive — equity intraday + NBBO for 8-9 US events. Fetcher is ready (`fetch_polygon_intraday`), activated via `POLYGON_API_KEY` and `primary_source=polygon` in `events.csv`.
- Kaiko — crypto L2 history before 2023. Expensive, not justified for our task.

## Main run: stress events

```bash
python run.py --list                          # catalogue of 30 events

# 1) Daily pass over all 30 events (5-10 minutes)
python run.py

# 2) Minute resolution for crypto (30-60 minutes)
python run.py --resolution minute --append --only \
    cr-2017-btctop cr-2019-tether cr-2020-covid cr-2021-elonchina \
    cr-2022-celsius cr-2022-ftx cr-2022-luna cr-2023-curve cr-2023-usdc \
    cr-2024-etf cr-2018-bchfork-abc cr-2018-bchfork-sv

# 3) Dukascopy tick for FX and metals (30-60 minutes with disk cache)
python run.py --resolution minute --dukascopy --append --only \
    fx-2013-goldcrash fx-2015-snb fx-2016-brexit fx-2019-jpyflash fx-2022-ldi

# 4) Real spread + depth via Binance futures bookTicker (2023+ events only)
python run.py --resolution minute --bookticker --append --only \
    cr-2023-curve cr-2024-etf

# 5) Proxy spread s_eff via aggTrades for pre-2023 crypto
# NOTE: s_eff does NOT substitute for real s (documented in [Research] Panic Sales.md §VII.10)
python run.py --resolution minute --aggtrades --append --only \
    cr-2020-covid cr-2021-elonchina cr-2022-celsius cr-2022-ftx cr-2022-luna
```

**Output:**

```
data/raw/<event_id>.parquet       raw OHLCV + bid/ask/depth where available
data/signals/<event_id>.parquet   s, v, sigma, q, d, s_eff, illiq
data/output/metrics.csv           132 rows (event × signal × resolution)
data/output/event_status.csv      source, symbol, coverage, quality_note
data/output/missing.csv           events without metrics
```

## run.py flags

| Flag | What it does |
|---|---|
| `--resolution day|minute` | Daily or minute resolution (default: day) |
| `--only ID [ID...]` | Limit the set to specific events |
| `--append` | Append/update metrics.csv, do not overwrite |
| `--bookticker` | For crypto+minute pull Binance futures bookTicker (real bid/ask) |
| `--aggtrades` | For crypto+minute pull aggTrades → `s_eff` proxy |
| `--dukascopy` | For FX/metals pull Dukascopy tick → real spread |
| `--dukascopy-lookback-days N` | Cap Dukascopy lookback for smoke tests |
| `--manual-daily-fallbacks` | For Dukascopy events fall back to daily Yahoo when tick is unavailable |
| `--crypto-daily-source binance-api|binance-s3|cryptocompare` | Source for crypto daily |
| `--crypto-minute-source binance-api|binance-s3` | Source for crypto minute (API more stable, S3 faster) |
| `--min-bars N` | Minimum bars to accept a fallback symbol |
| `--polygon-nbbo` | When `primary_source=polygon` sample NBBO for spread (requires `POLYGON_API_KEY`) |

## Recompute metrics without re-fetching

If you change `signals.py` (new signal, new methodology) and do not want to re-download:

```bash
python recompute.py                                     # all events
python recompute.py --only cr-2020-covid eq-2008-lehman # specific events only
```

Reads `data/raw/*.parquet`, rebuilds signals, overwrites `data/signals/` and `data/output/metrics.csv`.

## Analytics

**Correlation matrices with 95% bootstrap CIs:**

```bash
python correlations.py
```

Computes two-level Spearman with 2000-iteration bootstrap confidence intervals:
- **Within-event**: pairwise correlations of signals inside each event's window at minute resolution
- **Across-event**: correlations of peak_ratio across events

Output in `data/output/analysis/correlations_*.csv`, including `_ci95.csv` variants with per-cell CIs.

**Sensitivity analysis:**

```bash
python sensitivity.py
```

Recomputes metrics with BASELINE_DAYS ∈ {15, 30, 60} and STRESS_MULTIPLE ∈ {1.5, 2, 3}. Reports:
- Median absolute % change in peak_ratio vs the (30, 2.0) reference
- Spearman rank correlation with reference ranking

Result across the current dataset: peak_ratio changes by <10-20%, rank correlation stays >= 0.97. Findings are not parameter artifacts.

**Econometric spread estimators:**

```bash
python estimators.py
```

Three estimators applied to all 30 events using existing OHLCV:
- **Corwin-Schultz (2012)** — from high/low of two consecutive periods
- **Roll (1984)** — from returns autocovariance
- **Amihud (2002)** — illiquidity ratio |return|/dollar_volume

Output:
- `data/output/analysis/estimators_by_event.csv` — baseline and peak per estimator
- `data/output/analysis/estimators_vs_real_spread.csv` — comparison against real $s$ on 7 events

Key finding: **CS and Roll fail on jump events** (our stresses). They assume Brownian volatility, inflate peak ratios by 10-100x. Amihud works and is included as a first-class signal `illiq` in the main metrics. Details in `[Research] Panic Sales.md` §VII.11-12 and `data/output/analysis/academic_crosscheck.md`.

## Baseline corpus (separate research stream)

```bash
python baseline_corpus.py --list    # catalogue of 10 tokens
python baseline_corpus.py           # sampled fetch across 12 days for each
```

Sample scheme: log-spaced days 1, 3, 7, 14, 21, 30, 45, 60, 90, 120, 150, 180 after listing. For each point we try bookTicker first (real bid/ask), then fall back to aggTrades (`s_eff` proxy). Result: `data/baseline/token_days.csv` with 120 rows.

Key finding from the collected data: real spread for a fresh $30-100M mcap token stabilizes at **~1-2 bps** by day 60 after listing. Initial 30 days require conservative thresholds. Details in `[Research] Baseline Spread Evolution.md`.

## Cache and disk

Large downloads are cached on disk under `data/raw/` for fast Ctrl+C resume:

- `data/raw/bookticker_futures/<SYMBOL>/agg-YYYY-MM-DD.parquet` — 1-min bookTicker aggregates (~50-80 KB per day)
- `data/raw/aggtrades/<SYMBOL>/agg-YYYY-MM-DD.parquet` — 1-min aggTrades aggregates (~40-60 KB per day)
- `data/raw/dukascopy/<PAIR>/YYYY-MM-DD_HH.bi5` — hourly .bi5 files (~30-200 KB each)

Source ZIP files (200-500 MB per BTCUSDT bookTicker day) are not stored — they are streamed, aggregated in memory, and discarded.

The parent repository's `.gitignore` excludes these caches so git does not blow up.

## Known specifics

- `cr-2023-curve` and `fx-2019-jpyflash` had wrong dates in the original catalogue; `events.csv` uses the real event dates (2023-07-30 and 2019-01-03) with a note in `notes`.
- `fx-2020-wti` has no real spread (settlement-only); price and volume only. Velocity is computed as absolute price change (not log-return) because the April 20, 2020 close went negative.
- Pre-2019 crypto volume is heavily contaminated by wash trading on some venues; the CryptoCompare aggregated median is cleaner than single-venue pulls.
- 1987 Black Monday has no intraday or spread; daily only.
- BCH hash war 2018 is split into two events: `cr-2018-bchfork-abc` (BCHABCUSDT) and `cr-2018-bchfork-sv` (BCHSVUSDT), because after the fork they are different assets.
- `cr-2018-bchfork-*` and `cr-2023-usdc` events have `baseline_source=empty` in `metrics.csv` because pre-window data does not exist for these symbols. Their peak_ratio is NaN and downstream correlations ignore them.

Full list of limitations and trade-offs: `[Research] Panic Sales.md` §VIII.
