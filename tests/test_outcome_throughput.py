"""Outcome throughput regressions. Every exchange/transport here is fake."""
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace

from zeus.bots.outcome_worker import build_config
from zeus.bots.worldcup import HyperliquidSpreadBot, MarketState, MarketSpec, SessionStats, outcome_asset_id
from zeus.network import HyperliquidError, StartupReadCancelled, retry_after_seconds
from zeus.read_cache import ReadBudget, public_metadata
from zeus.safety import SafetyLatch, SubmissionSkipped
from zeus.ws_execution import WebSocketExecution, UncertainActionError
from zeus.outcome_flow import OutcomeFlowScanner
from zeus.process_control import OutcomeFunds
from zeus.outcome_supervisor import OutcomeSupervisor


def engine(qty=47, bid=.70004):
    bot = HyperliquidSpreadBot.__new__(HyperliquidSpreadBot)
    bot.cfg = build_config({'market': '#101', 'order_size_usd': 70, 'max_position_usd': 70,
                            'two_sided': True, 'selection_mode': 'auto',
                            'exit_policy': 'maker_then_ioc'}, None)
    bot.running = True
    bot.stats = SessionStats()
    bot.lock, bot.wake = threading.RLock(), threading.Event()
    bot.funds, bot.available_usdc = None, 100
    bot.safety = SafetyLatch(strict_maker_spot=True, is_running=lambda: bot.running)
    bot.safety.phase = 'running'
    bot.exit_reason, bot.exit_started = '', 0
    state = MarketState(MarketSpec('#101', 0), position=qty, available_position=qty,
                        avg_entry_px=bid, entry_basis_source='exchange')
    state.update_bbo(bid, bid+.00005)
    bot.states = {'#101': state}
    bot.sync_account = Mock(return_value=True)
    bot._submit_order = Mock(return_value={'status': 'ok', 'response': {'data': {
        'statuses': [{'filled': {'oid': 1, 'totalSz': str(qty), 'avgPx': str(bid)}}]}}})
    bot._parse_order_response = Mock()
    return bot, state


