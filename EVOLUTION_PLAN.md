# Swing Trader v2 — Evolution Plan

## Goal

Evolve the existing `swing_trader_v1` project into a sophisticated, rules-based swing-trading system that:

- Uses Alpaca for market data and eventual live execution.
- Can rigorously backtest the **same strategy code** that runs live.
- Separates strategy research from execution.
- Prevents look-ahead bias and other common backtesting mistakes.
- Supports walk-forward and out-of-sample validation.
- Measures risk, portfolio behavior, and realistic execution costs.
- Allows strategies to be developed and compared without rewriting the trading engine.
- Remains inexpensive to operate, preferably using existing hardware + Alpaca rather than an expensive trading SaaS.

The objective is **not** to build a giant quantitative platform or add as many indicators as possible.

The objective is to build a reliable research and execution framework that lets us determine whether a simple swing-trading strategy actually has a repeatable edge.

---

# 1. Starting Point

The existing bot already provides a useful foundation.

Current capabilities include:

- Alpaca integration
- Stock universe scanning
- Price and volume filters
- SMA 20/50/200
- RSI
- MACD
- ATR
- Volume expansion
- Breakout detection
- Bull flag / ascending triangle / cup-and-handle heuristics
- Historical forward-return calculations
- News/catalyst sentiment
- Candidate scoring
- ATR-based stops
- Position sizing
- Maximum-position limits
- Portfolio risk limits
- Daily loss circuit breaker
- Spread / quote-age checks
- Alpaca paper trading
- Bracket orders
- Fill persistence
- Broker reconciliation

This should be **evolved rather than thrown away**.

---

# 2. Core Architectural Change

The most important change is to separate:

1. Market data
2. Strategy
3. Portfolio/risk
4. Execution
5. Backtesting
6. Reporting

Target architecture:

```text
                         Swing Trader
                              |
                 +------------+------------+
                 |                         |
             Research                    Live
                 |                         |
        Historical Data             Alpaca Data
                 |                         |
                 +------------+------------+
                              |
                       Strategy Engine
                              |
                   +----------+----------+
                   |                     |
                Entries                Exits
                   |                     |
                   +----------+----------+
                              |
                         Risk Engine
                              |
                      Portfolio Manager
                              |
                   +----------+----------+
                   |                     |
              Backtest Fill        Alpaca Orders
                 Simulator              |
                   |                     |
                   +----------+----------+
                              |
                         Trade Ledger
                              |
                      Analytics/Reports
```

---

# 3. Phase 1 — Refactor Around Clear Interfaces

Before adding new trading logic, establish the boundaries that will allow research and live trading to share code.

## Core abstractions

Create interfaces/classes conceptually equivalent to:

```python
MarketDataProvider
Strategy
ExecutionEngine
Portfolio
RiskManager
TradeLedger
```

Then provide separate implementations where appropriate:

```text
AlpacaMarketDataProvider
HistoricalMarketDataProvider

BacktestExecutionEngine
AlpacaExecutionEngine
```

The strategy should operate against common market/portfolio abstractions.

### Principle

The strategy should not contain code such as:

```python
if live:
    alpaca.submit_order(...)
```

Instead:

```text
Strategy
   -> Signal
   -> Risk/Portfolio
   -> ExecutionEngine
```

This allows the exact same strategy to run in:

- historical backtests
- paper trading
- live trading

---

# 4. Phase 2 — Build the Event-Driven Backtester

This is the highest-priority feature.

The backtester should simulate the trading day in chronological order.

Conceptually:

```text
Market data arrives
       |
       v
Calculate indicators using only data available at that time
       |
       v
Run strategy
       |
       v
Generate candidate signals
       |
       v
Apply portfolio/risk rules
       |
       v
Generate orders
       |
       v
Simulate fills
       |
       v
Manage stops/targets/trailing exits
       |
       v
Update portfolio
       |
       v
Record trade state
```

The backtester must avoid looking into the future.

## Important rule

At timestamp `T`, the system can only use information that was available at `T`.

This includes:

- OHLCV
- indicators
- news
- earnings/event information
- market regime
- sector data
- portfolio state

---

