"""
telegram_bot.py — Telegram bot interface for the Kraken trading system.

Flow:
  1. Job queue scans markets every SCAN_INTERVAL_MINUTES.
  2. Each signal above threshold is sent to Groq for evaluation.
  3. If Groq approves → market order placed automatically on Kraken.
     If Groq rejects  → Telegram notification with the rejection reason.
  4. Separate 60-second job monitors open positions for TP / SL.
  5. On TP or SL hit → auto market-sell + Telegram notification.

Commands:
  /start      — welcome + instructions
  /status     — bot state summary
  /positions  — current open positions with live P&L
  /balance    — Kraken EUR + crypto balances
  /scan       — trigger an immediate manual market scan
  /close XYZ  — manually close position for pair XYZ
  /profile    — show active profile or switch (/profile aggressive|conservative)
"""
from __future__ import annotations

import asyncio
import datetime
import json
import logging
import threading
import time
from pathlib import Path

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

from core import config, db, kraken_client, market_data
from core.analyzer import analyze
from core.strategy import Signal, generate_signal, generate_short_signal, kelly_size_pct
from services import discovery, groq_evaluator
from services.groq_evaluator import GroqUnavailableError

logger = logging.getLogger(__name__)

_DIR                = Path(__file__).parent.parent   # project root
PENDING_ORDERS_FILE = _DIR / "pending_orders.json"
DYNAMIC_PAIRS_FILE  = _DIR / "dynamic_pairs.json"


# ── Misc helpers (non-persistence) ────────────────────────────────────────────

def _load_json(path: "Path", default):
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default
    except Exception:
        return default


def _margin_open_fee(pair: str) -> float:
    """Kraken margin opening fee: 0.01% for BTC/ETH, 0.02% for altcoins."""
    return config.MARGIN_OPEN_FEE_BTC if pair in config._BTC_PAIRS else config.MARGIN_OPEN_FEE_ALT


def _margin_rollover_rate(pair: str) -> float:
    """Kraken rollover fee per 4h: 0.01% for BTC/ETH, 0.02% for altcoins."""
    return config.MARGIN_ROLLOVER_FEE_BTC if pair in config._BTC_PAIRS else config.MARGIN_ROLLOVER_FEE_ALT


def _funding_cost_eur_pure(pos: dict, current_ts: float) -> float:
    """Pure version: accepts timestamp so historical backtest code can use it."""
    if pos.get("side") != "short":
        return 0.0
    holding_seconds = max(0.0, current_ts - pos.get("opened_at", current_ts))
    periods = holding_seconds / (4 * 3600)
    rate = _margin_rollover_rate(pos.get("pair", ""))
    return round(periods * rate * pos["size_eur"], 4)


def _funding_cost_eur(pos: dict) -> float:
    """Accumulated Kraken margin rollover cost for an open short position (charged every 4h)."""
    return _funding_cost_eur_pure(pos, time.time())


def _update_trailing_stop(pos: dict, current: float, atr: float, cfg) -> bool:
    """Update trailing stop in pos dict in-place. Returns True if the level was updated."""
    if not cfg.TRAILING_STOP_ENABLED:
        return False
    old_trailing = pos.get("trailing_stop", pos["stop_loss"])
    entry = pos["entry"]
    side  = pos.get("side", "long")
    if side == "short":
        activation = (entry - cfg.TRAILING_ACTIVATION_ATR * atr) if atr > 0 else entry * (1 - cfg.TRAILING_STOP_PCT)
        if current <= activation:
            new_trailing = (round(current + cfg.TRAILING_DISTANCE_ATR * atr, 8) if atr > 0
                            else round(current * (1 + cfg.TRAILING_STOP_PCT), 8))
            # Never set trailing above entry — would guarantee a loss on reversal
            new_trailing = min(new_trailing, entry)
            if new_trailing < old_trailing:
                pos["trailing_stop"] = new_trailing
                return True
    else:
        activation = (entry + cfg.TRAILING_ACTIVATION_ATR * atr) if atr > 0 else entry * (1 + cfg.TRAILING_STOP_PCT)
        if current >= activation:
            new_trailing = (round(current - cfg.TRAILING_DISTANCE_ATR * atr, 8) if atr > 0
                            else round(current * (1 - cfg.TRAILING_STOP_PCT), 8))
            # Never set trailing below entry — would guarantee a loss on reversal
            new_trailing = max(new_trailing, entry)
            if new_trailing > old_trailing:
                pos["trailing_stop"] = new_trailing
                return True
    return False


def _check_exit_signals(pos: dict, current: float) -> tuple:
    """Returns (hit_tp, hit_sl) based on current price and effective trailing stop."""
    side        = pos.get("side", "long")
    effective_sl = pos.get("trailing_stop", pos["stop_loss"])
    if side == "short":
        return current <= pos["take_profit"], current >= effective_sl
    return current >= pos["take_profit"], current <= effective_sl


def _adaptive_slippage(atr: float, price: float) -> float:
    """Slippage estimate based on current ATR; capped at 0.2%, floored at config value.
    Real Kraken slippage on liquid alts (SOL, DOGE) for <100€ orders is 0.01–0.15%.
    The old 1% cap was 5-10× too high and was eating all profit on small-margin exits."""
    atr_pct = atr / price
    return max(config.SLIPPAGE_PCT, min(atr_pct * 0.5, 0.002))


def _calc_exit(pos: dict, exit_price: float, cfg, current_ts: float = None) -> dict:
    """Calculate net P&L for closing a position. current_ts defaults to time.time()."""
    if current_ts is None:
        current_ts = time.time()
    side = pos.get("side", "long")
    gross_pnl_eur = (
        (pos["entry"] - exit_price) if side == "short" else (exit_price - pos["entry"])
    ) * pos["size_crypto"]
    fee_exit     = round(exit_price * pos["size_crypto"] * cfg.KRAKEN_TAKER_FEE, 4)
    fee_entry    = pos.get("fee_entry_eur", 0.0)
    atr          = pos.get("atr", 0.0)
    entry        = pos.get("entry", 0.0)
    slip_pct     = _adaptive_slippage(atr, entry) if atr > 0 and entry > 0 else cfg.SLIPPAGE_PCT
    slip_eur     = round(pos["size_eur"] * slip_pct * 2, 4)
    funding_cost = _funding_cost_eur_pure(pos, current_ts)
    net_pnl_eur  = gross_pnl_eur - fee_entry - fee_exit - slip_eur - funding_cost
    size_eur     = pos["size_eur"]
    return {
        "gross_pnl_eur": round(gross_pnl_eur, 4),
        "fee_exit":      fee_exit,
        "fee_entry":     fee_entry,
        "slip_eur":      slip_eur,
        "funding_cost":  funding_cost,
        "net_pnl_eur":   round(net_pnl_eur, 4),
        "net_pnl_pct":   round(net_pnl_eur / size_eur * 100 if size_eur > 0 else 0.0, 4),
    }


# ── Bot ───────────────────────────────────────────────────────────────────────

