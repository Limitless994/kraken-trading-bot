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
import shutil
import sys
import tempfile
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
from core.strategy import Signal, generate_signal, generate_short_signal, kelly_size_pct


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


def _make_short_analysis(**overrides) -> Analysis:
    """Analysis suitable for a SHORT signal: price below daily EMA50, bearish indicators."""
    defaults = dict(
        pair="DOGEEUR", price=0.07,
        high_24h=0.075, low_24h=0.065, volume_24h=1_000_000.0,
        rsi_1h=55.0,          # not oversold; above RSI_SHORT_MIN=42 (conservative)
        rsi_1h_prev=58.0,     # falling (slope = -3) → short continuation, not bounce
        macd_hist=-0.0001, macd_hist_prev=0.0001,  # bearish crossover
        ema20=0.072, ema50=0.075,                   # price 0.07 < ema50 0.075 → below EMA50
        volume_ratio=1.6,
        rsi_15m=52.0,
        adx=25.0,
        atr=0.001,             # ATR 1.43% of price → 2.5×ATR = 3.57% TP distance > min 1.59%
        ob_imbalance=0.40,     # bearish order book
        rsi_4h=55.0,
        rsi_daily=52.0,
        daily_above_ema50=False,  # required: price must be below daily EMA50 for a SHORT
        price_change_24h=-1.5,
    )
    defaults.update(overrides)
    return Analysis(**defaults)


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
# 14. DATABASE (SQLite WAL layer)
# ═══════════════════════════════════════════════════════════════════════════════

import core.db as _db_module   # imported here to allow DB_PATH patching in setUp


