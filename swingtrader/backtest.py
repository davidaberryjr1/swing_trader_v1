from dataclasses import asdict, dataclass, replace
from datetime import date
import hashlib
import json
import math
from typing import Callable

import numpy as np
import pandas as pd

from .config import TradingConfig
from .correlations import highest_absolute_correlation, validate_correlation_config
from .scanner import (
    STRATEGY_ID,
    STRATEGY_VERSION,
    TradeCandidate,
    evaluate_candidate,
    refresh_candidate_price,
)
from .sectors import SECTOR_BY_SYMBOL, SECTOR_ETF_BY_SECTOR, sector_for_symbol


@dataclass
class BacktestTrade:
    strategy_id: str
    strategy_version: str
    configuration_hash: str
    symbol: str
    sector: str
    sector_etf: str | None
    market_regime: str
    stock_return_20d: float | None
    market_return_20d: float | None
    sector_return_20d: float | None
    relative_strength_vs_market: float | None
    relative_strength_vs_sector: float | None
    entry_date: str
    exit_date: str
    entry_price: float
    exit_price: float
    shares: int
    holding_sessions: int
    stop_price: float
    target_price: float
    exit_reason: str
    realized_pnl: float
    realized_r: float
    strategy_score: int
    setup: str


@dataclass
class BacktestResult:
    trades: list[BacktestTrade]
    equity_curve: pd.Series
    starting_equity: float
    ending_equity: float
    benchmark_return: float | None
    max_drawdown: float
    win_rate: float
    average_r: float
    profit_factor: float
    strategy_id: str
    strategy_version: str
    configuration_hash: str
    configuration: dict
    cagr: float | None
    benchmark_alpha: float | None
    annualized_volatility: float
    sharpe_ratio: float | None
    sortino_ratio: float | None
    calmar_ratio: float | None
    average_win: float | None
    average_loss: float | None
    median_win: float | None
    median_loss: float | None
    expectancy_dollars: float
    max_consecutive_losses: int
    average_holding_sessions: float
    average_gross_exposure: float
    turnover: float
    average_open_risk_fraction: float
    peak_open_risk_fraction: float
    market_regime_metrics: dict[str, dict[str, float | int]]
    sector_mapping: dict[str, str]
    sector_etf_mapping: dict[str, str]
    sector_exposure_metrics: dict[str, dict[str, float]]
    average_pairwise_correlation: float | None
    max_pairwise_correlation: float | None
    correlation_blocked_entries: int
    average_relative_strength_vs_market: float | None
    average_relative_strength_vs_sector: float | None


@dataclass
class MonteCarloSummary:
    simulations: int
    trades_per_path: int
    mean_ending_equity: float
    median_ending_equity: float
    fifth_percentile_ending_equity: float
    ninety_fifth_percentile_ending_equity: float
    median_max_drawdown: float
    fifth_percentile_max_drawdown: float
    worst_max_drawdown: float
    ninety_fifth_percentile_losing_streak: float
    probability_of_loss: float
    probability_of_drawdown_threshold: float


@dataclass
class _Position:
    candidate: TradeCandidate
    entry_date: pd.Timestamp
    entry_price: float
    shares: int
    stop_price: float
    target_price: float
    initial_risk: float
    sector: str
    sector_etf: str | None
    market_regime: str
    stock_return_20d: float | None
    market_return_20d: float | None
    sector_return_20d: float | None
    relative_strength_vs_market: float | None
    relative_strength_vs_sector: float | None


@dataclass
class _Signal:
    candidate: TradeCandidate
    signal_date: pd.Timestamp
    market_regime: str
    sector: str
    sector_etf: str | None
    stock_return_20d: float | None
    market_return_20d: float | None
    sector_return_20d: float | None
    relative_strength_vs_market: float | None
    relative_strength_vs_sector: float | None


def _historical_strategy(symbol: str, bars: pd.DataFrame, cfg: TradingConfig):
    return evaluate_candidate(symbol, bars, cfg)


