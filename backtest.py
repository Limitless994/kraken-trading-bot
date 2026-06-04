"""
backtest.py — Historical backtest for the Kraken trading bot.

KNOWN LIMITS (printed at the top of every report):
  1. Order book imbalance not available historically.
     ob_imbalance = None for every candle → ob_bullish = ob_bearish = False.
     Max achievable score in backtest: 5/6 (not 6/6 as in live).
  2. Groq AI replaced by deterministic rule:
       execute = True (signal already passed scoring gate)
       size_pct = AI_SIZE_MED (fixed)
     This measures technical gate + scoring logic, NOT the AI's filtering.

Usage:
    python backtest.py                          # download + run all 3 profiles
    python backtest.py --no-download            # reuse cached data
    python backtest.py --start 2022-01-01       # narrow date range
    python backtest.py --budget 200             # different budget
    python backtest.py --profiles conservative  # single profile
"""
from __future__ import annotations

import argparse
import bisect
import json
import os
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

import requests

# ── Bootstrap: set env vars before importing the bot modules ─────────────────
ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

for _k, _v in [
    ("TELEGRAM_TOKEN",    "fake:token"),
    ("TELEGRAM_CHAT_ID",  "0"),
    ("GROQ_API_KEY",      "fake"),
    ("PAPER_TRADING",     "true"),
    ("KRAKEN_API_KEY",    ""),
    ("KRAKEN_API_SECRET", ""),
]:
    os.environ.setdefault(_k, _v)

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

from core import config as cfg
from core.analyzer import Analysis, _rsi, _ema, _macd_histogram, _volume_ratio, _adx, _atr
from core.strategy import generate_signal, generate_short_signal
from bot.telegram_bot import (
    _update_trailing_stop,
    _calc_exit,
    _funding_cost_eur_pure,   # noqa (available after refactor)
)

# ── Regime labels (edit boundaries to match your market view) ─────────────────
REGIMES = [
    ("bear",   date(2022,  1,  1), date(2022, 12, 31)),
    ("bull",   date(2023,  1,  1), date(2024,  3, 31)),
    ("recent", date(2024,  4,  1), date(2099,  1,  1)),
]

WARMUP_CANDLES_1H = 60    # 1h candles consumed before signal generation starts
MAX_LOOKBACK      = 300   # sliding window for indicator computation
CACHE_DIR         = ROOT / "backtest_data"
INTERVALS         = [15, 60, 240, 1440]  # minutes


# ─────────────────────────────────────────────────────────────────────────────
# 1. DATA FETCHING
# ─────────────────────────────────────────────────────────────────────────────
#
# DATA SOURCE NOTE:
#   Kraken's public OHLC endpoint always returns only the 720 most recent
#   candles (30 days at 1h, 8 days at 15m) and cannot be paginated backwards.
#   Historical data is fetched from CryptoCompare's free API, which has
#   BTC/EUR, ETH/EUR, SOL/EUR, XRP/EUR, DOGE/EUR going back years with
#   backward pagination via `toTs`.  No API key is required.
#   Prices come from CryptoCompare aggregated data (not Kraken's own orderbook),
#   so there may be minor price differences vs. live Kraken execution.

# Kraken pair → CryptoCompare base symbol
_CC_SYMBOL = {
    "XBTEUR":  "BTC",
    "ETHEUR":  "ETH",
    "SOLEUR":  "SOL",
    "XRPEUR":  "XRP",
    "DOGEEUR": "DOGE",
}
# interval (minutes) → (CryptoCompare endpoint, aggregate param)
_CC_ENDPOINT = {
    15:   ("histominute", 15),
    60:   ("histohour",    1),
    240:  ("histohour",    4),
    1440: ("histoday",     1),
}
_CC_BASE = "https://min-api.cryptocompare.com/data/v2"


def _ts(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())


def _cache_path(pair: str, interval: int) -> Path:
    CACHE_DIR.mkdir(exist_ok=True)
    return CACHE_DIR / f"{pair}_{interval}.json"