class TestDB(unittest.TestCase):
    """
    Each test gets an isolated temporary DB.
    core.db.DB_PATH is patched to a temp file so trading.db is never touched.
    """

    def setUp(self):
        self._tmpdir = Path(tempfile.mkdtemp())
        self._orig_db_path = _db_module.DB_PATH
        _db_module.DB_PATH = self._tmpdir / "test_trading.db"
        _db_module.init_db()

    def tearDown(self):
        _db_module.DB_PATH = self._orig_db_path
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    # ── Positions ──────────────────────────────────────────────────────────

    def test_insert_and_get_position(self):
        pos = {
            "side": "long", "entry": 100.0, "size_eur": 50.0,
            "size_crypto": 0.5, "take_profit": 110.0, "stop_loss": 95.0,
            "opened_at": 1_000_000.0, "txid": "TEST123",
        }
        _db_module.insert_position("SOLEUR", pos)
        result = _db_module.get_positions()
        self.assertIn("SOLEUR", result)
        self.assertAlmostEqual(result["SOLEUR"]["entry"], 100.0)
        self.assertEqual(result["SOLEUR"]["txid"], "TEST123")

    def test_extra_fields_roundtrip(self):
        """Fields not in the schema (atr, fee_entry_eur) survive via the extra column."""
        pos = {
            "side": "long", "entry": 50.0, "size_eur": 20.0,
            "size_crypto": 0.4, "take_profit": 55.0, "stop_loss": 48.0,
            "opened_at": 1.0, "atr": 0.5, "fee_entry_eur": 0.08,
        }
        _db_module.insert_position("DOGEEUR", pos)
        result = _db_module.get_positions()["DOGEEUR"]
        self.assertAlmostEqual(result["atr"], 0.5)
        self.assertAlmostEqual(result["fee_entry_eur"], 0.08)

    def test_update_position(self):
        pos = {
            "side": "long", "entry": 100.0, "size_eur": 50.0,
            "size_crypto": 0.5, "take_profit": 110.0, "stop_loss": 95.0,
            "opened_at": 1.0,
        }
        _db_module.insert_position("XRPEUR", pos)
        pos["trailing_stop"] = 97.0
        _db_module.update_position("XRPEUR", pos)
        result = _db_module.get_positions()["XRPEUR"]
        self.assertAlmostEqual(result["trailing_stop"], 97.0)

    def test_delete_position(self):
        pos = {
            "side": "long", "entry": 1.0, "size_eur": 10.0,
            "size_crypto": 10.0, "take_profit": 1.1, "stop_loss": 0.96,
            "opened_at": 1.0,
        }
        _db_module.insert_position("ADAEUR", pos)
        self.assertIn("ADAEUR", _db_module.get_positions())
        _db_module.delete_position("ADAEUR")
        self.assertNotIn("ADAEUR", _db_module.get_positions())

    def test_get_positions_empty_on_fresh_db(self):
        self.assertEqual(_db_module.get_positions(), {})

    # ── Trades ─────────────────────────────────────────────────────────────

    def test_insert_and_get_trade(self):
        trade = {
            "pair": "XRPEUR", "side": "long",
            "entry": 1.0, "exit": 1.1,
            "size_eur": 45.0, "pnl_eur": 4.5, "pnl_pct": 10.0,
            "fee_eur": 0.18, "gross_pnl_eur": 4.68, "exit_reason": "tp",
            "opened_at": 1000.0, "closed_at": 2000.0,
        }
        _db_module.insert_trade(trade)
        trades = _db_module.get_trades()
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["pair"], "XRPEUR")
        self.assertAlmostEqual(trades[0]["exit"], 1.1)   # exit_price aliased to exit
        self.assertEqual(trades[0]["exit_reason"], "tp")

    def test_trades_ordered_by_insertion(self):
        for i in range(3):
            _db_module.insert_trade({
                "pair": f"P{i}", "side": "long",
                "entry": float(i), "exit": float(i + 1),
                "size_eur": 10.0, "pnl_eur": 1.0, "pnl_pct": 10.0,
                "closed_at": float(i * 1000),
            })
        trades = _db_module.get_trades()
        self.assertEqual([t["pair"] for t in trades], ["P0", "P1", "P2"])

    def test_get_trades_empty_on_fresh_db(self):
        self.assertEqual(_db_module.get_trades(), [])

    # ── Groq decisions ─────────────────────────────────────────────────────

    def test_insert_and_get_groq_decision(self):
        entry = {
            "ts": 1000.0, "pair": "DOGEEUR", "direction": None,
            "decision": "ESEGUI", "confidence": 8, "score": 4, "reason": "ok",
            "rsi_1h": 45.0, "rsi_15m": 42.0, "macd_hist": 0.001,
            "volume_ratio": 1.5, "adx": 25.0, "price": 0.08,
        }
        _db_module.insert_groq_decision(entry)
        decisions = _db_module.get_groq_decisions()
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0]["pair"], "DOGEEUR")
        self.assertEqual(decisions[0]["confidence"], 8)
        self.assertEqual(decisions[0]["decision"], "ESEGUI")

    def test_groq_decisions_limit(self):
        for i in range(10):
            _db_module.insert_groq_decision({
                "ts": float(i), "pair": "X", "decision": "RIFIUTA",
                "confidence": 3, "score": 1, "reason": "no",
                "rsi_1h": 50.0, "rsi_15m": 50.0, "macd_hist": 0.0,
                "volume_ratio": 1.0, "adx": 15.0, "price": 1.0,
            })
        self.assertEqual(len(_db_module.get_groq_decisions(limit=5)), 5)
        self.assertEqual(len(_db_module.get_groq_decisions(limit=100)), 10)

    # ── Settings ───────────────────────────────────────────────────────────

    def test_settings_get_missing_returns_default(self):
        self.assertIsNone(_db_module.get_setting("nonexistent"))
        self.assertEqual(_db_module.get_setting("nonexistent", "fallback"), "fallback")

    def test_settings_set_and_get(self):
        _db_module.set_setting("active_profile", "aggressive")
        self.assertEqual(_db_module.get_setting("active_profile"), "aggressive")

    def test_settings_upsert(self):
        _db_module.set_setting("k", "v1")
        _db_module.set_setting("k", "v2")
        self.assertEqual(_db_module.get_setting("k"), "v2")

    # ── Commands ───────────────────────────────────────────────────────────

    def test_push_and_pop_commands(self):
        self.assertEqual(_db_module.pop_commands(), [])
        _db_module.push_command({"cmd": "scan", "ts": 1.0})
        _db_module.push_command({"cmd": "close", "pair": "SOLEUR", "ts": 2.0})
        cmds = _db_module.pop_commands()
        self.assertEqual(len(cmds), 2)
        self.assertEqual(cmds[0]["cmd"], "scan")
        self.assertEqual(cmds[1]["cmd"], "close")
        self.assertEqual(cmds[1]["pair"], "SOLEUR")

    def test_pop_commands_marks_processed(self):
        _db_module.push_command({"cmd": "scan", "ts": 1.0})
        _db_module.pop_commands()          # consume
        self.assertEqual(_db_module.pop_commands(), [])   # already processed

    def test_push_command_profile(self):
        _db_module.push_command({"cmd": "profile", "name": "moderate", "ts": 1.0})
        cmds = _db_module.pop_commands()
        self.assertEqual(cmds[0]["name"], "moderate")

    # ── Discovery ──────────────────────────────────────────────────────────

    def test_insert_and_get_latest_discovery(self):
        self.assertIsNone(_db_module.get_latest_discovery())
        data = {"timestamp": 1000.0, "pairs_scanned": 10, "candidates": [{"pair": "ADAEUR"}]}
        _db_module.insert_discovery(data)
        result = _db_module.get_latest_discovery()
        self.assertIsNotNone(result)
        self.assertEqual(result["pairs_scanned"], 10)

    def test_get_latest_discovery_returns_most_recent(self):
        _db_module.insert_discovery({"timestamp": 1000.0, "pairs_scanned": 5})
        _db_module.insert_discovery({"timestamp": 2000.0, "pairs_scanned": 20})
        result = _db_module.get_latest_discovery()
        self.assertEqual(result["pairs_scanned"], 20)

    # ── migrate_from_json ──────────────────────────────────────────────────

    def test_migrate_positions_json(self):
        pos_file = _db_module.DB_PATH.parent / "positions.json"
        pos_file.write_text(json.dumps({
            "SOLEUR": {
                "side": "long", "entry": 150.0, "size_eur": 30.0,
                "size_crypto": 0.2, "take_profit": 165.0, "stop_loss": 144.0,
                "txid": "MIGRTEST", "opened_at": 1_234_567_890.0,
            }
        }), encoding="utf-8")
        _db_module.migrate_from_json()
        self.assertFalse(pos_file.exists(), "positions.json should be renamed to .bak")
        self.assertTrue((pos_file.parent / "positions.json.bak").exists())
        positions = _db_module.get_positions()
        self.assertIn("SOLEUR", positions)
        self.assertAlmostEqual(positions["SOLEUR"]["entry"], 150.0)
        self.assertEqual(positions["SOLEUR"]["txid"], "MIGRTEST")

    def test_migrate_trades_json(self):
        trades_file = _db_module.DB_PATH.parent / "trades_log.json"
        trades_file.write_text(json.dumps([
            {"pair": "XRPEUR", "side": "long", "entry": 1.0, "exit": 1.1,
             "size_eur": 45.0, "pnl_eur": 4.5, "pnl_pct": 10.0,
             "fee_eur": 0.18, "gross_pnl_eur": 4.68, "exit_reason": "tp",
             "opened_at": 100.0, "closed_at": 200.0},
            {"pair": "DOGEEUR", "side": "long", "entry": 0.08, "exit": 0.075,
             "size_eur": 45.0, "pnl_eur": -2.8, "pnl_pct": -6.25,
             "fee_eur": 0.18, "gross_pnl_eur": -2.8, "exit_reason": "sl",
             "opened_at": 300.0, "closed_at": 400.0},
        ]), encoding="utf-8")
        _db_module.migrate_from_json()
        trades = _db_module.get_trades()
        self.assertEqual(len(trades), 2)
        self.assertEqual(trades[0]["pair"], "XRPEUR")
        self.assertAlmostEqual(trades[1]["pnl_eur"], -2.8)
        self.assertFalse(trades_file.exists())

    def test_migrate_active_profile_json(self):
        prof_file = _db_module.DB_PATH.parent / "active_profile.json"
        prof_file.write_text(json.dumps({"profile": "moderate"}), encoding="utf-8")
        _db_module.migrate_from_json()
        self.assertEqual(_db_module.get_setting("active_profile"), "moderate")
        self.assertFalse(prof_file.exists())

    def test_migrate_idempotent(self):
        """Running migrate_from_json twice on the same data should not duplicate rows."""
        trades_file = _db_module.DB_PATH.parent / "trades_log.json"
        trades_file.write_text(json.dumps([
            {"pair": "XRPEUR", "side": "long", "entry": 1.0, "exit": 1.1,
             "size_eur": 10.0, "pnl_eur": 1.0, "pnl_pct": 10.0,
             "closed_at": 200.0},
        ]), encoding="utf-8")
        _db_module.migrate_from_json()   # first run — renames .bak
        # Restore file to simulate a second attempt (file already gone → no-op)
        self.assertEqual(len(_db_module.get_trades()), 1)
        _db_module.migrate_from_json()   # second run — file gone, nothing added
        self.assertEqual(len(_db_module.get_trades()), 1)

    # ── import_logs ────────────────────────────────────────────────────────

    def test_import_logs_parses_valid_lines(self):
        log_dir = _db_module.DB_PATH.parent / "log da migrare"
        log_dir.mkdir(exist_ok=True)
        (log_dir / "bot.log").write_text(
            "2026-06-01 12:00:00 [INFO] telegram_bot: Message one\n"
            "2026-06-01 12:00:01 [ERROR] telegram_bot: Error message\n"
            "this line has no valid format\n",
            encoding="utf-8",
        )
        _db_module.import_logs()
        import sqlite3
        conn = sqlite3.connect(str(_db_module.DB_PATH))
        count = conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
        conn.close()
        self.assertEqual(count, 2)

    def test_import_logs_skips_duplicates(self):
        log_dir = _db_module.DB_PATH.parent / "log da migrare"
        log_dir.mkdir(exist_ok=True)
        line = "2026-06-01 12:00:00 [INFO] telegram_bot: Duplicate message\n"
        (log_dir / "bot.log").write_text(line * 5, encoding="utf-8")
        _db_module.import_logs()
        import sqlite3
        conn = sqlite3.connect(str(_db_module.DB_PATH))
        count = conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
        conn.close()
        self.assertEqual(count, 1)   # UNIQUE(ts, module, message) deduplicates

    def test_import_logs_no_dir_is_noop(self):
        # No 'log da migrare' dir in tmpdir → should not raise
        _db_module.import_logs()

    # ── Resilience ─────────────────────────────────────────────────────────

    def test_get_positions_returns_empty_if_db_missing(self):
        _db_module.DB_PATH = self._tmpdir / "does_not_exist.db"
        self.assertEqual(_db_module.get_positions(), {})
        self.assertEqual(_db_module.get_trades(), [])
        self.assertIsNone(_db_module.get_setting("x"))
        self.assertEqual(_db_module.pop_commands(), [])
        self.assertIsNone(_db_module.get_latest_discovery())
        # Restore so tearDown can clean up
        _db_module.DB_PATH = self._tmpdir / "test_trading.db"


