# FireRate Research

Внутренний репозиторий с исследованием механизма FireRate для Outbe Network L1. Содержит теоретическое описание, исторический датасет стрессовых событий, референс baseline-спреда для свежих токенов, пайплайн сборки данных и наблюдения для калибровки.

## Структура

```
firerate-research/
├── docs/
│   ├── [Research] Firerate Formulas.md              # каноничное описание механизма
│   ├── [Research] Panic Sales.md                    # 30 стресс-событий + §VII Observations
│   ├── [Research] Baseline Spread Evolution.md      # 10 свежих токенов, эволюция спреда
│   ├── [Validation] Phase 1 Findings.md             # валидация Phase 1
│   └── [Visualization] v2.0.html                    # интерактивная визуализация (5 вкладок, KaTeX)
├── data-mining/                                     # пайплайн выгрузки и расчёта
│   ├── README.md
│   ├── requirements.txt
│   ├── events.csv                                   # каталог 30 стресс-событий
│   ├── events_baseline.csv                          # 10 свежих токенов для baseline-корпуса
│   ├── config.py
│   ├── fetchers.py                                  # Yahoo/FRED/CryptoCompare/Binance/Dukascopy/Polygon
│   ├── signals.py                                   # s, v, sigma, q, d, s_eff, illiq + метрики
│   ├── run.py                                       # оркестратор основного датасета
│   ├── recompute.py                                 # пересчёт метрик без повторной выгрузки
│   ├── correlations.py                              # within-event + across-event матрицы
│   ├── estimators.py                                # Corwin-Schultz + Roll + Amihud оценщики
│   ├── baseline_corpus.py                           # оркестратор baseline-корпуса
│   └── data/
│       ├── raw/                                     # сырые OHLCV/тики, 30 parquet
│       ├── signals/                                 # рассчитанные сигналы, 30 parquet
│       ├── output/
│       │   ├── metrics.csv                          # главный результат: 132 строки метрик
│       │   ├── event_status.csv                     # аудит источников и качества
│       │   ├── missing.csv                          # события без метрик (сейчас пусто)
│       │   └── analysis/                            # производные таблицы: корреляции, топы, оценщики
│       └── baseline/
│           └── token_days.csv                       # baseline-корпус: 10 токенов × 12 sample-дней
├── LICENSE                                          # MIT
└── .gitignore                                       # исключает кэши, venv, snapshot-артефакты
```

## Как читать в первый раз

1. **Открой `docs/[Research] Firerate Formulas.md`** — каноничное описание механизма: сигналы, sigmoid-множитель, ramp-limit, инварианты I1-I8, cost-of-attack. База для понимания всего остального.
2. **Открой `docs/[Visualization] v2.0.html`** в браузере — пять вкладок: формальная модель, пайплайн вычислений, контекст, инварианты, игротехника.
3. **Прочитай `docs/[Research] Panic Sales.md`** — исследование сбора 30 исторических стресс-событий. §VII содержит 13 подразделов наблюдений, включая корреляционную матрицу и итог для калибровки.
4. **Прочитай `docs/[Research] Baseline Spread Evolution.md`** — отдельный research stream: эволюция «нормального» спреда для свежего $10-100M mcap токена в первые 180 дней после листинга. Референс для baseline-параметров FireRate под COEN-подобный токен.
5. **Посмотри `data-mining/data/output/metrics.csv`** — 132 строки: 30 событий × до 7 сигналов ($s$, $v$, $\sigma$, $q$, $d$, $s_{eff}$, `illiq`) с baseline, peak, peak_ratio, time_to_peak, duration_above, recovery.

## Как воспроизвести результаты с нуля

```bash
cd data-mining
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Бесплатные ключи (10 секунд на регистрацию каждого)
export FRED_API_KEY=...            # https://fred.stlouisfed.org
export CRYPTOCOMPARE_API_KEY=...   # https://min-api.cryptocompare.com

# 1) Дневной проход по всем 30 стресс-событиям
python run.py

# 2) Минутка по крипте (Binance klines через API)
python run.py --resolution minute --append --only \
    cr-2017-btctop cr-2019-tether cr-2020-covid cr-2021-elonchina \
    cr-2022-celsius cr-2022-ftx cr-2022-luna cr-2023-curve cr-2023-usdc \
    cr-2024-etf cr-2018-bchfork-abc cr-2018-bchfork-sv

# 3) Тики Dukascopy для FX и металлов (реальный bid/ask)
python run.py --resolution minute --dukascopy --append --only \
    fx-2013-goldcrash fx-2015-snb fx-2016-brexit fx-2019-jpyflash fx-2022-ldi

# 4) Реальный спред + глубина через Binance futures bookTicker (только для событий 2023+)
python run.py --resolution minute --bookticker --append --only \
    cr-2023-curve cr-2024-etf

# 5) Прокси-спред через aggTrades для крипты 2020-2022 (не заменяет real s, отдельный сигнал s_eff)
python run.py --resolution minute --aggtrades --append --only \
    cr-2020-covid cr-2021-elonchina cr-2022-celsius cr-2022-ftx cr-2022-luna

# 6) Пересчёт метрик когда меняешь signals.py
python recompute.py

# 7) Корреляционные матрицы
python correlations.py

# 8) Эконометрические оценщики (CS, Roll, Amihud)
python estimators.py

# 9) Отдельный research stream: baseline-корпус свежих токенов
python baseline_corpus.py
```

