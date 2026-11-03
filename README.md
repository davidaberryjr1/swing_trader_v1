# Swing Trader

A rules-based swing-trading research scanner and paper-trading bot for liquid
NYSE and NASDAQ stocks priced between $5 and $30.

## Two operating purposes

- **Research:** `python main.py --research` (or simply `python main.py`) produces
  the ranked candidate grid, JSON snapshot, and Markdown report. Research does
  not submit, cancel, or repair orders and needs no Alpaca credentials.
  Historical backtesting remains a separate research tool.
- **Paper bot:** `.\.venv\Scripts\python.exe main.py --paper-bot` scans candidates,
  places paper orders, reconciles broker state, manages recovery, and records
  forward observations. It uses the configured strategy, not backtest results,
  to decide entries. Live-money execution is not supported.

Research and paper operation flags cannot be combined. The research grid remains
available independently of the bot.

## What it does

- **Full Exchange Universe**: Pulls all ~10,000 NYSE and NASDAQ tickers directly from SEC EDGAR and filters for $5–$30 price and >500k volume.
- **Technical Indicator Alignment**: Verifies 20, 50, and 200 SMA alignment, RSI between 45 and 70, and MACD bullish crossovers.
- **Chart Pattern Recognition**: Detects Bull Flags, Ascending Triangles, Cup-and-Handles, and Resistance Breakouts.
- **Statistical Forward Probabilities**: Calculates historical probability of gaining $\ge 10\%$ vs. losing $> 5\%$ over the next 10 trading days.
- **Catalyst & 72h Sentiment Analysis**: Ingests recent headlines to identify primary catalysts (Earnings/Guidance, Analyst Ratings, Insider Flow, Corporate Events).
- **Position & Risk Sizing**: Calculates Entry, ATR-based Stop Loss, Target 1 (+8–10%), Target 2 (+16–20%), share sizing, and Risk Rating.
- **Probability Ranking**: Ranks candidates from highest probability of +10% gain to lowest.

## Setup

Windows PowerShell:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

For Alpaca paper trading, copy `config.example.py` to `config.py` in the project
root and fill in `APCA_API_KEY_ID` and `APCA_API_SECRET_KEY` with your **paper**
account credentials. The example also lists the scanner, sizing, sentiment, and
paper quote limits in `TRADING_CONFIG`; edit them in your local copy to change
the defaults. `--min-price` and `--max-price` override the local price settings
for a run. The local `config.py` is ignored by Git; do not commit or paste your
keys. A regular `python main.py` scan does not need credentials or a config file.

## Alpaca Paper Workflow

Paper modes require `alpaca-py` from `requirements.txt` and a saved, ignored
root `config.py` with **paper** keys. Use these commands during regular US
market hours for preview or entry:

```powershell
python main.py --paper-preview     # Scan and check the top candidate; no order
python main.py --paper-trade       # Submit one paper limit-entry bracket order
python main.py --paper-status      # Read-only account/order inspection; no scan
python main.py --paper-reconcile   # Request cancellation of timed-out, unfilled bot entries
```

The last two commands work outside market hours. Reconciliation requests
cancellation after ten minutes only for this bot's bracket entries with zero
filled shares; verify the broker accepted the cancellation. Partial fills and
missing exit legs require manual review in Alpaca. Validate a real paper fill and its exits in the
Alpaca dashboard before relying on `--paper-trade`; neither tests nor a preview
prove broker execution, and stops cannot guarantee the planned loss.

## Paper Bot (Supervised rollout before unattended use)