def fetch_ohlc(pair: str, interval: int, start_ts: int, end_ts: int) -> list:
    """
    Download all OHLC candles in [start_ts, end_ts) from CryptoCompare.
    Saves to disk cache.  Subsequent calls load from cache if range is covered.
    """
    cache_file = _cache_path(pair, interval)
    if cache_file.exists():
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            if cached:
                cs, ce = cached[0]["time"], cached[-1]["time"]
                if cs <= start_ts and ce >= end_ts - interval * 60:
                    filtered = [c for c in cached if start_ts <= c["time"] < end_ts]
                    print(f"    [cache] {pair:10s} {interval:4d}m  {len(filtered):5d} candles")
                    return filtered
        except Exception:
            pass

    fsym = _CC_SYMBOL.get(pair)
    if fsym is None:
        print(f"    [skip]  {pair} not in symbol map")
        return []
    endpoint, aggregate = _CC_ENDPOINT[interval]

    print(f"    [fetch] {pair:10s} {interval:4d}m ...", end="", flush=True)
    all_candles: dict[int, dict] = {}
    to_ts = end_ts
    calls = 0

    while to_ts > start_ts:
        try:
            params = {
                "fsym":      fsym,
                "tsym":      "EUR",
                "limit":     2000,
                "toTs":      to_ts,
                "aggregate": aggregate,
            }
            resp = requests.get(f"{_CC_BASE}/{endpoint}", params=params, timeout=20)
            resp.raise_for_status()
            data = resp.json()
            if data.get("Response") != "Success":
                print(f"\n      CryptoCompare error: {data.get('Message','?')}")
                break
            candles = data["Data"]["Data"]
            calls  += 1
            if not candles:
                break
            time_from = int(data["Data"]["TimeFrom"])
            for c in candles:
                t = int(c["time"])
                if start_ts <= t < end_ts and c["open"] > 0:
                    all_candles[t] = {
                        "time":   t,
                        "open":   float(c["open"]),
                        "high":   float(c["high"]),
                        "low":    float(c["low"]),
                        "close":  float(c["close"]),
                        "vwap":   float(c["close"]),      # CC doesn't provide VWAP
                        "volume": float(c["volumefrom"]),  # volume in base currency
                        "trades": 0,
                    }
            if time_from <= start_ts:
                break
            to_ts = time_from - 1
            time.sleep(0.25)
        except requests.exceptions.RequestException as exc:
            print(f"\n      network error ({exc}) — retrying in 5s")
            time.sleep(5)

    sorted_c = sorted(all_candles.values(), key=lambda c: c["time"])
    cache_file.write_text(json.dumps(sorted_c, ensure_ascii=False), encoding="utf-8")
    filtered = [c for c in sorted_c if start_ts <= c["time"] < end_ts]
    print(f" {len(filtered):5d} candles ({calls} API calls)")
    return filtered


def fetch_fear_greed() -> dict[date, int]:
    """Returns {date: fg_value} up to ~4 years of daily data."""
    fg_file = CACHE_DIR / "fear_greed.json"
    CACHE_DIR.mkdir(exist_ok=True)
    if fg_file.exists():
        try:
            raw = json.loads(fg_file.read_text(encoding="utf-8"))
            result = {date.fromisoformat(k): v for k, v in raw.items()}
            print(f"    [cache] Fear & Greed  {len(result):4d} days")
            return result
        except Exception:
            pass
    print("    [fetch] Fear & Greed ...", end="", flush=True)
    try:
        resp = requests.get(
            "https://api.alternative.me/fng/?limit=2000&date_format=us",
            timeout=20,
        )
        resp.raise_for_status()
        result: dict[date, int] = {}
        for e in resp.json().get("data", []):
            try:
                result[date.fromtimestamp(int(e["timestamp"]))] = int(e["value"])
            except Exception:
                pass
        fg_file.write_text(
            json.dumps({k.isoformat(): v for k, v in result.items()},
                       ensure_ascii=False),
            encoding="utf-8",
        )
        print(f" {len(result)} days")
        return result
    except Exception as exc:
        print(f" FAILED ({exc}) — F&G gate disabled")
        return {}


# ─────────────────────────────────────────────────────────────────────────────
# 2. ANALYSIS BUILDER (pure, from historical candle slices)
# ─────────────────────────────────────────────────────────────────────────────