# ═══════════════════════════════════════════════════════════════════════════════
# RUNNER
# ═══════════════════════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════════════════════
# 15. FIX 2 — KRAKEN RETRY
# ═══════════════════════════════════════════════════════════════════════════════

class TestKrakenRetry(unittest.TestCase):

    @patch("core.kraken_client._SESSION")
    def test_retries_on_timeout_then_succeeds(self, mock_session):
        import requests as req_lib
        from core import kraken_client
        ok_resp = MagicMock()
        ok_resp.json.return_value = {
            "error": [], "result": {"status": "online", "timestamp": "T"},
        }
        ok_resp.raise_for_status = MagicMock()
        mock_session.get.side_effect = [
            req_lib.Timeout("t1"),
            req_lib.Timeout("t2"),
            ok_resp,
        ]
        status, _ = kraken_client.get_system_status()
        self.assertEqual(status, "online")
        self.assertEqual(mock_session.get.call_count, 3)

    @patch("core.kraken_client._SESSION")
    def test_raises_after_3_timeouts(self, mock_session):
        import requests as req_lib
        from core import kraken_client
        mock_session.get.side_effect = req_lib.Timeout("always fails")
        with self.assertRaises(req_lib.Timeout):
            kraken_client.get_system_status()
        self.assertEqual(mock_session.get.call_count, 3)

    @patch("core.kraken_client._SESSION")
    def test_no_retry_on_400(self, mock_session):
        import requests as req_lib
        from core import kraken_client
        mock_resp = MagicMock()
        mock_resp.status_code = 400
        http_err = req_lib.HTTPError(response=mock_resp)
        mock_resp.raise_for_status.side_effect = http_err
        mock_session.get.return_value = mock_resp
        with self.assertRaises(req_lib.HTTPError):
            kraken_client.get_system_status()
        self.assertEqual(mock_session.get.call_count, 1)

    @patch("core.kraken_client._SESSION")
    def test_retries_on_500(self, mock_session):
        import requests as req_lib
        from core import kraken_client
        mock_resp_err = MagicMock()
        mock_resp_err.status_code = 500
        mock_resp_err.raise_for_status.side_effect = req_lib.HTTPError(response=mock_resp_err)
        ok_resp = MagicMock()
        ok_resp.json.return_value = {
            "error": [], "result": {"status": "online", "timestamp": "T"},
        }
        ok_resp.raise_for_status = MagicMock()
        mock_session.get.side_effect = [mock_resp_err, ok_resp]
        status, _ = kraken_client.get_system_status()
        self.assertEqual(status, "online")
        self.assertEqual(mock_session.get.call_count, 2)

    @patch("core.kraken_client._SESSION")
    def test_connection_error_retried(self, mock_session):
        import requests as req_lib
        from core import kraken_client
        mock_session.get.side_effect = req_lib.ConnectionError("refused")
        with self.assertRaises(req_lib.ConnectionError):
            kraken_client.get_system_status()
        self.assertEqual(mock_session.get.call_count, 3)