After verifying the one-shot paper workflow in Alpaca, start the paper-only bot
from the project directory on Windows with
`.\.venv\Scripts\python.exe main.py --paper-bot`. Use that explicit interpreter
if your terminal has not activated the virtual environment; a plain `python` may
point to another installation without `alpaca-py`. Keep the process running;
stop it with Ctrl+C. `.\.venv\Scripts\python.exe main.py --paper-bot-once` performs one cycle
and exits (it **can place paper orders**). Both modes require paper credentials.
Only one bot instance can run at a time. Outside regular US market hours the bot
reconciles broker state but does not place entries. Between 10:00 and 15:30 New
York time, it scans every 15 minutes while a position slot and planned-risk
budget remain available, and checks broker state every thirty seconds by default.
It skips any symbol already attempted by the paper bot that trading day, even
if its entry was canceled or its position has closed. A scan does not guarantee
that a candidate or an order fill will be available. Broker polling defaults to
30 seconds; a previously configured `bot_poll_seconds` overrides this.

Before every entry, the bot fetches fresh Alpaca quotes, positions and orders.
It enforces a five-position limit (including pending entries), 2% daily loss
pause, 10% combined planned downside, the existing per-trade risk setting
(1% by default, never over 2% in bot mode), and no more invested or reserved
than `account_size`. All three bot limits, `bot_poll_seconds`, and
`bot_scan_interval_seconds` are configurable in `TRADING_CONFIG`. Daily loss uses
Alpaca account equity versus previous-day
equity and includes other account activity; the bot does not liquidate existing
positions when the daily limit is reached. With $1,000 sizing, the default
per-trade risk is $10, daily pause is $20, and combined planned downside is
at most $100. Stops can slip beyond those planned limits.

The bot stores broker-reported cumulative fills, including partial buys and
sells, idempotently in `data/paper_fills.sqlite3`. Increasing filled quantities
update the same order rather than double-counting it. Existing fill databases
are retained and extended automatically. The database also stores new entry
plans with strategy/configuration/rationale, recovery requests, account equity
observations, the last successful synchronization, and operational events.
The journal binds to the first synchronized account and refuses a different
account afterward. Legacy records cannot retroactively establish account
identity or recover missing entry rationale.

With `bot_auto_recover=True` (default), the paper bot:

- Cancels stale unfilled entries and the unfilled remainder of partial entries.
- Verifies bot ownership and quantities before changing exits. Unknown positions,
  mixed/unmatched quantities, and unrelated working orders block automatic
  recovery and new entries; they are never automatically liquidated.
- Repairs missing/invalid protection using an OCO sell order at the original
  stop and target, without recalculating the strategy's target.
- Renews protection five days before a reported expiry, or after 80 days when
  the broker supplies no expiry. Cancellation must be observed before replacement.
- Closes verified bot exposure during regular market hours if original protection
  cannot be reconstructed, a repair is rejected, or the position reaches
  `bot_max_hold_days` (180 by default). It does not cancel a working emergency close.
- Cancels orphaned bot exits after confirmed flat positions and balanced fills.
- Uses persisted recovery client IDs to reconcile uncertain acknowledgements
  across restarts instead of submitting a new independent sell.

Recovery blocks all new entries until a later broker snapshot confirms safe
state. Broker/API failures, quantity disagreements, pending cancellations, and
unpageable equal-timestamp order boundaries fail closed. Order history is
paginated, rather than stopping at 500 orders. Recovery is not atomic: cancellation
and replacement can leave a protection gap, and market exits can slip. Outside
regular hours the bot defers protective-order replacement/market liquidation;
stale entry cancellations can still be requested. An outage or closed market
can prevent recovery. Prefer a dedicated paper account without manual trades.

Set `bot_auto_recover=False` to retain manual-review behavior. The one-shot
`--paper-trade` command does not run the ongoing recovery loop; use the bot for
continuous management. Both entry paths reserve a local daily attempt before
submission, so an uncertain buy acknowledgement is not retried that day.

Inspect forward results without credentials or broker access:

```powershell
.\.venv\Scripts\python.exe main.py --paper-performance
```

