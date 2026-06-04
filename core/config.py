"""
config.py — Centralised configuration for the Kraken+Telegram trading bot.
All sensitive values come from the .env file. Adjust strategy params here.
"""
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

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
TAKE_PROFIT_PCT  = float(os.getenv("TAKE_PROFIT_PCT", "0.10"))
STOP_LOSS_PCT    = float(os.getenv("STOP_LOSS_PCT",   "0.04"))
SLIPPAGE_PCT     = float(os.getenv("SLIPPAGE_PCT",    "0.001"))

# Exchange fees — set by Kraken, not user-configurable
KRAKEN_TAKER_FEE         = 0.004
KRAKEN_MAKER_FEE         = 0.0025
MARGIN_LEVERAGE          = int(os.getenv("MARGIN_LEVERAGE", "2"))
MARGIN_OPEN_FEE          = 0.0002
MARGIN_ROLLOVER_FEE_PER_4H = 0.0002

# ── Limit orders ──────────────────────────────────────────────────────────────
LIMIT_ORDERS_ENABLED = os.getenv("LIMIT_ORDERS_ENABLED", "true").lower() == "true"
LIMIT_OFFSET_PCT     = float(os.getenv("LIMIT_OFFSET_PCT", "0.001"))
LIMIT_TIMEOUT_SEC    = int(os.getenv("LIMIT_TIMEOUT_SEC", "300"))

# ── ATR-based TP/SL ───────────────────────────────────────────────────────────
ATR_TP_MULT       = float(os.getenv("ATR_TP_MULT",       "2.5"))
ATR_SL_MULT       = float(os.getenv("ATR_SL_MULT",       "1.0"))
ATR_TP_MULT_SHORT = float(os.getenv("ATR_TP_MULT_SHORT", "2.5"))
ATR_SL_MULT_SHORT = float(os.getenv("ATR_SL_MULT_SHORT", "1.3"))  # wider: short squeezes are more violent

# ── Trailing stop ─────────────────────────────────────────────────────────────
TRAILING_STOP_ENABLED   = os.getenv("TRAILING_STOP_ENABLED", "true").lower() == "true"
TRAILING_ACTIVATION_ATR = float(os.getenv("TRAILING_ACTIVATION_ATR", "1.0"))  # ATR multiples in profit before activating
TRAILING_DISTANCE_ATR   = float(os.getenv("TRAILING_DISTANCE_ATR",   "1.5"))  # trailing distance once active
TRAILING_STOP_PCT       = float(os.getenv("TRAILING_STOP_PCT", "0.03"))       # fallback when ATR unavailable

# ── Circuit Breaker ───────────────────────────────────────────────────────────
CIRCUIT_BREAKER_ENABLED  = os.getenv("CIRCUIT_BREAKER_ENABLED", "true").lower() == "true"
CIRCUIT_BREAKER_LOSS_EUR = float(os.getenv("CIRCUIT_BREAKER_LOSS_EUR", "15"))

# ── Signal scoring ────────────────────────────────────────────────────────────
MIN_SIGNAL_SCORE  = int(os.getenv("MIN_SIGNAL_SCORE",  "3"))
VOLUME_SPIKE_MULT = float(os.getenv("VOLUME_SPIKE_MULT", "1.15"))
RSI_1H_BUY_MAX    = int(os.getenv("RSI_1H_BUY_MAX",   "65"))
RSI_15M_BUY_MAX   = int(os.getenv("RSI_15M_BUY_MAX",  "62"))
RSI_4H_BUY_MAX    = int(os.getenv("RSI_4H_BUY_MAX",   "70"))
RSI_SHORT_MIN     = int(os.getenv("RSI_SHORT_MIN",     "35"))

# ── ADX ───────────────────────────────────────────────────────────────────────
ADX_MIN = int(os.getenv("ADX_MIN", "20"))

# ── Multi-timeframe ───────────────────────────────────────────────────────────
DAILY_EMA_FILTER = os.getenv("DAILY_EMA_FILTER", "true").lower() == "true"

