"""Deterministic read concurrency, stale-response and execution-gate regressions."""
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import threading
import time
import unittest
from unittest.mock import Mock, patch

from zeus.bots.outcome_worker import build_config
from zeus.bots.worldcup import HyperliquidSpreadBot, MarketSpec, MarketState, SessionStats
from zeus.outcome_reads import OutcomeAccountReads
from zeus.outcome_selection import no_fill_grace, candidate_rank
from zeus.safety import SafetyLatch


class ParallelReadsTests(unittest.TestCase):
    def test_reads_overlap_and_return_one_pair(self):
        both = threading.Barrier(2)
        def read(base, payload):
            both.wait(timeout=2)
            return payload['type']
        reader = OutcomeAccountReads('offline', 'dummy', read=read)
        try:
            self.assertEqual(reader.snapshot(), ('spotClearinghouseState', 'openOrders'))
        finally:
            reader.close()
        with self.assertRaises(RuntimeError):
            reader.snapshot()

    def test_failed_pair_drains_other_read_and_does_not_return_partial_data(self):
        both = threading.Barrier(2)
        completed = threading.Event()
        def read(base, payload):
            both.wait(timeout=2)
            if payload['type'] == 'spotClearinghouseState':
                raise TimeoutError('offline timeout')
            completed.set()
            return []
        reader = OutcomeAccountReads('offline', 'dummy', read=read)
        try:
            with self.assertRaises(TimeoutError):
                reader.snapshot()
            self.assertTrue(completed.is_set())
        finally:
            reader.close()