def configuration_hash(cfg: TradingConfig) -> str:
    payload = json.dumps(asdict(cfg), sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_walk_forward_windows(
    first_test_start: str,
    end: str,
    window_months: int,
) -> list[tuple[str, str]]:
    if window_months < 1:
        raise ValueError("Walk-forward window length must be at least one month")
    first = pd.Timestamp(date.fromisoformat(first_test_start))
    last = pd.Timestamp(date.fromisoformat(end))
    if first > last:
        raise ValueError("Walk-forward start date cannot be later than end date")

    windows = []
    window_start = first
    while window_start <= last:
        next_start = window_start + pd.DateOffset(months=window_months)
        window_end = min(next_start - pd.Timedelta(days=1), last)
        windows.append((window_start.date().isoformat(), window_end.date().isoformat()))
        window_start = window_end + pd.Timedelta(days=1)
    return windows


def monte_carlo_trade_bootstrap(
    trades: list[BacktestTrade],
    starting_equity: float,
    *,
    simulations: int = 1000,
    drawdown_threshold: float = -0.20,
    seed: int = 42,
) -> MonteCarloSummary:
    if not np.isfinite(starting_equity) or starting_equity <= 0:
        raise ValueError("Monte Carlo starting equity must be a positive finite value")
    if (
        isinstance(simulations, bool)
        or not isinstance(simulations, (int, np.integer))
        or simulations < 1
    ):
        raise ValueError("Monte Carlo simulations must be a positive integer")
    if isinstance(seed, bool) or not isinstance(seed, (int, np.integer)) or seed < 0:
        raise ValueError("Monte Carlo seed must be a non-negative integer")
    if not np.isfinite(drawdown_threshold) or not -1 <= drawdown_threshold <= 0:
        raise ValueError("Monte Carlo drawdown threshold must be between -1 and 0")

    pnl = np.asarray([trade.realized_pnl for trade in trades], dtype=float)
    if not np.isfinite(pnl).all():
        raise ValueError("Monte Carlo trade P&L must contain only finite values")
    if not len(pnl):
        return MonteCarloSummary(
            simulations=simulations,
            trades_per_path=0,
            mean_ending_equity=starting_equity,
            median_ending_equity=starting_equity,
            fifth_percentile_ending_equity=starting_equity,
            ninety_fifth_percentile_ending_equity=starting_equity,
            median_max_drawdown=0.0,
            fifth_percentile_max_drawdown=0.0,
            worst_max_drawdown=0.0,
            ninety_fifth_percentile_losing_streak=0.0,
            probability_of_loss=0.0,
            probability_of_drawdown_threshold=0.0,
        )

    sampled_pnl = np.random.default_rng(seed).choice(
        pnl, size=(simulations, len(pnl)), replace=True,
    )
    equity_paths = starting_equity + sampled_pnl.cumsum(axis=1)
    running_peaks = np.maximum.accumulate(
        np.column_stack((np.full(simulations, starting_equity), equity_paths)),
        axis=1,
    )[:, 1:]
    maximum_drawdowns = np.min(equity_paths / running_peaks - 1, axis=1)
    current_streak = np.zeros(simulations, dtype=int)
    longest_streak = np.zeros(simulations, dtype=int)
    for trade_pnl in sampled_pnl.T:
        current_streak = np.where(trade_pnl < 0, current_streak + 1, 0)
        longest_streak = np.maximum(longest_streak, current_streak)

    ending_equity = equity_paths[:, -1]
    return MonteCarloSummary(
        simulations=simulations,
        trades_per_path=len(pnl),
        mean_ending_equity=float(np.mean(ending_equity)),
        median_ending_equity=float(np.median(ending_equity)),
        fifth_percentile_ending_equity=float(np.percentile(ending_equity, 5)),
        ninety_fifth_percentile_ending_equity=float(np.percentile(ending_equity, 95)),
        median_max_drawdown=float(np.median(maximum_drawdowns)),
        fifth_percentile_max_drawdown=float(np.percentile(maximum_drawdowns, 5)),
        worst_max_drawdown=float(np.min(maximum_drawdowns)),
        ninety_fifth_percentile_losing_streak=float(np.percentile(longest_streak, 95)),
        probability_of_loss=float(np.mean(ending_equity < starting_equity)),
        probability_of_drawdown_threshold=float(np.mean(maximum_drawdowns <= drawdown_threshold)),
    )


def _normalize_bars(bars: pd.DataFrame) -> pd.DataFrame:
    data = bars.copy()
    index = pd.DatetimeIndex(pd.to_datetime(data.index))
    if index.tz is not None:
        index = index.tz_convert("UTC").tz_localize(None)
    data.index = index.normalize()
    return data.loc[~data.index.duplicated(keep="last")].sort_index()


def _market_regimes(benchmark: pd.DataFrame | None) -> dict[pd.Timestamp, str]:
    if benchmark is None or benchmark.empty:
        return {}

    bars = _normalize_bars(benchmark)
    close = bars["Close"]
    sma50 = close.rolling(50).mean()
    sma200 = close.rolling(200).mean()
    annualized_volatility = close.pct_change().rolling(20).std() * np.sqrt(252)
    regimes = {}
    for day, price, trend50, trend200, volatility in zip(
        bars.index, close, sma50, sma200, annualized_volatility,
    ):
        if pd.isna(trend50) or pd.isna(trend200) or pd.isna(volatility):
            regime = "UNKNOWN"
        elif volatility >= 0.30:
            regime = "HIGH_VOLATILITY"
        elif price > trend50 > trend200:
            regime = "BULL"
        elif price < trend50 < trend200:
            regime = "BEAR"
        else:
            regime = "NEUTRAL"
        regimes[day] = regime
    return regimes


def _value_at(series: pd.Series | None, day: pd.Timestamp) -> float | None:
    if series is None:
        return None
    value = series.get(day)
    return None if value is None or pd.isna(value) else float(value)


def _format_percent(value: float | None) -> str:
    return "N/A" if value is None else f"{value:.2%}"


def run_backtest(
    histories: dict[str, pd.DataFrame],
    cfg: TradingConfig,
    *,
    slippage_bps: float = 5.0,
    spread_bps: float = 0.0,
    commission_per_share: float = 0.0,
    target_fill_mode: str = "trade_through",
    target_trade_through_bps: float = 5.0,
    strategy: Callable[[str, pd.DataFrame, TradingConfig], TradeCandidate | None] = _historical_strategy,
    strategy_cache: dict[tuple[str, str, pd.Timestamp], TradeCandidate | None] | None = None,
    strategy_id: str = STRATEGY_ID,
    strategy_version: str = STRATEGY_VERSION,
    benchmark: pd.DataFrame | None = None,
    sector_map: dict[str, str] | None = None,
    sector_benchmarks: dict[str, pd.DataFrame] | None = None,
    simulation_start: str | None = None,
    simulation_end: str | None = None,
    universe_membership: dict[str, tuple[tuple[date, date | None], ...]] | None = None,
) -> BacktestResult:
    """Run close-generated signals at the following session's open using daily bars."""
    costs = (slippage_bps, spread_bps, commission_per_share, target_trade_through_bps)
    if any(not np.isfinite(cost) or cost < 0 for cost in costs):
        raise ValueError("Execution costs cannot be negative")
    if (
        not np.isfinite(cfg.max_sector_exposure_fraction)
        or not 0 < cfg.max_sector_exposure_fraction <= 1
    ):
        raise ValueError("Maximum sector exposure fraction must be greater than 0 and at most 1")
    if (
        not np.isfinite(cfg.max_combined_risk_fraction)
        or not 0 < cfg.max_combined_risk_fraction <= 1
    ):
        raise ValueError("Maximum combined risk fraction must be greater than 0 and at most 1")
    validate_correlation_config(cfg)
    if target_fill_mode not in {"touch", "trade_through"}:
        raise ValueError("Target fill mode must be 'touch' or 'trade_through'")

    data = {symbol: _normalize_bars(bars) for symbol, bars in histories.items() if not bars.empty}
    sessions = sorted(set().union(*(set(bars.index) for bars in data.values()))) if data else []
    if not sessions:
        raise ValueError("No historical bars available for the requested backtest")
    start_date = pd.Timestamp(date.fromisoformat(simulation_start)) if simulation_start else sessions[0]
    end_date = pd.Timestamp(date.fromisoformat(simulation_end)) if simulation_end else sessions[-1]
    if start_date > end_date:
        raise ValueError("Simulation start date cannot be later than end date")
    active_sessions = [day for day in sessions if start_date <= day <= end_date]
    if not active_sessions:
        raise ValueError("No historical bars available inside the requested simulation period")
    final_day = active_sessions[-1]

    def is_universe_member(symbol: str, day: pd.Timestamp) -> bool:
        if universe_membership is None:
            return True
        return any(
            first <= day.date() and (last is None or day.date() <= last)
            for first, last in universe_membership.get(symbol, ())
        )

    market_cost = (slippage_bps + spread_bps / 2) / 10_000
    config_fingerprint = configuration_hash(cfg)
    market_regimes = _market_regimes(benchmark)
    sectors = {
        symbol: sector_for_symbol(symbol)
        for symbol in data
    }
    if sector_map:
        sectors.update({symbol: sector_map.get(symbol, sectors[symbol]) for symbol in data})
    sector_etfs = {
        sector: SECTOR_ETF_BY_SECTOR[sector]
        for sector in set(sectors.values())
        if sector in SECTOR_ETF_BY_SECTOR
    }
    tracked_sectors = sorted(set(sectors.values()))
    daily_returns = {symbol: bars["Close"].pct_change() for symbol, bars in data.items()}
    stock_returns_20d = {symbol: bars["Close"].pct_change(20) for symbol, bars in data.items()}
    benchmark_bars = _normalize_bars(benchmark) if benchmark is not None and not benchmark.empty else None
    market_returns_20d = benchmark_bars["Close"].pct_change(20) if benchmark_bars is not None else None
    sector_returns_20d = {
        etf: _normalize_bars((sector_benchmarks or {})[etf])["Close"].pct_change(20)
        for etf in set(sector_etfs.values())
        if etf in (sector_benchmarks or {}) and not (sector_benchmarks or {})[etf].empty
    }
    cash = cfg.account_size
    positions: dict[str, _Position] = {}
    strategy_cache = {} if strategy_cache is None else strategy_cache
    pending: list[_Signal] = []
    trades: list[BacktestTrade] = []
    equity_values: list[float] = []
    equity_dates: list[pd.Timestamp] = []
    exposure_fractions: list[float] = []
    open_risk_fractions: list[float] = []
    sector_exposure_fractions = {sector: [] for sector in tracked_sectors}
    daily_pairwise_correlations: list[float] = []
    maximum_pairwise_correlations: list[float] = []
    correlation_blocked_entries = 0
    last_closes: dict[str, tuple[pd.Timestamp, float]] = {}

    def close_position(symbol: str, position: _Position, day: pd.Timestamp, price: float, reason: str):
        nonlocal cash
        exit_price = price
        if reason.startswith("stop") or reason == "end_of_test":
            exit_price *= 1 - market_cost
        elif reason == "target_gap":
            exit_price = max(position.target_price, exit_price * (1 - market_cost))
        pnl = (exit_price - position.entry_price) * position.shares
        fees = commission_per_share * position.shares * 2
        pnl -= fees
        cash += exit_price * position.shares - commission_per_share * position.shares
        realized_r = pnl / position.initial_risk if position.initial_risk else 0.0
        trades.append(BacktestTrade(
            strategy_id=strategy_id,
            strategy_version=strategy_version,
            configuration_hash=config_fingerprint,
            symbol=symbol,
            sector=position.sector,
            sector_etf=position.sector_etf,
            market_regime=position.market_regime,
            stock_return_20d=position.stock_return_20d,
            market_return_20d=position.market_return_20d,
            sector_return_20d=position.sector_return_20d,
            relative_strength_vs_market=position.relative_strength_vs_market,
            relative_strength_vs_sector=position.relative_strength_vs_sector,
            entry_date=position.entry_date.date().isoformat(),
            exit_date=day.date().isoformat(),
            entry_price=position.entry_price,
            exit_price=exit_price,
            shares=position.shares,
            holding_sessions=int(data[symbol].loc[position.entry_date:day].shape[0]),
            stop_price=position.stop_price,
            target_price=position.target_price,
            exit_reason=reason,
            realized_pnl=pnl,
            realized_r=realized_r,
            strategy_score=position.candidate.score,
            setup=position.candidate.setup,
        ))
        del positions[symbol]

    for session_index, day in enumerate(sessions):
        if day < start_date:
            continue
        if day > end_date:
            break
        today = {symbol: bars.loc[day] for symbol, bars in data.items() if day in bars.index}

        # Stops and targets crossed by the open execute before new entries compete for capital.
        for symbol, position in list(positions.items()):
            bar = today.get(symbol)
            if bar is None:
                continue
            if float(bar["Open"]) <= position.stop_price:
                close_position(symbol, position, day, float(bar["Open"]), "stop_gap")
            elif float(bar["Open"]) >= position.target_price:
                close_position(symbol, position, day, float(bar["Open"]), "target_gap")

        for signal in sorted(
            pending,
            key=lambda item: (
                item.candidate.prob_gain_10d,
                item.candidate.score,
                item.candidate.reward_risk,
            ),
            reverse=True,
        ):
            candidate = signal.candidate
            bar = today.get(candidate.symbol)
            if (
                bar is None
                or candidate.symbol in positions
                or not is_universe_member(candidate.symbol, day)
            ):
                continue
            if len(positions) >= cfg.max_open_positions:
                break
            fill_price = float(bar["Open"]) * (1 + market_cost)
            live_cfg = replace(cfg, account_size=max(cash, 0.0))
            sized = refresh_candidate_price(candidate, fill_price, live_cfg)
            if sized is None or sized.shares * (fill_price + commission_per_share) > cash:
                continue
            open_risk = sum(position.initial_risk for position in positions.values())
            portfolio_equity = cash + sum(
                position.shares * (
                    float(today[held_symbol]["Open"])
                    if held_symbol in today else last_closes[held_symbol][1]
                )
                for held_symbol, position in positions.items()
            )
            risk_balance = min(max(portfolio_equity, 0.0), cfg.account_size)
            remaining_risk = max(
                0.0,
                risk_balance * cfg.max_combined_risk_fraction - open_risk,
            )
            risk_per_share = fill_price - sized.stop
            affordable_risk_shares = math.floor(remaining_risk / risk_per_share)
            if affordable_risk_shares < sized.shares:
                if affordable_risk_shares < 1:
                    continue
                sized = replace(
                    sized,
                    shares=affordable_risk_shares,
                    risk_dollars=affordable_risk_shares * risk_per_share,
                )
            if sized.shares * (fill_price + commission_per_share) > cash:
                continue
            sector_exposure = sum(
                position.shares * (
                    float(today[held_symbol]["Open"])
                    if held_symbol in today else last_closes[held_symbol][1]
                )
                for held_symbol, position in positions.items()
                if position.sector == sectors[sized.symbol]
            )
            sector_limit = cfg.account_size * cfg.max_sector_exposure_fraction
            if sector_exposure + sized.shares * fill_price > sector_limit:
                continue
            if positions:
                correlation = highest_absolute_correlation(
                    daily_returns[sized.symbol].loc[:signal.signal_date],
                    {
                        held_symbol: daily_returns[held_symbol].loc[:signal.signal_date]
                        for held_symbol in positions
                    },
                    window=cfg.correlation_lookback_sessions,
                    minimum_observations=cfg.correlation_min_observations,
                )
                if (
                    correlation is not None
                    and correlation[1] >= cfg.correlation_absolute_threshold
                ):
                    correlation_blocked_entries += 1
                    continue
            cash -= sized.shares * fill_price + commission_per_share * sized.shares
            positions[sized.symbol] = _Position(
                candidate=sized,
                entry_date=day,
                entry_price=fill_price,
                shares=sized.shares,
                stop_price=sized.stop,
                target_price=fill_price * (1 + cfg.target_percent / 100),
                initial_risk=sized.risk_dollars,
                sector=sectors[sized.symbol],
                sector_etf=signal.sector_etf,
                market_regime=signal.market_regime,
                stock_return_20d=signal.stock_return_20d,
                market_return_20d=signal.market_return_20d,
                sector_return_20d=signal.sector_return_20d,
                relative_strength_vs_market=signal.relative_strength_vs_market,
                relative_strength_vs_sector=signal.relative_strength_vs_sector,
            )
        pending = []

        # When both barriers are inside a daily candle, assume the adverse stop happened first.
        for symbol, position in list(positions.items()):
            bar = today.get(symbol)
            if bar is None:
                continue
            if float(bar["Low"]) <= position.stop_price:
                close_position(symbol, position, day, position.stop_price, "stop")
            else:
                target_trigger = position.target_price
                if target_fill_mode == "trade_through":
                    target_trigger *= 1 + (target_trade_through_bps + spread_bps / 2) / 10_000
                if float(bar["High"]) >= target_trigger:
                    close_position(symbol, position, day, position.target_price, "target")

        if day < final_day:
            for symbol, bars in data.items():
                if day not in bars.index or not is_universe_member(symbol, day):
                    continue
                prefix = bars.loc[:day]
                cache_key = (config_fingerprint, symbol, day)
                if cache_key not in strategy_cache:
                    strategy_cache[cache_key] = strategy(symbol, prefix, cfg)
                candidate = strategy_cache[cache_key]
                if candidate is not None and symbol not in positions:
                    stock_return = _value_at(stock_returns_20d[symbol], day)
                    market_return = _value_at(market_returns_20d, day)
                    sector = sectors[symbol]
                    sector_etf = sector_etfs.get(sector)
                    sector_return = _value_at(sector_returns_20d.get(sector_etf), day) if sector_etf else None
                    pending.append(_Signal(
                        candidate=candidate,
                        signal_date=day,
                        market_regime=market_regimes.get(day, "UNKNOWN"),
                        sector=sector,
                        sector_etf=sector_etf,
                        stock_return_20d=stock_return,
                        market_return_20d=market_return,
                        sector_return_20d=sector_return,
                        relative_strength_vs_market=(stock_return - market_return)
                        if stock_return is not None and market_return is not None else None,
                        relative_strength_vs_sector=(stock_return - sector_return)
                        if stock_return is not None and sector_return is not None else None,
                    ))

        last_closes.update({symbol: (day, float(bar["Close"])) for symbol, bar in today.items()})
        gross_exposure = sum(
            position.shares * last_closes[symbol][1]
            for symbol, position in positions.items()
            if symbol in last_closes
        )
        marked_equity = cash + gross_exposure
        exposure_fractions.append(gross_exposure / marked_equity if marked_equity > 0 else 0.0)
        open_risk = sum(position.initial_risk for position in positions.values())
        open_risk_fractions.append(open_risk / marked_equity if marked_equity > 0 else 0.0)
        sector_market_values = {sector: 0.0 for sector in tracked_sectors}
        for symbol, position in positions.items():
            if symbol in last_closes:
                sector_market_values[position.sector] += position.shares * last_closes[symbol][1]
        for sector, market_value in sector_market_values.items():
            sector_exposure_fractions[sector].append(
                market_value / marked_equity if marked_equity > 0 else 0.0
            )

        open_symbols = list(positions)
        if len(open_symbols) > 1:
            return_frame = pd.concat(
                {symbol: daily_returns[symbol].loc[:day] for symbol in open_symbols},
                axis=1,
            ).tail(60).dropna()
            if len(return_frame) >= 20:
                correlation_matrix = return_frame.corr().to_numpy()
                pair_correlations = correlation_matrix[np.triu_indices(len(open_symbols), k=1)]
                pair_correlations = pair_correlations[np.isfinite(pair_correlations)]
                if len(pair_correlations):
                    daily_pairwise_correlations.append(float(np.mean(pair_correlations)))
                    maximum_pairwise_correlations.append(float(np.max(pair_correlations)))
        equity_values.append(marked_equity)
        equity_dates.append(day)

    for symbol, position in list(positions.items()):
        last_day, last_close = last_closes[symbol]
        close_position(symbol, position, last_day, last_close, "end_of_test")
    if equity_values:
        equity_values[-1] = cash

    equity_curve = pd.Series(equity_values, index=pd.DatetimeIndex(equity_dates), name="equity")
    drawdown = equity_curve / equity_curve.cummax() - 1
    winners = [trade.realized_pnl for trade in trades if trade.realized_pnl > 0]
    losers = [trade.realized_pnl for trade in trades if trade.realized_pnl < 0]
    gross_loss = abs(sum(losers))
    market_regime_metrics = {}
    for regime in sorted({trade.market_regime for trade in trades}):
        regime_trades = [trade for trade in trades if trade.market_regime == regime]
        regime_wins = [trade.realized_pnl for trade in regime_trades if trade.realized_pnl > 0]
        regime_losses = [trade.realized_pnl for trade in regime_trades if trade.realized_pnl < 0]
        regime_gross_loss = abs(sum(regime_losses))
        market_regime_metrics[regime] = {
            "trade_count": len(regime_trades),
            "win_rate": len(regime_wins) / len(regime_trades),
            "net_pnl": float(sum(trade.realized_pnl for trade in regime_trades)),
            "average_r": float(np.mean([trade.realized_r for trade in regime_trades])),
            "profit_factor": sum(regime_wins) / regime_gross_loss if regime_gross_loss else (float("inf") if regime_wins else 0.0),
        }
    returns = equity_curve.pct_change().dropna()
    daily_std = float(returns.std(ddof=1)) if len(returns) > 1 else 0.0
    annualized_volatility = daily_std * np.sqrt(252)
    downside_deviation = float(np.sqrt(np.mean(np.square(np.minimum(returns, 0))))) if len(returns) else 0.0
    elapsed_days = (equity_curve.index[-1] - equity_curve.index[0]).days if len(equity_curve) > 1 else 0
    cagr = None
    if elapsed_days > 0 and cfg.account_size > 0 and cash > 0:
        cagr = float((cash / cfg.account_size) ** (365.25 / elapsed_days) - 1)
    mean_daily_return = float(returns.mean()) if len(returns) else 0.0
    sharpe_ratio = mean_daily_return / daily_std * np.sqrt(252) if daily_std > 0 else None
    sortino_ratio = mean_daily_return * np.sqrt(252) / downside_deviation if downside_deviation > 0 else None
    calmar_ratio = cagr / abs(float(drawdown.min())) if cagr is not None and drawdown.min() < 0 else None
    losing_streak = 0
    max_consecutive_losses = 0
    for trade in trades:
        losing_streak = losing_streak + 1 if trade.realized_pnl < 0 else 0
        max_consecutive_losses = max(max_consecutive_losses, losing_streak)
    benchmark_return = None
    if benchmark is not None and not benchmark.empty:
        benchmark_bars = _normalize_bars(benchmark)
        benchmark_period = benchmark_bars.loc[active_sessions[0]:final_day]
        if not benchmark_period.empty:
            benchmark_return = float(
                benchmark_period["Close"].iloc[-1] / benchmark_period["Open"].iloc[0] - 1
            )
    benchmark_alpha = cash / cfg.account_size - 1 - benchmark_return if benchmark_return is not None else None
    average_equity = float(equity_curve.mean()) if not equity_curve.empty else 0.0
    traded_notional = sum((trade.entry_price + trade.exit_price) * trade.shares for trade in trades)
    sector_metrics = {
        sector: {
            "average_exposure": float(np.mean(values)) if values else 0.0,
            "peak_exposure": float(np.max(values)) if values else 0.0,
        }
        for sector, values in sector_exposure_fractions.items()
    }
    sector_etf_mapping = {sector: etf for sector, etf in sector_etfs.items()}
    relative_strength_market = [
        trade.relative_strength_vs_market for trade in trades
        if trade.relative_strength_vs_market is not None
    ]
    relative_strength_sector = [
        trade.relative_strength_vs_sector for trade in trades
        if trade.relative_strength_vs_sector is not None
    ]

    return BacktestResult(
        trades=trades,
        equity_curve=equity_curve,
        starting_equity=cfg.account_size,
        ending_equity=float(cash),
        benchmark_return=benchmark_return,
        max_drawdown=float(drawdown.min()) if not drawdown.empty else 0.0,
        win_rate=len(winners) / len(trades) if trades else 0.0,
        average_r=float(np.mean([trade.realized_r for trade in trades])) if trades else 0.0,
        profit_factor=sum(winners) / gross_loss if gross_loss else (float("inf") if winners else 0.0),
        strategy_id=strategy_id,
        strategy_version=strategy_version,
        configuration_hash=config_fingerprint,
        configuration=asdict(cfg),
        cagr=cagr,
        benchmark_alpha=benchmark_alpha,
        annualized_volatility=float(annualized_volatility),
        sharpe_ratio=float(sharpe_ratio) if sharpe_ratio is not None else None,
        sortino_ratio=float(sortino_ratio) if sortino_ratio is not None else None,
        calmar_ratio=float(calmar_ratio) if calmar_ratio is not None else None,
        average_win=float(np.mean(winners)) if winners else None,
        average_loss=float(np.mean(losers)) if losers else None,
        median_win=float(np.median(winners)) if winners else None,
        median_loss=float(np.median(losers)) if losers else None,
        expectancy_dollars=float(np.mean([trade.realized_pnl for trade in trades])) if trades else 0.0,
        max_consecutive_losses=max_consecutive_losses,
        average_holding_sessions=float(np.mean([trade.holding_sessions for trade in trades])) if trades else 0.0,
        average_gross_exposure=float(np.mean(exposure_fractions)) if exposure_fractions else 0.0,
        turnover=traded_notional / average_equity if average_equity > 0 else 0.0,
        average_open_risk_fraction=float(np.mean(open_risk_fractions)) if open_risk_fractions else 0.0,
        peak_open_risk_fraction=float(np.max(open_risk_fractions)) if open_risk_fractions else 0.0,
        market_regime_metrics=market_regime_metrics,
        sector_mapping=sectors,
        sector_etf_mapping=sector_etf_mapping,
        sector_exposure_metrics=sector_metrics,
        average_pairwise_correlation=(float(np.mean(daily_pairwise_correlations)) if daily_pairwise_correlations else None),
        max_pairwise_correlation=(float(np.max(maximum_pairwise_correlations)) if maximum_pairwise_correlations else None),
        correlation_blocked_entries=correlation_blocked_entries,
        average_relative_strength_vs_market=(float(np.mean(relative_strength_market)) if relative_strength_market else None),
        average_relative_strength_vs_sector=(float(np.mean(relative_strength_sector)) if relative_strength_sector else None),
    )


def format_spread_sensitivity_report(
    results: list[tuple[float, BacktestResult]],
    *,
    run_ids: list[str],
    database_path: str,
    symbols: list[str],
    start: str,
    end: str,
    slippage_bps: float,
    commission_per_share: float,
    target_fill_mode: str,
    target_trade_through_bps: float,
    monte_carlo_simulations: int = 1000,
    monte_carlo_drawdown_threshold: float = -0.20,
    monte_carlo_seed: int = 42,
    universe_source: str | None = None,
    universe_sha256: str | None = None,
    universe_record_count: int = 0,
    experiment_name: str | None = None,
) -> str:
    if not results:
        raise ValueError("At least one spread scenario is required")
    if len(run_ids) != len(results):
        raise ValueError("Each sensitivity scenario must have one persisted run ID")
    base_result = results[0][1]
    identity = (base_result.strategy_id, base_result.strategy_version, base_result.configuration_hash)
    if any(
        (result.strategy_id, result.strategy_version, result.configuration_hash) != identity
        for _, result in results[1:]
    ):
        raise ValueError("All scenarios must use the same strategy and trading configuration")

    report = [
        "# Swing Trader Execution-Cost Sensitivity",
        "",
        f"Period: {start} to {end}",
        f"Symbols: {', '.join(symbols)}",
        f"Strategy: {base_result.strategy_id} v{base_result.strategy_version}",
        f"Trading configuration SHA-256: {base_result.configuration_hash}",
        f"Universe: {'date-effective file' if universe_sha256 else 'explicit fixed symbol list'}",
        f"Universe source: {universe_source or 'N/A'}",
        f"Universe SHA-256: {universe_sha256 or 'N/A'}",
        f"Universe membership records: {universe_record_count}",
        f"Experiment: {experiment_name or 'N/A'}",
        "When provided, exact date-effective membership intervals are persisted with each run in the SQLite ledger.",
        f"SQLite ledger: `{database_path}`",
        "Sector mapping (manual, descriptive):",
        "```json",
        json.dumps(base_result.sector_mapping, sort_keys=True, indent=2),
        "```",
        "Sector ETF mapping:",
        "```json",
        json.dumps(base_result.sector_etf_mapping, sort_keys=True, indent=2),
        "```",
        "Trading configuration snapshot:",
        "```json",
        json.dumps(base_result.configuration, sort_keys=True, indent=2),
        "```",
        "Strategy and historical bars are held constant across scenarios.",
        f"Slippage: {slippage_bps:g} bps per market-side fill",
        f"Commission: ${commission_per_share:g} per share per side",
        f"Target limit fills: {target_fill_mode}; base trade-through: {target_trade_through_bps:g} bps, plus half-spread",
        "Open risk is planned stop distance times shares divided by marked equity; it does not account for gap losses.",
        "",
        "## Scenario Comparison",
        "",
        "| Run ID | Full spread (bps) | Ending equity | Return | Max drawdown | Trades | Corr blocks | Win rate | Avg R | Profit factor | Avg gross exposure | Avg open risk | Peak open risk | Turnover | Avg corr | Max corr | SPY price return |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for run_id, (spread_bps, result) in zip(run_ids, results):
        total_return = result.ending_equity / result.starting_equity - 1
        profit_factor = "inf" if np.isinf(result.profit_factor) else f"{result.profit_factor:.2f}"
        benchmark = "N/A" if result.benchmark_return is None else f"{result.benchmark_return:.2%}"
        average_correlation = "N/A" if result.average_pairwise_correlation is None else f"{result.average_pairwise_correlation:.2f}"
        maximum_correlation = "N/A" if result.max_pairwise_correlation is None else f"{result.max_pairwise_correlation:.2f}"
        report.append(
            f"| {run_id} | {spread_bps:g} | ${result.ending_equity:,.2f} | {total_return:.2%} | "
            f"{result.max_drawdown:.2%} | {len(result.trades)} | {result.correlation_blocked_entries} | "
            f"{result.win_rate:.2%} | "
            f"{result.average_r:.2f} | {profit_factor} | {result.average_gross_exposure:.2%} | "
            f"{result.average_open_risk_fraction:.2%} | {result.peak_open_risk_fraction:.2%} | "
            f"{result.turnover:.2f}x | {average_correlation} | {maximum_correlation} | {benchmark} |"
        )

    report.extend([
        "",
        "## Sector Exposure",
        "",
        "Exposure is sector market value divided by total marked equity, averaged over each session; peak is the maximum session value.",
        "Sector labels are a manual present-day map, not point-in-time historical classifications.",
        "",
        "| Full spread (bps) | Sector | Average exposure | Peak exposure |",
        "|---:|---|---:|---:|",
    ])
    for spread_bps, result in results:
        for sector, metrics in sorted(result.sector_exposure_metrics.items()):
            report.append(
                f"| {spread_bps:g} | {sector} | {metrics['average_exposure']:.2%} | "
                f"{metrics['peak_exposure']:.2%} |"
            )

    report.extend([
        "",
        "## Open-Position Correlation",
        "",
        "Descriptive pairwise portfolio correlation uses up to 60 daily returns through each session close, requires 20 common observations, and is sampled only when multiple positions are open. Entry blocking uses the separately configured threshold and minimum observation count.",
        "",
        "| Full spread (bps) | Average pairwise correlation | Maximum pairwise correlation |",
        "|---:|---:|---:|",
    ])
    for spread_bps, result in results:
        average_correlation = "N/A" if result.average_pairwise_correlation is None else f"{result.average_pairwise_correlation:.2f}"
        maximum_correlation = "N/A" if result.max_pairwise_correlation is None else f"{result.max_pairwise_correlation:.2f}"
        report.append(f"| {spread_bps:g} | {average_correlation} | {maximum_correlation} |")

    report.extend([
        "",
        "## Relative Strength",
        "",
        "20-session trailing returns are measured at each signal close. Relative strength is stock return minus benchmark return in percentage points; values do not affect signals or sizing.",
        "",
        "| Full spread (bps) | Average stock return | Average SPY return | Average stock - SPY | Average sector ETF return | Average stock - sector |",
        "|---:|---:|---:|---:|---:|---:|",
    ])
    for spread_bps, result in results:
        stock_returns = [trade.stock_return_20d for trade in result.trades if trade.stock_return_20d is not None]
        market_returns = [trade.market_return_20d for trade in result.trades if trade.market_return_20d is not None]
        sector_returns = [trade.sector_return_20d for trade in result.trades if trade.sector_return_20d is not None]
        percent = lambda values: "N/A" if not values else f"{np.mean(values):.2%}"
        relative_market = "N/A" if result.average_relative_strength_vs_market is None else f"{result.average_relative_strength_vs_market:.2%}"
        relative_sector = "N/A" if result.average_relative_strength_vs_sector is None else f"{result.average_relative_strength_vs_sector:.2%}"
        report.append(
            f"| {spread_bps:g} | {percent(stock_returns)} | {percent(market_returns)} | {relative_market} | "
            f"{percent(sector_returns)} | {relative_sector} |"
        )

    report.extend([
        "",
        "## Market Regime Performance",
        "",
        "Regime at the signal close uses SPY: BULL means close > SMA50 > SMA200; BEAR means "
        "close < SMA50 < SMA200; HIGH_VOLATILITY means annualized 20-session realized "
        "volatility >= 30%; otherwise NEUTRAL. Insufficient SPY history is UNKNOWN.",
        "Regimes are descriptive only and do not alter entries, sizing, or exits.",
        "",
        "| Full spread (bps) | Regime | Trades | Win rate | Net P&L | Avg R | Profit factor |",
        "|---:|---|---:|---:|---:|---:|---:|",
    ])
    for spread_bps, result in results:
        for regime, metrics in sorted(result.market_regime_metrics.items()):
            regime_profit_factor = metrics["profit_factor"]
            regime_pf_label = "inf" if np.isinf(regime_profit_factor) else f"{regime_profit_factor:.2f}"
            report.append(
                f"| {spread_bps:g} | {regime} | {metrics['trade_count']} | {metrics['win_rate']:.2%} | "
                f"${metrics['net_pnl']:.2f} | {metrics['average_r']:.2f} | {regime_pf_label} |"
            )

    report.extend([
        "",
        "## Annualized and Risk-Adjusted Metrics",
        "",
        "Risk-free rate assumed to be 0%; volatility and ratios annualized using 252 trading sessions.",
        "",
        "| Full spread (bps) | CAGR | Alpha vs SPY | Annualized volatility | Sharpe | Sortino | Calmar |",
        "|---:|---:|---:|---:|---:|---:|---:|",
    ])
    for spread_bps, result in results:
        ratio = lambda value: "N/A" if value is None else f"{value:.2f}"
        percent = lambda value: "N/A" if value is None else f"{value:.2%}"
        report.append(
            f"| {spread_bps:g} | {percent(result.cagr)} | {percent(result.benchmark_alpha)} | "
            f"{result.annualized_volatility:.2%} | {ratio(result.sharpe_ratio)} | "
            f"{ratio(result.sortino_ratio)} | {ratio(result.calmar_ratio)} |"
        )

    report.extend([
        "",
        "## Trade Distribution",
        "",
        "Expectancy is mean net P&L per closed trade; Avg R is mean realized R.",
        "",
        "| Full spread (bps) | Average win | Average loss | Median win | Median loss | Expectancy | Avg R | Max losing streak | Avg hold (sessions) |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ])
    for spread_bps, result in results:
        money = lambda value: "N/A" if value is None else f"${value:.2f}"
        report.append(
            f"| {spread_bps:g} | {money(result.average_win)} | {money(result.average_loss)} | "
            f"{money(result.median_win)} | {money(result.median_loss)} | ${result.expectancy_dollars:.2f} | "
            f"{result.average_r:.2f} | {result.max_consecutive_losses} | {result.average_holding_sessions:.1f} |"
        )

    report.extend([
        "",
        "## Monte Carlo Trade-Sequence Bootstrap",
        "",
        f"{monte_carlo_simulations:,} paths; seed {monte_carlo_seed}; drawdown threshold {monte_carlo_drawdown_threshold:.1%}. "
        "Each path resamples the scenario's closed-trade dollar P&L independently with replacement, keeping the observed trade count.",
        "This exploratory trade-sequence analysis does not re-simulate overlapping positions, dynamic sizing, or compounding, and does not preserve trade dependence. "
        "It is not a substitute for portfolio-path simulation.",
        "",
        "| Full spread (bps) | Median ending equity | 5th-95th percentile ending equity | Median max drawdown | 5th percentile max drawdown | Worst simulated drawdown | 95th percentile losing streak | Probability of loss | Probability of threshold drawdown |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ])
    for scenario_index, (spread_bps, result) in enumerate(results):
        summary = monte_carlo_trade_bootstrap(
            result.trades,
            result.starting_equity,
            simulations=monte_carlo_simulations,
            drawdown_threshold=monte_carlo_drawdown_threshold,
            seed=monte_carlo_seed + scenario_index,
        )
        report.append(
            f"| {spread_bps:g} | ${summary.median_ending_equity:,.2f} | "
            f"${summary.fifth_percentile_ending_equity:,.2f} - ${summary.ninety_fifth_percentile_ending_equity:,.2f} | "
            f"{summary.median_max_drawdown:.2%} | {summary.fifth_percentile_max_drawdown:.2%} | "
            f"{summary.worst_max_drawdown:.2%} | {summary.ninety_fifth_percentile_losing_streak:.0f} | "
            f"{summary.probability_of_loss:.2%} | {summary.probability_of_drawdown_threshold:.2%} |"
        )

    ledger_spread, ledger_result = min(results, key=lambda item: abs(item[0] - 5.0))
    report.extend([
        "",
        f"## Trade Ledger ({ledger_spread:g} bps spread scenario)",
        "",
        "| Strategy | Version | Symbol | Sector | Sector ETF | Regime | Entry date | Exit date | Stock 20d | SPY 20d | Sector 20d | Stock-SPY | Stock-sector | Shares | Held | Entry | Exit | Reason | P&L | R | Setup |",
        "|---|---|---|---|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---:|---:|---|",
    ])
    report.extend(
        f"| {trade.strategy_id} | {trade.strategy_version} | {trade.symbol} | {trade.sector} | "
        f"{trade.sector_etf or 'N/A'} | {trade.market_regime} | {trade.entry_date} | {trade.exit_date} | "
        f"{_format_percent(trade.stock_return_20d)} | {_format_percent(trade.market_return_20d)} | "
        f"{_format_percent(trade.sector_return_20d)} | "
        f"{_format_percent(trade.relative_strength_vs_market)} | "
        f"{_format_percent(trade.relative_strength_vs_sector)} | "
        f"{trade.shares} | {trade.holding_sessions} | "
        f"${trade.entry_price:.2f} | ${trade.exit_price:.2f} | {trade.exit_reason} | "
        f"${trade.realized_pnl:.2f} | {trade.realized_r:.2f} | {trade.setup} |"
        for trade in ledger_result.trades
    )
    return "\n".join(report) + "\n"