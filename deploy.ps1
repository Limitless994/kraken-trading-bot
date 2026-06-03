$NAS  = "limitless@192.168.0.200"
$PORT = "2299"
$KEY  = "C:\Users\Davide\.ssh\nas_trading"
$SRC  = "C:\Users\Davide\Desktop\trading yt"
$DST  = "~/tradingyt"

$files = @(
    "config.py",
    "main.py",
    "analyzer.py",
    "strategy.py",
    "groq_evaluator.py",
    "kraken_client.py",
    "market_data.py",
    "telegram_bot.py",
    "dashboard.py",
    "discovery.py",
    "start.sh"
)

Write-Host "Deploy in corso..." -ForegroundColor Cyan

foreach ($f in $files) {
    Write-Host "  Copio $f" -ForegroundColor Gray
    & scp -O -P $PORT -i $KEY "$SRC\$f" "${NAS}:${DST}/${f}"
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  ERRORE su $f" -ForegroundColor Red
    }
}

Write-Host "Aggiorno configurazione NAS..." -ForegroundColor Cyan
& ssh -p $PORT -i $KEY $NAS @'
sed -i 's/GROQ_MODEL=.*/GROQ_MODEL=llama-3.3-70b-versatile/' '/volume1/homes/limitless/trading yt/.env'
echo -n "GROQ_MODEL aggiornato: "; grep GROQ_MODEL '/volume1/homes/limitless/trading yt/.env'
'@

Write-Host "Aggiorno dipendenze Python sul NAS..." -ForegroundColor Cyan
& ssh -p $PORT -i $KEY $NAS @'
/volume1/homes/limitless/venv_trading/bin/pip install -q --upgrade httpx anyio sniffio
/volume1/homes/limitless/venv_trading/bin/pip install -q -r '/volume1/homes/limitless/trading yt/requirements.txt'
'@

Write-Host "Riavvio bot e dashboard sul NAS..." -ForegroundColor Cyan
& ssh -p $PORT -i $KEY $NAS @'
bash ~/tradingyt/start.sh
sleep 3
COUNT=$(ps aux | grep "venv_trading/bin/python" | grep "main.py" | grep -v grep | wc -l)
echo "Istanze main.py attive: $COUNT"
'@

Write-Host "Deploy completato." -ForegroundColor Green
Write-Host "Dashboard: http://192.168.0.200:8501" -ForegroundColor Yellow