def build_analysis(
    pair:          str,
    candles_1h:    list,
    candles_15m:   list,
    candles_4h:    list,
    candles_daily: list,
    candle_idx:    int,
    times_15m:     list,    # pre-computed [c["time"] for c in candles_15m]
    times_4h:      list,
    times_daily:   list,
) -> Optional[Analysis]:
    """
    Builds an Analysis from historical data at candle_idx in candles_1h.
    Uses a sliding window of MAX_LOOKBACK candles for indicator computation.
    No lookahead: only data at or before candle_idx is used.
    """
    horizon_ts = candles_1h[candle_idx]["time"] + 3600

    # ── 1h slice (primary TF) ─────────────────────────────────────────────────
    start_1h  = max(0, candle_idx + 1 - MAX_LOOKBACK)
    slice_1h  = candles_1h[start_1h: candle_idx + 1]
    if len(slice_1h) < cfg.EMA_SLOW + cfg.MACD_SIGNAL + 5:
        return None

    closes_1h  = [c["close"]  for c in slice_1h]
    highs_1h   = [c["high"]   for c in slice_1h]
    lows_1h    = [c["low"]    for c in slice_1h]
    volumes_1h = [c["volume"] for c in slice_1h]
    price      = closes_1h[-1]

    rsi_1h       = _rsi(closes_1h, cfg.RSI_PERIOD)
    hist         = _macd_histogram(closes_1h, cfg.MACD_FAST, cfg.MACD_SLOW, cfg.MACD_SIGNAL)
    macd_h       = hist[-1] if hist else 0.0
    macd_h_prev  = hist[-2] if len(hist) >= 2 else 0.0
    ema20_series = _ema(closes_1h, cfg.EMA_FAST)
    ema50_series = _ema(closes_1h, cfg.EMA_SLOW)
    vol_ratio    = _volume_ratio(volumes_1h)
    adx_val      = _adx(highs_1h, lows_1h, closes_1h, cfg.ADX_PERIOD)
    atr_val      = _atr(highs_1h, lows_1h, closes_1h, cfg.ATR_PERIOD)

    # ── 15m RSI (align to 1h horizon) ─────────────────────────────────────────
    idx15      = bisect.bisect_right(times_15m, horizon_ts - 1)
    start_15m  = max(0, idx15 - MAX_LOOKBACK)
    slice_15m  = candles_15m[start_15m:idx15]
    rsi_15m    = (_rsi([c["close"] for c in slice_15m], cfg.RSI_PERIOD)
                  if len(slice_15m) >= cfg.RSI_PERIOD + 2 else rsi_1h)

    # ── 4h RSI ────────────────────────────────────────────────────────────────
    idx4       = bisect.bisect_right(times_4h, horizon_ts - 1)
    slice_4h   = candles_4h[max(0, idx4 - MAX_LOOKBACK): idx4]
    rsi_4h     = (_rsi([c["close"] for c in slice_4h], cfg.RSI_PERIOD)
                  if len(slice_4h) >= cfg.RSI_PERIOD + 2 else rsi_1h)

    # ── Daily slice ───────────────────────────────────────────────────────────
    idx_d      = bisect.bisect_right(times_daily, horizon_ts - 1)
    slice_d    = candles_daily[max(0, idx_d - MAX_LOOKBACK): idx_d]

    rsi_daily         = rsi_1h
    daily_above_ema50 = True
    price_change_24h  = 0.0

    if len(slice_d) >= cfg.EMA_SLOW + 2:
        closes_d          = [c["close"] for c in slice_d]
        rsi_daily         = _rsi(closes_d, cfg.RSI_PERIOD)
        ema50_d           = _ema(closes_d, cfg.EMA_SLOW)
        daily_above_ema50 = closes_d[-1] >= ema50_d[-1] if ema50_d else True
        if len(closes_d) >= 2:
            price_change_24h = round(
                (closes_d[-1] - closes_d[-2]) / closes_d[-2] * 100, 2
            )

    last_24 = slice_1h[-24:]
    return Analysis(
        pair=pair,
        price=price,
        high_24h=max(c["high"]   for c in last_24) if len(last_24) >= 1 else price,
        low_24h =min(c["low"]    for c in last_24) if len(last_24) >= 1 else price,
        volume_24h=sum(c["volume"] for c in last_24),
        rsi_1h=round(rsi_1h, 2),
        macd_hist=macd_h,
        macd_hist_prev=macd_h_prev,
        ema20=ema20_series[-1] if ema20_series else price,
        ema50=ema50_series[-1] if ema50_series else price,
        volume_ratio=round(vol_ratio, 3),
        rsi_15m=round(rsi_15m, 2),
        adx=adx_val,
        atr=round(atr_val, 8),
        ob_imbalance=None,        # not available historically — known limit #1
        rsi_4h=round(rsi_4h, 2),
        rsi_daily=round(rsi_daily, 2),
        daily_above_ema50=daily_above_ema50,
        price_change_24h=price_change_24h,
        candles_1h=slice_1h[-60:],
        timestamp=float(candles_1h[candle_idx]["time"]),
    )


def precompute_analyses(
    pair:         str,
    candles_map:  dict,
    start_idx:    int = WARMUP_CANDLES_1H,
) -> list:
    """
    Pre-builds Analysis objects for every 1h candle from start_idx onwards.
    Returns a list aligned to candles_map[pair][60] (None for candles < start_idx).
    """
    p1h    = candles_map[pair][60]
    p15m   = candles_map[pair][15]
    p4h    = candles_map[pair][240]
    pdaily = candles_map[pair][1440]
    t15    = [c["time"] for c in p15m]
    t4     = [c["time"] for c in p4h]
    td     = [c["time"] for c in pdaily]

    analyses = [None] * len(p1h)
    for i in range(start_idx, len(p1h)):
        analyses[i] = build_analysis(pair, p1h, p15m, p4h, pdaily, i, t15, t4, td)
    return analyses


# ─────────────────────────────────────────────────────────────────────────────
# 3. WITHIN-CANDLE POSITION SIMULATION
# ─────────────────────────────────────────────────────────────────────────────

