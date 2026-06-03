"""
discovery.py — Daily scan di tutte le coppie EUR su Kraken per trovare nuove opportunità.

Pipeline:
  1. Fetch tutte le coppie EUR disponibili su Kraken
  2. Filtra per volume minimo (liquidità)
  3. Backtest meccanico su 90 giorni di candele 4h (puro Python, zero Groq)
  4. Score meccanico per ogni coppia
  5. Top 10 → UNA sola chiamata Groq per l'analisi finale
  6. Risultati salvati + notifica Telegram
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from groq import Groq

import config
import kraken_client
from analyzer import _adx, _ema, _macd_histogram, _rsi, _volume_ratio

logger = logging.getLogger(__name__)

DISCOVERY_MIN_VOLUME_EUR  = 500_000    # volume minimo EUR 24h
DISCOVERY_MAX_PAIRS       = 20         # scansiona max 20 coppie per velocità
DISCOVERY_TOP_N           = 8          # candidate da mandare a Groq
BACKTEST_INTERVAL         = 240        # candele 4h

# Soglie discovery più permissive rispetto al bot live
# (vogliamo trovare opportunità potenziali, non già-filtrate)
_BT_RSI_MAX     = 55      # vs 48 live
_BT_VOL_MULT    = 1.2     # vs 1.5 live
_BT_ADX_MIN     = 15      # vs 20 live
_BT_MIN_CANDLES = 100     # vs 150 live
_BT_MIN_SIGNALS = 1       # almeno 1 segnale storico


# ── Kraken pair list ──────────────────────────────────────────────────────────

def _get_all_eur_pairs() -> list:
    try:
        result = kraken_client._public("AssetPairs")
        pairs = set()
        for _, info in result.items():
            altname = info.get("altname", "")
            if altname.endswith("EUR") and info.get("status") == "online" and len(altname) > 3:
                pairs.add(altname)
        return sorted(pairs)
    except Exception as exc:
        logger.error("Impossibile recuperare coppie EUR: %s", exc)
        return []


def _filter_by_volume(pairs: list, min_eur: float) -> list:
    qualified = []
    for pair in pairs:
        try:
            ticker = kraken_client.get_ticker(pair)
            vol_eur = ticker["volume_24h"] * ticker["last"]
            if vol_eur >= min_eur:
                qualified.append(pair)
            time.sleep(0.35)
        except Exception:
            continue
    return qualified


# ── Backtest ──────────────────────────────────────────────────────────────────

def _sharpe(returns: list) -> float:
    """Sharpe ratio annualizzato (assumendo risk-free rate = 0)."""
    if len(returns) < 2:
        return 0.0
    n    = len(returns)
    mean = sum(returns) / n
    var  = sum((r - mean) ** 2 for r in returns) / (n - 1)
    std  = var ** 0.5
    if std == 0:
        return 0.0
    # Annualizzato per trade (approssimazione: ~8 trade/mese × 12 mesi)
    return round((mean / std) * (96 ** 0.5), 2)


def _run_backtest_slice(candles: list) -> dict:
    """Esegue backtest su uno slice di candele e ritorna statistiche."""
    closes  = [c["close"]  for c in candles]
    highs   = [c["high"]   for c in candles]
    lows    = [c["low"]    for c in candles]
    volumes = [c["volume"] for c in candles]

    signals_tried = signals_won = signals_lost = 0
    returns: list = []
    min_idx = max(config.EMA_SLOW + config.MACD_SIGNAL + 5, 60)

    for i in range(min_idx, len(candles) - 30):
        cs = closes[:i+1]; vs = volumes[:i+1]; hs = highs[:i+1]; ls = lows[:i+1]
        rsi = _rsi(cs, config.RSI_PERIOD)
        if rsi >= _BT_RSI_MAX:
            continue
        hist = _macd_histogram(cs, config.MACD_FAST, config.MACD_SLOW, config.MACD_SIGNAL)
        if len(hist) < 2:
            continue
        if not (hist[-1] > 0 and hist[-2] <= 0) and not (hist[-1] > 0 and hist[-1] > hist[-2]):
            continue
        if _volume_ratio(vs) < _BT_VOL_MULT:
            continue
        ema50 = _ema(cs, config.EMA_SLOW)
        if not ema50 or cs[-1] < ema50[-1]:
            continue
        if _adx(hs, ls, cs, config.ADX_PERIOD) < _BT_ADX_MIN:
            continue

        entry = closes[i]
        tp    = entry * (1 + config.TAKE_PROFIT_PCT)
        sl    = entry * (1 - config.STOP_LOSS_PCT)
        signals_tried += 1
        outcome = None
        for j in range(i + 1, min(i + 31, len(candles))):
            if candles[j]["high"] >= tp:
                outcome = "win"
                ret = config.TAKE_PROFIT_PCT - config.KRAKEN_TAKER_FEE * 2
                returns.append(ret)
                break
            if candles[j]["low"] <= sl:
                outcome = "loss"
                ret = -(config.STOP_LOSS_PCT + config.KRAKEN_TAKER_FEE * 2)
                returns.append(ret)
                break
        if outcome == "win":   signals_won  += 1
        elif outcome == "loss": signals_lost += 1

    win_rate  = signals_won / signals_tried * 100 if signals_tried else 0
    avg_pnl   = sum(returns) / len(returns) * 100 if returns else 0
    max_dd    = 0.0
    peak = 0.0
    cum  = 0.0
    for r in returns:
        cum  += r
        peak  = max(peak, cum)
        max_dd = max(max_dd, peak - cum)

    return {
        "signals_tried": signals_tried,
        "signals_won":   signals_won,
        "win_rate":      round(win_rate, 1),
        "avg_pnl_pct":   round(avg_pnl, 2),
        "sharpe":        _sharpe(returns),
        "max_drawdown":  round(max_dd * 100, 2),
    }


def _backtest_pair(pair: str) -> Optional[dict]:
    """
    Walk-forward backtest su candele 4h:
    - Train: primi 70% delle candele
    - Test:  ultimi 30% delle candele
    Calcola Sharpe ratio, max drawdown, win rate su entrambi.
    Flag di consistenza: test/train ratio < 0.7 = possibile overfitting.
    """
    try:
        candles = kraken_client.get_ohlc(pair, interval=BACKTEST_INTERVAL)
        time.sleep(0.6)

        if len(candles) < _BT_MIN_CANDLES:
            return None

        # Walk-forward split
        split     = int(len(candles) * 0.70)
        train     = candles[:split]
        test      = candles[split:]

        train_stats = _run_backtest_slice(train)
        test_stats  = _run_backtest_slice(test)

        if train_stats["signals_tried"] < 3:
            return None

        # Consistenza: quanto il test si avvicina al train
        if train_stats["win_rate"] > 0:
            consistency = test_stats["win_rate"] / train_stats["win_rate"]
        else:
            consistency = 0.0

        # Stato attuale
        closes    = [c["close"]  for c in candles]
        highs     = [c["high"]   for c in candles]
        lows      = [c["low"]    for c in candles]
        volumes   = [c["volume"] for c in candles]

        rsi_now   = _rsi(closes, config.RSI_PERIOD)
        hist_now  = _macd_histogram(closes, config.MACD_FAST, config.MACD_SLOW, config.MACD_SIGNAL)
        adx_now   = _adx(highs, lows, closes, config.ADX_PERIOD)
        ema50_now = _ema(closes, config.EMA_SLOW)
        vol_now   = _volume_ratio(volumes)

        price_chg = (closes[-1] - closes[0]) / closes[0] * 100 if closes[0] > 0 else 0.0
        vol_trend = 0.0
        if len(volumes) >= 60:
            avg_r = sum(volumes[-30:]) / 30
            avg_p = sum(volumes[-60:-30]) / 30
            vol_trend = (avg_r - avg_p) / avg_p * 100 if avg_p > 0 else 0.0

        return {
            "pair":            pair,
            # Walk-forward
            "train_win_rate":  train_stats["win_rate"],
            "train_signals":   train_stats["signals_tried"],
            "train_sharpe":    train_stats["sharpe"],
            "train_max_dd":    train_stats["max_drawdown"],
            "test_win_rate":   test_stats["win_rate"],
            "test_signals":    test_stats["signals_tried"],
            "test_sharpe":     test_stats["sharpe"],
            "consistency":     round(consistency, 2),
            # Aggregato (tutti i dati)
            "signals_tried":   train_stats["signals_tried"] + test_stats["signals_tried"],
            "signals_won":     train_stats["signals_won"]   + test_stats["signals_won"],
            "win_rate":        round(
                (train_stats["signals_won"] + test_stats["signals_won"]) /
                max(train_stats["signals_tried"] + test_stats["signals_tried"], 1) * 100, 1
            ),
            "avg_pnl_pct":     round((train_stats["avg_pnl_pct"] + test_stats["avg_pnl_pct"]) / 2, 2),
            # Stato attuale
            "price_chg_90d":   round(price_chg, 2),
            "vol_trend_pct":   round(vol_trend, 1),
            "rsi_now":         round(rsi_now, 1),
            "macd_now":        round(hist_now[-1] if hist_now else 0, 8),
            "adx_now":         round(adx_now, 1),
            "above_ema50":     closes[-1] >= ema50_now[-1] if ema50_now else False,
            "vol_ratio_now":   round(vol_now, 2),
            "price":           closes[-1],
        }

    except Exception as exc:
        logger.error("Backtest %s fallito: %s", pair, exc)
        return None


# ── Mechanical score ──────────────────────────────────────────────────────────

def _mech_score(s: dict) -> float:
    score = 0.0
    score += min(s["win_rate"] / 100 * 25, 25)           # win rate 0-25

    # Walk-forward consistency bonus
    cons = s.get("consistency", 1.0)
    if cons >= 0.9:   score += 15
    elif cons >= 0.7: score += 8
    elif cons < 0.5:  score -= 10                         # penalità overfitting

    # Sharpe ratio
    sharpe = s.get("test_sharpe", 0)
    if sharpe >= 1.5:   score += 15
    elif sharpe >= 0.5: score += 8

    monthly = s["signals_tried"] / 3
    if 2 <= monthly <= 8: score += 10
    elif monthly > 0:     score += 5

    if s["rsi_now"] < 35:                           score += 10
    elif s["rsi_now"] < config.RSI_1H_BUY_MAX:     score += 6

    if s["adx_now"] >= 30:                          score += 10
    elif s["adx_now"] >= config.ADX_MIN:            score += 5

    if s["vol_trend_pct"] > 20:  score += 8
    elif s["vol_trend_pct"] > 0: score += 4

    if s["above_ema50"]:         score += 7

    return round(max(score, 0), 1)


# ── Groq evaluation (singola chiamata per tutte le candidate) ─────────────────

def _groq_rank(candidates: list) -> str:
    try:
        client = Groq(api_key=config.GROQ_API_KEY)

        lines = []
        for i, c in enumerate(candidates, 1):
            lines.append(
                f"{i}. {c['pair']}\n"
                f"   Walk-forward: train {c['train_win_rate']}% | test {c['test_win_rate']}% | "
                f"consistenza {c['consistency']:.0%} | Sharpe {c.get('test_sharpe', 0):.2f}\n"
                f"   P&L medio {c['avg_pnl_pct']:+.2f}% | prezzo 90gg {c['price_chg_90d']:+.1f}%\n"
                f"   Ora: RSI {c['rsi_now']} | ADX {c['adx_now']} | "
                f"Vol {c['vol_ratio_now']}× | {'↑EMA50' if c['above_ema50'] else '↓EMA50'}"
            )

        prompt = (
            "Strategia: momentum crypto/EUR, TP +10%, SL -4%, R:R 2.5:1.\n"
            "Dati backtest 90gg + situazione attuale per queste coppie:\n\n"
            + "\n\n".join(lines)
            + "\n\nSeleziona le TOP 3 più promettenti. "
            "Per ognuna: nome + 1 riga motivazione. Sii conciso."
        )

        resp = client.chat.completions.create(
            model=config.GROQ_MODEL,
            messages=[
                {"role": "system", "content": "Analista quantitativo crypto. Rispondi in italiano."},
                {"role": "user",   "content": prompt},
            ],
            temperature=0.2,
            max_tokens=250,
        )
        return resp.choices[0].message.content.strip()

    except Exception as exc:
        logger.error("Groq discovery fallito: %s", exc)
        return f"Groq non disponibile: {exc}"


# ── Main ──────────────────────────────────────────────────────────────────────

def run_discovery() -> dict:
    logger.info("Discovery scan avviato...")

    all_pairs = _get_all_eur_pairs()
    skip      = set(config.PAIRS)
    to_scan   = [p for p in all_pairs if p not in skip]
    logger.info("Coppie EUR: %d totali, %d da scansionare", len(all_pairs), len(to_scan))

    liquid = _filter_by_volume(to_scan, DISCOVERY_MIN_VOLUME_EUR)
    liquid = liquid[:DISCOVERY_MAX_PAIRS]   # cap per velocità
    logger.info("Con liquidità sufficiente (cap %d): %d", DISCOVERY_MAX_PAIRS, len(liquid))

    if not liquid:
        return {"error": "Nessuna coppia liquida trovata", "candidates": [], "timestamp": time.time()}

    stats_list = []
    for pair in liquid:
        logger.info("Backtest %s ...", pair)
        s = _backtest_pair(pair)
        if s and s["signals_tried"] >= _BT_MIN_SIGNALS:
            s["mech_score"] = _mech_score(s)
            stats_list.append(s)
        time.sleep(0.3)

    if not stats_list:
        return {"error": "Backtest insufficiente su tutte le coppie", "candidates": [], "timestamp": time.time()}

    stats_list.sort(key=lambda x: x["mech_score"], reverse=True)
    top = stats_list[:DISCOVERY_TOP_N]

    logger.info("Top candidate: %s", [c["pair"] for c in top])

    groq_analysis = _groq_rank(top)

    result = {
        "timestamp":      time.time(),
        "pairs_scanned":  len(liquid),
        "candidates":     top,
        "groq_analysis":  groq_analysis,
    }
    logger.info("Discovery completata.\n%s", groq_analysis)
    return result


# ── Standalone entry point ────────────────────────────────────────────────────

if __name__ == "__main__":
    import json
    import sys
    from pathlib import Path
    from logging.handlers import RotatingFileHandler

    _dir = Path(__file__).parent
    (_dir / "logs").mkdir(exist_ok=True)

    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        level=logging.INFO,
        handlers=[
            logging.StreamHandler(sys.stdout),
            RotatingFileHandler(
                _dir / "logs" / "discovery.log",
                maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8",
            ),
        ],
    )

    result = run_discovery()

    out_file = _dir / "discovery_log.json"
    out_file.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Salvato in {out_file}")

    if result.get("groq_analysis"):
        print("\n--- Groq Analysis ---")
        print(result["groq_analysis"])

    # Notifica Telegram opzionale
    try:
        import requests
        msg = (
            f"DISCOVERY COMPLETATA\n"
            f"Coppie analizzate: {result.get('pairs_scanned', 0)}\n\n"
            f"{result.get('groq_analysis', 'N/A')}"
        )
        requests.post(
            f"https://api.telegram.org/bot{config.TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": config.TELEGRAM_CHAT_ID, "text": msg},
            timeout=5,
        )
    except Exception as e:
        print(f"Telegram notify failed: {e}")