# 5. Realistic Execution Simulation

The backtester should not assume:

```text
fill_price = close
```

It should support configurable execution assumptions.

## Slippage

Example configurations:

```text
0 bps
5 bps
10 bps
25 bps
```

## Spread

Where appropriate:

```text
Buy  -> ask
Sell -> bid
```

or use a configurable spread model.

## Stops

A stop must handle gap-through behavior.

Example:

```text
Stop = $10.00
Overnight open = $8.00
```

The backtester should not pretend we sold at $10.

The fill should occur around the actual executable price according to the configured model.

## Limit orders

A candle touching a limit price does not necessarily guarantee a fill.

The simulator should have configurable assumptions for limit-order fills.

---

# 6. Preserve the Existing Strategy as Version 1

Do not immediately optimize the existing rules.

First implement the current strategy in the new strategy framework.

This gives us a baseline.

Example:

```text
Strategy: CurrentSwingTraderV1

Inputs:
- price
- volume
- SMA20
- SMA50
- SMA200
- RSI
- MACD
- ATR
- breakout
- pattern
- sentiment
```

Then run the complete historical backtest.

This establishes:

> "This is what the existing bot actually does when tested honestly."

Only after this baseline exists should we begin changing rules.

---

# 7. Replace Arbitrary Scoring With Testable Features

The current score is useful for ranking candidates, but individual points should not automatically be assumed to represent real predictive value.

Instead of:

```text
SMA20 > SMA50 = +15
RSI = +10
MACD = +10
Volume = +10
```

treat these as measurable features.

Examples:

```text
trend_strength
rsi
macd_state
volume_ratio
atr_percent
breakout_distance
relative_strength
sector_strength
market_regime
```

Then test:

- Does each feature improve expectancy?
- Does it provide independent information?
- Does the effect persist out of sample?
- Does the feature interact with other features?

Remove features that don't demonstrate useful predictive value.

---

# 8. Build a Proper Feature Engine

Create a reusable feature layer.

Initial feature categories:

## Trend

- SMA20
- SMA50
- SMA200
- EMA20
- EMA50
- trend slope
- distance from moving averages

## Momentum

- RSI
- MACD
- rate of change
- multi-period returns

## Volatility

- ATR
- ATR percentage
- volatility contraction
- volatility expansion

## Volume

- average volume
- relative volume
- volume acceleration

## Price Structure

- recent high/low
- consolidation range
- breakout distance
- gap size
- distance from support/resistance

## Relative Strength

- stock vs SPY
- stock vs QQQ where appropriate
- stock vs sector ETF

## Sector

- sector return
- sector trend
- sector relative strength

## Market Regime

- SPY trend
- QQQ trend
- market breadth where data is available
- volatility regime
- bull/neutral/bear classification

---

# 9. Add Market Regime Detection

The same technical setup can behave very differently depending on the overall market.

Create a regime layer such as:

```text
BULL
NEUTRAL
BEAR
HIGH_VOLATILITY
```

Initially keep it simple and deterministic.

Example inputs:

- SPY relative to SMA200
- SPY relative to SMA50
- QQQ relative to SMA200
- market momentum
- volatility

Then report strategy performance by regime.

Example:

```text
Bull       +0.31R/trade
Neutral    +0.14R/trade
Bear       -0.18R/trade
```

This will tell us whether the strategy should be:

- always active
- regime filtered
- position-size adjusted by regime
- disabled during certain regimes

---

# 10. Add Relative Strength

Relative strength should become a first-class feature.

Example:

```text
Stock 20-day return:  +14%
SPY 20-day return:     +4%
Sector return:         +8%
```

This tells us the stock is demonstrating leadership.

Measure:

```text
stock_vs_market
stock_vs_sector
```

and test whether stronger relative strength improves trade expectancy.

---

# 11. Add Sector Context

Track sector exposure and sector momentum.

The strategy should eventually know:

```text
Symbol
Sector
Sector ETF
Sector momentum
Sector relative strength
```

This will also feed the portfolio manager.

Example:

```text
NVDA
AMD
AVGO
```

should not necessarily be treated as three completely independent risks.

---

# 12. Add Event Risk Filters

