#!/usr/bin/env bash
# deploy_raspberry.sh — Setup e deploy del Kraken Trading Bot su Raspberry Pi
# Esegui con: bash deploy_raspberry.sh
set -euo pipefail

# ── Colori ────────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'

ok()   { echo -e "${GREEN}[OK]${NC} $*"; }
info() { echo -e "${BLUE}[..] ${NC}$*"; }
warn() { echo -e "${YELLOW}[!!]${NC} $*"; }
fail() { echo -e "${RED}[ERR]${NC} $*"; exit 1; }

echo -e "${BOLD}"
echo "╔═══════════════════════════════════════════════════╗"
echo "║   Kraken Trading Bot — Deploy Raspberry Pi        ║"
echo "╚═══════════════════════════════════════════════════╝"
echo -e "${NC}"

# ── Configurazione ────────────────────────────────────────────────────────────
BOT_DIR="${BOT_DIR:-$HOME/kraken-trading-bot}"
VENV_DIR="$BOT_DIR/venv"
SERVICE_NAME="kraken-bot"
PYTHON_MIN="3.10"

# ── 1. Verifica sistema ───────────────────────────────────────────────────────
info "Verifica sistema..."

# Raspberry Pi / Linux
[[ "$(uname -s)" == "Linux" ]] || fail "Questo script richiede Linux (Raspberry Pi OS)."

