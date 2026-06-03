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
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

import config
import discovery
import groq_evaluator
import kraken_client
import market_data
from analyzer import analyze
from strategy import Signal, generate_signal, kelly_size_pct

logger = logging.getLogger(__name__)

_DIR                = Path(__file__).parent
POSITIONS_FILE      = _DIR / "positions.json"
TRADES_LOG_FILE     = _DIR / "trades_log.json"
GROQ_LOG_FILE       = _DIR / "groq_log.json"
DISCOVERY_FILE      = _DIR / "discovery_log.json"
PENDING_ORDERS_FILE = _DIR / "pending_orders.json"


# ── Persistence helpers ───────────────────────────────────────────────────────

def _load_positions() -> dict:
    if POSITIONS_FILE.exists():
        try:
            return json.loads(POSITIONS_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _load_json(path: "Path", default):
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default
    except Exception:
        return default


def _save_positions(positions: dict) -> None:
    POSITIONS_FILE.write_text(
        json.dumps(positions, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _append_groq_log(entry: dict) -> None:
    log = []
    if GROQ_LOG_FILE.exists():
        try:
            log = json.loads(GROQ_LOG_FILE.read_text(encoding="utf-8"))
        except Exception:
            log = []
    log.append(entry)
    GROQ_LOG_FILE.write_text(
        json.dumps(log, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _append_trade_log(entry: dict) -> None:
    log = []
    if TRADES_LOG_FILE.exists():
        try:
            log = json.loads(TRADES_LOG_FILE.read_text(encoding="utf-8"))
        except Exception:
            log = []
    log.append(entry)
    TRADES_LOG_FILE.write_text(
        json.dumps(log, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


# ── Bot ───────────────────────────────────────────────────────────────────────

class TradingBot:
    def __init__(self) -> None:
        self.app = Application.builder().token(config.TELEGRAM_TOKEN).build()
        self.positions: dict = _load_positions()
        self._scanning   = False   # lock scan
        self._discovering = False  # lock discovery
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
            "/discover   — discovery manuale nuove coppie\n\n"
            f"TP: +{config.TAKE_PROFIT_PCT*100:.0f}%  "
            f"SL: -{config.STOP_LOSS_PCT*100:.0f}%  "
            f"Max 2 posizioni aperte."
        )

    async def _cmd_status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            kraken_status, kraken_ts = await asyncio.to_thread(kraken_client.get_system_status)
            kraken_line = f"Kraken: {kraken_status.upper()}"
        except Exception as exc:
            kraken_line = f"Kraken: impossibile verificare ({exc})"

        mode = "PAPER TRADING" if config.PAPER_TRADING else "LIVE"
        await update.message.reply_text(
            f"Sistema: ATTIVO ({mode})\n"
            f"{kraken_line}\n"
            f"Decisioni: Groq AI ({config.GROQ_MODEL})\n"
            f"Posizioni aperte: {len(self.positions)} / {config.MAX_OPEN_TRADES}\n"
            f"Coppie monitorate: {', '.join(config.PAIRS)}\n"
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
                pnl_pct = (current - pos["entry"]) / pos["entry"] * 100
                pnl_eur = (current - pos["entry"]) * pos["size_crypto"]
                lines.append(
                    f"{pair}  {pnl_pct:+.2f}%  ({pnl_eur:+.2f}€)\n"
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

    async def _cmd_close(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not ctx.args:
            await update.message.reply_text(
                "Uso: /close <pair>\nEsempio: /close SOLEUR"
            )
            return

        pair = ctx.args[0].upper()
        if pair not in self.positions:
            await update.message.reply_text(f"Nessuna posizione aperta per {pair}.")
            return

        pos = self.positions[pair]
        try:
            ticker  = await asyncio.to_thread(kraken_client.get_ticker, pair)
            current = ticker["last"]

            if config.PAPER_TRADING:
                txid = "PAPER"
            else:
                vol_str = kraken_client.format_volume(pair, pos["size_crypto"])
                order   = await asyncio.to_thread(
                    kraken_client.add_order, pair, "sell", "market", vol_str
                )
                txids = order.get("txid", [])
                txid  = txids[0] if txids else "unknown"

            gross_pnl_eur = (current - pos["entry"]) * pos["size_crypto"]
            fee_exit      = round(current * pos["size_crypto"] * config.KRAKEN_TAKER_FEE, 4)
            fee_entry     = pos.get("fee_entry_eur", 0.0)
            slip_eur      = round(pos["size_eur"] * config.SLIPPAGE_PCT * 2, 4)
            net_pnl_eur   = gross_pnl_eur - fee_entry - fee_exit - slip_eur
            net_pnl_pct   = net_pnl_eur / pos["size_eur"] * 100

            del self.positions[pair]
            _save_positions(self.positions)
            _append_trade_log({
                "pair": pair, "exit_reason": "manual",
                "entry": pos["entry"], "exit": current,
                "size_eur": pos["size_eur"],
                "gross_pnl_eur": round(gross_pnl_eur, 4),
                "fee_eur": round(fee_entry + fee_exit, 4),
                "pnl_eur": round(net_pnl_eur, 4),
                "pnl_pct": round(net_pnl_pct, 4),
                "paper": config.PAPER_TRADING, "closed_at": time.time(),
            })

            await update.message.reply_text(
                f"Posizione chiusa: {pair}\n"
                f"P&L lordo: {gross_pnl_eur:+.2f}€\n"
                f"Fee totali: -{fee_entry + fee_exit:.4f}€\n"
                f"P&L netto: {net_pnl_eur:+.2f}€  ({net_pnl_pct:+.2f}%)\n"
                f"TX: {txid}"
            )
        except Exception as exc:
            await update.message.reply_text(f"Errore chiusura {pair}: {exc}")

    # ── Signal execution ──────────────────────────────────────────────────────

    async def _execute_signal(self, signal: Signal, groq_reason: str) -> None:
        try:
            use_limit   = config.LIMIT_ORDERS_ENABLED and not config.PAPER_TRADING
            fee_rate    = config.KRAKEN_MAKER_FEE if use_limit else config.KRAKEN_TAKER_FEE
            fee_entry   = round(signal.size_eur * fee_rate, 4)
            size_crypto_net = signal.size_eur * (1 - fee_rate) / signal.price

            if config.PAPER_TRADING:
                # Paper: simula limit a prezzo leggermente migliore
                if config.LIMIT_ORDERS_ENABLED:
                    limit_price = round(signal.price * (1 - config.LIMIT_OFFSET_PCT), 8)
                    size_crypto_net = signal.size_eur * (1 - fee_rate) / limit_price
                    actual_price = limit_price
                else:
                    actual_price = signal.price
                txid  = "PAPER"
                descr = ""

            elif use_limit:
                # Live: ordine limit reale
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
                # Salva in pending orders per monitoraggio fill
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
                    __import__("json").dumps(pending, indent=2), encoding="utf-8"
                )
                await self.app.bot.send_message(
                    chat_id=config.TELEGRAM_CHAT_ID,
                    text=(
                        f"LIMIT ORDER {signal.pair}  {signal.size_eur:.2f}€  "
                        f"@{limit_price:.6f}  TX {txid}\n"
                        f"Scade in {config.LIMIT_TIMEOUT_SEC//60} min se non eseguito"
                    ),
                )
                return   # posizione salvata quando il limit viene eseguito

            else:
                # Live: market order
                vol_str     = kraken_client.format_volume(signal.pair, signal.size_crypto)
                result      = await asyncio.to_thread(
                    kraken_client.add_order,
                    signal.pair, "buy", "market", vol_str,
                )
                txids = result.get("txid", [])
                txid  = txids[0] if txids else "unknown"
                descr = result.get("descr", {}).get("order", "")
                actual_price = signal.price
                size_crypto_net = signal.size_crypto  # real exchange handles net internally

            self.positions[signal.pair] = {
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
                "order_type":    "limit" if config.LIMIT_ORDERS_ENABLED else "market",
            }
            _save_positions(self.positions)

            mode = "[PAPER] " if config.PAPER_TRADING else ""
            await self.app.bot.send_message(
                chat_id=config.TELEGRAM_CHAT_ID,
                text=(
                    f"{mode}BUY {signal.pair}  {signal.size_eur:.2f}€  @{signal.price:.6f}\n"
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

        # Cap sul capitale deployato: non superare MAX_CAPITAL_DEPLOYED
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
        signal_found = False

        # ── Circuit breaker ───────────────────────────────────────────────────
        if config.CIRCUIT_BREAKER_ENABLED:
            trades    = _load_json(TRADES_LOG_FILE, [])
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

        if config.FEAR_GREED_ENABLED and market_data.fear_greed_blocks_trading(fg_val):
            label = mkt_ctx.get("fear_greed_label", "")
            logger.info("SCAN BLOCKED (all pairs): fear_greed=%d (%s)", fg_val, label)
            return False

        # ── Kelly sizing da storico trade ─────────────────────────────────────
        trades_history = _load_json(TRADES_LOG_FILE, [])
        kelly_pct = kelly_size_pct(trades_history)

        # ── BTC market filter — una sola fetch per tutta la scansione ─────────
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

        for pair in config.PAIRS:
            if pair in already_open:
                continue
            if len(self.positions) >= config.MAX_OPEN_TRADES:
                break

            logger.info("Scanning %s ...", pair)
            analysis = await asyncio.to_thread(analyze, pair)
            if analysis is None:
                continue

            signal = generate_signal(
                analysis,
                available_eur,
                already_open,
                btc_analysis=btc_analysis,
                open_pairs=already_open,
            )
            if not signal:
                continue

            logger.info(
                "Signal: %s  score=%d  ADX=%.1f  price=%.6f — asking Groq",
                signal.pair, signal.score, signal.adx, signal.price,
            )

            execute, confidence, reason, size_pct = await asyncio.to_thread(
                groq_evaluator.evaluate, signal, mkt_ctx
            )

            # Kelly sovrascrive la size se disponibile
            effective_size_pct = kelly_pct if kelly_pct else size_pct

            # Ridimensiona la posizione
            if execute and effective_size_pct > 0:
                ai_size = round(available_eur * effective_size_pct, 2)
                ai_size = max(ai_size, 10.0)
                if ai_size <= available_eur:
                    signal.size_eur    = ai_size
                    signal.size_crypto = ai_size / signal.price

            _append_groq_log({
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

                # ── Trailing stop update (ATR-based se disponibile) ──────
                if config.TRAILING_STOP_ENABLED:
                    atr_pos = pos.get("atr", 0)
                    if atr_pos > 0:
                        new_trailing = round(current - config.ATR_SL_MULT * atr_pos, 8)
                    else:
                        new_trailing = round(current * (1 - config.TRAILING_STOP_PCT), 8)
                    old_trailing = pos.get("trailing_stop", pos["stop_loss"])
                    if new_trailing > old_trailing:
                        pos["trailing_stop"] = new_trailing
                        self.positions[pair] = pos
                        _save_positions(self.positions)
                        logger.info("Trailing stop aggiornato %s: %.6f → %.6f", pair, old_trailing, new_trailing)

                effective_sl = pos.get("trailing_stop", pos["stop_loss"])
                hit_tp = current >= pos["take_profit"]
                hit_sl = current <= effective_sl

                if not (hit_tp or hit_sl):
                    continue

                label         = "TAKE PROFIT" if hit_tp else "STOP LOSS"
                gross_pnl_eur = (current - pos["entry"]) * pos["size_crypto"]
                fee_exit      = round(current * pos["size_crypto"] * config.KRAKEN_TAKER_FEE, 4)
                fee_entry     = pos.get("fee_entry_eur", 0.0)
                slip_eur      = round(pos["size_eur"] * config.SLIPPAGE_PCT * 2, 4)
                net_pnl_eur   = gross_pnl_eur - fee_entry - fee_exit - slip_eur
                net_pnl_pct   = net_pnl_eur / pos["size_eur"] * 100

                try:
                    if config.PAPER_TRADING:
                        suffix = "TX: PAPER"
                    else:
                        vol_str = kraken_client.format_volume(pair, pos["size_crypto"])
                        order   = await asyncio.to_thread(
                            kraken_client.add_order, pair, "sell", "market", vol_str
                        )
                        txids  = order.get("txid", [])
                        txid   = txids[0] if txids else "unknown"
                        suffix = f"TX: {txid}"
                except Exception as exc:
                    suffix = f"ERRORE vendita automatica — chiudi manualmente!\n{exc}"

                icon = "✅" if hit_tp else "🛑"
                await ctx.bot.send_message(
                    chat_id=config.TELEGRAM_CHAT_ID,
                    text=(
                        f"{icon} {'TP' if hit_tp else 'SL'} {pair}  "
                        f"{net_pnl_eur:+.2f}€ ({net_pnl_pct:+.2f}%)  "
                        f"fee -{fee_entry+fee_exit:.4f}€  {suffix}"
                    ),
                )
                _append_trade_log({
                    "pair": pair, "exit_reason": "tp" if hit_tp else "sl",
                    "entry": pos["entry"], "exit": current,
                    "size_eur": pos["size_eur"],
                    "gross_pnl_eur": round(gross_pnl_eur, 4),
                    "fee_eur": round(fee_entry + fee_exit, 4),
                    "pnl_eur": round(net_pnl_eur, 4),
                    "pnl_pct": round(net_pnl_pct, 4),
                    "paper": config.PAPER_TRADING, "closed_at": time.time(),
                })
                to_close.append(pair)

            except Exception as exc:
                logger.error("Position monitor error for %s: %s", pair, exc)

        for pair in to_close:
            del self.positions[pair]

        if to_close:
            _save_positions(self.positions)

    # ── Entry point ───────────────────────────────────────────────────────────

    async def _job_daily_report(self, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        positions = self.positions
        trades    = _load_json(TRADES_LOG_FILE, [])
        today_ts  = time.time() - 86400
        today_trades = [t for t in trades if t.get("closed_at", 0) > today_ts]

        # P&L totale
        total_pnl = sum(t["pnl_eur"] for t in trades)
        today_pnl = sum(t["pnl_eur"] for t in today_trades)

        # Posizioni aperte
        pos_lines = []
        for pair, pos in positions.items():
            try:
                ticker  = await asyncio.to_thread(kraken_client.get_ticker, pair)
                current = ticker["last"]
                pnl_pct = (current - pos["entry"]) / pos["entry"] * 100
                pnl_eur = (current - pos["entry"]) * pos["size_crypto"]
                pos_lines.append(f"  {pair} {pnl_pct:+.2f}% ({pnl_eur:+.2f}€)")
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
        """Controlla se i limit order live sono stati eseguiti o sono scaduti."""
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
                # Ordine non più aperto = eseguito
                self.positions[pair] = {
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
                }
                _save_positions(self.positions)
                to_remove.append(pair)
                await self.app.bot.send_message(
                    chat_id=config.TELEGRAM_CHAT_ID,
                    text=f"LIMIT ESEGUITO {pair}  @{po['limit_price']:.6f}  TX {txid}",
                )
            elif now > po["timeout_at"]:
                # Timeout — cancella ordine e usa market
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
                __import__("json").dumps(pending, indent=2), encoding="utf-8"
            )

    async def _cmd_ai(self, update, ctx) -> None:
        """Mostra le ultime N decisioni di Groq AI."""
        groq_log = _load_json(GROQ_LOG_FILE, [])
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

        DISCOVERY_FILE.write_text(
            __import__("json").dumps(result, indent=2, ensure_ascii=False),
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
        # Discovery è ora uno script autonomo (discovery.py)
        # Aggiungila come Task Scheduler separato in DSM.
        jq.run_daily(
            self._job_daily_report,
            time=__import__("datetime").time(hour=config.DAILY_REPORT_HOUR, minute=0),
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
