from dataclasses import dataclass
from pathlib import Path
import runpy


CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.py"


@dataclass(frozen=True)
class TradingConfig:
    # Starting balance used for position sizing.
    account_size: float = 1000.0
    # Largest planned loss per trade: 0.01 means 1% of the account ($10 here).
    risk_fraction: float = 0.01
    # Largest portion of the account allowed in one position: 0.40 means 40%.
    max_position_fraction: float = 0.40
    # Stop distance below entry, measured in Average True Range (ATR) units.
    stop_atr_multiple: float = 1.5
    # Profit target percent above entry: 1.0 means +1%.
    target_percent: float = 1.0
    # Avoid low-priced stocks that can have large percentage swings and wide spreads.
    minimum_price: float = 5.0
    # Avoid stocks above the largest whole-share position this account can take by default.
    maximum_price: float = 30.0
    # Require this many shares traded per day on average over the last 20 days.
    minimum_average_volume: int = 500_000
    # Average headline sentiment at or above this value earns a small score bonus.
    sentiment_positive_threshold: float = 0.2
    # Average headline sentiment at or below this value receives a score penalty.
    sentiment_negative_threshold: float = -0.2
    # Points added to an otherwise valid setup with clearly positive headline sentiment.
    sentiment_bonus_score: int = 5
    # Points subtracted from an otherwise valid setup with clearly negative headline sentiment.
    sentiment_penalty_score: int = 10
    max_spread_fraction: float = 0.002
    max_price_drift_fraction: float = 0.02
    max_quote_age_seconds: int = 60
    estimated_exit_cost_fraction: float = 0.001
    max_open_positions: int = 5
    daily_loss_fraction: float = 0.02
    max_combined_risk_fraction: float = 0.10
    max_sector_exposure_fraction: float = 0.60
    # Block entries at or above this absolute daily-return correlation.
    correlation_lookback_sessions: int = 60
    correlation_min_observations: int = 30
    correlation_absolute_threshold: float = 0.85
    bot_auto_recover: bool = True
    bot_max_hold_days: int = 180
    bot_poll_seconds: int = 30
    bot_scan_interval_seconds: int = 900


def load_trading_config(path: Path = CONFIG_PATH) -> TradingConfig:
    if not path.is_file():
        return TradingConfig()
    config = runpy.run_path(str(path)).get("TRADING_CONFIG", TradingConfig())
    if not isinstance(config, TradingConfig):
        raise ValueError("TRADING_CONFIG in config.py must be a TradingConfig instance")
    return config
