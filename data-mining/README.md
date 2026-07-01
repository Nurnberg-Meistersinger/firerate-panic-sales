# FireRate data-mining pipeline

Извлекает данные для двух исследовательских потоков:

1. **Основной датасет** — 30 исторических стресс-событий из `[Research] Panic Sales.md` §III. По каждому считаем сигналы FireRate ($s$, $v$, $\sigma$, $q$, $d$, $s_{eff}$, `illiq`) и стресс-метрики (baseline, peak, peak_ratio, time_to_peak, duration, recovery).
2. **Baseline-корпус** — 10 свежих ($10-100M mcap) токенов, листинги 2023-2024. По каждому в 12 sample-точках (день 1, 3, 7 ... 180 после листинга) собираем реальный спред. Референс для калибровки baseline FireRate под свежий COEN-подобный токен. Подробно в `[Research] Baseline Spread Evolution.md`.

Пайплайн запускается локально (внутренние ограничения sandbox блокируют биржевые API).

## Файлы

**Каталоги событий:**
- `events.csv` — 30 стресс-событий, по одной строке
- `events_baseline.csv` — 10 свежих токенов для baseline-корпуса

**Оркестраторы:**
- `run.py` — основной пайплайн: загрузка сырых данных + расчёт сигналов + метрики
- `baseline_corpus.py` — sampled-fetch по дням-с-листинга для baseline-корпуса
- `recompute.py` — пересчёт метрик без повторной выгрузки (когда меняется `signals.py`)
- `correlations.py` — within-event + across-event корреляционные матрицы
- `estimators.py` — эконометрические оценщики спреда (Corwin-Schultz, Roll, Amihud)

**Ядро:**
- `fetchers.py` — все источники данных (Yahoo, FRED, CryptoCompare, Binance klines/bookTicker/aggTrades, Dukascopy tick, Polygon-заглушка)
- `signals.py` — построение сигналов из OHLCV/bid-ask и расчёт эпизод-локальных метрик
- `config.py` — константы (baseline_days, stress_multiple, пути к API-ключам)

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

Бесплатные ключи по желанию:

```bash
export FRED_API_KEY=...            # https://fred.stlouisfed.org/docs/api/api_key.html
export CRYPTOCOMPARE_API_KEY=...   # https://min-api.cryptocompare.com (free tier)
```

Без ключей CryptoCompare работает по анониму (лимит 100k вызовов/мес). FRED без ключа не работает.

## Что где бесплатно и что даёт

| Источник | Покрывает | Сигналы | Флаг |
|---|---|---|---|
| Yahoo (`yfinance`) | equity, commodity, часть FX, daily | $v$, $\sigma$, $q$ | default |
| FRED | VIX, gold fix, FX-rates, gilt yields, daily | $v$, $\sigma$ | default |
| CryptoCompare | crypto с 2010, daily/hourly | $v$, $\sigma$, $q$ | `--crypto-daily-source cryptocompare` |
| Binance klines (S3 или API) | crypto с 2017, 1-min | $v$, $\sigma$, $q$ | `--resolution minute` |
| Binance bookTicker (futures) | crypto с mid-2023 | $s$ (real), $d$ | `--bookticker` |
| Binance aggTrades (spot) | crypto с 2017 | $s_{eff}$ (proxy) | `--aggtrades` |
| Dukascopy tick | FX-мажоры + XAUUSD с 2003 | $s$ (real) | `--dukascopy` |

Amihud illiquidity (`illiq`) рассчитывается автоматически из OHLCV во всех источниках, где есть volume.

Только за деньги:
- Polygon.io / Massive — equity intraday + NBBO для 8-9 US-событий. Код фетчера готов (`fetch_polygon_intraday`), включается через `POLYGON_API_KEY` и `primary_source=polygon` в `events.csv`.
- Kaiko — L2-история крипты до 2023. Дорого, не окупается для нашей задачи.

## Основной запуск — стресс-события

```bash
python run.py --list                          # каталог 30 событий

# 1) Дневной проход по всем 30 событиям (5-10 минут)
python run.py

# 2) Минутка по крипте с 2017+ (30-60 минут)
python run.py --resolution minute --append --only \
    cr-2017-btctop cr-2019-tether cr-2020-covid cr-2021-elonchina \
    cr-2022-celsius cr-2022-ftx cr-2022-luna cr-2023-curve cr-2023-usdc \
    cr-2024-etf cr-2018-bchfork-abc cr-2018-bchfork-sv

# 3) Тики Dukascopy для FX и металлов (30-60 минут, с диск-кэшем)
python run.py --resolution minute --dukascopy --append --only \
    fx-2013-goldcrash fx-2015-snb fx-2016-brexit fx-2019-jpyflash fx-2022-ldi

# 4) Реальный spread + depth через Binance futures bookTicker (только события 2023+)
python run.py --resolution minute --bookticker --append --only \
    cr-2023-curve cr-2024-etf

# 5) Прокси-спред s_eff через aggTrades для крипты 2020-2022
# ВАЖНО: s_eff НЕ заменяет s (документировано в [Research] Panic Sales.md §VII.10)
python run.py --resolution minute --aggtrades --append --only \
    cr-2020-covid cr-2021-elonchina cr-2022-celsius cr-2022-ftx cr-2022-luna
```

**Выход:**

```
data/raw/<event_id>.parquet       сырой OHLCV + bid/ask/depth где есть
data/signals/<event_id>.parquet   s, v, sigma, q, d, s_eff, illiq
data/output/metrics.csv           132 строки (событие × сигнал × разрешение)
data/output/event_status.csv      источник, символ, покрытие, quality_note
data/output/missing.csv           события без метрик
```