Introduce explicit event-risk handling.

Initial candidates:

- earnings
- major corporate events
- ex-dividend dates
- other known scheduled events where reliable data is available

Examples:

```text
Earnings within 3 trading days
    -> reject new trade
```

or:

```text
Earnings within 5 trading days
    -> reduce position size
```

This should be tested rather than assumed.

---

# 13. Rework Entry Strategies Into Independent Modules

Instead of one giant scoring system, create independent strategy modules.

Initial candidates:

## Strategy A — Breakout

Possible requirements:

```text
Strong trend
+
Consolidation
+
Breakout
+
Volume expansion
+
Relative strength
```

## Strategy B — Pullback

Possible requirements:

```text
Strong trend
+
Controlled pullback
+
Support
+
Momentum reversal
```

## Strategy C — Momentum Continuation

Possible requirements:

```text
Strong relative strength
+
Strong trend
+
Volatility contraction
+
Continuation trigger
```

## Strategy D — Mean Reversion

A separate strategy with fundamentally different assumptions.

Each strategy should be independently testable.

---

# 14. Rework Pattern Detection

Current pattern detectors are useful prototypes but should become measurable market structures.

Avoid relying solely on labels such as:

```text
bull flag
cup and handle
ascending triangle
```

Instead capture the measurable characteristics.

For a breakout, for example:

```text
consolidation_duration
consolidation_range
prior_trend_strength
breakout_magnitude
breakout_volume_ratio
ATR_normalized_breakout
relative_strength
market_regime
```

The goal is to discover which characteristics actually matter.

---

# 15. Rework Exits

The current multiple-target approach should become configurable and testable.

Test alternatives such as:

## Fixed R

```text
Stop = 1R
Target = 2R
```

## ATR target

```text
Stop = 1.5 ATR
Target = 3 ATR
```

## Structure-based

```text
Stop = below swing low
Target = next resistance
```

## Partial + trailing

```text
50% at 1.5R
50% trailing
```

## Time exit

```text
Exit after N trading days
if trade has not reached target
```

Measure every approach.

Do not assume the 1% target is optimal.

In particular, a very small target may be disproportionately affected by:

- spread
- slippage
- overnight gaps
- execution assumptions

---

# 16. Build a Real Portfolio Manager

The current per-trade risk model should evolve into portfolio-level risk management.

Track:

```text
Account equity
Cash
Buying power
Open risk
Sector exposure
Correlation
Number of positions
Maximum position size
Maximum portfolio risk
```

A candidate should compete for capital against other candidates.

Example:

```text
Candidate A: Semiconductor
Candidate B: Semiconductor
Candidate C: Energy
Candidate D: Financials
```

The portfolio manager should understand that A and B may represent similar risk.

---

# 17. Add Correlation-Aware Risk

Eventually position sizing should consider correlation.

A simple first implementation can use:

- recent return correlation
- sector membership
- sector exposure

Later, this can become a more sophisticated portfolio-risk model.

The goal is to avoid accidentally constructing:

```text
10 supposedly separate trades
```

that are really:

```text
one large technology/momentum position
```

---

# 18. Build a Comprehensive Trade Ledger

Every trade should capture the strategy state at entry.

Suggested fields:

```text
trade_id
symbol
timestamp
strategy_id
strategy_version

entry_price
stop_price
target_price
shares

market_regime
sector
trend_features
momentum_features
volatility_features
volume_features
relative_strength
signal_score

spread
ATR
event_risk

exit_price
exit_timestamp
exit_reason
realized_R
realized_PnL
```

This allows us to later answer questions like:

> What did our winners have in common?

and:

> Which features distinguish winners from losers?

---

# 19. Strategy Versioning

Every trade and backtest should identify the exact strategy version/configuration.

Example:

```text
strategy_id = breakout
strategy_version = 1.4
configuration_hash = ...
```

Changing:

```text
RSI 45-70
```

to:

```text
RSI 50-70
```

should create a new strategy/configuration version.

This makes experiments reproducible.

---

# 20. Build the Backtest Report

Every backtest should produce a standard report.

## Return Metrics

- Total return
- CAGR
- Benchmark return
- Alpha