The report includes remaining quantities, realized gross P&L/returns, closed-trade
holding days and exit types where known, win rate, average win/loss, expectancy,
profit factor, last synchronization, recent events, and last-observed open
positions. It also reports account-wide equity and sampled drawdown, explicitly
separate from bot-only trade P&L. Fees, deposits/withdrawals, and intracycle equity
moves are not reconstructed; these are not fee-adjusted or time-weighted returns.
Partial-fill holding timestamps can be cumulative observation times. A local
report is not a fresh broker query or proof of profitability.

Operational exceptions and cycle heartbeats go to rotating `data/paper_bot.log`
files; recovery events are also queryable through the performance report.
These are local alerts, not email/push notifications. Errors retry on subsequent
cycles, but ambiguous states deliberately remain paused rather than guessing.
Regularly check health during initial rollout; tests do not establish actual
broker execution.

### Optional Windows startup and process restart

After a monitored paper entry/exit and recovery check, register an opt-in Windows
Scheduled Task from PowerShell:

```powershell
.\scripts\Register-PaperBot.ps1
```

The script registers, but does not start, a task under the current logged-on user.
It uses the repository's virtual environment and working directory, starts at
future logons, prevents overlapping task instances, and requests process restarts
after failures. Existing tasks are not overwritten. Start paper trading explicitly:

```powershell
Start-ScheduledTask -TaskName SwingTraderPaperBot
```

The PC must remain powered on, awake, connected, and logged in. No service,
remote health monitor, or automatic wake configuration is installed.
`Disable-ScheduledTask` prevents future starts; `Stop-ScheduledTask` stops the
process but does not cancel broker-held orders or liquidate positions.

Paper orders use the configured `target_percent` (1% by default) plus an
`estimated_exit_cost_fraction` allowance (0.1% by default), calculated against
sale proceeds from the buy limit price and rounded up to the next cent. The default quote-spread
limit is 0.2% of midpoint. These are conservative planning assumptions, not
measured trading costs or a guarantee of 1% realized net profit; calculate
actual net results from broker fills and fees. Alpaca's GTC orders are subject
to an aged-order cancellation policy after 90 days, so this workflow cannot
leave a 180-day position unattended without a functioning renewal/recovery loop.

## Run scanner

To scan the entire NYSE and NASDAQ universe ($5–$30, >500k volume) and return the Top 25 candidates:

```powershell
python main.py
```

To scan specific tickers or adjust parameters:

```powershell
python main.py --symbols SOFI PLTR AGRO ABUS AES
python main.py --top 10 --min-price 5 --max-price 30
```

Each run prints a Markdown candidate table, saves a structured snapshot under
`data/scans/scan_YYYY-MM-DD.json`, and creates a new Markdown report under
`data/scans/scan_YYYY-MM-DD_HH-MM-SS_microseconds.md`. Open the report in VS Code's
Markdown preview to see the rendered table. Reports include scan progress, candidates
(or the no-candidates message), and total runtime. Repeated runs never overwrite an
earlier report.

Daily OHLCV histories are cached under `data/cache/ohlcv`. Repeated scans on the
same day reuse the cache; later scans fetch recent bars and merge them into the
stored one-year history. The CLI prints total application runtime when it finishes.
Technical signals remain based on completed daily candles. After qualification,
candidate entries, stops, targets, and share counts are refreshed in batches using
the latest available one-minute trade, including pre-market and after-hours bars.
Scanner and backtest data now share a provider interface; the scanner still uses
its cached yfinance adapter, while historical backtests use Alpaca IEX bars.

## Historical Backtest (Initial Baseline)

The first event-driven baseline uses Alpaca split-adjusted IEX daily bars and the
existing technical candidate logic. Specify the tested symbols and date range; the
run saves a Markdown report under `data/scans` and persists each spread scenario to
`data/backtests.sqlite3`:

```powershell
python main.py --backtest --symbols AAPL MSFT NVDA --start 2023-01-01 --end 2025-01-01 `
    --slippage-bps 5 --spread-scenarios-bps 0 5 10 25 --commission-per-share 0.005
