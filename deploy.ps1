# ── SSH config ────────────────────────────────────────────────────────────────
$NAS  = "limitless@192.168.0.200"
$PORT = "2299"
$KEY  = "$env:USERPROFILE\.ssh\nas_trading"

# ── Local source (directory di questo script) ─────────────────────────────────
$SRC = Split-Path -Parent $MyInvocation.MyCommand.Definition

# ── Legge BOT_DIR e BOT_PYTHON da .env (stessi valori usati da start.sh) ──────
$BOT_DIR    = "~/kraken-trading-bot"
$BOT_PYTHON = "~/venv_trading/bin/python"

$ENV_FILE = Join-Path $SRC ".env"
if (Test-Path $ENV_FILE) {
    Get-Content $ENV_FILE | ForEach-Object {
        if ($_ -match '^\s*BOT_DIR\s*=\s*(.+?)\s*$')    { $BOT_DIR    = $Matches[1] }
        if ($_ -match '^\s*BOT_PYTHON\s*=\s*(.+?)\s*$') { $BOT_PYTHON = $Matches[1] }
    }
}

Write-Host "Deploy verso $NAS`:$BOT_DIR" -ForegroundColor Cyan

# ── File e directory da copiare ───────────────────────────────────────────────
$files = @("main.py", "dashboard.py", "tests.py", "start.sh", "requirements.txt")
$dirs  = @("core", "bot", "services")

# Crea le directory sul NAS
& ssh -p $PORT -i $KEY $NAS "mkdir -p '$BOT_DIR/core' '$BOT_DIR/bot' '$BOT_DIR/services' '$BOT_DIR/logs'"

# Copia file singoli
foreach ($f in $files) {
    Write-Host "  $f" -ForegroundColor Gray
    & scp -O -P $PORT -i $KEY "$SRC\$f" "${NAS}:${BOT_DIR}/${f}"
    if ($LASTEXITCODE -ne 0) { Write-Host "  ERRORE su $f" -ForegroundColor Red }
}

# Copia directory (ricorsivo)
foreach ($d in $dirs) {
    Write-Host "  $d/" -ForegroundColor Gray
    & scp -O -r -P $PORT -i $KEY "$SRC\$d" "${NAS}:${BOT_DIR}/"
    if ($LASTEXITCODE -ne 0) { Write-Host "  ERRORE su $d/" -ForegroundColor Red }
}

# ── Dipendenze Python ─────────────────────────────────────────────────────────
Write-Host "Aggiorno dipendenze Python sul NAS..." -ForegroundColor Cyan
& ssh -p $PORT -i $KEY $NAS "'$BOT_PYTHON' -m pip install -q --upgrade pip && '$BOT_PYTHON' -m pip install -q -r '$BOT_DIR/requirements.txt'"

# ── Riavvio ───────────────────────────────────────────────────────────────────
Write-Host "Riavvio bot e dashboard..." -ForegroundColor Cyan
& ssh -p $PORT -i $KEY $NAS "bash '$BOT_DIR/start.sh'"

Write-Host ""
Write-Host "Deploy completato." -ForegroundColor Green
Write-Host "Dashboard: http://192.168.0.200:8501" -ForegroundColor Yellow
