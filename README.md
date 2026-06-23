# FireRate Research

Внутренний репозиторий с исследованием механизма FireRate для Outbe Network L1. Содержит теоретическое описание, исторический датасет стрессовых событий, пайплайн его сборки и первые наблюдения для калибровки.

## Структура

```
firerate-research/
├── docs/                            # исследовательские документы
│   ├── firerate-formulas.md         # каноничное описание механизма с формулами
│   ├── panic-sales.md               # методология сбора датасета + наблюдения §VII
│   ├── validation-phase-1.md        # валидация Phase 1
│   └── computation-flow.html        # интерактивная визуализация (5 вкладок, KaTeX)
├── data-mining/                     # код пайплайна выгрузки и расчёта сигналов
│   ├── README.md
│   ├── requirements.txt
│   ├── events.csv                   # каталог 30 событий
│   ├── config.py
│   ├── fetchers.py                  # Yahoo / FRED / CryptoCompare / Binance / Dukascopy / Polygon
│   ├── signals.py                   # s, v, sigma, q и метрики стресса
│   ├── run.py                       # оркестратор
│   └── recompute.py                 # пересчёт метрик без повторной выгрузки
└── data/
    ├── raw/                         # сырые OHLCV/тики, 30 parquet
    ├── signals/                     # s/v/sigma/q по каждому событию, 30 parquet
    └── output/
        ├── metrics.csv              # главный результат: 108 строк, по сигналу на каждое событие
        ├── event_status.csv         # аудит источников и качества данных
        ├── missing.csv              # события без метрик (сейчас пусто)
        └── analysis/                # производные таблицы: топы, пивоты, сравнения
```

## Как читать в первый раз

1. **Открой `docs/firerate-formulas.md`** — это каноничное описание механизма: четыре сигнала, sigmoid-множитель, ramp-limit, инварианты I1-I8, cost-of-attack. Это база для понимания всего остального.
2. **Открой `docs/computation-flow.html` в браузере** — пять вкладок: формальная модель, пайплайн вычислений, контекст, инварианты, игротехника. Помогает увидеть механизм глазами.
3. **Прочитай `docs/panic-sales.md`** — отдельное исследование про сбор исторических данных для калибровки. §VII содержит наблюдения по реальным цифрам из датасета.
4. **Посмотри `data/output/metrics.csv`** — главный результат: 30 событий × 4 сигнала = до 108 строк с baseline, peak/baseline, time-to-peak, duration_above и recovery time.

## Как воспроизвести результаты с нуля

```bash
cd data-mining
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Бесплатные ключи (10 секунд на регистрацию каждого)
export FRED_API_KEY=...            # https://fred.stlouisfed.org
export CRYPTOCOMPARE_API_KEY=...   # https://min-api.cryptocompare.com

# Дневной проход по всем 30 событиям (5-10 минут)
python run.py

# Минутка по крипте (Binance, 30-60 минут)
python run.py --resolution minute --append --only \
    cr-2017-btctop cr-2019-tether cr-2020-covid cr-2021-elonchina \
    cr-2022-celsius cr-2022-ftx cr-2022-luna cr-2023-curve cr-2023-usdc \
    cr-2024-etf cr-2018-bchfork-abc cr-2018-bchfork-sv

# Тики Dukascopy для FX и металлов (15-30 минут, с кэшем)
python run.py --resolution minute --dukascopy --append --only \
    fx-2013-goldcrash fx-2015-snb fx-2016-brexit fx-2019-jpyflash fx-2022-ldi

# Пересчёт метрик без повторной выгрузки (когда меняешь signals.py)
python recompute.py
```

Подробности про источники, флаги и фолбэки — в `data-mining/README.md`.

## Главные находки

Подробно в `docs/panic-sales.md` §VII. Кратко:

- Спред $s$ реагирует на стресс в 10 раз быстрее, чем волатильность ($\sigma$): TTP по спреду 0.7-1 час на тиковых FX-событиях против 4-5 часов по sigma.
- Восстановление спреда сильно отстаёт от его всплеска: до 90 часов на возврат к норме при пике за минуты. Калибровка FireRate должна быть асимметричной.
- LUNA даёт верхний потолок амплитуды (peak_ratio v = 1188× на минутке, q = 5.5M×). SNB EUR/CHF unpeg — отдельный режим из-за изначального пегга.
- USDC и стейблкоины в целом не ловятся одним порогом по $v$, нужен отдельный режим detection «sustained off-peg».
- Equity без Polygon-intraday остаётся пробелом: Flash Crash 2010 на дневке практически невидим. Это сознательное ограничение по бюджету.

## Известные ограничения

- **Equity intraday не покрыт.** Чтобы получить минутные данные и NBBO для acions (Flash Crash, Volmageddon и др.), нужен Polygon.io (~30 USD/мес). Код фетчера готов в `fetchers.py:fetch_polygon_intraday`, запускается через `POLYGON_API_KEY`.
- **Часть тиковых FX-данных Dukascopy имеет пропуски.** USDJPY декабрь 2018 потерял около 270 часов, GBPUSD во время Brexit-голосования пропустил 67 часов. Это provider-side gap. Само событие в окне присутствует, но baseline может быть слегка смещён.
- **Wash-trading в исторических объёмах крипты до 2019.** Используем только Binance/Coinbase/Kraken, но эффект остаточный. Подробно — `docs/panic-sales.md` §VIII.1.
- **Survivorship bias.** Биржи, не пережившие 2014-2017 годы, исключены из выборки. Их данные либо недоступны, либо недостоверны.

Полный список ограничений и компромиссов — `docs/panic-sales.md` §VIII.

## Ссылки на внутренние документы Outbe

Доки в `docs/` ссылаются на внутренние спецификации Outbe, которые в этот репозиторий не входят:

- `[Specification] Firerate.md` и `[Specification] Firerate mechanism.md` — спецификация механизма, source of truth.
- `[Research] Firerate Formula - discussion.md` — обсуждение математических альтернатив.

Эти документы лежат в основном рабочем пространстве Outbe и доступны коллегам отдельно.

## Лицензия

MIT, см. `LICENSE`.
