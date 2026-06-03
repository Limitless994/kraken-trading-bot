"""
strategy.py — Signal generation from an Analysis.

Scoring system (6 points max, threshold = config.MIN_SIGNAL_SCORE = 3):

  1. RSI multi-TF (1h AND 15m AND 4h below thresholds)  → 1 pt  (collapsed from 3 into 1)
  2. MACD bullish crossover (prev≤0→now>0)               → 1 pt  (reduced from 2: lagging indicator)
     MACD rising above zero                              → 1 pt
  3. Volume spike ≥ VOLUME_SPIKE_MULT                    → 1 pt
  4. Price above EMA50                                   → 1 pt
  5. Order book bullish (bid/ask > 0.55)                 → 1 pt
  6. Strong ADX (> 30, above gate of 20)                 → 1 pt

Hard gates (return None regardless of score):
  - ADX < ADX_MIN          → ranging market, false signals
  - Daily EMA50            → structural downtrend
  - BTC filter             → BTC bearish, blocks all longs
  - Correlation            → another pair from the same group already open

Risk/Reward:
  Take Profit = entry + ATR_TP_MULT × ATR   (default 2.5×)
  Stop Loss   = entry − ATR_SL_MULT × ATR   (default 1.0×)
  R:R ≈ 2.5 : 1
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Optional

from . import config
from .analyzer import Analysis

logger = logging.getLogger(__name__)


@dataclass
class Signal:
    id: str
    pair: str
    direction: str
    price: float
    size_eur: float
    size_crypto: float
    take_profit: float
    stop_loss: float
    trailing_stop: float
    rsi_1h: float
    rsi_15m: float
    macd_hist: float
    volume_ratio: float
    adx: float
    atr: float
    score: int
    reasons: list
    base_currency: str
    blocked_reason: str = ""


def _base_currency(pair: str) -> str:
    pair = pair.upper()
    for quote in ("EUR", "USD", "USDT", "USDC", "XBT"):
        if pair.endswith(quote) and pair != quote:
            base = pair[:-len(quote)]
            return {"X": "XBT", "XXBT": "BTC"}.get(base, base)
    return pair


def _corr_group(pair: str) -> Optional[str]:
    """Returns the correlation group the pair belongs to, or None."""
    for group in config.CORR_GROUPS:
        if pair in group:
            return str(group)
    return None


def generate_signal(
    analysis: Analysis,
    available_eur: float,
    already_open: set = None,
    btc_analysis: Analysis = None,
    open_pairs: set = None,
) -> Optional[Signal]:
    """
    Evaluates the analysis and returns a Signal if all conditions are met.
    Returns None if a hard gate blocks the signal.
    """
    if already_open and analysis.pair in already_open:
        return None

    # ── Gate 0: Daily EMA50 — long-term trend filter ─────────────────────────
    if config.DAILY_EMA_FILTER and not analysis.daily_above_ema50:
        logger.info("%s: SKIP gate=daily_EMA50", analysis.pair)
        return None   # price below daily EMA50 = structural downtrend

    # ── Gate 1: ADX — trend strength ─────────────────────────────────────────
    if analysis.adx < config.ADX_MIN:
        logger.info("%s: SKIP gate=ADX_basso (ADX=%.1f < %d)", analysis.pair, analysis.adx, config.ADX_MIN)
        return None   # ranging market

    # ── Gate 2: BTC market filter ─────────────────────────────────────────────
    if config.BTC_FILTER_ENABLED and btc_analysis and analysis.pair != config.BTC_PAIR:
        if btc_analysis.rsi_1h > config.BTC_RSI_MAX:
            logger.info("%s: SKIP gate=BTC_RSI_alto (BTC_RSI=%.1f > %d)", analysis.pair, btc_analysis.rsi_1h, config.BTC_RSI_MAX)
            return None   # overheated market
        if config.BTC_EMA_FILTER and not btc_analysis.price_above_ema50:
            logger.info("%s: SKIP gate=BTC_downtrend", analysis.pair)
            return None   # BTC in downtrend

    # ── Gate 3: correlation ───────────────────────────────────────────────────
    if open_pairs:
        my_group = _corr_group(analysis.pair)
        if my_group:
            for grp in config.CORR_GROUPS:
                if analysis.pair in grp:
                    for other in grp:
                        if other != analysis.pair and other in open_pairs:
                            logger.info("%s: SKIP gate=correlazione (%s già aperta)", analysis.pair, other)
                            return None   # correlated pair already open

    # ── Scoring ───────────────────────────────────────────────────────────────
    score = 0
    reasons: list = []

    # Scoring flags — computed once, used both in the score and in logging
    _rsi_ok   = (analysis.rsi_1h  < config.RSI_1H_BUY_MAX and
                 analysis.rsi_15m < config.RSI_15M_BUY_MAX and
                 analysis.rsi_4h  < config.RSI_4H_BUY_MAX)
    crossed_above = analysis.macd_hist > 0 and analysis.macd_hist_prev <= 0
    rising_above  = analysis.macd_hist > 0 and analysis.macd_hist_slope > 0
    _macd_ok  = crossed_above or rising_above
    _vol_ok   = analysis.volume_ratio >= config.VOLUME_SPIKE_MULT
    _ema_ok   = analysis.price_above_ema50
    _ob_ok    = analysis.ob_bullish
    _adx30_ok = analysis.adx >= 30

    # 1. RSI multi-timeframe: all three thresholds must pass (1 total point)
    if _rsi_ok:
        score += 1
        reasons.append(
            f"RSI multi-TF: 1h={analysis.rsi_1h:.1f}  "
            f"15m={analysis.rsi_15m:.1f}  4h={analysis.rsi_4h:.1f}"
        )

    # 2. MACD histogram — crossover worth 1 pt (lagging indicator, weight reduced from 2)
    if crossed_above:
        score += 1
        reasons.append(
            f"MACD crossover rialzista ({analysis.macd_hist_prev:+.6f} → {analysis.macd_hist:+.6f})"
        )
    elif rising_above:
        score += 1
        reasons.append(f"MACD sopra zero e in salita (+{analysis.macd_hist_slope:.6f})")

    # 3. Volume spike
    if _vol_ok:
        score += 1
        reasons.append(f"Volume spike: {analysis.volume_ratio:.1f}× la media")

    # 4. EMA50 trend filter
    if _ema_ok:
        score += 1
        reasons.append(f"Prezzo ({analysis.price:.6f}) > EMA50 ({analysis.ema50:.6f})")

    # 5. Order book bullish pressure
    if _ob_ok:
        score += 1
        reasons.append(f"Order book bullish: {analysis.ob_imbalance:.2f} (>0.55)")

    # 6. ADX strength bonus (above the ADX_MIN=20 gate)
    if _adx30_ok:
        score += 1
        reasons.append(f"ADX forte: {analysis.adx:.1f} (>30)")

    # ── Score gate ────────────────────────────────────────────────────────────
    if score < config.MIN_SIGNAL_SCORE:
        _ob_val = f"{analysis.ob_imbalance:.2f}" if analysis.ob_imbalance is not None else "N/A"
        logger.info(
            "%s: score=%d/6 [RSI=%d MACD=%d vol=%d(%.1f×) EMA50=%d OB=%d(%s) ADX30=%d(%.1f)] "
            "→ SKIP (min=%d)",
            analysis.pair, score,
            _rsi_ok, _macd_ok, _vol_ok, analysis.volume_ratio,
            _ema_ok, _ob_ok, _ob_val, _adx30_ok, analysis.adx,
            config.MIN_SIGNAL_SCORE,
        )
        return None

    # ── Position sizing (Groq will redefine size after evaluation) ──────────
    size_eur = max(round(available_eur * config.AI_SIZE_HIGH, 2), 10.0)
    if size_eur > available_eur:
        return None

    size_crypto = size_eur / analysis.price

    # ── ATR-based TP/SL: TP = entry + k_tp×ATR, SL = entry − k_sl×ATR, R:R ≈ 2.5:1
    atr = analysis.atr
    if atr and atr > 0:
        take_profit = round(analysis.price + config.ATR_TP_MULT * atr, 8)
        stop_loss   = round(analysis.price - config.ATR_SL_MULT * atr, 8)
    else:
        take_profit = round(analysis.price * (1 + config.TAKE_PROFIT_PCT), 8)
        stop_loss   = round(analysis.price * (1 - config.STOP_LOSS_PCT),   8)

    return Signal(
        id=str(uuid.uuid4())[:8],
        pair=analysis.pair,
        direction="BUY",
        price=analysis.price,
        size_eur=size_eur,
        size_crypto=size_crypto,
        take_profit=take_profit,
        stop_loss=stop_loss,
        trailing_stop=stop_loss,   # initially matches the fixed SL
        rsi_1h=analysis.rsi_1h,
        rsi_15m=analysis.rsi_15m,
        macd_hist=analysis.macd_hist,
        volume_ratio=analysis.volume_ratio,
        adx=analysis.adx,
        atr=analysis.atr,
        score=score,
        reasons=reasons,
        base_currency=_base_currency(analysis.pair),
    )


def generate_short_signal(
    analysis: Analysis,
    available_eur: float,
    already_open: set = None,
    btc_analysis: Analysis = None,
    open_pairs: set = None,
) -> Optional[Signal]:
    """SHORT signal (SELL margin). Inverse conditions relative to generate_signal."""
    if already_open and analysis.pair in already_open:
        return None

    # Gate 0: Daily EMA50 — for short the price must be BELOW EMA50
    if config.DAILY_EMA_FILTER and analysis.daily_above_ema50:
        logger.info("%s: SHORT SKIP gate=daily_EMA50_sopra", analysis.pair)
        return None

    # Gate 1: ADX — strong trend in any direction
    if analysis.adx < config.ADX_MIN:
        logger.info("%s: SHORT SKIP gate=ADX_basso (%.1f < %d)", analysis.pair, analysis.adx, config.ADX_MIN)
        return None

    # Gate 2: Inverted BTC filter — BTC must be in downtrend to short alts
    if config.BTC_FILTER_ENABLED and btc_analysis and analysis.pair != config.BTC_PAIR:
        if btc_analysis.rsi_1h < config.BTC_RSI_SHORT_MIN:
            logger.info("%s: SHORT SKIP gate=BTC_oversold (BTC_RSI=%.1f < %d)", analysis.pair, btc_analysis.rsi_1h, config.BTC_RSI_SHORT_MIN)
            return None
        if config.BTC_EMA_FILTER and btc_analysis.price_above_ema50:
            logger.info("%s: SHORT SKIP gate=BTC_uptrend", analysis.pair)
            return None

    # Gate 3: correlation
    if open_pairs:
        for grp in config.CORR_GROUPS:
            if analysis.pair in grp:
                for other in grp:
                    if other != analysis.pair and other in open_pairs:
                        logger.info("%s: SHORT SKIP gate=correlazione (%s già aperta)", analysis.pair, other)
                        return None

    # Gate 4: RSI not yet oversold — avoid shorting into capitulation
    if analysis.rsi_1h < config.RSI_SHORT_MIN:
        logger.info("%s: SHORT SKIP gate=RSI_oversold (%.1f < %d)", analysis.pair, analysis.rsi_1h, config.RSI_SHORT_MIN)
        return None

    # ── Scoring (symmetric to longs, conditions inverted) ────────────────────
    score = 0
    reasons: list = []

    _rsi_ok       = analysis.rsi_1h > config.RSI_SHORT_MIN   # not oversold, bearish trend
    crossed_below = analysis.macd_hist < 0 and analysis.macd_hist_prev >= 0
    falling_below = analysis.macd_hist < 0 and analysis.macd_hist_slope < 0
    _macd_ok      = crossed_below or falling_below
    _vol_ok       = analysis.volume_ratio >= config.VOLUME_SPIKE_MULT
    _ema_ok       = not analysis.price_above_ema50   # price below EMA50
    _ob_ok        = analysis.ob_bearish              # selling pressure
    _adx30_ok     = analysis.adx >= 30

    if _rsi_ok:
        score += 1
        reasons.append(
            f"RSI bearish: 1h={analysis.rsi_1h:.1f}  "
            f"15m={analysis.rsi_15m:.1f}  4h={analysis.rsi_4h:.1f}"
        )
    if crossed_below:
        score += 1
        reasons.append(f"MACD crossover ribassista ({analysis.macd_hist_prev:+.6f} → {analysis.macd_hist:+.6f})")
    elif falling_below:
        score += 1
        reasons.append(f"MACD sotto zero e in discesa ({analysis.macd_hist_slope:.6f})")
    if _vol_ok:
        score += 1
        reasons.append(f"Volume spike: {analysis.volume_ratio:.1f}× la media")
    if _ema_ok:
        score += 1
        reasons.append(f"Prezzo ({analysis.price:.6f}) < EMA50 ({analysis.ema50:.6f})")
    if _ob_ok:
        score += 1
        reasons.append(f"Order book bearish: {analysis.ob_imbalance:.2f} (<0.45)")
    if _adx30_ok:
        score += 1
        reasons.append(f"ADX forte: {analysis.adx:.1f} (>30)")

    if score < config.MIN_SIGNAL_SCORE:
        logger.info("%s: SHORT score=%d/6 → SKIP (min=%d)", analysis.pair, score, config.MIN_SIGNAL_SCORE)
        return None

    size_eur = max(round(available_eur * config.AI_SIZE_HIGH, 2), 10.0)
    if size_eur > available_eur:
        return None

    size_crypto = size_eur / analysis.price

    atr = analysis.atr
    if atr and atr > 0:
        take_profit = round(analysis.price - config.ATR_TP_MULT * atr, 8)
        stop_loss   = round(analysis.price + config.ATR_SL_MULT * atr, 8)
    else:
        take_profit = round(analysis.price * (1 - config.TAKE_PROFIT_PCT), 8)
        stop_loss   = round(analysis.price * (1 + config.STOP_LOSS_PCT),   8)

    take_profit = max(take_profit, analysis.price * 0.01)

    return Signal(
        id=str(uuid.uuid4())[:8],
        pair=analysis.pair,
        direction="SELL",
        price=analysis.price,
        size_eur=size_eur,
        size_crypto=size_crypto,
        take_profit=take_profit,
        stop_loss=stop_loss,
        trailing_stop=stop_loss,   # for short, starts at SL and moves down with price
        rsi_1h=analysis.rsi_1h,
        rsi_15m=analysis.rsi_15m,
        macd_hist=analysis.macd_hist,
        volume_ratio=analysis.volume_ratio,
        adx=analysis.adx,
        atr=analysis.atr,
        score=score,
        reasons=reasons,
        base_currency=_base_currency(analysis.pair),
    )


def kelly_size_pct(trades: list) -> Optional[float]:
    """Half-Kelly fraction based on trade history. Returns None if data is insufficient."""
    if not config.KELLY_ENABLED or len(trades) < config.KELLY_MIN_TRADES:
        return None
    wins   = [t for t in trades if t.get("pnl_eur", 0) > 0]
    losses = [t for t in trades if t.get("pnl_eur", 0) < 0]
    if not wins or not losses:
        return None
    p       = len(wins) / len(trades)
    avg_win = sum(t["pnl_eur"] for t in wins) / len(wins)
    avg_los = abs(sum(t["pnl_eur"] for t in losses) / len(losses))
    b       = avg_win / avg_los if avg_los > 0 else 1.0
    kelly   = (p * b - (1 - p)) / b
    half    = kelly * config.KELLY_FRACTION
    return round(max(min(half, config.KELLY_MAX_SIZE_PCT), config.KELLY_MIN_SIZE_PCT), 3)