class TradingBot:
    def __init__(self) -> None:
        self.app = Application.builder().token(config.TELEGRAM_TOKEN).build()
        self.positions: dict = db.get_positions()
        self._positions_lock = threading.Lock()   # guards in-memory dict + DB writes
        self._scanning   = False   # lock scan
        self._discovering = False  # lock discovery
        self._in_flight: set = set()  # per-scan idempotency guard
        self._register_handlers()

    # ── Handler registration ──────────────────────────────────────────────────

    def _register_handlers(self) -> None:
        add = self.app.add_handler
        add(CommandHandler("start",     self._cmd_start))
        add(CommandHandler("status",    self._cmd_status))
        add(CommandHandler("positions", self._cmd_positions))
        add(CommandHandler("balance",   self._cmd_balance))
        add(CommandHandler("scan",      self._cmd_scan))
        add(CommandHandler("close",     self._cmd_close))
        add(CommandHandler("discover",  self._cmd_discover))
        add(CommandHandler("ai",        self._cmd_ai))
        add(CommandHandler("profile",   self._cmd_profile))

    # ── Commands ──────────────────────────────────────────────────────────────

    async def _cmd_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(
            "Kraken Trading Bot (Groq AI)\n\n"
            "Monitora RSI, MACD e volume su coppie crypto EUR.\n"
            "Groq valuta ogni segnale automaticamente ed esegue l'ordine.\n"
            "Tu ricevi solo notifiche: approvato+eseguito oppure rifiutato+motivo.\n\n"
            "Comandi:\n"
            "/status     — stato sistema\n"
            "/positions  — posizioni aperte con P&L live\n"
            "/balance    — saldo Kraken\n"
            "/scan       — scansione manuale immediata\n"
            "/close XYZ  — chiudi posizione su XYZ (es. /close SOLEUR)\n"
            "/ai [N]     — ultime N decisioni Groq (default 5)\n"
            "/discover   — discovery manuale nuove coppie\n"
            "/profile    — mostra/cambia profilo di rischio\n\n"
            f"Profilo: {config.ACTIVE_PROFILE.upper()}  |  "
            f"TP: +{config.TAKE_PROFIT_PCT*100:.0f}%  "
            f"SL: -{config.STOP_LOSS_PCT*100:.0f}%"
        )

    async def _cmd_status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            kraken_status, kraken_ts = await asyncio.to_thread(kraken_client.get_system_status)
            kraken_line = f"Kraken: {kraken_status.upper()}"
        except Exception as exc:
            kraken_line = f"Kraken: impossibile verificare ({exc})"

        mode = "PAPER TRADING" if config.PAPER_TRADING else "LIVE"
        dynamic = _load_json(DYNAMIC_PAIRS_FILE, [])
        extra = [p for p in dynamic if p not in config.PAIRS]
        pairs_line = f"Coppie: {', '.join(config.PAIRS)}"
        if extra:
            pairs_line += f"\nDiscovery: {', '.join(extra)}"
        await update.message.reply_text(
            f"Sistema: ATTIVO ({mode})\n"
            f"{kraken_line}\n"
            f"Profilo: {config.ACTIVE_PROFILE.upper()}\n"
            f"Decisioni: Groq AI ({config.GROQ_MODEL})\n"
            f"Posizioni aperte: {len(self.positions)} / {config.MAX_OPEN_TRADES}\n"
            f"{pairs_line}\n"
            f"Intervallo scansione: {config.SCAN_INTERVAL_MINUTES} min\n"
            f"TP: +{config.TAKE_PROFIT_PCT*100:.0f}%  "
            f"SL: -{config.STOP_LOSS_PCT*100:.0f}%  "
            f"Fee: {config.KRAKEN_TAKER_FEE*100:.2f}% per lato"
        )

    async def _cmd_positions(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.positions:
            await update.message.reply_text("Nessuna posizione aperta.")
            return

        lines: list[str] = []
        for pair, pos in self.positions.items():
            try:
                ticker = await asyncio.to_thread(kraken_client.get_ticker, pair)
                current = ticker["last"]
                side = pos.get("side", "long")
                if side == "short":
                    pnl_pct = (pos["entry"] - current) / pos["entry"] * 100
                    pnl_eur = (pos["entry"] - current) * pos["size_crypto"]
                else:
                    pnl_pct = (current - pos["entry"]) / pos["entry"] * 100
                    pnl_eur = (current - pos["entry"]) * pos["size_crypto"]
                label = "SHORT" if side == "short" else "LONG"
                funding = _funding_cost_eur(pos)
                net_pnl = pnl_eur - funding
                funding_line = f"\n  Funding: -{funding:.4f}€  Net: {net_pnl:+.2f}€" if funding > 0 else ""
                lines.append(
                    f"{pair} [{label}]  {pnl_pct:+.2f}%  ({pnl_eur:+.2f}€){funding_line}\n"
                    f"  Entry: {pos['entry']:.6f}  |  Now: {current:.6f}\n"
                    f"  TP: {pos['take_profit']:.6f}  |  SL: {pos['stop_loss']:.6f}"
                )
            except Exception as exc:
                lines.append(f"{pair}: errore recupero prezzo — {exc}")

        await update.message.reply_text("\n\n".join(lines))

    async def _cmd_balance(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            bal = await asyncio.to_thread(kraken_client.get_balance)
            eur = bal.get("ZEUR", bal.get("EUR", 0.0))
            lines = [f"EUR disponibile: {eur:.2f} €"]
            for k, v in bal.items():
                if k not in ("ZEUR", "EUR"):
                    lines.append(f"{k}: {v:.6f}")
            await update.message.reply_text("\n".join(lines))
        except Exception as exc:
            await update.message.reply_text(f"Errore saldo: {exc}")

    async def _cmd_scan(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if self._scanning:
            await update.message.reply_text("Scansione già in corso — attendi.")
            return
        await update.message.reply_text("Scansione manuale in corso...")
        self._scanning = True
        try:
            found = await self._run_scan_inner()
        except Exception as exc:
            logger.error("Scan error: %s", exc, exc_info=True)
            await update.message.reply_text(
                f"Errore durante la scansione:\n{type(exc).__name__}: {exc}"
            )
            return
        finally:
            self._scanning = False
        if not found:
            await update.message.reply_text("Nessun segnale trovato.")

    async def _do_close(self, pair: str) -> str:
        """Execute market close for pair. Returns notification message."""
        pos = self.positions[pair]
        ticker  = await asyncio.to_thread(kraken_client.get_ticker, pair)
        current = ticker["last"]
        side    = pos.get("side", "long")

        if config.PAPER_TRADING:
            txid = "PAPER"
        else:
            vol_str = kraken_client.format_volume(pair, pos["size_crypto"])
            if side == "short":
                order = await asyncio.to_thread(
                    kraken_client.add_order, pair, "buy", "market", vol_str,
                    leverage=config.MARGIN_LEVERAGE,
                )
            else:
                order = await asyncio.to_thread(
                    kraken_client.add_order, pair, "sell", "market", vol_str
                )
            txids = order.get("txid", [])
            txid  = txids[0] if txids else "unknown"

        exit_data     = _calc_exit(pos, current, config)
        gross_pnl_eur = exit_data["gross_pnl_eur"]
        fee_exit      = exit_data["fee_exit"]
        fee_entry     = exit_data["fee_entry"]
        slip_eur      = exit_data["slip_eur"]
        funding_cost  = exit_data["funding_cost"]
        net_pnl_eur   = exit_data["net_pnl_eur"]
        net_pnl_pct   = exit_data["net_pnl_pct"]

        with self._positions_lock:
            del self.positions[pair]
            db.delete_position(pair)

        db.insert_trade({
            "pair": pair, "side": side, "exit_reason": "manual",
            "entry": pos["entry"], "exit": current,
            "size_eur": pos["size_eur"],
            "gross_pnl_eur": round(gross_pnl_eur, 4),
            "fee_eur": round(fee_entry + fee_exit + slip_eur + funding_cost, 4),
            "pnl_eur": round(net_pnl_eur, 4),
            "pnl_pct": round(net_pnl_pct, 4),
            "opened_at": pos.get("opened_at"),
            "closed_at": time.time(),
        })
        funding_line = f"Funding cost: -{funding_cost:.4f}€\n" if funding_cost > 0 else ""
        return (
            f"Posizione chiusa: {pair}\n"
            f"P&L lordo: {gross_pnl_eur:+.2f}€\n"
            f"Fee totali: -{fee_entry + fee_exit:.4f}€\n"
            f"{funding_line}"
            f"P&L netto: {net_pnl_eur:+.2f}€  ({net_pnl_pct:+.2f}%)\n"
            f"TX: {txid}"
        )

    async def _cmd_close(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not ctx.args:
            await update.message.reply_text("Uso: /close <pair>\nEsempio: /close SOLEUR")
            return
        pair = ctx.args[0].upper()
        if pair not in self.positions:
            await update.message.reply_text(f"Nessuna posizione aperta per {pair}.")
            return
        try:
            msg = await self._do_close(pair)
            await update.message.reply_text(msg)
        except Exception as exc:
            await update.message.reply_text(f"Errore chiusura {pair}: {exc}")

    # ── Signal execution ──────────────────────────────────────────────────────

    async def _execute_signal(self, signal: Signal, groq_reason: str) -> None:
        is_short = signal.direction == "SELL"
        try:
            use_limit   = config.LIMIT_ORDERS_ENABLED and not config.PAPER_TRADING and not is_short
            fee_rate    = config.KRAKEN_MAKER_FEE if use_limit else config.KRAKEN_TAKER_FEE
            extra_fee   = _margin_open_fee(signal.pair) if is_short else 0.0
            fee_entry   = round(signal.size_eur * (fee_rate + extra_fee), 4)
            if is_short:
                size_crypto_net = signal.size_eur / signal.price
            else:
                size_crypto_net = signal.size_eur * (1 - fee_rate) / signal.price

            if config.PAPER_TRADING:
                if config.LIMIT_ORDERS_ENABLED and not is_short:
                    limit_price = round(signal.price * (1 - config.LIMIT_OFFSET_PCT), 8)
                    size_crypto_net = signal.size_eur * (1 - fee_rate) / limit_price
                    actual_price = limit_price
                else:
                    actual_price = signal.price
                txid  = "PAPER"
                descr = ""

            elif is_short:
                vol_str = kraken_client.format_volume(signal.pair, signal.size_crypto)
                result  = await asyncio.to_thread(
                    kraken_client.add_order,
                    signal.pair, "sell", "market", vol_str,
                    leverage=config.MARGIN_LEVERAGE,
                )
                txids = result.get("txid", [])
                txid  = txids[0] if txids else "unknown"
                actual_price = signal.price

            elif use_limit:
                limit_price = round(signal.price * (1 - config.LIMIT_OFFSET_PCT), 8)
                vol_str     = kraken_client.format_volume(signal.pair, signal.size_crypto)
                result      = await asyncio.to_thread(
                    kraken_client.add_order,
                    signal.pair, "buy", "limit", vol_str,
                    price=str(limit_price),
                )
                txids = result.get("txid", [])
                txid  = txids[0] if txids else "unknown"
                descr = result.get("descr", {}).get("order", "")
                actual_price = limit_price
                with self._positions_lock:
                    pending = _load_json(PENDING_ORDERS_FILE, {})
                    pending[signal.pair] = {
                        "txid": txid, "limit_price": limit_price,
                        "signal_id": signal.id, "size_eur": signal.size_eur,
                        "size_crypto": signal.size_crypto, "placed_at": time.time(),
                        "timeout_at": time.time() + config.LIMIT_TIMEOUT_SEC,
                        "take_profit": signal.take_profit, "stop_loss": signal.stop_loss,
                        "trailing_stop": signal.trailing_stop,
                    }
                    PENDING_ORDERS_FILE.write_text(
                        json.dumps(pending, indent=2), encoding="utf-8"
                    )
                await self.app.bot.send_message(
                    chat_id=config.TELEGRAM_CHAT_ID,
                    text=(
                        f"LIMIT ORDER {signal.pair}  {signal.size_eur:.2f}€  "
                        f"@{limit_price:.6f}  TX {txid}\n"
                        f"Scade in {config.LIMIT_TIMEOUT_SEC//60} min se non eseguito"
                    ),
                )
                return

            else:
                vol_str     = kraken_client.format_volume(signal.pair, signal.size_crypto)
                result      = await asyncio.to_thread(
                    kraken_client.add_order,
                    signal.pair, "buy", "market", vol_str,
                )
                txids = result.get("txid", [])
                txid  = txids[0] if txids else "unknown"
                descr = result.get("descr", {}).get("order", "")
                actual_price = signal.price
                size_crypto_net = signal.size_crypto

            pos_dict = {
                "pair":          signal.pair,
                "side":          "short" if is_short else "long",
                "entry":         actual_price if config.PAPER_TRADING else signal.price,
                "size_crypto":   size_crypto_net,
                "size_eur":      signal.size_eur,
                "fee_entry_eur": fee_entry,
                "take_profit":   signal.take_profit,
                "stop_loss":     signal.stop_loss,
                "trailing_stop": signal.trailing_stop,
                "atr":           signal.atr,
                "txid":          txid,
                "opened_at":     time.time(),
                "order_type":    "limit" if (use_limit and not is_short) else "market",
            }
            with self._positions_lock:
                self.positions[signal.pair] = pos_dict
                db.insert_position(signal.pair, pos_dict)

            mode  = "[PAPER] " if config.PAPER_TRADING else ""
            label = "SHORT" if is_short else "BUY"
            await self.app.bot.send_message(
                chat_id=config.TELEGRAM_CHAT_ID,
                text=(
                    f"{mode}{label} {signal.pair}  {signal.size_eur:.2f}€  @{signal.price:.6f}\n"
                    f"TP {signal.take_profit:.6f}  SL {signal.stop_loss:.6f}\n"
                    f"Groq {groq_reason}\n"
                    f"Score {signal.score}/6  ADX {signal.adx:.1f}  TX {txid}"
                ),
            )
            logger.info("Order placed: %s TX=%s fee=%.4f paper=%s", signal.pair, txid, fee_entry, config.PAPER_TRADING)

        except Exception as exc:
            logger.error("Order failed for %s: %s", signal.pair, exc)
            await self.app.bot.send_message(
                chat_id=config.TELEGRAM_CHAT_ID,
                text=f"ERRORE ORDINE — {signal.pair}\n{exc}",
            )

    # ── Background jobs ───────────────────────────────────────────────────────

    async def _job_scan(self, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if self._scanning:
            logger.info("Scan già in corso — skip")
            return
        self._scanning = True
        try:
            await self._run_scan_inner()
        finally:
            self._scanning = False

    async def _run_scan_inner(self) -> bool:
        """Returns True if at least one signal was evaluated (approved or rejected)."""
        try:
            kraken_status, _ = await asyncio.to_thread(kraken_client.get_system_status)
            if kraken_status != kraken_client.KRAKEN_ONLINE:
                logger.warning("Kraken not online: %s — skipping scan.", kraken_status)
                await self.app.bot.send_message(
                    chat_id=config.TELEGRAM_CHAT_ID,
                    text=f"KRAKEN NON OPERATIVO: {kraken_status.upper()}\nScansione saltata.",
                )
                return False
        except Exception as exc:
            logger.error("Cannot reach Kraken status API: %s — skipping scan.", exc)
            return False

        if len(self.positions) >= config.MAX_OPEN_TRADES:
            logger.info("Max open trades reached (%d), skipping scan.", config.MAX_OPEN_TRADES)
            return False

        if config.PAPER_TRADING:
            total = config.BUDGET_EUR
            spent = sum(p["size_eur"] for p in self.positions.values())
            available_eur = max(total - spent, 0.0)
        else:
            try:
                bal = await asyncio.to_thread(kraken_client.get_balance)
                total = bal.get("ZEUR", bal.get("EUR", 0.0)) + sum(p["size_eur"] for p in self.positions.values())
                available_eur = bal.get("ZEUR", bal.get("EUR", 0.0))
            except Exception as exc:
                logger.error("Cannot fetch balance: %s", exc)
                return False

        spent       = sum(p["size_eur"] for p in self.positions.values())
        max_allowed = config.BUDGET_EUR * config.MAX_CAPITAL_DEPLOYED
        if spent >= max_allowed:
            logger.info("Capital cap raggiunto (%.2f€ / %.2f€) — skip scan.", spent, max_allowed)
            return False

        available_eur = min(available_eur, max_allowed - spent)

        if available_eur < 10.0:
            logger.info("EUR disponibile insufficiente (%.2f€) — skip scan.", available_eur)
            return False

        already_open = set(self.positions.keys())
        self._in_flight.clear()
        signal_found = False

        # ── Circuit breaker ───────────────────────────────────────────────────
        if config.CIRCUIT_BREAKER_ENABLED:
            trades    = db.get_trades()
            today_ts  = time.time() - 86400
            daily_loss = sum(
                t["pnl_eur"] for t in trades
                if t.get("closed_at", 0) > today_ts and t["pnl_eur"] < 0
            )
            if abs(daily_loss) >= config.CIRCUIT_BREAKER_LOSS_EUR:
                logger.info("SCAN BLOCKED (all pairs): circuit_breaker (perdita %.2f€ >= %.2f€)", abs(daily_loss), config.CIRCUIT_BREAKER_LOSS_EUR)
                await self.app.bot.send_message(
                    chat_id=config.TELEGRAM_CHAT_ID,
                    text=f"CIRCUIT BREAKER — perdita {abs(daily_loss):.2f}€ nelle ultime 24h. Trading sospeso.",
                )
                return False

        # ── Market context (Fear & Greed, funding rates) ──────────────────────
        mkt_ctx = await asyncio.to_thread(market_data.get_market_context)
        fg_val  = mkt_ctx.get("fear_greed_value")

        long_blocked_fg = config.FEAR_GREED_ENABLED and market_data.fear_greed_blocks_trading(fg_val)
        short_blocked_fg = config.FEAR_GREED_ENABLED and market_data.fear_greed_blocks_short(fg_val)

        if long_blocked_fg:
            label = mkt_ctx.get("fear_greed_label", "")
            logger.info("LONG BLOCKED: fear_greed=%d (%s)", fg_val, label)

        # ── Kelly sizing from trade history ───────────────────────────────────
        trades_history = db.get_trades()
        kelly_pct = kelly_size_pct(trades_history)

        # ── BTC market filter — single fetch for the entire scan ──────────────
        btc_analysis = None
        if config.BTC_FILTER_ENABLED:
            btc_analysis = await asyncio.to_thread(analyze, config.BTC_PAIR)
            if btc_analysis:
                _btc_blocks = (
                    (config.BTC_EMA_FILTER and not btc_analysis.price_above_ema50) or
                    btc_analysis.rsi_1h > config.BTC_RSI_MAX
                )
                logger.info(
                    "BTC filter: RSI=%.1f  EMA50=%s  ADX=%.1f%s",
                    btc_analysis.rsi_1h,
                    "OK" if btc_analysis.price_above_ema50 else "SOTTO",
                    btc_analysis.adx,
                    " → bloccherà long non-BTC" if _btc_blocks else " → OK",
                )
                # Persist BTC filter state for the dashboard
                db.set_setting("btc_filter_state", json.dumps({
                    "rsi":   btc_analysis.rsi_1h,
                    "ema50": "OK" if btc_analysis.price_above_ema50 else "SOTTO",
                    "adx":   btc_analysis.adx,
                }))

        dynamic = _load_json(DYNAMIC_PAIRS_FILE, [])
        scan_pairs = list(dict.fromkeys(config.PAIRS + dynamic))

        analysis_cache: dict = {}

        for pair in ([] if long_blocked_fg else scan_pairs):
            if pair in already_open:
                continue
            if len(self.positions) >= config.MAX_OPEN_TRADES:
                break

            logger.info("Scanning %s ...", pair)
            analysis = await asyncio.to_thread(analyze, pair)
            if analysis is None:
                continue
            analysis_cache[pair] = analysis

            signal = generate_signal(
                analysis,
                available_eur,
                already_open,
                btc_analysis=btc_analysis,
                open_pairs=already_open,
            )
            if not signal:
                continue

            if config.MAX_BTC_BETA_EXPOSURE_PCT < 1.0:
                _max_beta = config.BUDGET_EUR * config.MAX_BTC_BETA_EXPOSURE_PCT
                _long_exp = sum(p["size_eur"] for p in self.positions.values() if p.get("side", "long") == "long")
                if _long_exp + signal.size_eur > _max_beta:
                    logger.info("%s: SKIP BTC-beta long (%.0f€ + %.0f€ > %.0f€)", signal.pair, _long_exp, signal.size_eur, _max_beta)
                    continue

            logger.info(
                "Signal: %s  score=%d  ADX=%.1f  price=%.6f — asking Groq",
                signal.pair, signal.score, signal.adx, signal.price,
            )

            try:
                execute, confidence, reason, size_pct = await asyncio.to_thread(
                    groq_evaluator.evaluate, signal, mkt_ctx
                )
            except GroqUnavailableError as exc:
                logger.warning("Groq non disponibile — segnale %s skippato: %s", signal.pair, exc)
                await self.app.bot.send_message(
                    chat_id=config.TELEGRAM_CHAT_ID,
                    text=f"GROQ NON DISPONIBILE — {signal.pair} skippato\n{exc}",
                )
                continue

            effective_size_pct = min(kelly_pct, size_pct) if kelly_pct else size_pct

            if execute and effective_size_pct > 0:
                ai_size = round(available_eur * effective_size_pct, 2)
                ai_size = max(ai_size, 10.0)
                if ai_size <= available_eur:
                    signal.size_eur    = ai_size
                    signal.size_crypto = ai_size / signal.price

            db.insert_groq_decision({
                "ts":           time.time(),
                "pair":         signal.pair,
                "decision":     "ESEGUI" if execute else "RIFIUTA",
                "confidence":   confidence,
                "size_pct":     round(size_pct * 100, 0),
                "size_eur":     signal.size_eur,
                "reason":       reason,
                "score":        signal.score,
                "adx":          signal.adx,
                "rsi_1h":       signal.rsi_1h,
                "rsi_15m":      signal.rsi_15m,
                "macd_hist":    signal.macd_hist,
                "volume_ratio": signal.volume_ratio,
                "price":        signal.price,
            })

            if execute:
                if signal.pair in self._in_flight:
                    logger.warning("Guard: %s già in-flight — skip", signal.pair)
                else:
                    self._in_flight.add(signal.pair)
                    await self._execute_signal(signal, f"{reason} [{confidence}/10, {size_pct*100:.0f}% capitale]")
                    already_open.add(signal.pair)
            else:
                reasons_text = "\n".join(f"  • {r}" for r in signal.reasons)
                await self.app.bot.send_message(
                    chat_id=config.TELEGRAM_CHAT_ID,
                    text=(
                        f"SEGNALE RIFIUTATO — {signal.pair}\n\n"
                        f"Groq ({confidence}/10): {reason}\n\n"
                        f"Score: {signal.score}/6  |  ADX: {signal.adx:.1f}\n"
                        f"Indicatori:\n{reasons_text}"
                    ),
                )
                logger.info("Groq rejected %s [%d/10]: %s", signal.pair, confidence, reason)

            signal_found = True
            await asyncio.sleep(1)

        # ── Short scan ────────────────────────────────────────────────────────
        if short_blocked_fg:
            label = mkt_ctx.get("fear_greed_label", "")
            logger.info("SHORT BLOCKED: fear_greed=%d (%s)", fg_val, label)

        sl_cooldown_pairs = db.get_pairs_in_sl_cooldown(config.SL_COOLDOWN_HOURS)
        if sl_cooldown_pairs:
            logger.info("SL cooldown attivo per: %s (%.0fh)", ", ".join(sorted(sl_cooldown_pairs)), config.SL_COOLDOWN_HOURS)

        for pair in ([] if short_blocked_fg else scan_pairs):
            if pair in already_open:
                continue
            if pair in sl_cooldown_pairs:
                logger.info("%s: SKIP — SL cooldown attivo (%.0fh)", pair, config.SL_COOLDOWN_HOURS)
                continue
            if len(self.positions) >= config.MAX_OPEN_TRADES:
                break

            available_eur_now = min(
                available_eur,
                config.BUDGET_EUR * config.MAX_CAPITAL_DEPLOYED - sum(p["size_eur"] for p in self.positions.values()),
            )
            if available_eur_now < 10.0:
                break

            logger.info("Short-scanning %s ...", pair)
            analysis = analysis_cache.get(pair) or await asyncio.to_thread(analyze, pair)
            if analysis is None:
                continue

            signal = generate_short_signal(
                analysis,
                available_eur_now,
                already_open,
                btc_analysis=btc_analysis,
                open_pairs=already_open,
            )
            if not signal:
                continue

            if config.MAX_BTC_BETA_EXPOSURE_PCT < 1.0:
                _max_beta = config.BUDGET_EUR * config.MAX_BTC_BETA_EXPOSURE_PCT
                _short_exp = sum(p["size_eur"] for p in self.positions.values() if p.get("side") == "short")
                if _short_exp + signal.size_eur > _max_beta:
                    logger.info("%s: SKIP BTC-beta short (%.0f€ + %.0f€ > %.0f€)", signal.pair, _short_exp, signal.size_eur, _max_beta)
                    continue

            logger.info(
                "SHORT Signal: %s  score=%d  ADX=%.1f  price=%.6f — asking Groq",
                signal.pair, signal.score, signal.adx, signal.price,
            )

            try:
                execute, confidence, reason, size_pct = await asyncio.to_thread(
                    groq_evaluator.evaluate, signal, mkt_ctx
                )
            except GroqUnavailableError as exc:
                logger.warning("Groq non disponibile — segnale SHORT %s skippato: %s", signal.pair, exc)
                await self.app.bot.send_message(
                    chat_id=config.TELEGRAM_CHAT_ID,
                    text=f"GROQ NON DISPONIBILE — SHORT {signal.pair} skippato\n{exc}",
                )
                continue

            effective_size_pct = min(kelly_pct, size_pct) if kelly_pct else size_pct

            if execute and effective_size_pct > 0:
                ai_size = round(available_eur_now * effective_size_pct, 2)
                ai_size = max(ai_size, 10.0)
                if ai_size <= available_eur_now:
                    signal.size_eur    = ai_size
                    signal.size_crypto = ai_size / signal.price

            db.insert_groq_decision({
                "ts":           time.time(),
                "pair":         signal.pair,
                "direction":    "SHORT",
                "decision":     "ESEGUI" if execute else "RIFIUTA",
                "confidence":   confidence,
                "size_pct":     round(size_pct * 100, 0),
                "size_eur":     signal.size_eur,
                "reason":       reason,
                "score":        signal.score,
                "adx":          signal.adx,
                "rsi_1h":       signal.rsi_1h,
                "rsi_15m":      signal.rsi_15m,
                "macd_hist":    signal.macd_hist,
                "volume_ratio": signal.volume_ratio,
                "price":        signal.price,
            })

            if execute:
                if signal.pair in self._in_flight:
                    logger.warning("Guard: %s già in-flight — skip", signal.pair)
                else:
                    self._in_flight.add(signal.pair)
                    await self._execute_signal(signal, f"{reason} [{confidence}/10, {size_pct*100:.0f}% capitale]")
                    already_open.add(signal.pair)
            else:
                reasons_text = "\n".join(f"  • {r}" for r in signal.reasons)
                await self.app.bot.send_message(
                    chat_id=config.TELEGRAM_CHAT_ID,
                    text=(
                        f"SHORT RIFIUTATO — {signal.pair}\n\n"
                        f"Groq ({confidence}/10): {reason}\n\n"
                        f"Score: {signal.score}/6  |  ADX: {signal.adx:.1f}\n"
                        f"Indicatori:\n{reasons_text}"
                    ),
                )
                logger.info("Groq rejected SHORT %s [%d/10]: %s", signal.pair, confidence, reason)

            signal_found = True
            await asyncio.sleep(1)

        return signal_found

    async def _job_monitor_positions(self, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Check every open position against its TP and SL."""
        if not self.positions:
            return

        to_close: list[str] = []

        for pair, pos in list(self.positions.items()):
            try:
                ticker  = await asyncio.to_thread(kraken_client.get_ticker, pair)
                current = ticker["last"]

                side = pos.get("side", "long")

                # ── Trailing stop update ──────────────────────────────────
                if config.TRAILING_STOP_ENABLED:
                    atr_pos      = pos.get("atr", 0)
                    old_trailing = pos.get("trailing_stop", pos["stop_loss"])
                    if _update_trailing_stop(pos, current, atr_pos, config):
                        with self._positions_lock:
                            self.positions[pair] = pos
                            db.update_position(pair, pos)
                        logger.info(
                            "Trailing stop %s %s: %.6f → %.6f",
                            side.upper(), pair, old_trailing, pos["trailing_stop"],
                        )

                hit_tp, hit_sl = _check_exit_signals(pos, current)
                if not (hit_tp or hit_sl):
                    continue

                label         = "TAKE PROFIT" if hit_tp else "STOP LOSS"
                exit_data     = _calc_exit(pos, current, config)
                gross_pnl_eur = exit_data["gross_pnl_eur"]
                fee_exit      = exit_data["fee_exit"]
                fee_entry     = exit_data["fee_entry"]
                slip_eur      = exit_data["slip_eur"]
                funding_cost  = exit_data["funding_cost"]
                net_pnl_eur   = exit_data["net_pnl_eur"]
                net_pnl_pct   = exit_data["net_pnl_pct"]

                try:
                    if config.PAPER_TRADING:
                        suffix = "TX: PAPER"
                    else:
                        vol_str = kraken_client.format_volume(pair, pos["size_crypto"])
                        if side == "short":
                            order = await asyncio.to_thread(
                                kraken_client.add_order, pair, "buy", "market", vol_str,
                                leverage=config.MARGIN_LEVERAGE,
                            )
                        else:
                            order = await asyncio.to_thread(
                                kraken_client.add_order, pair, "sell", "market", vol_str
                            )
                        txids  = order.get("txid", [])
                        txid   = txids[0] if txids else "unknown"
                        suffix = f"TX: {txid}"
                except Exception as exc:
                    suffix = f"ERRORE chiusura automatica — chiudi manualmente!\n{exc}"

                icon = "✅" if hit_tp else "🛑"
                funding_str = f"  funding -{funding_cost:.4f}€" if funding_cost > 0 else ""
                await ctx.bot.send_message(
                    chat_id=config.TELEGRAM_CHAT_ID,
                    text=(
                        f"{icon} {'TP' if hit_tp else 'SL'} {pair}  "
                        f"{net_pnl_eur:+.2f}€ ({net_pnl_pct:+.2f}%)  "
                        f"fee -{fee_entry+fee_exit:.4f}€{funding_str}  {suffix}"
                    ),
                )
                db.insert_trade({
                    "pair": pair, "side": side,
                    "exit_reason": "tp" if hit_tp else "sl",
                    "entry": pos["entry"], "exit": current,
                    "size_eur": pos["size_eur"],
                    "gross_pnl_eur": round(gross_pnl_eur, 4),
                    "fee_eur": round(fee_entry + fee_exit + slip_eur + funding_cost, 4),
                    "pnl_eur": round(net_pnl_eur, 4),
                    "pnl_pct": round(net_pnl_pct, 4),
                    "opened_at": pos.get("opened_at"),
                    "closed_at": time.time(),
                })
                to_close.append(pair)

            except Exception as exc:
                logger.error("Position monitor error for %s: %s", pair, exc)

        for pair in to_close:
            with self._positions_lock:
                del self.positions[pair]
                db.delete_position(pair)

    # ── Entry point ───────────────────────────────────────────────────────────

    async def _job_daily_report(self, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        positions = self.positions
        trades    = db.get_trades()
        today_ts  = time.time() - 86400
        today_trades = [t for t in trades if t.get("closed_at", 0) > today_ts]

        total_pnl = sum(t["pnl_eur"] for t in trades)
        today_pnl = sum(t["pnl_eur"] for t in today_trades)

        pos_lines = []
        for pair, pos in positions.items():
            try:
                ticker  = await asyncio.to_thread(kraken_client.get_ticker, pair)
                current = ticker["last"]
                side    = pos.get("side", "long")
                if side == "short":
                    pnl_pct = (pos["entry"] - current) / pos["entry"] * 100
                    pnl_eur = (pos["entry"] - current) * pos["size_crypto"]
                else:
                    pnl_pct = (current - pos["entry"]) / pos["entry"] * 100
                    pnl_eur = (current - pos["entry"]) * pos["size_crypto"]
                pos_lines.append(f"  {pair} [{side.upper()}] {pnl_pct:+.2f}% ({pnl_eur:+.2f}€)")
            except Exception:
                pos_lines.append(f"  {pair} —")

        spent = sum(p["size_eur"] for p in positions.values())

        kraken_status = "?"
        try:
            kraken_status, _ = await asyncio.to_thread(kraken_client.get_system_status)
        except Exception:
            pass

        lines = [
            f"REPORT {time.strftime('%d/%m %H:%M')}",
            f"Kraken: {kraken_status.upper()}  |  {'PAPER' if config.PAPER_TRADING else 'LIVE'}",
            f"Capitale deployato: {spent:.2f}€ / {config.BUDGET_EUR * config.MAX_CAPITAL_DEPLOYED:.0f}€ max",
            f"Posizioni aperte: {len(positions)} / {config.MAX_OPEN_TRADES}",
        ]
        if pos_lines:
            lines += pos_lines
        lines += [
            f"Trade ultimi 24h: {len(today_trades)}  P&L: {today_pnl:+.2f}€",
            f"P&L totale: {total_pnl:+.2f}€",
        ]

        await self.app.bot.send_message(
            chat_id=config.TELEGRAM_CHAT_ID,
            text="\n".join(lines),
        )

    async def _job_check_pending_orders(self, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Check whether live limit orders have been filled or have timed out."""
        if config.PAPER_TRADING:
            return
        pending = _load_json(PENDING_ORDERS_FILE, {})
        if not pending:
            return
        now     = time.time()
        to_remove = []
        try:
            open_orders = await asyncio.to_thread(kraken_client.get_open_orders)
            open_txids  = set(open_orders.get("open", {}).keys())
        except Exception as exc:
            logger.error("Cannot fetch open orders: %s", exc)
            return
        for pair, po in list(pending.items()):
            txid = po["txid"]
            if txid not in open_txids:
                order_status = "unknown"
                try:
                    info = await asyncio.to_thread(kraken_client.query_orders, [txid])
                    order_status = info.get(txid, {}).get("status", "unknown")
                except Exception as exc:
                    logger.warning("Cannot query order %s: %s — assuming filled", txid, exc)
                if order_status in ("canceled", "expired"):
                    logger.warning("Limit order %s was %s — no position created", txid, order_status)
                    to_remove.append(pair)
                    await self.app.bot.send_message(
                        chat_id=config.TELEGRAM_CHAT_ID,
                        text=f"LIMIT {order_status.upper()} {pair}  TX {txid} — nessuna posizione aperta",
                    )
                    continue
                pos_dict = {
                    "entry":         po["limit_price"],
                    "size_crypto":   po["size_crypto"],
                    "size_eur":      po["size_eur"],
                    "fee_entry_eur": round(po["size_eur"] * config.KRAKEN_MAKER_FEE, 4),
                    "take_profit":   po["take_profit"],
                    "stop_loss":     po["stop_loss"],
                    "trailing_stop": po["trailing_stop"],
                    "txid":          txid,
                    "opened_at":     now,
                    "order_type":    "limit",
                    "side":          "long",
                }
                with self._positions_lock:
                    self.positions[pair] = pos_dict
                    db.insert_position(pair, pos_dict)
                to_remove.append(pair)
                await self.app.bot.send_message(
                    chat_id=config.TELEGRAM_CHAT_ID,
                    text=f"LIMIT ESEGUITO {pair}  @{po['limit_price']:.6f}  TX {txid}",
                )
            elif now > po["timeout_at"]:
                try:
                    await asyncio.to_thread(kraken_client.cancel_order, txid)
                except Exception:
                    pass
                to_remove.append(pair)
                await self.app.bot.send_message(
                    chat_id=config.TELEGRAM_CHAT_ID,
                    text=f"LIMIT SCADUTO {pair}  TX {txid} — cancellato",
                )
        for pair in to_remove:
            pending.pop(pair, None)
        if to_remove:
            PENDING_ORDERS_FILE.write_text(
                json.dumps(pending, indent=2), encoding="utf-8"
            )

    async def _cmd_ai(self, update, ctx) -> None:
        """Show the last N Groq AI decisions."""
        groq_log = db.get_groq_decisions()
        if not groq_log:
            await update.message.reply_text("Nessuna valutazione Groq ancora.")
            return
        n = int(ctx.args[0]) if ctx.args and ctx.args[0].isdigit() else 5
        lines = []
        for g in reversed(groq_log[-n:]):
            icon = "✅" if g["decision"] == "ESEGUI" else "❌"
            conf = f" [{g.get('confidence', '?')}/10]" if g.get("confidence") else ""
            size = f" {g.get('size_pct', 0):.0f}%" if g.get("size_pct") else ""
            ts   = time.strftime("%d/%m %H:%M", time.localtime(g["ts"]))
            lines.append(
                f"{icon} {ts} {g['pair']} score={g['score']}/6{conf}{size}\n"
                f"   {g['reason']}\n"
                f"   RSI(1h)={g['rsi_1h']} ADX={g.get('adx',0):.0f} Vol={g['volume_ratio']:.1f}×"
            )
        await update.message.reply_text("\n\n".join(lines))

    async def _cmd_profile(self, update, ctx) -> None:
        """Show active profile or switch (/profile aggressive|conservative)."""
        if ctx.args:
            name = ctx.args[0].lower()
            try:
                config.apply_profile(name)
                p = config.PROFILES[name]
                await update.message.reply_text(
                    f"Profilo cambiato: {name.upper()}\n\n"
                    f"Posizioni max: {p['MAX_OPEN_TRADES']}  |  Capitale: {p['MAX_CAPITAL_DEPLOYED']*100:.0f}%\n"
                    f"TP: {p['ATR_TP_MULT']}×ATR ({p['TAKE_PROFIT_PCT']*100:.0f}% fallback)  "
                    f"SL: {p['ATR_SL_MULT']}×ATR ({p['STOP_LOSS_PCT']*100:.0f}% fallback)\n"
                    f"Size AI alto/medio/basso: {p['AI_SIZE_HIGH']*100:.0f}% / {p['AI_SIZE_MED']*100:.0f}% / {p['AI_SIZE_MIN']*100:.0f}%\n"
                    f"Confidenza min Groq: {p['GROQ_MIN_CONFIDENCE']}/10  |  Score min: {p['MIN_SIGNAL_SCORE']}/6\n"
                    f"Circuit breaker: -{p['CIRCUIT_BREAKER_LOSS_EUR']:.0f}€/giorno\n"
                    f"Trailing stop: {p['TRAILING_STOP_PCT']*100:.0f}%  |  ADX min: {p['ADX_MIN']}\n"
                    f"Fear&Greed: {p['FEAR_GREED_MIN']}–{p['FEAR_GREED_MAX']}  |  BTC RSI max: {p['BTC_RSI_MAX']}"
                )
            except ValueError as exc:
                await update.message.reply_text(str(exc))
            return

        p = config.PROFILES[config.ACTIVE_PROFILE]
        await update.message.reply_text(
            f"Profilo attivo: {config.ACTIVE_PROFILE.upper()}\n\n"
            f"Posizioni max: {p['MAX_OPEN_TRADES']}  |  Capitale: {p['MAX_CAPITAL_DEPLOYED']*100:.0f}%\n"
            f"TP: {p['ATR_TP_MULT']}×ATR ({p['TAKE_PROFIT_PCT']*100:.0f}% fallback)  "
            f"SL: {p['ATR_SL_MULT']}×ATR ({p['STOP_LOSS_PCT']*100:.0f}% fallback)\n"
            f"Size AI alto/medio/basso: {p['AI_SIZE_HIGH']*100:.0f}% / {p['AI_SIZE_MED']*100:.0f}% / {p['AI_SIZE_MIN']*100:.0f}%\n"
            f"Confidenza min Groq: {p['GROQ_MIN_CONFIDENCE']}/10  |  Score min: {p['MIN_SIGNAL_SCORE']}/6\n"
            f"Circuit breaker: -{p['CIRCUIT_BREAKER_LOSS_EUR']:.0f}€/giorno\n"
            f"Trailing stop: {p['TRAILING_STOP_PCT']*100:.0f}%  |  ADX min: {p['ADX_MIN']}\n"
            f"Fear&Greed: {p['FEAR_GREED_MIN']}–{p['FEAR_GREED_MAX']}  |  BTC RSI max: {p['BTC_RSI_MAX']}\n\n"
            f"Cambia con: /profile conservative | /profile moderate | /profile aggressive"
        )

    async def _cmd_discover(self, update, ctx) -> None:
        if self._discovering:
            await update.message.reply_text("Discovery già in corso — attendi la notifica.")
            return
        await update.message.reply_text(
            "Discovery avviata in background — riceverai una notifica al termine (2-3 min)."
        )
        asyncio.create_task(self._run_discovery_bg())

    async def _run_discovery_bg(self) -> None:
        if self._discovering:
            return
        self._discovering = True
        try:
            result = await asyncio.to_thread(discovery.run_discovery)
            await self._notify_discovery(result)
            if not result.get("error"):
                await self._act_on_discovery(result)
        except Exception as exc:
            logger.error("Discovery fallita: %s", exc, exc_info=True)
            try:
                await self.app.bot.send_message(
                    chat_id=config.TELEGRAM_CHAT_ID,
                    text=f"Discovery fallita: {type(exc).__name__}: {exc}",
                )
            except Exception as send_exc:
                logger.error("Impossibile notificare errore discovery: %s", send_exc)
        finally:
            self._discovering = False

    async def _job_discovery(self, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        asyncio.create_task(self._run_discovery_bg())

    async def _notify_discovery(self, result: dict) -> None:
        if result.get("error"):
            await self.app.bot.send_message(
                chat_id=config.TELEGRAM_CHAT_ID,
                text=f"Discovery fallita: {result['error']}",
            )
            return

        db.insert_discovery(result)

        top_pairs = [c["pair"] for c in result.get("candidates", [])[:5]]
        DYNAMIC_PAIRS_FILE.write_text(
            json.dumps(top_pairs, ensure_ascii=False),
            encoding="utf-8",
        )

        candidates = result.get("candidates", [])[:5]
        lines = []
        for c in candidates:
            lines.append(
                f"{c['pair']}  score={c['mech_score']}  "
                f"win={c['win_rate']}%  segnali={c['signals_tried']}  "
                f"RSI={c['rsi_now']}  ADX={c['adx_now']}"
            )

        await self.app.bot.send_message(
            chat_id=config.TELEGRAM_CHAT_ID,
            text=(
                f"DISCOVERY GIORNALIERA\n\n"
                f"Coppie analizzate: {result['pairs_scanned']}\n\n"
                f"Top candidate:\n" + "\n".join(lines) +
                f"\n\nGroq AI:\n{result['groq_analysis']}"
            ),
        )

    async def _job_reconcile_orders(self, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Compare Kraken open orders vs DB positions every 60 min and report discrepancies."""
        if config.PAPER_TRADING:
            return
        try:
            kraken_data = await asyncio.to_thread(kraken_client.get_open_orders)
            kraken_txids = set(kraken_data.get("open", {}).keys())
        except Exception as exc:
            logger.error("Reconcile: impossibile recuperare ordini Kraken: %s", exc)
            return

        db_positions = db.get_positions()
        db_txids = {
            pos.get("txid") for pos in db_positions.values()
            if pos.get("txid") and pos.get("txid") not in ("PAPER", "unknown")
        }

        orphan_on_kraken = kraken_txids - db_txids
        orphan_in_db = db_txids - kraken_txids

        issues: list[str] = []
        if orphan_on_kraken:
            issues.append(f"Ordini su Kraken non in DB: {', '.join(sorted(orphan_on_kraken))}")
        if orphan_in_db:
            issues.append(f"Posizioni in DB non su Kraken (possibilmente chiuse): {', '.join(sorted(orphan_in_db))}")

        if issues:
            msg = "RICONCILIAZIONE ORDINI — discrepanze rilevate:\n" + "\n".join(issues)
            logger.warning("Reconcile: %s", " | ".join(issues))
            await self.app.bot.send_message(chat_id=config.TELEGRAM_CHAT_ID, text=msg)
        else:
            logger.info("Reconcile: nessuna discrepanza (Kraken=%d, DB=%d)", len(kraken_txids), len(db_txids))

    async def _job_process_commands(self, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Process commands queued by the web dashboard."""
        try:
            commands = db.pop_commands()
        except Exception as exc:
            logger.error("bot_commands DB read error: %s", exc)
            return
        if not commands:
            return

        for cmd in commands:
            try:
                c = cmd.get("cmd")
                if c == "scan":
                    if not self._scanning:
                        self._scanning = True
                        try:
                            await self._run_scan_inner()
                        finally:
                            self._scanning = False
                elif c == "discover":
                    asyncio.create_task(self._run_discovery_bg())
                elif c == "close":
                    pair = cmd.get("pair", "").upper()
                    if pair in self.positions:
                        msg = await self._do_close(pair)
                        await self.app.bot.send_message(
                            chat_id=config.TELEGRAM_CHAT_ID,
                            text=f"[Dashboard] {msg}",
                        )
                    else:
                        logger.warning("Dashboard close: %s non in posizioni", pair)
                elif c == "profile":
                    name = cmd.get("name", "")
                    try:
                        config.apply_profile(name)
                        await self.app.bot.send_message(
                            chat_id=config.TELEGRAM_CHAT_ID,
                            text=f"[Dashboard] Profilo cambiato: {name.upper()}",
                        )
                    except ValueError as exc:
                        logger.error("Profile change error: %s", exc)
                elif c == "restart":
                    logger.info("Restart richiesto dalla dashboard — uscita in corso")
                    import sys
                    sys.exit(0)
            except Exception as exc:
                logger.error("Command error [%s]: %s", cmd, exc)

    async def _act_on_discovery(self, result: dict) -> None:
        """Run live signal evaluation on top discovery candidates and trade if approved."""
        candidates = result.get("candidates", [])
        if not candidates:
            return

        if config.PAPER_TRADING:
            spent = sum(p["size_eur"] for p in self.positions.values())
            available_eur = max(config.BUDGET_EUR - spent, 0.0)
        else:
            try:
                bal = await asyncio.to_thread(kraken_client.get_balance)
                spent = sum(p["size_eur"] for p in self.positions.values())
                available_eur = bal.get("ZEUR", bal.get("EUR", 0.0))
            except Exception as exc:
                logger.error("Discovery: impossibile recuperare saldo: %s", exc)
                return

        max_allowed = config.BUDGET_EUR * config.MAX_CAPITAL_DEPLOYED
        spent = sum(p["size_eur"] for p in self.positions.values())
        available_eur = min(available_eur, max_allowed - spent)

        if available_eur < 10.0 or len(self.positions) >= config.MAX_OPEN_TRADES:
            return

        if config.CIRCUIT_BREAKER_ENABLED:
            trades = db.get_trades()
            daily_loss = sum(
                t["pnl_eur"] for t in trades
                if t.get("closed_at", 0) > time.time() - 86400 and t["pnl_eur"] < 0
            )
            if abs(daily_loss) >= config.CIRCUIT_BREAKER_LOSS_EUR:
                return

        mkt_ctx = await asyncio.to_thread(market_data.get_market_context)
        if config.FEAR_GREED_ENABLED and market_data.fear_greed_blocks_trading(
            mkt_ctx.get("fear_greed_value")
        ):
            return

        btc_analysis = None
        if config.BTC_FILTER_ENABLED:
            btc_analysis = await asyncio.to_thread(analyze, config.BTC_PAIR)

        trades_history = db.get_trades()
        kelly_pct = kelly_size_pct(trades_history)
        already_open = set(self.positions.keys())

        evaluated: list[str] = []
        any_executed = False

        for c in candidates[:3]:
            pair = c["pair"]
            if pair in already_open:
                continue
            if len(self.positions) >= config.MAX_OPEN_TRADES:
                break

            logger.info("Discovery live eval: %s (mech_score=%.1f)", pair, c.get("mech_score", 0))
            analysis = await asyncio.to_thread(analyze, pair)
            if analysis is None:
                evaluated.append(f"• {pair}: dati non disponibili")
                continue

            signal = generate_signal(
                analysis, available_eur, already_open,
                btc_analysis=btc_analysis, open_pairs=already_open,
            )
            if not signal:
                evaluated.append(f"• {pair}: gate live non superati")
                continue

            try:
                execute, confidence, reason, size_pct = await asyncio.to_thread(
                    groq_evaluator.evaluate, signal, mkt_ctx
                )
            except GroqUnavailableError as exc:
                logger.warning("Groq non disponibile — discovery %s skippato: %s", pair, exc)
                evaluated.append(f"• {pair}: Groq non disponibile — {exc}")
                continue

            effective_size_pct = min(kelly_pct, size_pct) if kelly_pct else size_pct

            if execute and effective_size_pct > 0:
                ai_size = max(round(available_eur * effective_size_pct, 2), 10.0)
                if ai_size <= available_eur:
                    signal.size_eur    = ai_size
                    signal.size_crypto = ai_size / signal.price

            db.insert_groq_decision({
                "ts":           time.time(),
                "pair":         signal.pair,
                "decision":     "ESEGUI" if execute else "RIFIUTA",
                "confidence":   confidence,
                "size_pct":     round(size_pct * 100, 0),
                "size_eur":     signal.size_eur,
                "reason":       f"[DISCOVERY] {reason}",
                "score":        signal.score,
                "adx":          signal.adx,
                "rsi_1h":       signal.rsi_1h,
                "rsi_15m":      signal.rsi_15m,
                "macd_hist":    signal.macd_hist,
                "volume_ratio": signal.volume_ratio,
                "price":        signal.price,
            })

            if execute:
                await self._execute_signal(
                    signal, f"[DISCOVERY] {reason} [{confidence}/10, {size_pct*100:.0f}% capitale]"
                )
                already_open.add(pair)
                any_executed = True
            else:
                evaluated.append(f"• {pair}: Groq rifiuta [{confidence}/10] — {reason}")

            await asyncio.sleep(1)

        if not any_executed and evaluated:
            await self.app.bot.send_message(
                chat_id=config.TELEGRAM_CHAT_ID,
                text="DISCOVERY — valutazione live:\n" + "\n".join(evaluated),
            )
        elif not any_executed:
            await self.app.bot.send_message(
                chat_id=config.TELEGRAM_CHAT_ID,
                text="DISCOVERY — nessun candidato supera i gate live al momento.",
            )

    def stop(self) -> None:
        """Request graceful shutdown. Called from the SIGTERM/SIGINT handler."""
        logger.info("TradingBot.stop() — shutdown in corso")
        import sys
        sys.exit(0)

    def run(self) -> None:
        jq = self.app.job_queue
        jq.run_repeating(
            self._job_scan,
            interval=config.SCAN_INTERVAL_MINUTES * 60,
            first=10,
            name="market_scan",
        )
        jq.run_repeating(
            self._job_monitor_positions,
            interval=config.POSITION_CHECK_SECONDS,
            first=20,
            name="position_monitor",
        )
        jq.run_repeating(
            self._job_check_pending_orders,
            interval=30,
            first=30,
            name="pending_orders",
        )
        jq.run_repeating(
            self._job_process_commands,
            interval=15,
            first=15,
            name="dashboard_commands",
        )
        jq.run_repeating(
            self._job_reconcile_orders,
            interval=3600,
            first=3600,
            name="reconcile_orders",
        )
        jq.run_daily(
            self._job_discovery,
            time=datetime.time(hour=7, minute=0),
            name="discovery_morning",
        )
        jq.run_daily(
            self._job_discovery,
            time=datetime.time(hour=19, minute=0),
            name="discovery_evening",
        )
        jq.run_daily(
            self._job_daily_report,
            time=datetime.time(hour=config.DAILY_REPORT_HOUR, minute=0),
            name="daily_report",
        )

        logger.info(
            "Bot avviato. Coppie: %s  TP=%.1f×ATR  SL=%.1f×ATR  R:R≈%.1f:1  Scan ogni %d min  Groq: %s",
            config.PAIRS,
            config.ATR_TP_MULT,
            config.ATR_SL_MULT,
            config.ATR_TP_MULT / config.ATR_SL_MULT,
            config.SCAN_INTERVAL_MINUTES,
            config.GROQ_MODEL,
        )
        self.app.run_polling(allowed_updates=Update.ALL_TYPES)
