"""Offline leverage/margin setup; no exchange writes or network requests."""
import importlib
import os
import unittest
from unittest.mock import Mock, patch

from zeus.safety import SafetyLatch


class LeverageSetupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with patch.dict(os.environ, {"PRIVATE_KEY": "1" * 64, "WALLET_ADDRESS": "0x" + "1" * 40}):
            cls.engine = importlib.import_module("zeus.bots.order_book")

    def test_metadata_preserves_isolated_requirement_and_caps_leverage(self):
        for symbol, maximum in (("io:OAI", 6), ("io:ANTH", 15), ("io:SNDK", 15)):
            with self.subTest(symbol=symbol):
                payload = [{"universe": [{"name": symbol, "szDecimals": 3,
                                           "maxLeverage": maximum, "onlyIsolated": True}]}, [{"midPx": "100"}]]
                with patch.object(self.engine, "post_info", return_value=payload) as request:
                    meta = self.engine.fetch_token_meta(symbol)
                self.assertTrue(meta["onlyIsolated"])
                self.assertEqual(self.engine.leverage_settings(meta, 20), (maximum, False))
                self.assertEqual(request.call_args.args[1]["dex"], "io")

    def test_cross_market_and_lower_requested_leverage_are_preserved(self):
        self.assertEqual(self.engine.leverage_settings({"maxLeverage": 50, "onlyIsolated": False}, 20), (20, True))
        self.assertEqual(self.engine.leverage_settings({"maxLeverage": 15, "onlyIsolated": True}, 3), (3, False))
        with self.assertRaises(ValueError): self.engine.leverage_settings({"maxLeverage": 0}, 20)

    def test_live_setup_sends_isolated_mode_and_accepts_default_ack(self):
        bot = self.engine.OrderBookBot.__new__(self.engine.OrderBookBot)
        bot.live, bot.running = True, True
        bot.safety = SafetyLatch(lambda: setattr(bot, "running", False))
        raw = Mock()
        raw.update_leverage.return_value = {"status": "ok", "response": {"type": "default"}}
        bot.exchange = bot.safety.wrap(raw)
        leverage, is_cross = self.engine.leverage_settings({"maxLeverage": 6, "onlyIsolated": True}, 20)
        with patch.multiple(self.engine, LEVERAGE=leverage, IS_CROSS=is_cross, SYMBOL="io:OAI"):
            bot.set_leverage()
        raw.update_leverage.assert_called_once_with(6, "io:OAI", is_cross=False)
        self.assertTrue(bot.running)
        self.assertFalse(bot.safety.error)
        raw.order.assert_not_called()

    def test_paper_setup_does_not_send_any_leverage_action(self):
        bot = self.engine.OrderBookBot.__new__(self.engine.OrderBookBot)
        bot.live = False
        bot.exchange = Mock()
        bot.set_leverage()
        bot.exchange.update_leverage.assert_not_called()

    def test_rejection_exposes_sanitized_exchange_reason(self):
        gate = SafetyLatch()
        raw = Mock()
        raw.update_leverage.return_value = {"status": "err", "response": "Cross margin is not available for this asset"}
        with self.assertRaisesRegex(RuntimeError, "Cross margin is not available"):
            gate.wrap(raw).update_leverage(20, "io:ANTH", is_cross=True)
        self.assertIn("Cross margin is not available", gate.error)
        self.assertTrue(gate.stopped.is_set())

    def test_invalid_leverage_ack_does_not_enable_trading(self):
        for value in (None, {}, {"status": "ok"}, {"status": "ok", "response": {"type": "error"}}):
            with self.subTest(value=value):
                gate, raw = SafetyLatch(), Mock()
                raw.update_leverage.return_value = value
                with self.assertRaises(RuntimeError): gate.wrap(raw).update_leverage(6, "io:OAI", is_cross=False)
                self.assertTrue(gate.stopped.is_set())

    def test_rejection_reason_never_prints_a_private_key(self):
        gate, raw = SafetyLatch(), Mock()
        raw.update_leverage.return_value = {"status": "err", "response": "private_key=" + "a" * 64}
        with self.assertRaises(RuntimeError): gate.wrap(raw).update_leverage(6, "io:OAI")
        self.assertNotIn("a" * 64, gate.error)


if __name__ == "__main__": unittest.main()