## Risk Metrics

- Maximum drawdown
- Average drawdown
- Volatility
- Downside deviation

## Trade Metrics

- Number of trades
- Win rate
- Average win
- Average loss
- Median win
- Median loss
- Expectancy
- Profit factor
- Average R
- Maximum consecutive losses
- Average holding period

## Risk-Adjusted Metrics

- Sharpe
- Sortino
- Calmar

## Portfolio Metrics

- Exposure
- Turnover
- Sector exposure
- Position concentration

## Regime Metrics

- Bull performance
- Neutral performance
- Bear performance
- High-volatility performance

---

# 21. Benchmark Against Buy-and-Hold

Every backtest should compare against appropriate benchmarks.

At minimum:

```text
Strategy
SPY buy-and-hold
```

Potentially:

```text
QQQ
sector benchmark
```

This prevents a strategy from looking impressive simply because the overall market went up.

---

# 22. Add Walk-Forward Testing

After the normal backtester is working, implement walk-forward validation.

Conceptually:

```text
TRAIN             TEST
2016 ─── 2019     2020

TRAIN             TEST
2017 ─── 2020     2021

TRAIN             TEST
2018 ─── 2021     2022

...
```

The test period must not influence strategy development.

This is one of the primary defenses against overfitting.

---

# 23. Maintain an Untouched Holdout Period

Reserve the most recent historical period as a final holdout.

Do not repeatedly tune against it.

Suggested lifecycle:

```text
Training data
    ↓
Strategy development
    ↓
Validation / walk-forward
    ↓
Frozen holdout
    ↓
Paper trading
    ↓
Live
```

The holdout should only be used for final evaluation.

---

# 24. Add Monte Carlo Analysis

Once a strategy has a meaningful trade history, run Monte Carlo simulations.

Analyze:

- distribution of ending equity
- expected drawdown
- worst-case drawdown
- losing streaks
- probability of losing money
- probability of hitting a drawdown threshold

This helps determine whether the strategy's backtest result is robust or overly dependent on a favorable trade sequence.

---

# 25. Survivorship Bias

Historical testing must eventually account for the fact that today's stock universe is not the same as the historical stock universe.

Avoid assuming:

```text
Today's NYSE/NASDAQ stocks
+
Historical prices
=
Historical market
```

That can exclude stocks that:

- went bankrupt
- were delisted
- were acquired
- collapsed
- changed exchanges

This can materially improve apparent historical performance.

This is a later-stage data-quality task, but it should be explicitly tracked as a limitation.

---

# 26. Experiment Framework

Make strategy experiments cheap.

Example configuration:

```yaml
strategy:
  id: breakout
  version: 1.0

entry:
  breakout_period: 20
  min_volume_ratio: 1.5
  min_rsi: 45
  max_rsi: 70

risk:
  risk_per_trade: 0.01
  max_positions: 5

exit:
  stop_atr: 1.5
  target_r: 2.0
```

Run:

```text
Backtest A
Backtest B
Backtest C
```

and compare them automatically.

The system should make it easy to determine whether a rule actually improves performance.

---

# 27. Do Not Optimize Everything

Avoid brute-force optimization of dozens of parameters.

For example, don't search:

```text
RSI = 41..72
SMA = 17..63
ATR = 0.8..2.4
Volume = 1.1..2.9
```

until something produces a spectacular backtest.

That is a recipe for overfitting.

Prefer:

1. Hypothesis
2. Simple rule
3. Backtest
4. Out-of-sample test
5. Walk-forward
6. Monte Carlo
7. Keep/reject

---

# 28. Separate Research From Live Execution

The research system should be capable of running without Alpaca order execution.

```text
Research
    |
    +--> Historical Data
    +--> Backtester
    +--> Analytics
```

Live:

```text
Live
    |
    +--> Alpaca Data
    +--> Strategy
    +--> Risk
    +--> Alpaca Execution
```

Both use the same strategy implementation.

This is the most important architectural property of the system.

---

# 29. Paper Trading Must Follow Backtesting

Once a strategy survives historical testing:

