# Piano: sistema azionario sistematico

Stato: proposta del 12 novembre 2026, da implementare per fasi. Ogni fase ha un criterio di uscita; se non lo supera, ci si ferma lì.

Questo documento è scritto per chi implementa (Claude Code / Opus) e per chi decide (Davide). Non è consulenza finanziaria né fiscale.

---

## 1. Cosa significa "all'avanguardia" a questa scala

La ricerca fatta per questo piano porta a una conclusione scomoda: per un conto retail che opera su barre giornaliere, lo stato dell'arte **non è un modello più esotico**. È, in ordine di importanza:

1. **Dati che non mentono**: storico con i titoli delistati e la composizione storica degli indici. Senza, ogni backtest su "le azioni dell'S&P 500 di oggi" è gonfiato.
2. **Una procedura di validazione che non si può aggirare**: soglie decise prima, conteggio di tutte le varianti provate, test fuori campione veri.
3. **Pochi premi documentati e lenti** (momentum a 12 mesi, trend, controllo della volatilità), combinati, con costi bassi.
4. **ML e LLM solo dopo**, come strato aggiuntivo che deve battere la versione semplice fuori campione.

Cosa dice l'evidenza recente sui pezzi "di moda":

| Idea | Evidenza trovata | Decisione |
|---|---|---|
| Trend veloce (giorni/settimane) | Su futures, lo Sharpe del trend veloce è sceso da 0,84 (1995–2009) a 0,12 (2009–2025); quello lento da 0,70 a 0,40 [^trend] | Solo orizzonti lenti |
| Sentiment da notizie con LLM | Risultati forti su carta (Sharpe 2,85 con costi di 5 bps), ma su 19 mesi, con ribilanciamento giornaliero, più forte sui titoli illiquidi, e la replica pubblica scende a 0,92 [^llmnews] | Fase 4, solo in ombra |
| Backtest storici di LLM | Gli LLM "ricordano" il futuro: la capacità predittiva sui dati dentro il training è gonfiata dalla memorizzazione e sparisce dopo la data di cutoff [^lap] | Un LLM si valuta solo in avanti |
| Deriva post-utili (PEAD) | Ancora presente sulle large cap ma in calo: spread di 2,6 punti per gli annunci 2023–24, 0,5 punti per il 2025–26; 6,0% annuo dopo 10 bps di costi, 2,1% dopo 30 bps [^pead] | Rimandata, bassa priorità |
| Modelli fondazionali per serie storiche, deep RL | Non verificati in questa ricerca | Esclusi finché non c'è evidenza solida |

Aspettativa onesta: una strategia long-only di momentum su large cap ha un rendimento vicino a quello del mercato; il beneficio realistico è un drawdown più contenuto, non battere l'indice. Uno degli esiti legittimi di questo piano è scoprire che conviene un ETF sull'indice.

---

## 2. Vincoli che cambiano il progetto

### 2.1 Niente ETF americani dal vivo

Un cliente retail dell'UE non può comprare ETF domiciliati negli USA (SPY, QQQ, ecc.) perché non hanno il documento KID richiesto dal regolamento PRIIPs [^priips]. Conseguenze:

- L'universo attuale (`STOCK_SYMBOLS=SPY,QQQ,IWM,EFA,EEM,GLD,TLT,XLE`) funziona **solo in paper**.
- L'universo dal vivo deve essere fatto di **singole azioni USA**. Le azioni ordinarie non sono prodotti "packaged" e non dovrebbero rientrare nel PRIIPs: da confermare col broker prima della fase 6.
- Gli ETF UCITS quotati in Europa restano un'alternativa, ma hanno un trattamento fiscale peggiore (vedi sotto).

Quindi la strategia passa da "trend su 8 ETF" a "selezione di azioni dentro un indice, con la composizione storica corretta".

### 2.2 Broker