```

Before simulation, the run checks each returned history for required OHLCV fields,
duplicate or unordered sessions, invalid bars, and dates outside the requested
range. Missing bars are checked against the NYSE exchange calendar, so holidays and
weekends are excluded and missing open sessions are reported as data-gap warnings.
These warnings do not by themselves fail the run. The calendar integration uses
`pandas-market-calendars` (`NYSE`) and treats its trading dates as the expected
sessions for this US-equity backtest; it does not account for symbol-specific
halts or exchange-specific exceptions.

Backtest market data uses the Alpaca credentials in the ignored root `config.py`.
Signals are evaluated after each completed close, entries occur no earlier than
the next session's open, and stop gaps fill at the open with configured slippage.
When a daily candle touches both stop and target, the simulator assumes the stop
happened first. `--spread-bps` models the full quoted spread, applying half on
each market-side fill. Target limit orders default to requiring a 5-basis-point
trade-through plus half the spread before assuming a fill; target fills remain at
the limit price. `--target-fill-mode touch` runs the more optimistic touch
assumption. `--spread-scenarios-bps` compares multiple spread assumptions using
the same downloaded bars. Slippage defaults to 5 basis points per market-side
fill; `--commission-per-share` is charged on both entry and exit (default $0).
The report compares price return against SPY.
The SQLite database has `backtest_runs`, `backtest_trades`, and `backtest_equity`
tables. Each report row contains the corresponding run ID, making it possible to
retrieve the exact run and its ledger; the existing paper-fill database is separate.
Review recent runs with `python main.py --backtest-history` (optionally pass a
row limit), or inspect a specific run, its saved configuration, closed trades,
and equity-curve span with `python main.py --backtest-run RUN_ID`. These views
read the database only and do not request market data. History is chronological,
not a performance ranking; compare runs only when period, symbols, data coverage,
configuration, and execution assumptions are appropriate to compare.

The default `max_sector_exposure_fraction=0.60` caps gross market value in each
sector at 60% of configured `account_size` for both backtest entries and paper
orders. Open paper positions and remaining open buy orders count toward the
paper cap; a pending buy without a verifiable limit price prevents a new order
in the same sector. Backtests use the current session's open price (or the last
available close when that symbol has no bar that session) for existing positions.
The current sector assignments are a small, manually maintained map; symbols
without a mapping all count in one `Unclassified` bucket. This cap is a gross
exposure guard, not a guarantee of independent sector risk.

A second entry guard blocks a new backtest or paper position when its absolute
Pearson correlation with any currently open position reaches the configurable
`correlation_absolute_threshold` (default 0.85), using up to
`correlation_lookback_sessions` daily returns (default 60) and requiring at least
`correlation_min_observations` overlapping returns (default 30). Negative
correlation is treated by magnitude as well. Backtests compare only bars through
the signal close, before next-session entry; paper checks request split-adjusted
IEX daily bars only through the previous New York calendar date. A pair with too
few shared observations is not used to block; missing requested paper history
fails closed. Correlation is a historical descriptive measure, not a forecast or
guarantee of diversification.

For a date-effective universe, provide a CSV instead of `--symbols`:

```csv
symbol,start_date,end_date
ABC,2018-01-02,2020-06-30
ABC,2021-02-15,
XYZ,2019-04-01,2024-12-31
```

Dates are inclusive; a blank `end_date` means membership continues indefinitely.
Intervals for the same symbol must not overlap. Run it with:

```powershell
python main.py --backtest --universe-file data/historical_members.csv --start 2018-01-01 --end 2025-01-01
```

The simulator evaluates and enters a symbol only while it is a member; an open
position can still be managed and exited after membership ends. Each ledger run
records the source path, SHA-256, record count, and exact membership snapshot.
The backtest report also compares returned bars with expected NYSE sessions during
each member's active intervals, including symbol-level missing-session counts and
example dates. Sessions outside membership are excluded from both gap warnings
and the coverage denominator.
The Alpaca adapter requests literal symbols with `asof="-"` so Alpaca's current
symbol-rename mapping does not silently merge an old ticker's bars into another
symbol. This still does not guarantee bars for every inactive/delisted security:
the report's coverage counts must be reviewed for every run.
See Alpaca's [historical bars API](https://docs.alpaca.markets/us/reference/stockbars)
for `asof` symbol mapping and [historical data documentation](https://docs.alpaca.markets/us/docs/historical-stock-data-1)
for feed coverage details.

### Inactive-symbol coverage smoke test (2026-10-18)

Using this configured Alpaca account's IEX feed, a read-only historical-bars
request with literal-symbol mode returned 1,276 of 1,277 expected NYSE sessions
for four manually selected inactive symbols over their supplied membership
intervals: TWTR 206/207, ATVI 447/447, SIVB 297/297, and BBBY 326/326. TWTR was
missing 2022-03-08. This is a small API-availability smoke test, not proof of a
complete delisted universe, price correctness, or full-market volume coverage.
The manually supplied membership cutoffs and NYSE-session denominators should be
independently validated before using this basket to make strategy claims.

### Alpaca asset-catalog check (2026-10-18)

A read-only query of the Alpaca paper trading asset endpoint returned 33,577
US-equity assets, including 19,168 marked inactive. The catalog did not contain
TWTR, ATVI, SIVB, or BBBY, despite the historical-bars endpoint returning data
for those symbols when requested directly. The asset endpoint is therefore not a
complete historical delisted-symbol catalog, and it cannot supply past listing
intervals. Alpaca remains usable for price data and an asset-status snapshot,
but a catalog-only backtest universe would omit some known historical symbols.
Recording asset snapshots going forward could preserve future status changes;
it cannot reconstruct membership before snapshot collection began.

To begin keeping those observations, use the explicit read-only command:

```powershell
python main.py --snapshot-alpaca-assets
```

It saves each retrieved US-equity catalog and retrieval timestamp to
`data/alpaca_assets.sqlite3` (`asset_snapshots` and `asset_observations` tables).
Repeated runs are retained as separate snapshots; the API's current active and
inactive states are observations, not effective-dated exchange membership and
must not be used as historical membership intervals. This command reads the
asset catalog only and does not place or modify orders.

This is a mechanism to use a properly sourced point-in-time universe, not a source
of constituent data: the CSV must include delisted, acquired, failed, and otherwise
removed securities to reduce survivorship bias. Alpaca may not provide historical
bars for every removed security, and manual membership data can have its own
coverage or classification bias.

For a chronological holdout check, add `--holdout-start` to split the requested
range into independent in-sample and out-of-sample simulations:

```powershell
python main.py --backtest --symbols AAPL MSFT NVDA --start 2022-01-01 --holdout-start 2024-01-01 --end 2025-01-01 `
    --slippage-bps 5 --spread-scenarios-bps 0 5 10 25 --commission-per-share 0.005
```

