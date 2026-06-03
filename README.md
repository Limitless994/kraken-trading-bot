# Kraken Trading Bot

Bot di trading algoritmico per coppie crypto/EUR su Kraken, con segnali tecnici multi-timeframe, valutazione AI tramite Groq, notifiche e controllo via Telegram.

## Funzionalità principali

- **Long e Short**: apre posizioni spot (BUY) e a margine (SELL/SHORT) in base alle condizioni di mercato
- **Analisi tecnica multi-timeframe**: RSI su 15m/1h/4h/Daily, MACD, EMA50, ADX, ATR, volume
- **Valutazione AI**: ogni segnale viene valutato da un LLM (Groq) che assegna uno score 1-10 e decide la size
- **Trailing stop**: adatta lo stop loss al movimento del prezzo (sia per long che per short)
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
| Fear & Greed | F&G non in extreme greed (> 80) — extreme fear è FAVOREVOLE per shorts |

**Scoring (6 punti max, stessa soglia):**
| Condizione | Punti |
|---|---|
| RSI in zona bearish (sopra RSI_SHORT_MIN) | 1 |
| MACD crossover ribassista o falling below zero | 1 |
| Volume spike | 1 |
| Prezzo < EMA50 (1h) | 1 |
| Order book bearish (bid/ask < 0.45) | 1 |
| ADX >= 30 | 1 |

**TP/SL invertiti**: `TP = entry − ATR_TP_MULT × ATR` · `SL = entry + ATR_SL_MULT × ATR`
**Trailing stop**: scende col prezzo (si aggiorna solo quando il nuovo livello è più basso del precedente)

---

## Profili di rischio

Cambio a runtime: `/profile aggressive`

| Parametro | Conservative | Moderate | Aggressive |
|---|---|---|---|
| MAX_OPEN_TRADES | 4 | 5 | 6 |
| MAX_CAPITAL_DEPLOYED | 80% | 87% | 95% |
| ATR TP/SL mult | 2.5× / 1.0× | 3.2× / 1.2× | 4.0× / 1.5× |
| AI_SIZE_HIGH | 25% | 35% | 45% |
| GROQ_MIN_CONFIDENCE | 6/10 | 6/10 | 5/10 |
| MIN_SIGNAL_SCORE | 3/6 | 2/6 | 2/6 |
| CIRCUIT_BREAKER | €15 | €22 | €30 |
| FEAR_GREED range | 20–80 | 15–85 | 10–90 |

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
GROQ_MODEL=llama3-8b-8192

# Opzionali
BUDGET_EUR=100
PAPER_TRADING=true          # true = nessun ordine reale
TRADING_PROFILE=conservative
ATR_TP_MULT=2.5
ATR_SL_MULT=1.0

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

| File | Contenuto |
|---|---|
| `positions.json` | Posizioni aperte |
| `trades.json` | Storico trade chiusi |
| `active_profile.json` | Profilo attivo |
| `dynamic_pairs.json` | Coppie aggiornate dall'ultima discovery |
| `logs/bot.log` | Log principale |
| `logs/trading.log` | Scan e ordini |
| `logs/groq.log` | Decisioni AI |
| `logs/discovery.log` | Discovery giornaliera |
| `logs/kraken.log` | Chiamate API Kraken |

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