| | Paper | Dal vivo per un residente italiano |
|---|---|---|
| Alpaca | Sì, gratuito, già integrato | **Non confermato**: la guida di Alpaca per i non residenti USA non elenca i paesi supportati e il ramo europeo è presentato come infrastruttura per altre società, non come servizio diretto ai privati [^alpacalive] [^alpacaeu] |
| Interactive Brokers | Sì (conto paper incluso) | Sì; è la strada realistica per il live |

Decisione: **Alpaca per il paper, Interactive Brokers per l'eventuale live.** L'interfaccia `Broker` esiste già proprio per questo.

### 2.3 Fisco (da verificare con un commercialista)

Secondo una guida di settore, Interactive Brokers opera in regime dichiarativo: nessun sostituto d'imposta, quadri RT e RW a carico del contribuente, IVAFE dello 0,2% [^tasse]. Due effetti sul progetto:

- Le plusvalenze su **azioni** vanno in RT e si compensano con le minusvalenze; i guadagni su **ETF** sono redditi di capitale e non si compensano. Un motivo in più per le azioni singole.
- Il 26% sui guadagni realizzati ogni anno penalizza le strategie ad alto turnover rispetto al tenere un indice. Il backtest deve riportare anche il risultato dopo le imposte.

Il sistema deve produrre un **registro fiscale esportabile** (data, titolo, quantità, prezzi, cambio EUR/USD del giorno) per ogni operazione.

---

## 3. Cosa comprare

| Voce | Costo | Quando | Perché |
|---|---|---|---|
| **Norgate Data, US Stocks Platinum** | 630 $/anno [^vendors] | Fase 1 | Titoli delistati e composizione storica degli indici dal 1990. È la voce che conta |
| Dati fondamentali point-in-time (FMP Premium a 69 $/mese, oppure Sharadar) [^providers] [^vendors] | circa 830 $/anno | Solo fase 5 | Servono solo al modello ML |
| API LLM per le notizie | a consumo | Solo fase 4 | Sentiment in ombra |
| Dati di mercato IBKR per il live | da verificare | Solo fase 6 | Quotazioni per gli ordini |

Da **non** comprare: feed in tempo reale a pagamento (Alpaca SIP a 99 $/mese, Databento), GPU, server. Le barre giornaliere bastano, il feed gratuito IEX di Alpaca basta per il paper [^alpacadata], il NAS basta per far girare il bot, LightGBM gira sul PC.

Note su Norgate:

