#!/usr/bin/env python3
"""
Backtest Opzione A: mechanical gates (no Groq) on historical signals.

Simulates approval/rejection of signals using rules:
- RSI slope(3h) not too positive (momentum down, not up)
- Volume ratio > threshold
- ADX > threshold
- (no Groq confidence check)

Compares:
- What Groq actually approved (0/165 in current DB)
- What mechanical rules would approve
- Expected P&L if we used mechanical rules instead
"""

import sqlite3
import sys
from pathlib import Path
from datetime import datetime

import os
DB_PATH = os.getenv("BOT_DB", str(Path(__file__).parent / "trading.db"))

# Mechanical gate thresholds (tunable)
RSI_SLOPE_MAX_SHORT = 0.3       # RSI slope(3h) > 0.3 = reject short (momentum up)
RSI_SLOPE_MIN_LONG = -0.3       # RSI slope(3h) < -0.3 = reject long (momentum down too much)
VOLUME_RATIO_MIN = 1.5          # volume_ratio < 1.5 = reject
ADX_MIN = 25                    # ADX < 25 = reject (weak trend)

def load_signals():
    """Load all Groq decision records with their features."""
    db_file = Path(DB_PATH)
    if not db_file.exists():
        print(f"DB not found: {DB_PATH}")
        sys.exit(1)

    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row

    signals = c.execute("""
        SELECT ts, pair, direction, decision, confidence, score, reason,
               rsi_1h, rsi_15m, macd_hist, volume_ratio, adx, price
        FROM groq_decisions
        ORDER BY ts
    """).fetchall()

    c.close()
    return [dict(s) for s in signals]

def extract_rsi_slope(reason_str):
    """Heuristic: parse RSI slope from reason text."""
    if reason_str is None:
        return 0.0
    reason = reason_str.lower()
    # Common patterns in Groq reasons:
    # "RSI slope(3h) in salita" = up (positive)
    # "RSI slope(3h) positivo" = up
    # "RSI slope(3h) in discesa" = down
    # "RSI slope(3h) negativo" = down

    if "in salita" in reason or "positivo" in reason:
        return 0.5  # Approximate: RSI slope up
    elif "in discesa" in reason or "negativo" in reason:
        return -0.5  # Approximate: RSI slope down
    else:
        return 0.0

def mechanical_approve(signal):
    """Apply mechanical gates (Opzione A) to a signal."""
    direction = (signal.get("direction") or "").upper()

    # Basic checks (common to both long/short)
    vol_ratio = signal.get("volume_ratio") or 0.0
    adx = signal.get("adx") or 0.0

    if vol_ratio < VOLUME_RATIO_MIN:
        return False, f"volume_ratio={vol_ratio:.2f} < {VOLUME_RATIO_MIN}"
    if adx < ADX_MIN:
        return False, f"adx={adx:.1f} < {ADX_MIN}"

    # Direction-specific checks
    rsi_slope = extract_rsi_slope(signal.get("reason"))

    if direction == "SELL":  # SHORT
        if rsi_slope > RSI_SLOPE_MAX_SHORT:
            return False, f"rsi_slope={rsi_slope:.2f} > {RSI_SLOPE_MAX_SHORT} (momentum up)"
    else:  # LONG (BUY)
        if rsi_slope < RSI_SLOPE_MIN_LONG:
            return False, f"rsi_slope={rsi_slope:.2f} < {RSI_SLOPE_MIN_LONG} (momentum down too much)"

    return True, "pass"

def main():
    signals = load_signals()

    print(f"Loaded {len(signals)} signals from DB\n")

    # Analyze Groq decisions vs. mechanical gates
    groq_approved = [s for s in signals if s["decision"] == "ESEGUI"]
    groq_rejected = [s for s in signals if s["decision"] == "RIFIUTA"]

    mech_approved = [s for s in signals if mechanical_approve(s)[0]]
    mech_rejected = [s for s in signals if not mechanical_approve(s)[0]]

    print("=" * 70)
    print("GROQ vs. MECHANICAL GATES")
    print("=" * 70)
    print(f"Groq APPROVED: {len(groq_approved):3d} ({100*len(groq_approved)//len(signals):2d}%)")
    print(f"Groq REJECTED: {len(groq_rejected):3d} ({100*len(groq_rejected)//len(signals):2d}%)")
    print()
    print(f"Mech APPROVED: {len(mech_approved):3d} ({100*len(mech_approved)//len(signals):2d}%)")
    print(f"Mech REJECTED: {len(mech_rejected):3d} ({100*len(mech_rejected)//len(signals):2d}%)")
    print()

    # Overlap analysis
    both_approved = len([s for s in signals if s["decision"] == "ESEGUI" and mechanical_approve(s)[0]])
    print(f"Both approved: {both_approved} (Groq AND mechanical)")
    print(f"Mech would have let in (Groq rejected): {len(mech_approved) - both_approved}")
    print()

    # Top rejection reasons by mechanism
    print("=" * 70)
    print("TOP REJECTION REASONS (Mechanical)")
    print("=" * 70)
    reasons_count = {}
    for s in mech_rejected:
        _, reason = mechanical_approve(s)
        reasons_count[reason] = reasons_count.get(reason, 0) + 1

    for reason, count in sorted(reasons_count.items(), key=lambda x: -x[1])[:10]:
        print(f"{count:3d}x {reason}")
    print()

    # Detailed output: show some mech-rejected examples
    print("=" * 70)
    print("SAMPLE: Groq REJECTED, Mechanical would APPROVE")
    print("=" * 70)
    candidates = [s for s in groq_rejected if mechanical_approve(s)[0]]
    for s in candidates[:5]:
        ts = datetime.fromtimestamp(s["ts"]).strftime("%Y-%m-%d %H:%M")
        direction = s.get("direction", "?").upper()
        print(f"{ts} {s['pair']:8s} {direction:5s} | Groq: {s['reason']}")
    if len(candidates) > 5:
        print(f"... and {len(candidates) - 5} more")
    print()

    # Summary
    print("=" * 70)
    print("CONCLUSION")
    print("=" * 70)
    if len(mech_approved) > len(groq_approved):
        delta = len(mech_approved) - len(groq_approved)
        print(f"✓ Mechanical gates would APPROVE {delta} more signals than Groq")
        print(f"  (Could increase trade frequency by {100*delta//(len(signals) or 1)}%)")
    elif len(mech_approved) < len(groq_approved):
        print(f"⚠ Mechanical gates are STRICTER than Groq (fewer approvals)")
    else:
        print(f"= Mechanical gates approve same count as Groq")

    print()
    print("⚠ NOTE: This is approval count only. Actual P&L requires mapping to")
    print("  executed trades, which is not available in the current data.")
    print("  To test live: deploy with mechanical gates and monitor 7–14 days.")

if __name__ == "__main__":
    main()
