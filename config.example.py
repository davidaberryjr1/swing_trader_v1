# Copy this file to config.py and put your Alpaca paper account keys there.
# The root config.py is ignored by Git; never add real keys to this example.
from swingtrader.config import TradingConfig

# Validated as the paper endpoint; the SDK connects there with paper=True.
APCA_API_BASE_URL = "https://paper-api.alpaca.markets/v2"
APCA_API_KEY_ID = "PKMUG4SDLORJF4QYT4OKHU4T62"
APCA_API_SECRET_KEY = "2xyvwBBUua9CEDnfB1QEKpby8kQD6ujHhv1oqD6GaZj7"

TRADING_CONFIG = TradingConfig(
	account_size=1000.0,                   # Dollars used for sizing (not Alpaca account equity).
	risk_fraction=0.01,                    # Maximum planned loss per trade: 1% of account_size.
	max_position_fraction=0.40,            # Maximum value of one position: 40% of account_size.
	stop_atr_multiple=1.5,                 # Stop distance in daily ATR units.
	target_percent=1.0,                    # Quick target shown in scans, in percent.
	minimum_price=5.0,                     # Minimum eligible share price in dollars.
	maximum_price=30.0,                    # Maximum eligible share price in dollars.
	minimum_average_volume=500_000,       # Minimum 20-day average daily share volume.
	sentiment_positive_threshold=0.2,     # Positive headline threshold (VADER score).
	sentiment_negative_threshold=-0.2,    # Negative headline threshold (VADER score).
	sentiment_bonus_score=5,              # Points added for positive headlines.
	sentiment_penalty_score=10,           # Points removed for negative headlines.
	max_spread_fraction=0.002,            # Paper order: reject spreads wider than 0.2%.
	max_price_drift_fraction=0.02,        # Paper order: reject ask >2% from scan price.
	max_quote_age_seconds=60,             # Paper order: maximum Alpaca quote age.
	estimated_exit_cost_fraction=0.001,   # Paper target: 0.1% allowance for estimated sale costs.
	max_open_positions=5,                 # Bot: positions plus pending entries.
	daily_loss_fraction=0.02,             # Bot: pause new buys after 2% daily account loss.
	max_combined_risk_fraction=0.10,      # Bot: maximum planned downside across open trades.
	bot_poll_seconds=300,                 # Bot: reconcile broker orders every five minutes.
	bot_scan_interval_seconds=900,        # Bot: rescan open slots every fifteen minutes.
)
