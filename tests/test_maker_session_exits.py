"""Offline regressions: routine exits must not turn into taker orders.

Normal MID mode (default) holds a reduce-only exit at the fee-aware profit
anchor and never cuts a position on an ordinary stop or max-hold signal. The
legacy loss-cut windows remain available behind OBI_MID_LOSS_CUTS=1.
"""
import importlib
import time
import unittest
from unittest.mock import Mock, patch

class MakerExitTests(unittest.TestCase):
    """Legacy loss-cut windows: never convert a timer into a taker order."""
    def setUp(self):
        self.mod = importlib.import_module("zeus.bots.order_book")
        self.enterContext(patch.object(self.mod, "MID_LOSS_CUTS_ENABLED", True))
        self.bot = self.mod.OrderBookBot.__new__(self.mod.OrderBookBot)
        self.bot._hard_exit_at = 0
        self.bot._hard_exit_reason = ""
        self.bot.bank_until = self.bot.pause_until = 0
        self.bot.force_quote = False
        self.bot._eff_stop_bps = Mock(return_value=10)
        self.bot._eff_max_hold = Mock(return_value=60)
        self.bot.flatten = Mock()

    def test_expired_max_hold_never_escalates_to_ioc(self):
        b=self.bot
        b._manage_session_exit(1000,1,0,0,1,1)
        self.assertEqual(b._hard_exit_reason,"max hold")
        for now in (1010,1100,2000):
            b._manage_session_exit(now,1,0,0,1,1)
            self.assertGreater(b.bank_until,now)
        b.flatten.assert_not_called()

    def test_normal_adverse_exit_stays_maker_after_timeout(self):
        b=self.bot
        b._manage_session_exit(1000,1,-1,-11,990,1)
        b._manage_session_exit(1020,1,-1,-11,990,1)
        b.flatten.assert_not_called()
        self.assertTrue(b.force_quote)
        self.assertGreater(b.bank_until,1020)

    def test_true_runaway_protection_stays_maker(self):
        b=self.bot
        b._hard_exit_at=1005
        b._hard_exit_reason="max hold"
        b._manage_session_exit(1010,1,-3,-21,990,1)
        b.flatten.assert_not_called()
        self.assertIn("runaway",b._hard_exit_reason)
        self.assertGreater(b.bank_until,1010)
        self.assertTrue(b.force_quote)

    def test_flat_clears_maker_exit(self):
        b=self.bot
        b._hard_exit_at=1000
        b._hard_exit_reason="max hold"
        b._manage_session_exit(1010,0,0,0,1,1)
        self.assertEqual(b._hard_exit_at,0)
        self.assertEqual(b._hard_exit_reason,"")
        b.flatten.assert_not_called()


class NormalExitTests(unittest.TestCase):
    """Default mode: no routine loss cuts, only a catastrophic passive stop."""
    def setUp(self):
        self.mod = importlib.import_module("zeus.bots.order_book")
        self.enterContext(patch.object(self.mod, "MID_LOSS_CUTS_ENABLED", False))
        self.enterContext(patch.object(self.mod, "MID_CATASTROPHIC_STOP_BPS", 50.0))
        self.bot = self.mod.OrderBookBot.__new__(self.mod.OrderBookBot)
        self.bot._hard_exit_at = 0
        self.bot._hard_exit_reason = ""
        self.bot.bank_until = self.bot.pause_until = 0
        self.bot.force_quote = False
        self.bot._eff_stop_bps = Mock(return_value=10)
        self.bot._eff_max_hold = Mock(return_value=60)
        self.bot.flatten = Mock()

    def test_ordinary_adverse_move_does_not_open_a_loss_exit(self):
        b=self.bot
        b._manage_session_exit(1000,1,-1,-11,990,1)
        b._manage_session_exit(1020,1,-1,-11,990,1)
        self.assertEqual((b.bank_until,b._hard_exit_at,b._hard_exit_reason),(0,0,""))
        self.assertFalse(b.force_quote)
        b.flatten.assert_not_called()

    def test_position_age_does_not_force_a_loss_exit(self):
        b=self.bot
        b._manage_session_exit(1000,1,0,0,1,1)
        self.assertEqual((b.bank_until,b._hard_exit_reason),(0,""))
        b.flatten.assert_not_called()

    def test_catastrophic_move_still_protects(self):
        b=self.bot
        b._manage_session_exit(1010,1,-5,-60,990,1)
        self.assertIn("runaway",b._hard_exit_reason)
        self.assertGreater(b.bank_until,1010)
        self.assertTrue(b.force_quote)
        b.flatten.assert_not_called()

    def test_toxic_scratch_is_disabled(self):
        b=self.bot
        b.bank_until=0
        b._maybe_toxic_scratch()
        self.assertEqual(b.bank_until,0)

    def test_exit_price_never_follows_the_touch_into_a_loss(self):
        b=self.bot
        now=time.time()
        # Close a long: sell at entry + fees + one tick, above the bid.
        px=b._mid_exit_price(False,99.0,99.1,.1,now,entry=100.0,
                             pos_since=now-120,bank=False,risk_exit=False,
                             entry_rate=.001,maker_rate=.001)
        self.assertGreater(px,100.0)
        self.assertGreaterEqual(px,99.1)
        # Close a short: buy at entry - fees - one tick, below the ask.
        px=b._mid_exit_price(True,100.0,100.1,.1,now,entry=100.0,
                             pos_since=now-120,bank=False,risk_exit=False,
                             entry_rate=.001,maker_rate=.001)
        self.assertLess(px,100.0)
        self.assertLessEqual(px,100.0)


if __name__=="__main__": unittest.main()
