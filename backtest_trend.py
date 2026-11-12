"""
backtest_trend.py — Portfolio-level backtest of the TREND mode.

It calls the SAME rule functions as the live bot (core.trend.evaluate_entry /
evaluate_exit) and mirrors core/trend_engine.py's handling around them:

  * decisions on COMPLETED daily candles only (day i's close decides);
  * the order goes out after that close, so the fill is the NEXT day's open;
  * one fixed slot per symbol (budget / n_symbols), no compounding;
  * an entry is skipped when the fill price is more than
    TREND_MAX_ENTRY_DRIFT_PCT above the signal close (same guard as live);
  * costs per side = fee + slippage.

What it reports, so the result cannot be read more kindly than it deserves:
net P&L, buy-and-hold of the same slots, P&L per calendar year, how much of the
total comes from the best 5 trades, and the worst drawdown of the daily
mark-to-market equity.

Crypto data: the CryptoCompare cache in backtest_data/ (`python backtest.py`
populates it). Stock data: downloaded from Alpaca (needs ALPACA_API_KEY/SECRET)
and cached in backtest_data/stocks_<SYMBOL>.json.

Usage:
    python backtest_trend.py                       # crypto, defaults
    python backtest_trend.py --market stocks       # ETFs via Alpaca data
    python backtest_trend.py --grid                # 3×3 parameter grid totals
    python backtest_trend.py --start 2023-01-01 --budget 100
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

for _k, _v in [
    ("TELEGRAM_TOKEN",    "fake:token"),
    ("TELEGRAM_CHAT_ID",  "0"),
    ("GROQ_API_KEY",      "fake"),
    ("PAPER_TRADING",     "true"),
]:
    os.environ.setdefault(_k, _v)

from core import config as cfg
from core import trend

DATA = ROOT / "backtest_data"
CRYPTO_PAIRS = ["XBTEUR", "ETHEUR", "SOLEUR", "XRPEUR", "DOGEEUR"]


# ── Data ──────────────────────────────────────────────────────────────────────

def _day(c: dict) -> str:
    """Calendar day of a candle as YYYY-MM-DD (crypto: epoch; stocks: ISO date)."""
    if "date" in c:
        return c["date"]
    return datetime.fromtimestamp(int(c["time"]), tz=timezone.utc).strftime("%Y-%m-%d")


def load_crypto() -> dict:
    data = {}
    for pair in CRYPTO_PAIRS:
        path = DATA / f"{pair}_1440.json"
        if not path.exists():
            sys.exit(f"Cache mancante: {path} — esegui prima `python backtest.py`.")
        candles = json.loads(path.read_text(encoding="utf-8"))
        data[pair] = [c for c in candles if c.get("close", 0) > 0]
    return data


def load_stocks(symbols: list, start: str, refresh: bool) -> dict:
    """Daily bars from Alpaca, cached on disk. Warm-up history is fetched from
    one year before `start` so the indicators are ready on day one."""
    from core.broker import AlpacaBroker, BrokerError
    broker = None
    fetch_from = f"{int(start[:4]) - 1}{start[4:]}"
    data = {}
    for sym in symbols:
        path = DATA / f"stocks_{sym}.json"
        if path.exists() and not refresh:
            data[sym] = json.loads(path.read_text(encoding="utf-8"))
            continue
        try:
            broker = broker or AlpacaBroker()
            candles = broker.history(sym, fetch_from)
        except BrokerError as exc:
            sys.exit(f"Dati azionari non disponibili per {sym}: {exc}")
        # A bar for a session still in progress is not a completed candle.
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        candles = [c for c in candles if c["date"] < today]
        DATA.mkdir(exist_ok=True)
        path.write_text(json.dumps(candles), encoding="utf-8")
        data[sym] = candles
    return data


# ── Simulation ────────────────────────────────────────────────────────────────

def simulate(data: dict, start: str, budget: float, cost_side: float,
             max_drift: float) -> dict:
    """Run the whole portfolio day by day. Returns trades, equity curve, benchmark."""
    symbols = list(data)
    slot = budget / len(symbols)
    index = {s: {_day(c): i for i, c in enumerate(data[s])} for s in symbols}
    days = sorted(d for d in set().union(*[set(index[s]) for s in symbols]) if d >= start)

    pos: dict = {}        # symbol -> {"entry", "peak", "qty", "day"}
    order: dict = {}      # symbol -> ("buy", decision) | ("sell",) to fill at today's open
    last_px: dict = {}    # symbol -> latest close, for mark-to-market
    trades, equity = [], []
    realized = 0.0
    skipped_drift = 0

    for day in days:
        for sym in symbols:
            i = index[sym].get(day)
            if i is None:
                continue                      # no bar that day (holiday / listing gap)
            candle = data[sym][i]

            # 1. Fill what was decided on the previous close, at today's open.
            pending = order.pop(sym, None)
            if pending and pending[0] == "buy" and sym not in pos:
                dec = pending[1]
                fill = candle["open"]
                if fill > dec.close * (1 + max_drift):
                    skipped_drift += 1
                else:
                    fill *= 1 + cost_side
                    pos[sym] = {"entry": fill, "peak": dec.close,
                                "qty": slot / fill, "day": day}
            elif pending and pending[0] == "sell" and sym in pos:
                p = pos.pop(sym)
                fill = candle["open"] * (1 - cost_side)
                pnl = (fill - p["entry"]) * p["qty"]
                realized += pnl
                trades.append({"symbol": sym, "open": p["day"], "close": day,
                               "pnl": pnl, "still_open": False})

            # 2. Decide on today's completed close (acted on tomorrow).
            window = data[sym][:i + 1]
            if sym in pos:
                res = trend.evaluate_exit(window, pos[sym]["peak"])
                if res is not None:
                    pos[sym]["peak"] = res["peak"]
                    if res["exit"]:
                        order[sym] = ("sell",)
            else:
                dec = trend.evaluate_entry(window)
                if dec is not None and dec.enter:
                    order[sym] = ("buy", dec)
            last_px[sym] = candle["close"]

        open_pnl = sum((last_px[s] * (1 - cost_side) - p["entry"]) * p["qty"]
                       for s, p in pos.items())
        equity.append((day, realized + open_pnl))

    # Positions still open at the end: marked to market, flagged as such.
    for sym, p in pos.items():
        pnl = (last_px[sym] * (1 - cost_side) - p["entry"]) * p["qty"]
        trades.append({"symbol": sym, "open": p["day"], "close": days[-1],
                       "pnl": pnl, "still_open": True})

    # Buy-and-hold of the same slots over the same days, same costs.
    bh = 0.0
    for sym in symbols:
        bars = [data[sym][index[sym][d]] for d in days if d in index[sym]]
        if len(bars) >= 2:
            buy = bars[0]["open"] * (1 + cost_side)
            bh += slot * (bars[-1]["close"] * (1 - cost_side) / buy - 1)

    return {"trades": trades, "equity": equity, "buy_hold": bh,
            "days": days, "skipped_drift": skipped_drift, "slot": slot}


def max_drawdown(equity: list) -> tuple:
    """Largest peak-to-trough fall of the equity curve: (amount, peak_day, trough_day)."""
    peak, peak_day = 0.0, equity[0][0] if equity else ""
    worst = (0.0, peak_day, peak_day)
    for day, value in equity:
        if value > peak:
            peak, peak_day = value, day
        if peak - value > worst[0]:
            worst = (peak - value, peak_day, day)
    return worst


# ── Report ────────────────────────────────────────────────────────────────────

def report(res: dict, budget: float, cur: str, label: str) -> None:
    trades, equity = res["trades"], res["equity"]
    net = sum(t["pnl"] for t in trades)
    wins = [t for t in trades if t["pnl"] > 0]
    dd, dd_peak, dd_trough = max_drawdown(equity)

    print(f"\n{label}")
    print(f"Periodo {res['days'][0]} → {res['days'][-1]}   "
          f"budget {budget:.0f}{cur} ({res['slot']:.2f}{cur} per simbolo, senza reinvestimento)\n")
    print(f"  Netto strategia        {net:+9.2f}{cur}   ({net / budget * 100:+.1f}% sul budget)")
    print(f"  Buy-and-hold (slot)    {res['buy_hold']:+9.2f}{cur}")
    print(f"  Trade                  {len(trades):9d}   "
          f"(vincenti {len(wins) / max(len(trades), 1) * 100:.0f}%, "
          f"ancora aperti {sum(t['still_open'] for t in trades)})")
    print(f"  Drawdown massimo       {-dd:+9.2f}{cur}   (dal picco del {dd_peak} al {dd_trough})")
    if res["skipped_drift"]:
        print(f"  Ingressi saltati       {res['skipped_drift']:9d}   (apertura troppo sopra il segnale)")

    by_year: dict = {}
    for t in trades:
        by_year[t["close"][:4]] = by_year.get(t["close"][:4], 0.0) + t["pnl"]
    print("\n  Per anno di chiusura:")
    for year in sorted(by_year):
        print(f"    {year}   {by_year[year]:+9.2f}{cur}")
    positive = sum(1 for v in by_year.values() if v > 0)
    print(f"    anni positivi: {positive}/{len(by_year)}")

    best = sorted(trades, key=lambda t: -t["pnl"])[:5]
    best_sum = sum(t["pnl"] for t in best)
    print(f"\n  I 5 trade migliori valgono {best_sum:+.2f}{cur}; "
          f"tutto il resto {net - best_sum:+.2f}{cur}:")
    for t in best:
        flag = "  (ancora aperto)" if t["still_open"] else ""
        print(f"    {t['symbol']:<8} {t['open']} → {t['close']}   {t['pnl']:+8.2f}{cur}{flag}")


def grid(data: dict, args, cost_side: float, cur: str) -> None:
    """Total net P&L across a small parameter grid — robustness, not optimisation."""
    saved = (cfg.TREND_BREAKOUT_DAYS, cfg.TREND_CHANDELIER_MULT)
    print("\nGriglia parametri (netto totale):")
    print("  breakout \\ chandelier " + "".join(f"{m:>10.1f}x" for m in (2.5, 3.0, 3.5)))
    try:
        for lookback in (30, 40, 50):
            row = []
            for mult in (2.5, 3.0, 3.5):
                cfg.TREND_BREAKOUT_DAYS, cfg.TREND_CHANDELIER_MULT = lookback, mult
                res = simulate(data, args.start, args.budget, cost_side, args.drift)
                row.append(sum(t["pnl"] for t in res["trades"]))
            print(f"  {lookback:>3d} giorni            " + "".join(f"{v:>+10.1f}{cur}" for v in row))
    finally:
        cfg.TREND_BREAKOUT_DAYS, cfg.TREND_CHANDELIER_MULT = saved


def main() -> None:
    p = argparse.ArgumentParser(description="Portfolio backtest of TREND mode")
    p.add_argument("--market", choices=["crypto", "stocks"], default="crypto")
    p.add_argument("--start", default="2022-01-01")
    p.add_argument("--budget", type=float, default=None,
                   help="default: 100 (crypto, EUR) / STOCK_BUDGET_USD (stocks)")
    p.add_argument("--cost", type=float, default=None,
                   help="cost per side as a fraction (default: crypto 0.005 = taker fee "
                        "+ slippage; stocks = STOCK_SLIPPAGE_PCT)")
    p.add_argument("--drift", type=float, default=cfg.TREND_MAX_ENTRY_DRIFT_PCT)
    p.add_argument("--symbols", default=None, help="comma-separated override (stocks)")
    p.add_argument("--refresh", action="store_true", help="re-download stock data")
    p.add_argument("--grid", action="store_true", help="also print the 3×3 parameter grid")
    args = p.parse_args()

    if args.market == "crypto":
        data, cur = load_crypto(), "€"
        cost_side = args.cost if args.cost is not None else cfg.KRAKEN_TAKER_FEE + cfg.SLIPPAGE_PCT
        args.budget = args.budget or 100.0
    else:
        symbols = ([s.strip().upper() for s in args.symbols.split(",") if s.strip()]
                   if args.symbols else cfg.STOCK_SYMBOLS)
        data, cur = load_stocks(symbols, args.start, args.refresh), "$"
        cost_side = args.cost if args.cost is not None else cfg.STOCK_SLIPPAGE_PCT
        args.budget = args.budget or cfg.STOCK_BUDGET_USD

    label = (f"TREND {args.market} — breakout {cfg.TREND_BREAKOUT_DAYS}g + EMA{cfg.TREND_EMA_DAYS}, "
             f"chandelier {cfg.TREND_CHANDELIER_MULT}×ATR{cfg.TREND_ATR_DAYS}, "
             f"costi {cost_side * 100:.2f}% per lato")
    report(simulate(data, args.start, args.budget, cost_side, args.drift),
           args.budget, cur, label)
    if args.grid:
        grid(data, args, cost_side, cur)


if __name__ == "__main__":
    main()
