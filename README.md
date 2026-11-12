# Kraken Trading Bot

Bot di trading algoritmico con notifiche e controllo via Telegram. Due mercati, una sola strategia di default:

- **Crypto/EUR su Kraken** (paper o live)
- **Azioni/ETF USA su Alpaca** — **solo paper**, opzionale

## Strategie

| `TRADING_MODE` | Cosa fa | Stato |
|---|---|---|
| `trend` (default) | Trend-following daily, long-only: ingresso su breakout delle chiusure a 40 giorni sopra EMA50, uscita con stop chandelier (picco − 3×ATR20). Nessun take profit, nessuna AI. | Attiva |
| `momentum` | Strategia 1h storica: punteggio RSI/MACD/volume, valutazione Groq, short a margine con leva 5×. | Solo confronto: nei backtest perde in ogni finestra |

Le sezioni "Segnali LONG/SHORT", "Profili di rischio" e "Discovery" più sotto descrivono la modalità `momentum`.

### Cosa dice il backtest della trend mode

`python backtest_trend.py --grid` simula il portafoglio con le stesse funzioni del bot (`core/trend.py`), fill all'apertura del giorno dopo il segnale e costi dello 0,5% per lato. Sui dati crypto 2022-01 → 2026-06, 100€ di budget:

- netto circa **+128€**, contro **−17€** del buy-and-hold delle stesse coppie;
- **2 anni positivi su 5**: quasi tutto il risultato viene dal 2024;
- i 5 trade migliori valgono +170€, tutti gli altri insieme −42€;
- drawdown massimo circa **−74€** dal picco.

È il profilo tipico del trend-following: molte piccole perdite, pochi trade grandi. Non è una garanzia sul futuro, e le coppie sono state scelte col senno di poi.

### Azionario in paper (Alpaca)

Stesse regole trend su ETF USA liquidi (`STOCK_SYMBOLS`), con budget separato in dollari e tabelle separate nel database: non entra nei conti, nelle protezioni né nella dashboard della parte crypto.

1. Crea un account Alpaca gratuito e genera le chiavi **Paper Trading**.
2. Nel `.env`: `STOCKS_ENABLED=true`, `ALPACA_API_KEY`, `ALPACA_API_SECRET`.
3. `STOCK_EXECUTION=local` simula i fill nel bot; `alpaca_paper` invia gli ordini al conto paper di Alpaca.
4. Backtest: `python backtest_trend.py --market stocks --start 2018-01-01` (scarica i dati da Alpaca).
5. Su Telegram: `/stocks`.

Non esiste una modalità live: `AlpacaBroker` rifiuta qualunque host diverso dall'endpoint paper. Gli ordini partono solo a mercato aperto, sul segnale della chiusura precedente.

Gli ETF americani dell'universo di default (SPY, QQQ, …) vanno bene solo in paper: un cliente retail dell'UE non può comprarli dal vivo (regolamento PRIIPs). Il percorso verso un sistema azionario completo — dati, validazione, strategie, eventuale live — è in [docs/PIANO_AZIONARIO.md](docs/PIANO_AZIONARIO.md).

### Sicurezza

- `PAPER_TRADING` vale `true` anche se la riga manca dal `.env`: il live richiede `PAPER_TRADING=false` esplicito.
- La dashboard senza `DASHBOARD_PASSWORD` è in sola lettura; con la password richiede HTTP Basic auth.
- In trend mode una posizione viene aperta o chiusa solo su fill confermato dal broker.
- Non c'è uno stop piazzato sull'exchange: l'uscita è decisa dal bot sulla chiusura daily, quindi a bot spento la posizione non è protetta.

---

## Architettura

```
main.py                      # Entry point, logging, crash handler
dashboard.py                 # Dashboard web (Flask, porta 8501)
backtest_trend.py            # Backtest di portafoglio della trend mode (crypto e azionario)
core/
  config.py                  # Tutti i parametri + sistema profili
  trend.py                   # Regole trend: ingresso e uscita su candele daily completate
  trend_engine.py            # TrendTrader: scan, fill, uscite — uguale per ogni mercato
  broker.py                  # Interfaccia Broker, KrakenBroker, AlpacaBroker (solo paper)
  kraken_client.py           # Wrapper API Kraken (public + private)
  db.py                      # SQLite: posizioni, trade, impostazioni (crypto e azionario separati)
  prompts.py                 # [momentum] Prompt Groq
  analyzer.py                # [momentum] Indicatori (RSI, MACD, EMA, ADX, ATR)
  strategy.py                # [momentum] Segnali LONG e SHORT
  regime.py                  # [momentum] Regime di mercato
  market_data.py             # [momentum] Fear & Greed, funding rates, news, mempool
services/
  groq_evaluator.py          # [momentum] Valutazione LLM del segnale
  discovery.py               # [momentum] Scansione giornaliera di nuove coppie
bot/
  telegram_bot.py            # Loop principale, job, comandi Telegram, percorso momentum
```

### Flusso di una scansione (momentum)

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

## Segnali LONG (momentum)

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

## Segnali SHORT (momentum, margine con leva 5×)

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
| MAX_CONSECUTIVE_LOSSES | 3 | 4 | 5 |
| FEAR_GREED long | 20–80 | 15–85 | 10–90 |
| FEAR_GREED_SHORT_MIN | 20 | 15 | 10 |
| TRAILING_ACTIVATION_ATR | 1.0× | 0.8× | 0.7× |

Il profilo attivo viene salvato in `trading.db` (chiave `active_profile`) e ripristinato al riavvio. Si può fare una preview prima di committarlo con `/profile preview <nome>` da Telegram.

I due circuit breaker sono indipendenti:
- **`CIRCUIT_BREAKER_LOSS_EUR`**: blocca lo scan se la perdita cumulata 24h supera la soglia in €
- **`MAX_CONSECUTIVE_LOSSES`**: blocca lo scan dopo N stop loss consecutivi (segnale di regime change). Lo streak si resetta al primo TP, così il bot si auto-recupera quando un trade aperto raggiunge il take profit. Telegram notifica una volta sola alla transizione di stato.

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
| `/profile preview <nome>` | Backtest dry-run di 30 giorni con i parametri del profilo target — **non** cambia il profilo attivo |
| `/pause` / `/resume` | Sospende/riprende lo scan automatico |
| `/stocks` | Azionario in paper: stato del mercato, posizioni e storico |

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

Il bot e la dashboard partono come servizi separati. I log persistono in `./logs` e il database in `./data/trading.db`.

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
MAX_CONSECUTIVE_LOSSES=4    # 0 = disabilita il CB consecutivo

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
| `trading.db` → `stock_positions` / `stock_trades` | Azionario in paper (in dollari, separato dal resto) |
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