The holdout simulation uses history before the split for indicator warmup, but does
not trade or include those sessions in its reported equity curve. Each period
starts with a fresh account and is persisted as a separate set of runs. This command
does not optimize or select parameters; it evaluates the unchanged strategy and
configuration on the later period. Choose a `--start` far enough ahead of the
holdout to provide the strategy's required indicator warmup. Treat the holdout as
untouched: do not use it to tune rules and continue calling the same period
"out-of-sample" after inspecting and changing the strategy.

For multiple consecutive out-of-sample windows after the development period, add
`--walk-forward-months`:

```powershell
python main.py --backtest --symbols AAPL MSFT NVDA --start 2021-01-01 --holdout-start 2023-01-01 --walk-forward-months 6 --end 2025-01-01 `
    --slippage-bps 5 --spread-scenarios-bps 0 5 10 25 --commission-per-share 0.005
```

Each window starts with a fresh account, uses only prior bars for causal indicator
warmup, and is persisted independently. Windows are consecutive and the final one
may be shorter. This measures temporal consistency of the fixed strategy, not
walk-forward parameter optimization; do not treat the separate results as one
continuously invested portfolio.

Each backtest report also includes a seeded Monte Carlo trade-sequence bootstrap.
It resamples closed-trade dollar P&L with replacement and reports ending-equity
percentiles, drawdowns, losing streaks, and probabilities of loss or crossing the
configured drawdown threshold:

```powershell
python main.py --backtest --symbols AAPL MSFT NVDA --start 2022-01-01 --end 2025-01-01 `
    --monte-carlo-simulations 5000 --monte-carlo-drawdown-threshold -0.20
```

