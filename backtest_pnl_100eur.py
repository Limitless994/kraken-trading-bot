#!/usr/bin/env python3
"""
Backtest P&L: How much would you have made/lost with 100€ budget
using mechanical gates instead of Groq?

Maps the 18 signals that mechanical gates would approve to actual
closed trades in the DB, sums their P&L, and scales to 100€ budget.
"""

import sqlite3
import sys
import os
from pathlib import Path
from datetime import datetime, timedelta

DB_PATH = os.getenv("BOT_DB", str(Path(__file__).parent / "trading.db"))

# Thresholds (same as backtest_meccanical_gates.py)
RSI_SLOPE_MAX_SHORT = 0.3
VOLUME_RATIO_MIN = 1.5
ADX_MIN = 25

BUDGET_EUR = 100.0

def load_data():
    """Load signals and trades from DB."""
    if not Path(DB_PATH).exists():
        print(f"DB not found: {DB_PATH}")
        sys.exit(1)

    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row

    signals = c.execute("""
        SELECT ts, pair, direction, decision, reason, volume_ratio, adx
        FROM groq_decisions ORDER BY ts
    """).fetchall()

    trades = c.execute("""
        SELECT pair, side, opened_at, closed_at, pnl_eur, pnl_pct, size_eur
        FROM trades ORDER BY closed_at
    """).fetchall()

    c.close()
    return [dict(s) for s in signals], [dict(t) for t in trades]

def extract_rsi_slope(reason_str):
    """Heuristic: parse RSI slope from reason text."""
    if reason_str is None:
        return 0.0
    reason = reason_str.lower()
    if "in salita" in reason or "positivo" in reason:
        return 0.5
    elif "in discesa" in reason or "negativo" in reason:
        return -0.5
    else:
        return 0.0

def mechanical_approve(signal):
    """Apply mechanical gates (Opzione A) to a signal."""
    direction = (signal.get("direction") or "").upper()
    vol_ratio = signal.get("volume_ratio") or 0.0
    adx = signal.get("adx") or 0.0

    if vol_ratio < VOLUME_RATIO_MIN:
        return False
    if adx < ADX_MIN:
        return False

    rsi_slope = extract_rsi_slope(signal.get("reason"))

    if direction == "SELL":  # SHORT
        if rsi_slope > RSI_SLOPE_MAX_SHORT:
            return False
    else:  # LONG
        if rsi_slope < -0.3:
            return False

    return True

def find_corresponding_trade(signal, trades, window_sec=3600):
    """Find the closest trade that matches pair, side, and timeframe.

    Returns the trade dict if found, else None.
    """
    pair = signal.get("pair")
    direction = (signal.get("direction") or "").upper()
    signal_ts = signal.get("ts")

    # Map direction to side
    side = "short" if direction == "SELL" else "long"

    # Look for trades on same pair, same side, within time window
    for trade in trades:
        if trade.get("pair") != pair or trade.get("side") != side:
            continue

        opened = trade.get("opened_at") or 0
        # If signal happened ~opened_at (within window), this is likely the trade
        if abs(opened - signal_ts) < window_sec:
            return trade

    return None

def main():
    signals, trades = load_data()

    print(f"Loaded {len(signals)} signals and {len(trades)} trades from DB\n")

    # Filter: mechanical approve only
    mech_approved = [s for s in signals if mechanical_approve(s)]
    print(f"Mechanical gates approved: {len(mech_approved)}/{len(signals)} ({100*len(mech_approved)//len(signals)}%)\n")

    # Try to map approved signals to actual trades
    matched_trades = []
    for sig in mech_approved:
        trade = find_corresponding_trade(sig, trades)
        if trade:
            matched_trades.append(trade)
            print(f"✓ {sig['pair']:8s} {sig.get('direction', '?'):5s} @ {datetime.fromtimestamp(sig['ts']).strftime('%Y-%m-%d %H:%M')} "
                  f"→ {trade['side']:5s} pnl={trade['pnl_eur']:+.2f}€ size={trade['size_eur']:.1f}€")
        else:
            print(f"✗ {sig['pair']:8s} {sig.get('direction', '?'):5s} @ {datetime.fromtimestamp(sig['ts']).strftime('%Y-%m-%d %H:%M')} "
                  f"→ NO MATCHING TRADE FOUND")

    print()
    print("=" * 70)
    print(f"MATCHED TRADES: {len(matched_trades)}/{len(mech_approved)}")
    print("=" * 70)

    if not matched_trades:
        print("\n⚠ No trades matched. This could mean:")
        print("  - The signals didn't result in actual trades (filtered by other gates)")
        print("  - The time window is too small")
        print("  - The trades were for a different pair or side")
        return

    # Calculate P&L
    total_pnl_eur = sum(t.get("pnl_eur", 0) for t in matched_trades)
    total_size_eur = sum(t.get("size_eur", 0) for t in matched_trades)
    avg_pnl_pct = (total_pnl_eur / total_size_eur * 100) if total_size_eur > 0 else 0.0
    win_count = sum(1 for t in matched_trades if (t.get("pnl_eur") or 0) > 0)

    print(f"\nMatched trades summary:")
    print(f"  Total trades: {len(matched_trades)}")
    print(f"  Win rate: {100*win_count//len(matched_trades)}% ({win_count}/{len(matched_trades)})")
    print(f"  Total P&L: {total_pnl_eur:+.2f}€")
    print(f"  Total size deployed: {total_size_eur:.2f}€")
    print(f"  Avg P&L %: {avg_pnl_pct:+.2f}%")

    # Scale to 100€ budget
    if total_size_eur > 0:
        scale_factor = BUDGET_EUR / total_size_eur
        projected_pnl = total_pnl_eur * scale_factor
        projected_pnl_pct = avg_pnl_pct
    else:
        projected_pnl = 0
        projected_pnl_pct = 0

    print()
    print("=" * 70)
    print(f"PROJECTION: WITH {BUDGET_EUR}€ BUDGET")
    print("=" * 70)
    print(f"Scale factor: {scale_factor:.2f}x (actual deployed {total_size_eur:.1f}€ → {BUDGET_EUR:.0f}€)")
    print(f"Projected P&L: {projected_pnl:+.2f}€")
    print(f"Projected P&L %: {projected_pnl_pct:+.2f}%")
    print(f"Final balance: {BUDGET_EUR + projected_pnl:.2f}€")
    print()

    if projected_pnl > 0:
        print(f"✅ WITH MECHANICAL GATES, you would have GAINED {projected_pnl:.2f}€")
    elif projected_pnl < 0:
        print(f"❌ WITH MECHANICAL GATES, you would have LOST {abs(projected_pnl):.2f}€")
    else:
        print(f"= Breakeven")

    print()
    print("⚠ NOTE: This assumes the matched trades are representative.")
    print("  Real P&L depends on exact entry timing, size allocation, and fees.")

if __name__ == "__main__":
    main()
