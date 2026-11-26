"""WS-first account state, 429 backoff and shared snapshot regressions."""
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from test_outcome_maker_exit import make_bot
from zeus.bots.worldcup import MarketSpec, MarketState, RateLimited, read_info
from zeus.network import retry_read


SPOT = {"data": {"user": "0xabc", "spotState": {"balances": [
    {"coin": "USDC", "total": "100", "hold": "70"},
    {"coin": "+101", "total": "40", "hold": "40", "entryNtl": "20"},
]}}}
ORDERS = {"data": {"user": "0xabc", "dex": "", "orders": [
    {"coin": "#101", "side": "A", "limitPx": "0.50", "sz": "40", "oid": 7},
]}}


class WsAccountTests(unittest.TestCase):
    def bot(self):
        bot = make_bot(order_size_usd=70, max_position_usd=120)
        bot.states = {'#101': MarketState(MarketSpec('#101', 0, balance_coin='+101'))}
        bot.lock = threading.RLock()
        bot.account_settle_until = 0.0
        bot.ws_spot_state = None
        bot.ws_open_orders = None
        bot.ws_spot_at = 0.0
        bot.ws_orders_at = 0.0
        bot.ws_account_ready = False
        bot.ws_account_pending = False
        bot.terminal_oids = {}
        bot.safety = SimpleNamespace(account_known=False)
        bot.available_usdc = 0.0
        return bot

    def test_spot_and_orders_snapshots_replace_account_polling(self):
        bot = self.bot()
        bot.on_spot_state(SPOT)
        self.assertFalse(bot.account_ready())
        bot.on_open_orders(ORDERS)
        state = bot.states['#101']
        self.assertEqual(state.position, 40)
        self.assertEqual(state.available_position, 0)
        self.assertEqual(bot.available_usdc, 30)
        self.assertTrue(bot.account_ready())
        self.assertEqual(state.ask_oid, 7)
        self.assertAlmostEqual(state.quoted_ask, .50)
        self.assertAlmostEqual(state.ask_quantity, 40)
        self.assertFalse(state.reconcile_required)
        self.assertTrue(bot.safety.account_known)

    def test_fill_settle_window_keeps_reconcile_until_next_snapshot(self):
        bot = self.bot()
        bot.on_spot_state(SPOT)
        bot.on_open_orders(ORDERS)
        bot.account_settle_until = time.time() + 2
        bot.states['#101'].reconcile_required = True
        bot.on_open_orders(ORDERS)
        self.assertTrue(bot.states['#101'].reconcile_required)
        bot.account_settle_until = 0
        with patch('zeus.bots.worldcup.time.time', return_value=time.time() + 3):
            bot.on_open_orders(ORDERS)
        self.assertFalse(bot.states['#101'].reconcile_required)

    def test_invalid_snapshot_does_not_mutate_or_claim_ready(self):
        bot = self.bot()
        bad = {"data": {"spotState": {"balances": [
            {"coin": "+101", "total": "10", "hold": "20"}]}}}
        bot.on_spot_state(bad)
        self.assertFalse(bot.ws_account_ready)
        self.assertEqual(bot.states['#101'].position, 0)

    def test_ready_state_expires(self):
        bot = self.bot()
        bot.on_spot_state(SPOT)
        bot.on_open_orders(ORDERS)
        self.assertTrue(bot.account_ready(max_age=10))
        with patch('zeus.bots.worldcup.time.time', return_value=time.time() + 20):
            self.assertFalse(bot.account_ready(max_age=10))

    def test_lagging_snapshot_keeps_newer_local_ack(self):
        bot = self.bot()
        bot.on_spot_state(SPOT)
        bot.on_open_orders({"data": {"orders": []}})
        state = bot.states['#101']
        with bot.lock:
            state.bid_oid = 99
            state.quoted_bid = .40
            state.bid_quantity = 10
            state.bid_order_ts = time.time()
            state.reconcile_required = True
        # A frame generated before the local acknowledgement must not clear
        # the order; that was the open/cancel loop at the same price.
        bot.on_open_orders({"data": {"orders": []}})
        self.assertEqual(state.bid_oid, 99)
        self.assertAlmostEqual(state.quoted_bid, .40)
        self.assertTrue(state.reconcile_required)

    def test_terminal_order_is_not_resurrected_by_a_stale_frame(self):
        bot = self.bot()
        bot.on_spot_state(SPOT)
        bot.on_open_orders(ORDERS)
        state = bot.states['#101']
        with bot.lock:
            state.ask_oid = None
            state.ask_quantity = 0.0
            state.reconcile_required = True
        bot._mark_terminal(7)
        bot.on_open_orders(ORDERS)  # still contains the dead oid
        self.assertIsNone(state.ask_oid)
        self.assertFalse(state.reconcile_required)

    def test_confirmed_snapshot_clears_the_reconcile_gate(self):
        bot = self.bot()
        orders = {"data": {"orders": [
            {"coin": "#101", "side": "B", "limitPx": "0.40", "sz": "10", "oid": 11}]}}
        bot.on_spot_state(SPOT)
        bot.on_open_orders(orders)
        state = bot.states['#101']
        with bot.lock:
            state.reconcile_required = True
        bot.on_open_orders(orders)
        self.assertEqual(state.bid_oid, 11)
        self.assertFalse(state.reconcile_required)

    def test_filled_update_marks_terminal_and_blocks_resurrection(self):
        bot = self.bot()
        bot.on_spot_state(SPOT)
        bot.on_open_orders(ORDERS)
        state = bot.states['#101']
        self.assertEqual(state.ask_oid, 7)
        bot.on_order_updates({'data': [{
            'status': 'filled',
            'order': {'coin': '#101', 'side': 'A', 'oid': 7,
                      'limitPx': '0.50', 'sz': '40'}}]})
        self.assertIsNone(state.ask_oid)
        self.assertIn(7, bot.terminal_oids)
        bot.account_settle_until = 0.0  # settle window elapsed
        bot.on_open_orders(ORDERS)  # delayed frame still lists the filled oid
        self.assertIsNone(state.ask_oid)
        self.assertFalse(state.reconcile_required)

    def test_strict_rest_snapshot_still_clears_and_proves_account(self):
        bot = self.bot()
        state = bot.states['#101']
        with bot.lock:
            state.bid_oid = 123
            state.reconcile_required = True
        spot = {"balances": [{"coin": "USDC", "total": "100", "hold": "0"}]}
        self.assertTrue(bot._apply_account_snapshot(spot, [], source="rest"))
        self.assertIsNone(state.bid_oid)
        self.assertFalse(state.reconcile_required)
        self.assertTrue(bot.safety.account_known)

    def test_pending_ws_snapshot_applies_after_the_settle_window(self):
        bot = self.bot()
        bot.on_spot_state(SPOT)
        bot.on_open_orders(ORDERS)
        state = bot.states['#101']
        bot.account_settle_until = time.time() + 30
        with bot.lock:
            state.reconcile_required = True
        bot.on_open_orders(ORDERS)
        self.assertTrue(bot.ws_account_pending)
        bot.apply_pending_ws_account()
        self.assertTrue(bot.ws_account_pending)
        bot.account_settle_until = 0.0
        bot.apply_pending_ws_account()
        self.assertFalse(bot.ws_account_pending)
        self.assertFalse(state.reconcile_required)


