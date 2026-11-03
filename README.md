# Swing Trader

A rules-based swing-trading scanner for liquid NYSE and NASDAQ stocks priced between $5 and $30.

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
keys. Running `python main.py` without arguments opens the GUI. Add `--cli` to
run a command-line scan, which does not need credentials or a config file.

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

## Paper Bot (Experimental)

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
budget remain available, and checks broker state every five minutes by default.
It skips any symbol already attempted by the paper bot that trading day, even
if its entry was canceled or its position has closed. A scan does not guarantee
that a candidate or an order fill will be available.

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

The bot stores broker-reported fills, including buys and sells, idempotently
in `data/paper_fills.sqlite3`. This is a gross fill ledger, not a fee-adjusted
net-return report. Unknown positions, partial fills, missing exit legs, pending
cancellations, order-history overflow and approaching GTC expiry stop new buys;
check Alpaca and repair those situations manually. The bot does **not** yet
renew a bracket before Alpaca's 90-day GTC cancellation or automatically close
a position at 180 days. Do not leave it unmonitored for long holds or treat this
as proof of a profitable strategy.

Paper orders use the configured `target_percent` (1% by default) plus an
`estimated_exit_cost_fraction` allowance (0.1% by default), calculated against
sale proceeds from the buy limit price and rounded up to the next cent. The default quote-spread
limit is 0.2% of midpoint. These are conservative planning assumptions, not
measured trading costs or a guarantee of 1% realized net profit; calculate
actual net results from broker fills and fees. Alpaca's GTC orders are subject
to an aged-order cancellation policy after 90 days, so this workflow cannot
leave a 180-day position unattended without checking and renewing its exits.

## Run scanner

Launch the Kivy interface and run scans from its **Run Scan** button:

```powershell
python main.py
```

The ranked candidates populate the matching table fields when the scan finishes.
The interface supports the scanner options `--symbols`, `--top`, `--min-price`,
`--max-price`, and `--output`.
Completed GUI scans are saved as timestamped JSON files under
`data/scans/gui_runs`; the app restores the most recent saved results on launch.
Selecting a ranked symbol opens its info window with a one-year daily closing-price
graph from the local OHLCV cache in the upper-right quarter, with quarter-dollar
price ticks on the right, three-letter month labels along the bottom, and a white
200-day simple moving average across the full displayed year. Use the up and down
arrow keys in the info window to view the previous or next ranked symbol;
navigation stops at the first and last results.

To scan the entire NYSE and NASDAQ universe ($5–$30, >500k volume) and return the Top 25 candidates:

```powershell
python main.py --cli
```

To scan specific tickers or adjust parameters:

```powershell
python main.py --cli --symbols SOFI PLTR AGRO ABUS AES
python main.py --cli --top 10 --min-price 5 --max-price 30
```

Each run prints a Markdown candidate table, saves a structured snapshot under
`data/scans/scan_YYYY-MM-DD.json`, and creates a new Markdown report under
`data/scans/scan_YYYY-MM-DD_HH-MM-SS_microseconds.md`. Open the report in VS Code's
Markdown preview to see the rendered table. Reports include scan progress, candidates
(or the no-candidates message), and total runtime. Repeated runs never overwrite an
earlier report.

Daily OHLCV histories are cached under `data/cache/ohlcv`. The one-year scan cache
retains up to two years of daily bars so the chart can calculate 199 warm-up
sessions before the displayed year; scanner analysis still uses only the latest
year. Initial downloads fetch two years, and older one-year caches are backfilled
when they lack enough SMA history. Repeated scans on the same day reuse the cache;
later scans fetch from the day after each symbol's latest cached bar through the
current date and merge available trading-day bars. The CLI prints total application
runtime when it finishes.
Technical signals remain based on completed daily candles. After qualification,
candidate entries, stops, targets, and share counts are refreshed in batches using
the latest available one-minute trade, including pre-market and after-hours bars.

## Historical Backtest (Initial Baseline)

The first event-driven baseline uses Alpaca split-adjusted IEX daily bars and the
existing technical candidate logic. Specify the tested symbols and date range; the
run saves a Markdown report under `data/scans` and persists each spread scenario to
`data/backtests.sqlite3`:

```powershell
python main.py --backtest --symbols AAPL MSFT NVDA --start 2023-01-01 --end 2025-01-01 `
    --slippage-bps 5 --spread-scenarios-bps 0 5 10 25 --commission-per-share 0.005
```

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
map is explicit and present-day, not a point-in-time industry database; correlation
and sector outputs are descriptive only and do not alter position sizing.
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