This is an exploratory trade-order robustness check, not a portfolio-path
simulation: it does not model overlapping trades, capital reallocation, dynamic
sizing, compounding, or dependence between trades. The seed is fixed at 42 for
reproducibility.

To compare a small, hypothesis-driven set of configuration variants on the same
development-period bars, create a JSON file:

```json
{
  "experiments": [
    {"name": "baseline", "overrides": {}},
    {"name": "wider-target", "overrides": {"target_percent": 2.0}}
  ]
}
```

Run the comparison with `--experiment-file` instead of a holdout or walk-forward
split:

```powershell
python main.py --backtest --symbols AAPL MSFT NVDA --start 2022-01-01 --end 2023-12-31 `
    --experiment-file data/experiments.json
```

Each experiment inherits the configured `TradingConfig`, applies only named
`TradingConfig` field overrides, and gets its own report section and persisted
ledger runs. Experiment names are saved in SQLite and the resulting configuration
hash is recorded. Unknown fields, duplicate names, and values with the wrong JSON
type are rejected. The runner does not search parameters, rank or select a winner,
and cannot be combined with `--holdout-start` or `--walk-forward-months`; reserve
later periods for evaluation after the hypothesis is fixed.

This is a first baseline, not yet a full validation framework: historical news is
disabled, the explicit symbol list does not reconstruct historical constituents,
survivorship bias remains, and shared capital allocation, sector, and correlation
risk are not yet modeled.
The existing strategy's feature calculations are called on bars ending at each
signal date, preventing later bars from entering earlier decisions.

### Recorded Baseline (2026-10-05)

Basket: `F BAC SOFI SNAP PLTR INTC PFE CCL NU RIVN`, 2021-01-01 through
2025-12-31. The 1% target exit was held fixed. All scenarios used 5 bps slippage
per market-side fill and $0.005/share commission per side; only full-spread
assumption varied:

| Full spread | Ending equity | Return | Max drawdown | Trades | Win rate | Avg R | Profit factor |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 0 bps | $675.33 | -32.47% | -33.23% | 1,260 | 82.54% | -0.04 | 0.76 |
| 5 bps | $599.51 | -40.05% | -40.34% | 1,232 | 81.66% | -0.06 | 0.70 |
| 10 bps | $595.18 | -40.48% | -40.71% | 1,226 | 81.65% | -0.06 | 0.70 |
| 25 bps | $496.98 | -50.30% | -50.55% | 1,177 | 79.86% | -0.08 | 0.61 |

SPY price return over the same period was +81.67%. These figures are a
survivor-biased sample, not evidence of general strategy performance; no
historical constituents or headlines were used. See the complete assumptions and
ledger in [the sensitivity report](data/scans/backtest_2026-10-05_10-06-18_173797.md).
New sensitivity reports include a strategy ID/version, a SHA-256 fingerprint,
and the exact trading configuration snapshot. They also include CAGR, simple
return difference versus SPY, annualized volatility and risk ratios (252 sessions
per year, zero risk-free rate), trade win/loss distributions, net expectancy,
loss streak, average holding sessions, gross exposure, and turnover. Exposure is
daily gross position value divided by marked equity, averaged over the test;
turnover is total entry-plus-exit notional divided by average daily equity (not
annualized). CAGR uses elapsed calendar days; benchmark difference is not
beta-adjusted alpha. Trades are grouped by SPY regime at the signal close: BULL
uses close > SMA50 > SMA200, BEAR uses close < SMA50 < SMA200, HIGH_VOLATILITY
uses annualized 20-session realized volatility >= 30%, and other fully observed
dates are NEUTRAL. Insufficient SPY history is UNKNOWN. Regimes are descriptive
only and never filter trades. Increment the strategy version in
`swingtrader/scanner.py` whenever the strategy rules change.
Reports also break down average/peak portfolio exposure by sector and summarize
pairwise correlations among open positions. Correlations use up to 60 daily
returns through each session close and require 20 common observations. The sector
map is explicit and present-day, not a point-in-time industry database. These
summary metrics do not change share sizing. A separate entry guard blocks backtest
and paper entries when absolute correlation to an open position meets the
configured threshold; backtest reports and persisted run history include the
number of entries blocked by this guard.
The combined-risk limit (10% by default) caps planned stop-loss risk across open
trades. Backtests
reduce a new trade's whole-share size to fit the remaining risk budget, while the
paper bot uses the same configured fraction when sizing orders. Reports show the
average and peak planned open risk as a fraction of marked equity; this is an
estimate, not a guarantee against gaps or slippage.
Each trade also records its 20-session stock return, SPY return, mapped sector ETF
return, and stock-minus-benchmark relative strength at the signal close. ETF history
that is unavailable or too short is reported as `N/A`; no future values are filled
in, and these measurements do not change candidate ranking or execution.

## Sentiment & Catalyst Classification

For candidates that pass the technical rules, the scanner fetches headlines within the last 72 hours and scores them with VADER. It also classifies the primary catalyst (e.g. Earnings/Guidance, Analyst Action, Insider/Institutional Flow, Corporate Events). Clearly positive news adds 5 points to the candidate score; clearly negative news subtracts 10 points.

Sentiment is a minor ranking adjustment, not an entry signal. Never let sentiment override technical structure, liquidity, or risk rules.

## Assumptions & Risk Management

- Account size: $1,000
- Risk per trade: 1% ($10)
- Stock price range: $5 to $30 (configurable via `--min-price` / `--max-price`)
- Profit targets: +1% quick target, Target 1 (+8–10%), Target 2 (+16–20%)
- Maximum position value: 40% of account ($400)
- Long-only
- Daily bars
- Signals and entries are evaluated using the latest completed adjusted daily close

## Reading a trade plan

For a long trade, `entry` is the intended buy price. `stop` is the planned loss
exit, and `target` is the planned profit exit. After a buy fills, use a one-cancels-
other (OCO) bracket: a sell stop at the stop price and a sell limit at the target
price. If either exit fills, the broker cancels the other exit.

The displayed risk is planned rather than guaranteed: a stop order can fill below
its stop price during a fast move or overnight gap. Re-check the market price before
placing an order; a stale entry, stop, or target may no longer be appropriate.

## Position limits

The top-15 scan output is a ranked watchlist, not 15 simultaneous orders. Each
candidate is independently sized as if it were the only position in a $1,000
account, so adding their position values can exceed $1,000. The current application
does not yet allocate capital across a shared multi-position portfolio.

While learning, choose only a small number of the strongest setups and keep the
combined position value within available cash. Keep combined planned risk within a
limit you decide before trading; the per-trade $10 risk limit does not automatically
cap risk across multiple open positions.

## Suggested workflow

1. Run the daily scan and review the saved candidates.
2. Use a broad liquid watchlist and active-movers scan to find additional symbols,
   then run them with `python main.py scan --symbols ...`.
3. Verify each candidate's chart, liquidity, bid/ask spread, and upcoming events.
4. Paper trade the proposed entry, stop, target, and share count with an OCO bracket.
5. Keep a trade journal before considering real-money use.

The scanner is a research tool, not an automatic trading system. Its output is not
financial advice.
