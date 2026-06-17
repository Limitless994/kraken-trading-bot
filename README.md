# Kraken Trading Bot

Bot di trading algoritmico per coppie crypto/EUR su Kraken, con segnali tecnici multi-timeframe, valutazione AI tramite Groq, notifiche e controllo via Telegram.

## Funzionalità principali

- **Long e Short**: apre posizioni spot (BUY) e a margine (SELL/SHORT) in base alle condizioni di mercato
- **Analisi tecnica multi-timeframe**: RSI su 15m/1h/4h/Daily, MACD, EMA50, ADX, ATR, volume
- **Valutazione AI**: ogni segnale viene valutato da un LLM (Groq) che assegna uno score 1-10 e decide la size
- **Trailing stop**: si attiva solo dopo che il trade è in profitto di almeno `TRAILING_ACTIVATION_ATR × ATR`; la distanza è configurabile separatamente (`TRAILING_DISTANCE_ATR`)
- **Paper trading**: modalità simulata senza eseguire ordini reali
- **Profili di rischio**: conservative / moderate / aggressive, cambiabili a runtime via Telegram
- **Discovery giornaliera**: scansione automatica di tutte le coppie EUR su Kraken per trovare opportunità
- **Circuit breaker**: blocca automaticamente il trading se le perdite giornaliere superano una soglia
- **Dashboard web**: interfaccia Flask su `http://localhost:8501`

---

## Architettura

```
main.py                      # Entry point, logging, crash handler
dashboard.py                 # Dashboard web (Flask, porta 8501)
core/
  config.py                  # Tutti i parametri + sistema profili
  prompts.py                 # Prompt Groq (editabili senza toccare il codice)
  kraken_client.py           # Wrapper API Kraken (public + private)
  analyzer.py                # Calcolo indicatori (RSI, MACD, EMA, ADX, ATR)
  strategy.py                # Generazione segnali LONG e SHORT
  market_data.py             # Fear & Greed, funding rates, news, mempool
services/
  groq_evaluator.py          # Valutazione LLM del segnale
  discovery.py               # Scansione giornaliera di nuove coppie
bot/
  telegram_bot.py            # Loop principale, scan, ordini, comandi Telegram
```

### Flusso di una scansione

```
ogni 15 minuti
    ↓
Fear & Greed check (separato per LONG e SHORT)
    ↓
per ogni coppia (PAIRS + dynamic_pairs.json):
    analyze() → RSI, MACD, EMA, ADX, ATR, volume, order book
        ↓
    generate_signal() [LONG]  →  gate: daily EMA50, ADX, BTC filter, correlazione, score
    generate_short_signal() [SHORT] → gate: daily EMA50 (invertito), ADX, BTC downtrend, RSI non oversold
        ↓
    Groq evaluate() → score 1-10, size %
        ↓
    add_order() su Kraken  (o PAPER se PAPER_TRADING=true)
        ↓
ogni 60 secondi: monitor posizioni → trailing stop, TP, SL → close se necessario
```

---

## Segnali LONG

**Gate hard (uno solo blocca tutto):**
| Gate | Condizione |
|---|---|
| Daily EMA50 | Prezzo deve essere SOPRA EMA50 daily |
| ADX | ADX >= ADX_MIN (default 20) |
| BTC filter | BTC RSI non surriscaldato + BTC in uptrend |
| Correlazione | Nessuna coppia correlata già aperta |
| Fear & Greed | F&G non in extreme fear (< 20) né extreme greed (> 80) |

**Scoring (6 punti max, soglia = MIN_SIGNAL_SCORE):**
| Condizione | Punti |
|---|---|
| RSI 1h + 15m + 4h tutti sotto soglia | 1 |
| MACD crossover rialzista o rising above zero | 1 |
| Volume spike >= VOLUME_SPIKE_MULT | 1 |
| Prezzo > EMA50 (1h) | 1 |
| Order book bullish (bid/ask > 0.55) | 1 |
| ADX >= 30 | 1 |

**TP/SL**: `TP = entry + ATR_TP_MULT × ATR` · `SL = entry − ATR_SL_MULT × ATR` (R:R ≈ 2.5:1)

---

## Segnali SHORT (margin, leverage 2x)

**Gate hard:**
| Gate | Condizione |
|---|---|
| Daily EMA50 | Prezzo deve essere SOTTO EMA50 daily |
| ADX | ADX >= ADX_MIN |
| BTC filter (invertito) | BTC deve essere in DOWNTREND, RSI BTC non già oversold |
| Correlazione | Nessuna coppia correlata già aperta |
| RSI non oversold | RSI 1h >= RSI_SHORT_MIN (non shortare in capitolazione) |
| Fear & Greed | F&G non in extreme greed (> FEAR_GREED_MAX) né extreme fear (< FEAR_GREED_SHORT_MIN, default 20) — extreme fear blocca gli short (rischio rimbalzo da capitolazione) |
| BTC-beta bucket | Esposizione short totale < MAX_BTC_BETA_EXPOSURE_PCT × BUDGET_EUR (default 60%) |