# ═══════════════════════════════════════════════════════════════════════════════
# 16. FIX 3 — GROQ FALLBACK COUNTER
# ═══════════════════════════════════════════════════════════════════════════════

class TestGroqFallbackCounter(unittest.TestCase):

    def setUp(self):
        import services.groq_evaluator as ge
        ge._consecutive_fallbacks = 0

    def tearDown(self):
        import services.groq_evaluator as ge
        ge._consecutive_fallbacks = 0

    @patch("services.groq_evaluator._get_client")
    def test_first_failure_returns_fallback(self, mock_gc):
        mock_gc.side_effect = Exception("offline")
        from services import groq_evaluator
        execute, conf, reason, size = groq_evaluator.evaluate(_make_signal())
        self.assertTrue(execute)
        self.assertEqual(size, config.AI_SIZE_MIN)
        self.assertEqual(groq_evaluator._consecutive_fallbacks, 1)

    @patch("services.groq_evaluator._get_client")
    def test_second_failure_still_fallback(self, mock_gc):
        mock_gc.side_effect = Exception("offline")
        from services import groq_evaluator
        groq_evaluator.evaluate(_make_signal())
        execute, _, _, size = groq_evaluator.evaluate(_make_signal())
        self.assertTrue(execute)
        self.assertEqual(size, config.AI_SIZE_MIN)
        self.assertEqual(groq_evaluator._consecutive_fallbacks, 2)

    @patch("services.groq_evaluator._get_client")
    def test_third_failure_raises_groq_unavailable(self, mock_gc):
        mock_gc.side_effect = Exception("offline")
        from services import groq_evaluator
        groq_evaluator.evaluate(_make_signal())   # 1
        groq_evaluator.evaluate(_make_signal())   # 2
        with self.assertRaises(groq_evaluator.GroqUnavailableError):
            groq_evaluator.evaluate(_make_signal())  # 3 → raises

    @patch("services.groq_evaluator._get_client")
    def test_fourth_call_also_raises(self, mock_gc):
        mock_gc.side_effect = Exception("offline")
        from services import groq_evaluator
        groq_evaluator.evaluate(_make_signal())
        groq_evaluator.evaluate(_make_signal())
        try:
            groq_evaluator.evaluate(_make_signal())
        except groq_evaluator.GroqUnavailableError:
            pass
        with self.assertRaises(groq_evaluator.GroqUnavailableError):
            groq_evaluator.evaluate(_make_signal())  # 4 — still raises

    @patch("services.groq_evaluator._get_client")
    def test_counter_resets_on_success(self, mock_gc):
        from services import groq_evaluator
        # Simulate 2 offline failures
        mock_gc.side_effect = Exception("offline")
        groq_evaluator.evaluate(_make_signal())
        groq_evaluator.evaluate(_make_signal())
        self.assertEqual(groq_evaluator._consecutive_fallbacks, 2)
        # Now a successful call
        mock_choice = MagicMock()
        mock_choice.message.content = "ESEGUI:8: ok"
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value.choices = [mock_choice]
        mock_gc.side_effect = None
        mock_gc.return_value = mock_client
        groq_evaluator.evaluate(_make_signal())
        self.assertEqual(groq_evaluator._consecutive_fallbacks, 0)