class FastWorkerTests(unittest.TestCase):
    def bot(self):
        bot = HyperliquidSpreadBot.__new__(HyperliquidSpreadBot)
        bot.cfg = build_config(dict(market='#101', order_size_usd=25, max_position_usd=200,
            exec_mode='maker', two_sided=True, full_size_quotes=True, selection_mode='auto'), None)
        bot.cfg.account_address = 'offline'
        bot.running = True
        bot.lock = threading.RLock()
        bot.wake = threading.Event()
        bot.safety = SafetyLatch(lambda: setattr(bot, 'running', False))
        bot.safety.phase = 'running'
        bot.safety.account_known = True
        bot.stats = SessionStats()
        bot.account_revision = 0
        bot.available_usdc = 100
        bot.recovery_next, bot.recovery_delay = 0, 2
        bot.funds = None
        bot.exchange = Mock()
        bot.info = Mock()
        bot.info.spot_user_state.return_value = {'balances': [{'coin': 'USDC', 'total': '100', 'hold': '0'}]}
        bot.info.open_orders.return_value = []
        st = MarketState(MarketSpec('#101', 0, balance_coin='+101'))
        st.update_bbo(.4, .401)
        bot.states = {'#101': st}
        return bot, st

    def test_parallel_snapshot_is_validated_then_published(self):
        bot, st = self.bot()
        bot.account_reader = Mock()
        bot.account_reader.snapshot.return_value = ({'balances': [{'coin': 'USDC', 'total': '80', 'hold': '0'}]}, [])
        self.assertTrue(bot.sync_account())
        self.assertEqual(bot.available_usdc, 80)
        self.assertGreater(bot.account_synced_at, 0)
        bot.info.open_orders.assert_not_called()

    def test_ws_fill_overtaking_rest_pair_discards_entire_pair(self):
        bot, st = self.bot()
        def snapshot():
            bot.account_revision += 1
            st.reconcile_required = True
            return {'balances': [{'coin': 'USDC', 'total': '999', 'hold': '0'}]}, []
        bot.account_reader = SimpleNamespace(snapshot=snapshot)
        self.assertFalse(bot.sync_account())
        self.assertEqual(bot.available_usdc, 100)
        self.assertTrue(st.reconcile_required)

    def test_invalid_parallel_orders_never_fall_back_to_another_response(self):
        bot, st = self.bot()
        bot.account_reader = Mock()
        bot.account_reader.snapshot.return_value = ({'balances': []}, None)
        self.assertFalse(bot.sync_account())
        bot.info.open_orders.assert_not_called()
        self.assertEqual(bot.safety.phase, 'recovering')

    def test_book_timestamps_cannot_move_backwards(self):
        bot, st = self.bot()
        stamp = int((time.time()+.1)*1000)
        self.assertTrue(st.update_bbo(.45, .46, stamp))
        self.assertFalse(st.update_bbo(.39, .40, stamp-500))
        self.assertEqual((st.bid, st.ask), (.45, .46))

    def test_new_crossed_book_invalidates_execution(self):
        bot, st = self.bot()
        self.assertFalse(st.update_bbo(.5, .4, int((time.time()+.1)*1000)))
        self.assertFalse(st.has_bbo)
        self.assertEqual(bot.risk_block_reason(st), 'waiting bbo')

    def test_rest_response_cannot_overwrite_a_ws_update_received_in_flight(self):
        bot, st = self.bot()
        bot.rest_backoff_until = bot.last_rest_sweep_ts = 0
        st.last_book_ts = time.time()-3
        def rest(symbol):
            with bot.lock:
                st.update_bbo(.45, .46)
                st.book_source = 'ws'
            return .4, .401
        bot.fetch_rest_bbo = Mock(side_effect=rest)
        try:
            bot.refresh_rest_bbos(force=True)
            self.assertEqual((st.bid, st.ask, st.book_source), (.45, .46, 'ws'))
        finally:
            bot.rest_pool.shutdown(wait=True)

    def test_confirmed_requote_removes_two_second_idle_but_requires_reconciliation(self):
        bot, st = self.bot()
        bot.can_send_action = Mock(return_value=True)
        st.bid_oid, st.bid_quantity, st.quoted_bid = 1, 63, .4
        bot.exchange.bulk_cancel.return_value = {'status': 'ok', 'response': {'data': {'statuses': ['success']}}}
        bot.cancel_bilateral_side(st, True, 'requote')
        self.assertIsNone(st.bid_oid)
        self.assertLess(st.opening_rearm_until-time.time(), .2)
        self.assertTrue(st.reconcile_required)
        bot.place_bilateral(st)
        bot.exchange.order.assert_not_called()

    def test_limit_pull_keeps_rearm_delay_and_manual_mode_is_unchanged(self):
        for auto, reason in ((True, 'limit'), (False, 'requote')):
            bot, st = self.bot()
            if not auto:
                bot.cfg.auto_max_spread_bps = None
            bot.can_send_action = Mock(return_value=True)
            st.bid_oid = 1
            bot.exchange.bulk_cancel.return_value = {'status': 'ok', 'response': {'data': {'statuses': ['success']}}}
            bot.cancel_bilateral_side(st, True, reason)
            self.assertGreater(st.opening_rearm_until-time.time(), 1.5)
            self.assertTrue(st.reconcile_required)

    def test_loop_reconciles_account_event_without_waiting_periodic_interval(self):
        bot, st = self.bot()
        bot.cfg.account_sync_sec = 999
        bot.sync_account = Mock(return_value=True)
        bot.restart_ws_if_needed = bot.refresh_rest_bbos = bot.finish_rotation_if_flat = Mock()
        calls = []
        def quote(state):
            calls.append(1)
            state.reconcile_required = True
            if len(calls) == 2:
                bot.running = False
        bot.place_or_requote = quote
        bot.wake = Mock()
        bot._trading_loop(0)
        self.assertEqual(bot.sync_account.call_count, 2)

    def test_execution_diagnostics_distinguish_working_bid_and_small_cash(self):
        bot, st = self.bot()
        st.bid_oid, st.bid_quantity, st.quoted_bid = 1, 63, .4
        self.assertEqual(bot.execution_status(st)[0], 'Bid working')
        bot.available_usdc = 5
        self.assertEqual(bot.execution_status(st)[0], 'Bid working')
        st.bid_oid = None
        self.assertEqual(bot.execution_status(st)[0], 'Insufficient USDC')


class SelectionTests(unittest.TestCase):
    def test_no_fill_grace_is_queue_aware_and_capped_by_user_window(self):
        cfg = {'auto_rotation_after_sec': 180}
        self.assertEqual(no_fill_grace(cfg, {}), 60)
        self.assertEqual(no_fill_grace(cfg, {'queue_proxy_sec': 80}), 100)
        self.assertEqual(no_fill_grace(cfg, {'queue_proxy_sec': 1000}), 180)
        self.assertEqual(no_fill_grace({'auto_rotation_after_sec': 30}, {}), 30)

    def test_zero_fill_history_penalizes_a_market_without_permanent_ban(self):
        candidate = {'market': '#101', 'queue_proxy_sec': 10}
        history = {'#101': {'zero_fill_attempts': 2, 'updated_at': 1000}}
        penalized = candidate_rank(candidate, history, 1000)[0]
        self.assertEqual(penalized, 30)
        self.assertLess(candidate_rank(candidate, history, 10000)[0], penalized)


if __name__ == '__main__':
    unittest.main()
