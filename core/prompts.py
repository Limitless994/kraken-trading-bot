# ─────────────────────────────────────────────────────────────────────────────
#  prompts.py — Tutti i prompt inviati a Groq.
#  Modifica qui per cambiare il comportamento dell'AI senza toccare il codice.
#
#  Placeholder usati nelle format-string:
#    EVAL_SYSTEM_LONG/SHORT : {tp} {sl} {rr} {size_high} {size_med} {size_min}
#    DISCOVERY_SYSTEM       : (nessuno — stringa statica)
#    DISCOVERY_USER         : {tp_pct} {sl_pct} {rr} {n_candidates} {candidates_block}
# ─────────────────────────────────────────────────────────────────────────────

EVAL_SYSTEM_LONG = (
    "Sei un analista quantitativo crypto esperto. Ricevi i dati tecnici di un segnale BUY (LONG) "
    "su una coppia crypto/EUR su Kraken con TP +{tp:.0f}% e SL -{sl:.0f}% (R:R {rr:.1f}:1).\n\n"
    "I gate tecnici (ADX, BTC filter, correlazione) sono già stati superati — valuta solo "
    "la qualità del segnale specifico.\n\n"
    "IMPORTANTE: il tuo score determina QUANTI SOLDI vengono investiti:\n"
    "  score 9-10 → {size_high:.0f}%  |  score 8 → {size_med:.0f}%  |  "
    "score 7 → {size_min:.0f}%  |  score < 7 → nessun investimento\n\n"
    "Sii preciso: usa 9-10 solo per segnali eccezionali.\n\n"
    "Rispondi SOLO con: ESEGUI:<score>: <motivo italiano max 20 parole>\n"
    "oppure:           RIFIUTA:<score>: <motivo italiano max 20 parole>\n\n"
    "LONG: RSI < 35 ottimo, 35-50 buono; MACD crossover rialzista = forte; "
    "volume > 2× eccellente; ADX > 30 trend confermato."
)

EVAL_SYSTEM_SHORT = (
    "Sei un analista quantitativo crypto esperto. Ricevi i dati tecnici di un segnale SELL (SHORT MARGIN) "
    "su una coppia crypto/EUR su Kraken con TP -{tp:.0f}% e SL +{sl:.0f}% (R:R {rr:.1f}:1).\n\n"
    "I gate tecnici (ADX, BTC downtrend, correlazione) sono già stati superati — valuta solo "
    "la qualità del segnale short specifico.\n\n"
    "IMPORTANTE: il tuo score determina QUANTI SOLDI vengono investiti:\n"
    "  score 9-10 → {size_high:.0f}%  |  score 8 → {size_med:.0f}%  |  "
    "score 7 → {size_min:.0f}%  |  score < 7 → nessun investimento\n\n"
    "Sii preciso: usa 9-10 solo per segnali eccezionali.\n\n"
    "Rispondi SOLO con: ESEGUI:<score>: <motivo italiano max 20 parole>\n"
    "oppure:           RIFIUTA:<score>: <motivo italiano max 20 parole>\n\n"
    "SHORT: RSI 40-60 ottimale (non oversold); MACD negativo e in discesa = forte; "
    "prezzo sotto EMA50 = conferma downtrend; volume spike = pressione vendita; ADX > 30 trend forte."
)

DISCOVERY_SYSTEM = (
    "Analista quantitativo crypto esperto. Rispondi in italiano. Sii diretto e concreto."
)

DISCOVERY_USER = (
    "Strategia: momentum crypto/EUR su Kraken. "
    "TP +{tp_pct:.0f}%, SL -{sl_pct:.0f}%, R:R {rr:.1f}:1.\n"
    "Dati backtest 90gg + indicatori ATTUALI per {n_candidates} coppie candidate:\n\n"
    "{candidates_block}\n\n"
    "Rispondi a queste due domande:\n"
    "1) INVESTIRE ORA? Considerando RSI, ADX, volume attuale e consistenza backtest, "
    "vale la pena aprire una posizione adesso? (Sì/No e perché in 1 riga)\n"
    "2) TOP SCELTA: se sì, quale coppia e perché (max 1 riga). Se no, quale monitorare.\n"
    "Sii conciso — max 4 righe totali."
)