class InfoBackoffTests(unittest.TestCase):
    def tearDown(self):
        import zeus.bots.worldcup as wc
        with wc._INFO_BACKOFF_LOCK:
            wc._INFO_BACKOFF_UNTIL = 0.0
            wc._INFO_BACKOFF_SEC = 0.0

    def test_read_info_honours_429_retry_after(self):
        response = Mock(status_code=429, headers={'Retry-After': '7'})
        session = Mock()
        session.post.return_value = response
        with patch('zeus.bots.worldcup._local', Mock(session=session)):
            with self.assertRaises(RateLimited) as raised:
                read_info('https://api.hyperliquid.xyz', {'type': 'l2Book'})
        self.assertGreaterEqual(raised.exception.retry_after, 7)
        session.post.assert_called_once()

    def test_retry_read_sleeps_for_retry_after(self):
        class Limited(Exception):
            retryable = True
            retry_after = 4.0

        calls = {'n': 0}
        sleeps = []
        def operation():
            calls['n'] += 1
            if calls['n'] < 3:
                raise Limited()
            return 'ok'
        result = retry_read('startup', operation, attempts=3, sleep=sleeps.append)
        self.assertEqual(result, 'ok')
        self.assertEqual(calls['n'], 3)
        self.assertGreaterEqual(min(sleeps), 4.0)


if __name__ == '__main__':
    unittest.main()