def _sim_candle(pos: dict, candle: dict, candle_ts: float) -> Optional[dict]:
    """
    Simulate one OHLC candle against an open position.

    Order of checks (worst-case / conservative for the strategy):
      1. Gap risk: candle.open already past effective SL → exit at open price.
      2. SL check (worst extreme) vs TP check (best extreme).
         If both hit in same candle: SL wins (worst case assumption).
      3. No exit: update trailing stop using the favorable extreme for next candle.

    Returns exit_data dict (including 'exit_price') or None if position survives.
    Modifies pos['trailing_stop'] in-place if trailing was updated.
    """
    side         = pos.get("side", "long")
    effective_sl = pos.get("trailing_stop", pos["stop_loss"])
    tp           = pos["take_profit"]
    atr          = pos.get("atr", 0)
    O, H, L      = candle["open"], candle["high"], candle["low"]

    def _exit(price: float) -> dict:
        result = _calc_exit(pos, price, cfg, candle_ts)
        result["exit_price"] = price
        return result

    if side == "long":
        if O <= effective_sl:                  # 1. gap down
            return _exit(O)
        if L <= effective_sl:                  # 2. SL (wins vs TP)
            return _exit(effective_sl)
        if H >= tp:                            # 2. TP
            return _exit(tp)
        _update_trailing_stop(pos, H, atr, cfg)   # 3. no exit, update trail
        return None
    else:  # short
        if O >= effective_sl:                  # 1. gap up
            return _exit(O)
        if H >= effective_sl:                  # 2. SL
            return _exit(effective_sl)
        if L <= tp:                            # 2. TP
            return _exit(tp)
        _update_trailing_stop(pos, L, atr, cfg)   # 3. update trail
        return None


# ─────────────────────────────────────────────────────────────────────────────
# 4. BACKTEST RUNNER (single profile)
# ─────────────────────────────────────────────────────────────────────────────