# ── BTC market filter ─────────────────────────────────────────────────────────
BTC_FILTER_ENABLED = os.getenv("BTC_FILTER_ENABLED", "true").lower() == "true"
BTC_PAIR           = os.getenv("BTC_PAIR", "XBTEUR")
BTC_RSI_MAX        = int(os.getenv("BTC_RSI_MAX",       "65"))
BTC_RSI_SHORT_MIN  = int(os.getenv("BTC_RSI_SHORT_MIN", "28"))
BTC_EMA_FILTER     = os.getenv("BTC_EMA_FILTER", "true").lower() == "true"

# ── Correlation groups ───────────────────────────────────────────────────────
CORR_GROUPS: list = [
    ["SOLEUR", "ETHEUR"],
    ["DOGEEUR", "XRPEUR"],
]
# BTC-beta bucket: all positions (alts + BTC) count as full BTC-beta exposure.
# Caps long exposure and short exposure separately as a fraction of BUDGET_EUR.
# Set to 1.0 to disable. Example: 0.60 → max 60€ long + max 60€ short on a 100€ budget.
MAX_BTC_BETA_EXPOSURE_PCT = float(os.getenv("MAX_BTC_BETA_EXPOSURE_PCT", "0.60"))

# ── Fear & Greed ─────────────────────────────────────────────────────────────
FEAR_GREED_ENABLED   = os.getenv("FEAR_GREED_ENABLED", "true").lower() == "true"
FEAR_GREED_MIN       = int(os.getenv("FEAR_GREED_MIN",       "20"))   # blocks longs (extreme fear)
FEAR_GREED_MAX       = int(os.getenv("FEAR_GREED_MAX",       "80"))   # blocks both longs and shorts (extreme greed)
FEAR_GREED_SHORT_MIN = int(os.getenv("FEAR_GREED_SHORT_MIN", "20"))   # blocks shorts (extreme fear = capitulation bounce risk)

# ── News sentiment ────────────────────────────────────────────────────────────
NEWS_ENABLED   = os.getenv("NEWS_ENABLED", "true").lower() == "true"
NEWS_HEADLINES = int(os.getenv("NEWS_HEADLINES", "4"))

# ── On-chain data ─────────────────────────────────────────────────────────────
ONCHAIN_ENABLED = os.getenv("ONCHAIN_ENABLED", "true").lower() == "true"

# ── Groq confidence and sizing ───────────────────────────────────────────────
GROQ_MIN_CONFIDENCE = int(os.getenv("GROQ_MIN_CONFIDENCE", "6"))
AI_SIZE_HIGH        = float(os.getenv("AI_SIZE_HIGH", "0.25"))
AI_SIZE_MED         = float(os.getenv("AI_SIZE_MED",  "0.18"))
AI_SIZE_MIN         = float(os.getenv("AI_SIZE_MIN",  "0.12"))

# ── Kelly Criterion ───────────────────────────────────────────────────────────
KELLY_ENABLED      = os.getenv("KELLY_ENABLED", "true").lower() == "true"
KELLY_MIN_TRADES   = int(os.getenv("KELLY_MIN_TRADES",   "50"))
KELLY_FRACTION     = float(os.getenv("KELLY_FRACTION",   "0.25"))
KELLY_MIN_SIZE_PCT = float(os.getenv("KELLY_MIN_SIZE_PCT", "0.10"))
KELLY_MAX_SIZE_PCT = float(os.getenv("KELLY_MAX_SIZE_PCT", "0.25"))

# ── Scan timing ───────────────────────────────────────────────────────────────
SCAN_INTERVAL_MINUTES  = int(os.getenv("SCAN_INTERVAL_MINUTES",  "15"))
POSITION_CHECK_SECONDS = int(os.getenv("POSITION_CHECK_SECONDS", "60"))
DAILY_REPORT_HOUR      = int(os.getenv("DAILY_REPORT_HOUR",      "8"))

# ── Pairs ─────────────────────────────────────────────────────────────────────
PAIRS = [p.strip() for p in os.getenv("PAIRS", "DOGEEUR,XRPEUR,SOLEUR,ETHEUR,XBTEUR").split(",")]

# ── Volume decimal precision per pair ────────────────────────────────────────
PAIR_VOLUME_DECIMALS: dict = {
    "XBTEUR":  8,
    "ETHEUR":  6,
    "SOLEUR":  2,
    "XRPEUR":  0,
    "DOGEEUR": 0,
}

