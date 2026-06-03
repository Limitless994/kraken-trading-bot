"""
dashboard.py — Flask dashboard (ARM-compatible, no numpy/pandas required).

Run with:
    python3 dashboard.py
"""
import json
import re
import time
import uuid
from datetime import datetime
from pathlib import Path

import requests
from flask import Flask, jsonify, redirect, render_template_string, request, url_for

from core import config, market_data

app = Flask(__name__)

_DIR              = Path(__file__).parent
TRADING_LOG_FILE  = _DIR / "logs" / "trading.log"
POSITIONS_FILE    = _DIR / "positions.json"
TRADES_LOG_FILE   = _DIR / "trades_log.json"
GROQ_LOG_FILE     = _DIR / "groq_log.json"
DISCOVERY_FILE    = _DIR / "discovery_log.json"
BOT_COMMANDS_FILE = _DIR / "bot_commands.json"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return default


def _fetch_price(pair: str):
    try:
        r = requests.get(
            f"https://api.kraken.com/0/public/Ticker?pair={pair}", timeout=5
        )
        result = r.json()["result"]
        key = next(iter(result))
        return float(result[key]["c"][0])
    except Exception:
        return None


def _fetch_kraken_status() -> str:
    try:
        r = requests.get(
            "https://api.kraken.com/0/public/SystemStatus", timeout=5
        )
        return r.json().get("result", {}).get("status", "unknown")
    except Exception:
        return "error"


def _fmt_time(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%d/%m %H:%M")


def _parse_btc_filter() -> dict:
    """Estrae RSI, EMA50, ADX di BTC dall'ultima riga del trading.log."""
    if not TRADING_LOG_FILE.exists():
        return {}
    try:
        lines = TRADING_LOG_FILE.read_text(encoding="utf-8").splitlines()
        for line in reversed(lines[-300:]):
            m = re.search(r'BTC filter: RSI=([\d.]+)\s+EMA50=(\w+)\s+ADX=([\d.]+)', line)
            if m:
                return {"rsi": float(m.group(1)), "ema50": m.group(2), "adx": float(m.group(3))}
    except Exception:
        pass
    return {}


# ── Command queue ────────────────────────────────────────────────────────────

def _queue_command(cmd: dict) -> None:
    """Append a command to bot_commands.json for the bot to process."""
    try:
        existing = _load_json(BOT_COMMANDS_FILE, [])
        cmd.setdefault("id", str(uuid.uuid4())[:8])
        cmd.setdefault("ts", time.time())
        existing.append(cmd)
        BOT_COMMANDS_FILE.write_text(
            json.dumps(existing, ensure_ascii=False), encoding="utf-8"
        )
    except Exception as exc:
        app.logger.error("queue_command error: %s", exc)


# ── Action routes ─────────────────────────────────────────────────────────────

_MSG_LABELS = {
    "profile_changed": "Profilo aggiornato",
    "scan_queued":     "Scan in coda — eseguito entro 15s",
    "discover_queued": "Discovery avviata in background",
    "close_queued":    "Chiusura in coda — eseguita entro 15s",
    "error":           "Errore — controlla i log",
    "unknown_profile": "Profilo non valido",
    "no_pair":         "Coppia non specificata",
}


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


@app.route("/api/balance")
def api_balance():
    try:
        from core import kraken_client
        bal = kraken_client.get_balance()
        eur = bal.pop("ZEUR", bal.pop("EUR", 0.0))
        return jsonify({"ok": True, "eur": eur, "crypto": bal})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)})


# ── HTML template ─────────────────────────────────────────────────────────────

