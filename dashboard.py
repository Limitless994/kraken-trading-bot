"""
dashboard.py — Flask dashboard con layout a tab e SSE live updates.
"""
from __future__ import annotations

import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import requests
from flask import Flask, Response, jsonify, redirect, render_template_string, request, stream_with_context, url_for

from core import config, db, market_data

app = Flask(__name__)

_DYNAMIC_PAIRS_FILE = Path(__file__).parent / "dynamic_pairs.json"

# ── Helpers ────────────────────────────────────────────────────────────────────

def _fetch_price(pair: str):
    try:
        r = requests.get(f"https://api.kraken.com/0/public/Ticker?pair={pair}", timeout=3)
        result = r.json()["result"]
        return float(result[next(iter(result))]["c"][0])
    except Exception:
        return None


def _fetch_prices_concurrent(pairs: list) -> dict:
    if not pairs:
        return {}
    with ThreadPoolExecutor(max_workers=min(len(pairs), 6)) as ex:
        futs = {ex.submit(_fetch_price, p): p for p in pairs}
        return {futs[f]: f.result() for f in as_completed(futs)}


def _fetch_kraken_status() -> str:
    try:
        r = requests.get("https://api.kraken.com/0/public/SystemStatus", timeout=4)
        return r.json().get("result", {}).get("status", "unknown")
    except Exception:
        return "error"


def _fmt_time(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%d/%m %H:%M")


def _parse_btc_filter() -> dict:
    try:
        raw = db.get_setting("btc_filter_state")
        if raw:
            return json.loads(raw)
    except Exception:
        pass
    return {}


def _load_dynamic_pairs() -> list:
    try:
        if _DYNAMIC_PAIRS_FILE.exists():
            data = json.loads(_DYNAMIC_PAIRS_FILE.read_text(encoding="utf-8"))
            return list(data.keys()) if isinstance(data, dict) else list(data)
    except Exception:
        pass
    return []


def _queue_command(cmd: dict) -> None:
    try:
        cmd.setdefault("id", str(uuid.uuid4())[:8])
        cmd.setdefault("ts", time.time())
        db.push_command(cmd)
    except Exception as exc:
        app.logger.error("queue_command error: %s", exc)


def _enrich_positions(positions_raw: dict, prices: dict = None) -> list:
    if prices is None:
        prices = _fetch_prices_concurrent(list(positions_raw.keys()))
    enriched = []
    for pair, pos in positions_raw.items():
        price = prices.get(pair)
        side = pos.get("side", "long")
        if price:
            if side == "short":
                pnl_pct = (pos["entry"] - price) / pos["entry"] * 100
                pnl_eur = (pos["entry"] - price) * pos.get("size_crypto", 0)
            else:
                pnl_pct = (price - pos["entry"]) / pos["entry"] * 100
                pnl_eur = (price - pos["entry"]) * pos.get("size_crypto", 0)
        else:
            pnl_pct = pnl_eur = 0.0
        enriched.append({
            "pair":      pair,
            "side":      side,
            "entry":     pos["entry"],
            "price":     price or 0.0,
            "pnl_pct":   round(pnl_pct, 4),
            "pnl_eur":   round(pnl_eur, 4),
            "tp":        pos.get("take_profit", 0),
            "sl":        pos.get("stop_loss", 0),
            "trailing":  pos.get("trailing_stop"),
            "size_eur":  pos.get("size_eur", 0),
            "opened_at": _fmt_time(pos.get("opened_at", time.time())),
        })
    return enriched


def _collect_live_data() -> dict:
    positions_raw = db.get_positions()
    trades_log    = db.get_trades()
    groq_log      = db.get_groq_decisions(500)
    enriched      = _enrich_positions(positions_raw)

    total_pnl    = sum(t["pnl_eur"] for t in trades_log)
    total_fees   = sum(t.get("fee_eur", 0.0) for t in trades_log)
    wins         = sum(1 for t in trades_log if t["pnl_eur"] > 0)
    total_trades = len(trades_log)
    winrate      = (wins / total_trades * 100) if total_trades else 0.0
    groq_approved = sum(1 for g in groq_log if g["decision"] == "ESEGUI")
    groq_rejected = sum(1 for g in groq_log if g["decision"] == "RIFIUTA")

    today_ts   = time.time() - 86400
    daily_loss = sum(t["pnl_eur"] for t in trades_log if t.get("closed_at", 0) > today_ts and t["pnl_eur"] < 0)
    cb_active  = config.CIRCUIT_BREAKER_ENABLED and abs(daily_loss) >= config.CIRCUIT_BREAKER_LOSS_EUR

    return {
        "positions": enriched,
        "metrics": {
            "open_count":    len(enriched),
            "max_trades":    config.MAX_OPEN_TRADES,
            "total_trades":  total_trades,
            "total_pnl":     round(total_pnl, 4),
            "winrate":       round(winrate, 1),
            "groq_approved": groq_approved,
            "groq_rejected": groq_rejected,
            "total_fees":    round(total_fees, 4),
        },
        "btc":            _parse_btc_filter(),
        "circuit_breaker": cb_active,
    }


def _profile_to_form(pd: dict) -> dict:
    return {
        "MIN_SIGNAL_SCORE":        pd.get("MIN_SIGNAL_SCORE", 3),
        "ADX_MIN":                 pd.get("ADX_MIN", 20),
        "VOLUME_SPIKE_MULT":       pd.get("VOLUME_SPIKE_MULT", 1.15),
        "RSI_1H_BUY_MAX":          pd.get("RSI_1H_BUY_MAX", 65),
        "RSI_15M_BUY_MAX":         pd.get("RSI_15M_BUY_MAX", 62),
        "BTC_RSI_MAX":             pd.get("BTC_RSI_MAX", 65),
        "GROQ_MIN_CONFIDENCE":     pd.get("GROQ_MIN_CONFIDENCE", 6),
        "ATR_TP_MULT":             pd.get("ATR_TP_MULT", 2.5),
        "ATR_SL_MULT":             pd.get("ATR_SL_MULT", 1.0),
        "ATR_TP_MULT_SHORT":       pd.get("ATR_TP_MULT_SHORT", 2.5),
        "ATR_SL_MULT_SHORT":       pd.get("ATR_SL_MULT_SHORT", 1.3),
        "TRAILING_ACTIVATION_ATR": pd.get("TRAILING_ACTIVATION_ATR", 1.0),
        "TRAILING_DISTANCE_ATR":   pd.get("TRAILING_DISTANCE_ATR", 1.5),
        "CIRCUIT_BREAKER_LOSS_EUR": pd.get("CIRCUIT_BREAKER_LOSS_EUR", 15),
        "MAX_OPEN_TRADES":         pd.get("MAX_OPEN_TRADES", 4),
        "MAX_CAPITAL_DEPLOYED_PCT": round(pd.get("MAX_CAPITAL_DEPLOYED", 0.80) * 100),
        "AI_SIZE_HIGH_PCT":        round(pd.get("AI_SIZE_HIGH", 0.25) * 100),
        "AI_SIZE_MED_PCT":         round(pd.get("AI_SIZE_MED", 0.18) * 100),
        "AI_SIZE_MIN_PCT":         round(pd.get("AI_SIZE_MIN", 0.12) * 100),
        "SCAN_INTERVAL_MINUTES":   config.SCAN_INTERVAL_MINUTES,
        "BUDGET_EUR":              config.BUDGET_EUR,
    }


def _trade_to_js(t: dict) -> dict:
    esito_map = {"tp": "✅ TP", "sl": "\U0001f6d1 SL", "manual": "\U0001f527 Manuale"}
    gross   = t.get("gross_pnl_eur", t["pnl_eur"])
    pnl_eur = t["pnl_eur"]
    pnl_pct = t.get("pnl_pct", 0.0)
    return {
        "date":  _fmt_time(t["closed_at"]),
        "pair":  t["pair"],
        "side":  t.get("side", "long"),
        "esito": esito_map.get(t.get("exit_reason", ""), "\U0001f527 Manuale"),
        "entry": f"{t['entry']:.6g}",
        "exit":  f"{t['exit']:.6g}",
        "gross": f"{gross:+.2f}€",
        "fee":   f"-{t.get('fee_eur', 0):.4f}€",
        "net":   f"{pnl_eur:+.2f}€",
        "pct":   f"{pnl_pct:+.2f}%",
    }


def _groq_to_js(g: dict) -> dict:
    return {
        "ok":    g["decision"] == "ESEGUI",
        "dir":   g.get("direction", "LONG"),
        "pair":  g.get("pair", ""),
        "conf":  g.get("confidence", 0),
        "score": g.get("score", 0),
        "time":  _fmt_time(g["ts"]),
        "reason": g.get("reason", ""),
        "rsi1h":  f"{g.get('rsi_1h', 0):.1f}",
        "rsi15m": f"{g.get('rsi_15m', 0):.1f}",
        "macd":   f"{g.get('macd_hist', 0):+.4f}",
        "vol":    f"{g.get('volume_ratio', 0):.2f}x",
        "adx":    f"{g.get('adx', 0):.1f}",
        "price":  f"{g.get('price', 0):.6g}",
    }


def _disc_to_js(c: dict) -> dict:
    score = c.get("mech_score", 0)
    cls   = "bg-success" if score >= 7 else ("bg-secondary" if score >= 5 else "bg-danger")
    return {
        "pair":       c.get("pair", ""),
        "score":      score,
        "trainWR":    f"{c.get('train_win_rate', 0)}%",
        "testWR":     f"{c.get('test_win_rate', 0)}%",
        "consist":    f"{c.get('consistency', 0)*100:.0f}%",
        "sharpe":     f"{c.get('test_sharpe', 0):.2f}",
        "pnl":        f"{c.get('avg_pnl_pct', 0):+.2f}%",
        "rsi":        str(c.get("rsi_now", "—")),
        "adx":        str(c.get("adx_now", "—")),
        "scoreClass": cls,
    }


# ── Message labels ─────────────────────────────────────────────────────────────

_MSG_LABELS = {
    "profile_changed":  "Profilo aggiornato",
    "scan_queued":      "Scan in coda — eseguito entro 15s",
    "discover_queued":  "Discovery avviata in background",
    "close_queued":     "Chiusura in coda — entro 15s",
    "restart_queued":   "Riavvio in coda — max 15s",
    "params_saved":     "Parametri salvati ✓",
    "param_rr_error":   "Errore: ATR TP deve essere > ATR SL",
    "param_size_error": "Errore: le size devono essere HIGH > MED > MIN",
    "error":            "Errore — controlla i log",
    "unknown_profile":  "Profilo non valido",
    "no_pair":          "Coppia non specificata",
}


# ── Action routes ──────────────────────────────────────────────────────────────

@app.route("/action/profile", methods=["POST"])
def action_profile():
    name = request.form.get("profile", "").lower()
    if name not in config.PROFILES:
        return redirect(url_for("index", msg="unknown_profile"))
    try:
        config.apply_profile(name)
        _queue_command({"cmd": "profile", "name": name})
        return redirect(url_for("index", msg="profile_changed"))
    except Exception:
        return redirect(url_for("index", msg="error"))


@app.route("/action/scan", methods=["POST"])
def action_scan():
    _queue_command({"cmd": "scan"})
    return redirect(url_for("index", msg="scan_queued"))


@app.route("/action/discover", methods=["POST"])
def action_discover():
    _queue_command({"cmd": "discover"})
    return redirect(url_for("index", msg="discover_queued"))


@app.route("/action/close", methods=["POST"])
def action_close():
    pair = request.form.get("pair", "").upper()
    if not pair:
        return redirect(url_for("index", msg="no_pair"))
    _queue_command({"cmd": "close", "pair": pair})
    return redirect(url_for("index", msg="close_queued"))


@app.route("/action/restart", methods=["POST"])
def action_restart():
    _queue_command({"cmd": "restart"})
    return redirect(url_for("index", msg="restart_queued"))


@app.route("/action/set_params", methods=["POST"])
def action_set_params():
    profile_name = request.form.get("profile", "custom").lower()

    def _f(n):
        try:
            return float(request.form.get(n, "0"))
        except (ValueError, TypeError):
            return 0.0

    if _f("ATR_TP_MULT") <= _f("ATR_SL_MULT"):
        return redirect(url_for("index", msg="param_rr_error"))
    if not (_f("AI_SIZE_HIGH_PCT") > _f("AI_SIZE_MED_PCT") > _f("AI_SIZE_MIN_PCT")):
        return redirect(url_for("index", msg="param_size_error"))

    param_keys = [
        "MIN_SIGNAL_SCORE", "ADX_MIN", "VOLUME_SPIKE_MULT",
        "RSI_1H_BUY_MAX", "RSI_15M_BUY_MAX", "BTC_RSI_MAX",
        "GROQ_MIN_CONFIDENCE", "ATR_TP_MULT", "ATR_SL_MULT",
        "ATR_TP_MULT_SHORT", "ATR_SL_MULT_SHORT",
        "TRAILING_ACTIVATION_ATR", "TRAILING_DISTANCE_ATR",
        "CIRCUIT_BREAKER_LOSS_EUR", "MAX_OPEN_TRADES",
        "MAX_CAPITAL_DEPLOYED_PCT", "AI_SIZE_HIGH_PCT",
        "AI_SIZE_MED_PCT", "AI_SIZE_MIN_PCT",
        "SCAN_INTERVAL_MINUTES", "BUDGET_EUR",
    ]
    params = {k: _f(k) for k in param_keys if request.form.get(k) is not None}
    try:
        db.set_setting(f"profile_{profile_name}", json.dumps(params))
        config.apply_profile_params(params)
        return redirect(url_for("index", msg="params_saved"))
    except Exception:
        return redirect(url_for("index", msg="error"))


@app.route("/api/balance")
def api_balance():
    try:
        from core import kraken_client
        bal = kraken_client.get_balance()
        eur = bal.pop("ZEUR", bal.pop("EUR", 0.0))
        return jsonify({"ok": True, "eur": eur, "crypto": bal})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)})


