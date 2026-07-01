# FireRate Research

Internal repository with the FireRate mechanism research for Outbe Network L1. Contains the theoretical description, the historical stress-event dataset, the baseline spread reference for fresh tokens, the data pipeline, and calibration observations.

## Structure

```
firerate-research/
├── docs/
│   ├── [Research] Firerate Formulas.md              # canonical mechanism description
│   ├── [Research] Panic Sales.md                    # 30 stress events + §VII observations
│   ├── [Research] Baseline Spread Evolution.md      # 10 fresh tokens, spread evolution
│   ├── [Validation] Phase 1 Findings.md             # phase 1 validation
│   └── [Visualization] v2.0.html                    # interactive visualization (5 tabs, KaTeX)
├── data-mining/                                     # extraction and computation pipeline
│   ├── README.md
│   ├── requirements.txt
│   ├── events.csv                                   # 30 stress events catalogue
│   ├── events_baseline.csv                          # 10 fresh tokens for baseline corpus
│   ├── config.py
│   ├── fetchers.py                                  # Yahoo, FRED, CryptoCompare, Binance, Dukascopy, Polygon
│   ├── signals.py                                   # s, v, sigma, q, d, s_eff, illiq + metrics
│   ├── run.py                                       # main dataset orchestrator
│   ├── recompute.py                                 # recompute metrics without re-fetching
│   ├── correlations.py                              # within-event + across-event matrices with bootstrap CIs
│   ├── estimators.py                                # Corwin-Schultz, Roll, Amihud estimators
│   ├── sensitivity.py                               # parameter sensitivity analysis
│   ├── baseline_corpus.py                           # baseline corpus orchestrator
│   └── data/
│       ├── raw/                                     # raw OHLCV/ticks, 30 parquets
│       ├── signals/                                 # derived signals, 30 parquets
│       ├── output/
│       │   ├── metrics.csv                          # main result: 132 rows
│       │   ├── event_status.csv                     # source and quality audit
│       │   ├── missing.csv                          # events with no metrics (currently empty)
│       │   └── analysis/                            # derived tables: correlations, tops, estimators, academic crosscheck
│       └── baseline/
│           └── token_days.csv                       # baseline corpus: 10 tokens × 12 sample days
├── LICENSE                                          # MIT
└── .gitignore                                       # excludes caches, venv, snapshot artifacts
```

## First read

1. **Open `docs/[Research] Firerate Formulas.md`** — canonical mechanism description: signals, sigmoid multiplier, ramp limit, invariants I1-I8, cost of attack. This is the foundation for everything else.
2. **Open `docs/[Visualization] v2.0.html`** in a browser — five tabs: formal model, computation pipeline, context, invariants, game theory.
3. **Read `docs/[Research] Panic Sales.md`** — research on the 30 historical stress events. §VII opens with a plain-language summary of what actually held up under bootstrap confidence intervals versus what was a small-sample guess.
4. **Read `docs/[Research] Baseline Spread Evolution.md`** — separate research stream: how the "normal" spread of a fresh $10-100M mcap token evolves over the first 180 days after listing. Reference for FireRate baseline parameters for a COEN-like token.
5. **Look at `data-mining/data/output/metrics.csv`** — 132 rows: 30 events × up to 7 signals (s, v, sigma, q, d, s_eff, illiq) with baseline, peak, peak_ratio, time_to_peak, duration_above, recovery.

## Reproduce from scratch

```bash
cd data-mining
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Free keys (10 seconds to register each)
export FRED_API_KEY=...            # https://fred.stlouisfed.org
export CRYPTOCOMPARE_API_KEY=...   # https://min-api.cryptocompare.com

# 1) Daily pass over all 30 stress events
python run.py

# 2) Minute resolution for crypto (Binance klines via API)
python run.py --resolution minute --append --only \
    cr-2017-btctop cr-2019-tether cr-2020-covid cr-2021-elonchina \
    cr-2022-celsius cr-2022-ftx cr-2022-luna cr-2023-curve cr-2023-usdc \
    cr-2024-etf cr-2018-bchfork-abc cr-2018-bchfork-sv

# 3) Dukascopy tick for FX and metals (real bid/ask)
python run.py --resolution minute --dukascopy --append --only \
    fx-2013-goldcrash fx-2015-snb fx-2016-brexit fx-2019-jpyflash fx-2022-ldi

# 4) Real spread + depth via Binance futures bookTicker (2023+ events only)
python run.py --resolution minute --bookticker --append --only \
    cr-2023-curve cr-2024-etf

# 5) Proxy spread via aggTrades for pre-2023 crypto (documented as NOT a substitute for real s)
python run.py --resolution minute --aggtrades --append --only \
    cr-2020-covid cr-2021-elonchina cr-2022-celsius cr-2022-ftx cr-2022-luna

# 6) Recompute metrics after changes to signals.py
python recompute.py

# 7) Correlation matrices with 95% bootstrap CIs
python correlations.py

# 8) Econometric estimators (CS, Roll, Amihud)
python estimators.py

# 9) Sensitivity analysis on BASELINE_DAYS and STRESS_MULTIPLE
python sensitivity.py

# 10) Separate research stream: baseline corpus of fresh tokens
python baseline_corpus.py
```

See `data-mining/README.md` for source details, flags, and fallbacks.

