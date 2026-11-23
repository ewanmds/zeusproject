"""Manual cancel-and-market only; all exchange operations below are offline fakes."""
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from zeus.bots.worldcup import HyperliquidSpreadBot, MarketState, MarketSpec
from zeus.bots.outcome_worker import run_worker
from zeus.outcome_supervisor import OutcomeSupervisor
from zeus.outcome_stop import attributable_inventory
from zeus.safety import SafetyLatch, SubmissionSkipped, atomic_json, read_json
from test_outcome_maker_exit import make_bot


class InventoryAttributionTests(unittest.TestCase):
    def fill(self, tid, side='B', size='40', oid=7, stamp=1000):
        return dict(coin='#101', oid=oid, tid=tid, side=side, sz=size, time=stamp)

    def test_only_owned_order_fills_count_and_repeated_fill_is_deduplicated(self):
        buy = self.fill(1)
        info = Mock(user_fills_by_time=Mock(return_value=[buy, buy, self.fill(2, 'A', '10'), self.fill(3, size='500', oid=99)]))
        self.assertEqual(attributable_inventory(info, 'wallet', '#101', {7}, 1), 30)

    def test_pages_overlap_at_boundary_without_losing_partial_fills(self):
        first = [self.fill(tid, size='1', stamp=1000+tid) for tid in range(2000)]
        second = [first[-1], self.fill(2001, 'A', '50', stamp=3000)]
        info = Mock(user_fills_by_time=Mock(side_effect=[first, second]))
        self.assertEqual(attributable_inventory(info, 'wallet', '#101', {7}, 1), 1950)
        self.assertEqual(info.user_fills_by_time.call_args_list[1].args[1], 2999)

    def test_stalled_missing_or_invalid_history_never_authorizes_liquidation(self):
        for pages in ([None], [[{'coin':'#101', 'oid':7, 'time':1000}]],
                      [[self.fill(1, size='NaN')]], [[self.fill(1, 'A')]],
                      [[self.fill(i, stamp=1000) for i in range(2000)]]*2):
            info = Mock(user_fills_by_time=Mock(side_effect=pages))
            with self.assertRaises(RuntimeError):
                attributable_inventory(info, 'wallet', '#101', {7}, 1)


class PendingPlacementTests(unittest.TestCase):
    def bot(self):
        bot = make_bot()
        bot.info = Mock()
        bot.cfg.account_address = 'wallet'
        st = MarketState(MarketSpec('#101', 0, balance_coin='+101'))
        bot.states = {'#101': st}
        bot.safety = SafetyLatch(strict_maker_spot=True)
        bot.safety.pending_cloids.add('0xabc')
        bot.rotation_requested = lambda: True
        return bot

    def test_unknown_cloid_is_cleared_only_when_flat_and_orderless(self):
        bot = self.bot()
        with patch('zeus.bots.worldcup.confirmed_client_order',
                   side_effect=RuntimeError('Placement still unconfirmed; awaiting client order ID')), \
             patch('zeus.bots.worldcup.account_snapshot', return_value=[('#101', 0, [])]):
            self.assertEqual(bot._cleanup_snapshot(), [('#101', 0, [])])
        self.assertFalse(bot.safety.pending_cloids)

    def test_unknown_cloid_with_inventory_stays_unconfirmed(self):
        bot = self.bot()
        with patch('zeus.bots.worldcup.confirmed_client_order',
                   side_effect=RuntimeError('Placement still unconfirmed; awaiting client order ID')), \
             patch('zeus.bots.worldcup.account_snapshot', return_value=[('#101', 10, [])]):
            with self.assertRaises(RuntimeError):
                bot._cleanup_snapshot()
        self.assertIn('0xabc', bot.safety.pending_cloids)