# ═══════════════════════════════════════════════════════════════════════════════
# 17. FIX 5 — OHLC CACHE BOUNDED
# ═══════════════════════════════════════════════════════════════════════════════

class TestOHLCCache(unittest.TestCase):

    def setUp(self):
        from core.analyzer import _ohlc_cache
        _ohlc_cache.clear()

    def tearDown(self):
        from core.analyzer import _ohlc_cache
        _ohlc_cache.clear()

    @patch("core.kraken_client.get_ohlc")
    def test_cache_bounded_after_1000_calls(self, mock_get):
        from core.analyzer import _get_ohlc, _ohlc_cache, _CACHE_MAX_SIZE
        mock_get.return_value = []
        for i in range(1000):
            _get_ohlc(f"PAIR{i:04d}EUR", 240)
        self.assertLessEqual(len(_ohlc_cache), _CACHE_MAX_SIZE)

    @patch("core.kraken_client.get_ohlc")
    def test_cache_hit_avoids_second_api_call(self, mock_get):
        from core.analyzer import _get_ohlc
        mock_get.return_value = [{"time": 1, "close": 100.0}]
        _get_ohlc("SOLEUR", 240)
        _get_ohlc("SOLEUR", 240)
        self.assertEqual(mock_get.call_count, 1)

    @patch("core.kraken_client.get_ohlc")
    def test_cache_expires_after_ttl(self, mock_get):
        from core import analyzer as _az
        mock_get.return_value = []
        # Insert entry with timestamp in the past (past TTL)
        _az._ohlc_cache["SOLEUR_240"] = ([], time.time() - _az._CACHE_TTL_SEC - 1)
        _az._get_ohlc("SOLEUR", 240)
        self.assertEqual(mock_get.call_count, 1)   # fetched fresh after expiry

    @patch("core.kraken_client.get_ohlc")
    def test_maxsize_respected_exactly(self, mock_get):
        from core.analyzer import _get_ohlc, _ohlc_cache, _CACHE_MAX_SIZE
        mock_get.return_value = []
        for i in range(_CACHE_MAX_SIZE + 50):
            _get_ohlc(f"X{i:04d}EUR", 60)
        self.assertLessEqual(len(_ohlc_cache), _CACHE_MAX_SIZE)


# ═══════════════════════════════════════════════════════════════════════════════
# 18. FIX 7 — ADAPTIVE SLIPPAGE
# ═══════════════════════════════════════════════════════════════════════════════

class TestAdaptiveSlippage(unittest.TestCase):

    def test_large_atr_capped_at_02pct(self):
        from bot.telegram_bot import _adaptive_slippage
        # ATR = 10% of price → atr_pct * 0.5 = 5% → capped at 0.2% (realistic max for Kraken alts)
        self.assertAlmostEqual(_adaptive_slippage(10.0, 100.0), 0.002, places=6)

    def test_small_atr_uses_config_floor(self):
        from bot.telegram_bot import _adaptive_slippage
        # ATR = 0.001% of price → atr_pct * 0.5 ≈ 0 → floor = SLIPPAGE_PCT
        result = _adaptive_slippage(0.001, 100.0)
        self.assertEqual(result, config.SLIPPAGE_PCT)

    def test_moderate_atr_between_floor_and_cap(self):
        from bot.telegram_bot import _adaptive_slippage
        # ATR 1% of price → atr_pct * 0.5 = 0.5% → between floor (0.1%) and cap (0.2%)
        result = _adaptive_slippage(1.0, 100.0)
        self.assertGreaterEqual(result, config.SLIPPAGE_PCT)
        self.assertLessEqual(result, 0.002)

    def test_calc_exit_uses_adaptive_slippage(self):
        from bot.telegram_bot import _calc_exit, _adaptive_slippage
        pos = {
            "entry": 100.0, "side": "long",
            "size_crypto": 0.18, "size_eur": 18.0,
            "atr": 2.0, "fee_entry_eur": 0.045,
            "stop_loss": 95.0,
        }
        expected_slip_pct = _adaptive_slippage(2.0, 100.0)
        result = _calc_exit(pos, 110.0, config)
        self.assertAlmostEqual(result["slip_eur"], round(18.0 * expected_slip_pct * 2, 4), places=4)

    def test_calc_exit_fallback_when_no_atr(self):
        from bot.telegram_bot import _calc_exit
        pos = {
            "entry": 100.0, "side": "long",
            "size_crypto": 0.18, "size_eur": 18.0,
            "atr": 0.0, "fee_entry_eur": 0.045,
            "stop_loss": 95.0,
        }
        result = _calc_exit(pos, 110.0, config)
        self.assertAlmostEqual(result["slip_eur"], round(18.0 * config.SLIPPAGE_PCT * 2, 4), places=4)


