"""
config.py — Centralised configuration for the Kraken+Telegram trading bot.
All sensitive values come from the .env file. Adjust strategy params here.
"""
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

# ── Credentials ───────────────────────────────────────────────────────────────
KRAKEN_API_KEY    = os.getenv("KRAKEN_API_KEY", "")
KRAKEN_API_SECRET = os.getenv("KRAKEN_API_SECRET", "")
TELEGRAM_TOKEN    = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID  = os.getenv("TELEGRAM_CHAT_ID", "")
GROQ_API_KEY      = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL        = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
PAPER_TRADING     = os.getenv("PAPER_TRADING", "false").lower() == "true"

# ── Budget & position sizing ──────────────────────────────────────────────────
BUDGET_EUR           = float(os.getenv("BUDGET_EUR", "100"))
MAX_OPEN_TRADES      = int(os.getenv("MAX_OPEN_TRADES", "4"))
MAX_CAPITAL_DEPLOYED = float(os.getenv("MAX_CAPITAL_DEPLOYED", "0.80"))

# ── Risk parameters ───────────────────────────────────────────────────────────
TAKE_PROFIT_PCT  = 0.10
STOP_LOSS_PCT    = 0.04
KRAKEN_TAKER_FEE = 0.004
KRAKEN_MAKER_FEE = 0.0025   # 0.25% — limit orders (maker)

# ── Limit orders ──────────────────────────────────────────────────────────────
LIMIT_ORDERS_ENABLED = os.getenv("LIMIT_ORDERS_ENABLED", "true").lower() == "true"
LIMIT_OFFSET_PCT     = float(os.getenv("LIMIT_OFFSET_PCT", "0.001"))  # 0.1% sotto bid
LIMIT_TIMEOUT_SEC    = int(os.getenv("LIMIT_TIMEOUT_SEC", "300"))     # 5 min poi market

# ── ATR-based TP/SL ───────────────────────────────────────────────────────────
ATR_PERIOD  = 14
ATR_TP_MULT = float(os.getenv("ATR_TP_MULT", "2.5"))   # TP = entry + mult×ATR
ATR_SL_MULT = float(os.getenv("ATR_SL_MULT", "1.0"))   # SL = entry − mult×ATR  →  R:R ≈ 2.5:1

# ── Slippage ──────────────────────────────────────────────────────────────────
SLIPPAGE_PCT = float(os.getenv("SLIPPAGE_PCT", "0.001"))  # 0.1% per lato

# ── Trailing stop ─────────────────────────────────────────────────────────────
TRAILING_STOP_ENABLED = True
TRAILING_STOP_PCT     = 0.03

# ── Circuit Breaker ───────────────────────────────────────────────────────────
CIRCUIT_BREAKER_ENABLED  = True
CIRCUIT_BREAKER_LOSS_EUR = float(os.getenv("CIRCUIT_BREAKER_LOSS_EUR", "15"))

# ── Signal scoring ────────────────────────────────────────────────────────────
MIN_SIGNAL_SCORE  = 3
VOLUME_SPIKE_MULT = 1.15
RSI_1H_BUY_MAX    = 65
RSI_15M_BUY_MAX   = 62
RSI_4H_BUY_MAX    = 70    # multi-timeframe: RSI 4h deve essere < 70

# ── ADX ───────────────────────────────────────────────────────────────────────
ADX_PERIOD = 14
ADX_MIN    = 20

# ── Multi-timeframe ───────────────────────────────────────────────────────────
DAILY_EMA_FILTER = os.getenv("DAILY_EMA_FILTER", "true").lower() == "true"

# ── BTC market filter ─────────────────────────────────────────────────────────
BTC_FILTER_ENABLED = True
BTC_PAIR           = "XBTEUR"
BTC_RSI_MAX        = 65
BTC_EMA_FILTER     = True

# ── Correlazione ─────────────────────────────────────────────────────────────
CORR_GROUPS: list = [
    ["SOLEUR", "ETHEUR"],
    ["DOGEEUR", "XRPEUR"],
]

# ── Fear & Greed ─────────────────────────────────────────────────────────────
FEAR_GREED_ENABLED = True
FEAR_GREED_MIN     = 20
FEAR_GREED_MAX     = 80

# ── News sentiment ────────────────────────────────────────────────────────────
NEWS_ENABLED       = os.getenv("NEWS_ENABLED", "true").lower() == "true"
NEWS_HEADLINES     = int(os.getenv("NEWS_HEADLINES", "4"))

# ── On-chain data ─────────────────────────────────────────────────────────────
ONCHAIN_ENABLED    = os.getenv("ONCHAIN_ENABLED", "true").lower() == "true"

# ── Groq confidence e sizing ─────────────────────────────────────────────────
GROQ_MIN_CONFIDENCE = 6
AI_SIZE_HIGH        = float(os.getenv("AI_SIZE_HIGH", "0.25"))
AI_SIZE_MED         = float(os.getenv("AI_SIZE_MED",  "0.18"))
AI_SIZE_MIN         = float(os.getenv("AI_SIZE_MIN",  "0.12"))

# ── Kelly Criterion ───────────────────────────────────────────────────────────
KELLY_ENABLED      = True
KELLY_MIN_TRADES   = int(os.getenv("KELLY_MIN_TRADES", "50"))  # trade minimi prima di attivare Kelly
KELLY_FRACTION     = 0.25   # quarter-Kelly (più conservativo di half-Kelly)
KELLY_MIN_SIZE_PCT = 0.10
KELLY_MAX_SIZE_PCT = 0.25

# ── Daily report ──────────────────────────────────────────────────────────────
DAILY_REPORT_HOUR = int(os.getenv("DAILY_REPORT_HOUR", "8"))

# ── Scan interval ─────────────────────────────────────────────────────────────
SCAN_INTERVAL_MINUTES  = 15
POSITION_CHECK_SECONDS = 60

# ── Pairs ─────────────────────────────────────────────────────────────────────
PAIRS = ["DOGEEUR", "XRPEUR", "SOLEUR", "ETHEUR", "XBTEUR"]

# ── Volume decimal precision per pair ────────────────────────────────────────
PAIR_VOLUME_DECIMALS: dict = {
    "XBTEUR":  8,
    "ETHEUR":  6,
    "SOLEUR":  2,
    "XRPEUR":  0,
    "DOGEEUR": 0,
}

# ── Indicator periods ─────────────────────────────────────────────────────────
RSI_PERIOD  = 14
MACD_FAST   = 12
MACD_SLOW   = 26
MACD_SIGNAL = 9
EMA_FAST    = 20
EMA_SLOW    = 50


def validate():
    missing = []
    if not TELEGRAM_TOKEN:    missing.append("TELEGRAM_TOKEN")
    if not TELEGRAM_CHAT_ID:  missing.append("TELEGRAM_CHAT_ID")
    if not GROQ_API_KEY:      missing.append("GROQ_API_KEY")
    if not PAPER_TRADING:
        if not KRAKEN_API_KEY:    missing.append("KRAKEN_API_KEY")
        if not KRAKEN_API_SECRET: missing.append("KRAKEN_API_SECRET")
    if missing:
        raise EnvironmentError(
            f"Missing required environment variables: {', '.join(missing)}\n"
            "Copy .env.example to .env and fill in the values."
        )