class ManualCloseTests(unittest.TestCase):
    def latch(self):
        gate = SafetyLatch(strict_maker_spot=True)
        gate.exposure_started = True
        gate.request_manual_taker_stop()
        return gate

    def test_ioc_sell_requires_both_manual_authority_and_cleanup_scope(self):
        gate = self.latch()
        request = dict(is_buy=False, order_type={'limit': {'tif': 'Ioc'}}, reduce_only=False)
        with self.assertRaises(SubmissionSkipped):
            gate.assert_allowed_action('order', kwargs=request)
        with gate.manual_close_scope(), gate.cleanup_scope():
            gate.assert_allowed_action('order', kwargs=request)
            for values in ({**request, 'is_buy': True}, {**request, 'order_type': {'limit': {'tif': 'Gtc'}}}):
                with self.assertRaises(SubmissionSkipped):
                    gate.assert_allowed_action('order', kwargs=values)
        ordinary = SafetyLatch(strict_maker_spot=True)
        ordinary.request_manual_stop()
        with ordinary.manual_close_scope(), ordinary.cleanup_scope(), self.assertRaises(SubmissionSkipped):
            ordinary.assert_allowed_action('order', kwargs=request)

    def test_cancel_owned_then_close_fresh_balance_and_verify_flat(self):
        gate = self.latch()
        gate.owned.add(7)
        state = {'position': 40, 'orders': [{'oid': 7, 'coin': '#101'}]}
        calls = []
        def snapshot():
            return [('#101', state['position'], list(state['orders']))]
        def cancel(rows):
            self.assertEqual(rows, [('#101', 7)])
            calls.append('cancel')
            state['orders'] = []
            state['position'] = 42  # a last fill before cancellation must be included
        def close(symbol, position):
            self.assertEqual((symbol, position), ('#101', 42))
            self.assertFalse(state['orders'])
            calls.append('close')
            state['position'] = 0
        with gate.manual_close_scope():
            self.assertTrue(gate.cleanup(snapshot, cancel, close, sleep=lambda _: None))
        self.assertEqual(calls, ['cancel', 'close'])
        self.assertTrue(gate.cleanup_confirmed)
        with gate.manual_close_scope():
            gate.cleanup(snapshot, cancel, close)
        self.assertEqual(calls, ['cancel', 'close'])  # repeated shutdown never resends

    def test_unrelated_order_is_never_cancelled_or_position_liquidated(self):
        gate = self.latch()
        gate.owned.add(7)
        rows = [{'coin': '#101', 'oid': 7}, {'coin': '#101', 'oid': 99}]
        cancelled, close = [], Mock()
        def cancel(owned):
            cancelled.extend(owned)
            rows[:] = [order for order in rows if order['oid'] != 7]
        with gate.manual_close_scope():
            result = gate.cleanup(lambda: [('#101', 40, rows)], cancel, close, sleep=lambda _: None)
        self.assertFalse(result)
        self.assertEqual(cancelled, [('#101', 7)])
        self.assertEqual(rows[0]['oid'], 99)
        close.assert_not_called()
        self.assertIn('Unrelated orders', gate.cleanup_error)

    def test_preexisting_inventory_and_unresolved_placement_are_not_sold(self):
        for unknown in (False, True):
            gate = self.latch()
            gate.exposure_started = unknown
            if unknown:
                gate.pending_cloids.add('unresolved')
            close = Mock()
            with gate.manual_close_scope():
                self.assertFalse(gate.cleanup(lambda: [('#101', 40, [])], Mock(), close, sleep=lambda _: None))
            close.assert_not_called()
            self.assertFalse(gate.cleanup_confirmed)

    def test_uncertain_ioc_is_sent_once_and_never_reported_closed(self):
        gate = self.latch()
        sdk = SimpleNamespace(order=Mock(side_effect=TimeoutError('response lost')))
        exchange = gate.wrap(sdk)
        def close(symbol, position):
            exchange.order(name=symbol, is_buy=False, sz=position, limit_px=.49,
                           order_type={'limit': {'tif': 'Ioc'}}, reduce_only=False)
        with gate.manual_close_scope():
            self.assertFalse(gate.cleanup(lambda: [('#101', 40, [])], Mock(), close, sleep=lambda _: None))
        self.assertEqual(sdk.order.call_count, 1)
        self.assertTrue(gate.uncertain_close)
        self.assertTrue(gate.pending_cloids)
        self.assertFalse(gate.cleanup_confirmed)

    def test_partial_ioc_retries_only_fresh_residual_not_original_size(self):
        gate = self.latch()
        position, sizes = [40], []
        def close(symbol, size):
            sizes.append(size)
            position[0] = 10 if len(sizes) == 1 else 0
        with gate.manual_close_scope():
            self.assertTrue(gate.cleanup(lambda: [('#101', position[0], [])], Mock(), close, sleep=lambda _: None))
        self.assertEqual(sizes, [40, 10])

    def test_outcome_close_uses_integer_inventory_and_bounded_ioc(self):
        bot = make_bot()
        bot.safety = self.latch()
        bot.fetch_rest_bbo = Mock(return_value=(.50, .51))
        bot.info = Mock(user_fills_by_time=Mock(return_value=[
            {'coin':'#101', 'oid':7, 'tid':1, 'time':1000, 'sz':'41', 'side':'B'}]))
        bot.safety.metadata['started'] = 1
        bot.safety.owned.add(7)
        bot._submit_order = Mock()
        bot._cleanup_close('#101', 41)
        req = bot._submit_order.call_args.kwargs
        self.assertFalse(req['is_buy'])
        self.assertFalse(req['reduce_only'])  # Outcome is spot
        self.assertEqual(req['sz'], 41)
        self.assertEqual(req['order_type'], {'limit': {'tif': 'Ioc'}})
        self.assertGreaterEqual(req['limit_px'], .5*(1-bot.cfg.inventory_exit_slippage_bps/10000)-.000001)
        self.assertLess(req['limit_px'], .5)
        with self.assertRaises(SubmissionSkipped):
            bot._cleanup_close('#111', 41)

        # Extra manually acquired shares are never included in the market sell.
        bot._cleanup_close('#101', 60)
        self.assertEqual(bot._submit_order.call_args.kwargs['sz'], 41)

    def test_marker_authority_is_exact_session_and_market(self):
        with tempfile.TemporaryDirectory() as folder:
            bot = make_bot()
            bot.cfg.stop_file = str(Path(folder)/'stop')
            for sid, market, granted in [('other', '#101', False), ('s1', '#111', False), ('s1', '#101', True)]:
                bot.safety = SafetyLatch(strict_maker_spot=True)
                bot.safety.session_id = 's1'
                atomic_json(bot.cfg.stop_file, dict(mode='cancel_and_market', session_id=sid, market=market))
                bot.request_user_stop()
                self.assertTrue(bot.safety.stopped.is_set())
                self.assertEqual(bot.safety.manual_taker_close_requested, granted)

    def test_corrupt_attribution_metadata_never_kills_the_stop_monitor(self):
        with tempfile.TemporaryDirectory() as folder:
            bot = make_bot()
            bot.cfg.stop_file = str(Path(folder)/'stop')
            bot.safety = SafetyLatch(strict_maker_spot=True)
            bot.safety.session_id = 's1'
            bot.safety.metadata['started'] = 'not-a-time'
            atomic_json(bot.cfg.stop_file, dict(mode='cancel_and_market', session_id='s1',
                                                market='#101', attribution_started=10))
            bot.request_user_stop()
            self.assertTrue(bot.safety.manual_taker_close_requested)
            self.assertEqual(bot.safety.metadata['attribution_started'], 10)

    def test_shutdown_reads_marker_without_waiting_for_monitor(self):
        with tempfile.TemporaryDirectory() as folder:
            bot = make_bot()
            bot.cfg.live = True
            bot.cfg.stop_file = str(Path(folder)/'stop')
            bot.safety = self.latch()
            bot.safety.manual_taker_close_requested = False
            atomic_json(bot.cfg.stop_file, dict(mode='cancel_and_market', session_id=bot.safety.session_id, market='#101'))
            bot._cleanup_snapshot = Mock(return_value=[('#101', 0, [])])
            bot.exchange = SimpleNamespace(bulk_cancel=Mock())
            bot.ws_info = None
            bot.shutdown()
            self.assertTrue(bot.safety.manual_taker_close_requested)
            self.assertTrue(bot.safety.cleanup_confirmed)


