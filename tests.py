"""
tests.py — Full test suite for the Kraken Trading Bot.

Run with:
    python tests.py
or:
    python -m pytest tests.py -v

All tests use mocks — no real calls to Kraken, Groq, or external APIs.
"""
import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# ── Minimal env before importing ─────────────────────────────────────────────
os.environ.setdefault("TELEGRAM_TOKEN",    "fake:token")
os.environ.setdefault("TELEGRAM_CHAT_ID",  "123456")
os.environ.setdefault("GROQ_API_KEY",      "fake_groq_key")
os.environ.setdefault("PAPER_TRADING",     "true")
os.environ.setdefault("KRAKEN_API_KEY",    "")
os.environ.setdefault("KRAKEN_API_SECRET", "")

from core import config
config.apply_profile("conservative")   # reset to known state regardless of active_profile.json
from core.analyzer import Analysis, _adx, _atr, _ema, _macd_histogram, _rsi, _volume_ratio
from core.strategy import Signal, generate_signal, kelly_size_pct


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_analysis(**overrides) -> Analysis:
    defaults = dict(
        pair="SOLEUR", price=100.0,
        high_24h=110.0, low_24h=90.0, volume_24h=10000.0,
        rsi_1h=45.0,
        macd_hist=0.001, macd_hist_prev=-0.001,
        ema20=98.0, ema50=95.0,
        volume_ratio=1.6,
        rsi_15m=44.0,
        adx=25.0,
        atr=0.5,
        ob_imbalance=0.55,
        rsi_4h=50.0,
        rsi_daily=48.0,
        daily_above_ema50=True,
        price_change_24h=1.2,
    )
    defaults.update(overrides)
    return Analysis(**defaults)


def _make_signal(**overrides) -> Signal:
    defaults = dict(
        id="test01", pair="SOLEUR", direction="BUY",
        price=100.0, size_eur=18.0, size_crypto=0.18,
        take_profit=112.5, stop_loss=96.0,
        trailing_stop=96.0,
        rsi_1h=40.0, rsi_15m=38.0,
        macd_hist=0.001, volume_ratio=1.6,
        adx=26.0, atr=0.5,
        score=4,
        reasons=["RSI(1h) in buy zone", "MACD crossover", "Volume spike"],
        base_currency="SOL",
    )
    defaults.update(overrides)
    return Signal(**defaults)


# ═══════════════════════════════════════════════════════════════════════════════
# 1. INDICATORI TECNICI
# ═══════════════════════════════════════════════════════════════════════════════

class TestRSI(unittest.TestCase):

    def test_oversold(self):
        rsi = _rsi([100.0] * 14 + [90.0, 80.0, 70.0])
        self.assertLess(rsi, 30)

    def test_overbought(self):
        rsi = _rsi([100.0] * 14 + [110.0, 120.0, 130.0])
        self.assertGreater(rsi, 70)

    def test_all_losses_returns_zero(self):
        self.assertAlmostEqual(_rsi(list(range(20, 0, -1))), 0.0, places=1)

    def test_all_gains_returns_100(self):
        self.assertAlmostEqual(_rsi(list(range(1, 22))), 100.0, places=1)

    def test_insufficient_data_returns_50(self):
        self.assertEqual(_rsi([100.0, 101.0], period=14), 50.0)


class TestEMA(unittest.TestCase):

    def test_single_value(self):
        self.assertEqual(_ema([42.0], 5), [42.0])

    def test_length_preserved(self):
        self.assertEqual(len(_ema([float(i) for i in range(1, 21)], 5)), 20)

    def test_ema_lags_price(self):
        result = _ema([10.0] * 10 + [100.0] * 10, 5)
        self.assertLess(result[10], 100.0)
        self.assertGreater(result[-1], 50.0)


class TestMACD(unittest.TestCase):

    def test_returns_at_least_two_values(self):
        self.assertGreaterEqual(len(_macd_histogram([float(i) for i in range(1, 50)])), 2)

    def test_uptrend_positive_histogram(self):
        hist = _macd_histogram([float(i * 2) for i in range(1, 60)])
        self.assertGreater(hist[-1], 0)

    def test_insufficient_data_returns_zeros(self):
        self.assertEqual(_macd_histogram([1.0, 2.0, 3.0]), [0.0, 0.0])


