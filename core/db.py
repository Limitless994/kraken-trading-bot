"""
db.py — SQLite persistence layer (WAL mode) for the trading bot.

Replaces flat JSON files with a single trading.db database.
Migration from JSON is performed once on first startup via migrate_from_json().
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "trading.db"

_logger = logging.getLogger(__name__)

# Module-level write lock — protects concurrent position writes from the
# asyncio scan + position-monitor jobs that run on the same event loop.
_write_lock = threading.Lock()

# ── Connection ────────────────────────────────────────────────────────────────

@contextmanager
def get_conn():
    """Context manager that yields a thread-safe SQLite connection."""
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── Schema ────────────────────────────────────────────────────────────────────

def init_db() -> None:
    """Create all tables (IF NOT EXISTS) and enable WAL mode."""
    with get_conn() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS positions (
                pair TEXT PRIMARY KEY,
                side TEXT NOT NULL,
                entry REAL NOT NULL,
                size_eur REAL,
                size_crypto REAL,
                take_profit REAL,
                stop_loss REAL,
                trailing_stop REAL,
                trailing_active INTEGER DEFAULT 0,
                peak_price REAL,
                opened_at REAL,
                order_type TEXT,
                limit_txid TEXT,
                extra TEXT
            );
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pair TEXT NOT NULL,
                side TEXT NOT NULL,
                entry REAL,
                entry_signal REAL,
                slip_entry_pct REAL,
                exit_price REAL,
                size_eur REAL,
                pnl_eur REAL,
                pnl_pct REAL,
                fee_eur REAL,
                gross_pnl_eur REAL,
                exit_reason TEXT,
                opened_at REAL,
                closed_at REAL
            );
            CREATE TABLE IF NOT EXISTS groq_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL,
                pair TEXT,
                direction TEXT,
                decision TEXT,
                confidence INTEGER,
                score INTEGER,
                reason TEXT,
                rsi_1h REAL,
                rsi_15m REAL,
                macd_hist REAL,
                volume_ratio REAL,
                adx REAL,
                price REAL
            );
            CREATE TABLE IF NOT EXISTS discovery_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL,
                data TEXT
            );
            CREATE TABLE IF NOT EXISTS commands (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL DEFAULT (unixepoch()),
                cmd TEXT NOT NULL,
                args TEXT,
                processed INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            );
            CREATE TABLE IF NOT EXISTS pending_orders (
                pair TEXT PRIMARY KEY,
                txid TEXT NOT NULL,
                limit_price REAL,
                signal_id TEXT,
                size_eur REAL,
                size_crypto REAL,
                placed_at REAL,
                timeout_at REAL,
                take_profit REAL,
                stop_loss REAL,
                trailing_stop REAL
            );
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT,
                level TEXT,
                module TEXT,
                message TEXT,
                UNIQUE(ts, module, message)
            );
        """)
        # Forward-compat ALTERs for DBs created before these columns existed.
        # Silently skipped when the columns are already in place.
        for sql in (
            "ALTER TABLE trades ADD COLUMN entry_signal REAL",
            "ALTER TABLE trades ADD COLUMN slip_entry_pct REAL",
        ):
            try:
                conn.execute(sql)
            except sqlite3.OperationalError:
                pass
    _logger.info("DB inizializzato: %s", DB_PATH)


# ── Positions ─────────────────────────────────────────────────────────────────