Подробности про источники, флаги, фолбэки — в `data-mining/README.md`.

## Главные находки

Полностью в `docs/[Research] Panic Sales.md` §VII (13 подразделов) и `docs/[Research] Baseline Spread Evolution.md`. Кратко:

**Сигналы и калибровка:**

- **Пять независимых сигналов.** Изначально спецификация Firerate требовала 4 ($s$, $v$, $\sigma$, $q$). По ходу работы добавили пятый — `illiq` (Amihud illiquidity, прокси для impact slope). Корреляционная матрица показывает: медианные попарные Spearman-корреляции внутри событий 0.09-0.54, все пять сигналов несут независимую информацию.
- **Спред $s$ реагирует на стресс в 10 раз быстрее волатильности $\sigma$**: TTP по спреду 0.7-1 час на FX-событиях против 4-5 часов по sigma.
- **Восстановление спреда асимметричное**: пик за минуты, возврат к норме до 90 часов. Калибровка FireRate должна иметь разные скорости роста и падения порогов.

**Два режима стресса:**

Отрицательная корреляция `s ↔ q` по peak_ratio (-0.24) и `illiq ↔ s` (-0.40) указывают на два качественно разных режима:
- **«Liquidity drought»** (LDI 2022, SNB 2015) — огромный спред при модерёжном объёме
- **«Panic trading»** (LUNA, ETF approval) — огромный объём при умеренном спреде

FireRate должен обрабатывать оба с разными весами $s$ и $q$.

**Baseline для свежего токена:**

Реальный спред для $30-100M mcap токена на major CEX стабилизируется на ~1-2 bps к дню 60 после листинга. Первые 30 дней требуют консервативных порогов (baseline ещё не устоялся). Множитель сжатия за первые 60 дней: 5-7×.

**Что не сработало (задокументировано как negative results):**

- **aggTrades-прокси $s_{eff}$** не заменяет реальный спред: методологически коррелирует с velocity (0.74 внутри событий), измеряет то же, что $v$, другой математикой.
- **Corwin-Schultz и Roll оценщики** не работают на jump-событиях (наши стрессы): выведены под Броуновскую волатильность, относят амплитуду скачков на счёт спреда, завышают peak_ratio в 10-100 раз.

## Покрытие датасета

| Сигнал | Покрытие | Источник |
|---|---|---|
| $v$ (velocity) | 30/30 | все источники |
| $\sigma$ (volatility) | 30/30 | вычисляется |
| $q$ (volume) | 26/30 | все, кроме FX-yahoo с нулевыми объёмами |
| `illiq` (Amihud) | 24/30 | вычисляется из OHLCV |
| $s$ (real spread) | 7/30 | 5 FX Dukascopy tick + 2 crypto Binance bookTicker |
| $s_{eff}$ (proxy) | 5/30 | aggTrades price range (отдельный сигнал, не замена $s$) |
| $d$ (depth top) | 2/30 | bookTicker для 2 crypto |

7 событий имеют полный 4-сигнальный набор ($s$, $v$, $\sigma$, $q$): 5 FX Dukascopy + cr-2023-curve + cr-2024-etf. Плюс baseline-корпус: 10 свежих токенов × 12 sample-дней = 120 sample-точек эволюции спреда.

## Известные ограничения

- **Equity intraday и NBBO не покрыт бесплатно.** Для 8-9 US equity-событий (Flash Crash 2010, Volmageddon 2018 и др.) нужен Massive/Polygon Stocks Starter (~$29/мес). Код фетчера готов в `fetchers.py:fetch_polygon_intraday`, включается через `POLYGON_API_KEY`. Спецификация тимлиду: https://massive.com/pricing
- **Crypto пре-2023 не имеет реального $s$ и $d$ бесплатно.** Binance Vision архив bookTicker начинается только с мидьlle-2023. Для Mt.Gox 2014, BTC-top 2017, BCH fork 2018 нужен Kaiko (~$1200+/мес), но для калибровки первого прохода не критично.
- **Часть тиковых FX-данных Dukascopy имеет provider-side gaps.** USDJPY декабрь 2018 потерял ~270 часов, GBPUSD во время Brexit-голосования 67 часов. Само событие в окне есть, baseline может быть слегка смещён.
- **Wash-trading в исторических объёмах крипты до 2019.** Используем только Binance/Coinbase/Kraken, эффект остаточный. Подробно — `[Research] Panic Sales.md` §VIII.1.
- **Survivorship bias.** Биржи, не пережившие 2014-2017 годы, исключены. Их данные либо недоступны, либо недостоверны.

Полный список ограничений и компромиссов — `[Research] Panic Sales.md` §VIII.

## Ссылки на внутренние документы Outbe

Доки в `docs/` ссылаются на внутренние спецификации Outbe, не входящие в этот репозиторий:

- `[Specification] Firerate.md` и `[Specification] Firerate mechanism.md` — спецификация механизма, source of truth.
- `[Research] Firerate Formula - discussion.md` — обсуждение математических альтернатив.

Эти документы лежат в основном рабочем пространстве Outbe и доступны коллегам отдельно.

## Лицензия

MIT, см. `LICENSE`.