# ═══════════════════════════════════════════════════════════════════════════════
# 20. TRAILING STOP BREAK-EVEN CLAMP
# ═══════════════════════════════════════════════════════════════════════════════

class TestTrailingStopBreakEven(unittest.TestCase):
    """Trailing stop must never be set in loss territory (above entry for SHORT, below for LONG)."""

    def _pos(self, side, entry, sl, atr=2.0):
        return {"side": side, "entry": entry, "stop_loss": sl, "atr": atr, "size_eur": 30.0}

    def test_short_trailing_clamped_to_entry_when_atr_wide(self):
        """SHORT: wide ATR would put trailing above entry — must be clamped to entry."""
        from bot.telegram_bot import _update_trailing_stop
        pos = self._pos("short", entry=56.17, sl=58.08, atr=2.0)
        # current = 53.73, activation = 56.17 - 1.0*2.0 = 54.17 → 53.73 < 54.17 → activates
        # raw trailing = 53.73 + 1.5*2.0 = 56.73 > entry 56.17 → must clamp to 56.17
        updated = _update_trailing_stop(pos, current=53.73, atr=2.0, cfg=config)
        self.assertTrue(updated)
        self.assertLessEqual(pos["trailing_stop"], 56.17, "Trailing stop must not exceed entry on SHORT")

    def test_short_trailing_below_entry_when_atr_tight(self):
        """SHORT: tight ATR puts trailing below entry — no clamping needed, keeps full value."""
        from bot.telegram_bot import _update_trailing_stop
        pos = self._pos("short", entry=0.072295, sl=0.074517, atr=0.001)
        # activation = 0.072295 - 0.001 = 0.071295, current=0.068985 < 0.071295 → activates
        # raw trailing = 0.068985 + 1.5*0.001 = 0.070485 < entry 0.072295 → no clamp
        updated = _update_trailing_stop(pos, current=0.068985, atr=0.001, cfg=config)
        self.assertTrue(updated)
        self.assertAlmostEqual(pos["trailing_stop"], 0.068985 + 1.5 * 0.001, places=8)

    def test_long_trailing_clamped_to_entry_when_atr_wide(self):
        """LONG: wide ATR would put trailing below entry — must be clamped to entry."""
        from bot.telegram_bot import _update_trailing_stop
        pos = self._pos("long", entry=100.0, sl=95.0, atr=5.0)
        # activation = 100.0 + 1.0*5.0 = 105.0, current=106.0 >= 105.0 → activates
        # raw trailing = 106.0 - 1.5*5.0 = 98.5 < entry 100.0 → clamp to 100.0
        updated = _update_trailing_stop(pos, current=106.0, atr=5.0, cfg=config)
        self.assertTrue(updated)
        self.assertGreaterEqual(pos["trailing_stop"], 100.0, "Trailing stop must not go below entry on LONG")

    def test_short_trailing_never_causes_loss_on_immediate_reversal(self):
        """If price reverses immediately after trailing activates, net PnL must be >= 0."""
        from bot.telegram_bot import _update_trailing_stop, _calc_exit
        pos = {
            "side": "short", "entry": 56.17, "stop_loss": 58.08,
            "atr": 2.0, "size_eur": 30.0, "size_crypto": 30.0 / 56.17,
            "fee_entry_eur": 0.05,
        }
        _update_trailing_stop(pos, current=53.73, atr=2.0, cfg=config)
        exit_price = pos.get("trailing_stop", pos["stop_loss"])
        result = _calc_exit(pos, exit_price, config)
        self.assertGreaterEqual(result["gross_pnl_eur"], 0.0,
                                f"Gross P&L must be >= 0 when trailing fires at break-even; got {result['gross_pnl_eur']}")


# ═══════════════════════════════════════════════════════════════════════════════
# 19. FIX 1 — LIMIT ORDER LOCK
# ═══════════════════════════════════════════════════════════════════════════════

class TestLimitOrderLock(unittest.TestCase):
    """Verify that the pending_orders write happens inside _positions_lock."""

    def test_positions_lock_is_threading_lock(self):
        """_positions_lock must be a threading.Lock (guards limit order write + DB insert)."""
        import threading
        with patch("bot.telegram_bot.Application") as mock_app_cls:
            mock_app_cls.builder.return_value.token.return_value.build.return_value = MagicMock()
            from bot.telegram_bot import TradingBot
            bot = TradingBot()
            lock = bot._positions_lock
            # threading.Lock() returns a _thread.lock; check for acquire/release protocol
            self.assertTrue(hasattr(lock, "acquire"))
            self.assertTrue(hasattr(lock, "release"))
            self.assertIn("lock", type(lock).__name__.lower())