def insert_position(pair: str, pos: dict) -> None:
    """Upsert a position (INSERT OR REPLACE)."""
    _dedicated = {
        "pair", "side", "entry", "size_eur", "size_crypto",
        "take_profit", "stop_loss", "trailing_stop", "trailing_active",
        "peak_price", "opened_at", "order_type",
    }
    txid = pos.get("txid")
    extra = {k: v for k, v in pos.items() if k not in _dedicated and k != "txid" and k != "pair"}
    with _write_lock:
        with get_conn() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO positions
                (pair, side, entry, size_eur, size_crypto,
                 take_profit, stop_loss, trailing_stop, trailing_active,
                 peak_price, opened_at, order_type, limit_txid, extra)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    pair,
                    pos.get("side", "long"),
                    pos.get("entry", 0.0),
                    pos.get("size_eur"),
                    pos.get("size_crypto"),
                    pos.get("take_profit"),
                    pos.get("stop_loss"),
                    pos.get("trailing_stop"),
                    int(pos.get("trailing_active", 0)),
                    pos.get("peak_price"),
                    pos.get("opened_at"),
                    pos.get("order_type"),
                    txid,
                    json.dumps(extra) if extra else None,
                ),
            )


update_position = insert_position  # alias — INSERT OR REPLACE handles updates


def delete_position(pair: str) -> None:
    with _write_lock:
        with get_conn() as conn:
            conn.execute("DELETE FROM positions WHERE pair=?", (pair,))


def get_positions() -> dict:
    """Return all open positions as {pair: pos_dict}, compatible with legacy code."""
    if not DB_PATH.exists():
        return {}
    try:
        with get_conn() as conn:
            rows = conn.execute("SELECT * FROM positions").fetchall()
    except Exception:
        return {}
    result = {}
    for row in rows:
        d = dict(row)
        pair = d.pop("pair")
        extra_raw = d.pop("extra", None)
        if extra_raw:
            try:
                d.update(json.loads(extra_raw))
            except Exception:
                pass
        txid = d.pop("limit_txid", None)
        if txid is not None:
            d["txid"] = txid
        result[pair] = d
    return result


# ── Pending limit orders ──────────────────────────────────────────────────────

def insert_pending_order(pair: str, po: dict) -> None:
    """Upsert a pending limit order keyed by pair."""
    with _write_lock:
        with get_conn() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO pending_orders
                (pair, txid, limit_price, signal_id, size_eur, size_crypto,
                 placed_at, timeout_at, take_profit, stop_loss, trailing_stop)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    pair,
                    po.get("txid", ""),
                    po.get("limit_price"),
                    po.get("signal_id"),
                    po.get("size_eur"),
                    po.get("size_crypto"),
                    po.get("placed_at"),
                    po.get("timeout_at"),
                    po.get("take_profit"),
                    po.get("stop_loss"),
                    po.get("trailing_stop"),
                ),
            )


def delete_pending_order(pair: str) -> None:
    with _write_lock:
        with get_conn() as conn:
            conn.execute("DELETE FROM pending_orders WHERE pair=?", (pair,))


def get_pending_orders() -> dict:
    """Return all pending orders as {pair: po_dict}."""
    if not DB_PATH.exists():
        return {}
    try:
        with get_conn() as conn:
            rows = conn.execute("SELECT * FROM pending_orders").fetchall()
    except Exception:
        return {}
    result = {}
    for row in rows:
        d = dict(row)
        pair = d.pop("pair")
        result[pair] = d
    return result


# ── Trades ────────────────────────────────────────────────────────────────────

def insert_trade(trade: dict) -> None:
    # Derive entry slippage when the caller didn't pre-compute it.
    entry        = trade.get("entry")
    entry_signal = trade.get("entry_signal")
    slip_pct     = trade.get("slip_entry_pct")
    if slip_pct is None and entry and entry_signal:
        try:
            slip_pct = (entry - entry_signal) / entry_signal * 100
        except ZeroDivisionError:
            slip_pct = None
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO trades
            (pair, side, entry, entry_signal, slip_entry_pct, exit_price,
             size_eur, pnl_eur, pnl_pct, fee_eur, gross_pnl_eur,
             exit_reason, opened_at, closed_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                trade.get("pair", ""),
                trade.get("side", "long"),
                entry,
                entry_signal,
                slip_pct,
                trade.get("exit"),        # JSON used "exit", DB column is exit_price
                trade.get("size_eur"),
                trade.get("pnl_eur"),
                trade.get("pnl_pct"),
                trade.get("fee_eur"),
                trade.get("gross_pnl_eur"),
                trade.get("exit_reason"),
                trade.get("opened_at"),
                trade.get("closed_at"),
            ),
        )