# ── Indicator periods — technical constants, not user-configurable ────────────
RSI_PERIOD  = 14
MACD_FAST   = 12
MACD_SLOW   = 26
MACD_SIGNAL = 9
EMA_FAST    = 20
EMA_SLOW    = 50
ADX_PERIOD  = 14
ATR_PERIOD  = 14


# ── Trading profiles ──────────────────────────────────────────────────────────

PROFILE_CONSERVATIVE: dict = {
    "MAX_OPEN_TRADES":        4,
    "MAX_CAPITAL_DEPLOYED":   0.80,
    "ATR_TP_MULT":            2.5,
    "ATR_SL_MULT":            1.0,
    "TAKE_PROFIT_PCT":        0.10,
    "STOP_LOSS_PCT":          0.04,
    "AI_SIZE_HIGH":           0.25,
    "AI_SIZE_MED":            0.18,
    "AI_SIZE_MIN":            0.12,
    "GROQ_MIN_CONFIDENCE":    6,
    "MIN_SIGNAL_SCORE":       3,
    "CIRCUIT_BREAKER_LOSS_EUR": 15.0,
    "TRAILING_STOP_PCT":      0.03,
    "FEAR_GREED_MIN":         20,
    "FEAR_GREED_MAX":         80,
    "BTC_RSI_MAX":            65,
    "VOLUME_SPIKE_MULT":      1.15,
    "ADX_MIN":                20,
    "RSI_1H_BUY_MAX":         65,
    "RSI_15M_BUY_MAX":        62,
    "RSI_4H_BUY_MAX":         70,
    "KELLY_FRACTION":         0.25,
    "KELLY_MAX_SIZE_PCT":     0.25,
    "RSI_SHORT_MIN":          42,
    "BTC_RSI_SHORT_MIN":      32,
    "ATR_TP_MULT_SHORT":      2.5,
    "ATR_SL_MULT_SHORT":      1.3,
    "TRAILING_ACTIVATION_ATR": 1.0,
    "TRAILING_DISTANCE_ATR":  1.5,
    "FEAR_GREED_SHORT_MIN":   20,
}

PROFILE_MODERATE: dict = {
    "MIN_SIGNAL_SCORE":       2,
    "ADX_MIN":                17,
    "VOLUME_SPIKE_MULT":      1.08,
    "RSI_1H_BUY_MAX":         68,
    "RSI_15M_BUY_MAX":        65,
    "RSI_4H_BUY_MAX":         73,
    "BTC_RSI_MAX":            68,
    "FEAR_GREED_MIN":         15,
    "FEAR_GREED_MAX":         85,
    "GROQ_MIN_CONFIDENCE":    6,
    "MAX_OPEN_TRADES":        5,
    "MAX_CAPITAL_DEPLOYED":   0.87,
    "ATR_TP_MULT":            3.2,
    "ATR_SL_MULT":            1.2,
    "TAKE_PROFIT_PCT":        0.14,
    "STOP_LOSS_PCT":          0.05,
    "AI_SIZE_HIGH":           0.35,
    "AI_SIZE_MED":            0.26,
    "AI_SIZE_MIN":            0.17,
    "CIRCUIT_BREAKER_LOSS_EUR": 22.0,
    "TRAILING_STOP_PCT":      0.04,
    "KELLY_FRACTION":         0.35,
    "KELLY_MAX_SIZE_PCT":     0.35,
    "RSI_SHORT_MIN":          36,
    "BTC_RSI_SHORT_MIN":      28,
    "ATR_TP_MULT_SHORT":      2.8,
    "ATR_SL_MULT_SHORT":      1.4,
    "TRAILING_ACTIVATION_ATR": 0.8,
    "TRAILING_DISTANCE_ATR":  1.5,
    "FEAR_GREED_SHORT_MIN":   15,
}