## What we found

Full details in `docs/[Research] Panic Sales.md` §VII and `docs/[Research] Baseline Spread Evolution.md`. Summary of what held up under 2000-iteration bootstrap confidence intervals:

**Robust findings (17+ events, CI does not cross zero):**

- Signal pairs v-sigma, v-q, v-illiq, sigma-q have Spearman correlations in the 0.43-0.54 band with tight CIs. Signals move together in stress but do not collapse into each other, so keeping them separate is justified.
- q-illiq is negatively correlated (-0.28, CI [-0.36, -0.11]). Mathematically expected (illiq = |return|/dollar_volume falls when volume rises) but confirmed empirically.
- Peak ratios change by less than 10% when varying BASELINE_DAYS from 15 to 60. Event ranking stays stable (Spearman rank correlation >= 0.97). Findings about extreme events (LUNA, SNB) are not parameter artifacts.

**Downgraded to hypothesis (7 events, CI wide or crosses zero):**

- "Spread orthogonal to v, sigma, q" is directionally supported but not statistically confirmed. At n=7 the CI for s-v is [-0.05, 0.38], for s-q is [-0.17, 0.37]. Both cross zero.
- "Two stress regimes" (liquidity drought vs panic trading) was built on s-q cross-event correlation of -0.24. Under resampling the CI is [-0.95, 1.00], completely uninformative. Hypothesis remains, no confirmation.
- Spread reacts faster than sigma (TTP 0.7 vs 4-5 hours). Direction clear on 5 FX + 2 crypto events, but 7 observations do not support formal testing.

**Calibration traps to avoid:**

- SNB peak_ratio_v = 13031x is a pegged-baseline artifact, not a physical shock measurement. Use absolute peak in bps, not the ratio.
- LUNA peak_ratio_v = 1188x is inflated by minute aggregation plus tight pre-collapse baseline. Academic tick-level estimates: 50-200x. Our number is a methodological ceiling, not a physical one.
- Flash Crash 2010 peak_ratio_v = 6.68x on daily resolution is broken by design (the event was 36 minutes intraday). Not usable for calibration without Polygon intraday data.

**Baseline for a fresh token:**

Real spread for a fresh $30-100M mcap token on a major CEX stabilizes at ~1-2 bps by day 60 after listing. Initial 30 days require conservative thresholds (baseline has not settled). Compression multiplier over the first 60 days: 5-7x.

**Negative results (documented so we do not repeat them):**

- aggTrades s_eff proxy is not a substitute for real spread. It correlates with velocity (0.74 within events) because it measures intra-minute price range, not book width.
- Corwin-Schultz and Roll estimators fail on jump events (our stresses). They assume Brownian volatility and inflate peak_ratio by 10-100x. Amihud illiquidity works because it makes no such assumption.

## Coverage

| Signal | Coverage | Source |
|---|---|---|
| v (velocity) | 30/30 | all sources |
| sigma (volatility) | 30/30 | computed |
| q (volume) | 26/30 | all except Yahoo FX with zero volume |
| illiq (Amihud) | 24/30 | computed from OHLCV |
| s (real spread) | 7/30 | 5 FX Dukascopy tick + 2 crypto Binance bookTicker |
| s_eff (proxy) | 5/30 | aggTrades price range (documented as NOT a substitute for s) |
| d (depth top) | 2/30 | bookTicker for 2 crypto events |

7 events have the full 4-signal set (s, v, sigma, q): 5 FX Dukascopy + cr-2023-curve + cr-2024-etf. Plus the baseline corpus: 10 fresh tokens × 12 sample days = 120 sample points.

## Known limitations

- **Equity intraday and NBBO not covered by free sources.** For 8-9 US equity events (Flash Crash 2010, Volmageddon 2018 and others) we would need Massive/Polygon Stocks Starter (~29 USD/month). Fetcher code is ready in `fetchers.py:fetch_polygon_intraday`, activated via `POLYGON_API_KEY`. Pricing details for the team lead: https://massive.com/pricing
- **Pre-2023 crypto has no free real s or d.** The Binance Vision bookTicker archive starts only mid-2023. For Mt.Gox 2014, BTC top 2017, BCH fork 2018 we would need Kaiko (~1200 USD/month starting tier), which is not justified for a first-pass calibration.
- **Dukascopy tick data has provider-side gaps.** USDJPY December 2018 lost about 270 hours, GBPUSD during the Brexit vote lost 67 hours. The event itself is in the window but baseline and tails may be slightly biased.
- **Wash trading in pre-2019 crypto volumes.** We use only Binance, Coinbase and Kraken but the effect is residual. Details in `[Research] Panic Sales.md` §VIII.1.
- **Survivorship bias.** Exchanges that did not survive 2014-2017 are excluded from the sample. Their data is either unavailable or unreliable.

Full list of limitations and trade-offs: `[Research] Panic Sales.md` §VIII.

## Internal Outbe references

Documents in `docs/` reference internal Outbe specifications that are not included in this repository:

- `[Specification] Firerate.md` and `[Specification] Firerate mechanism.md` — mechanism specification, source of truth.
- `[Research] Firerate Formula - discussion.md` — discussion of mathematical alternatives.

These live in the main Outbe workspace and are available to colleagues separately.

## License

MIT, see `LICENSE`.