**Scoring (6 punti max, stessa soglia):**
| Condizione | Punti |
|---|---|
| RSI in zona bearish (sopra RSI_SHORT_MIN) | 1 |
| MACD crossover ribassista o falling below zero | 1 |
| Volume spike | 1 |
| Prezzo < EMA50 (1h) | 1 |
| Order book bearish (bid/ask < 0.45) | 1 |
| ADX >= 30 | 1 |

**TP/SL invertiti**: `TP = entry − ATR_TP_MULT_SHORT × ATR` · `SL = entry + ATR_SL_MULT_SHORT × ATR` (default 2.5× / 1.3×, più largo dei long)
**Size massima**: cappata a `AI_SIZE_MED` (più piccola dei long) per contenere il rischio illimitato degli short
**Funding cost**: il rollover Kraken (0.02%/4h) viene sottratto dal P&L netto a chiusura
**Trailing stop**: attivazione con soglia `TRAILING_ACTIVATION_ATR`; scende col prezzo solo quando il trade è già in profitto

---

## Profili di rischio

Cambio a runtime: `/profile aggressive`

| Parametro | Conservative | Moderate | Aggressive |
|---|---|---|---|
| MAX_OPEN_TRADES | 4 | 5 | 6 |
| MAX_CAPITAL_DEPLOYED | 80% | 87% | 95% |
| ATR TP/SL long | 2.5× / 1.0× | 3.2× / 1.2× | 4.0× / 1.5× |
| ATR TP/SL short | 2.5× / 1.3× | 2.8× / 1.4× | 3.5× / 1.6× |
| AI_SIZE_HIGH (long) | 25% | 35% | 45% |
| GROQ_MIN_CONFIDENCE | 6/10 | 6/10 | 5/10 |
| MIN_SIGNAL_SCORE | 3/6 | 2/6 | 2/6 |
| CIRCUIT_BREAKER | €15 | €22 | €30 |
| FEAR_GREED long | 20–80 | 15–85 | 10–90 |
| FEAR_GREED_SHORT_MIN | 20 | 15 | 10 |
| TRAILING_ACTIVATION_ATR | 1.0× | 0.8× | 0.7× |

Il profilo attivo viene salvato in `active_profile.json` e ripristinato al riavvio.

---

## Comandi Telegram

| Comando | Descrizione |
|---|---|
| `/status` | Stato bot, profilo, F&G, capitale |
| `/positions` | Posizioni aperte con P&L live |
| `/history` | Ultimi trade chiusi |
| `/scan` | Avvia scansione manuale immediata |
| `/discover` | Avvia discovery di nuove coppie |
| `/close <ID>` | Chiude una posizione manualmente |
| `/profile` | Mostra profilo attivo |
| `/profile <nome>` | Cambia profilo (conservative/moderate/aggressive) |
| `/pause` / `/resume` | Sospende/riprende lo scan automatico |

I comandi sono accettati solo dalla chat indicata in `TELEGRAM_CHAT_ID`. Messaggi da altri chat vengono ignorati: chiunque ottenga il token del bot non può comandarlo.

---

## Setup

### Prerequisiti