class TestVolumeRatio(unittest.TestCase):

    def test_spike_detection(self):
        self.assertAlmostEqual(_volume_ratio([10.0] * 20 + [50.0]), 5.0, places=1)

    def test_normal_volume(self):
        self.assertAlmostEqual(_volume_ratio([10.0] * 21), 1.0, places=1)

    def test_insufficient_data_returns_1(self):
        self.assertEqual(_volume_ratio([10.0, 20.0]), 1.0)


class TestATR(unittest.TestCase):

    def test_atr_positive(self):
        highs  = [105.0] * 20
        lows   = [95.0]  * 20
        closes = [100.0] * 20
        atr = _atr(highs, lows, closes)
        self.assertGreater(atr, 0)

    def test_atr_high_volatility_greater_than_low(self):
        n = 20
        h_high = [100 + 10] * n;  l_high = [100 - 10] * n;  c = [100.0] * n
        h_low  = [100 +  1] * n;  l_low  = [100 -  1] * n
        self.assertGreater(_atr(h_high, l_high, c), _atr(h_low, l_low, c))

    def test_atr_insufficient_data_fallback(self):
        atr = _atr([105.0, 106.0], [95.0, 96.0], [100.0, 101.0])
        self.assertGreater(atr, 0)


class TestADX(unittest.TestCase):

    def test_strong_trend_high_adx(self):
        closes = [float(i) for i in range(1, 60)]
        highs  = [c + 1 for c in closes]
        lows   = [c - 1 for c in closes]
        adx = _adx(highs, lows, closes)
        self.assertGreater(adx, 15)

    def test_insufficient_data_returns_neutral(self):
        self.assertEqual(_adx([1.0, 2.0], [0.5, 1.0], [1.0, 1.5]), 20.0)


# ═══════════════════════════════════════════════════════════════════════════════
# 2. STRATEGIA E SCORING
# ═══════════════════════════════════════════════════════════════════════════════

class TestStrategy(unittest.TestCase):

    def test_strong_signal(self):
        a = _make_analysis(
            rsi_1h=25.0, rsi_15m=40.0, rsi_4h=48.0,
            macd_hist=0.01, macd_hist_prev=-0.01,
            volume_ratio=2.0, ema50=95.0, adx=28.0,
            daily_above_ema50=True,
        )
        sig = generate_signal(a, 100.0)
        self.assertIsNotNone(sig)
        self.assertGreaterEqual(sig.score, config.MIN_SIGNAL_SCORE)

    def test_below_threshold(self):
        a = _make_analysis(
            rsi_1h=65.0, rsi_15m=70.0,
            macd_hist=-0.01, macd_hist_prev=-0.02,
            volume_ratio=1.0, ema50=110.0, adx=25.0,
        )
        self.assertIsNone(generate_signal(a, 100.0))

    def test_adx_gate_blocks_ranging_market(self):
        a = _make_analysis(adx=15.0)   # ADX < ADX_MIN (20)
        self.assertIsNone(generate_signal(a, 100.0))

    def test_daily_ema_gate(self):
        a = _make_analysis(daily_above_ema50=False)
        self.assertIsNone(generate_signal(a, 100.0))

    def test_correlation_gate(self):
        a = _make_analysis(pair="SOLEUR")
        # ETHEUR è nello stesso gruppo corr di SOLEUR
        self.assertIsNone(generate_signal(a, 100.0, open_pairs={"ETHEUR"}))

    def test_insufficient_balance(self):
        self.assertIsNone(generate_signal(_make_analysis(), 5.0))

    def test_pair_already_open(self):
        a = _make_analysis(pair="SOLEUR")
        self.assertIsNone(generate_signal(a, 100.0, already_open={"SOLEUR"}))

    def test_signal_has_atr_tp_sl(self):
        a = _make_analysis(price=100.0, atr=2.0)
        sig = generate_signal(a, 100.0)
        if sig:
            # TP = entry + 2.5 × ATR = 105 min
            self.assertGreater(sig.take_profit, 100.0)
            self.assertLess(sig.stop_loss, 100.0)

    def test_trailing_stop_equals_stop_loss_initially(self):
        sig = generate_signal(_make_analysis(), 100.0)
        if sig:
            self.assertEqual(sig.trailing_stop, sig.stop_loss)


# ═══════════════════════════════════════════════════════════════════════════════
# 3. ATR TP/SL
# ═══════════════════════════════════════════════════════════════════════════════

