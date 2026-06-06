#!/bin/bash
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Load .env if present (sets BOT_DIR and BOT_PYTHON)
if [ -f "$SCRIPT_DIR/.env" ]; then
    set -a
    # shellcheck disable=SC1091
    source "$SCRIPT_DIR/.env"
    set +a
fi

PYTHON="${BOT_PYTHON:-$SCRIPT_DIR/venv/bin/python}"
DIR="${BOT_DIR:-$SCRIPT_DIR}"
LOOP_PID_FILE="$DIR/logs/bot_loop.pid"
DASH_PID_FILE="$DIR/logs/dashboard.pid"

mkdir -p "$DIR/logs"

# Kill previous loop (kill -0 checks if the process is still running)
if [ -f "$LOOP_PID_FILE" ]; then
    OLD_PID=$(cat "$LOOP_PID_FILE")
    if kill -0 "$OLD_PID" 2>/dev/null; then
        kill -9 "$OLD_PID" 2>/dev/null
        sleep 1
    fi
    rm -f "$LOOP_PID_FILE"
fi
if [ -f "$DASH_PID_FILE" ]; then
    OLD_PID=$(cat "$DASH_PID_FILE")
    if kill -0 "$OLD_PID" 2>/dev/null; then
        kill -9 "$OLD_PID" 2>/dev/null
    fi
    rm -f "$DASH_PID_FILE"
fi

# Kill any previous start.sh loop (bash process, excludes current script)
pkill -9 -f "bash.*start.sh" 2>/dev/null
# Kill bot Python processes (excludes start.sh itself)
pkill -9 -f "python.*kraken-trading-bot" 2>/dev/null
# Wait and verify all old processes are gone (prevents deploy-with-old-code-in-memory bug)
for i in 1 2 3 4 5; do
    sleep 1
    pgrep -f "python.*kraken-trading-bot" > /dev/null 2>&1 || break
    if [ "$i" -eq 5 ]; then
        pkill -9 -f "python.*kraken-trading-bot" 2>/dev/null
        sleep 2
    fi
done

# Auto-restart loop with PID file
(
    while true; do
        "$PYTHON" "$DIR/main.py" >> "$DIR/logs/stdout_bot.log" 2>&1
        EXIT=$?
        echo "$(date '+%Y-%m-%d %H:%M:%S') bot exited (code $EXIT) — riavvio tra 30s" \
            >> "$DIR/logs/stdout_bot.log"
        sleep 30
    done
) > /dev/null 2>&1 &
echo $! > "$LOOP_PID_FILE"
disown

nohup "$PYTHON" "$DIR/dashboard.py" > "$DIR/logs/stdout_dashboard.log" 2>&1 &
echo $! > "$DASH_PID_FILE"
disown

echo "Bot e dashboard avviati."
echo "Dashboard: http://192.168.0.200:8501"
echo ""
echo "Log strutturati in $DIR/logs/"
echo "  bot.log       — log principale"
echo "  trading.log   — scan e ordini"
echo "  groq.log      — decisioni AI"
echo "  discovery.log — discovery giornaliera"
echo "  kraken.log    — API Kraken"
echo ""
echo "Discovery separata: aggiungi cron in DSM Task Scheduler:"
echo "  $PYTHON \"$DIR/services/discovery.py\""