```text
Backtest
    ↓
Walk-forward
    ↓
Holdout
    ↓
Paper trading
    ↓
Live
```

Do not move directly from:

```text
"Backtest looks good"
```

to:

```text
"Trade real money."
```

Paper trading should validate:

- market-data timing
- signal timing
- order behavior
- fills
- stops
- reconciliation
- execution assumptions
- operational reliability

---

# 30. Live Trading Safety

The existing safeguards should remain and be strengthened.

Required protections:

- Maximum position size
- Maximum portfolio risk
- Maximum open positions
- Maximum daily loss
- Maximum sector exposure
- Duplicate-order prevention
- Broker reconciliation
- Stale-data detection
- Spread checks
- Market-hours checks
- Kill switch
- Order timeout handling
- Persistent trade state

Live execution should fail safely.

---

# 31. Suggested Development Order

## Phase 1 — Baseline

- Refactor interfaces
- Preserve existing strategy
- Implement historical data provider
- Implement event-driven backtester
- Implement simulated execution
- Produce baseline report

**Deliverable:**

> Existing SwingTrader strategy can be backtested honestly.

---

## Phase 2 — Execution Realism

- Slippage
- Spread
- Gap-through stops
- Limit order assumptions
- Configurable commissions/costs
- Order/fill simulation

**Deliverable:**

> Backtest approximates realistic trading conditions.

---

## Phase 3 — Research Framework

- Strategy configuration
- Strategy versioning
- Experiment runner
- Trade ledger
- Performance reports
- Benchmark comparison

**Deliverable:**

> We can rapidly test strategy hypotheses.

---

## Phase 4 — Better Market Context

Add:

- Market regime
- Relative strength
- Sector strength
- Sector exposure
- Event risk

**Deliverable:**

> Strategy understands the environment in which a stock is trading.

---

## Phase 5 — Portfolio Intelligence

Add:

- Portfolio-level risk
- Correlation
- Sector concentration
- Candidate ranking
- Capital allocation

**Deliverable:**

> Multiple simultaneous trades are managed as one portfolio.

---

## Phase 6 — Validation

Add:

- Walk-forward testing
- Out-of-sample testing
- Holdout period
- Monte Carlo analysis
- Robustness tests

**Deliverable:**

> We have evidence that the strategy is not simply overfit.

---

## Phase 7 — Strategy Research

Develop independently testable strategies:

1. Breakout
2. Pullback
3. Momentum continuation
4. Mean reversion

Measure them individually and in combination.

**Deliverable:**

> A small strategy library with evidence for when each strategy works.

---

## Phase 8 — Paper Trading

Run the validated strategy against Alpaca paper trading.

Compare:

```text
Backtest expectation
        vs
Paper results
```

Investigate discrepancies.

**Deliverable:**

> Live market behavior is consistent with the research system.

---

## Phase 9 — Live Trading

Only after successful paper validation:

- Enable small real-money allocation
- Keep strict risk limits
- Continue recording all strategy state
- Compare live vs backtest vs paper performance

## Progress Record (2026-10-05)

- Phase 1 initial baseline: the current technical candidate rules run through an event-driven daily-bar backtester with date-bounded strategy inputs, next-session-open entries, and gap-aware stops.
- Phase 2 initial execution assumptions: 5 bps slippage, configurable full spread, configurable per-share commissions, conservative target trade-through, and stop-first resolution for daily bars touching both exit levels.
- The 2021-01-01 to 2025-12-31 test of `F BAC SOFI SNAP PLTR INTC PFE CCL NU RIVN` lost money at every tested spread assumption and materially lagged SPY. The high win rate did not imply positive expectancy.
- Full cost table, ledger, and assumptions: [data/scans/backtest_2026-10-05_10-06-18_173797.md](data/scans/backtest_2026-10-05_10-06-18_173797.md).
- Backtests now record strategy ID/version and a SHA-256 hash plus exact JSON snapshot of `TradingConfig`; increment the strategy version manually when rules change.
- Standard reports now include annualized return/risk ratios, benchmark return difference, trade win/loss distributions, expectancy, loss streak, holding sessions, gross exposure, turnover, descriptive SPY regime performance, sector exposure, open-position correlation, and signal-time relative strength versus SPY/sector ETF.
- Backtest runs, normalized trade ledgers, daily equity points, regime summaries, sector aggregates, correlation summaries, and relative-strength observations now persist in a dedicated SQLite database; sensitivity report rows carry their run IDs.
- Scanner and backtester now share a market-data provider contract with yfinance and Alpaca adapters; the scanner remains on its existing yfinance cache and the backtester remains on Alpaca.
- Limitations remain: survivorship-biased explicit symbol basket, no historical sentiment, daily-bar fill ambiguity, manual non-point-in-time sector assignments, and no sector/correlation-aware capital allocation.
- The experiment runner is deferred; strategy/config identity remains recorded to preserve reproducibility.
---