PROFILE_AGGRESSIVE: dict = {
    # ── Entry filters — lower = more trades, higher noise ratio ──────────────
    "MIN_SIGNAL_SCORE":       2,    # conservative=3: accepts weaker setups
    "ADX_MIN":                15,   # conservative=20: enters ranging markets
    "VOLUME_SPIKE_MULT":      1.05, # conservative=1.15: lower bar for volume confirmation
    # ── RSI thresholds — higher = buys more extended moves ───────────────────
    "RSI_1H_BUY_MAX":         72,
    "RSI_15M_BUY_MAX":        68,
    "RSI_4H_BUY_MAX":         76,
    "BTC_RSI_MAX":            72,
    # ── Sentiment gates — wider window keeps more signals alive ──────────────
    "FEAR_GREED_MIN":         10,   # conservative=20: trades deeper into fear
    "FEAR_GREED_MAX":         90,   # conservative=80: trades deeper into greed
    "FEAR_GREED_SHORT_MIN":   10,
    # ── AI quality gate ───────────────────────────────────────────────────────
    "GROQ_MIN_CONFIDENCE":    5,    # conservative=6: accepts lower-conviction approvals
    # ── Position limits — more simultaneous exposure ──────────────────────────
    "MAX_OPEN_TRADES":        6,    # conservative=4: more concurrent positions
    "MAX_CAPITAL_DEPLOYED":   0.95, # conservative=0.80: nearly full capital deployed
    # ── R:R — wider TP and SL, larger target but survives more noise ──────────
    "ATR_TP_MULT":            4.0,  # conservative=2.5: larger profit target
    "ATR_SL_MULT":            1.5,  # conservative=1.0: wider stop
    "ATR_TP_MULT_SHORT":      3.5,
    "ATR_SL_MULT_SHORT":      1.6,
    "TAKE_PROFIT_PCT":        0.18,
    "STOP_LOSS_PCT":          0.07,
    # ── Position sizing — larger per-trade allocation ─────────────────────────
    "AI_SIZE_HIGH":           0.45, # conservative=0.25: 45% of capital per trade
    "AI_SIZE_MED":            0.35,
    "AI_SIZE_MIN":            0.25,
    # ── Loss protection — allows larger drawdown before halting ───────────────
    "CIRCUIT_BREAKER_LOSS_EUR": 30.0, # conservative=15€: halts after larger daily loss
    # ── Trailing stop — activates sooner, tighter on the way down ────────────
    "TRAILING_STOP_PCT":      0.05,
    "TRAILING_ACTIVATION_ATR": 0.7, # conservative=1.0: trail starts earlier in the move
    "TRAILING_DISTANCE_ATR":  1.5,
    # ── Kelly criterion ───────────────────────────────────────────────────────
    "KELLY_FRACTION":         0.50, # conservative=0.25: bets larger relative to edge
    "KELLY_MAX_SIZE_PCT":     0.45,
    "RSI_SHORT_MIN":          30,
    "BTC_RSI_SHORT_MIN":      25,
}

PROFILES: dict = {
    "conservative": PROFILE_CONSERVATIVE,
    "moderate":     PROFILE_MODERATE,
    "aggressive":   PROFILE_AGGRESSIVE,
}

_ACTIVE_PROFILE_FILE = Path(__file__).parent.parent / "active_profile.json"

ACTIVE_PROFILE: str = "conservative"


def apply_profile(name: str) -> None:
    """Switch trading profile at runtime and persist the choice."""
    import sys, json as _json
    name = name.lower().strip()
    if name not in PROFILES:
        raise ValueError(f"Profilo '{name}' sconosciuto. Scegli: {list(PROFILES)}")
    module = sys.modules[__name__]
    for key, val in PROFILES[name].items():
        setattr(module, key, val)
    setattr(module, "ACTIVE_PROFILE", name)
    try:
        _ACTIVE_PROFILE_FILE.write_text(
            _json.dumps({"profile": name}), encoding="utf-8"
        )
    except Exception:
        pass


def _load_active_profile() -> str:
    import json as _json
    default = os.getenv("TRADING_PROFILE", "conservative").lower()
    try:
        if _ACTIVE_PROFILE_FILE.exists():
            data = _json.loads(_ACTIVE_PROFILE_FILE.read_text(encoding="utf-8"))
            candidate = data.get("profile", default)
            return candidate if candidate in PROFILES else default
    except Exception:
        pass
    return default


apply_profile(_load_active_profile())


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