def run_backtest(
    pairs:          list[str],
    candles_map:    dict,         # {pair: {interval: [candles]}}
    analyses_map:   dict,         # {pair: [Analysis | None]} indexed like candles_map[pair][60]
    times_1h_map:   dict,         # {pair: [timestamps]} pre-computed
    fg_history:     dict,         # {date: fg_value}
    start_ts:       int,
    end_ts:         int,
    budget:         float,
    profile_name:   str,
) -> dict:
    """Run the full simulation for one profile. Returns a result dict."""
    cfg.apply_profile(profile_name)
    print(f"\n  Profile: {profile_name.upper()}")

    # ── State ──────────────────────────────────────────────────────────────────
    positions:   dict[str, dict] = {}
    trades_log:  list[dict]      = []
    equity_curve: list[tuple]    = []  # (timestamp, equity)
    capital = budget

    def _deployed() -> float:
        return sum(p["size_eur"] for p in positions.values())

    # ── Reference time axis: XBTEUR 1h candles inside the backtest window ──────
    ref_pair = cfg.BTC_PAIR if cfg.BTC_PAIR in pairs else pairs[0]
    ref_times = times_1h_map[ref_pair]
    ref_all   = candles_map[ref_pair][60]
    # Get indices of candles in [start_ts, end_ts)
    lo = bisect.bisect_left(ref_times, start_ts)
    hi = bisect.bisect_left(ref_times, end_ts)
    ref_indices = range(lo, hi)

    if len(ref_indices) < WARMUP_CANDLES_1H + 2:
        raise ValueError(f"Not enough 1h candles: {len(ref_indices)}")

    prev_day   = None
    daily_loss = 0.0

    for global_i in ref_indices:
        ts  = ref_times[global_i]
        dt  = date.fromtimestamp(ts)
        equity_curve.append((ts, capital + _deployed()))

        # Reset daily circuit breaker at midnight
        if dt != prev_day:
            daily_loss = 0.0
            prev_day   = dt

        # ── Monitor open positions ─────────────────────────────────────────────
        to_close: list[str] = []
        for pair, pos in list(positions.items()):
            t1h   = times_1h_map[pair]
            c_idx = bisect.bisect_right(t1h, ts) - 1
            if c_idx < 0:
                continue
            candle   = candles_map[pair][60][c_idx]
            exit_data = _sim_candle(pos, candle, float(ts))
            if exit_data is not None:
                net_pnl = exit_data["net_pnl_eur"]
                trades_log.append({
                    "pair":           pair,
                    "side":           pos.get("side", "long"),
                    "entry":          pos["entry"],
                    "exit_price":     exit_data["exit_price"],
                    "entry_ts":       pos.get("opened_at", ts),
                    "exit_ts":        ts,
                    "size_eur":       pos["size_eur"],
                    "gross_pnl_eur":  exit_data["gross_pnl_eur"],
                    "fee_eur":        exit_data["fee_entry"] + exit_data["fee_exit"],
                    "slip_eur":       exit_data["slip_eur"],
                    "funding_cost":   exit_data["funding_cost"],
                    "net_pnl_eur":    net_pnl,
                    "net_pnl_pct":    exit_data["net_pnl_pct"],
                    "regime":         _regime(date.fromtimestamp(pos.get("opened_at", ts))),
                })
                capital   += pos["size_eur"] + net_pnl
                if net_pnl < 0:
                    daily_loss += abs(net_pnl)
                to_close.append(pair)

        for pair in to_close:
            del positions[pair]

        # ── Skip signal generation during warm-up ─────────────────────────────
        if global_i < lo + WARMUP_CANDLES_1H:
            continue

        # ── Circuit breaker ────────────────────────────────────────────────────
        if cfg.CIRCUIT_BREAKER_ENABLED and daily_loss >= cfg.CIRCUIT_BREAKER_LOSS_EUR:
            continue

        # ── Capital / trade count gates ────────────────────────────────────────
        if len(positions) >= cfg.MAX_OPEN_TRADES:
            continue
        max_deployed = budget * cfg.MAX_CAPITAL_DEPLOYED
        if _deployed() >= max_deployed:
            continue
        avail = min(capital, max_deployed - _deployed())
        if avail < 10.0:
            continue

        # ── Fear & Greed gate ─────────────────────────────────────────────────
        fg_val        = fg_history.get(dt)
        long_blocked  = (cfg.FEAR_GREED_ENABLED and fg_val is not None and
                         (fg_val < cfg.FEAR_GREED_MIN or fg_val > cfg.FEAR_GREED_MAX))
        short_blocked = (cfg.FEAR_GREED_ENABLED and fg_val is not None and
                         (fg_val > cfg.FEAR_GREED_MAX or fg_val < cfg.FEAR_GREED_SHORT_MIN))

        already_open = set(positions.keys())

        # ── BTC analysis (pre-computed, looked up by timestamp) ───────────────
        btc_analysis = None
        if cfg.BTC_FILTER_ENABLED and cfg.BTC_PAIR in pairs:
            btc_t1h = times_1h_map[cfg.BTC_PAIR]
            btc_idx = bisect.bisect_right(btc_t1h, ts) - 1
            if btc_idx >= 0:
                btc_analysis = analyses_map[cfg.BTC_PAIR][btc_idx]

        # ── Signal scan ───────────────────────────────────────────────────────
        new_entries: list[tuple[str, dict]] = []

        for pair in pairs:
            if pair in already_open or pair in {e[0] for e in new_entries}:
                continue
            if len(positions) + len(new_entries) >= cfg.MAX_OPEN_TRADES:
                break

            t1h     = times_1h_map[pair]
            pair_ci = bisect.bisect_right(t1h, ts) - 1
            if pair_ci < 0:
                continue
            analysis = analyses_map[pair][pair_ci]
            if analysis is None:
                continue

            p1h_arr  = candles_map[pair][60]
            next_ci  = pair_ci + 1

            avail_now = min(
                capital - sum(e[1]["size_eur"] for e in new_entries),
                max_deployed - _deployed() - sum(e[1]["size_eur"] for e in new_entries),
            )
            if avail_now < 10.0:
                break

            open_set = already_open | {e[0] for e in new_entries}

            # ── Long signal ──────────────────────────────────────────────────
            if not long_blocked:
                sig = generate_signal(analysis, avail_now, open_set,
                                      btc_analysis=btc_analysis, open_pairs=open_set)
                if sig:
                    size_eur = max(round(avail_now * cfg.AI_SIZE_MED, 2), 10.0)
                    if size_eur > avail_now:
                        sig = None
                    # BTC-beta long bucket
                    elif cfg.MAX_BTC_BETA_EXPOSURE_PCT < 1.0:
                        max_beta = budget * cfg.MAX_BTC_BETA_EXPOSURE_PCT
                        long_exp = (sum(p["size_eur"] for p in positions.values() if p.get("side","long")=="long") +
                                    sum(e[1]["size_eur"] for e in new_entries if e[1].get("side","long")=="long"))
                        if long_exp + size_eur > max_beta:
                            sig = None

                if sig:
                    entry_p = p1h_arr[next_ci]["open"] if next_ci < len(p1h_arr) else sig.price
                    atr = sig.atr
                    tp  = round(entry_p + cfg.ATR_TP_MULT * atr, 8) if atr > 0 else round(entry_p * (1 + cfg.TAKE_PROFIT_PCT), 8)
                    sl  = round(entry_p - cfg.ATR_SL_MULT * atr, 8) if atr > 0 else round(entry_p * (1 - cfg.STOP_LOSS_PCT), 8)
                    new_entries.append((pair, {
                        "side":          "long",
                        "entry":         entry_p,
                        "size_crypto":   size_eur * (1 - cfg.KRAKEN_TAKER_FEE) / entry_p,
                        "size_eur":      size_eur,
                        "fee_entry_eur": round(size_eur * cfg.KRAKEN_TAKER_FEE, 4),
                        "take_profit":   tp,
                        "stop_loss":     sl,
                        "trailing_stop": sl,
                        "atr":           atr,
                        "opened_at":     float(p1h_arr[next_ci]["time"] if next_ci < len(p1h_arr) else ts),
                        "score":         sig.score,
                    }))
                    continue  # one signal per pair per candle

            # ── Short signal ─────────────────────────────────────────────────
            if not short_blocked:
                sig = generate_short_signal(analysis, avail_now, open_set,
                                            btc_analysis=btc_analysis, open_pairs=open_set)
                if sig:
                    size_eur = max(round(avail_now * cfg.AI_SIZE_MED, 2), 10.0)
                    if size_eur > avail_now:
                        sig = None
                    elif cfg.MAX_BTC_BETA_EXPOSURE_PCT < 1.0:
                        max_beta  = budget * cfg.MAX_BTC_BETA_EXPOSURE_PCT
                        short_exp = (sum(p["size_eur"] for p in positions.values() if p.get("side")=="short") +
                                     sum(e[1]["size_eur"] for e in new_entries if e[1].get("side")=="short"))
                        if short_exp + size_eur > max_beta:
                            sig = None

                if sig:
                    entry_p = p1h_arr[next_ci]["open"] if next_ci < len(p1h_arr) else sig.price
                    atr = sig.atr
                    tp  = round(entry_p - cfg.ATR_TP_MULT_SHORT * atr, 8) if atr > 0 else round(entry_p * (1 - cfg.TAKE_PROFIT_PCT), 8)
                    sl  = round(entry_p + cfg.ATR_SL_MULT_SHORT * atr, 8) if atr > 0 else round(entry_p * (1 + cfg.STOP_LOSS_PCT), 8)
                    tp  = max(tp, entry_p * 0.01)
                    new_entries.append((pair, {
                        "side":          "short",
                        "entry":         entry_p,
                        "size_crypto":   size_eur / entry_p,
                        "size_eur":      size_eur,
                        "fee_entry_eur": round(size_eur * (cfg.KRAKEN_TAKER_FEE + cfg.MARGIN_OPEN_FEE), 4),
                        "take_profit":   tp,
                        "stop_loss":     sl,
                        "trailing_stop": sl,
                        "atr":           atr,
                        "opened_at":     float(p1h_arr[next_ci]["time"] if next_ci < len(p1h_arr) else ts),
                        "score":         sig.score,
                    }))

        # ── Open new positions ─────────────────────────────────────────────────
        for pair, pos_d in new_entries:
            if pair not in positions:
                positions[pair] = pos_d
                capital -= pos_d["size_eur"]

    # ── Force-close any positions open at end of period ───────────────────────
    for pair, pos in list(positions.items()):
        p1h = candles_map[pair][60]
        if p1h:
            last_c    = p1h[-1]
            exit_data = _sim_candle(pos, last_c, float(last_c["time"]))
            if exit_data is None:
                # candle didn't trigger SL/TP → close at last close price
                res = _calc_exit(pos, last_c["close"], cfg, float(last_c["time"]))
                res["exit_price"] = last_c["close"]
                exit_data = res
            trades_log.append({
                "pair":          pair,
                "side":          pos.get("side", "long"),
                "entry":         pos["entry"],
                "exit_price":    exit_data["exit_price"],
                "entry_ts":      pos.get("opened_at", last_c["time"]),
                "exit_ts":       last_c["time"],
                "size_eur":      pos["size_eur"],
                "gross_pnl_eur": exit_data["gross_pnl_eur"],
                "fee_eur":       exit_data["fee_entry"] + exit_data["fee_exit"],
                "slip_eur":      exit_data["slip_eur"],
                "funding_cost":  exit_data["funding_cost"],
                "net_pnl_eur":   exit_data["net_pnl_eur"],
                "net_pnl_pct":   exit_data["net_pnl_pct"],
                "regime":        _regime(date.fromtimestamp(pos.get("opened_at", last_c["time"]))),
                "force_closed":  True,
            })
            capital += pos["size_eur"] + exit_data["net_pnl_eur"]

    equity_curve.append((end_ts, capital))
    return {
        "profile":       profile_name,
        "budget":        budget,
        "final_capital": capital,
        "trades":        trades_log,
        "equity_curve":  equity_curve,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 5. METRICS & REPORT
# ─────────────────────────────────────────────────────────────────────────────

def _regime(d: date) -> str:
    for name, start, end in REGIMES:
        if start <= d <= end:
            return name
    return "other"


def _max_drawdown(equity_curve: list) -> float:
    if not equity_curve:
        return 0.0
    peak = equity_curve[0][1]
    mdd  = 0.0
    for _, eq in equity_curve:
        if eq > peak:
            peak = eq
        if peak > 0:
            mdd = max(mdd, (peak - eq) / peak)
    return mdd


def _metrics(trades: list, budget: float) -> dict:
    if not trades:
        return {"n": 0, "win_rate": 0.0, "avg_win": 0.0, "avg_loss": 0.0,
                "expectancy": 0.0, "total_net": 0.0, "total_gross": 0.0,
                "total_fees": 0.0, "total_funding": 0.0, "net_return_pct": 0.0}
    wins   = [t["net_pnl_eur"] for t in trades if t["net_pnl_eur"] > 0]
    losses = [t["net_pnl_eur"] for t in trades if t["net_pnl_eur"] <= 0]
    total_net   = sum(t["net_pnl_eur"]   for t in trades)
    total_gross = sum(t["gross_pnl_eur"] for t in trades)
    total_fees  = sum(t["fee_eur"]       for t in trades)
    total_fund  = sum(t.get("funding_cost", 0) for t in trades)
    avg_win  = sum(wins)   / len(wins)   if wins   else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    win_rate = len(wins) / len(trades)
    expectancy = win_rate * avg_win + (1 - win_rate) * avg_loss
    return {
        "n":              len(trades),
        "win_rate":       win_rate,
        "avg_win":        avg_win,
        "avg_loss":       avg_loss,
        "expectancy":     expectancy,
        "total_net":      total_net,
        "total_gross":    total_gross,
        "total_fees":     total_fees,
        "total_funding":  total_fund,
        "net_return_pct": total_net / budget * 100,
    }


def _bah_return(pair: str, candles_map: dict, start_ts: int, end_ts: int) -> Optional[float]:
    """Buy-and-hold return % for a pair over [start_ts, end_ts)."""
    for interval in [1440, 240, 60]:
        cs = [c for c in candles_map.get(pair, {}).get(interval, [])
              if start_ts <= c["time"] < end_ts]
        if len(cs) >= 2:
            return (cs[-1]["close"] - cs[0]["open"]) / cs[0]["open"] * 100
    return None


def print_report(
    results:     list[dict],
    candles_map: dict,
    start_ts:    int,
    end_ts:      int,
    budget:      float,
) -> None:
    start_s = date.fromtimestamp(start_ts).isoformat()
    end_s   = date.fromtimestamp(end_ts).isoformat()
    profiles = [r["profile"] for r in results]
    W   = 80
    COL = 18

    lines: list[str] = []
    def sep(c="-"): lines.append(c * W)
    def hdr(t):     lines.append(f"  {t}")
    def blank():    lines.append("")

    sep("="); hdr("KRAKEN TRADING BOT -- BACKTEST REPORT")
    hdr(f"Period : {start_s}  to  {end_s}   Budget: {budget:.0f} EUR")
    hdr(f"Profiles: {', '.join(p.upper() for p in profiles)}")
    sep("="); blank()

    hdr("!! KNOWN LIMITS:")
    hdr("  1. ob_imbalance = None for all candles -> max score 5/6 (not 6/6).")
    hdr("     Live bot earns the OB point when the order book confirms the move.")
    hdr("  2. Groq AI -> deterministic rule: execute=True always, size=AI_SIZE_MED.")
    hdr("     Live results differ when AI blocks or resizes signals.")
    hdr("  3. 15m data unavailable from CryptoCompare free tier for historical dates.")
    hdr("     rsi_15m falls back to rsi_1h. RSI_15M gate uses 1h value as proxy.")
    hdr("  4. Price data from CryptoCompare (aggregated), not Kraken's own orderbook.")
    hdr("     Minor price differences possible vs. actual Kraken execution.")
    blank()

    sep(); hdr("BUY-AND-HOLD BENCHMARK  (same period, zero costs)"); sep()
    for bp in ["XBTEUR", "ETHEUR"]:
        bah = _bah_return(bp, candles_map, start_ts, end_ts)
        if bah is not None:
            hdr(f"  {bp:<10} {bah:+8.1f}%   "
                f"({bah / 100 * budget:+.2f} EUR on {budget:.0f} EUR)")
    blank()

    # ── Side-by-side table ────────────────────────────────────────────────────
    def row(label, vals):
        line = f"  {label:<28}" + "".join(f"{v:<{COL}}" for v in vals)
        lines.append(line)

    sep(); row("Profile", [p.upper() for p in profiles]); sep()

    ms  = [_metrics(r["trades"], budget) for r in results]
    mdd = [_max_drawdown(r["equity_curve"]) for r in results]

    row("Net return EUR",    [f"{m['total_net']:+.2f} EUR"       for m in ms])
    row("Net return %",      [f"{m['net_return_pct']:+.1f}%"   for m in ms])
    row("Max drawdown",      [f"{d*100:.1f}%"                  for d in mdd])
    row("# Trades",          [str(m["n"])                      for m in ms])
    row("Win rate",          [f"{m['win_rate']*100:.1f}%"      for m in ms])
    row("Avg win EUR",       [f"{m['avg_win']:+.3f} EUR"         for m in ms])
    row("Avg loss EUR",      [f"{m['avg_loss']:+.3f} EUR"        for m in ms])
    row("Expectancy/trade",  [f"{m['expectancy']:+.3f} EUR"      for m in ms])
    row("Gross return EUR",  [f"{m['total_gross']:+.2f} EUR"     for m in ms])
    row("Total fees EUR",    [f"{m['total_fees']:.2f} EUR"       for m in ms])
    row("Total funding EUR", [f"{m['total_funding']:.2f} EUR"    for m in ms])
    blank()

    # ── Cost impact ───────────────────────────────────────────────────────────
    sep(); hdr("GROSS vs NET  (how much costs ate)"); sep()
    for r, m in zip(results, ms):
        eaten  = m["total_gross"] - m["total_net"]
        pct_of = abs(eaten / m["total_gross"] * 100) if m["total_gross"] else 0.0
        sign   = "ate" if eaten > 0 else "saved"
        hdr(f"  {r['profile'].upper():<14} gross {m['total_gross']:+.2f} EUR  "
            f"net {m['total_net']:+.2f} EUR  "
            f"costs {sign} {abs(eaten):.2f} EUR ({pct_of:.0f}% of gross)")
    blank()

    # ── By regime ─────────────────────────────────────────────────────────────
    all_regimes = sorted({t["regime"] for r in results for t in r["trades"]})
    for regime in all_regimes:
        sep(); hdr(f"REGIME: {regime.upper()}"); sep()
        row("Profile", [p.upper() for p in profiles]); sep()

        def rg_vals(key, fmt):
            cells = []
            for r in results:
                ts_r = [t for t in r["trades"] if t["regime"] == regime]
                if not ts_r:
                    cells.append("n/a")
                    continue
                m_r = _metrics(ts_r, budget)
                v   = m_r.get(key, 0)
                cells.append(fmt(v))
            return cells

        row("Net return EUR",   rg_vals("total_net",    lambda v: f"{v:+.2f} EUR"))
        row("# Trades",        rg_vals("n",             lambda v: str(int(v))))
        row("Win rate",        rg_vals("win_rate",      lambda v: f"{v*100:.1f}%"))
        row("Expectancy/trade",rg_vals("expectancy",    lambda v: f"{v:+.3f} EUR"))
        blank()

    sep("=")
    report = "\n".join(lines)
    out = ROOT / "backtest_report.txt"
    out.write_text(report, encoding="utf-8")
    # Print safely regardless of console encoding
    try:
        print(report)
    except UnicodeEncodeError:
        print(report.encode("ascii", errors="replace").decode("ascii"))
    print(f"\n  Saved: {out}")


# ─────────────────────────────────────────────────────────────────────────────
# 6. MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(description="Kraken bot backtest")
    p.add_argument("--pairs",       nargs="+", default=None)
    p.add_argument("--start",       default="2022-01-01")
    p.add_argument("--end",         default=None)
    p.add_argument("--budget",      type=float, default=100.0)
    p.add_argument("--cache-dir",   default=str(ROOT / "backtest_data"))
    p.add_argument("--no-download", action="store_true")
    p.add_argument("--profiles",    nargs="+",
                   default=["conservative", "moderate", "aggressive"])
    args = p.parse_args()

    global CACHE_DIR
    CACHE_DIR = Path(args.cache_dir)

    start_dt = date.fromisoformat(args.start)
    end_dt   = date.today() if args.end is None else date.fromisoformat(args.end)
    start_ts = _ts(start_dt)
    end_ts   = _ts(end_dt)

    pairs = args.pairs or list(cfg.PAIRS)
    if cfg.BTC_PAIR not in pairs:
        pairs = [cfg.BTC_PAIR] + pairs

    print(f"\nKraken Bot Backtest  {start_dt} to {end_dt}")
    print(f"Pairs:    {pairs}")
    print(f"Budget:   {args.budget} EUR   Profiles: {args.profiles}\n")

    # ── Load / download data ──────────────────────────────────────────────────
    print("Loading OHLC data...")
    candles_map: dict = {}
    for pair in pairs:
        candles_map[pair] = {}
        for interval in INTERVALS:
            if args.no_download:
                try:
                    cached = json.loads(_cache_path(pair, interval).read_text(encoding="utf-8"))
                    candles_map[pair][interval] = [c for c in cached if start_ts <= c["time"] < end_ts]
                    print(f"    [cache] {pair:10s} {interval:4d}m  {len(candles_map[pair][interval]):5d} candles")
                except Exception:
                    candles_map[pair][interval] = []
            else:
                candles_map[pair][interval] = fetch_ohlc(pair, interval, start_ts, end_ts)

    print("\nLoading Fear & Greed...")
    if args.no_download:
        try:
            raw = json.loads((CACHE_DIR / "fear_greed.json").read_text(encoding="utf-8"))
            fg_history = {date.fromisoformat(k): v for k, v in raw.items()}
            print(f"    [cache] Fear & Greed  {len(fg_history)} days")
        except Exception:
            fg_history = {}
            print("    F&G cache missing — gate disabled")
    else:
        fg_history = fetch_fear_greed()

    # ── Pre-compute analyses (one-time) ───────────────────────────────────────
    print("\nPrecomputing analyses...")
    analyses_map: dict = {}
    times_1h_map: dict = {}
    for pair in pairs:
        times_1h_map[pair] = [c["time"] for c in candles_map[pair][60]]
        analyses_map[pair] = precompute_analyses(pair, candles_map)
        n_computed = sum(1 for a in analyses_map[pair] if a is not None)
        print(f"  {pair}: {n_computed} analyses")

    # ── Run simulation per profile ────────────────────────────────────────────
    results: list[dict] = []
    for prof in args.profiles:
        if prof not in cfg.PROFILES:
            print(f"  Unknown profile '{prof}', skipping.")
            continue
        result = run_backtest(
            pairs=pairs,
            candles_map=candles_map,
            analyses_map=analyses_map,
            times_1h_map=times_1h_map,
            fg_history=fg_history,
            start_ts=start_ts,
            end_ts=end_ts,
            budget=args.budget,
            profile_name=prof,
        )
        results.append(result)
        n   = len(result["trades"])
        net = sum(t["net_pnl_eur"] for t in result["trades"])
        print(f"    -> {n} trades   net P&L = {net:+.2f} EUR")

    if results:
        print()
        print_report(results, candles_map, start_ts, end_ts, args.budget)


if __name__ == "__main__":
    main()
