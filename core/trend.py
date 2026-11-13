"""
trend.py — Daily trend-following strategy (TRADING_MODE="trend").

Daily trend-following, shared by every market (crypto on Kraken, stocks/ETFs on
Alpaca) through core/trend_engine.py. Evidence and its limits are in
backtest_trend.py — run it rather than trusting a number quoted here:

  ENTRY (long only): last COMPLETED daily close breaks above the highest close
                     of the previous TREND_BREAKOUT_DAYS days AND is above the
                     daily EMA50.
  EXIT:              chandelier stop — completed daily close drops below
                     (highest close since entry − TREND_CHANDELIER_MULT × ATR20).
                     No take-profit: winners run until the trend dies.
  SIZING:            fixed equal slot per symbol (BUDGET / n_symbols).

CONTRACT: every function here takes COMPLETED daily candles only, oldest first.
The caller owns dropping the forming candle — brokers do it in daily_candles()
(kraken_client.get_ohlc already excludes it). Nothing is dropped here: dropping
it twice made the live bot decide one day late versus the backtest. An
evaluation is therefore idempotent until the next daily close.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from . import config
from .analyzer import _atr, _ema

logger = logging.getLogger(__name__)


@dataclass
class TrendDecision:
    enter: bool
    close: float          # last completed daily close (decision price)
    ema50: float
    breakout_level: float # highest close of the lookback window
    atr: float            # daily ATR (TREND_ATR_DAYS)
    reason: str


def evaluate_entry(daily_candles: list) -> Optional[TrendDecision]:
    """Entry rule on completed daily candles. None when there isn't enough history."""
    days = daily_candles
    lookback = config.TREND_BREAKOUT_DAYS
    need = max(lookback + 1, config.TREND_EMA_DAYS, config.TREND_ATR_DAYS + 2)
    if len(days) < need:
        return None

    closes = [c["close"] for c in days]
    highs  = [c["high"]  for c in days]
    lows   = [c["low"]   for c in days]

    close   = closes[-1]
    hh      = max(closes[-lookback - 1:-1])   # highest close BEFORE today
    ema50   = _ema(closes, config.TREND_EMA_DAYS)[-1]
    atr_d   = _atr(highs, lows, closes, config.TREND_ATR_DAYS)

    if close > hh and close > ema50:
        return TrendDecision(
            enter=True, close=close, ema50=ema50, breakout_level=hh, atr=atr_d,
            reason=(f"Breakout Donchian {lookback}d ({close:.6g} > {hh:.6g}), "
                    f"sopra EMA{config.TREND_EMA_DAYS} ({ema50:.6g})"),
        )
    return TrendDecision(
        enter=False, close=close, ema50=ema50, breakout_level=hh, atr=atr_d,
        reason=(f"no breakout ({close:.6g} <= {hh:.6g})" if close <= hh
                else f"sotto EMA{config.TREND_EMA_DAYS}"),
    )


def chandelier_stop(peak_close: float, atr_daily: float) -> float:
    """Stop level: highest close since entry minus N daily ATRs."""
    return peak_close - config.TREND_CHANDELIER_MULT * atr_daily


def evaluate_exit(daily_candles: list, peak_close: float) -> Optional[dict]:
    """Exit rule on completed daily candles.

    Returns {'exit': bool, 'close': float, 'peak': float, 'stop': float, 'atr': float}
    with peak updated to include the latest completed close, or None when there
    is not enough data (caller should keep the position untouched).
    """
    days = daily_candles
    if len(days) < config.TREND_ATR_DAYS + 2:
        return None

    closes = [c["close"] for c in days]
    highs  = [c["high"]  for c in days]
    lows   = [c["low"]   for c in days]

    close  = closes[-1]
    peak   = max(peak_close, close)
    atr_d  = _atr(highs, lows, closes, config.TREND_ATR_DAYS)
    stop   = chandelier_stop(peak, atr_d)

    return {"exit": close < stop, "close": close, "peak": peak, "stop": stop, "atr": atr_d}
