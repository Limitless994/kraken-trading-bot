"""
groq_evaluator.py — Groq LLM evaluates each trading signal with a confidence score.

Response format: ESEGUI:8: motivo  or  RIFIUTA:3: motivo
Only executes if confidence >= config.GROQ_MIN_CONFIDENCE.
"""
from __future__ import annotations

import logging

from groq import Groq

from core import config, prompts
from core.strategy import Signal

logger = logging.getLogger(__name__)

_client: Groq = None
_consecutive_fallbacks: int = 0


class GroqUnavailableError(Exception):
    """Raised when Groq fails 3 or more consecutive times."""


def _get_client() -> Groq:
    global _client
    if _client is None:
        _client = Groq(api_key=config.GROQ_API_KEY)
    return _client


def _size_pct(confidence: int) -> float:
    if confidence >= 9:   return config.AI_SIZE_HIGH
    elif confidence >= 8: return config.AI_SIZE_MED
    else:                 return config.AI_SIZE_MIN


def evaluate(signal: Signal, market_ctx: dict = None) -> tuple:
    """
    Returns (execute: bool, confidence: int, reason: str, size_pct: float).
    Falls back to (True, 6, reason, AI_SIZE_MIN) if Groq is unavailable (<3 consecutive).
    Raises GroqUnavailableError after 3 consecutive failures.
    """
    global _consecutive_fallbacks
    is_short = signal.direction == "SELL"
    if is_short:
        tp_pct = (1 - signal.take_profit / signal.price) * 100 if signal.price > 0 else config.TAKE_PROFIT_PCT * 100
        sl_pct = (signal.stop_loss / signal.price - 1) * 100   if signal.price > 0 else config.STOP_LOSS_PCT * 100
    else:
        tp_pct = (signal.take_profit / signal.price - 1) * 100 if signal.price > 0 else config.TAKE_PROFIT_PCT * 100
        sl_pct = (1 - signal.stop_loss / signal.price) * 100   if signal.price > 0 else config.STOP_LOSS_PCT * 100
    rr     = tp_pct / sl_pct if sl_pct > 0 else 2.5
    system = (prompts.EVAL_SYSTEM_SHORT if is_short else prompts.EVAL_SYSTEM_LONG).format(
        tp=tp_pct,
        sl=sl_pct,
        rr=rr,
        size_high=config.AI_SIZE_HIGH * 100,
        size_med=config.AI_SIZE_MED * 100,
        size_min=config.AI_SIZE_MIN * 100,
    )
    reasons_text = "; ".join(signal.reasons)
    rsi_slope    = round(signal.rsi_1h - signal.rsi_1h_prev, 1)
    macd_slope   = signal.macd_hist - signal.macd_hist_prev
    slope_label  = "⬆ in salita" if rsi_slope > 1 else ("⬇ in discesa" if rsi_slope < -1 else "→ piatto")
    user_msg = (
        f"Coppia: {signal.pair}\n"
        f"Prezzo: {signal.price:.6f} EUR\n"
        f"Size: {signal.size_eur:.2f}€ ({signal.size_crypto:.6f} {signal.base_currency})\n"
        f"RSI(1h): {signal.rsi_1h:.1f}  slope(3h): {rsi_slope:+.1f} ({slope_label})\n"
        f"RSI(15m): {signal.rsi_15m:.1f}  |  RSI(4h): {signal.rsi_4h:.1f}  |  RSI(daily): {signal.rsi_daily:.1f}\n"
        f"MACD histogram: {signal.macd_hist:+.6f}  slope: {macd_slope:+.6f}\n"
        f"Volume ratio: {signal.volume_ratio:.2f}×\n"
        f"ADX: {signal.adx:.1f}  |  ATR: {signal.atr:.6f}\n"
        f"Condizioni tecniche: {reasons_text}"
    )

    # Aggiungi contesto mercato se disponibile
    if market_ctx:
        fg      = market_ctx.get("fear_greed_value")
        fgl     = market_ctx.get("fear_greed_label", "")
        btcfr   = market_ctx.get("btc_funding")
        news    = market_ctx.get("news_headlines", [])
        mempool = market_ctx.get("mempool")
        inflow  = market_ctx.get("exchange_inflow")

        if fg is not None:
            user_msg += f"\nFear & Greed: {fg}/100 ({fgl})"
        if btcfr is not None:
            user_msg += f"\nBTC Funding Rate: {btcfr*100:+.4f}%"
        if mempool:
            user_msg += f"\nBTC Mempool: {mempool['tx_count']} tx | fee {mempool['fee_hour']} sat/vB"
        if inflow and inflow.get("high_activity"):
            user_msg += f"\nOn-chain: alta attività (vol/mcap {inflow['vol_mcap_ratio']:.1%})"
        if news:
            headlines = " | ".join(news[:3])
            user_msg += f"\nUltimi titoli crypto: {headlines}"

    try:
        reply = (
            _get_client()
            .chat.completions.create(
                model=config.GROQ_MODEL,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user",   "content": user_msg},
                ],
                temperature=0.1,
                max_tokens=80,
            )
            .choices[0]
            .message.content.strip()
        )
        logger.info("Groq [%s] → %s", signal.pair, reply)

        upper = reply.upper()
        execute = upper.startswith("ESEGUI")

        # Parse confidence score and reason: ESEGUI:8: motivo
        confidence = config.GROQ_MIN_CONFIDENCE  # default
        reason = reply
        parts = reply.split(":", 2)
        if len(parts) >= 2:
            try:
                confidence = int(parts[1].strip())
            except ValueError:
                pass
            reason = parts[2].strip() if len(parts) >= 3 else parts[1].strip()

        if not upper.startswith("ESEGUI") and not upper.startswith("RIFIUTA"):
            logger.warning("Groq formato inatteso per %s: %s", signal.pair, reply)
            return True, config.GROQ_MIN_CONFIDENCE, "risposta inattesa — eseguito per default"

        if execute and confidence < config.GROQ_MIN_CONFIDENCE:
            logger.info(
                "Groq ESEGUI ma confidenza %d < %d — rifiuto",
                confidence, config.GROQ_MIN_CONFIDENCE,
            )
            return False, confidence, f"confidenza {confidence}/10 sotto soglia ({config.GROQ_MIN_CONFIDENCE})", 0.0

        _consecutive_fallbacks = 0
        return execute, confidence, reason, _size_pct(confidence) if execute else 0.0

    except Exception as exc:
        _consecutive_fallbacks += 1
        logger.error(
            "Groq non disponibile per %s: %s — fallback consecutivo %d",
            signal.pair, exc, _consecutive_fallbacks,
        )
        if _consecutive_fallbacks >= 3:
            raise GroqUnavailableError(
                f"Groq non raggiungibile da {_consecutive_fallbacks} tentativi consecutivi"
            ) from exc
        return True, 6, f"Groq offline — eseguito (score {signal.score}/6)", config.AI_SIZE_MIN