def get_trades() -> list:
    """Return all trades as list of dicts. 'exit_price' mapped back to 'exit' for compatibility."""
    if not DB_PATH.exists():
        return []
    try:
        with get_conn() as conn:
            rows = conn.execute("SELECT * FROM trades ORDER BY id").fetchall()
    except Exception:
        return []
    result = []
    for row in rows:
        t = dict(row)
        t["exit"] = t.pop("exit_price", None)  # backward-compat alias
        result.append(t)
    return result


def get_sl_count_24h(pair: str) -> int:
    """Return number of stop-loss exits for `pair` in the last 24 hours."""
    if not DB_PATH.exists():
        return 0
    cutoff = time.time() - 86400
    try:
        with get_conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM trades "
                "WHERE pair=? AND LOWER(exit_reason)='sl' AND closed_at >= ?",
                (pair, cutoff),
            ).fetchone()
    except Exception:
        return 0
    return row[0] if row else 0


def get_pairs_in_sl_cooldown(hours: float) -> set:
    """Return pairs that hit a stop-loss exit within the last `hours` hours."""
    if not DB_PATH.exists():
        return set()
    cutoff = time.time() - hours * 3600
    try:
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT DISTINCT pair FROM trades "
                "WHERE LOWER(exit_reason)='sl' AND closed_at >= ?",
                (cutoff,),
            ).fetchall()
    except Exception:
        return set()
    return {r["pair"] for r in rows}


def consecutive_losses() -> int:
    """Count consecutive losing trades from the most recent backwards.

    A losing trade is one with pnl_eur <= 0. The streak ends as soon as we
    encounter a winning trade. Returns 0 if no trades exist.
    """
    if not DB_PATH.exists():
        return 0
    try:
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT pnl_eur FROM trades ORDER BY closed_at DESC LIMIT 50"
            ).fetchall()
    except Exception:
        return 0
    streak = 0
    for r in rows:
        pnl = r["pnl_eur"] or 0.0
        if pnl <= 0:
            streak += 1
        else:
            break
    return streak


def seconds_since_last_trade():
    """Seconds elapsed since the most recent trade closed, or None if no trades.

    Used by the consecutive-loss circuit breaker to auto-recover: a loss streak
    that never sees a new winning trade would otherwise latch the breaker forever
    once the bot stops opening positions. After a quiet period we let it probe.
    """
    if not DB_PATH.exists():
        return None
    try:
        with get_conn() as conn:
            row = conn.execute("SELECT MAX(closed_at) AS last FROM trades").fetchone()
    except Exception:
        return None
    if not row or row["last"] is None:
        return None
    return max(0.0, time.time() - float(row["last"]))


def get_slippage_summary_24h() -> dict:
    """Average and max entry slippage % over trades closed in the last 24h.

    Returns {"count": int, "avg_pct": float, "max_pct": float}.
    """
    empty = {"count": 0, "avg_pct": 0.0, "max_pct": 0.0}
    if not DB_PATH.exists():
        return empty
    cutoff = time.time() - 86400
    try:
        with get_conn() as conn:
            row = conn.execute(
                """
                SELECT COUNT(slip_entry_pct) AS n,
                       AVG(ABS(slip_entry_pct)) AS avg_abs,
                       MAX(ABS(slip_entry_pct)) AS max_abs
                FROM trades
                WHERE closed_at >= ? AND slip_entry_pct IS NOT NULL
                """,
                (cutoff,),
            ).fetchone()
    except Exception:
        return empty
    if not row or not row["n"]:
        return empty
    return {
        "count":  int(row["n"]),
        "avg_pct": float(row["avg_abs"] or 0.0),
        "max_pct": float(row["max_abs"] or 0.0),
    }


