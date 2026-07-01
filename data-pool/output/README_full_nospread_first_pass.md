# Full no-spread first pass

Generated dataset:
- metrics_full_nospread_first_pass.csv
- event_status_full_nospread_first_pass.csv
- missing_full_nospread_first_pass.csv

Coverage:
- 29/29 events covered
- signals: v, sigma, q for every event
- resolution: daily
- spread / bid-ask data intentionally excluded

Known caveats:
- cr-2018-bchfork uses BCHABCUSDT as proxy for BCHUSDT
- cr-2023-usdc uses BTCUSDT as proxy for USDCUSDT due insufficient USDCUSDT bars
- manual Dukascopy FX events use daily Yahoo fallback, no bid/ask/spread
- fx-2020-wti dropped one non-positive close