# Python 3.10+
PYTHON_BIN=""
for candidate in python3.12 python3.11 python3.10 python3; do
    if command -v "$candidate" &>/dev/null; then
        ver=$("$candidate" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
        major=${ver%.*}; minor=${ver#*.}
        if [[ $major -ge 3 && $minor -ge 10 ]]; then
            PYTHON_BIN="$candidate"
            break
        fi
    fi
done

if [[ -z "$PYTHON_BIN" ]]; then
    warn "Python 3.10+ non trovato. Installo..."
    sudo apt-get update -qq
    sudo apt-get install -y python3 python3-pip python3-venv
    PYTHON_BIN="python3"
fi
ok "Python: $($PYTHON_BIN --version)"

# ── 2. Dipendenze di sistema ──────────────────────────────────────────────────
info "Installo dipendenze di sistema..."
sudo apt-get update -qq
sudo apt-get install -y --no-install-recommends \
    git curl ca-certificates python3-venv python3-pip 2>/dev/null
ok "Dipendenze di sistema OK"

# ── 3. Sorgente del codice ────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}Come vuoi installare il bot?${NC}"
echo "  1) Clona da GitHub (inserisci URL)"
echo "  2) Il codice è già in questa directory (copia locale)"
read -rp "Scelta [1/2]: " SRC_CHOICE

if [[ "$SRC_CHOICE" == "1" ]]; then
    read -rp "URL repository Git: " GIT_URL
    if [[ -d "$BOT_DIR/.git" ]]; then
        info "Repository già presente, aggiorno..."
        git -C "$BOT_DIR" pull --ff-only
    else
        info "Clono repository in $BOT_DIR..."
        git clone "$GIT_URL" "$BOT_DIR"
    fi
    ok "Codice aggiornato"
else
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    if [[ "$SCRIPT_DIR" != "$BOT_DIR" ]]; then
        info "Copio i file da $SCRIPT_DIR a $BOT_DIR..."
        mkdir -p "$BOT_DIR"
        rsync -a --exclude='venv/' --exclude='__pycache__/' \
              --exclude='*.pyc' --exclude='logs/' \
              "$SCRIPT_DIR/" "$BOT_DIR/"
        ok "File copiati in $BOT_DIR"
    else
        ok "Già nella directory corretta: $BOT_DIR"
    fi
fi

# ── 4. Virtualenv e dipendenze Python ────────────────────────────────────────
info "Creo virtual environment..."
"$PYTHON_BIN" -m venv "$VENV_DIR"
PYTHON="$VENV_DIR/bin/python"
PIP="$VENV_DIR/bin/pip"

info "Aggiorno pip..."
"$PIP" install --upgrade pip --quiet

info "Installo dipendenze Python..."
"$PIP" install -r "$BOT_DIR/requirements.txt" --quiet
ok "Dipendenze Python installate"

# ── 5. Configurazione .env ────────────────────────────────────────────────────
ENV_FILE="$BOT_DIR/.env"

if [[ -f "$ENV_FILE" ]]; then
    warn ".env già presente — non sovrascrivo. Edita manualmente se necessario:"
    warn "  nano $ENV_FILE"
else
    info "Configuro .env..."
    cp "$BOT_DIR/.env.example" "$ENV_FILE"

    echo ""
    echo -e "${BOLD}Inserisci le credenziali (lascia vuoto per compilare dopo):${NC}"

    read -rp "  TELEGRAM_TOKEN: "   TG_TOKEN
    read -rp "  TELEGRAM_CHAT_ID: " TG_CHAT
    read -rp "  GROQ_API_KEY: "     GROQ_KEY
    read -rp "  KRAKEN_API_KEY: "   KRK_KEY
    read -rp "  KRAKEN_API_SECRET: " KRK_SECRET

    [[ -n "$TG_TOKEN"   ]] && sed -i "s|^TELEGRAM_TOKEN=.*|TELEGRAM_TOKEN=$TG_TOKEN|"     "$ENV_FILE"
    [[ -n "$TG_CHAT"    ]] && sed -i "s|^TELEGRAM_CHAT_ID=.*|TELEGRAM_CHAT_ID=$TG_CHAT|" "$ENV_FILE"
    [[ -n "$GROQ_KEY"   ]] && sed -i "s|^GROQ_API_KEY=.*|GROQ_API_KEY=$GROQ_KEY|"         "$ENV_FILE"
    [[ -n "$KRK_KEY"    ]] && sed -i "s|^KRAKEN_API_KEY=.*|KRAKEN_API_KEY=$KRK_KEY|"       "$ENV_FILE"
    [[ -n "$KRK_SECRET" ]] && sed -i "s|^KRAKEN_API_SECRET=.*|KRAKEN_API_SECRET=$KRK_SECRET|" "$ENV_FILE"

    # Aggiusta il path Python nel .env
    sed -i "s|^BOT_DIR=.*|BOT_DIR=$BOT_DIR|"       "$ENV_FILE"
    sed -i "s|^BOT_PYTHON=.*|BOT_PYTHON=$PYTHON|"   "$ENV_FILE"

    chmod 600 "$ENV_FILE"
    ok ".env creato (permessi 600)"
fi

# ── 6. Directory log ──────────────────────────────────────────────────────────
mkdir -p "$BOT_DIR/logs"
ok "Directory logs OK"

# ── 7. Servizio systemd ───────────────────────────────────────────────────────
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

info "Creo servizio systemd: $SERVICE_NAME..."

sudo tee "$SERVICE_FILE" > /dev/null <<EOF
[Unit]
Description=Kraken Trading Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$BOT_DIR
ExecStart=$PYTHON $BOT_DIR/main.py
Restart=on-failure
RestartSec=30
StandardOutput=append:$BOT_DIR/logs/bot.log
StandardError=append:$BOT_DIR/logs/bot.log
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"
ok "Servizio systemd creato e abilitato"

# ── 8. Logrotate ─────────────────────────────────────────────────────────────
info "Configuro rotazione log..."
sudo tee "/etc/logrotate.d/$SERVICE_NAME" > /dev/null <<EOF
$BOT_DIR/logs/*.log {
    daily
    rotate 7
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
}
EOF
ok "Logrotate configurato (7 giorni)"

# ── 9. Riepilogo finale ───────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}${GREEN}╔═══════════════════════════════════════════════════╗"
echo       "║   Deploy completato!                              ║"
echo -e    "╚═══════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "${BOLD}Directory bot:${NC} $BOT_DIR"
echo -e "${BOLD}Python:${NC}       $PYTHON"
echo -e "${BOLD}Servizio:${NC}     $SERVICE_NAME"
echo ""
echo -e "${BOLD}Comandi utili:${NC}"
echo "  Avvia bot:        sudo systemctl start $SERVICE_NAME"
echo "  Ferma bot:        sudo systemctl stop $SERVICE_NAME"
echo "  Stato:            sudo systemctl status $SERVICE_NAME"
echo "  Log in tempo reale: journalctl -u $SERVICE_NAME -f"
echo "  Log file:         tail -f $BOT_DIR/logs/bot.log"
echo ""

# Controlla se .env è compilato prima di avviare
MISSING=()
ENV_CONTENT=$(cat "$ENV_FILE")
[[ "$ENV_CONTENT" =~ "TELEGRAM_TOKEN=" && ! "$ENV_CONTENT" =~ "TELEGRAM_TOKEN=$" ]] || MISSING+=("TELEGRAM_TOKEN")
[[ "$ENV_CONTENT" =~ "GROQ_API_KEY=" && ! "$ENV_CONTENT" =~ "GROQ_API_KEY=$" ]]     || MISSING+=("GROQ_API_KEY")

if [[ ${#MISSING[@]} -gt 0 ]]; then
    warn "Alcune credenziali sono ancora vuote. Completa il .env prima di avviare:"
    warn "  nano $ENV_FILE"
    warn "  sudo systemctl start $SERVICE_NAME"
else
    read -rp "Avvio il bot adesso? [s/N]: " START_NOW
    if [[ "$START_NOW" =~ ^[sSyY]$ ]]; then
        sudo systemctl start "$SERVICE_NAME"
        sleep 2
        sudo systemctl status "$SERVICE_NAME" --no-pager -l
    else
        info "Avvia manualmente con: sudo systemctl start $SERVICE_NAME"
    fi
fi