class PartialInventoryTests(unittest.TestCase):
    def test_reconnect_snapshot_counts_missing_fills_once_and_tid_can_repeat_in_another_block(self):
        bot, state = engine()
        stamp = int(time.time()*1000)+1
        fill = {'coin': '#101', 'time': stamp, 'tid': 77, 'px': '.7', 'sz': '10',
                'side': 'B', 'startPosition': '47', 'closedPnl': '0', 'fee': '0'}
        bot.on_user_fills({'data': {'isSnapshot': True, 'fills': [fill]}})
        bot.on_user_fills({'data': {'isSnapshot': False, 'fills': [fill]}})
        self.assertEqual(bot.stats.fills, 1)
        bot.on_user_fills({'data': {'fills': [{**fill, 'time': stamp+1, 'startPosition': '57'}]}})
        self.assertEqual(bot.stats.fills, 2)
        self.assertEqual(bot.stats.volume_usd, 14)
        self.assertEqual(bot.safety.metadata['session_accounting']['volume_usd'], 14)

    def test_actual_taker_fee_cannot_be_underestimated_by_the_configured_floor(self):
        bot, state = engine()
        bot.info = SimpleNamespace(post=Mock(return_value={'userSpotCrossRate': '.002', 'activeReferralDiscount': '0'}))
        bot.outcome_meta = {'feeScale': 1, 'outcomes': [{'outcome': 10, 'deployerFeeScale': 1}]}
        self.assertEqual(bot._bounded_exit_fee_bps(state), 40)

    def test_screenshot_inventories_get_an_ask_without_a_full_70_dollars(self):
        for quantity, bid in [(47, .70004), (82, .5967)]:
            bot, state = engine(quantity, bid)
            plan = bot.bilateral_requests(state)
            self.assertIn(False, plan)
            self.assertEqual(plan[False]['sz'], quantity)
            self.assertEqual(plan[False]['order_type']['limit']['tif'], 'Alo')

    def test_normal_inventory_gets_a_bounded_exit_without_stopping_the_session(self):
        bot, state = engine()
        state.inventory_since = time.time()-100
        self.assertFalse(bot.rotation_requested())
        bot.maybe_bounded_exit(state)
        bot._submit_order.assert_called_once()
        self.assertTrue(state.inventory_exit_active)
        self.assertFalse(bot.rotation_requested())
        order = bot._submit_order.call_args.kwargs
        self.assertGreaterEqual(order['limit_px'], state.bid*(1-.002)-1e-12)
        self.assertLessEqual(order['sz'], state.available_position)

    def test_partial_fill_and_requote_do_not_reset_inventory_age(self):
        bot, state = engine()
        bot._track_inventory(state)
        original = state.inventory_since
        state.position -= 2
        bot._track_inventory(state)
        self.assertEqual(state.inventory_since, original)
        self.assertEqual(bot.safety.metadata['inventory_since']['#101'], original)
        state.position = 0
        state.inventory_exit_active = True
        bot._track_inventory(state)
        self.assertFalse(state.inventory_exit_active)
        self.assertEqual(state.inventory_since, 0)

    def test_failed_final_send_is_not_reported_as_an_uncertain_order(self):
        bot, state = engine()
        state.inventory_since = time.time()-100
        bot._submit_order.side_effect = SubmissionSkipped('book changed')
        bot.maybe_bounded_exit(state)
        self.assertEqual(state.last_inventory_exit['submission'], 'not_sent')
        self.assertFalse(state.reconcile_required)

    def test_maker_is_not_reposted_between_cancellation_and_ioc(self):
        bot, state = engine()
        state.inventory_since = time.time()-100
        state.ask_oid, state.ask_quantity = 123, 47
        state.available_position = 0
        bot.cancel_all = Mock()
        bot.maybe_bounded_exit(state)
        bot.cancel_all.assert_called_once()
        self.assertTrue(state.ioc_cancel_pending)
        self.assertEqual(bot.bilateral_requests(state), {})
        bot._submit_order.assert_not_called()

    def test_blocked_ioc_keeps_the_existing_maker_queue(self):
        bot, state = engine()
        state.inventory_since, state.avg_entry_px = time.time()-100, 5
        state.ask_oid, state.ask_quantity = 123, 47
        state.available_position = 0
        bot.cancel_all = Mock()
        bot.maybe_bounded_exit(state)
        bot.cancel_all.assert_not_called()
        self.assertFalse(state.ioc_cancel_pending)
        self.assertIn(False, bot.bilateral_requests(state))

    def test_ioc_final_gate_rejects_slippage_and_changed_inventory(self):
        bot, state = engine()
        action = {'action': {'type': 'order', 'orders': [
            {'a': outcome_asset_id(10, 1), 'b': False, 'p': str(state.bid*.99),
             's': '47', 't': {'limit': {'tif': 'Ioc'}}}]}}
        with bot.safety.bounded_exit_scope(), self.assertRaises(SubmissionSkipped):
            bot._final_outcome_gate(action)
        action['action']['orders'][0]['p'] = str(state.bid)
        state.available_position = 40
        with bot.safety.bounded_exit_scope(), self.assertRaises(SubmissionSkipped):
            bot._final_outcome_gate(action)

    def test_healthy_ws_book_never_triggers_the_rest_fallback(self):
        bot, state = engine()
        bot.last_rest_sweep_ts, bot.rest_backoff_until = 0, 0
        state.book_source = 'l2'
        bot.fetch_rest_bbo = Mock(side_effect=AssertionError('REST should not be used'))
        bot.refresh_rest_bbos()
        bot.fetch_rest_bbo.assert_not_called()

    def test_live_reader_prefers_ws_and_does_not_fall_back_after_timeout(self):
        bot, _ = engine()
        transport = Mock()
        transport.state.return_value = {'ready': True}
        transport.post_info.return_value = []
        bot._execution_transport = transport
        with patch('zeus.bots.worldcup.request_info') as http:
            self.assertEqual(bot._read_info('https://example.test', {'type': 'openOrders'}), [])
            http.assert_not_called()
            transport.post_info.side_effect = TimeoutError('lost reply')
            with self.assertRaises(TimeoutError):
                bot._read_info('https://example.test', {'type': 'openOrders'})
            http.assert_not_called()