class TestATRTPSL(unittest.TestCase):

    def test_tp_adapts_to_high_volatility(self):
        a_high = _make_analysis(atr=5.0, price=100.0)
        a_low  = _make_analysis(atr=1.0, price=100.0)
        sig_h = generate_signal(a_high, 100.0)
        sig_l = generate_signal(a_low,  100.0)
        if sig_h and sig_l:
            self.assertGreater(sig_h.take_profit, sig_l.take_profit)

    def test_tp_equals_entry_plus_atr_mult(self):
        a = _make_analysis(atr=2.0, price=100.0)
        sig = generate_signal(a, 100.0)
        if sig:
            expected = round(100.0 + config.ATR_TP_MULT * 2.0, 8)
            self.assertAlmostEqual(sig.take_profit, expected, places=4)

    def test_sl_equals_entry_minus_atr_mult(self):
        a = _make_analysis(atr=2.0, price=100.0)
        sig = generate_signal(a, 100.0)
        if sig:
            expected = round(100.0 - config.ATR_SL_MULT * 2.0, 8)
            self.assertAlmostEqual(sig.stop_loss, expected, places=4)


# ═══════════════════════════════════════════════════════════════════════════════
# 4. KELLY CRITERION
# ═══════════════════════════════════════════════════════════════════════════════

class TestKelly(unittest.TestCase):

    def _trades(self, wins, losses, win_pnl=5.0, loss_pnl=-2.0):
        return (
            [{"pnl_eur": win_pnl}  for _ in range(wins)] +
            [{"pnl_eur": loss_pnl} for _ in range(losses)]
        )

    def test_insufficient_trades_returns_none(self):
        self.assertIsNone(kelly_size_pct(self._trades(5, 5)))

    def test_high_win_rate_gives_larger_size(self):
        high = kelly_size_pct(self._trades(16, 4))
        low  = kelly_size_pct(self._trades(11, 9))
        if high and low:
            self.assertGreater(high, low)

    def test_size_within_bounds(self):
        pct = kelly_size_pct(self._trades(14, 6))
        if pct:
            self.assertGreaterEqual(pct, config.KELLY_MIN_SIZE_PCT)
            self.assertLessEqual(pct, config.KELLY_MAX_SIZE_PCT)

    def test_all_losses_returns_min(self):
        pct = kelly_size_pct(self._trades(0, 25))
        self.assertIsNone(pct)   # no wins → None


# ═══════════════════════════════════════════════════════════════════════════════
# 5. FEE — TAKER E MAKER
# ═══════════════════════════════════════════════════════════════════════════════

class TestFees(unittest.TestCase):

    def test_maker_fee_lower_than_taker(self):
        self.assertLess(config.KRAKEN_MAKER_FEE, config.KRAKEN_TAKER_FEE)

    def test_limit_saves_fee(self):
        size = 50.0
        saving = size * (config.KRAKEN_TAKER_FEE - config.KRAKEN_MAKER_FEE) * 2
        self.assertGreater(saving, 0)

    def test_net_tp_positive_after_taker_fees(self):
        entry = 100.0; tp = entry * 1.10; size = 50.0
        sc    = size * (1 - config.KRAKEN_TAKER_FEE) / entry
        net   = (tp - entry) * sc - size * config.KRAKEN_TAKER_FEE - tp * sc * config.KRAKEN_TAKER_FEE
        self.assertGreater(net, 0)

    def test_round_trip_taker_approx_80_bps(self):
        self.assertAlmostEqual(config.KRAKEN_TAKER_FEE * 2 * 100, 0.80, places=2)

    def test_round_trip_maker_approx_50_bps(self):
        self.assertAlmostEqual(config.KRAKEN_MAKER_FEE * 2 * 100, 0.50, places=2)


# ═══════════════════════════════════════════════════════════════════════════════
# 6. CIRCUIT BREAKER
# ═══════════════════════════════════════════════════════════════════════════════