class StopSupervisorTests(unittest.TestCase):
    def setup_supervisor(self, folder):
        sup = OutcomeSupervisor.__new__(OutcomeSupervisor)
        sup.manager = SimpleNamespace(lock=threading.RLock())
        sup._closed, sup._recovery_lock = threading.Event(), threading.RLock()
        sup.rotation = SimpleNamespace(cancel=Mock(return_value=[]))
        sup.root = Path(folder)
        sup.procs = {}
        sup._orphan_alive = Mock(return_value=False)
        sup._resume = Mock(return_value={'session_id': 'new'})
        return sup

    def test_live_stop_is_durable_fast_and_does_not_touch_other_bot(self):
        with tempfile.TemporaryDirectory() as folder:
            sup = self.setup_supervisor(folder)
            for sid, market in [('s1', '#101'), ('s2', '#111')]:
                sup.procs[sid] = dict(session_id=sid, market=market, stop_file=str(Path(folder)/sid), proc=Mock(poll=Mock(return_value=None)))
            sup.records = lambda: []
            start = time.monotonic()
            self.assertEqual(sup.stop_now('s1'), ['s1'])
            self.assertLess(time.monotonic()-start, 1)
            self.assertEqual(read_json(Path(folder)/'s1')['mode'], 'cancel_and_market')
            self.assertFalse((Path(folder)/'s2').exists())
            sup.rotation.cancel.assert_called_once_with('s1')
            sup._resume.assert_not_called()

    def test_manual_stop_terminates_a_worker_that_ignores_the_marker(self):
        with tempfile.TemporaryDirectory() as folder:
            sup = self.setup_supervisor(folder)
            sup._closed.set()  # skip the grace wait; exercise the escalation
            journal = Path(folder)/'journal.json'
            stop = Path(folder)/'stop'
            atomic_json(journal, dict(session_id='s1', symbols=['#101'], account='wallet',
                                      cleanup_confirmed=False, stop_file=str(stop),
                                      exposure_started=True, started=10))
            proc = Mock()
            proc.poll.return_value = None
            proc.terminate.side_effect = lambda: proc.poll.configure_mock(return_value=1)
            info = dict(session_id='s1', symbols=['#101'], account='wallet',
                        stop_file=str(stop), safety_file=str(journal), proc=proc)
            sup.records = lambda: [(journal, read_json(journal))]
            sup._finish_manual_stop('s1', info)
            proc.terminate.assert_called_once()
            sup._resume.assert_called_once_with('s1', exit_only=True, manual_taker=True)

    def test_orphan_recovers_only_exit_with_published_attribution(self):
        with tempfile.TemporaryDirectory() as folder:
            sup = self.setup_supervisor(folder)
            journal = Path(folder)/'journal.json'
            row = dict(session_id='s1', symbols=['#101'], account='wallet', cleanup_confirmed=False,
                       stop_file=str(Path(folder)/'stop'), exposure_started=True)
            atomic_json(journal, row)
            sup.records = lambda: [(journal, read_json(journal))]
            done = threading.Event()
            def resume(*args, **kwargs):
                done.set()
                raise RuntimeError('offline recovery failure')
            sup._resume.side_effect = resume
            sup.stop_now('s1')
            self.assertTrue(done.wait(2))
            sup._resume.assert_called_once_with('s1', exit_only=True, manual_taker=True)
            deadline = time.monotonic()+2
            while not read_json(journal).get('cleanup_error') and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertIn('offline recovery failure', read_json(journal)['cleanup_error'])
            self.assertFalse(read_json(journal)['cleanup_confirmed'])

    def test_resume_keeps_manual_authority_and_never_restarts_quoting(self):
        from zeus.server import BotManager, WorldcupStartRequest
        from zeus.workspace.accounts import AccountContext
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(OutcomeSupervisor, '_recovery_loop'), \
                patch('zeus.outcomes.resolve_market', return_value={'key': '#101', 'name': 'Offline'}), \
                patch('zeus.outcome_supervisor.subprocess.Popen') as spawn:
            manager = BotManager(Path(folder), AccountContext('wallet', 'wallet', 'agent', 'offline-key'))
            try:
                spawn.return_value = Mock(pid=1001, poll=Mock(return_value=None))
                sid = manager.outcome.start(WorldcupStartRequest(market='#101', auto_rotate=False))['session_id']
                first = manager.outcome.procs[sid]
                first['proc'].poll.return_value = 1
                old = read_json(first['safety_file'])
                old.update(exposure_started=True, owned_oids=[7], cloids=['client-7'], pending_cloids=[])
                atomic_json(first['safety_file'], old)
                manager._scan_cache = None
                spawn.return_value = Mock(pid=1002, poll=Mock(return_value=None))
                result = manager.outcome._resume(sid, exit_only=True, manual_taker=True)
                child = manager.outcome.procs[result['session_id']]
                seed = read_json(child['safety_file'])
                self.assertTrue(seed['manual_taker_close_requested'])
                self.assertEqual(seed['owned_oids'], [7])
                self.assertTrue(seed['config']['resume_exit'])
                self.assertFalse(seed['config']['resume_trading'])
                self.assertFalse(seed['config']['auto_rotate'])
                # A failed recovery spawn must not label inherited exposure closed.
                child['proc'].poll.return_value = 1
                manager._scan_cache = None
                spawn.side_effect = RuntimeError('offline spawn failure')
                with self.assertRaises(RuntimeError):
                    manager.outcome._resume(result['session_id'], exit_only=True, manual_taker=True)
                failed = [r for _, r in manager._session_files(fresh=True) if r.get('error') == 'offline spawn failure']
                self.assertEqual(len(failed), 1)
                self.assertFalse(failed[0]['cleanup_confirmed'])
            finally:
                manager.outcome._closed.set()
                manager.outcome.rotation.close()

    def test_seeded_worker_goes_directly_to_cleanup_without_bot_run(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {
                'PRIVATE_KEY_OUTCOME': 'offline-unused', 'WALLET_ADDRESS_OUTCOME': 'offline-wallet'}):
            root = Path(folder)
            journal = root/'journal.json'
            atomic_json(journal, dict(session_id='exit', exposure_started=True,
                                     owned_oids=[7], manual_taker_close_requested=True))
            config = root/'config.json'
            atomic_json(config, dict(config={'market':'#101', 'order_size_usd':70, 'max_position_usd':120},
                                     safety_file=str(journal), funds_root=str(root/'funds'), locks_root=str(root/'locks')))
            bots = []
            class FakeBot:
                def __init__(self, cfg):
                    self.safety = SafetyLatch(path=cfg.safety_file, strict_maker_spot=True)
                    self.run = Mock()
                    self.wake = threading.Event()
                    bots.append(self)
                def request_stop(self):
                    self.safety.stop()
                def export_dashboard_stats(self):
                    return {}
                def shutdown(self):
                    assert self.safety.manual_taker_close_requested
                    assert self.safety.owned == {7}
                    self.safety.cleanup_confirmed = True
                    self.safety.phase = 'stopped'
                    self.safety.publish()
            self.assertEqual(run_worker(config, bot_factory=FakeBot), 0)
            bots[0].run.assert_not_called()
            self.assertTrue(read_json(journal)['cleanup_confirmed'])

    def test_auto_recovery_completes_cancel_and_market_after_worker_crash(self):
        with tempfile.TemporaryDirectory() as folder:
            sup = self.setup_supervisor(folder)
            sup._recovery = {}
            journal = Path(folder)/'journal.json'
            stop = Path(folder)/'stop'
            atomic_json(journal, dict(session_id='s1', symbols=['#101'], account='wallet',
                                      cleanup_confirmed=False, stop_file=str(stop),
                                      exposure_started=True, started=10))
            atomic_json(stop, dict(mode='cancel_and_market', session_id='s1', market='#101'))
            sup.records = lambda: [(journal, read_json(journal))]
            sup._auto_recover()
            sup._resume.assert_called_once_with('s1', exit_only=True, manual_taker=True)

    def test_auto_recovery_keeps_manual_taker_after_repeated_crash(self):
        with tempfile.TemporaryDirectory() as folder:
            sup = self.setup_supervisor(folder)
            sup._recovery = {}
            journal = Path(folder)/'journal.json'
            atomic_json(journal, dict(session_id='s1', symbols=['#101'], account='wallet',
                                      cleanup_confirmed=False, stop_file=str(Path(folder)/'stop'),
                                      exposure_started=True, started=10,
                                      manual_taker_close_requested=True))
            sup.records = lambda: [(journal, read_json(journal))]
            sup._auto_recover()
            sup._resume.assert_called_once_with('s1', exit_only=True, manual_taker=True)

    def test_auto_recovery_never_restarts_after_plain_or_foreign_stop(self):
        with tempfile.TemporaryDirectory() as folder:
            sup = self.setup_supervisor(folder)
            sup._recovery = {}
            journal = Path(folder)/'journal.json'
            stop = Path(folder)/'stop'
            atomic_json(journal, dict(session_id='s1', symbols=['#101'], account='wallet',
                                      cleanup_confirmed=False, stop_file=str(stop),
                                      exposure_started=True, started=10))
            sup.records = lambda: [(journal, read_json(journal))]
            for marker in ({}, dict(mode='maker', session_id='s1', market='#101'),
                           dict(mode='cancel_and_market', session_id='other', market='#101'),
                           dict(mode='cancel_and_market', session_id='s1', market='#111')):
                sup._resume.reset_mock()
                atomic_json(stop, marker)
                sup._auto_recover()
                sup._resume.assert_not_called()

    def test_stop_follows_only_same_account_market_recovery_lineage(self):
        with tempfile.TemporaryDirectory() as folder:
            sup = self.setup_supervisor(folder)
            parent = dict(session_id='parent', started=10, account='wallet', symbols=['#101'],
                          stop_file=str(Path(folder)/'parent'), cleanup_confirmed=False, resumed_session_id='child')
            child = dict(parent, session_id='child', started=20, resumed_session_id='foreign', stop_file=str(Path(folder)/'child'))
            foreign = dict(child, session_id='foreign', account='other-wallet', stop_file=str(Path(folder)/'foreign'))
            sup.records = lambda: [('unused-'+row['session_id'], row) for row in [parent, child, foreign]]
            with patch.object(sup, '_finish_manual_stop'):
                self.assertEqual(set(sup.stop_now('parent')), {'parent', 'child'})
            self.assertEqual(read_json(Path(folder)/'child')['attribution_started'], 10)
            self.assertFalse((Path(folder)/'foreign').exists())


if __name__ == '__main__':
    unittest.main()