TEMPLATE = """
<!DOCTYPE html>
<html lang="it">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="15">
  <title>Trading Bot</title>
  <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
  <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
  <style>
    body { background: #0f1117; color: #e0e0e0; font-family: 'Segoe UI', sans-serif; }
    .card { background: #1a1d27; border: 1px solid #2a2d3e; border-radius: 8px; }
    .card-header { background: #22263a; border-bottom: 1px solid #2a2d3e; color: #e0e0e0; font-weight: 600; padding: 10px 16px; }
    .table { color: #e0e0e0; font-size: 0.88rem; }
    .table-dark { --bs-table-bg: #1a1d27; }
    .table-dark td, .table-dark th { border-color: #2a2d3e; }
    .badge-online { background: #1a7a4a; }
    .badge-warn   { background: #7a5a1a; }
    .badge-error  { background: #7a1a1a; }
    .metric-val   { font-size: 1.5rem; font-weight: 700; margin-top: 4px; color: #e0e0e0; }
    .metric-label { color: #aaa; font-size: 0.78rem; }
    .pnl-pos { color: #4caf82; }
    .pnl-neg { color: #e05c5c; }
    .groq-esegui  { border-left: 3px solid #4caf82; background: #111c14; color: #e0e0e0; padding: 10px 14px; border-radius: 4px; margin-bottom: 8px; }
    .groq-rifiuta { border-left: 3px solid #e05c5c; background: #1c1111; color: #e0e0e0; padding: 10px 14px; border-radius: 4px; margin-bottom: 8px; }
    .groq-reason  { font-size: 0.9rem;  margin-top: 6px; color: #e8e8e8; line-height: 1.4; }
    .groq-stats   { font-size: 0.83rem; color: #c0c0c0; margin-top: 5px; display: flex; flex-wrap: wrap; gap: 6px 16px; }
    .refresh-note { font-size: 0.75rem; color: #aaa; }
    .text-muted   { color: #aaa !important; }
    .conf-badge   { font-size: 0.7rem; padding: 2px 6px; border-radius: 10px; background: #2a2d3e; color: #ccc; }
    .side-long    { color: #4caf82; font-size: 0.75rem; font-weight: 600; }
    .side-short   { color: #e05c5c; font-size: 0.75rem; font-weight: 600; }
    canvas        { max-height: 220px; }
  </style>
</head>
<body>
<div class="container-fluid py-3 px-4">

  <!-- Header -->
  <div class="d-flex align-items-center gap-3 mb-3 flex-wrap">
    <h5 class="mb-0 fw-bold">Kraken Trading Bot</h5>
    {% if paper %}<span class="badge bg-warning text-dark">PAPER TRADING</span>
    {% else %}<span class="badge bg-success">LIVE</span>{% endif %}
    {% if kraken_status == 'online' %}
      <span class="badge badge-online">Kraken: ONLINE</span>
    {% elif kraken_status in ('cancel_only','post_only','limit_only') %}
      <span class="badge badge-warn">Kraken: {{ kraken_status.upper() }}</span>
    {% else %}
      <span class="badge badge-error">Kraken: {{ kraken_status.upper() }}</span>
    {% endif %}
    {% if btc_rsi %}
    <span class="badge {% if btc_rsi < 65 %}bg-secondary{% else %}badge-warn{% endif %}">
      BTC RSI: {{ "%.1f"|format(btc_rsi) }}
    </span>
    {% endif %}
    {% if fear_greed %}
    <span class="badge {% if fear_greed.value <= 20 or fear_greed.value >= 80 %}badge-warn{% elif fear_greed.value <= 40 %}bg-secondary{% else %}badge-online{% endif %}">
      F&G: {{ fear_greed.value }} {{ fear_greed.label }}
    </span>
    {% endif %}
    {% if circuit_breaker_active %}
    <span class="badge badge-error">CIRCUIT BREAKER ATTIVO</span>
    {% endif %}
    <span class="refresh-note ms-auto">Aggiornamento ogni 15s — {{ now }}</span>
  </div>

  <!-- Control Panel -->
  <div class="card mb-3">
    <div class="card-body py-2 px-3">
      <div class="d-flex flex-wrap align-items-center gap-2">
        <span style="font-size:0.78rem;color:#aaa;">Profilo:</span>
        {% set profile_colors = {'conservative': 'btn-success', 'moderate': 'btn-warning', 'aggressive': 'btn-danger'} %}
        {% for p_name in ['conservative', 'moderate', 'aggressive'] %}
        <form method="post" action="/action/profile" class="mb-0">
          <input type="hidden" name="profile" value="{{ p_name }}">
          <button type="submit"
            class="btn btn-sm {% if active_profile == p_name %}{{ profile_colors[p_name] }}{% else %}btn-outline-secondary{% endif %}"
            style="font-size:0.78rem;">
            {{ p_name|capitalize }}
          </button>
        </form>
        {% endfor %}
        <div class="vr" style="height:24px;"></div>
        <form method="post" action="/action/scan" class="mb-0">
          <button type="submit" class="btn btn-sm btn-outline-light" style="font-size:0.78rem;">Scan ora</button>
        </form>
        <form method="post" action="/action/discover" class="mb-0">
          <button type="submit" class="btn btn-sm btn-outline-info" style="font-size:0.78rem;">Discovery ora</button>
        </form>
        <button type="button" class="btn btn-sm btn-outline-secondary" style="font-size:0.78rem;" onclick="loadBalance()">Saldo Kraken</button>
        {% if msg %}
        <span class="badge {% if 'error' in msg or 'unknown' in msg or 'no_pair' in msg %}bg-danger{% else %}bg-success{% endif %} ms-1" style="font-size:0.78rem;">
          {{ msg_label }}
        </span>
        {% endif %}
        <span id="balance-result" style="font-size:0.82rem;"></span>
      </div>
    </div>
  </div>

  <!-- Metrics -->
  <div class="row g-2 mb-3">
    {% set metrics = [
      ("Posizioni aperte",  positions|length ~ " / " ~ max_trades, ""),
      ("Trade chiusi",      total_trades, ""),
      ("Win rate",          "%.0f%%"|format(winrate), ""),
      ("P&L netto",         "%+.2f€"|format(total_pnl), "pnl-pos" if total_pnl >= 0 else "pnl-neg"),
      ("Fee pagate",        "-%.4f€"|format(total_fees), "pnl-neg"),
      ("Groq ✅ / ❌",      groq_approved ~ " / " ~ groq_rejected, ""),
    ] %}
    {% for label, val, cls in metrics %}
    <div class="col-6 col-md-2">
      <div class="card text-center p-2">
        <div class="metric-label">{{ label }}</div>
        <div class="metric-val {{ cls }}">{{ val }}</div>
      </div>
    </div>
    {% endfor %}
  </div>

  <!-- Stato Mercato BTC -->
  <div class="card mb-3">
    <div class="card-header d-flex justify-content-between align-items-center">
      <span>Stato Mercato — BTC</span>
      {% if btc_s and btc_s.ema50 == 'SOTTO' and btc_ema_filter %}
        <span class="badge badge-error">BOT IN ATTESA — BTC sotto EMA50</span>
      {% elif btc_s and btc_s.rsi > 65 %}
        <span class="badge badge-warn">BTC IPERCOMPRATO — long bloccati</span>
      {% elif btc_s %}
        <span class="badge badge-online">MERCATO FAVOREVOLE</span>
      {% else %}
        <span class="badge bg-secondary">In attesa dati...</span>
      {% endif %}
    </div>
    <div class="card-body">
      {% if btc_s %}
      <div class="row g-2 text-center mb-3">
        <div class="col-4">
          <div class="card p-2" style="border-color:{% if btc_s.rsi < 30 %}#f0a500{% elif btc_s.rsi > 70 %}#e05c5c{% else %}#2a2d3e{% endif %}">
            <div class="metric-label">BTC RSI (1h)</div>
            <div class="metric-val {% if btc_s.rsi < 30 %}text-warning{% elif btc_s.rsi > 70 %}pnl-neg{% else %}text-white{% endif %}">{{ "%.1f"|format(btc_s.rsi) }}</div>
            <div style="font-size:0.75rem;color:#aaa">
              {% if btc_s.rsi < 30 %}Ipervenduto{% elif btc_s.rsi > 70 %}Ipercomprato{% elif btc_s.rsi > 60 %}Zona calda{% else %}Neutro{% endif %}
            </div>
          </div>
        </div>
        <div class="col-4">
          <div class="card p-2" style="border-color:{% if btc_s.ema50 == 'SOPRA' %}#1a7a4a{% else %}#7a1a1a{% endif %}">
            <div class="metric-label">BTC / EMA50</div>
            <div class="metric-val {% if btc_s.ema50 == 'SOPRA' %}pnl-pos{% else %}pnl-neg{% endif %}">{{ btc_s.ema50 }}</div>
            <div style="font-size:0.75rem;color:#aaa">{% if btc_s.ema50 == 'SOPRA' %}Trend rialzista{% else %}Trend ribassista{% endif %}</div>
          </div>
        </div>
        <div class="col-4">
          <div class="card p-2" style="border-color:{% if btc_s.adx > 50 %}#e05c5c{% elif btc_s.adx > 30 %}#f0a500{% else %}#2a2d3e{% endif %}">
            <div class="metric-label">BTC ADX</div>
            <div class="metric-val {% if btc_s.adx > 50 %}pnl-neg{% elif btc_s.adx > 30 %}text-warning{% else %}text-white{% endif %}">{{ "%.1f"|format(btc_s.adx) }}</div>
            <div style="font-size:0.75rem;color:#aaa">
              {% if btc_s.adx > 50 %}Trend molto forte{% elif btc_s.adx > 30 %}Trend moderato{% elif btc_s.adx > 20 %}Trend debole{% else %}Mercato laterale{% endif %}
            </div>
          </div>
        </div>
      </div>
      <div class="p-2" style="background:#0d1520;border-radius:6px;font-size:0.83rem;line-height:1.6;color:#d0d0d0;">
        {% if btc_s.ema50 == 'SOTTO' and btc_ema_filter %}
          <strong style="color:#e05c5c;">Perché il bot non sta tradando?</strong><br>
          BTC è sotto la sua <strong>EMA50</strong> (media mobile a 50 periodi) — indica una fase ribassista.
          Il bot blocca tutti i long sulle altcoin per non comprare mentre il mercato scende.<br><br>
          <strong>Quando riprenderà?</strong> Quando BTC risale sopra l'EMA50.
          Con ADX = {{ "%.0f"|format(btc_s.adx) }} (trend forte) potrebbe richiedere qualche giorno.
        {% elif btc_s.rsi > 65 %}
          <strong style="color:#f0a500;">BTC ipercomprato</strong><br>
          RSI &gt; 65 segnala un mercato surriscaldato. Il bot blocca i long per evitare di entrare a prezzi di picco.
        {% else %}
          <strong style="color:#4caf82;">Il bot è operativo</strong><br>
          Le condizioni di mercato sono favorevoli. Scansione ogni {{ scan_interval }} minuti.
        {% endif %}
      </div>
      {% else %}
      <p class="text-muted mb-0">Dati BTC non ancora disponibili — attendi il primo scan (max 15 min).</p>
      {% endif %}
    </div>
  </div>

  <!-- Charts row -->
  {% if pnl_chart_labels %}
  <div class="row g-2 mb-3">
    <div class="col-md-6">
      <div class="card p-3">
        <div class="card-header">P&L cumulativo</div>
        <canvas id="pnlChart"></canvas>
      </div>
    </div>
    <div class="col-md-3">
      <div class="card p-3">
        <div class="card-header">Esiti trade</div>
        <canvas id="outcomeChart"></canvas>
      </div>
    </div>
    <div class="col-md-3">
      <div class="card p-3">
        <div class="card-header">Groq decisions</div>
        <canvas id="groqChart"></canvas>
      </div>
    </div>
  </div>
  {% endif %}

  <!-- Open Positions -->
  <div class="card mb-3">
    <div class="card-header">Posizioni aperte</div>
    <div class="card-body p-0">
      {% if positions %}
      <table class="table table-dark table-hover mb-0">
        <thead><tr>
          <th>Coppia</th><th>Tipo</th><th>Entry</th><th>Ora</th><th>P&L %</th><th>P&L €</th>
          <th>TP</th><th>SL</th><th>Trailing</th><th>Size</th><th>Aperta</th><th></th>
        </tr></thead>
        <tbody>
        {% for p in positions %}
        <tr>
          <td><strong>{{ p.pair }}</strong></td>
          <td><span class="{% if p.side == 'short' %}side-short{% else %}side-long{% endif %}">{{ p.side|upper }}</span></td>
          <td>{{ "%.6f"|format(p.entry) }}</td>
          <td>{{ "%.6f"|format(p.price) if p.price else "—" }}</td>
          <td class="{% if p.pnl_pct >= 0 %}pnl-pos{% else %}pnl-neg{% endif %}">{{ "%+.2f"|format(p.pnl_pct) }}%</td>
          <td class="{% if p.pnl_eur >= 0 %}pnl-pos{% else %}pnl-neg{% endif %}">{{ "%+.2f"|format(p.pnl_eur) }}€</td>
          <td>{{ "%.6f"|format(p.tp) }}</td>
          <td class="pnl-neg">{{ "%.6f"|format(p.sl) }}</td>
          <td class="text-warning">{{ "%.6f"|format(p.trailing) if p.trailing else "—" }}</td>
          <td>{{ "%.2f"|format(p.size_eur) }}€</td>
          <td>{{ p.opened_at }}</td>
          <td>
            <form method="post" action="/action/close" class="mb-0"
                  onsubmit="return confirm('Chiudere {{ p.pair }}?')">
              <input type="hidden" name="pair" value="{{ p.pair }}">
              <button type="submit" class="btn btn-sm btn-outline-danger" style="font-size:0.75rem;">Chiudi</button>
            </form>
          </td>
        </tr>
        {% endfor %}
        </tbody>
      </table>
      {% else %}
      <p class="text-muted p-3 mb-0">Nessuna posizione aperta.</p>
      {% endif %}
    </div>
  </div>

  <!-- Trade History -->
  <div class="card mb-3">
    <div class="card-header">Storico trade chiusi</div>
    <div class="card-body p-0">
      {% if trades %}
      <table class="table table-dark table-hover mb-0">
        <thead><tr>
          <th>Data</th><th>Coppia</th><th>Esito</th><th>Entry</th>
          <th>Exit</th><th>P&L lordo</th><th>Fee</th><th>P&L netto</th><th>%</th>
        </tr></thead>
        <tbody>
        {% for t in trades %}
        <tr>
          <td>{{ fmt_time(t.closed_at) }}</td>
          <td><strong>{{ t.pair }}</strong></td>
          <td>
            {% if t.exit_reason == 'tp' %}✅ TP
            {% elif t.exit_reason == 'sl' %}🛑 SL
            {% else %}🔧 Manuale{% endif %}
          </td>
          <td>{{ "%.6f"|format(t.entry) }}</td>
          <td>{{ "%.6f"|format(t.exit) }}</td>
          <td class="{% if t.get('gross_pnl_eur', t.pnl_eur) >= 0 %}pnl-pos{% else %}pnl-neg{% endif %}">{{ "%+.2f"|format(t.get('gross_pnl_eur', t.pnl_eur)) }}€</td>
          <td class="pnl-neg">-{{ "%.4f"|format(t.get('fee_eur', 0)) }}€</td>
          <td class="{% if t.pnl_eur >= 0 %}pnl-pos{% else %}pnl-neg{% endif %}">{{ "%+.2f"|format(t.pnl_eur) }}€</td>
          <td class="{% if t.pnl_pct >= 0 %}pnl-pos{% else %}pnl-neg{% endif %}">{{ "%+.2f"|format(t.pnl_pct) }}%</td>
        </tr>
        {% endfor %}
        </tbody>
      </table>
      {% else %}
      <p class="text-muted p-3 mb-0">Nessun trade chiuso ancora.</p>
      {% endif %}
    </div>
  </div>

  <!-- Groq Decisions -->
  <div class="card mb-3">
    <div class="card-header">Ragionamenti Groq AI</div>
    <div class="card-body">
      {% if groq_log %}
        {% for g in groq_log %}
        <div class="{% if g.decision == 'ESEGUI' %}groq-esegui{% else %}groq-rifiuta{% endif %}">
          <div class="d-flex justify-content-between align-items-center flex-wrap gap-1">
            <strong style="color:{% if g.decision == 'ESEGUI' %}#4caf82{% else %}#e05c5c{% endif %}">
              {% if g.decision == 'ESEGUI' %}✅{% else %}❌{% endif %}
              {{ g.decision }}{% if g.get('direction') %} [{{ g.direction }}]{% endif %} — {{ g.pair }}
            </strong>
            <div class="d-flex gap-2 align-items-center">
              {% if g.get('confidence') %}<span class="conf-badge">{{ g.confidence }}/10</span>{% endif %}
              <span style="font-size:0.75rem;color:#aaa;">{{ fmt_time(g.ts) }} | Score {{ g.score }}/6</span>
            </div>
          </div>
          <div class="groq-reason">{{ g.reason }}</div>
          <div class="groq-stats">
            <span>RSI 1h: <b>{{ "%.1f"|format(g.rsi_1h) }}</b></span>
            <span>RSI 15m: <b>{{ "%.1f"|format(g.rsi_15m) }}</b></span>
            <span>MACD: <b>{{ "%+.4f"|format(g.macd_hist) }}</b></span>
            <span>Vol: <b>{{ "%.2f"|format(g.volume_ratio) }}×</b></span>
            <span>ADX: <b>{{ "%.1f"|format(g.get('adx', 0)) }}</b></span>
            <span>Prezzo: <b>{{ "%.6g"|format(g.price) }}</b></span>
          </div>
        </div>
        {% endfor %}
      {% else %}
        <p class="text-muted mb-0">Nessuna valutazione ancora.</p>
      {% endif %}
    </div>
  </div>

  <!-- News + On-chain -->
  {% if news_headlines or mempool %}
  <div class="row g-2 mb-3">
    {% if news_headlines %}
    <div class="{% if mempool %}col-md-8{% else %}col-12{% endif %}">
      <div class="card h-100">
        <div class="card-header">Ultime notizie crypto</div>
        <div class="card-body p-2">
          {% for n in news_headlines %}
          <div style="font-size:0.82rem;padding:3px 0;border-bottom:1px solid #2a2d3e;color:#d0d0d0;">{{ n.title }}</div>
          {% endfor %}
        </div>
      </div>
    </div>
    {% endif %}
    {% if mempool %}
    <div class="{% if news_headlines %}col-md-4{% else %}col-12{% endif %}">
      <div class="card h-100">
        <div class="card-header">BTC On-chain / Mempool</div>
        <div class="card-body" style="font-size:0.85rem;color:#d0d0d0;">
          <div class="row g-2">
            <div class="col-6"><span style="color:#aaa;">Tx in attesa</span><div><strong>{{ mempool.tx_count }}</strong></div></div>
            <div class="col-6"><span style="color:#aaa;">Dimensione</span><div><strong>{{ mempool.size_mb }} MB</strong></div></div>
            <div class="col-6"><span style="color:#aaa;">Fee rapida</span><div><strong>{{ mempool.fee_fastest }} sat/vB</strong></div></div>
            <div class="col-6"><span style="color:#aaa;">Fee 1h</span><div><strong>{{ mempool.fee_hour }} sat/vB</strong></div></div>
          </div>
          <div class="mt-2" style="font-size:0.75rem;color:#aaa;">Alta fee = alta domanda BTC = bullish signal</div>
        </div>
      </div>
    </div>
    {% endif %}
  </div>
  {% endif %}

  <!-- Discovery -->
  <div class="card mb-3">
    <div class="card-header">Discovery giornaliera — nuove opportunità</div>
    <div class="card-body">
      {% if discovery %}
        <div class="mb-2" style="font-size:0.8rem;color:#aaa;">
          Ultima scansione: {{ fmt_time(discovery.timestamp) }} |
          Coppie analizzate: {{ discovery.pairs_scanned }}
        </div>
        <div class="p-2 mb-3" style="background:#0d1520;border-radius:6px;font-size:0.85rem;white-space:pre-wrap;color:#d0d0d0;">{{ discovery.groq_analysis }}</div>
        {% if discovery.candidates %}
        <table class="table table-dark table-hover mb-0" style="font-size:0.82rem;">
          <thead><tr>
            <th>Coppia</th><th>Score</th>
            <th>Train WR</th><th>Test WR</th><th>Consist.</th><th>Sharpe</th>
            <th>P&L medio</th><th>RSI ora</th><th>ADX ora</th>
          </tr></thead>
          <tbody>
          {% for c in discovery.candidates[:8] %}
          <tr>
            <td><strong>{{ c.pair }}</strong></td>
            <td><span class="badge bg-secondary">{{ c.mech_score }}</span></td>
            <td class="{% if c.train_win_rate >= 55 %}pnl-pos{% else %}pnl-neg{% endif %}">{{ c.train_win_rate }}%</td>
            <td class="{% if c.test_win_rate >= 55 %}pnl-pos{% else %}pnl-neg{% endif %}">{{ c.test_win_rate }}%</td>
            <td class="{% if c.consistency >= 0.7 %}pnl-pos{% else %}pnl-neg{% endif %}">{{ "%.0f"|format(c.consistency * 100) }}%</td>
            <td class="{% if c.test_sharpe >= 1 %}pnl-pos{% elif c.test_sharpe >= 0 %}text-muted{% else %}pnl-neg{% endif %}">{{ "%.2f"|format(c.get('test_sharpe', 0)) }}</td>
            <td class="{% if c.avg_pnl_pct >= 0 %}pnl-pos{% else %}pnl-neg{% endif %}">{{ "%+.2f"|format(c.avg_pnl_pct) }}%</td>
            <td>{{ c.rsi_now }}</td>
            <td>{{ c.adx_now }}</td>
          </tr>
          {% endfor %}
          </tbody>
        </table>
        {% endif %}
      {% else %}
        <p class="text-muted mb-0">Nessuna discovery ancora — usa /discover su Telegram o attendi la scansione automatica (ogni 24h).</p>
      {% endif %}
    </div>
  </div>

  <!-- Legenda -->
  <div class="card mb-3">
    <div class="card-header" data-bs-toggle="collapse" data-bs-target="#legenda" style="cursor:pointer;">
      Legenda indicatori <span style="font-size:0.8rem;color:#aaa;">(clicca per espandere)</span>
    </div>
    <div id="legenda" class="collapse">
      <div class="card-body" style="font-size:0.83rem;line-height:1.7;color:#d0d0d0;">
        <div class="row g-4">
          <div class="col-md-6">
            <h6 class="text-warning">RSI — Relative Strength Index</h6>
            <p>Misura se un asset è stato comprato troppo o troppo poco di recente. Va da 0 a 100.</p>
            <ul>
              <li><span class="text-warning">RSI &lt; 30</span> — Ipervenduto: il prezzo è sceso molto, possibile rimbalzo</li>
              <li><span class="pnl-neg">RSI &gt; 70</span> — Ipercomprato: il prezzo è salito molto, rischio correzione</li>
              <li><span class="text-muted">RSI 40–60</span> — Zona neutrale, nessun segnale forte</li>
            </ul>
            <small class="text-muted">Il bot compra solo con RSI(1h) &lt; {{ rsi_1h_max }} e RSI(15m) &lt; {{ rsi_15m_max }} per non entrare a prezzi già alti.</small>
          </div>
          <div class="col-md-6">
            <h6 class="text-warning">EMA50 — Media Mobile Esponenziale</h6>
            <p>Una "linea di tendenza" che segue il prezzo medio degli ultimi 50 periodi. Indica la direzione del trend.</p>
            <ul>
              <li><span class="pnl-pos">Prezzo SOPRA EMA50</span> — Trend rialzista, il mercato sale</li>
              <li><span class="pnl-neg">Prezzo SOTTO EMA50</span> — Trend ribassista, il mercato scende</li>
            </ul>
            <small class="text-muted">Il bot usa l'EMA50 di BTC come semaforo: se BTC è sotto EMA50, blocca i long su tutte le altcoin.</small>
          </div>
          <div class="col-md-6">
            <h6 class="text-warning">ADX — Average Directional Index</h6>
            <p>Misura la <em>forza</em> del trend, non la direzione. Va da 0 a 100.</p>
            <ul>
              <li><span class="text-muted">ADX &lt; 20</span> — Mercato laterale (sideways), no trend chiaro</li>
              <li><span class="text-warning">ADX 20–40</span> — Trend moderato</li>
              <li><span class="pnl-neg">ADX &gt; 40</span> — Trend molto forte (rialzo o ribasso deciso)</li>
            </ul>
          </div>
          <div class="col-md-6">
            <h6 class="text-warning">MACD</h6>
            <p>Confronta due medie mobili per segnalare cambi di momentum (slancio del prezzo).</p>
            <ul>
              <li><span class="pnl-pos">Istogramma &gt; 0 e in salita</span> — Momentum rialzista, segnale di acquisto</li>
              <li><span class="pnl-neg">Istogramma &lt; 0</span> — Momentum ribassista</li>
              <li>Crossover (passa da negativo a positivo) — segnale più forte</li>
            </ul>
          </div>
          <div class="col-md-6">
            <h6 class="text-warning">Score segnale (0–6 punti)</h6>
            <p>Prima di aprire un trade il bot verifica 6 condizioni. Servono almeno {{ min_score }}/6 per procedere.</p>
            <ul>
              <li>RSI multi-timeframe OK → 1 pt</li>
              <li>MACD rialzista (o ribassista per short) → 1 pt</li>
              <li>Volume spike ≥ {{ vol_mult }}× la media → 1 pt</li>
              <li>Prezzo sopra (o sotto) EMA50 → 1 pt</li>
              <li>Order book bullish (o bearish per short) → 1 pt</li>
              <li>ADX &gt; 30 → 1 pt</li>
            </ul>
          </div>
          <div class="col-md-6">
            <h6 class="text-warning">TP / SL / Trailing Stop</h6>
            <ul>
              <li><span class="pnl-pos">TP (Take Profit)</span> — Prezzo a cui il bot chiude in profitto automaticamente</li>
              <li><span class="pnl-neg">SL (Stop Loss)</span> — Prezzo a cui il bot chiude in perdita per limitare il danno</li>
              <li><span class="text-warning">Trailing Stop</span> — SL che si sposta con il prezzo, bloccando i guadagni</li>
            </ul>
            <small class="text-muted">R:R ≈ 2.5:1 — rischi 1€ per potenzialmente guadagnarne 2.5€. Short: TP/SL invertiti.</small>
          </div>
        </div>
      </div>
    </div>
  </div>

  <!-- Config -->
  <div class="card mb-3">
    <div class="card-header d-flex align-items-center gap-2">
      Configurazione attiva
      {% set pc = {'conservative':'bg-success','moderate':'bg-warning text-dark','aggressive':'bg-danger'} %}
      <span class="badge {{ pc[active_profile] }}" style="font-size:0.75rem;">{{ active_profile|upper }}</span>
    </div>
    <div class="card-body" style="font-size:0.82rem; color:#aaa;">
      <div class="row">
        <div class="col-md-4">
          <strong>Filtri</strong><br>
          Score min: {{ min_score }}/6 | Volume: ≥{{ vol_mult }}× | ADX min: {{ adx_min }}<br>
          RSI(1h): &lt;{{ rsi_1h_max }} | RSI(15m): &lt;{{ rsi_15m_max }}<br>
          BTC filter: {{ "ON" if btc_filter else "OFF" }} (RSI max {{ btc_rsi_max }}) | Groq: {{ groq_conf }}/10
        </div>
        <div class="col-md-4">
          <strong>Risk / Sizing</strong><br>
          TP: +{{ tp_pct }}% | SL: -{{ sl_pct }}% | Trailing: {{ trailing_pct }}%<br>
          Max pos: {{ max_trades }} | Capitale max: {{ capital_pct }}%<br>
          Size: {{ size_high }}% / {{ size_med }}% / {{ size_min }}%
        </div>
        <div class="col-md-4">
          <strong>Sistema</strong><br>
          Coppie: {{ pairs }} | Scan: {{ scan_interval }}min<br>
          Circuit breaker: {{ cb_eur }}€/giorno<br>
          Fee: {{ fee_pct }}%/lato | Groq: {{ groq_model }}
        </div>
      </div>
    </div>
  </div>

</div>

{% if pnl_chart_labels %}
<script>
new Chart(document.getElementById('pnlChart'), {
  type: 'line',
  data: {
    labels: {{ pnl_chart_labels | tojson }},
    datasets: [{
      label: 'P&L €',
      data: {{ pnl_chart_data | tojson }},
      borderColor: '#4caf82',
      backgroundColor: 'rgba(76,175,130,0.1)',
      fill: true,
      tension: 0.3,
      pointRadius: 3,
    }]
  },
  options: {
    plugins: { legend: { labels: { color: '#ccc' } } },
    scales: {
      x: { ticks: { color: '#888', maxTicksLimit: 8 }, grid: { color: '#2a2d3e' } },
      y: { ticks: { color: '#888' }, grid: { color: '#2a2d3e' } }
    }
  }
});

new Chart(document.getElementById('outcomeChart'), {
  type: 'doughnut',
  data: {
    labels: ['TP ✅', 'SL 🛑', 'Manuale 🔧'],
    datasets: [{
      data: {{ outcome_data | tojson }},
      backgroundColor: ['#4caf82', '#e05c5c', '#7a7a9a'],
    }]
  },
  options: { plugins: { legend: { labels: { color: '#ccc', font: { size: 11 } } } } }
});

new Chart(document.getElementById('groqChart'), {
  type: 'doughnut',
  data: {
    labels: ['Approvati ✅', 'Rifiutati ❌'],
    datasets: [{
      data: [{{ groq_approved }}, {{ groq_rejected }}],
      backgroundColor: ['#4caf82', '#e05c5c'],
    }]
  },
  options: { plugins: { legend: { labels: { color: '#ccc', font: { size: 11 } } } } }
});
</script>
{% endif %}
<script>
function loadBalance() {
  var el = document.getElementById('balance-result');
  el.innerHTML = '<span style="color:#aaa">Caricamento...</span>';
  fetch('/api/balance')
    .then(function(r){ return r.json(); })
    .then(function(d){
      if (!d.ok) { el.innerHTML = '<span style="color:#e05c5c">Errore: ' + d.error + '</span>'; return; }
      var txt = '<span style="color:#4caf82">EUR: ' + d.eur.toFixed(2) + '€</span>';
      var keys = Object.keys(d.crypto);
      if (keys.length) {
        txt += '<span style="color:#aaa;margin-left:8px;">';
        keys.forEach(function(k){ txt += k + ': ' + parseFloat(d.crypto[k]).toFixed(4) + '  '; });
        txt += '</span>';
      }
      el.innerHTML = txt;
    })
    .catch(function(){ el.innerHTML = '<span style="color:#e05c5c">Connessione fallita</span>'; });
}
</script>
</body>
</html>
"""