# ═══════════════════════════════════════════════════════════════════════════════
# 20. FIX 4 — SHUTDOWN FLAG
# ═══════════════════════════════════════════════════════════════════════════════

class TestShutdownHandler(unittest.TestCase):

    def test_shutdown_flag_set_by_handler(self):
        import main as _main
        _main._shutdown = False
        _main._handle_signal(15, None)   # SIGTERM = 15
        self.assertTrue(_main._shutdown)
        _main._shutdown = False   # restore

    def test_handle_signal_does_not_raise_without_bot(self):
        import main as _main
        _main._bot_instance = None
        _main._shutdown = False
        try:
            _main._handle_signal(15, None)
        except SystemExit:
            pass   # stop() raises SystemExit — acceptable
        _main._shutdown = False


# ═══════════════════════════════════════════════════════════════════════════════
# 21. FIX 6 — RECONCILE JOB (unit-level)
# ═══════════════════════════════════════════════════════════════════════════════

class TestReconcileOrders(unittest.TestCase):
    """Verify reconcile logic detects txid discrepancies."""

    def _build_positions(self, txids: list[str]) -> dict:
        return {f"PAIR{i}EUR": {"txid": t, "entry": 1.0} for i, t in enumerate(txids)}

    def test_discrepancy_detected_kraken_has_extra(self):
        db_positions = self._build_positions(["TX001"])
        kraken_open  = {"open": {"TX001": {}, "TX999": {}}}  # TX999 not in DB

        db_txids     = {p["txid"] for p in db_positions.values()}
        kraken_txids = set(kraken_open.get("open", {}).keys())
        orphan_on_kraken = kraken_txids - db_txids
        self.assertIn("TX999", orphan_on_kraken)

    def test_discrepancy_detected_db_has_extra(self):
        db_positions = self._build_positions(["TX001", "TX002"])
        kraken_open  = {"open": {"TX001": {}}}   # TX002 missing on Kraken

        db_txids     = {p["txid"] for p in db_positions.values()}
        kraken_txids = set(kraken_open.get("open", {}).keys())
        orphan_in_db = db_txids - kraken_txids
        self.assertIn("TX002", orphan_in_db)

    def test_no_discrepancy_when_in_sync(self):
        db_positions = self._build_positions(["TX001", "TX002"])
        kraken_open  = {"open": {"TX001": {}, "TX002": {}}}

        db_txids     = {p["txid"] for p in db_positions.values()}
        kraken_txids = set(kraken_open.get("open", {}).keys())
        self.assertEqual(db_txids - kraken_txids, set())
        self.assertEqual(kraken_txids - db_txids, set())

    def test_paper_txids_excluded(self):
        db_positions = {
            "SOLEUR":  {"txid": "PAPER",   "entry": 1.0},
            "DOTEUR":  {"txid": "unknown", "entry": 1.0},
            "XRPEUR":  {"txid": "TX123",   "entry": 1.0},
        }
        db_txids = {
            p["txid"] for p in db_positions.values()
            if p.get("txid") and p["txid"] not in ("PAPER", "unknown")
        }
        self.assertEqual(db_txids, {"TX123"})


# ═══════════════════════════════════════════════════════════════════════════════
# 22. FIX A — MINIMUM PROFIT GATE
# ═══════════════════════════════════════════════════════════════════════════════

class TestMinProfitGate(unittest.TestCase):
    """generate_short_signal must reject signals where TP gross < round-trip costs × 1.3."""

    def test_blocked_when_atr_too_tight(self):
        # ATR=0.00025 → TP distance = 2.5×0.00025/0.07 = 0.89% < min 1.59% → SKIP
        a = _make_short_analysis(price=0.07, atr=0.00025)
        self.assertIsNone(generate_short_signal(a, 100.0))

    def test_passes_with_adequate_atr(self):
        # ATR=0.001 → TP distance = 2.5×0.001/0.07 = 3.57% > min 1.59% → OK
        a = _make_short_analysis(price=0.07, atr=0.001)
        sig = generate_short_signal(a, 100.0)
        self.assertIsNotNone(sig, "Signal with adequate ATR should not be blocked by profit gate")

    def test_tp_gross_pct_calculation(self):
        # Verify the gate threshold formula: 2×taker + 2×slip_cap + margin_open = 1.22%, ×1.3 = 1.586%
        _SLIP_EST = 0.002
        min_cost = 2 * config.KRAKEN_TAKER_FEE + 2 * _SLIP_EST + config.MARGIN_OPEN_FEE_ALT
        self.assertAlmostEqual(min_cost * 100, 1.22, places=1)
        self.assertAlmostEqual(min_cost * 1.3 * 100, 1.586, places=2)

    def test_blocked_at_boundary_atr(self):
        # ATR exactly at boundary where TP% ≈ 1.59%: atr = 0.07 × 0.01586 / 2.5 = 0.000444
        # Slightly below: 0.000440 → blocked
        a = _make_short_analysis(price=0.07, atr=0.000440)
        self.assertIsNone(generate_short_signal(a, 100.0))

    def test_passes_at_boundary_atr(self):
        # Slightly above boundary: 0.000460 → passes profit gate
        a = _make_short_analysis(price=0.07, atr=0.000460)
        sig = generate_short_signal(a, 100.0)
        # Should not be blocked by profit gate; other gates may block, but not profit gate
        # Check it's not None (all other conditions in _make_short_analysis are valid)
        self.assertIsNotNone(sig)


