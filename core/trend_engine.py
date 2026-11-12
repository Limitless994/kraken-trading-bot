"""
trend_engine.py — One trend-following trader for every market.

TrendTrader runs core/trend.py's rules against a Broker (core/broker.py). The
bot creates one per market — crypto on Kraken, stocks/ETFs on Alpaca paper — so
both share the same entry, exit, sizing and fill handling, and backtest_trend.py
simulates exactly these rules.

It is synchronous and does blocking network I/O: call scan()/monitor() from a
worker thread (asyncio.to_thread). Both return a list of human-readable event
strings for the caller to forward (Telegram); nothing here knows about Telegram.

Fills are never assumed. With real orders (execute_orders=True) a position is
opened or closed only on a confirmed fill; an order the broker has not confirmed
yet leaves the position marked `pending` and is resolved on a later monitor()
pass, so a slow confirmation can neither lose a position nor sell it twice.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

from . import trend
from .broker import Broker, DEAD, FILLED

logger = logging.getLogger(__name__)


@dataclass
class TrendMarket:
    name: str               # "crypto" | "stocks" — also the position's `market` tag
    broker: Broker
    symbols: list
    budget: float           # total capital for this market, in broker.currency
    execute_orders: bool    # False = simulate fills locally at the quote (no orders sent)
    slippage_pct: float     # per side; charged on simulated fills only
    max_entry_drift: float  # skip an entry when ask is this far above the signal close

    @property
    def slot(self) -> float:
        """Fixed equal slot per symbol — what backtest_trend.py simulates."""
        return round(self.budget / max(len(self.symbols), 1), 2)


class PositionStore:
    """Where a TrendTrader keeps its positions and closed trades."""

    def positions(self) -> dict:                      # {symbol: pos}
        raise NotImplementedError

    def save(self, symbol: str, pos: dict) -> None:
        raise NotImplementedError

    def remove(self, symbol: str) -> None:
        raise NotImplementedError

    def record_trade(self, trade: dict) -> None:
        raise NotImplementedError

    def gate(self, symbol: str, event: str) -> None:  # funnel analytics, optional
        pass


class TrendTrader:
    def __init__(self, market: TrendMarket, store: PositionStore) -> None:
        self.market = market
        self.store = store
        self._lock = threading.Lock()   # one scan/monitor pass at a time per market

    # ── helpers ───────────────────────────────────────────────────────────────

    def _tag(self) -> str:
        return f"TREND[{self.market.name}]"

    def _cur(self) -> str:
        return {"EUR": "€", "USD": "$"}.get(self.market.broker.currency, self.market.broker.currency)

    def _new_position(self, symbol: str, dec: trend.TrendDecision, price: float,
                      qty: float, order_id: str, pending: bool) -> dict:
        size = round(qty * price, 2)
        stop = round(trend.chandelier_stop(dec.close, dec.atr), 8)
        return {
            "pair":          symbol,
            "side":          "long",
            "mode":          "trend",
            "market":        self.market.name,
            "currency":      self.market.broker.currency,
            "entry":         price,
            "entry_signal":  dec.close,
            "size_crypto":   qty,              # quantity (coins or shares)
            "size_eur":      size,             # notional in `currency`
            "fee_entry_eur": round(size * self.market.broker.fee_rate, 4),
            "take_profit":   round(price * 100, 8),   # sentinel: no TP, winners run
            "stop_loss":     stop,
            "trailing_stop": stop,
            "peak_price":    dec.close,
            "atr":           dec.atr,
            "txid":          order_id,
            "opened_at":     time.time(),
            "order_type":    "market",
            "pending":       "entry" if pending else None,
        }

    def _apply_fill_to_entry(self, pos: dict, price: float, qty: float) -> None:
        pos["entry"]         = price
        pos["size_crypto"]   = qty
        pos["size_eur"]      = round(qty * price, 2)
        pos["fee_entry_eur"] = round(pos["size_eur"] * self.market.broker.fee_rate, 4)
        pos["take_profit"]   = round(price * 100, 8)
        pos["pending"]       = None

    def _close(self, symbol: str, pos: dict, exit_price: float, qty: float,
               reason: str, detail: str) -> str:
        """Book the closed trade, drop the position, return the event text."""
        m = self.market
        gross     = (exit_price - pos["entry"]) * qty
        fee_entry = pos.get("fee_entry_eur", 0.0)
        fee_exit  = round(exit_price * qty * m.broker.fee_rate, 4)
        # Real fills already contain the slippage; simulated ones get an estimate.
        slip      = 0.0 if m.execute_orders else round(pos["size_eur"] * m.slippage_pct * 2, 4)
        net       = gross - fee_entry - fee_exit - slip
        size      = pos["size_eur"] or 0.0
        net_pct   = net / size * 100 if size > 0 else 0.0

        self.store.record_trade({
            "pair": symbol, "side": "long", "exit_reason": reason,
            "market": m.name, "currency": m.broker.currency,
            "entry": pos["entry"], "exit": exit_price,
            "entry_signal": pos.get("entry_signal"),
            "qty": qty, "size_eur": size,
            "gross_pnl_eur": round(gross, 4),
            "fee_eur": round(fee_entry + fee_exit + slip, 4),
            "pnl_eur": round(net, 4), "pnl_pct": round(net_pct, 4),
            "opened_at": pos.get("opened_at"), "closed_at": time.time(),
        })
        self.store.remove(symbol)
        logger.info("%s EXIT %s @%.6f  net=%+.2f%s (%s)",
                    self._tag(), symbol, exit_price, net, self._cur(), detail)
        icon = "✅" if net >= 0 else "🛑"
        return (f"{icon} TREND EXIT {symbol}  {net:+.2f}{self._cur()} ({net_pct:+.2f}%)\n"
                f"{detail}")

    # ── scan: entries ─────────────────────────────────────────────────────────

    def scan(self, available: float = None, blocked: bool = False) -> list:
        """Look for breakouts on symbols without a position. Returns event texts.

        `available` caps what may be spent this pass (None = the market budget
        minus what is already deployed). `blocked` is the caller's protections.
        """
        if not self._lock.acquire(blocking=False):
            return []
        try:
            return self._scan(available, blocked)
        finally:
            self._lock.release()

    def _scan(self, available, blocked: bool) -> list:
        m, events = self.market, []
        if blocked:
            logger.info("%s scan: entrate bloccate dalle protezioni — skip", self._tag())
            return events
        if not m.broker.is_market_open():
            logger.info("%s scan: mercato chiuso — ingressi alla riapertura", self._tag())
            return events

        positions = self.store.positions()
        deployed = sum(p.get("size_eur") or 0.0 for p in positions.values())
        room = max(m.budget - deployed, 0.0)
        available = room if available is None else min(available, room)

        for symbol in m.symbols:
            if symbol in positions:
                continue
            try:
                dec = trend.evaluate_entry(m.broker.daily_candles(symbol))
            except Exception as exc:
                logger.warning("%s %s: dati daily non disponibili (%s)", self._tag(), symbol, exc)
                continue
            if dec is None:
                logger.info("%s %s: storico daily insufficiente", self._tag(), symbol)
                continue
            if not dec.enter:
                logger.info("%s %s: %s", self._tag(), symbol, dec.reason)
                self.store.gate(symbol, "trend_no_breakout")
                continue

            size = min(m.slot, available)
            if size < m.broker.min_order:
                logger.info("%s %s: capitale insufficiente (%.2f < %.2f)",
                            self._tag(), symbol, size, m.broker.min_order)
                continue

            try:
                ask = float(m.broker.quote(symbol)["ask"])
            except Exception as exc:
                logger.warning("%s %s: quotazione non disponibile (%s)", self._tag(), symbol, exc)
                continue
            # The signal is the last daily close. If the bot was down and price
            # has since run away, entering now is not the trade that was tested.
            if ask > dec.close * (1 + m.max_entry_drift):
                logger.info("%s %s: ask %.6g oltre +%.1f%% dal segnale %.6g — skip",
                            self._tag(), symbol, ask, m.max_entry_drift * 100, dec.close)
                self.store.gate(symbol, "trend_entry_drift")
                continue

            self.store.gate(symbol, "signal_ok")
            event = self._enter(symbol, dec, ask, size)
            if event:
                events.append(event)
                available -= size
        return events

    def _enter(self, symbol: str, dec: trend.TrendDecision, ask: float, size: float):
        m = self.market
        # Lot rounding matters only for a real order (and may cost an API call).
        qty = m.broker.round_qty(symbol, size / ask) if m.execute_orders else size / ask
        if qty <= 0:
            return None

        if not m.execute_orders:
            pos = self._new_position(symbol, dec, ask, qty, "PAPER", pending=False)
        else:
            try:
                order_id = m.broker.market_buy(symbol, qty)
            except Exception as exc:
                logger.error("%s entry FAILED %s: %s", self._tag(), symbol, exc)
                return f"🚨 TREND ingresso {symbol} FALLITO\n{exc}"
            fill = m.broker.order_fill(order_id)
            if fill.status == DEAD:
                logger.warning("%s %s: ordine %s annullato dal broker", self._tag(), symbol, order_id)
                return f"⚠️ TREND {symbol}: ordine di ingresso annullato dal broker ({order_id})"
            pos = self._new_position(symbol, dec, ask, qty, order_id,
                                     pending=fill.status != FILLED)
            if fill.status == FILLED:
                self._apply_fill_to_entry(pos, fill.price, fill.qty)

        self.store.save(symbol, pos)
        if pos["pending"]:
            logger.warning("%s %s: fill di ingresso non ancora confermato (ordine %s)",
                           self._tag(), symbol, pos["txid"])
            return (f"⏳ TREND {symbol}: ordine di ingresso inviato, fill non ancora "
                    f"confermato — verifico al prossimo ciclo")
        self.store.gate(symbol, "entry_fill")
        logger.info("%s ENTRY %s @%.6f  stop=%.6f  size=%.2f%s  TX %s", self._tag(), symbol,
                    pos["entry"], pos["stop_loss"], pos["size_eur"], self._cur(), pos["txid"])
        return (f"🌊 TREND ENTRY {symbol} LONG @{pos['entry']:.6f}\n"
                f"{dec.reason}\n"
                f"Stop chandelier: {pos['stop_loss']:.6f}  |  "
                f"Size: {pos['size_eur']:.2f}{self._cur()}\n"
                f"Nessun TP — si esce quando il trend muore.")

    # ── monitor: pending orders and exits ─────────────────────────────────────

    def monitor(self) -> list:
        """Resolve pending orders and apply the chandelier exit. Returns event texts."""
        if not self._lock.acquire(blocking=False):
            return []
        try:
            events = []
            for symbol, pos in list(self.store.positions().items()):
                if pos.get("mode") != "trend":
                    continue      # e.g. a legacy momentum position in the same store
                try:
                    event = self._monitor_one(symbol, pos)
                except Exception as exc:
                    logger.error("%s monitor error for %s: %s", self._tag(), symbol, exc)
                    continue
                if event:
                    events.append(event)
            return events
        finally:
            self._lock.release()

    def _monitor_one(self, symbol: str, pos: dict):
        m = self.market

        # 1. An order sent earlier whose fill was not confirmed at the time.
        if pos.get("pending") == "entry":
            fill = m.broker.order_fill(pos["txid"], wait_s=2.0)
            if fill.status == FILLED:
                self._apply_fill_to_entry(pos, fill.price, fill.qty)
                self.store.save(symbol, pos)
                self.store.gate(symbol, "entry_fill")
                return (f"🌊 TREND ENTRY {symbol} confermato @{pos['entry']:.6f}  "
                        f"Size: {pos['size_eur']:.2f}{self._cur()}")
            if fill.status == DEAD:
                self.store.remove(symbol)
                return f"⚠️ TREND {symbol}: ordine di ingresso annullato, nessuna posizione aperta"
            return None
        if pos.get("pending") == "exit":
            fill = m.broker.order_fill(pos["exit_txid"], wait_s=2.0)
            if fill.status == FILLED:
                return self._close(symbol, pos, fill.price, fill.qty, "chandelier",
                                   f"Chandelier — fill confermato, TX {pos['exit_txid']}")
            if fill.status == DEAD:
                pos["pending"] = None
                pos.pop("exit_txid", None)
                self.store.save(symbol, pos)
                return (f"⚠️ TREND {symbol}: ordine di chiusura annullato dal broker — "
                        f"posizione mantenuta, riprovo")
            return None

        # 2. Chandelier exit on completed daily closes.
        res = trend.evaluate_exit(m.broker.daily_candles(symbol),
                                  pos.get("peak_price") or pos["entry"])
        if res is None:
            return None
        new_stop = round(res["stop"], 8)
        if res["peak"] != pos.get("peak_price") or new_stop != pos.get("trailing_stop"):
            pos["peak_price"], pos["trailing_stop"], pos["atr"] = res["peak"], new_stop, res["atr"]
            self.store.save(symbol, pos)
            logger.info("%s %s: peak=%.6f  stop=%.6f", self._tag(), symbol, res["peak"], new_stop)
        if not res["exit"]:
            return None
        if not m.broker.is_market_open():
            return None          # stocks: the exit goes out when the session reopens

        detail = f"Chandelier: chiusura daily {res['close']:.6f} < stop {res['stop']:.6f}"
        qty = pos["size_crypto"]
        if not m.execute_orders:
            bid = float(m.broker.quote(symbol)["bid"])
            return self._close(symbol, pos, bid, qty, "chandelier", f"{detail}\nTX: PAPER")

        try:
            order_id = m.broker.market_sell(symbol, qty)
        except Exception as exc:
            logger.error("%s close FAILED %s: %s — posizione mantenuta", self._tag(), symbol, exc)
            return f"🚨 Chiusura TREND {symbol} FALLITA — riprovo al prossimo ciclo\n{exc}"
        fill = m.broker.order_fill(order_id)
        if fill.status == FILLED:
            return self._close(symbol, pos, fill.price, fill.qty, "chandelier",
                               f"{detail}\nTX: {order_id}")
        if fill.status == DEAD:
            return (f"⚠️ TREND {symbol}: ordine di chiusura annullato dal broker — "
                    f"posizione mantenuta, riprovo")
        # Sent but unconfirmed: remember the order so the next pass resolves it
        # instead of selling a second time.
        pos["pending"], pos["exit_txid"] = "exit", order_id
        self.store.save(symbol, pos)
        return (f"⏳ TREND {symbol}: ordine di chiusura inviato, fill non ancora "
                f"confermato — verifico al prossimo ciclo")