# ── Route ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    msg = request.args.get("msg", "")
    positions_raw = _load_json(POSITIONS_FILE, {})
    trades_log    = _load_json(TRADES_LOG_FILE, [])
    groq_log      = _load_json(GROQ_LOG_FILE, [])
    discovery_data = _load_json(DISCOVERY_FILE, None)
    kraken_status  = _fetch_kraken_status()
    fear_greed     = market_data.get_fear_greed()
    news_headlines = market_data.get_crypto_news(5) if config.NEWS_ENABLED else []
    mempool        = market_data.get_btc_mempool() if config.ONCHAIN_ENABLED else None

    # Circuit breaker check
    today_ts   = time.time() - 86400
    daily_loss = sum(t["pnl_eur"] for t in trades_log if t.get("closed_at", 0) > today_ts and t["pnl_eur"] < 0)
    cb_active  = config.CIRCUIT_BREAKER_ENABLED and abs(daily_loss) >= config.CIRCUIT_BREAKER_LOSS_EUR

    # Posizioni arricchite con prezzi live (P&L corretto per long e short)
    enriched = []
    for pair, pos in positions_raw.items():
        price = _fetch_price(pair)
        side  = pos.get("side", "long")
        if price:
            if side == "short":
                pnl_pct = (pos["entry"] - price) / pos["entry"] * 100
                pnl_eur = (pos["entry"] - price) * pos["size_crypto"]
            else:
                pnl_pct = (price - pos["entry"]) / pos["entry"] * 100
                pnl_eur = (price - pos["entry"]) * pos["size_crypto"]
        else:
            pnl_pct = pnl_eur = 0.0
        enriched.append({
            "pair":      pair,
            "side":      side,
            "entry":     pos["entry"],
            "price":     price,
            "pnl_pct":   pnl_pct,
            "pnl_eur":   pnl_eur,
            "tp":        pos["take_profit"],
            "sl":        pos["stop_loss"],
            "trailing":  pos.get("trailing_stop"),
            "adx":       pos.get("adx"),
            "size_eur":  pos["size_eur"],
            "opened_at": _fmt_time(pos["opened_at"]),
        })

    # Metriche
    total_pnl    = sum(t["pnl_eur"] for t in trades_log)
    total_fees   = sum(t.get("fee_eur", 0.0) for t in trades_log)
    wins         = sum(1 for t in trades_log if t["pnl_eur"] > 0)
    total_trades = len(trades_log)
    winrate      = (wins / total_trades * 100) if total_trades else 0.0
    groq_approved = sum(1 for g in groq_log if g["decision"] == "ESEGUI")
    groq_rejected = sum(1 for g in groq_log if g["decision"] == "RIFIUTA")

    # P&L cumulativo
    pnl_cum = 0.0
    pnl_labels, pnl_data = [], []
    for t in trades_log:
        pnl_cum += t["pnl_eur"]
        pnl_labels.append(_fmt_time(t["closed_at"]))
        pnl_data.append(round(pnl_cum, 4))

    outcome_data = [
        sum(1 for t in trades_log if t["exit_reason"] == "tp"),
        sum(1 for t in trades_log if t["exit_reason"] == "sl"),
        sum(1 for t in trades_log if t["exit_reason"] == "manual"),
    ]

    btc_rsi = None
    for g in reversed(groq_log):
        if g.get("pair") == config.BTC_PAIR:
            btc_rsi = g.get("rsi_1h")
            break

    btc_s = _parse_btc_filter()

    _MSG_LABELS = {
        "profile_changed": "Profilo aggiornato",
        "scan_queued":     "Scan in coda — eseguito entro 15s",
        "discover_queued": "Discovery avviata",
        "close_queued":    "Chiusura in coda — entro 15s",
        "error":           "Errore",
        "unknown_profile": "Profilo non valido",
        "no_pair":         "Coppia non specificata",
    }

    return render_template_string(
        TEMPLATE,
        positions=enriched,
        trades=list(reversed(trades_log)),
        groq_log=list(reversed(groq_log[-40:])),
        kraken_status=kraken_status,
        total_pnl=total_pnl,
        total_fees=total_fees,
        winrate=winrate,
        total_trades=total_trades,
        groq_approved=groq_approved,
        groq_rejected=groq_rejected,
        pnl_chart_labels=pnl_labels,
        pnl_chart_data=pnl_data,
        outcome_data=outcome_data,
        paper=config.PAPER_TRADING,
        max_trades=config.MAX_OPEN_TRADES,
        pairs=", ".join(config.PAIRS),
        scan_interval=config.SCAN_INTERVAL_MINUTES,
        tp_pct=f"{config.TAKE_PROFIT_PCT*100:.0f}",
        sl_pct=f"{config.STOP_LOSS_PCT*100:.0f}",
        fee_pct=f"{config.KRAKEN_TAKER_FEE*100:.2f}",
        groq_model=config.GROQ_MODEL,
        min_score=config.MIN_SIGNAL_SCORE,
        vol_mult=config.VOLUME_SPIKE_MULT,
        rsi_1h_max=config.RSI_1H_BUY_MAX,
        rsi_15m_max=config.RSI_15M_BUY_MAX,
        adx_min=config.ADX_MIN,
        trailing_pct=f"{config.TRAILING_STOP_PCT*100:.0f}",
        btc_filter=config.BTC_FILTER_ENABLED,
        btc_rsi_max=config.BTC_RSI_MAX,
        btc_rsi=btc_rsi,
        groq_conf=config.GROQ_MIN_CONFIDENCE,
        corr_groups=" | ".join(["+".join(g) for g in config.CORR_GROUPS]),
        discovery=discovery_data,
        fear_greed=fear_greed,
        news_headlines=news_headlines,
        mempool=mempool,
        circuit_breaker_active=cb_active,
        daily_loss=abs(daily_loss),
        btc_s=btc_s,
        btc_ema_filter=config.BTC_EMA_FILTER,
        now=datetime.now().strftime("%H:%M:%S"),
        fmt_time=_fmt_time,
        active_profile=config.ACTIVE_PROFILE,
        capital_pct=f"{config.MAX_CAPITAL_DEPLOYED*100:.0f}",
        size_high=f"{config.AI_SIZE_HIGH*100:.0f}",
        size_med=f"{config.AI_SIZE_MED*100:.0f}",
        size_min=f"{config.AI_SIZE_MIN*100:.0f}",
        cb_eur=f"{config.CIRCUIT_BREAKER_LOSS_EUR:.0f}",
        msg=msg,
        msg_label=_MSG_LABELS.get(msg, msg),
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8501, debug=False)