## Progress Update (2026-10-18)

- Added pre-backtest daily-bar contract checks for required OHLCV fields, invalid/inconsistent values, duplicate or unordered sessions, and dates outside the requested range.
- Integrated the `pandas-market-calendars` NYSE calendar so missing-bar diagnostics compare returned bars against expected exchange sessions; weekends and market holidays are no longer reported as gaps.
- Genuine missing NYSE sessions are reported as data-gap warnings, including when no bars are returned for an otherwise-open requested range. A range containing only closed sessions does not generate a missing-history warning.
- Added `--holdout-start` to run separate in-sample and untouched later-period simulations. The holdout uses pre-split bars only for causal indicator warmup; its trades, equity, and benchmark return are restricted to the holdout window.
- Holdout and in-sample results for each spread scenario persist as distinct ledger runs. The option does not tune parameters; it evaluates the unchanged strategy and configuration.
- Added `--walk-forward-months` to divide the later period into consecutive out-of-sample windows, each simulated and persisted independently with prior history available only for indicator warmup. This measures temporal consistency; it does not optimize strategy parameters.
- Added a seeded Monte Carlo bootstrap of each scenario's closed-trade dollar P&L, reporting ending-equity distribution, max drawdowns, losing streaks, probability of loss, and probability of a configured drawdown threshold. It is explicitly limited to trade-order robustness and does not model portfolio re-sizing, compounding, overlaps, or dependence.
- Added `--universe-file` CSV support for inclusive symbol membership intervals. Signals and next-session entries are constrained by membership dates; open positions continue to be managed after membership ends.
- Backtest ledgers and reports record the universe source, SHA-256, record count, and full membership snapshot for reproducibility.
- Added a historical data coverage report comparing each symbol's returned bars to expected NYSE sessions during its membership dates, with counts, percentages, missing-symbol indication, and sample missing dates. Gap diagnostics now exclude dates outside each symbol's membership window.
- Added a JSON experiment runner for named `TradingConfig` override variants. Experiments share the downloaded bars and execution assumptions, persist under their names/configuration hashes, and cannot be combined with holdout or walk-forward evaluation.
- The experiment runner performs no search, ranking, or winner selection; its supported workflow is a small, fixed hypothesis comparison on development dates before a separate holdout evaluation.
- Reviewed [Alpaca historical bars](https://docs.alpaca.markets/us/reference/stockbars) and [feed documentation](https://docs.alpaca.markets/us/docs/historical-stock-data-1): historical bars support inactive-symbol queries and literal-symbol mode (`asof="-"`), but the current adapter uses free IEX-only data, so this does not establish full-market volume or delisted-symbol coverage. Alpaca documents IEX as a single exchange and roughly 2.5% of US volume; SIP represents consolidated all-exchange coverage and requires its eligible subscription.
- The Alpaca adapter now requests literal symbols to avoid implicit rename mapping when querying historical universe members; each run's coverage audit remains the actual check for returned data.
- Read-only Alpaca IEX smoke test on 2026-10-18 retrieved bars for four manually selected inactive symbols over their supplied membership windows: TWTR 206/207 sessions (missing 2022-03-08), ATVI 447/447, SIVB 297/297, and BBBY 326/326. The result confirms these example histories can be queried, not complete universe, adjustment, or full-market coverage. No historical bars were saved by the test.
- The user initially selected EODHD for evaluation, then chose to keep the project Alpaca-only. [EODHD's delisted-symbol documentation](https://eodhd.com/financial-apis/delisted-stock-companies-data-2) says the US inactive-symbol list includes common stocks and standard EOD histories remain available, but the documented list fields do not include listing start/end dates. No account or paid plan was started.
- Reconsidered data-source preference on 2026-10-18: the user prefers remaining Alpaca-only. A read-only paper trading asset-catalog query returned 33,577 US-equity assets, 19,168 inactive; none of TWTR, ATVI, SIVB, or BBBY were in the catalog, although their bars were retrievable by direct symbol request. Alpaca's current asset catalog therefore cannot be treated as a complete delisted-symbol source or a source of historic listing intervals.
- Added `--snapshot-alpaca-assets` to capture dated, read-only US-equity asset catalog observations in a dedicated SQLite database (`data/alpaca_assets.sqlite3`). Snapshots are retained append-only with active/inactive status and selected asset attributes; they do not reconstruct pre-snapshot history or count as membership intervals. Backtest survivorship limitations remain.
- Added read-only `--backtest-history [LIMIT]` and `--backtest-run RUN_ID` commands for chronological run comparison and persisted configuration/trade/equity inspection. They query the existing backtest SQLite database only; missing databases are reported without being created. This supports tracking experiments but does not make incomparable runs statistically comparable.
- Added configurable `max_sector_exposure_fraction` (default 60% of configured account size) to enforce a per-sector gross market-value cap in backtest entries and all paper-order paths. Current positions and remaining open buy-order notional count for paper entries. Classification uses the shared existing manual symbol map; unknowns share `Unclassified`, and the map remains incomplete.
- Added a blocking correlation-aware entry guard to backtest and paper entries. It checks absolute Pearson correlation against currently open positions using up to 60 daily returns, at least 30 shared observations, and a configurable 0.85 threshold. The backtest uses history through the signal close; paper mode requests split-adjusted IEX bars through the prior New York date. Insufficient overlap alone is not a block; missing paper histories fail closed. This is descriptive historical correlation, not a predictive risk model.
- Backtest runs now count entries rejected by that guard; sensitivity reports, persisted SQLite run history, and run-detail views expose the count. SQLite schema version 6 adds the count without assigning historical runs any inferred blocks; read-only views show zero for records created before this field existed.
- Backtests now size entries to remaining combined planned stop-risk capacity (using whole shares) instead of rejecting every candidate that exceeds the remaining budget. The risk budget is based on marked account equity capped at configured account size, matching the paper bot's balance convention; reports and SQLite schema version 7 include average/peak planned open-risk fractions. Historical runs without these observations show `N/A`, not fabricated values. Planned stop risk does not guarantee maximum loss because of gaps and execution costs.
- EODHD's [EOD documentation](https://eodhd.com/financial-apis/api-for-historical-data-and-volumes) describes raw OHLC, split-and-dividend-adjusted `adjusted_close`, and split-adjusted volume; that is not a drop-in match for this backtester's split-adjusted OHLC. Its [whole-exchange bulk API](https://eodhd.com/financial-apis/bulk-api-eod-splits-dividends) bills 100 API calls per exchange-date, while standard per-symbol EOD history is one call per symbol; bulk history is not in the free plan. A 1-symbol-per-history approach may be more quota-efficient for an initial sample, but scale and plan entitlements still need checking.
- [EODHD's terms](https://eodhd.com/financial-apis/terms-conditions) permit private non-commercial analysis for non-professional users, prohibit redistribution, and direct uncertain/commercial use to sales. The intended use/license category must be confirmed before adopting the data. No account was created, credentials were requested, or paid access initiated.
- [Norgate's US package comparison](https://norgatedata.com/stockmarketpackages.php) explicitly includes delisted securities and historical index constituents in Platinum/Diamond tiers (US data back to 1990/1950), but its [data-access description](https://norgatedata.com/) says it uses a proprietary Windows database and only historical price data can be exported. It appears stronger for historical data research but less directly compatible with this project's general-market CSV universe workflow; constituent export/integration rights and membership scope need confirmation before purchase.
- Neither an inactive-symbol catalog nor a historical constituent list for selected indices alone is proof of the full, point-in-time NYSE/NASDAQ listing universe needed by this project's broad scanner. Survivorship bias remains unresolved.
- Next: consider deriving forward-only observed status transitions from successive asset snapshots, with gaps and symbol omissions made explicit; keep these separate from exchange-membership intervals and retain survivorship-bias warnings for historical periods before reliable observations.

---

## Progress Update (2026-10-24): Two purposes and paper automation

- Research remains a standalone ranked-grid scanner (`--research`, also the
  default mode); it never executes paper orders. Backtest research is retained.
- The paper bot now synchronizes cumulative partial/full fills with paginated
  broker order history, preserves new entry plans/configurations, records
  account observations and operational events, and exposes a local read-only
  `--paper-performance` report. Legacy fills remain readable.
- Automatic paper recovery cancels stale/partial entries, verifies quantities,
  restores original stop/target protection, renews aging exits, and closes
  verified bot positions when protection cannot be reconstructed/repaired or
  the configured maximum holding period is reached. Unknown/mixed ownership
  fails closed rather than liquidating unrelated positions.
- Recovery requests are persisted before submission and reconciled by client
  ID across restarts. New buys are paused during uncertain broker state.
- Added rotating local operational logs and an optional Windows logon task
  registration script; no task is installed or started automatically.
- This is implementation progress, not broker validation or evidence of trading
  success. Actual paper fills/recovery still require a monitored rollout.
  Fees, external notifications, always-on hosting, and cash-flow-adjusted
  performance are not implemented. The configured profit target is unchanged.

---

# 32. Definition of "Good Enough"

The bot should not be considered successful because it has:

```text
high win rate
```

or:

```text
one impressive backtest
```

A strategy should demonstrate:

- Positive expectancy
- Reasonable profit factor
- Acceptable maximum drawdown
- Positive risk-adjusted returns
- Performance across multiple market regimes
- Robustness to realistic slippage
- Reasonable trade count
- Out-of-sample performance
- Walk-forward stability
- Monte Carlo robustness
- Results better than an appropriate benchmark
- No obvious look-ahead/survivorship bias
- Reproducible results

---

# 33. Cost Philosophy

The project should remain inexpensive.

Preferred stack:

```text
GitHub             $0
Python              $0
Alpaca              $0 initially
Local PC            $0
SQLite              $0
VS Code             $0
```

Only add paid data, compute, or SaaS products when testing demonstrates that they provide enough value to justify the cost.

Avoid building the project around an expensive trading platform unless it provides something we cannot reasonably reproduce.

---

# 34. Guiding Principles

## 1. Evidence over intuition

Every trading rule is a hypothesis until tested.

## 2. Simplicity over complexity

More indicators do not automatically mean more edge.

## 3. Same strategy everywhere

Backtest, paper, and live should use the same strategy code.

## 4. No future information

The backtester must only use information available at the simulated time.

## 5. Risk first

Position sizing and portfolio risk are as important as entry signals.

## 6. Don't optimize into the past

Avoid parameter mining and overfitting.

## 7. Measure the entire portfolio

Individual trade quality does not guarantee portfolio quality.

## 8. Keep the system reproducible

A historical result should be repeatable from the same data and configuration.

## 9. Use the Internet for hypotheses, not authority

Existing strategies, academic research, trading communities, and commercial platforms can provide ideas. They do not establish that a strategy has an edge.

## 10. The bot should earn the right to trade real money

```text
Idea
  ↓
Implementation
  ↓
Backtest
  ↓
Out-of-sample
  ↓
Walk-forward
  ↓
Monte Carlo
  ↓
Paper
  ↓
Small live allocation
  ↓
Scale
```

---

# 35. First Milestone

The first concrete milestone should **not** be a new indicator.

It should be:

> ## "Run the current SwingTrader strategy through a proper event-driven Alpaca-backed backtester and produce a trustworthy baseline report."

Once that exists, every future change can answer:

```text
Did this actually improve the strategy?
```

rather than:

```text
Does this rule sound like it should help?
```

That change—from building a scanner to building a **measurable trading research system**—is the foundation for everything else.