- Python 3.10+ (o Docker)
- Account Kraken con API key (permessi: Query Funds, Create & Modify Orders, Cancel & Close Orders — **NON** Withdraw)
- Bot Telegram (via @BotFather)
- API key Groq gratuita (https://console.groq.com/keys)

### Installazione locale

```bash
git clone <repo>
cd kraken-trading-bot
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
# Compila .env con le tue credenziali
python main.py
```

### Docker

```bash
cp .env.example .env
# Compila .env con le tue credenziali
docker compose up -d
```

Il bot e la dashboard partono come servizi separati. I log e lo stato persistono nei volumi montati.

### File .env

```env
KRAKEN_API_KEY=your_key
KRAKEN_API_SECRET=your_secret
TELEGRAM_TOKEN=123456789:ABCdef...
TELEGRAM_CHAT_ID=987654321
GROQ_API_KEY=gsk_your_key
GROQ_MODEL=llama-3.3-70b-versatile

# Opzionali
BUDGET_EUR=100
PAPER_TRADING=true          # true = nessun ordine reale
TRADING_PROFILE=conservative
ATR_TP_MULT=2.5
ATR_SL_MULT=1.0
ATR_TP_MULT_SHORT=2.5
ATR_SL_MULT_SHORT=1.3
TRAILING_ACTIVATION_ATR=1.0
TRAILING_DISTANCE_ATR=1.5
FEAR_GREED_SHORT_MIN=20
MAX_BTC_BETA_EXPOSURE_PCT=0.60

# Solo per deploy su NAS/server remoto (non committare)
BOT_DIR=/volume1/homes/youruser/kraken-trading-bot
BOT_PYTHON=/volume1/homes/youruser/venv_trading/bin/python
```

### Deploy su NAS Synology

`deploy.ps1` legge `BOT_DIR` e `BOT_PYTHON` dal `.env` locale, poi copia tutti i file sul NAS via SCP e riavvia il bot via SSH.

```powershell
.\deploy.ps1
```

`start.sh` si occupa di:
1. Terminare i processi precedenti tramite PID file
2. Avviare il bot in un loop auto-restart (riavvio dopo 30s in caso di crash)
3. Avviare la dashboard

Per la discovery automatica, aggiungere un task nel DSM Task Scheduler:
```
$BOT_PYTHON "$BOT_DIR/services/discovery.py"
```
(sostituendo `$BOT_PYTHON` e `$BOT_DIR` con i valori del tuo `.env`)

### Deploy su Raspberry Pi

`deploy_raspberry.sh` automatizza l'installazione completa su Raspberry Pi OS (o qualsiasi distro Debian/Ubuntu ARM).

**Dal PC Windows, copia il progetto sul Raspberry:**

```powershell
scp -r kraken-trading-bot pi@<IP_RASPBERRY>:~/
```

**Sul Raspberry (via SSH), esegui lo script:**

```bash
ssh pi@<IP_RASPBERRY>
bash ~/kraken-trading-bot/deploy_raspberry.sh
```

Lo script si occupa di:
1. Verificare Python 3.10+ (lo installa se mancante)
2. Creare il virtual environment e installare le dipendenze
3. Guidare la configurazione del `.env` in modo interattivo
4. Creare un servizio **systemd** per l'avvio automatico al boot
5. Configurare **logrotate** (rotazione log giornaliera, 7 giorni)

**Comandi utili dopo il deploy:**

```bash
sudo systemctl start kraken-bot      # avvia
sudo systemctl stop kraken-bot       # ferma
sudo systemctl status kraken-bot     # stato
journalctl -u kraken-bot -f          # log in tempo reale
```

Il bot si riavvia automaticamente in caso di crash (dopo 30 secondi) e ad ogni reboot del Raspberry.

---

## Test

```bash
python tests.py
# oppure
python -m pytest tests.py -v
```

Tutti i test usano mock — nessuna chiamata reale a Kraken, Groq o API esterne.

---

## File di stato (runtime)

Lo stato persistente vive in `trading.db` (SQLite, WAL mode). Al primo avvio i vecchi file JSON vengono importati e rinominati in `.bak`.

| Storage | Contenuto |
|---|---|
| `trading.db` → `positions` | Posizioni aperte |
| `trading.db` → `trades` | Storico trade chiusi |
| `trading.db` → `groq_decisions` | Decisioni AI (per `/ai` e dashboard) |
| `trading.db` → `pending_orders` | Limit order in attesa di esecuzione |
| `trading.db` → `settings` | Profilo attivo + flag vari |
| `dynamic_pairs.json` | Coppie aggiornate dall'ultima discovery |
| `logs/bot.log` | Log principale |
| `logs/trading.log` | Scan e ordini |
| `logs/groq.log` | Decisioni AI |
| `logs/discovery.log` | Discovery giornaliera |
| `logs/kraken.log` | Chiamate API Kraken (incl. slippage di esecuzione su ordini market) |

---

## Discovery automatica

`services/discovery.py` analizza tutte le coppie EUR su Kraken:
1. Filtra per volume minimo 24h (€500.000)
2. Esegue un backtest walk-forward su candele 4h (90 giorni, 70% train / 30% test)
3. Calcola uno score meccanico (win rate, Sharpe, consistenza train/test, indicatori attuali)
4. Invia le top 8 candidate a Groq per analisi qualitativa
5. Salva le top 5 in `dynamic_pairs.json` — usate nelle scansioni successive

---

## Note sul paper trading

Con `PAPER_TRADING=true`:
- Gli ordini vengono simulati localmente (nessuna chiamata a Kraken `AddOrder`)
- Le posizioni sono tracciate in `positions.json` come `"TX": "PAPER"`
- Il P&L è calcolato sul prezzo live aggiornato ogni 60 secondi
- Utile per validare la strategia prima di passare al trading reale
