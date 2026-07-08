"""
backtest_trend.py — Walk-forward backtest of the TREND mode (core/trend.py).

Uses the SAME entry/exit functions as the live bot (core.trend.evaluate_entry /
evaluate_exit), so this is a parity test, not a re-implementation. Data comes
from the CryptoCompare cache in backtest_data/ (daily candles, 2022 → today —
run `python backtest.py` once to populate it).

Validated result (2026-07-08, defaults 40d/EMA50/3×ATR20, costs 1% RT):
  +141€ on 100€ over 4.5y, 93 trades, whole 3×3 parameter grid positive
  (+111…+172€). The 1h momentum strategy lost ~90€ per window on the same data.

Usage:
    python backtest_trend.py
    python backtest_trend.py --windows 6 --budget 100 --cost 0.010
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

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

from core import config as cfg
from core import trend

DATA  = ROOT / "backtest_data"
PAIRS = ["XBTEUR", "ETHEUR", "SOLEUR", "XRPEUR", "DOGEEUR"]


def run_pair(candles: list, w_start: int, w_end: int, slot_eur: float, cost_rt: float):
    """Simulate one pair inside [w_start, w_end) using core.trend rules.

    Feeds candles[:i+2] so core.trend sees candles[:i+1] as completed days and
    decides on the close of day i — identical to what the live bot sees after
    each daily close. Returns (n_trades, pnl_eur).
    """
    ts = [c["time"] for c in candles]
    in_pos = False
    entry = peak = 0.0
    trades, pnl_total = 0, 0.0
    last_close = None

    for i in range(len(candles) - 1):
        if not (w_start <= ts[i] < w_end):
            continue
        window = candles[:i + 2]          # completed days = candles[:i+1]
        if not in_pos:
            dec = trend.evaluate_entry(window)
            if dec is not None and dec.enter:
                in_pos, entry, peak = True, dec.close, dec.close
        else:
            res = trend.evaluate_exit(window, peak)
            if res is None:
                continue
            peak = res["peak"]
            if res["exit"]:
                gross = (res["close"] - entry) / entry
                pnl_total += slot_eur * (gross - cost_rt)
                trades += 1
                in_pos = False
        last_close = candles[i]["close"]

    if in_pos and last_close is not None:     # mark-to-market at window end
        gross = (last_close - entry) / entry
        pnl_total += slot_eur * (gross - cost_rt)
        trades += 1
    return trades, pnl_total


def main():
    p = argparse.ArgumentParser(description="Walk-forward backtest of TREND mode")
    p.add_argument("--windows", type=int, default=6)
    p.add_argument("--budget", type=float, default=100.0)
    p.add_argument("--cost", type=float, default=0.010,
                   help="round-trip cost fraction (default 1%%, pessimistic)")
    p.add_argument("--start", default="2022-01-01")
    args = p.parse_args()

    start_ts = int(datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc).timestamp())
    end_ts   = int(datetime.now(timezone.utc).timestamp())
    slot     = args.budget / len(PAIRS)

    data = {}
    for pair in PAIRS:
        path = DATA / f"{pair}_1440.json"
        if not path.exists():
            print(f"Cache mancante: {path} — esegui prima `python backtest.py` per scaricare i dati.")
            sys.exit(1)
        candles = json.loads(path.read_text(encoding="utf-8"))
        data[pair] = [c for c in candles if c.get("close", 0) > 0]

    span = (end_ts - start_ts) // args.windows
    bounds = [(start_ts + i * span,
               start_ts + (i + 1) * span if i < args.windows - 1 else end_ts)
              for i in range(args.windows)]

    print(f"TREND MODE walk-forward — Donchian {cfg.TREND_BREAKOUT_DAYS}d + EMA{cfg.TREND_EMA_DAYS}, "
          f"chandelier {cfg.TREND_CHANDELIER_MULT}xATR{cfg.TREND_ATR_DAYS}")
    print(f"Budget {args.budget:.0f}€ ({slot:.0f}€/pair), costi {args.cost*100:.1f}% RT\n")
    print(f"{'finestra':<26}{'trade':>6}{'net P&L':>10}")

    nets = []
    for w_start, w_end in bounds:
        t_tot, pnl_tot = 0, 0.0
        for pair in PAIRS:
            t, pnl = run_pair(data[pair], w_start, w_end, slot, args.cost)
            t_tot += t
            pnl_tot += pnl
        nets.append(pnl_tot)
        print(f"{date.fromtimestamp(w_start)} -> {date.fromtimestamp(w_end)} "
              f"{t_tot:>5d} {pnl_tot:>+9.2f}€")

    pos  = sum(1 for x in nets if x > 0)
    mean = sum(nets) / len(nets)
    print(f"\nFinestre positive: {pos}/{len(nets)}   media {mean:+.2f}€   totale {sum(nets):+.2f}€")


if __name__ == "__main__":
    main()