class SharedReadTests(unittest.TestCase):
    def test_importing_a_manager_never_launches_recovery(self):
        with tempfile.TemporaryDirectory() as folder:
            manager = SimpleNamespace(state_dir=Path(folder), _session_files=lambda: [])
            sup = OutcomeSupervisor(manager)
            self.assertIsNone(sup._recovery_thread)
            sup.close()

    def test_locally_skipped_order_releases_funds_but_uncertain_order_keeps_them(self):
        for error, expected in [(SubmissionSkipped('not sent'), 0), (TimeoutError('unknown'), 7)]:
            with tempfile.TemporaryDirectory() as folder:
                funds = OutcomeFunds(folder, 'account', 'session')
                exchange = funds.wrap(SimpleNamespace(order=Mock(side_effect=error)))
                with self.assertRaises(type(error)):
                    exchange.order(cloid='one', is_buy=True, sz=10, limit_px=.7)
                self.assertEqual(funds.reserved_usdc(), expected)

    def test_independent_budget_instances_share_a_limit_and_risk_reserve(self):
        with tempfile.TemporaryDirectory() as folder:
            one = ReadBudget(folder, limit=40, ordinary=20)
            two = ReadBudget(folder, limit=40, ordinary=20)
            one.acquire('meta')
            with self.assertRaises(HyperliquidError):
                two.acquire('meta')
            two.acquire('openOrders', critical=True)
            with self.assertRaises(HyperliquidError):
                one.acquire('orderStatus', critical=True)

    def test_cooldown_and_stop_apply_before_any_request(self):
        with tempfile.TemporaryDirectory() as folder:
            budget = ReadBudget(folder)
            budget.cooldown(120)
            with self.assertRaises(HyperliquidError) as error:
                budget.acquire('l2Book')
            self.assertGreater(error.exception.retry_after, 100)
            with self.assertRaises(StartupReadCancelled):
                budget.acquire('l2Book', cancelled=lambda: True)

    def test_metadata_is_shared_without_caching_balances(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict('os.environ', {'ZEUS_NETWORK_ROOT': folder}):
            fetch = Mock(return_value={'universe': []})
            public_metadata('https://example.test', 'meta', fetch)
            public_metadata('https://example.test', 'meta', fetch)
            fetch.assert_called_once()
            with self.assertRaises(ValueError):
                public_metadata('https://example.test', 'openOrders', fetch)

    def test_invalid_metadata_is_not_cached(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict('os.environ', {'ZEUS_NETWORK_ROOT': folder}):
            with self.assertRaises(ValueError):
                public_metadata('https://example.test', 'spotMeta', lambda: {'universe': []})
            self.assertEqual(list((Path(folder)/'metadata').glob('*.json')), [])


class WebSocketInfoTests(unittest.TestCase):
    def test_action_ack_is_preserved_and_missing_ack_is_never_resent(self):
        for acknowledge in (True, False):
            ws = WebSocketExecution('https://example.test', request_timeout=.02)
            sent = []
            class Socket:
                def send(self, raw):
                    packet = json.loads(raw)
                    sent.append(packet)
                    if acknowledge:
                        ws._message(self, json.dumps({'channel': 'post', 'data': {'id': packet['id'],
                            'response': {'type': 'action', 'payload': {'status': 'ok',
                            'response': {'type': 'order', 'data': {'statuses': [{'resting': {'oid': 123}}]}}}}}}))
            ws.socket = Socket()
            if acknowledge:
                response = ws.post_action({'action': {'type': 'order'}})
                self.assertEqual(response['response']['data']['statuses'][0]['resting']['oid'], 123)
            else:
                with self.assertRaises(UncertainActionError):
                    ws.post_action({'action': {'type': 'order'}})
            self.assertEqual(len(sent), 1)
            self.assertEqual(sent[0]['request']['type'], 'action')

    def test_info_rpc_unwraps_documented_response_without_signing(self):
        ws = WebSocketExecution('https://example.test', final_gate=Mock(side_effect=AssertionError('not an action')))
        class Socket:
            def send(self, raw):
                packet = json.loads(raw)
                assert packet['request']['type'] == 'info'
                ws._message(self, json.dumps({'channel': 'post', 'data': {'id': packet['id'],
                    'response': {'type': 'info', 'payload': {'type': 'openOrders', 'data': []}}}}))
        ws.socket = Socket()
        self.assertEqual(ws.post_info({'type': 'openOrders'}), [])
        ws.final_gate.assert_not_called()

    def test_scanner_uses_streamed_book_without_http(self):
        scan = OutcomeFlowScanner()
        book = {'coin': '#101', 'time': time.time()*1000, 'levels': [[], []]}
        scan.ingest_book(book)
        with patch('zeus.network.request_info', side_effect=AssertionError('REST')):
            self.assertEqual(scan._book('#101'), book)
        scan.ingest_book({**book, 'time': book['time']-10000})
        self.assertEqual(scan.books['#101'][1], book)


if __name__ == '__main__':
    unittest.main()