## Флаги run.py

| Флаг | Что делает |
|---|---|
| `--resolution day|minute` | Дневное или минутное разрешение (default day) |
| `--only ID [ID...]` | Ограничить набор конкретными событиями |
| `--append` | Добавлять/обновлять metrics.csv, не перезаписывать |
| `--bookticker` | Для крипты+minute подтянуть Binance futures bookTicker (real bid/ask) |
| `--aggtrades` | Для крипты+minute подтянуть aggTrades → `s_eff` proxy |
| `--dukascopy` | Для FX/металлов подтянуть тик Dukascopy → real spread |
| `--dukascopy-lookback-days N` | Ограничить lookback Dukascopy для теста |
| `--manual-daily-fallbacks` | Для Dukascopy-событий фолбэк на дневной Yahoo если тик недоступен |
| `--crypto-daily-source binance-api|binance-s3|cryptocompare` | Источник для крипто daily |
| `--crypto-minute-source binance-api|binance-s3` | Источник для крипто minute (API стабильнее, S3 быстрее) |
| `--min-bars N` | Минимум баров для приёма fallback-символа |
| `--polygon-nbbo` | При `primary_source=polygon` семплить NBBO для спреда (требует `POLYGON_API_KEY`) |

## Пересчёт метрик без пересборки

Если поменял `signals.py` (новый сигнал, новая методология), не хочешь качать заново:

```bash
python recompute.py                                     # все события
python recompute.py --only cr-2020-covid eq-2008-lehman # только указанные
```

Читает `data/raw/*.parquet`, пересобирает сигналы, перезаписывает `data/signals/` и `data/output/metrics.csv`.

## Аналитика

**Корреляционные матрицы:**

```bash
python correlations.py
```

Считает two-level Spearman:
- **Within-event**: попарные корреляции сигналов внутри окна каждого события с минутной резолюцией
- **Across-event**: корреляции peak_ratio по событиям

Выход в `data/output/analysis/correlations_*.csv`.

**Эконометрические оценщики спреда:**

```bash
python estimators.py
```

Три оценщика, применённые ко всем 30 событиям на существующих OHLCV:
- **Corwin-Schultz (2012)** — из high-low двух подряд периодов
- **Roll (1984)** — из автоковариации возвратов
- **Amihud (2002)** — illiquidity ratio |return|/dollar_volume

Выход:
- `data/output/analysis/estimators_by_event.csv` — baseline и peak для каждого оценщика
- `data/output/analysis/estimators_vs_real_spread.csv` — сравнение с реальным $s$ на 7 событиях

Ключевой факт: **CS и Roll не работают на jump-событиях** (наши стрессы), они выведены под Броуновскую волатильность. Amihud работает и включён как первоклассный сигнал `illiq` в основные метрики. Детали в `[Research] Panic Sales.md` §VII.11-12.

## Baseline-корпус (отдельный research stream)

```bash
python baseline_corpus.py --list    # каталог 10 токенов
python baseline_corpus.py           # sampled fetch по 12 дням для каждого
```

Sample-схема: log-spaced дни 1, 3, 7, 14, 21, 30, 45, 60, 90, 120, 150, 180 после листинга. По каждой точке пробуется сначала bookTicker (real bid/ask), фолбэк aggTrades (`s_eff` proxy). Итог `data/baseline/token_days.csv` — 120 строк.

Ключевой факт из полученных данных: реальный спред для свежего $30-100M mcap токена стабилизируется на **~1-2 bps** к дню 60 после листинга. Первые 30 дней требуют консервативных порогов. Подробно в `[Research] Baseline Spread Evolution.md`.

## Кэш и диск

Все скачиваемые крупные файлы кэшируются на диске в `data/raw/` для быстрого возобновления после Ctrl+C:

- `data/raw/bookticker_futures/<SYMBOL>/agg-YYYY-MM-DD.parquet` — минутные аггрегаты bookTicker (~50-80 КБ на день)
- `data/raw/aggtrades/<SYMBOL>/agg-YYYY-MM-DD.parquet` — минутные аггрегаты aggTrades (~40-60 КБ на день)
- `data/raw/dukascopy/<PAIR>/YYYY-MM-DD_HH.bi5` — hourly .bi5 файлы (~30-200 КБ)

Сами исходные ZIP-файлы (200-500 МБ на день BTCUSDT bookTicker) не хранятся — стримятся, агрегируются в память, сбрасываются.

`.gitignore` в родительском репозитории исключает эти кэши, чтобы гит не разросся.

## Известные особенности

- `cr-2023-curve` и `fx-2019-jpyflash` имели неверные даты в исходном каталоге; `events.csv` использует реальные даты (2023-07-30 и 2019-01-03) с пометкой в `notes`.
- `fx-2020-wti` не имеет реального спреда (settlement-only); только цена и объём. Velocity считается как абсолютное изменение цены (не log-return) из-за отрицательного клоуза 20 апреля 2020.
- Крипто-volume до 2019 сильно загрязнён wash-trading на некоторых биржах; медиана из CryptoCompare aggregated чище single-venue.
- 1987 Black Monday не имеет intraday или spread; только daily.
- BCH hash war 2018 разбит на два события: `cr-2018-bchfork-abc` (BCHABCUSDT) и `cr-2018-bchfork-sv` (BCHSVUSDT), потому что после fork это разные активы.

Полный список ограничений и компромиссов — `[Research] Panic Sales.md` §VIII.