@app.route("/api/stream")
def stream():
    def generate():
        try:
            while True:
                try:
                    payload = _collect_live_data()
                    yield "data: " + json.dumps(payload) + "\n\n"
                except Exception as e:
                    yield "data: " + json.dumps({"error": str(e)}) + "\n\n"
                time.sleep(10)
        except GeneratorExit:
            pass
    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Template ───────────────────────────────────────────────────────────────────

TEMPLATE = """<!DOCTYPE html>
<html lang="it">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Trading Bot</title>
  <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
  <style>
    body { background:#0f1117; color:#e0e0e0; font-family:'Segoe UI',sans-serif; }
    .card { background:#1a1d27; border:1px solid #2a2d3e; border-radius:8px; }
    .card-header { background:#22263a; border-bottom:1px solid #2a2d3e; color:#e0e0e0; font-weight:600; padding:10px 16px; font-size:0.88rem; }
    .table { color:#e0e0e0; font-size:0.85rem; margin-bottom:0; }
    .table-dark { --bs-table-bg:#1a1d27; }
    .table-dark td, .table-dark th { border-color:#2a2d3e; }
    .table-responsive { border-radius:0 0 8px 8px; }
    .badge-online { background:#1a7a4a; }
    .badge-warn   { background:#7a5a1a; }
    .badge-error  { background:#7a1a1a; }
    .metric-val   { font-size:1.35rem; font-weight:700; margin-top:4px; color:#fff; }
    .metric-label { color:#aaa; font-size:0.72rem; text-transform:uppercase; letter-spacing:.04em; }
    .pnl-pos { color:#4caf82; }
    .pnl-neg { color:#e05c5c; }
    .side-long  { color:#4caf82; font-size:.75rem; font-weight:700; }
    .side-short { color:#e05c5c; font-size:.75rem; font-weight:700; }
    .groq-esegui  { border-left:3px solid #4caf82; background:#111c14; padding:10px 14px; border-radius:4px; margin-bottom:8px; }
    .groq-rifiuta { border-left:3px solid #e05c5c; background:#1c1111; padding:10px 14px; border-radius:4px; margin-bottom:8px; }
    .groq-reason  { font-size:.87rem; margin-top:5px; color:#e0e0e0; line-height:1.4; }
    .groq-stats   { font-size:.78rem; color:#b0b0b0; margin-top:5px; display:flex; flex-wrap:wrap; gap:4px 14px; }
    .conf-badge   { font-size:.7rem; padding:2px 6px; border-radius:10px; background:#2a2d3e; color:#ccc; }
    .nav-tabs { border-bottom:1px solid #2a2d3e; flex-wrap:nowrap; overflow-x:auto; overflow-y:hidden; -webkit-overflow-scrolling:touch; }
    .nav-tabs::-webkit-scrollbar { height:3px; }
    .nav-tabs::-webkit-scrollbar-thumb { background:#2a2d3e; }
    .nav-tabs .nav-link { color:#aaa; border:none; border-bottom:2px solid transparent; border-radius:0; padding:8px 14px; font-size:.84rem; white-space:nowrap; }
    .nav-tabs .nav-link:hover { color:#e0e0e0; border-bottom-color:#4a4d5e; }
    .nav-tabs .nav-link.active { color:#4caf82; background:transparent; border-bottom:2px solid #4caf82; }
    .tab-content { padding-top:16px; }
    .sse-dot { width:8px; height:8px; border-radius:50%; background:#4caf82; display:inline-block; animation:pulse 2s infinite; }
    @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.3} }
    .chart-container { position:relative; width:100%; }
    .btc-card { text-align:center; padding:12px 8px; border-radius:6px; background:#1a1d27; border:1px solid #2a2d3e; }
    .btc-card .val { font-size:1.25rem; font-weight:700; }
    .btc-card .lbl { font-size:.72rem; color:#aaa; margin-bottom:4px; }
    .btc-card .sub { font-size:.72rem; color:#888; margin-top:2px; }
    .pagination-bar { display:flex; align-items:center; justify-content:space-between; padding:8px 16px; border-top:1px solid #2a2d3e; font-size:.8rem; color:#aaa; background:#1a1d27; border-radius:0 0 8px 8px; }
    .pagination-bar .btn { font-size:.78rem; padding:2px 10px; }
    .page-info { display:flex; align-items:center; gap:8px; }
    .news-item { font-size:.82rem; padding:6px 0; border-bottom:1px solid #2a2d3e; color:#d0d0d0; line-height:1.4; }
    .news-item:last-child { border-bottom:none; }
    .cfg-row { display:flex; flex-wrap:wrap; gap:4px 20px; font-size:.82rem; }
    .cfg-key { color:#888; }
    .cfg-val { color:#e0e0e0; font-weight:500; }
    .param-input { background:#0f1117; border:1px solid #2a2d3e; color:#e0e0e0; border-radius:4px; padding:3px 8px; width:90px; font-size:.82rem; text-align:right; }
    .param-input:focus { outline:none; border-color:#4caf82; box-shadow:0 0 0 2px rgba(76,175,130,.15); }
    .param-input:invalid { border-color:#e05c5c; }
    .param-row { display:flex; align-items:center; justify-content:space-between; padding:5px 0; border-bottom:1px solid #1e2132; }
    .param-row:last-child { border-bottom:none; }
    .param-label { font-size:.82rem; color:#ccc; }
    .param-label small { display:block; font-size:.72rem; color:#666; }
    .param-range { font-size:.7rem; color:#555; margin-right:6px; white-space:nowrap; }
    .param-group-title { font-size:.72rem; text-transform:uppercase; letter-spacing:.06em; color:#666; margin:14px 0 6px; }
    @media (max-width:767px) { .col-hide-sm { display:none; } .metric-val { font-size:1.1rem; } h5.fw-bold { font-size:1rem; } }
  </style>
</head>
<body>
<div class="container-fluid py-3 px-3 px-md-4">

  <!-- Header -->
  <div class="d-flex align-items-center gap-2 mb-3 flex-wrap">
    <h5 class="mb-0 fw-bold me-1">Kraken Trading Bot</h5>
    {% if paper %}<span class="badge bg-warning text-dark">PAPER</span>{% else %}<span class="badge bg-success">LIVE</span>{% endif %}
    {% if kraken_status == 'online' %}<span class="badge badge-online">Kraken: ONLINE</span>
    {% elif kraken_status in ('cancel_only','post_only','limit_only') %}<span class="badge badge-warn">Kraken: {{ kraken_status.upper() }}</span>
    {% else %}<span class="badge badge-error">Kraken: {{ kraken_status.upper() }}</span>{% endif %}
    {% set btc_rsi_init = init_data.btc.get('rsi') %}
    {% if btc_rsi_init %}<span id="hdr-btc-rsi" class="badge {% if btc_rsi_init < 65 %}bg-secondary{% else %}badge-warn{% endif %} d-none d-sm-inline">BTC RSI: {{ "%.1f"|format(btc_rsi_init) }}</span>{% endif %}
    {% if fear_greed %}<span class="badge {% if fear_greed.value <= 20 or fear_greed.value >= 80 %}badge-warn{% elif fear_greed.value <= 40 %}bg-secondary{% else %}badge-online{% endif %} d-none d-sm-inline">F&G: {{ fear_greed.value }} {{ fear_greed.label }}</span>{% endif %}
    {% if init_data.circuit_breaker %}<span class="badge badge-error">CIRCUIT BREAKER</span>{% endif %}
    <span class="ms-auto d-flex align-items-center gap-2" style="font-size:.75rem;color:#aaa;">
      <span class="sse-dot" id="sse-dot"></span>
      <span id="last-update">--:--:--</span>
    </span>
  </div>

  <!-- Control panel -->
  <div class="card mb-3">
    <div class="card-body py-2 px-3">
      <div class="d-flex flex-wrap align-items-center gap-2">
        <span style="font-size:.78rem;color:#aaa;" class="d-none d-sm-inline">Profilo:</span>
        {% set pc = {'conservative':'btn-success','moderate':'btn-warning','aggressive':'btn-danger'} %}
        {% for pname in ['conservative','moderate','aggressive'] %}
        <form method="post" action="/action/profile" class="mb-0">
          <input type="hidden" name="profile" value="{{ pname }}">
          <button type="submit" class="btn btn-sm {% if active_profile == pname %}{{ pc[pname] }}{% else %}btn-outline-secondary{% endif %}" style="font-size:.78rem;">{{ pname|capitalize }}</button>
        </form>
        {% endfor %}
        <div class="vr d-none d-sm-block" style="height:24px;"></div>
        <form method="post" action="/action/scan" class="mb-0">
          <button type="submit" class="btn btn-sm btn-outline-light" style="font-size:.78rem;">Scan ora</button>
        </form>
        <form method="post" action="/action/discover" class="mb-0">
          <button type="submit" class="btn btn-sm btn-outline-info d-none d-sm-inline" style="font-size:.78rem;">Discovery ora</button>
        </form>
        <button type="button" class="btn btn-sm btn-outline-secondary d-none d-md-inline" style="font-size:.78rem;" onclick="loadBalance()">Saldo Kraken</button>
        <span id="balance-result" style="font-size:.82rem;color:#4caf82;"></span>
        {% if msg %}
        <span class="badge {% if 'error' in msg or 'unknown' in msg or 'no_pair' in msg %}bg-danger{% else %}bg-success{% endif %} ms-1" style="font-size:.76rem;">{{ msg_label }}</span>
        {% endif %}
        <div class="vr d-none d-md-block" style="height:24px;"></div>
        <button class="btn btn-sm btn-outline-danger d-none d-md-inline" style="font-size:.78rem;" data-bs-toggle="modal" data-bs-target="#restartModal">Riavvia Bot</button>
      </div>
    </div>
  </div>

  <!-- Tabs -->
  <ul class="nav nav-tabs" id="mainTabs">
    <li class="nav-item"><button class="nav-link active" data-bs-toggle="tab" data-bs-target="#tab-live">Live</button></li>
    <li class="nav-item"><button class="nav-link" data-bs-toggle="tab" data-bs-target="#tab-storico">Storico</button></li>
    <li class="nav-item"><button class="nav-link" data-bs-toggle="tab" data-bs-target="#tab-ai">
      AI <span id="ai-badge" class="badge bg-secondary ms-1" style="font-size:.65rem;">{{ groq_js|length }}</span>
    </button></li>
    <li class="nav-item"><button class="nav-link" data-bs-toggle="tab" data-bs-target="#tab-mercato">Mercato</button></li>
    <li class="nav-item"><button class="nav-link" data-bs-toggle="tab" data-bs-target="#tab-discovery">Discovery</button></li>
    <li class="nav-item"><button class="nav-link" data-bs-toggle="tab" data-bs-target="#tab-config">Config</button></li>
  </ul>

  <div class="tab-content" id="mainTabContent">

    <!-- ═══ TAB LIVE ═══ -->
    <div class="tab-pane fade show active" id="tab-live">

      <!-- Metric cards -->
      <div class="row g-2 mb-3">
        <div class="col-6 col-md-2"><div class="card text-center p-2">
          <div class="metric-label">Posizioni</div>
          <div class="metric-val" id="m-pos-count">{{ init_data.metrics.open_count }} / {{ init_data.metrics.max_trades }}</div>
        </div></div>
        <div class="col-6 col-md-2"><div class="card text-center p-2">
          <div class="metric-label">Trade chiusi</div>
          <div class="metric-val" id="m-closed">{{ init_data.metrics.total_trades }}</div>
        </div></div>
        <div class="col-6 col-md-2"><div class="card text-center p-2">
          <div class="metric-label">Win rate</div>
          <div class="metric-val" id="m-winrate">{{ "%.0f"|format(init_data.metrics.winrate) }}%</div>
        </div></div>
        <div class="col-6 col-md-2"><div class="card text-center p-2">
          <div class="metric-label">P&L netto</div>
          {% set pnl0 = init_data.metrics.total_pnl %}
          <div class="metric-val {% if pnl0 >= 0 %}pnl-pos{% else %}pnl-neg{% endif %}" id="m-pnl">{{ "%+.2f"|format(pnl0) }}&euro;</div>
        </div></div>
        <div class="col-6 col-md-2"><div class="card text-center p-2">
          <div class="metric-label">Fee pagate</div>
          <div class="metric-val pnl-neg" id="m-fees">-{{ "%.4f"|format(init_data.metrics.total_fees) }}&euro;</div>
        </div></div>
        <div class="col-6 col-md-2"><div class="card text-center p-2">
          <div class="metric-label">Groq &#x2705; / &#x274c;</div>
          <div class="metric-val" id="m-groq">{{ init_data.metrics.groq_approved }} / {{ init_data.metrics.groq_rejected }}</div>
        </div></div>
      </div>

      <!-- BTC status -->
      {% set btc = init_data.btc %}
      <div class="card mb-3">
        <div class="card-header d-flex justify-content-between align-items-center">
          <span>Stato Mercato &mdash; BTC</span>
          {% if btc.get('ema50') == 'SOTTO' and btc_ema_filter %}
            <span class="badge badge-error" id="btc-badge">BOT IN ATTESA &mdash; BTC sotto EMA50</span>
          {% elif btc.get('rsi') and btc.rsi > 65 %}
            <span class="badge badge-warn" id="btc-badge">BTC IPERCOMPRATO &mdash; long bloccati</span>
          {% elif btc.get('rsi') %}
            <span class="badge badge-online" id="btc-badge">MERCATO FAVOREVOLE</span>
          {% else %}
            <span class="badge bg-secondary" id="btc-badge">In attesa dati...</span>
          {% endif %}
        </div>
        <div class="card-body">
          {% if btc.get('rsi') %}
          <div class="row g-2 mb-3">
            <div class="col-4">
              <div class="btc-card" style="border-color:{% if btc.rsi < 30 %}#f0a500{% elif btc.rsi > 70 %}#e05c5c{% else %}#2a2d3e{% endif %};">
                <div class="lbl">BTC RSI (1h)</div>
                <div class="val {% if btc.rsi < 30 %}text-warning{% elif btc.rsi > 70 %}pnl-neg{% else %}text-white{% endif %}" id="btc-rsi">{{ "%.1f"|format(btc.rsi) }}</div>
                <div class="sub">{% if btc.rsi < 30 %}Ipervenduto{% elif btc.rsi > 70 %}Ipercomprato{% elif btc.rsi > 60 %}Zona calda{% else %}Neutro{% endif %}</div>
              </div>
            </div>
            <div class="col-4">
              <div class="btc-card" style="border-color:{% if btc.get('ema50') == 'SOPRA' %}#1a7a4a{% else %}#7a1a1a{% endif %};">
                <div class="lbl">BTC / EMA50</div>
                <div class="val {% if btc.get('ema50') == 'SOPRA' %}pnl-pos{% else %}pnl-neg{% endif %}" id="btc-ema">{{ btc.get('ema50','—') }}</div>
                <div class="sub">{% if btc.get('ema50') == 'SOPRA' %}Rialzista{% else %}Ribassista{% endif %}</div>
              </div>
            </div>
            <div class="col-4">
              <div class="btc-card" style="border-color:{% if btc.get('adx',0) > 50 %}#e05c5c{% elif btc.get('adx',0) > 30 %}#f0a500{% else %}#2a2d3e{% endif %};">
                <div class="lbl">BTC ADX</div>
                <div class="val {% if btc.get('adx',0) > 50 %}pnl-neg{% elif btc.get('adx',0) > 30 %}text-warning{% else %}text-white{% endif %}" id="btc-adx">{{ "%.1f"|format(btc.get('adx',0)) }}</div>
                <div class="sub">{% if btc.get('adx',0) > 50 %}Trend forte{% elif btc.get('adx',0) > 30 %}Trend mod.{% elif btc.get('adx',0) > 20 %}Trend debole{% else %}Laterale{% endif %}</div>
              </div>
            </div>
          </div>
          <div class="p-2" style="background:#0d1520;border-radius:6px;font-size:.83rem;color:#d0d0d0;" id="btc-summary">
            {% if btc.get('ema50') == 'SOTTO' and btc_ema_filter %}
              <strong style="color:#e05c5c;">Il bot non sta tradando</strong> &mdash; BTC sotto EMA50 (trend ribassista). Riprende quando BTC risale sopra EMA50.
            {% elif btc.get('rsi',0) > 65 %}
              <strong style="color:#f0a500;">BTC ipercomprato</strong> &mdash; RSI &gt; 65: long bloccati per evitare ingressi a prezzi di picco.
            {% else %}
              <strong style="color:#4caf82;">Il bot &egrave; operativo</strong> &mdash; Condizioni favorevoli. Scansione ogni {{ scan_interval }} minuti.
            {% endif %}
          </div>
          {% else %}
          <p class="text-muted mb-0">Dati BTC non ancora disponibili &mdash; attendi il primo scan (max {{ scan_interval }} min).</p>
          {% endif %}
        </div>
      </div>

      <!-- Posizioni aperte -->
      <div class="card mb-3">
        <div class="card-header">Posizioni aperte</div>
        <div class="table-responsive">
          <table class="table table-dark table-hover" id="pos-table">
            <thead><tr>
              <th>Coppia</th><th>Tipo</th>
              <th class="col-hide-sm">Entry</th><th class="col-hide-sm">Prezzo</th>
              <th>P&amp;L %</th><th>P&amp;L &euro;</th>
              <th class="col-hide-sm">TP</th><th class="col-hide-sm">SL</th>
              <th class="col-hide-sm">Trailing</th><th class="col-hide-sm">Size</th>
              <th class="col-hide-sm">Aperta</th><th></th>
            </tr></thead>
            <tbody id="pos-tbody">
              {% for p in init_data.positions %}
              <tr id="pos-row-{{ p.pair }}">
                <td><strong>{{ p.pair }}</strong></td>
                <td><span class="{{ 'side-short' if p.side == 'short' else 'side-long' }}">{{ p.side|upper }}</span></td>
                <td class="col-hide-sm">{{ "%.6g"|format(p.entry) }}</td>
                <td class="col-hide-sm" id="price-{{ p.pair }}">{{ "%.6g"|format(p.price) if p.price else '—' }}</td>
                <td class="{{ 'pnl-pos' if p.pnl_pct >= 0 else 'pnl-neg' }}" id="pnlpct-{{ p.pair }}">{{ "%+.2f"|format(p.pnl_pct) }}%</td>
                <td class="{{ 'pnl-pos' if p.pnl_eur >= 0 else 'pnl-neg' }}" id="pnleur-{{ p.pair }}">{{ "%+.2f"|format(p.pnl_eur) }}&euro;</td>
                <td class="col-hide-sm pnl-pos">{{ "%.6g"|format(p.tp) }}</td>
                <td class="col-hide-sm pnl-neg">{{ "%.6g"|format(p.sl) }}</td>
                <td class="col-hide-sm text-warning">{{ "%.6g"|format(p.trailing) if p.trailing else '—' }}</td>
                <td class="col-hide-sm">{{ "%.2f"|format(p.size_eur) }}&euro;</td>
                <td class="col-hide-sm">{{ p.opened_at }}</td>
                <td><button class="btn btn-sm btn-outline-danger" style="font-size:.75rem;" onclick="closePos('{{ p.pair }}')">Chiudi</button></td>
              </tr>
              {% else %}
              <tr><td colspan="12" class="text-muted text-center p-3">Nessuna posizione aperta.</td></tr>
              {% endfor %}
            </tbody>
          </table>
        </div>
      </div>

    </div><!-- /tab-live -->


    <!-- ═══ TAB STORICO ═══ -->
    <div class="tab-pane fade" id="tab-storico">

      {% if pnl_chart_labels %}
      <div class="row g-2 mb-3">
        <div class="col-12 col-md-8">
          <div class="card p-3">
            <div class="card-header">P&amp;L cumulativo</div>
            <div class="chart-container"><canvas id="pnlChart"></canvas></div>
          </div>
        </div>
        <div class="col-12 col-md-4">
          <div class="card p-3">
            <div class="card-header">Esiti trade</div>
            <div class="chart-container"><canvas id="outcomeChart"></canvas></div>
          </div>
        </div>
      </div>
      {% endif %}

      <div class="card mb-3">
        <div class="card-header d-flex justify-content-between align-items-center">
          <span>Trade chiusi</span>
          <span id="trades-count-label" style="font-size:.78rem;color:#aaa;"></span>
        </div>
        <div class="table-responsive">
          <table class="table table-dark table-hover">
            <thead><tr>
              <th>Data</th><th>Coppia</th>
              <th class="col-hide-sm">Tipo</th><th>Esito</th>
              <th class="col-hide-sm">Entry</th><th class="col-hide-sm">Exit</th>
              <th class="col-hide-sm">P&amp;L lordo</th><th class="col-hide-sm">Fee</th>
              <th>P&amp;L netto</th><th>%</th>
            </tr></thead>
            <tbody id="trades-tbody"></tbody>
          </table>
        </div>
        <div class="pagination-bar">
          <div class="page-info">
            <button class="btn btn-sm btn-outline-secondary" id="trades-prev">&#x2039; Prec</button>
            <span id="trades-page-info" style="min-width:70px;text-align:center;"></span>
            <button class="btn btn-sm btn-outline-secondary" id="trades-next">Succ &#x203a;</button>
          </div>
          <span id="trades-total-info" style="font-size:.78rem;color:#aaa;"></span>
        </div>
      </div>

    </div><!-- /tab-storico -->


    <!-- ═══ TAB AI ═══ -->
    <div class="tab-pane fade" id="tab-ai">
      <div class="row g-2">
        <div class="col-12 col-md-8 col-lg-9">
          <div class="card">
            <div class="card-header d-flex justify-content-between align-items-center">
              <span>Ragionamenti Groq AI</span>
              <span id="groq-count-label" style="font-size:.78rem;color:#aaa;"></span>
            </div>
            <div class="card-body" id="groq-cards-container" style="padding-bottom:0;"></div>
            <div class="pagination-bar">
              <div class="page-info">
                <button class="btn btn-sm btn-outline-secondary" id="groq-prev">&#x2039; Prec</button>
                <span id="groq-page-info" style="min-width:70px;text-align:center;"></span>
                <button class="btn btn-sm btn-outline-secondary" id="groq-next">Succ &#x203a;</button>
              </div>
              <span id="groq-total-info" style="font-size:.78rem;color:#aaa;"></span>
            </div>
          </div>
        </div>
        <div class="col-12 col-md-4 col-lg-3">
          <div class="card p-3 mb-2">
            <div class="card-header">Groq decisions</div>
            <div class="chart-container"><canvas id="groqChart"></canvas></div>
          </div>
          <div class="card p-3">
            <div class="card-header">Confidence distribution</div>
            <div class="chart-container"><canvas id="confChart"></canvas></div>
          </div>
        </div>
      </div>
    </div><!-- /tab-ai -->


    <!-- ═══ TAB MERCATO ═══ -->
    <div class="tab-pane fade" id="tab-mercato">
      <div class="row g-2 mb-3">

        <!-- Fear & Greed -->
        <div class="col-12 col-sm-6 col-md-4">
          <div class="card h-100">
            <div class="card-header">Fear &amp; Greed Index</div>
            {% if fear_greed %}
            <div class="card-body text-center d-flex flex-column align-items-center justify-content-center py-4">
              {% set fgv = fear_greed.value %}
              {% if fgv <= 25 %}{% set fgcol = '#e05c5c' %}
              {% elif fgv <= 45 %}{% set fgcol = '#f0a500' %}
              {% elif fgv <= 55 %}{% set fgcol = '#aaa' %}
              {% elif fgv <= 75 %}{% set fgcol = '#f0a500' %}
              {% else %}{% set fgcol = '#e05c5c' %}{% endif %}
              <div style="font-size:3.5rem;font-weight:700;color:{{ fgcol }};line-height:1;">{{ fgv }}</div>
              <div style="font-size:1rem;color:{{ fgcol }};margin-bottom:12px;">{{ fear_greed.label }}</div>
              <div style="width:100%;height:8px;border-radius:4px;background:linear-gradient(to right,#e05c5c,#f0a500,#4caf82);position:relative;margin-bottom:8px;">
                <div style="position:absolute;left:{{ fgv }}%;top:-3px;width:2px;height:14px;background:#fff;border-radius:2px;transform:translateX(-50%);"></div>
              </div>
              <div class="d-flex justify-content-between w-100" style="font-size:.68rem;color:#888;"><span>Paura</span><span>Neutro</span><span>Aviditagrave;</span></div>
              <div class="mt-3" style="font-size:.8rem;color:#aaa;">
                Long: <span class="{{ 'pnl-pos' if fgv >= fg_min else 'pnl-neg' }}">{{ 'OK' if fgv >= fg_min else 'BLOCCATO' }}</span>
                &nbsp;&middot;&nbsp;
                Short: <span class="{{ 'pnl-pos' if fgv <= fg_max else 'pnl-neg' }}">{{ 'OK' if fgv <= fg_max else 'BLOCCATO' }}</span>
              </div>
            </div>
            {% else %}
            <div class="card-body text-center text-muted py-4">Dati non disponibili</div>
            {% endif %}
          </div>
        </div>

        <!-- News -->
        {% if news_headlines %}
        <div class="col-12 col-sm-6 col-md-5">
          <div class="card h-100">
            <div class="card-header">Ultime notizie crypto</div>
            <div class="card-body p-3">
              {% for n in news_headlines %}
              <div class="news-item">{{ n.title }}</div>
              {% endfor %}
            </div>
          </div>
        </div>
        {% endif %}

        <!-- Mempool -->
        {% if mempool %}
        <div class="col-12 col-md-3">
          <div class="card h-100">
            <div class="card-header">BTC On-chain / Mempool</div>
            <div class="card-body">
              <div class="row g-3" style="font-size:.85rem;color:#d0d0d0;">
                <div class="col-6"><div style="color:#aaa;font-size:.75rem;">Tx in attesa</div><div style="font-size:1.1rem;font-weight:600;">{{ mempool.tx_count }}</div></div>
                <div class="col-6"><div style="color:#aaa;font-size:.75rem;">Dimensione</div><div style="font-size:1.1rem;font-weight:600;">{{ mempool.size_mb }} MB</div></div>
                <div class="col-6"><div style="color:#aaa;font-size:.75rem;">Fee rapida</div><div style="font-size:1.1rem;font-weight:600;">{{ mempool.fee_fastest }} sat/vB</div></div>
                <div class="col-6"><div style="color:#aaa;font-size:.75rem;">Fee 1h</div><div style="font-size:1.1rem;font-weight:600;">{{ mempool.fee_hour }} sat/vB</div></div>
              </div>
              <div class="mt-3" style="font-size:.72rem;color:#888;border-top:1px solid #2a2d3e;padding-top:8px;">Fee elevate = alta domanda &rarr; segnale bullish</div>
            </div>
          </div>
        </div>
        {% endif %}

      </div>
    </div><!-- /tab-mercato -->


    <!-- ═══ TAB DISCOVERY ═══ -->
    <div class="tab-pane fade" id="tab-discovery">

      {% if discovery %}
      <div class="card mb-3">
        <div class="card-header d-flex justify-content-between align-items-center flex-wrap gap-1">
          <span>Ultima discovery</span>
          <span style="font-size:.78rem;color:#aaa;">{{ fmt_time(discovery.timestamp) }} &middot; {{ discovery.pairs_scanned }} coppie analizzate</span>
        </div>
        {% if discovery.groq_analysis %}
        <div class="card-body">
          <div class="p-3" style="background:#0d1520;border-radius:6px;font-size:.83rem;color:#d0d0d0;line-height:1.6;">
            <strong style="color:#4caf82;">Analisi Groq AI:</strong> {{ discovery.groq_analysis }}
          </div>
        </div>
        {% endif %}
      </div>
      {% endif %}

      <div class="card mb-3">
        <div class="card-header d-flex justify-content-between align-items-center">
          <span>Coppie candidate</span>
          <span id="disc-count-label" style="font-size:.78rem;color:#aaa;"></span>
        </div>
        <div class="table-responsive">
          <table class="table table-dark table-hover">
            <thead><tr>
              <th>Coppia</th><th>Score</th>
              <th class="col-hide-sm">Train WR</th><th>Test WR</th>
              <th class="col-hide-sm">Consist.</th><th>Sharpe</th>
              <th class="col-hide-sm">P&amp;L medio</th>
              <th class="col-hide-sm">RSI ora</th><th class="col-hide-sm">ADX ora</th>
            </tr></thead>
            <tbody id="disc-tbody"></tbody>
          </table>
        </div>
        <div class="pagination-bar">
          <div class="page-info">
            <button class="btn btn-sm btn-outline-secondary" id="disc-prev">&#x2039; Prec</button>
            <span id="disc-page-info" style="min-width:70px;text-align:center;"></span>
            <button class="btn btn-sm btn-outline-secondary" id="disc-next">Succ &#x203a;</button>
          </div>
          <span id="disc-total-info" style="font-size:.78rem;color:#aaa;"></span>
        </div>
      </div>

      {% if dynamic_pairs %}
      <div class="card mb-3">
        <div class="card-header">Coppie dinamiche attive</div>
        <div class="card-body" style="font-size:.84rem;color:#d0d0d0;">
          {% for dp in dynamic_pairs %}
          <span class="badge bg-success me-1">{{ dp }}</span>
          {% endfor %}
        </div>
      </div>
      {% endif %}

    </div><!-- /tab-discovery -->


    <!-- ═══ TAB CONFIG ═══ -->
    <div class="tab-pane fade" id="tab-config">
      <div class="row g-3">

        <!-- Profilo attivo -->
        <div class="col-12 col-md-4">
          <div class="card h-100">
            <div class="card-header">
              Profilo attivo
              {% set pc2 = {'conservative':'bg-success','moderate':'bg-warning text-dark','aggressive':'bg-danger'} %}
              <span class="badge {{ pc2.get(active_profile, 'bg-secondary') }} ms-2" style="font-size:.72rem;">{{ active_profile|upper }}</span>
            </div>
            <div class="card-body">
              <div class="cfg-row mb-2">
                <span><span class="cfg-key">Score min:</span> <span class="cfg-val">{{ min_score }}/6</span></span>
                <span><span class="cfg-key">ADX min:</span> <span class="cfg-val">{{ adx_min }}</span></span>
                <span><span class="cfg-key">Volume:</span> <span class="cfg-val">&ge;{{ vol_mult }}&times;</span></span>
              </div>
              <div class="cfg-row mb-2">
                <span><span class="cfg-key">RSI 1h:</span> <span class="cfg-val">&lt;{{ rsi_1h_max }}</span></span>
                <span><span class="cfg-key">RSI 15m:</span> <span class="cfg-val">&lt;{{ rsi_15m_max }}</span></span>
                <span><span class="cfg-key">BTC RSI max:</span> <span class="cfg-val">{{ btc_rsi_max }}</span></span>
              </div>
              <div class="cfg-row mb-2">
                <span><span class="cfg-key">Groq conf:</span> <span class="cfg-val">{{ groq_conf }}/10</span></span>
                <span><span class="cfg-key">TP ATR:</span> <span class="cfg-val">{{ atr_tp }}&times;</span></span>
                <span><span class="cfg-key">SL ATR:</span> <span class="cfg-val">{{ atr_sl }}&times;</span></span>
              </div>
              <hr style="border-color:#2a2d3e;">
              <div class="cfg-row mb-2">
                <span><span class="cfg-key">Max pos:</span> <span class="cfg-val">{{ max_trades }}</span></span>
                <span><span class="cfg-key">Cap max:</span> <span class="cfg-val">{{ capital_pct }}%</span></span>
              </div>
              <div class="cfg-row mb-2">
                <span><span class="cfg-key">Size:</span> <span class="cfg-val">{{ size_high }}% / {{ size_med }}% / {{ size_min }}%</span></span>
              </div>
              <div class="cfg-row">
                <span><span class="cfg-key">Circuit breaker:</span> <span class="cfg-val">{{ cb_eur }}&euro;/giorno</span></span>
              </div>
            </div>
          </div>
        </div>

        <!-- Parametri di sistema -->
        <div class="col-12 col-md-4">
          <div class="card h-100">
            <div class="card-header">Parametri di sistema</div>
            <div class="card-body">
              <div class="cfg-row mb-2">
                <span><span class="cfg-key">Coppie:</span> <span class="cfg-val">{{ pairs }}</span></span>
              </div>
              <div class="cfg-row mb-2">
                <span><span class="cfg-key">Scan:</span> <span class="cfg-val">{{ scan_interval }} min</span></span>
              </div>
              <hr style="border-color:#2a2d3e;">
              <div class="cfg-row mb-2">
                <span><span class="cfg-key">Fee taker:</span> <span class="cfg-val">{{ fee_taker }}%</span></span>
                <span><span class="cfg-key">Fee maker:</span> <span class="cfg-val">{{ fee_maker }}%</span></span>
              </div>
              <div class="cfg-row mb-2">
                <span><span class="cfg-key">Leverage:</span> <span class="cfg-val">{{ leverage }}&times;</span></span>
                <span><span class="cfg-key">Groq model:</span> <span class="cfg-val">{{ groq_model }}</span></span>
              </div>
              <div class="cfg-row">
                <span><span class="cfg-key">Kelly:</span> <span class="cfg-val">{{ kelly_fraction }} fraz. &middot; min {{ kelly_min_trades }} trade</span></span>
              </div>
            </div>
          </div>
        </div>

        <!-- Filtri attivi -->
        <div class="col-12 col-md-4">
          <div class="card h-100">
            <div class="card-header">Filtri attivi</div>
            <div class="card-body" style="font-size:.82rem;">
              <div class="d-flex flex-column gap-2">
                <div class="d-flex justify-content-between"><span style="color:#aaa;">Daily EMA50 filter</span><span class="badge {{ 'badge-online' if daily_ema_filter else 'bg-secondary' }}">{{ 'ON' if daily_ema_filter else 'OFF' }}</span></div>
                <div class="d-flex justify-content-between"><span style="color:#aaa;">BTC correlation filter</span><span class="badge {{ 'badge-online' if btc_filter else 'bg-secondary' }}">{{ 'ON' if btc_filter else 'OFF' }}</span></div>
                <div class="d-flex justify-content-between"><span style="color:#aaa;">Fear &amp; Greed gate</span><span class="badge {{ 'badge-online' if fg_enabled else 'bg-secondary' }}">{{ ('ON (%d–%d)'|format(fg_min, fg_max)) if fg_enabled else 'OFF' }}</span></div>
                <div class="d-flex justify-content-between"><span style="color:#aaa;">Trailing stop</span><span class="badge {{ 'badge-online' if trailing_enabled else 'bg-secondary' }}">{{ 'ON' if trailing_enabled else 'OFF' }}</span></div>
                <div class="d-flex justify-content-between"><span style="color:#aaa;">Circuit breaker</span><span class="badge {{ 'badge-online' if cb_enabled else 'bg-secondary' }}">{{ ('ON (%s€)'|format(cb_eur)) if cb_enabled else 'OFF' }}</span></div>
                <div class="d-flex justify-content-between"><span style="color:#aaa;">News sentiment</span><span class="badge {{ 'badge-online' if news_enabled else 'bg-secondary' }}">{{ 'ON' if news_enabled else 'OFF' }}</span></div>
                <div class="d-flex justify-content-between"><span style="color:#aaa;">On-chain data</span><span class="badge {{ 'badge-online' if onchain_enabled else 'bg-secondary' }}">{{ 'ON' if onchain_enabled else 'OFF' }}</span></div>
                <div class="d-flex justify-content-between"><span style="color:#aaa;">Kelly criterion</span><span class="badge {{ 'badge-online' if kelly_enabled else 'bg-secondary' }}">{{ 'ON' if kelly_enabled else 'OFF' }}</span></div>
              </div>
            </div>
          </div>
        </div>

        <!-- Editor profili -->
        <div class="col-12">
          <div class="card">
            <div class="card-header d-flex align-items-center justify-content-between flex-wrap gap-2">
              <span>Editor profili</span>
              <div class="d-flex gap-1" id="profile-tabs">
                <button class="btn btn-sm btn-success profile-tab-btn active" data-profile="conservative" onclick="switchProfile('conservative',this)" style="font-size:.78rem;">Conservative</button>
                <button class="btn btn-sm btn-outline-secondary profile-tab-btn" data-profile="moderate" onclick="switchProfile('moderate',this)" style="font-size:.78rem;">Moderate</button>
                <button class="btn btn-sm btn-outline-secondary profile-tab-btn" data-profile="aggressive" onclick="switchProfile('aggressive',this)" style="font-size:.78rem;">Aggressive</button>
                <button class="btn btn-sm btn-outline-info profile-tab-btn" data-profile="custom" onclick="switchProfile('custom',this)" style="font-size:.78rem;">Custom &#x270f;</button>
              </div>
            </div>
            <div class="card-body">
              <div class="mb-3 p-2" style="background:#0d1520;border-radius:6px;font-size:.8rem;color:#aaa;">
                Stai modificando il profilo <strong id="editing-profile-name" style="color:#e0e0e0;">Conservative</strong>.
                <span id="custom-notice" style="display:none;color:#4caf82;"> Il profilo Custom &egrave; salvato nel DB e persiste tra le sessioni.</span>
              </div>

              <form id="params-form" onsubmit="applyParams(event)" novalidate>
                <input type="hidden" name="profile" id="params-profile-field" value="{{ active_profile }}">
                <div class="row g-4">

                  <!-- Col 1: Filtri segnale -->
                  <div class="col-12 col-md-4">
                    <div class="param-group-title">Filtri segnale</div>
                    <div class="param-row"><div class="param-label">Score minimo<small>punti su 6</small></div><div class="d-flex align-items-center gap-1"><span class="param-range">1&ndash;6</span><input class="param-input" type="number" name="MIN_SIGNAL_SCORE" min="1" max="6" step="1" required></div></div>
                    <div class="param-row"><div class="param-label">ADX minimo<small>forza trend</small></div><div class="d-flex align-items-center gap-1"><span class="param-range">10&ndash;40</span><input class="param-input" type="number" name="ADX_MIN" min="10" max="40" step="1" required></div></div>
                    <div class="param-row"><div class="param-label">Volume spike<small>min vs media</small></div><div class="d-flex align-items-center gap-1"><span class="param-range">1.0&ndash;2.0</span><input class="param-input" type="number" name="VOLUME_SPIKE_MULT" min="1.0" max="2.0" step="0.05" required></div></div>
                    <div class="param-row"><div class="param-label">RSI 1h max (long)</div><div class="d-flex align-items-center gap-1"><span class="param-range">40&ndash;80</span><input class="param-input" type="number" name="RSI_1H_BUY_MAX" min="40" max="80" step="1" required></div></div>
                    <div class="param-row"><div class="param-label">RSI 15m max (long)</div><div class="d-flex align-items-center gap-1"><span class="param-range">40&ndash;80</span><input class="param-input" type="number" name="RSI_15M_BUY_MAX" min="40" max="80" step="1" required></div></div>
                    <div class="param-row"><div class="param-label">BTC RSI max<small>blocca long se BTC &gt; soglia</small></div><div class="d-flex align-items-center gap-1"><span class="param-range">50&ndash;80</span><input class="param-input" type="number" name="BTC_RSI_MAX" min="50" max="80" step="1" required></div></div>
                    <div class="param-row"><div class="param-label">Groq confidence min<small>score AI minimo</small></div><div class="d-flex align-items-center gap-1"><span class="param-range">1&ndash;10</span><input class="param-input" type="number" name="GROQ_MIN_CONFIDENCE" min="1" max="10" step="1" required></div></div>
                  </div>

                  <!-- Col 2: TP/SL/Trailing -->
                  <div class="col-12 col-md-4">
                    <div class="param-group-title">TP / SL / Trailing</div>
                    <div class="param-row"><div class="param-label">ATR TP mult (long)<small>TP = entry + N&times;ATR</small></div><div class="d-flex align-items-center gap-1"><span class="param-range">1.0&ndash;6.0</span><input class="param-input" type="number" name="ATR_TP_MULT" min="1.0" max="6.0" step="0.1" required></div></div>
                    <div class="param-row"><div class="param-label">ATR SL mult (long)</div><div class="d-flex align-items-center gap-1"><span class="param-range">0.5&ndash;3.0</span><input class="param-input" type="number" name="ATR_SL_MULT" min="0.5" max="3.0" step="0.1" required></div></div>
                    <div class="param-row"><div class="param-label">ATR TP mult (short)</div><div class="d-flex align-items-center gap-1"><span class="param-range">1.0&ndash;6.0</span><input class="param-input" type="number" name="ATR_TP_MULT_SHORT" min="1.0" max="6.0" step="0.1" required></div></div>
                    <div class="param-row"><div class="param-label">ATR SL mult (short)</div><div class="d-flex align-items-center gap-1"><span class="param-range">0.5&ndash;4.0</span><input class="param-input" type="number" name="ATR_SL_MULT_SHORT" min="0.5" max="4.0" step="0.1" required></div></div>
                    <div class="param-row"><div class="param-label">Trailing activation (ATR)<small>profitto min</small></div><div class="d-flex align-items-center gap-1"><span class="param-range">0.3&ndash;3.0</span><input class="param-input" type="number" name="TRAILING_ACTIVATION_ATR" min="0.3" max="3.0" step="0.1" required></div></div>
                    <div class="param-row"><div class="param-label">Trailing distance (ATR)</div><div class="d-flex align-items-center gap-1"><span class="param-range">0.5&ndash;4.0</span><input class="param-input" type="number" name="TRAILING_DISTANCE_ATR" min="0.5" max="4.0" step="0.1" required></div></div>
                    <div class="param-row"><div class="param-label">Circuit breaker (&euro;/giorno)</div><div class="d-flex align-items-center gap-1"><span class="param-range">5&ndash;200</span><input class="param-input" type="number" name="CIRCUIT_BREAKER_LOSS_EUR" min="5" max="200" step="1" required></div></div>
                  </div>

                  <!-- Col 3: Sizing -->
                  <div class="col-12 col-md-4">
                    <div class="param-group-title">Sizing</div>
                    <div class="param-row"><div class="param-label">Max posizioni aperte</div><div class="d-flex align-items-center gap-1"><span class="param-range">1&ndash;10</span><input class="param-input" type="number" name="MAX_OPEN_TRADES" min="1" max="10" step="1" required></div></div>
                    <div class="param-row"><div class="param-label">Capitale max (%)<small>% del budget</small></div><div class="d-flex align-items-center gap-1"><span class="param-range">10&ndash;100</span><input class="param-input" type="number" name="MAX_CAPITAL_DEPLOYED_PCT" min="10" max="100" step="5" required></div></div>
                    <div class="param-row"><div class="param-label">Size AI HIGH (%)<small>conf 9&ndash;10</small></div><div class="d-flex align-items-center gap-1"><span class="param-range">5&ndash;50</span><input class="param-input" type="number" name="AI_SIZE_HIGH_PCT" min="5" max="50" step="1" required></div></div>
                    <div class="param-row"><div class="param-label">Size AI MED (%)<small>conf 7&ndash;8</small></div><div class="d-flex align-items-center gap-1"><span class="param-range">5&ndash;40</span><input class="param-input" type="number" name="AI_SIZE_MED_PCT" min="5" max="40" step="1" required></div></div>
                    <div class="param-row"><div class="param-label">Size AI MIN (%)<small>conf 6</small></div><div class="d-flex align-items-center gap-1"><span class="param-range">5&ndash;30</span><input class="param-input" type="number" name="AI_SIZE_MIN_PCT" min="5" max="30" step="1" required></div></div>
                    <div class="param-group-title">Sistema</div>
                    <div class="param-row"><div class="param-label">Scan interval (minuti)</div><div class="d-flex align-items-center gap-1"><span class="param-range">5&ndash;60</span><input class="param-input" type="number" name="SCAN_INTERVAL_MINUTES" min="5" max="60" step="5" required></div></div>
                    <div class="param-row"><div class="param-label">Budget (&euro;)</div><div class="d-flex align-items-center gap-1"><span class="param-range">10&ndash;9999</span><input class="param-input" type="number" name="BUDGET_EUR" min="10" max="9999" step="10" required></div></div>
                  </div>

                </div><!-- /row -->

                <div class="d-flex flex-wrap align-items-center gap-2 mt-4 pt-3" style="border-top:1px solid #2a2d3e;">
                  <button type="submit" class="btn btn-sm btn-warning" style="font-size:.82rem;">Applica profilo</button>
                  <button type="button" class="btn btn-sm btn-outline-secondary" style="font-size:.82rem;" onclick="resetCurrentProfile()">Ripristina default</button>
                  <button type="button" class="btn btn-sm btn-outline-info" style="font-size:.82rem;" onclick="openGeminiValidator()">&#x2736; Valida con Gemini</button>
                  <span id="params-saved-badge" class="badge bg-success" style="font-size:.72rem;display:none;">Salvato &#x2713;</span>
                </div>
              </form>

              <!-- Gemini validator -->
              <div id="gemini-panel" style="display:none;" class="mt-3 p-3">
                <div class="d-flex align-items-center gap-2 mb-2 flex-wrap">
                  <span style="font-size:.82rem;color:#aaa;white-space:nowrap;">Gemini API Key:</span>
                  <input id="gemini-key" type="password" placeholder="AIza..." autocomplete="off"
                    style="background:#0f1117;border:1px solid #2a2d3e;color:#e0e0e0;border-radius:4px;padding:3px 10px;font-size:.82rem;flex:1;min-width:200px;">
                  <button class="btn btn-sm btn-outline-info" style="font-size:.78rem;" onclick="validateWithGemini()">Analizza</button>
                  <button class="btn btn-sm btn-outline-secondary" style="font-size:.78rem;" onclick="document.getElementById('gemini-panel').style.display='none'">&#x2715;</button>
                </div>
                <div id="gemini-result" style="display:none;background:#0d1520;border-radius:6px;padding:12px;font-size:.84rem;line-height:1.6;color:#d0d0d0;border-left:3px solid #4285f4;">
                  <div id="gemini-spinner" style="color:#aaa;display:none;">&#x2736; Gemini sta analizzando i parametri&hellip;</div>
                  <div id="gemini-text"></div>
                </div>
              </div>

            </div>
          </div>
        </div>

        <!-- Legenda -->
        <div class="col-12">
          <div class="card">
            <div class="card-header" data-bs-toggle="collapse" data-bs-target="#legenda" style="cursor:pointer;">
              Legenda indicatori <span style="font-size:.8rem;color:#aaa;">(clicca per espandere)</span>
            </div>
            <div id="legenda" class="collapse">
              <div class="card-body" style="font-size:.83rem;line-height:1.7;color:#d0d0d0;">
                <div class="row g-3">
                  <div class="col-12 col-md-6">
                    <h6 class="text-warning">RSI &mdash; Relative Strength Index</h6>
                    <ul><li><span class="text-warning">RSI &lt; 30</span> &mdash; Ipervenduto: possibile rimbalzo</li>
                    <li><span class="pnl-neg">RSI &gt; 70</span> &mdash; Ipercomprato: rischio correzione</li>
                    <li><span class="text-muted">RSI 40&ndash;60</span> &mdash; Zona neutrale</li></ul>
                  </div>
                  <div class="col-12 col-md-6">
                    <h6 class="text-warning">ADX &mdash; Average Directional Index</h6>
                    <ul><li><span class="text-muted">ADX &lt; 20</span> &mdash; Mercato laterale</li>
                    <li><span class="text-warning">ADX 20&ndash;40</span> &mdash; Trend moderato</li>
                    <li><span class="pnl-neg">ADX &gt; 40</span> &mdash; Trend molto forte</li></ul>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>

      </div>
    </div><!-- /tab-config -->

  </div><!-- /tab-content -->
</div><!-- /container -->


<!-- Restart modal -->
<div class="modal fade" id="restartModal" tabindex="-1">
  <div class="modal-dialog modal-dialog-centered">
    <div class="modal-content" style="background:#1a1d27;border:1px solid #2a2d3e;color:#e0e0e0;">
      <div class="modal-header" style="border-bottom:1px solid #2a2d3e;">
        <h6 class="modal-title fw-bold">Riavvia il bot?</h6>
        <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button>
      </div>
      <div class="modal-body" style="font-size:.88rem;color:#d0d0d0;line-height:1.6;">
        <p>Il processo si fermer&agrave; e si riavvier&agrave; entro <strong>30 secondi</strong> tramite il loop di avvio automatico.</p>
        <p class="mb-0" style="color:#f0a500;">&#x26A0; Le posizioni aperte non vengono chiuse &mdash; TP, SL e trailing stop riprendono automaticamente al riavvio.</p>
      </div>
      <div class="modal-footer" style="border-top:1px solid #2a2d3e;">
        <button type="button" class="btn btn-sm btn-outline-secondary" data-bs-dismiss="modal">Annulla</button>
        <button type="button" class="btn btn-sm btn-danger" onclick="restartBot()">Riavvia</button>
      </div>
    </div>
  </div>
</div>


<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<script>

// ── Dati iniettati dal server ──────────────────────────────────────────────────
const TRADES_DATA  = {{ trades_js  | tojson }};
const GROQ_DATA    = {{ groq_js    | tojson }};
const DISC_DATA    = {{ disc_js    | tojson }};
const INIT_DATA    = {{ init_data  | tojson }};
const PROFILES_JS  = {{ profiles_js | tojson }};

{% if pnl_chart_labels %}
const PNL_LABELS   = {{ pnl_chart_labels | tojson }};
const PNL_DATA     = {{ pnl_chart_data   | tojson }};
const OUTCOME_DATA = {{ outcome_data     | tojson }};
const CONF_DATA    = {{ conf_distribution | tojson }};
{% else %}
const PNL_LABELS = null;
{% endif %}


// ── Utilities ─────────────────────────────────────────────────────────────────
function setEl(id, txt) { var e = document.getElementById(id); if (e) e.textContent = txt; }
function fmtSign(n, d) { d = d||2; return (n>=0?'+':'')+n.toFixed(d); }


// ── Classe Paginator (tabella generica) ───────────────────────────────────────
class Paginator {
  constructor(opts) {
    this.data       = opts.data;
    this.perPage    = opts.perPage;
    this.renderRow  = opts.renderRow;
    this.tbody      = document.getElementById(opts.tbodyId);
    this.prevBtn    = document.getElementById(opts.prevId);
    this.nextBtn    = document.getElementById(opts.nextId);
    this.pageInfoEl = document.getElementById(opts.pageInfoId);
    this.countLbl   = document.getElementById(opts.countLabelId);
    this.totalInfo  = document.getElementById(opts.totalInfoId);
    if (!this.tbody || !this.prevBtn || !this.nextBtn || !this.pageInfoEl) {
      console.error('Paginator: elementi non trovati', opts.tbodyId, !!this.tbody, opts.prevId, !!this.prevBtn);
      return;
    }
    this.page  = 0;
    this.pages = Math.max(1, Math.ceil(this.data.length / this.perPage));
    this.prevBtn.addEventListener('click', () => { if (this.page > 0) { this.page--; this.render(); } });
    this.nextBtn.addEventListener('click', () => { if (this.page < this.pages-1) { this.page++; this.render(); } });
    this.render();
  }
  render() {
    if (!this.tbody) return;
    var start = this.page * this.perPage;
    var slice = this.data.slice(start, start + this.perPage);
    try {
      this.tbody.innerHTML = slice.map(this.renderRow).join('');
    } catch(err) {
      this.tbody.innerHTML = '<tr><td colspan="12" style="color:#e05c5c;padding:12px">Errore render: ' + err.message + '</td></tr>';
      console.error('Paginator render error:', err);
      return;
    }
    if (this.pageInfoEl) this.pageInfoEl.textContent = 'Pag. ' + (this.page+1) + ' / ' + this.pages;
    if (this.totalInfo) this.totalInfo.textContent  = this.data.length + ' elementi totali';
    if (this.countLbl) this.countLbl.textContent = '(' + (start+1) + '-' + Math.min(start+this.perPage, this.data.length) + ' di ' + this.data.length + ')';
    this.prevBtn.disabled = this.page === 0;
    this.nextBtn.disabled = this.page === this.pages - 1;
  }
}


// ── Groq Paginator (card layout) ──────────────────────────────────────────────
class GroqPaginator {
  constructor(data, perPage) {
    this.data       = data;
    this.perPage    = perPage;
    this.page       = 0;
    this.pages      = Math.max(1, Math.ceil(data.length / perPage));
    this.container  = document.getElementById('groq-cards-container');
    this.prevBtn    = document.getElementById('groq-prev');
    this.nextBtn    = document.getElementById('groq-next');
    this.pageInfoEl = document.getElementById('groq-page-info');
    this.countLbl   = document.getElementById('groq-count-label');
    this.totalInfo  = document.getElementById('groq-total-info');
    this.prevBtn.addEventListener('click', () => { if (this.page > 0) { this.page--; this.render(); } });
    this.nextBtn.addEventListener('click', () => { if (this.page < this.pages-1) { this.page++; this.render(); } });
    this.render();
  }
  render() {
    var start = this.page * this.perPage;
    var slice = this.data.slice(start, start + this.perPage);
    this.container.innerHTML = slice.map(function(g) {
      var cls = g.ok ? 'groq-esegui' : 'groq-rifiuta';
      var ico = g.ok ? '✅' : '❌';
      var lbl = g.ok ? 'ESEGUI' : 'RIFIUTA';
      var col = g.ok ? '#4caf82' : '#e05c5c';
      return '<div class="' + cls + '">' +
        '<div class="d-flex justify-content-between align-items-start flex-wrap gap-1">' +
          '<strong style="color:' + col + ';">' + ico + ' ' + lbl + ' [' + g.dir + '] - ' + g.pair + '</strong>' +
          '<div class="d-flex gap-2 align-items-center">' +
            '<span class="conf-badge">' + g.conf + '/10</span>' +
            '<span style="font-size:.75rem;color:#aaa;">' + g.time + ' | Score ' + g.score + '/6</span>' +
          '</div>' +
        '</div>' +
        '<div class="groq-reason">' + g.reason + '</div>' +
        '<div class="groq-stats">' +
          '<span>RSI 1h: <b>' + g.rsi1h + '</b></span>' +
          '<span>RSI 15m: <b>' + g.rsi15m + '</b></span>' +
          '<span>MACD: <b>' + g.macd + '</b></span>' +
          '<span>Vol: <b>' + g.vol + '</b></span>' +
          '<span>ADX: <b>' + g.adx + '</b></span>' +
          '<span>Prezzo: <b>' + g.price + '</b></span>' +
        '</div>' +
      '</div>';
    }).join('');
    this.pageInfoEl.textContent = 'Pag. ' + (this.page+1) + ' / ' + this.pages;
    this.totalInfo.textContent  = this.data.length + ' valutazioni totali';
    this.countLbl.textContent   = '(' + (start+1) + '–' + Math.min(start+this.perPage, this.data.length) + ' di ' + this.data.length + ')';
    this.prevBtn.disabled = this.page === 0;
    this.nextBtn.disabled = this.page === this.pages - 1;
  }
}


// ── Render functions ──────────────────────────────────────────────────────────
function renderTrade(t) {
  var pnlCls  = t.net.charAt(0) === '+' ? 'pnl-pos' : 'pnl-neg';
  var pctCls  = t.pct.charAt(0) === '+' ? 'pnl-pos' : 'pnl-neg';
  var sideCls = t.side === 'short' ? 'side-short' : 'side-long';
  var esiCls  = t.esito.indexOf('TP') >= 0 ? 'pnl-pos' : (t.esito.indexOf('SL') >= 0 ? 'pnl-neg' : 'text-warning');
  return '<tr><td>' + t.date + '</td>' +
    '<td><strong>' + t.pair + '</strong></td>' +
    '<td class="col-hide-sm"><span class="' + sideCls + '">' + t.side.toUpperCase() + '</span></td>' +
    '<td class="' + esiCls + '">' + t.esito + '</td>' +
    '<td class="col-hide-sm">' + t.entry + '</td>' +
    '<td class="col-hide-sm">' + t.exit + '</td>' +
    '<td class="col-hide-sm ' + pctCls + '">' + t.gross + '</td>' +
    '<td class="col-hide-sm pnl-neg">' + t.fee + '</td>' +
    '<td class="' + pnlCls + '">' + t.net + '</td>' +
    '<td class="' + pctCls + '">' + t.pct + '</td></tr>';
}

function renderDisc(d) {
  var pnlCls    = d.pnl.charAt(0) === '+' ? 'pnl-pos' : 'pnl-neg';
  var sharpeCls = parseFloat(d.sharpe) >= 1 ? 'pnl-pos' : (parseFloat(d.sharpe) >= 0 ? '' : 'pnl-neg');
  var testCls   = parseInt(d.testWR) >= 55 ? 'pnl-pos' : 'pnl-neg';
  return '<tr><td><strong>' + d.pair + '</strong></td>' +
    '<td><span class="badge ' + d.scoreClass + '" style="font-size:.75rem;">' + d.score + '</span></td>' +
    '<td class="col-hide-sm">' + d.trainWR + '</td>' +
    '<td class="' + testCls + '">' + d.testWR + '</td>' +
    '<td class="col-hide-sm">' + d.consist + '</td>' +
    '<td class="' + sharpeCls + '">' + d.sharpe + '</td>' +
    '<td class="col-hide-sm ' + pnlCls + '">' + d.pnl + '</td>' +
    '<td class="col-hide-sm">' + d.rsi + '</td>' +
    '<td class="col-hide-sm">' + d.adx + '</td></tr>';
}

function renderPosRow(p) {
  var sideCls = p.side === 'short' ? 'side-short' : 'side-long';
  var pctCls  = p.pnl_pct >= 0 ? 'pnl-pos' : 'pnl-neg';
  var eurCls  = p.pnl_eur >= 0 ? 'pnl-pos' : 'pnl-neg';
  var priceStr = p.price ? p.price.toFixed(6) : '—';
  var trailStr = p.trailing ? p.trailing.toFixed(6) : '—';
  return '<tr id="pos-row-' + p.pair + '">' +
    '<td><strong>' + p.pair + '</strong></td>' +
    '<td><span class="' + sideCls + '">' + p.side.toUpperCase() + '</span></td>' +
    '<td class="col-hide-sm">' + p.entry.toFixed(6) + '</td>' +
    '<td class="col-hide-sm" id="price-' + p.pair + '">' + priceStr + '</td>' +
    '<td class="' + pctCls + '" id="pnlpct-' + p.pair + '">' + fmtSign(p.pnl_pct) + '%</td>' +
    '<td class="' + eurCls + '" id="pnleur-' + p.pair + '">' + fmtSign(p.pnl_eur) + '€</td>' +
    '<td class="col-hide-sm pnl-pos">' + p.tp.toFixed(6) + '</td>' +
    '<td class="col-hide-sm pnl-neg">' + p.sl.toFixed(6) + '</td>' +
    '<td class="col-hide-sm text-warning">' + trailStr + '</td>' +
    '<td class="col-hide-sm">' + p.size_eur.toFixed(2) + '€</td>' +
    '<td class="col-hide-sm">' + p.opened_at + '</td>' +
    '<td><button class="btn btn-sm btn-outline-danger" style="font-size:.75rem;" onclick="closePos(\\'' + p.pair + '\\')">Chiudi</button></td>' +
    '</tr>';
}


// ── SSE live updates ───────────────────────────────────────────────────────────
function updatePositions(positions) {
  var tbody = document.getElementById('pos-tbody');
  if (!tbody) return;
  if (!positions || !positions.length) {
    tbody.innerHTML = '<tr><td colspan="12" class="text-muted text-center p-3">Nessuna posizione aperta.</td></tr>';
    return;
  }
  tbody.innerHTML = positions.map(renderPosRow).join('');
}

function updateMetrics(m) {
  setEl('m-pos-count', m.open_count + ' / ' + m.max_trades);
  setEl('m-closed', m.total_trades);
  setEl('m-winrate', m.winrate.toFixed(0) + '%');
  var pnlEl = document.getElementById('m-pnl');
  if (pnlEl) {
    pnlEl.textContent = fmtSign(m.total_pnl) + '€';
    pnlEl.className   = 'metric-val ' + (m.total_pnl >= 0 ? 'pnl-pos' : 'pnl-neg');
  }
  setEl('m-fees', '-' + m.total_fees.toFixed(4) + '€');
  setEl('m-groq', m.groq_approved + ' / ' + m.groq_rejected);
  // header BTC RSI
  var hdrRsi = document.getElementById('hdr-btc-rsi');
  // updated below in updateBtc
}

function updateBtc(btc, cb) {
  if (!btc || !btc.rsi) return;
  setEl('btc-rsi', btc.rsi.toFixed(1));
  var emaEl = document.getElementById('btc-ema');
  if (emaEl) {
    emaEl.textContent = btc.ema50 || '—';
    emaEl.className   = 'val ' + (btc.ema50 === 'SOPRA' ? 'pnl-pos' : 'pnl-neg');
  }
  if (btc.adx) setEl('btc-adx', btc.adx.toFixed(1));
  var badge = document.getElementById('btc-badge');
  if (badge) {
    if (btc.ema50 === 'SOTTO') {
      badge.className   = 'badge badge-error';
      badge.textContent = 'BOT IN ATTESA — BTC sotto EMA50';
    } else if (btc.rsi > 65) {
      badge.className   = 'badge badge-warn';
      badge.textContent = 'BTC IPERCOMPRATO — long bloccati';
    } else {
      badge.className   = 'badge badge-online';
      badge.textContent = 'MERCATO FAVOREVOLE';
    }
  }
  var hdrRsi = document.getElementById('hdr-btc-rsi');
  if (hdrRsi) hdrRsi.textContent = 'BTC RSI: ' + btc.rsi.toFixed(1);
}

// Pre-populate with initial server-side data (no flash before first SSE)
updatePositions(INIT_DATA.positions);
updateMetrics(INIT_DATA.metrics);
updateBtc(INIT_DATA.btc, INIT_DATA.circuit_breaker);

// SSE connection
(function() {
  var src = new EventSource('/api/stream');
  var dot = document.getElementById('sse-dot');

  src.onopen = function() {
    if (dot) dot.style.background = '#4caf82';
  };
  src.onerror = function() {
    if (dot) dot.style.background = '#e05c5c';
  };
  src.onmessage = function(e) {
    try {
      var d = JSON.parse(e.data);
      if (d.error) { console.warn('SSE error:', d.error); return; }
      document.getElementById('last-update').textContent = new Date().toTimeString().slice(0,8);
      if (dot) dot.style.background = '#4caf82';
      updatePositions(d.positions);
      updateMetrics(d.metrics);
      updateBtc(d.btc, d.circuit_breaker);
    } catch(ex) {
      console.warn('SSE parse error:', ex);
    }
  };
})();


// ── Paginatori (init immediato) + Chart (lazy su tab show) ────────────────────
var chartCfg = { plugins: { legend: { labels: { color: '#ccc', font: { size: 11 } } } } };
var _chartsInit = {};

var _tradePg = new Paginator({
  data: TRADES_DATA, perPage: 8, renderRow: renderTrade,
  tbodyId: 'trades-tbody', prevId: 'trades-prev', nextId: 'trades-next',
  pageInfoId: 'trades-page-info', countLabelId: 'trades-count-label', totalInfoId: 'trades-total-info',
});

var _groqPg = new GroqPaginator(GROQ_DATA, 5);

var _discPg = new Paginator({
  data: DISC_DATA, perPage: 5, renderRow: renderDisc,
  tbodyId: 'disc-tbody', prevId: 'disc-prev', nextId: 'disc-next',
  pageInfoId: 'disc-page-info', countLabelId: 'disc-count-label', totalInfoId: 'disc-total-info',
});


function _initStorico() {
  if (_tradePg && _tradePg.tbody) _tradePg.render();
  if (!_chartsInit.storico && PNL_LABELS && PNL_LABELS.length) {
    _chartsInit.storico = true;
    new Chart(document.getElementById('pnlChart'), {
      type: 'line',
      data: {
        labels: PNL_LABELS,
        datasets: [{ label: 'P&L EUR', data: PNL_DATA,
          borderColor: '#4caf82', backgroundColor: 'rgba(76,175,130,0.1)',
          fill: true, tension: 0.3, pointRadius: 3 }]
      },
      options: Object.assign({}, chartCfg, { responsive: true, maintainAspectRatio: true,
        scales: {
          x: { ticks: { color: '#888', maxTicksLimit: 9 }, grid: { color: '#2a2d3e' } },
          y: { ticks: { color: '#888' }, grid: { color: '#2a2d3e' } }
        }
      })
    });
    new Chart(document.getElementById('outcomeChart'), {
      type: 'doughnut',
      data: { labels: ['TP OK','SL Stop','Manuale'],
        datasets: [{ data: OUTCOME_DATA, backgroundColor: ['#4caf82','#e05c5c','#7a7a9a'] }] },
      options: Object.assign({}, chartCfg, { responsive: true, maintainAspectRatio: true })
    });
  }
}

function _initAi() {
  _groqPg.render();
  if (!_chartsInit.ai) {
    _chartsInit.ai = true;
    new Chart(document.getElementById('groqChart'), {
      type: 'doughnut',
      data: { labels: ['Approvati','Rifiutati'],
        datasets: [{ data: [INIT_DATA.metrics.groq_approved, INIT_DATA.metrics.groq_rejected],
          backgroundColor: ['#4caf82','#e05c5c'] }] },
      options: Object.assign({}, chartCfg, { responsive: true, maintainAspectRatio: true })
    });
    if (PNL_LABELS && PNL_LABELS.length) {
      new Chart(document.getElementById('confChart'), {
        type: 'bar',
        data: { labels: ['1','2','3','4','5','6','7','8','9','10'],
          datasets: [{ label: 'Decisioni', data: CONF_DATA,
            backgroundColor: '#4a4d6e', borderRadius: 3 }] },
        options: Object.assign({}, chartCfg, { responsive: true, maintainAspectRatio: true,
          scales: {
            x: { ticks: { color: '#888' }, grid: { color: '#2a2d3e' } },
            y: { ticks: { color: '#888' }, grid: { color: '#2a2d3e' } }
          }
        })
      });
    }
  }
}

function _initDisc() {
  _discPg.render();
}

var _tabInitFns = { 'tab-storico': _initStorico, 'tab-ai': _initAi, 'tab-discovery': _initDisc };


// ── Tab persistence + lazy init ───────────────────────────────────────────────
(function() {
  var saved = localStorage.getItem('activeTab') || 'tab-live';
  var btn = document.querySelector('[data-bs-target="#' + saved + '"]');
  if (btn) new bootstrap.Tab(btn).show();
  // Init the saved tab if it has lazy content
  if (_tabInitFns[saved]) _tabInitFns[saved]();

  document.querySelectorAll('[data-bs-toggle="tab"]').forEach(function(t) {
    t.addEventListener('shown.bs.tab', function(e) {
      var tabId = e.target.dataset.bsTarget.slice(1);
      localStorage.setItem('activeTab', tabId);
      if (_tabInitFns[tabId]) _tabInitFns[tabId]();
    });
  });
})();


// ── Profile editor ─────────────────────────────────────────────────────────────
var activeProfileName = '{{ active_profile }}';

function fillForm(profile) {
  var form = document.getElementById('params-form');
  Object.keys(profile).forEach(function(k) { if (form[k]) form[k].value = profile[k]; });
}

function switchProfile(name, btn) {
  activeProfileName = name;
  document.querySelectorAll('.profile-tab-btn').forEach(function(b) {
    b.className = b.className.replace(/btn-(success|warning|info|danger)(\\s+|$)/, 'btn-outline-secondary ').replace('active', '').trim();
  });
  var colorMap = { conservative:'btn-success', moderate:'btn-warning', aggressive:'btn-danger', custom:'btn-info' };
  btn.className = btn.className.replace('btn-outline-secondary', colorMap[name] || 'btn-secondary') + ' active';
  document.getElementById('editing-profile-name').textContent = name.charAt(0).toUpperCase() + name.slice(1);
  document.getElementById('custom-notice').style.display = name === 'custom' ? 'inline' : 'none';
  document.getElementById('params-profile-field').value = name;
  fillForm(PROFILES_JS[name] || PROFILES_JS.conservative);
  document.getElementById('gemini-result').style.display = 'none';
}

function applyParams(e) {
  e.preventDefault();
  var form = document.getElementById('params-form');
  var f = function(n) { return parseFloat(form[n] ? form[n].value : 0); };
  if (f('ATR_TP_MULT') <= f('ATR_SL_MULT')) { alert('ATR TP mult deve essere > ATR SL mult (R:R deve essere > 1).'); return; }
  if (!(f('AI_SIZE_HIGH_PCT') > f('AI_SIZE_MED_PCT') && f('AI_SIZE_MED_PCT') > f('AI_SIZE_MIN_PCT'))) { alert('Le size devono essere in ordine decrescente: HIGH > MED > MIN.'); return; }
  form.submit();
  var badge = document.getElementById('params-saved-badge');
  badge.style.display = 'inline-block';
  setTimeout(function() { badge.style.display = 'none'; }, 3000);
}

function resetCurrentProfile() {
  fillForm(PROFILES_JS[activeProfileName] || PROFILES_JS.conservative);
}

// Inizializza form col profilo attivo
fillForm(PROFILES_JS[activeProfileName] || PROFILES_JS.conservative);


// ── Restart bot ───────────────────────────────────────────────────────────────
function restartBot() {
  var modal = bootstrap.Modal.getInstance(document.getElementById('restartModal'));
  if (modal) modal.hide();
  var btn = document.querySelector('[data-bs-target="#restartModal"]');
  if (btn) { btn.textContent = 'Riavvio...'; btn.disabled = true; }
  fetch('/action/restart', { method: 'POST' }).then(function() {
    setTimeout(function() {
      if (btn) { btn.textContent = 'Riavvia Bot'; btn.disabled = false; }
    }, 5000);
  });
}


// ── Chiudi posizione ──────────────────────────────────────────────────────────
function closePos(pair) {
  if (!confirm('Chiudere ' + pair + '?')) return;
  var row = document.getElementById('pos-row-' + pair);
  if (row) row.style.opacity = '0.4';
  fetch('/action/close', {
    method: 'POST',
    body: new URLSearchParams({ pair: pair }),
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
  });
}


// ── Saldo Kraken ──────────────────────────────────────────────────────────────
function loadBalance() {
  var el = document.getElementById('balance-result');
  el.innerHTML = '<span style="color:#aaa">Caricamento...</span>';
  fetch('/api/balance')
    .then(function(r) { return r.json(); })
    .then(function(d) {
      if (!d.ok) { el.innerHTML = '<span style="color:#e05c5c">Errore: ' + d.error + '</span>'; return; }
      var txt = '<span style="color:#4caf82">EUR: ' + d.eur.toFixed(2) + '€</span>';
      var keys = Object.keys(d.crypto);
      if (keys.length) {
        txt += '<span style="color:#aaa;margin-left:8px;">';
        keys.forEach(function(k) { txt += k + ': ' + parseFloat(d.crypto[k]).toFixed(4) + '  '; });
        txt += '</span>';
      }
      el.innerHTML = txt;
    })
    .catch(function() { el.innerHTML = '<span style="color:#e05c5c">Connessione fallita</span>'; });
}


// ── Gemini validator ──────────────────────────────────────────────────────────
function openGeminiValidator() {
  var panel = document.getElementById('gemini-panel');
  panel.style.display = panel.style.display === 'none' ? 'block' : 'none';
}

function validateWithGemini() {
  var key = document.getElementById('gemini-key').value.trim();
  if (!key) { alert('Inserisci la Gemini API key.'); return; }
  var form    = document.getElementById('params-form');
  var spinner = document.getElementById('gemini-spinner');
  var textEl  = document.getElementById('gemini-text');
  var result  = document.getElementById('gemini-result');
  var params  = {};
  Object.keys(PROFILES_JS.conservative).forEach(function(k) { if (form[k]) params[k] = parseFloat(form[k].value); });
  var prompt = 'Analizza questi parametri di un trading bot su Kraken (crypto/EUR):\\n\\n' + JSON.stringify(params, null, 2) + '\\n\\nFornisci in italiano (max 220 parole):\\n1. Rischi principali\\n2. Coerenza parametri\\n3. Suggerimenti (max 2-3)';
  result.style.display  = 'block';
  spinner.style.display = 'block';
  textEl.textContent    = '';
  fetch('https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key=' + key, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ contents: [{ parts: [{ text: prompt }] }] }),
  }).then(function(res) {
    return res.json().then(function(data) {
      if (!res.ok) throw new Error(data.error && data.error.message ? data.error.message : 'HTTP ' + res.status);
      var text = (data.candidates && data.candidates[0] && data.candidates[0].content &&
                  data.candidates[0].content.parts && data.candidates[0].content.parts[0] &&
                  data.candidates[0].content.parts[0].text) ? data.candidates[0].content.parts[0].text : 'Nessuna risposta.';
      textEl.innerHTML = text.replace(/\\*\\*(.*?)\\*\\*/g, '<strong>$1</strong>').replace(/\\n/g, '<br>');
    });
  }).catch(function(err) {
    textEl.innerHTML = '<span style="color:#e05c5c;">Errore: ' + err.message + '</span>';
  }).finally(function() {
    spinner.style.display = 'none';
  });
}
</script>
</body>
</html>"""