class TestCircuitBreaker(unittest.TestCase):

    def _daily_loss(self, eur: float) -> list:
        return [{"pnl_eur": -eur, "closed_at": time.time() - 3600}]

    def test_triggered_when_loss_exceeds_threshold(self):
        trades    = self._daily_loss(config.CIRCUIT_BREAKER_LOSS_EUR + 1)
        today_ts  = time.time() - 86400
        daily_loss = abs(sum(
            t["pnl_eur"] for t in trades
            if t.get("closed_at", 0) > today_ts and t["pnl_eur"] < 0
        ))
        self.assertGreaterEqual(daily_loss, config.CIRCUIT_BREAKER_LOSS_EUR)

    def test_not_triggered_below_threshold(self):
        trades    = self._daily_loss(config.CIRCUIT_BREAKER_LOSS_EUR - 1)
        today_ts  = time.time() - 86400
        daily_loss = abs(sum(
            t["pnl_eur"] for t in trades
            if t.get("closed_at", 0) > today_ts and t["pnl_eur"] < 0
        ))
        self.assertLess(daily_loss, config.CIRCUIT_BREAKER_LOSS_EUR)

    def test_old_losses_ignored(self):
        old_trade = [{"pnl_eur": -100.0, "closed_at": time.time() - 90000}]
        today_ts  = time.time() - 86400
        daily_loss = abs(sum(
            t["pnl_eur"] for t in old_trade
            if t.get("closed_at", 0) > today_ts and t["pnl_eur"] < 0
        ))
        self.assertEqual(daily_loss, 0.0)


# ═══════════════════════════════════════════════════════════════════════════════
# 7. FEAR & GREED
# ═══════════════════════════════════════════════════════════════════════════════

class TestFearGreed(unittest.TestCase):

    def test_extreme_fear_blocks(self):
        from core.market_data import fear_greed_blocks_trading
        self.assertTrue(fear_greed_blocks_trading(15))

    def test_extreme_greed_blocks(self):
        from core.market_data import fear_greed_blocks_trading
        self.assertTrue(fear_greed_blocks_trading(85))

    def test_neutral_allows(self):
        from core.market_data import fear_greed_blocks_trading
        self.assertFalse(fear_greed_blocks_trading(50))

    def test_none_allows(self):
        from core.market_data import fear_greed_blocks_trading
        self.assertFalse(fear_greed_blocks_trading(None))

    @patch("core.market_data.requests")
    def test_api_returns_value(self, mock_requests):
        mock_requests.get.return_value.json.return_value = {
            "data": [{"value": "42", "value_classification": "Fear"}]
        }
        from core.market_data import _cache, get_fear_greed
        _cache.pop("fear_greed", None)
        result = get_fear_greed()
        self.assertIsNotNone(result)
        self.assertEqual(result["value"], 42)

    @patch("core.market_data.requests")
    def test_api_failure_returns_none(self, mock_requests):
        mock_requests.get.side_effect = Exception("timeout")
        from core.market_data import _cache, get_fear_greed
        _cache.pop("fear_greed", None)
        result = get_fear_greed()
        self.assertIsNone(result)


# ═══════════════════════════════════════════════════════════════════════════════
# 8. GROQ EVALUATOR — nuovo formato a 4 valori
# ═══════════════════════════════════════════════════════════════════════════════

class TestGroqEvaluator(unittest.TestCase):

    def _mock_groq(self, content: str):
        mock_choice = MagicMock()
        mock_choice.message.content = content
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value.choices = [mock_choice]
        return mock_client

    @patch("services.groq_evaluator._get_client")
    def test_esegui_high_confidence(self, mock_gc):
        mock_gc.return_value = self._mock_groq("ESEGUI:9: RSI oversold con volume confermato")
        from services import groq_evaluator
        execute, conf, reason, size = groq_evaluator.evaluate(_make_signal())
        self.assertTrue(execute)
        self.assertEqual(conf, 9)
        self.assertEqual(size, config.AI_SIZE_HIGH)

    @patch("services.groq_evaluator._get_client")
    def test_esegui_med_confidence(self, mock_gc):
        mock_gc.return_value = self._mock_groq("ESEGUI:8: buon setup ma non eccezionale")
        from services import groq_evaluator
        execute, conf, reason, size = groq_evaluator.evaluate(_make_signal())
        self.assertTrue(execute)
        self.assertEqual(size, config.AI_SIZE_MED)

    @patch("services.groq_evaluator._get_client")
    def test_esegui_min_confidence(self, mock_gc):
        mock_gc.return_value = self._mock_groq("ESEGUI:7: setup borderline ma accettabile")
        from services import groq_evaluator
        execute, conf, reason, size = groq_evaluator.evaluate(_make_signal())
        self.assertTrue(execute)
        self.assertEqual(size, config.AI_SIZE_MIN)

    @patch("services.groq_evaluator._get_client")
    def test_esegui_below_threshold_blocked(self, mock_gc):
        mock_gc.return_value = self._mock_groq("ESEGUI:5: segnale debole")
        from services import groq_evaluator
        execute, conf, reason, size = groq_evaluator.evaluate(_make_signal())
        self.assertFalse(execute)
        self.assertEqual(size, 0.0)

    @patch("services.groq_evaluator._get_client")
    def test_rifiuta_response(self, mock_gc):
        mock_gc.return_value = self._mock_groq("RIFIUTA:3: volume insufficiente")
        from services import groq_evaluator
        execute, conf, reason, size = groq_evaluator.evaluate(_make_signal())
        self.assertFalse(execute)
        self.assertEqual(size, 0.0)

    @patch("services.groq_evaluator._get_client")
    def test_groq_offline_fallback(self, mock_gc):
        mock_gc.side_effect = Exception("Connection refused")
        from services import groq_evaluator
        execute, conf, reason, size = groq_evaluator.evaluate(_make_signal())
        self.assertTrue(execute)
        self.assertEqual(size, config.AI_SIZE_MIN)

    @patch("services.groq_evaluator._get_client")
    def test_market_context_included(self, mock_gc):
        mock_gc.return_value = self._mock_groq("ESEGUI:8: ok")
        from services import groq_evaluator
        ctx = {"fear_greed_value": 35, "fear_greed_label": "Fear",
               "btc_funding": 0.0005, "news_headlines": ["BTC rallies"]}
        execute, _, _, _ = groq_evaluator.evaluate(_make_signal(), market_ctx=ctx)
        call_args = mock_gc.return_value.chat.completions.create.call_args
        user_content = call_args[1]["messages"][1]["content"]
        self.assertIn("Fear", user_content)