# ── Groq decisions ────────────────────────────────────────────────────────────

def insert_groq_decision(entry: dict) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO groq_decisions
            (ts, pair, direction, decision, confidence, score, reason,
             rsi_1h, rsi_15m, macd_hist, volume_ratio, adx, price)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                entry.get("ts"),
                entry.get("pair"),
                entry.get("direction"),
                entry.get("decision"),
                entry.get("confidence"),
                entry.get("score"),
                entry.get("reason"),
                entry.get("rsi_1h"),
                entry.get("rsi_15m"),
                entry.get("macd_hist"),
                entry.get("volume_ratio"),
                entry.get("adx"),
                entry.get("price"),
            ),
        )


def get_groq_decisions(limit: int = 500) -> list:
    """Return last `limit` Groq decisions in chronological order."""
    if not DB_PATH.exists():
        return []
    try:
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM groq_decisions ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
    except Exception:
        return []
    return [dict(row) for row in reversed(rows)]


# ── Discovery ─────────────────────────────────────────────────────────────────

def insert_discovery(data: dict) -> None:
    ts = data.get("timestamp", time.time())
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO discovery_results (ts, data) VALUES (?,?)",
            (ts, json.dumps(data, ensure_ascii=False)),
        )


def get_latest_discovery() -> dict | None:
    if not DB_PATH.exists():
        return None
    try:
        with get_conn() as conn:
            row = conn.execute(
                "SELECT data FROM discovery_results ORDER BY id DESC LIMIT 1"
            ).fetchone()
    except Exception:
        return None
    if row:
        try:
            return json.loads(row[0])
        except Exception:
            return None
    return None


# ── Settings ──────────────────────────────────────────────────────────────────

def get_setting(key: str, default: str = None) -> str | None:
    if not DB_PATH.exists():
        return default
    try:
        with get_conn() as conn:
            row = conn.execute(
                "SELECT value FROM settings WHERE key=?", (key,)
            ).fetchone()
        return row[0] if row else default
    except Exception:
        return default


def set_setting(key: str, value: str) -> None:
    if not DB_PATH.exists():
        return
    try:
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
    except Exception as exc:
        _logger.error("set_setting(%s) fallito: %s", key, exc)


# ── Commands ──────────────────────────────────────────────────────────────────

def push_command(cmd: dict) -> None:
    """Enqueue a command from the dashboard."""
    args = {k: v for k, v in cmd.items() if k not in ("cmd", "ts", "id")}
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO commands (ts, cmd, args, processed) VALUES (?,?,?,0)",
            (
                cmd.get("ts", time.time()),
                cmd.get("cmd", ""),
                json.dumps(args) if args else None,
            ),
        )


def pop_commands() -> list:
    """Atomically return all unprocessed commands and mark them processed."""
    if not DB_PATH.exists():
        return []
    try:
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM commands WHERE processed=0 ORDER BY id"
            ).fetchall()
            if rows:
                ids = [row["id"] for row in rows]
                conn.execute(
                    "UPDATE commands SET processed=1 WHERE id IN ({})".format(
                        ",".join("?" * len(ids))
                    ),
                    ids,
                )
    except Exception as exc:
        _logger.error("pop_commands fallito: %s", exc)
        return []
    result = []
    for row in rows:
        entry = {"cmd": row["cmd"], "ts": row["ts"]}
        if row["args"]:
            try:
                entry.update(json.loads(row["args"]))
            except Exception:
                pass
        result.append(entry)
    return result


# ── Migration from JSON ───────────────────────────────────────────────────────