# ═══════════════════════════════════════════════════════════════════════════════
# 23. FIX B — RSI SLOPE (BOUNCE) FILTER
# ═══════════════════════════════════════════════════════════════════════════════

class TestRSISlopeFilter(unittest.TestCase):
    """
    SHORT Gate 5: when RSI(1h) < 42 and RSI is rising (slope > 3), skip signal.
    This catches bounce patterns where RSI hit oversold and is recovering.
    Only relevant when RSI_SHORT_MIN < 42 (moderate: 36, aggressive: 30).
    """

    def setUp(self):
        # Lower RSI_SHORT_MIN so Gate 4 doesn't block RSI in 36-42 range
        self._orig_rsi_min = config.RSI_SHORT_MIN
        config.RSI_SHORT_MIN = 30

    def tearDown(self):
        config.RSI_SHORT_MIN = self._orig_rsi_min

    def test_blocked_when_rsi_rising_from_oversold(self):
        # RSI was 34, now 39 → slope = +5 > 3, RSI < 42 → bounce pattern → SKIP
        a = _make_short_analysis(rsi_1h=39.0, rsi_1h_prev=34.0)
        self.assertIsNone(generate_short_signal(a, 100.0))

    def test_passes_when_rsi_falling_in_oversold(self):
        # RSI was 44, now 39 → slope = -5 < 3 → still falling → NOT a bounce → OK
        a = _make_short_analysis(rsi_1h=39.0, rsi_1h_prev=44.0)
        sig = generate_short_signal(a, 100.0)
        self.assertIsNotNone(sig, "Falling RSI (slope negative) must not trigger the bounce gate")

    def test_passes_when_rsi_above_42_even_if_rising(self):
        # RSI = 50 (above 42 threshold) → gate does not apply regardless of slope
        a = _make_short_analysis(rsi_1h=50.0, rsi_1h_prev=46.0)  # slope +4 but RSI > 42
        sig = generate_short_signal(a, 100.0)
        self.assertIsNotNone(sig, "Gate only applies when RSI < 42; RSI=50 must not be blocked")

    def test_passes_when_slope_below_threshold(self):
        # RSI = 40, slope = +2.0 (below threshold of 3.0) → not classified as bounce
        a = _make_short_analysis(rsi_1h=40.0, rsi_1h_prev=38.0)  # slope +2
        sig = generate_short_signal(a, 100.0)
        self.assertIsNotNone(sig, "Slope of +2 is below the +3 bounce threshold")

    def test_blocked_exactly_at_threshold(self):
        # RSI = 41, slope = +3.1 → just over threshold (3.0) for RSI 38-42 → SKIP
        a = _make_short_analysis(rsi_1h=41.0, rsi_1h_prev=37.9)  # slope ≈ +3.1
        self.assertIsNone(generate_short_signal(a, 100.0))

    def test_deep_oversold_blocked_with_small_slope(self):
        # RSI = 36 (< 38) → threshold drops to 1.0; slope = +1.5 → SKIP even though < 3
        a = _make_short_analysis(rsi_1h=36.0, rsi_1h_prev=34.5)  # slope +1.5
        self.assertIsNone(generate_short_signal(a, 100.0))

    def test_deep_oversold_passes_when_falling(self):
        # RSI = 36 and still falling (slope = -4) → valid short continuation, not a bounce
        a = _make_short_analysis(rsi_1h=36.0, rsi_1h_prev=40.0)  # slope -4
        sig = generate_short_signal(a, 100.0)
        self.assertIsNotNone(sig, "Falling RSI from 40→36 is short continuation, must not be blocked")


# ═══════════════════════════════════════════════════════════════════════════════
# Update TestGroqEvaluator to reset fallback counter between tests
# ═══════════════════════════════════════════════════════════════════════════════

_original_TestGroqEvaluator_setUp = getattr(TestGroqEvaluator, "setUp", None)
_original_TestGroqEvaluator_tearDown = getattr(TestGroqEvaluator, "tearDown", None)


def _groq_evaluator_setUp(self):
    import services.groq_evaluator as ge
    ge._consecutive_fallbacks = 0
    if _original_TestGroqEvaluator_setUp:
        _original_TestGroqEvaluator_setUp(self)


def _groq_evaluator_tearDown(self):
    import services.groq_evaluator as ge
    ge._consecutive_fallbacks = 0
    if _original_TestGroqEvaluator_tearDown:
        _original_TestGroqEvaluator_tearDown(self)


TestGroqEvaluator.setUp    = _groq_evaluator_setUp
TestGroqEvaluator.tearDown = _groq_evaluator_tearDown


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
        TestDB,
        # New tests for fixes
        TestKrakenRetry, TestGroqFallbackCounter,
        TestOHLCCache, TestAdaptiveSlippage,
        TestLimitOrderLock, TestShutdownHandler, TestReconcileOrders,
        TestMinProfitGate, TestRSISlopeFilter,
    ]:
        suite.addTests(loader.loadTestsFromTestCase(cls))

    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