# ── Index route ────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    msg = request.args.get("msg", "")

    trades_log = db.get_trades()
    groq_log   = db.get_groq_decisions(500)
    discovery  = db.get_latest_discovery()

    kraken_status  = _fetch_kraken_status()
    fear_greed     = market_data.get_fear_greed()
    news_headlines = market_data.get_crypto_news(5) if config.NEWS_ENABLED else []
    mempool        = market_data.get_btc_mempool() if config.ONCHAIN_ENABLED else None

    init_data = _collect_live_data()

    trades_js = [_trade_to_js(t) for t in reversed(trades_log)]
    groq_js   = [_groq_to_js(g) for g in reversed(groq_log)]
    disc_js   = [_disc_to_js(c) for c in (discovery.get("candidates", []) if discovery else [])]

    pnl_cum = 0.0
    pnl_labels, pnl_data = [], []
    for t in trades_log:
        pnl_cum += t["pnl_eur"]
        pnl_labels.append(_fmt_time(t["closed_at"]))
        pnl_data.append(round(pnl_cum, 4))

    outcome_data = [
        sum(1 for t in trades_log if t.get("exit_reason") == "tp"),
        sum(1 for t in trades_log if t.get("exit_reason") == "sl"),
        sum(1 for t in trades_log if t.get("exit_reason") not in ("tp", "sl")),
    ]

    conf_counts = [0] * 10
    for g in groq_log:
        c = g.get("confidence", 0)
        if 1 <= c <= 10:
            conf_counts[c - 1] += 1

    custom_raw  = db.get_setting("profile_custom")
    custom_form = json.loads(custom_raw) if custom_raw else _profile_to_form(config.PROFILE_CONSERVATIVE)
    profiles_js = {
        "conservative": _profile_to_form(config.PROFILE_CONSERVATIVE),
        "moderate":     _profile_to_form(config.PROFILE_MODERATE),
        "aggressive":   _profile_to_form(config.PROFILE_AGGRESSIVE),
        "custom":       custom_form,
    }

    return render_template_string(
        TEMPLATE,
        init_data=init_data,
        trades_js=trades_js,
        groq_js=groq_js,
        disc_js=disc_js,
        pnl_chart_labels=pnl_labels,
        pnl_chart_data=pnl_data,
        outcome_data=outcome_data,
        conf_distribution=conf_counts,
        discovery=discovery,
        dynamic_pairs=_load_dynamic_pairs(),
        fear_greed=fear_greed,
        news_headlines=news_headlines,
        mempool=mempool,
        profiles_js=profiles_js,
        active_profile=config.ACTIVE_PROFILE,
        paper=config.PAPER_TRADING,
        kraken_status=kraken_status,
        pairs=", ".join(config.PAIRS),
        scan_interval=config.SCAN_INTERVAL_MINUTES,
        groq_model=config.GROQ_MODEL,
        fee_taker=f"{config.KRAKEN_TAKER_FEE*100:.2f}",
        fee_maker=f"{config.KRAKEN_MAKER_FEE*100:.2f}",
        leverage=config.MARGIN_LEVERAGE,
        kelly_fraction=config.KELLY_FRACTION,
        kelly_min_trades=config.KELLY_MIN_TRADES,
        kelly_enabled=config.KELLY_ENABLED,
        btc_filter=config.BTC_FILTER_ENABLED,
        btc_ema_filter=config.BTC_EMA_FILTER,
        daily_ema_filter=config.DAILY_EMA_FILTER,
        fg_enabled=config.FEAR_GREED_ENABLED,
        fg_min=config.FEAR_GREED_MIN,
        fg_max=config.FEAR_GREED_MAX,
        news_enabled=config.NEWS_ENABLED,
        onchain_enabled=config.ONCHAIN_ENABLED,
        trailing_enabled=config.TRAILING_STOP_ENABLED,
        cb_enabled=config.CIRCUIT_BREAKER_ENABLED,
        min_score=config.MIN_SIGNAL_SCORE,
        adx_min=config.ADX_MIN,
        vol_mult=config.VOLUME_SPIKE_MULT,
        rsi_1h_max=config.RSI_1H_BUY_MAX,
        rsi_15m_max=config.RSI_15M_BUY_MAX,
        btc_rsi_max=config.BTC_RSI_MAX,
        groq_conf=config.GROQ_MIN_CONFIDENCE,
        atr_tp=config.ATR_TP_MULT,
        atr_sl=config.ATR_SL_MULT,
        max_trades=config.MAX_OPEN_TRADES,
        capital_pct=f"{config.MAX_CAPITAL_DEPLOYED*100:.0f}",
        size_high=f"{config.AI_SIZE_HIGH*100:.0f}",
        size_med=f"{config.AI_SIZE_MED*100:.0f}",
        size_min=f"{config.AI_SIZE_MIN*100:.0f}",
        cb_eur=f"{config.CIRCUIT_BREAKER_LOSS_EUR:.0f}",
        fmt_time=_fmt_time,
        msg=msg,
        msg_label=_MSG_LABELS.get(msg, msg),
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8501, debug=False, threaded=True)