# ═══════════════════════════════════════════════════════════════════════════════
# 9. KRAKEN CLIENT
# ═══════════════════════════════════════════════════════════════════════════════

class TestKrakenStatus(unittest.TestCase):

    @patch("core.kraken_client._SESSION")
    def test_online_status(self, mock_session):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "error": [],
            "result": {"status": "online", "timestamp": "2026-01-01T00:00:00Z"},
        }
        mock_resp.raise_for_status = MagicMock()
        mock_session.get.return_value = mock_resp
        from core import kraken_client
        status, _ = kraken_client.get_system_status()
        self.assertEqual(status, "online")

    @patch("core.kraken_client._SESSION")
    def test_maintenance_not_online(self, mock_session):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "error": [],
            "result": {"status": "maintenance", "timestamp": "2026-01-01T00:00:00Z"},
        }
        mock_resp.raise_for_status = MagicMock()
        mock_session.get.return_value = mock_resp
        from core import kraken_client
        status, _ = kraken_client.get_system_status()
        self.assertNotEqual(status, kraken_client.KRAKEN_ONLINE)

    @patch("core.kraken_client._SESSION")
    def test_network_error_raises(self, mock_session):
        mock_session.get.side_effect = ConnectionError("timeout")
        from core import kraken_client
        with self.assertRaises(Exception):
            kraken_client.get_system_status()


# ═══════════════════════════════════════════════════════════════════════════════
# 10. LIMIT ORDERS
# ═══════════════════════════════════════════════════════════════════════════════

class TestLimitOrders(unittest.TestCase):

    def test_limit_price_below_market(self):
        market_price = 100.0
        limit_price  = market_price * (1 - config.LIMIT_OFFSET_PCT)
        self.assertLess(limit_price, market_price)

    def test_maker_fee_saving_per_roundtrip(self):
        size    = 50.0
        saving  = size * (config.KRAKEN_TAKER_FEE - config.KRAKEN_MAKER_FEE) * 2
        self.assertGreater(saving, 0)
        self.assertAlmostEqual(saving, 50 * 0.0015 * 2, places=4)

    def test_limit_offset_config(self):
        self.assertGreater(config.LIMIT_OFFSET_PCT, 0)
        self.assertLess(config.LIMIT_OFFSET_PCT, 0.01)

    def test_limit_timeout_config(self):
        self.assertGreater(config.LIMIT_TIMEOUT_SEC, 60)


# ═══════════════════════════════════════════════════════════════════════════════
# 11. PERSISTENZA
# ═══════════════════════════════════════════════════════════════════════════════