- Le versioni Silver e Gold **non** includono i delistati: serve Platinum [^vendors].
- Per quanto ne so, l'aggiornatore di Norgate gira solo su Windows: la ricerca si fa sul PC, non sul NAS. Da verificare all'acquisto (c'è un periodo di prova).
- I dati sono su licenza: **non vanno mai nella repo, che è pubblica**. La cartella dei dati di ricerca deve stare in `.gitignore`.

Alternativa a costo zero per partire: EODHD a 19,99 $/mese o Tiingo a 30 $/mese [^providers], sapendo che la copertura dei delistati è parziale e il backtest resta ottimistico.

---

## 4. Architettura di arrivo

Due ambienti separati, con il codice delle strategie in comune:

```
research/                  # offline, sul PC — mai in produzione
  data/
    norgate_export.py      # Norgate → Parquet locale (prezzi rettificati, delisting, appartenenza storica agli indici)
    store.py               # lettura Parquet: prices(asof), universe(asof)
  backtest/
    engine.py              # simulatore di portafoglio giornaliero
    costs.py               # commissioni, spread, cambio, imposte
    validation.py          # walk-forward, purged k-fold, deflated Sharpe, bootstrap
    registry.py            # registro di ogni configurazione provata
  reports/                 # report generati (non in repo se contengono dati su licenza)

core/
  strategies/
    base.py                # protocollo Strategy
    xs_momentum.py         # momentum cross-sezionale 12-1
    trend_breakout.py      # adattatore delle regole esistenti di core/trend.py
    composite.py           # combinazione + volatility targeting
    ml_ranker.py           # fase 5
  portfolio.py             # pesi obiettivo → ordini
  risk.py                  # limiti e interruttori
  broker.py                # + IBKRBroker (fase 6)
  trend_engine.py          # invariato: resta per la parte crypto
```

### 4.1 Contratto delle strategie

```python
class Strategy(Protocol):
    name: str
    def target_weights(self, asof: date, prices: PriceStore, universe: list[str]) -> dict[str, float]:
        """Pesi obiettivo per simbolo (somma ≤ 1; il resto è liquidità).
        Può usare solo dati con data ≤ asof. Deve essere deterministica."""
```

La stessa classe gira nel backtest e nel bot. È la regola che ha già evitato guai nella trend mode (`core/trend.py` usato sia dal live sia da `backtest_trend.py`): va mantenuta.

### 4.2 Simulatore (`research/backtest/engine.py`)

- Decisione sulla chiusura del giorno *t*, esecuzione all'apertura di *t+1*.
- Universo **alla data**: solo i titoli che in quel giorno erano nell'indice.
- Un titolo delistato esce al suo ultimo prezzo disponibile; niente buchi silenziosi.
- Costi: commissione per azione con minimo per ordine, mezzo spread per lato (parametro in bps, default prudente 5 bps sulle large cap), cambio EUR/USD.
- Uscite: curva del capitale giornaliera, operazioni, turnover, esposizione, e **due** risultati: prima e dopo imposta del 26% sui guadagni realizzati per anno.

### 4.3 Validazione (`research/backtest/validation.py`)

Ogni strategia candidata passa dagli stessi controlli, nessuno escluso:

1. **Walk-forward**: parametri scelti su una finestra, misurati sulla successiva, mai il contrario.
2. **Purged k-fold con embargo** per tutto ciò che viene addestrato (fase 5).
3. **Deflated Sharpe Ratio**: lo Sharpe corretto per il numero di varianti provate. Il numero viene dal registro, non dalla memoria.
4. **Sensibilità ai parametri**: griglia intorno ai valori scelti; il risultato deve reggere su tutto l'intorno.
5. **Concentrazione**: risultato ricalcolato togliendo i 5 titoli migliori.
6. **Sotto-periodi**: 2000–02, 2008–09, 2020, 2022 riportati separatamente.
7. **Benchmark**: S&P 500 total return e universo equipesato, stessi costi.

### 4.4 Portafoglio e rischio

- `portfolio.py`: differenza tra pesi obiettivo e posizioni reali → ordini. Soglia minima di scostamento per operare (default 20% del peso obiettivo), tetto di turnover per ribilanciamento.
- `risk.py`:
  - tetto per singolo titolo (default 6%) e per settore (default 30%);
  - **volatility targeting** di portafoglio (default 12% annuo, esposizione massima 100%: niente leva);
  - filtro di regime: indice sotto la media a 200 giorni → esposizione dimezzata o azzerata (parametro da validare);
  - interruttore sul drawdown: oltre una soglia dal picco, stop ai nuovi ingressi e notifica;
  - **stop protettivi piazzati presso il broker** per ogni posizione azionaria, così la protezione non dipende dal bot acceso;
  - controllo dei dati vecchi: se l'ultima barra non è quella attesa, nessun ordine.
- Niente short, niente margine, niente opzioni.

### 4.5 Ciclo operativo giornaliero

1. Dopo la chiusura USA (orario preso dal calendario del broker, non calcolato a mano): scarica le barre, calcola i pesi obiettivo, salva il piano ordini, invia il riepilogo su Telegram.
2. All'apertura successiva: invia gli ordini, verifica i fill (meccanismo `pending` già presente in `trend_engine.py`).
3. Riconciliazione: posizioni del broker contro database; ogni differenza blocca gli ordini e avvisa.
4. I segnali dal vivo usano le barre del broker; Norgate serve solo alla ricerca. La lista dei titoli dell'indice è un file aggiornato una volta al mese dal PC.

---

## 5. Strategie candidate

Nessuna è "approvata": ognuna deve superare il cancello della fase 3.

**A. Momentum cross-sezionale 12-1 (candidata principale)**
- Universo: titoli dell'S&P 500 alla data, filtrati per liquidità.
- Punteggio: rendimento degli ultimi 12 mesi escluso l'ultimo; variante **residuale** (momentum al netto del beta di mercato) da confrontare.
- Portafoglio: i primi 20–30 titoli, pesi inversi alla volatilità, ribilanciamento mensile.
- Con filtro di regime e volatility targeting della sezione 4.4.

**B. Breakout di trend su singole azioni (base di confronto)**
- Le regole già scritte in `core/trend.py`, applicate ai titoli dell'universo con un numero massimo di posizioni.
- Serve come riferimento: è già implementata e testata.

**C. Composita A + B** con pesi fissi, se entrambe superano il cancello.

**D. Ranker ML (fase 5)** — LightGBM con obiettivo di ranking su caratteristiche di prezzo, volume e fondamentali point-in-time. Entra solo se batte A fuori campione al netto dei costi.

**E. Sentiment da notizie con LLM (fase 4)** — solo in ombra: registra i segnali, non opera.

Escluse: strategie intraday, inversione a breve termine (turnover troppo alto per i costi retail), PEAD (in calo, vedi sezione 1), qualunque uso di leva.

---

## 6. Fasi

Ogni fase è una pull request separata. I test esistenti (`python -m pytest tests.py`) devono restare verdi.

### Fase 0 — Pulizia (1 PR piccola)
- Aggiungere a `.gitignore` la cartella dei dati di ricerca.
- Decidere con Davide se rimuovere la modalità `momentum` crypto (strategia in perdita nei backtest, short a leva 5×, circa metà di `bot/telegram_bot.py`). Non rimuovere senza conferma.
- **Uscita**: repo pulita, nessun dato su licenza tracciabile.

### Fase 1 — Dati
- `research/data/norgate_export.py` e `store.py`.
- Test: un titolo delistato noto è presente fino al delisting; l'universo a una data passata contiene titoli poi usciti dall'indice; nessuna barra con data futura rispetto ad `asof`.
- **Uscita**: `universe(asof)` e `prices(asof)` verificati su almeno tre date storiche controllate a mano.

### Fase 2 — Simulatore e validazione
- `research/backtest/` completo, con i sette controlli della sezione 4.3.
- Test anti-lookahead come quelli già presenti per `backtest_trend.py` (`TestTrendBacktest`).
- Prova di sanità: il simulatore applicato a "compra e tieni l'indice" deve riprodurre il benchmark entro una tolleranza dichiarata.
- **Uscita**: prova di sanità superata; registro delle configurazioni attivo.

### Fase 3 — Strategie A e B e **cancello di ricerca**
- Implementare `xs_momentum.py`, `trend_breakout.py`, `composite.py`, `portfolio.py`, `risk.py`.
- Produrre il report di validazione.
- **Cancello (soglie fissate ora, non dopo aver visto i numeri)** — sul periodo 2000 → oggi, al netto dei costi:
  1. Sharpe superiore a quello dell'S&P 500 total return;
  2. drawdown massimo non peggiore di quello dell'indice;
  3. positiva in almeno il 60% delle finestre mobili di 3 anni;
  4. tutto l'intorno dei parametri positivo;
  5. Deflated Sharpe Ratio sopra 0,95;
  6. risultato ancora positivo togliendo i 5 titoli migliori;
  7. rendimento dopo imposte riportato accanto a quello dell'indice dopo imposte.
- **Se il cancello non è superato, il progetto si ferma qui** e il report lo dice chiaramente. Non si ritoccano i parametri per farlo passare.

### Fase 4 — Paper e sentiment in ombra
- Collegare le strategie approvate al bot su Alpaca paper, con il ciclo della sezione 4.5 e la scheda azionario nella dashboard.
- In parallelo, modulo LLM in ombra: legge le notizie, registra punteggio e rendimento successivo, **non opera**. Valutazione solo su dati successivi al cutoff del modello usato [^lap].
- **Uscita dopo almeno 3 mesi**: i segnali del paper coincidono con quelli del backtest rieseguito sullo stesso periodo in almeno il 95% dei giorni; scostamento di rendimento spiegato dai fill.

### Fase 5 — Ranker ML (opzionale)
- Solo se la fase 3 è superata. Richiede l'acquisto dei dati fondamentali.
- **Uscita**: batte la strategia A fuori campione al netto dei costi, con Deflated Sharpe sopra 0,95 contando tutte le prove. Altrimenti si scarta.

### Fase 6 — Live su Interactive Brokers (decisione di Davide)
- `IBKRBroker` dietro l'interfaccia `Broker`; da verificare sulla documentazione ufficiale la libreria Python e la modalità di esecuzione del gateway sul NAS.
- Prima su conto paper IBKR, poi capitale piccolo e fisso.
- Prerequisiti: conferma del broker sull'acquistabilità delle azioni USA, registro fiscale funzionante, stop presso il broker attivi, riconciliazione attiva, parere del commercialista sul regime fiscale.
- `AlpacaBroker` resta solo paper.

---

## 7. Regole per chi implementa

1. Una fase per pull request; non anticipare fasi successive.
2. La stessa classe `Strategy` nel backtest e nel bot. Mai una copia della logica.
3. Ogni numero in un report deve essere riproducibile con un comando scritto nel report.
4. Ogni configurazione provata va nel registro, anche quelle scartate.
5. Soglie e cancelli di questo documento non si modificano nella stessa PR che li misura.
6. Nessun dato su licenza, chiave o credenziale nella repo.
7. Davanti a una scelta non coperta da questo piano: fermarsi e chiedere.
8. Nessuna funzione di trading dal vivo prima della fase 6.

Prompt di avvio per ogni fase:

```
Leggi docs/PIANO_AZIONARIO.md. Implementa SOLO la Fase N.
Prima di scrivere codice, mostrami: i file che creerai o modificherai, i test
che aggiungerai e come verificherai il criterio di uscita. Aspetta la mia conferma.
Rispetta le regole della sezione 7. Se una scelta non è coperta dal piano, fermati e chiedi.
```

---

## Fonti

[^trend]: [Is Trend Still Your Friend? — arXiv 2607.01550](https://arxiv.org/pdf/2607.01550)
[^llmnews]: [Financial Language Models as Applied AI Systems for News-Based Trading under Market Frictions — arXiv 2609.23703](https://arxiv.org/pdf/2609.23703)
[^lap]: [Detecting Lookahead Bias in LLM Forecasts — arXiv 2512.23847](https://arxiv.org/pdf/2512.23847)
[^pead]: [Is Post-Earnings Drift Still Tradable? 43,000 Announcements, 2023–2026](https://guptagaurav.info/articles/earnings-drift)
[^priips]: [justETF — US-domiciled ETFs: why they are no longer available from many online brokers](https://www.justetf.com/news/etf/us-domiciled-etfs.html)
[^alpacalive]: [Alpaca — How to Open a Live Trading Account as a Non-US Resident](https://alpaca.markets/learn/live-trading-account-non-us)
[^alpacaeu]: [FinTech Global — Alpaca expands into Europe with WealthKernel deal](https://fintech.global/2026/04/21/alpaca-expands-into-europe-with-wealthkernel-deal/)
[^alpacadata]: [Alpaca — About Market Data API](https://docs.alpaca.markets/us/docs/about-market-data-api.md)
[^tasse]: [TasseTrading — Guida alle tasse su Interactive Brokers 2026](https://www.tassetrading.it/tasse-interactive-brokers/)
[^vendors]: [Frontier Ledger — Survivorship-Bias-Free Equity Data: 9 Vendors Compared](https://frontierledger.ai/strategy-backtesting-evaluation/survivorship-bias-free-equity-universes-data-vendors-compared)
[^providers]: [Quant Data Providers Compared: Databento, Massive, FMP, EODHD](https://waylandz.com/quant-book-en/Data-Provider-Comparison/)
