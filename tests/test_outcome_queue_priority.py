"""Keep resting OIDs through book flicker and signing waits; fake exchange only."""
from contextlib import contextmanager
from itertools import count
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from test_outcome_maker_exit import make_bot
from zeus.bots.worldcup import MarketSpec, MarketState
from zeus.safety import SafetyLatch
from zeus.signing import SigningCoordinator


ACK = {'status': 'ok', 'response': {'data': {'statuses': ['success']}}}


class QueuePriorityTests(unittest.TestCase):
    def fixture(self, *, buy=False):
        bot = make_bot(order_size_usd=70, max_position_usd=120)
        bot.available_usdc, bot.account_revision, bot.running = 31.86, 0, True
        bot._quote_cancel_local = threading.local()
        bot.safety = SafetyLatch(strict_maker_spot=True)
        bot.safety.phase = 'running'
        bot.can_send_action = Mock(return_value=True)
        bot._pause_execution = Mock()
        raw = SimpleNamespace(bulk_cancel=Mock(return_value=ACK), order=Mock())
        bot.exchange = bot._queue_checked_exchange(raw)
        st = MarketState(MarketSpec('#101', 0, balance_coin='+101'))
        now = time.time()
        if buy:
            st.update_bbo(.69879, .69889)
            st.bid_oid, st.quoted_bid, st.bid_quantity, st.bid_order_ts = 123, .69879, 100, now-48
        else:
            st.update_bbo(.69869, .69879)
            st.position, st.available_position = 100, 0
            st.ask_oid, st.quoted_ask, st.ask_quantity, st.ask_order_ts = 123, .69879, 100, now-48
        bot.states = {'#101': st}
        # Start the simulated clock at the accepted book publication. On a
        # high-resolution clock update_bbo() occurs after the earlier `now`;
        # rewinding there would correctly reject the next mocked book as old.
        return bot, st, raw, st.last_book_ts

    def test_unchanged_bid_and_ask_keep_original_oid_and_age_with_low_free_cash(self):
        for buy in (True, False):
            with self.subTest(buy=buy):
                bot, st, raw, now = self.fixture(buy=buy)
                side = 'bid' if buy else 'ask'
                old_age = getattr(st, side+'_order_ts')
                for _ in range(20):
                    bot.place_bilateral(st)
                raw.bulk_cancel.assert_not_called()
                raw.order.assert_not_called()
                self.assertEqual(getattr(st, side+'_oid'), 123)
                self.assertEqual(getattr(st, side+'_order_ts'), old_age)
                self.assertEqual(bot.stats.cancels_sent, 0)

    def test_brief_price_move_then_return_never_requeues(self):
        bot, st, raw, now = self.fixture()
        with patch('zeus.bots.worldcup.time.time', return_value=now):
            st.update_bbo(.69868, .69878)
            bot.place_bilateral(st)
        raw.bulk_cancel.assert_not_called()
        with patch('zeus.bots.worldcup.time.time', return_value=now+.1):
            st.update_bbo(.69869, .69879)
            bot.place_bilateral(st)
        self.assertEqual(st.ask_oid, 123)
        self.assertEqual(st.pending_reprices, {})
        raw.bulk_cancel.assert_not_called()

    def test_sustained_price_change_reprices_once_after_confirmation(self):
        # Exercise Linux-like submillisecond clock steps on every platform.
        clock = count(time.time(), .0001)
        with patch('zeus.bots.worldcup.time.time', side_effect=clock.__next__):
            bot, st, raw, now = self.fixture()
        with patch('zeus.bots.worldcup.time.time', return_value=now):
            st.update_bbo(.69868, .69878)
            bot.place_bilateral(st)
        with patch('zeus.bots.worldcup.time.time', return_value=now+.1):
            bot.place_bilateral(st)
        raw.bulk_cancel.assert_not_called()
        with patch('zeus.bots.worldcup.time.time', return_value=now+.3):
            bot.place_bilateral(st)
        raw.bulk_cancel.assert_called_once_with([{'coin': '#101', 'oid': 123}])
        self.assertIsNone(st.ask_oid)
        self.assertTrue(st.reconcile_required)
        self.assertEqual(st.last_cancel['price'], .69879)
        self.assertEqual(st.last_cancel['target_price'], .69878)
        bot.place_bilateral(st)
        self.assertEqual(raw.bulk_cancel.call_count, 1)
        raw.order.assert_not_called()  # requires fresh account confirmation

    def test_old_plan_cannot_cancel_when_latest_book_already_matches_quote(self):
        bot, st, raw, now = self.fixture()
        st.update_bbo(.69868, .69878)
        st.pending_reprices['ask'] = (123, .69878, now-1)
        real_plan = bot.bilateral_requests
        def flickering_plan(state):
            request = real_plan(state)
            state.update_bbo(.69869, .69879)
            return request
        with patch.object(bot, 'bilateral_requests', side_effect=flickering_plan):
            bot.place_bilateral(st)
        raw.bulk_cancel.assert_not_called()
        self.assertEqual(st.ask_oid, 123)
        self.assertEqual(bot.stats.requotes_skipped, 1)
        self.assertEqual(st.cancel_retry_until, 0)
        self.assertEqual(bot.safety.phase, 'running')

    def test_book_is_rechecked_after_real_signing_wrapper_acquires_its_lock(self):
        bot, st, raw, now = self.fixture()
        st.update_bbo(.69868, .69878)
        with tempfile.TemporaryDirectory() as root:
            signer = SigningCoordinator(root, 'offline-test-wallet')
            @contextmanager
            def delayed_lock():
                st.update_bbo(.69869, .69879)
                yield
            # Same wrapper order as production: safety -> signing -> final gate -> SDK.
            bot.exchange = bot.safety.wrap(signer.wrap(bot._queue_checked_exchange(raw)),
                                           on_error=bot._pause_execution)
            with patch.object(signer, 'transaction', side_effect=delayed_lock):
                bot.cancel_bilateral_side(st, False, 'requote', target_px=.69878)
        raw.bulk_cancel.assert_not_called()
        self.assertEqual(st.ask_oid, 123)
        self.assertEqual(bot.stats.requotes_skipped, 1)
        self.assertEqual(bot.safety.phase, 'running')
        bot._pause_execution.assert_not_called()
        self.assertFalse(bot.safety.pending_cloids)

    def test_manual_exit_and_inventory_limit_do_not_wait_for_price_confirmation(self):
        bot, st, raw, now = self.fixture()
        st.ask_order_ts = now-.01
        st.update_bbo(.69868, .69878)
        bot.exit_reason = 'Manual stop: maker exit'
        bot.place_bilateral(st)
        raw.bulk_cancel.assert_called_once()
        self.assertEqual(st.last_cancel['reason'], 'drain')
        bot, st, raw, now = self.fixture(buy=True)
        st.bid_order_ts, st.position = now-.01, 160
        bot.place_bilateral(st)
        raw.bulk_cancel.assert_called_once()
        self.assertEqual(st.last_cancel['reason'], 'limit')

    def test_stale_book_keeps_the_resting_exit_quote(self):
        bot, st, raw, now = self.fixture()
        st.last_book_ts = now-5
        st.book_received_at = now-5
        bot.place_bilateral(st)
        raw.bulk_cancel.assert_not_called()
        self.assertEqual(st.ask_oid, 123)
        self.assertIn('stale feed', st.last_action)

    def test_transient_empty_plan_keeps_the_resting_ask(self):
        bot, st, raw, now = self.fixture()
        with patch.object(bot, 'bilateral_requests', return_value={}):
            bot.place_bilateral(st)
        raw.bulk_cancel.assert_not_called()
        self.assertEqual(st.ask_oid, 123)
        self.assertEqual(bot.stats.cancels_sent, 0)

    def test_low_fill_rotation_keeps_a_fresh_ask_during_a_flicker(self):
        bot, st, raw, now = self.fixture()
        bot.exit_reason = 'low executed $/min'
        st.ask_order_ts = now-0.5
        st.update_bbo(.69868, .69878)
        bot.place_bilateral(st)
        raw.bulk_cancel.assert_not_called()
        self.assertEqual(st.ask_oid, 123)

    def test_low_fill_rotation_confirms_a_better_ask_before_canceling(self):
        bot, st, raw, now = self.fixture()
        bot.exit_reason = 'low executed $/min'
        with patch('zeus.bots.worldcup.time.time', return_value=now):
            st.update_bbo(.69868, .69878)
            bot.place_bilateral(st)
        raw.bulk_cancel.assert_not_called()
        with patch('zeus.bots.worldcup.time.time', return_value=now+.1):
            bot.place_bilateral(st)
        raw.bulk_cancel.assert_not_called()
        with patch('zeus.bots.worldcup.time.time', return_value=now+.3):
            bot.place_bilateral(st)
        raw.bulk_cancel.assert_called_once()
        self.assertEqual(st.last_cancel['reason'], 'drain')

    def update(self, st, *, oid=123, size='100', timestamp=None):
        return {'data': [{'status': 'open', 'order': {'coin': '#101', 'side': 'B',
            'oid': oid, 'limitPx': '.69879', 'sz': size,
            'timestamp': timestamp or int(time.time()*1000)}}]}

    def test_websocket_open_carries_funded_quantity_and_preserves_order_age(self):
        bot, st, raw, now = self.fixture(buy=True)
        st.bid_oid, st.bid_quantity, st.bid_order_ts = None, 0, 0
        original = int((now-48)*1000)
        event = self.update(st, timestamp=original)
        bot.on_order_updates(event)
        self.assertEqual(st.bid_quantity, 100)
        self.assertAlmostEqual(st.bid_order_ts, original/1000)
        self.assertTrue(st.reconcile_required)
        st.reconcile_required = False  # fresh balances/orders have been confirmed
        bot.on_order_updates(self.update(st, timestamp=int(now*1000)))
        self.assertAlmostEqual(st.bid_order_ts, original/1000)
        bot.place_bilateral(st)
        raw.bulk_cancel.assert_not_called()

    def test_incomplete_or_delayed_websocket_event_never_replaces_current_order(self):
        for changes in ({'size': None}, {'oid': 99}, {'size': '110'}):
            with self.subTest(changes=changes):
                bot, st, raw, now = self.fixture(buy=True)
                age = st.bid_order_ts
                bot.on_order_updates(self.update(st, **changes))
                self.assertEqual(st.bid_oid, 123)
                self.assertEqual(st.bid_quantity, 100)
                self.assertEqual(st.bid_order_ts, age)
                self.assertTrue(st.reconcile_required)
                bot.place_bilateral(st)
                raw.bulk_cancel.assert_not_called()

    def test_account_refresh_retains_oid_queue_age_and_held_cash_credit(self):
        bot, st, raw, now = self.fixture(buy=True)
        age = st.bid_order_ts
        bot.cfg.account_address = 'offline-test-account'
        bot.info = SimpleNamespace(
            spot_user_state=Mock(return_value={'balances': [{'coin': 'USDC',
                'total': '101.739', 'hold': '69.879'}]}),
            open_orders=Mock(return_value=[{'coin': '#101', 'oid': 123, 'side': 'B',
                                           'limitPx': '.69879', 'sz': '100'}]))
        for _ in range(5):
            self.assertTrue(bot.sync_account())
            bot.place_bilateral(st)
        self.assertEqual(st.bid_oid, 123)
        self.assertEqual(st.bid_order_ts, age)
        raw.bulk_cancel.assert_not_called()
        raw.order.assert_not_called()