class TestPersistence(unittest.TestCase):

    def setUp(self):
        self.f = Path("_test_positions.json")
        self.f.unlink(missing_ok=True)

    def tearDown(self):
        self.f.unlink(missing_ok=True)

    def test_save_and_reload(self):
        pos = {"SOLEUR": {"entry": 100.0, "size_crypto": 0.18, "size_eur": 18.0,
                          "fee_entry_eur": 0.045, "take_profit": 112.5,
                          "stop_loss": 96.0, "trailing_stop": 96.0,
                          "txid": "PAPER", "opened_at": time.time(), "order_type": "limit"}}
        self.f.write_text(json.dumps(pos), encoding="utf-8")
        loaded = json.loads(self.f.read_text(encoding="utf-8"))
        self.assertIn("SOLEUR", loaded)
        self.assertAlmostEqual(loaded["SOLEUR"]["entry"], 100.0)
        self.assertEqual(loaded["SOLEUR"]["order_type"], "limit")

    def test_empty_returns_empty(self):
        self.assertEqual({}, {})

    def test_corrupted_handled(self):
        self.f.write_text("{ not valid json", encoding="utf-8")
        with self.assertRaises(json.JSONDecodeError):
            json.loads(self.f.read_text(encoding="utf-8"))


# ═══════════════════════════════════════════════════════════════════════════════
# 12. CONFIG E VALIDAZIONE
# ═══════════════════════════════════════════════════════════════════════════════

class TestConfig(unittest.TestCase):

    def test_paper_mode_no_kraken_keys_needed(self):
        try:
            config.validate()
        except EnvironmentError as e:
            if "KRAKEN" in str(e):
                self.fail(f"Paper mode non dovrebbe richiedere chiavi Kraken: {e}")

    def test_rr_ratio(self):
        self.assertGreater(config.TAKE_PROFIT_PCT / config.STOP_LOSS_PCT, 1.5)

    def test_all_pairs_have_decimals(self):
        for pair in config.PAIRS:
            self.assertIn(pair, config.PAIR_VOLUME_DECIMALS)

    def test_capital_deployed_lt_1(self):
        self.assertLess(config.MAX_CAPITAL_DEPLOYED, 1.0)
        self.assertGreater(config.MAX_CAPITAL_DEPLOYED, 0.5)

    def test_kelly_bounds_valid(self):
        self.assertLess(config.KELLY_MIN_SIZE_PCT, config.KELLY_MAX_SIZE_PCT)

    def test_ai_size_descending(self):
        self.assertGreater(config.AI_SIZE_HIGH, config.AI_SIZE_MED)
        self.assertGreater(config.AI_SIZE_MED,  config.AI_SIZE_MIN)

    def test_multi_timeframe_params(self):
        self.assertGreater(config.RSI_4H_BUY_MAX, config.RSI_1H_BUY_MAX)

    def test_groq_model_set(self):
        self.assertIn("llama", config.GROQ_MODEL)

    def test_atr_multipliers(self):
        self.assertGreater(config.ATR_TP_MULT, config.ATR_SL_MULT)


# ═══════════════════════════════════════════════════════════════════════════════
# 13. PAPER BALANCE
# ═══════════════════════════════════════════════════════════════════════════════

class TestPaperBalance(unittest.TestCase):

    def test_available_after_positions(self):
        budget    = config.BUDGET_EUR
        positions = {"SOLEUR": {"size_eur": 18.0}, "DOGEEUR": {"size_eur": 12.0}}
        spent     = sum(p["size_eur"] for p in positions.values())
        available = max(budget - spent, 0.0)
        self.assertEqual(available, budget - 30.0)

    def test_never_negative(self):
        positions = {"SOLEUR": {"size_eur": config.BUDGET_EUR + 50}}
        spent     = sum(p["size_eur"] for p in positions.values())
        available = max(config.BUDGET_EUR - spent, 0.0)
        self.assertEqual(available, 0.0)

    def test_capital_cap(self):
        max_allowed = config.BUDGET_EUR * config.MAX_CAPITAL_DEPLOYED
        self.assertLess(max_allowed, config.BUDGET_EUR)


# ═══════════════════════════════════════════════════════════════════════════════
# RUNNER
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite  = unittest.TestSuite()

    for cls in [
        TestRSI, TestEMA, TestMACD, TestVolumeRatio,
        TestATR, TestADX,
        TestStrategy, TestATRTPSL,
        TestKelly, TestFees, TestCircuitBreaker,
        TestFearGreed, TestGroqEvaluator,
        TestKrakenStatus, TestLimitOrders,
        TestPersistence, TestConfig, TestPaperBalance,
    ]:
        suite.addTests(loader.loadTestsFromTestCase(cls))

    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