def migrate_from_json() -> None:
    """
    Import each JSON state file into the DB.
    On success, rename the source file to <name>.json.bak.
    Idempotent: skipped if the .json file is already gone.
    """
    _root = DB_PATH.parent

    # positions.json → positions table
    _migrate_positions(_root / "positions.json")

    # trades_log.json → trades table
    _migrate_trades(_root / "trades_log.json")

    # groq_log.json → groq_decisions table
    _migrate_groq_log(_root / "groq_log.json")

    # discovery_log.json → discovery_results table
    _migrate_discovery(_root / "discovery_log.json")

    # bot_commands.json → commands table
    _migrate_commands(_root / "bot_commands.json")

    # active_profile.json → settings table (key "active_profile")
    _migrate_profile(_root / "active_profile.json")

    # pending_orders.json → pending_orders table
    _migrate_pending_orders(_root / "pending_orders.json")


def _bak(path: Path) -> None:
    bak = path.with_suffix(".json.bak")
    try:
        path.rename(bak)
    except Exception as exc:
        _logger.warning("Impossibile rinominare %s: %s", path, exc)


def _migrate_positions(path: Path) -> None:
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        with get_conn() as conn:
            for pair, pos in data.items():
                _dedicated = {
                    "side", "entry", "size_eur", "size_crypto",
                    "take_profit", "stop_loss", "trailing_stop",
                    "trailing_active", "peak_price", "opened_at", "order_type",
                }
                txid = pos.get("txid")
                extra = {
                    k: v for k, v in pos.items()
                    if k not in _dedicated and k not in ("txid", "pair")
                }
                conn.execute(
                    """
                    INSERT OR REPLACE INTO positions
                    (pair, side, entry, size_eur, size_crypto,
                     take_profit, stop_loss, trailing_stop, trailing_active,
                     peak_price, opened_at, order_type, limit_txid, extra)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        pair,
                        pos.get("side", "long"),
                        pos.get("entry", 0.0),
                        pos.get("size_eur"),
                        pos.get("size_crypto"),
                        pos.get("take_profit"),
                        pos.get("stop_loss"),
                        pos.get("trailing_stop"),
                        int(pos.get("trailing_active", 0)),
                        pos.get("peak_price"),
                        pos.get("opened_at"),
                        pos.get("order_type"),
                        txid,
                        json.dumps(extra) if extra else None,
                    ),
                )
        _logger.info("Migrato positions.json → %d posizioni", len(data))
        _bak(path)
    except Exception as exc:
        _logger.error("Migrazione positions.json fallita: %s", exc)


def _migrate_trades(path: Path) -> None:
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        with get_conn() as conn:
            conn.executemany(
                """
                INSERT INTO trades
                (pair, side, entry, exit_price, size_eur, pnl_eur, pnl_pct,
                 fee_eur, gross_pnl_eur, exit_reason, opened_at, closed_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                [
                    (
                        t.get("pair", ""),
                        t.get("side", "long"),
                        t.get("entry"),
                        t.get("exit"),
                        t.get("size_eur"),
                        t.get("pnl_eur"),
                        t.get("pnl_pct"),
                        t.get("fee_eur"),
                        t.get("gross_pnl_eur"),
                        t.get("exit_reason"),
                        t.get("opened_at"),
                        t.get("closed_at"),
                    )
                    for t in data
                ],
            )
        _logger.info("Migrato trades_log.json → %d trade", len(data))
        _bak(path)
    except Exception as exc:
        _logger.error("Migrazione trades_log.json fallita: %s", exc)


def _migrate_groq_log(path: Path) -> None:
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        with get_conn() as conn:
            conn.executemany(
                """
                INSERT INTO groq_decisions
                (ts, pair, direction, decision, confidence, score, reason,
                 rsi_1h, rsi_15m, macd_hist, volume_ratio, adx, price)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                [
                    (
                        g.get("ts"),
                        g.get("pair"),
                        g.get("direction"),
                        g.get("decision"),
                        g.get("confidence"),
                        g.get("score"),
                        g.get("reason"),
                        g.get("rsi_1h"),
                        g.get("rsi_15m"),
                        g.get("macd_hist"),
                        g.get("volume_ratio"),
                        g.get("adx"),
                        g.get("price"),
                    )
                    for g in data
                ],
            )
        _logger.info("Migrato groq_log.json → %d decisioni", len(data))
        _bak(path)
    except Exception as exc:
        _logger.error("Migrazione groq_log.json fallita: %s", exc)


def _migrate_discovery(path: Path) -> None:
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO discovery_results (ts, data) VALUES (?,?)",
                (data.get("timestamp", time.time()), json.dumps(data, ensure_ascii=False)),
            )
        _logger.info("Migrato discovery_log.json")
        _bak(path)
    except Exception as exc:
        _logger.error("Migrazione discovery_log.json fallita: %s", exc)


def _migrate_commands(path: Path) -> None:
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data:
            with get_conn() as conn:
                for cmd in data:
                    args = {
                        k: v for k, v in cmd.items()
                        if k not in ("cmd", "ts", "id")
                    }
                    conn.execute(
                        "INSERT INTO commands (ts, cmd, args, processed) VALUES (?,?,?,0)",
                        (
                            cmd.get("ts", time.time()),
                            cmd.get("cmd", ""),
                            json.dumps(args) if args else None,
                        ),
                    )
            _logger.info("Migrato bot_commands.json → %d comandi", len(data))
        _bak(path)
    except Exception as exc:
        _logger.error("Migrazione bot_commands.json fallita: %s", exc)


def _migrate_profile(path: Path) -> None:
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        profile = data.get("profile", "conservative")
        set_setting("active_profile", profile)
        _logger.info("Migrato active_profile.json → profilo '%s'", profile)
        _bak(path)
    except Exception as exc:
        _logger.error("Migrazione active_profile.json fallita: %s", exc)


def _migrate_pending_orders(path: Path) -> None:
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data:
            for pair, po in data.items():
                insert_pending_order(pair, po)
            _logger.info("Migrato pending_orders.json → %d ordini pendenti", len(data))
        _bak(path)
    except Exception as exc:
        _logger.error("Migrazione pending_orders.json fallita: %s", exc)


# ── Log import ────────────────────────────────────────────────────────────────

_LOG_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \[(\w+)\] ([\w._]+): (.+)$"
)

_LOG_FILES = [
    "bot.log", "bot.log.1", "bot.log.2", "bot.log.3",
    "trading.log", "analyzer.log", "strategy.log",
    "groq.log", "market.log", "discovery.log", "kraken.log",
]

# Largest file (groq.log ~28 MB) is processed in chunks to keep memory sane
_CHUNK_SIZE = 5000


def import_logs() -> None:
    """
    Parse .log files from 'log da migrare/' and insert rows into audit_log.
    Duplicates are silently skipped via INSERT OR IGNORE on the UNIQUE index.
    """
    log_dir = DB_PATH.parent / "log da migrare"
    if not log_dir.exists():
        _logger.info("Cartella 'log da migrare' non trovata — skip import log")
        return

    total_imported = 0

    for fname in _LOG_FILES:
        fpath = log_dir / fname
        if not fpath.exists():
            continue
        file_imported = 0
        try:
            text = fpath.read_text(encoding="utf-8", errors="replace")
            rows: list = []
            for line in text.splitlines():
                m = _LOG_RE.match(line.strip())
                if not m:
                    continue
                rows.append((m.group(1), m.group(2), m.group(3), m.group(4)))
                if len(rows) >= _CHUNK_SIZE:
                    file_imported += _insert_log_chunk(rows)
                    rows = []
            if rows:
                file_imported += _insert_log_chunk(rows)
            _logger.info("audit_log: %d righe da %s", file_imported, fname)
            total_imported += file_imported
        except Exception as exc:
            _logger.error("Errore import log %s: %s", fname, exc)

    _logger.info("audit_log: %d righe totali importate", total_imported)


def _insert_log_chunk(rows: list) -> int:
    try:
        with get_conn() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO audit_log (ts, level, module, message) VALUES (?,?,?,?)",
                rows,
            )
        return len(rows)
    except Exception as exc:
        _logger.error("Errore insert audit_log chunk: %s", exc)
        return 0
