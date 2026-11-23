"""Post-only entries/risk exits, legacy maker Stop and explicit maker resume.

The separate manual cancel-and-market capability is covered by instant-stop tests.
"""
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from zeus.bots.outcome_worker import build_config
from zeus.bots.worldcup import HyperliquidSpreadBot, MarketState, MarketSpec, SessionStats
from zeus.outcome_supervisor import OutcomeSupervisor
from zeus.safety import SafetyLatch, SubmissionSkipped
from zeus.workspace.runtime import Runtimes, visible_session


def make_config(**overrides):
    public = {'market': '#101', 'order_size_usd': 15, 'max_position_usd': 200,
              'exec_mode': 'maker', 'selection_mode': 'auto', 'two_sided': True,
              'maker_spread_pct': None}
    public.update(overrides)
    return build_config(public, None)


def make_bot(**overrides):
    bot = HyperliquidSpreadBot.__new__(HyperliquidSpreadBot)
    bot.cfg = make_config(**overrides)
    bot.stats = SessionStats(start_ts=time.time() - 10)
    bot.lock, bot.wake = threading.RLock(), threading.Event()
    bot.funds = None
    bot.available_usdc = 100
    bot.states = {}
    return bot


class OutcomeMakerConfigTests(unittest.TestCase):
    def test_outcome_defaults_to_maker_and_rejects_taker(self):
        self.assertEqual(make_config().exec_mode, 'maker')
        with self.assertRaises(ValueError):
            make_config(exec_mode='taker')

    def test_worker_defaults_maker_when_field_is_missing(self):
        public = {'market': '#101', 'order_size_usd': 15, 'max_position_usd': 200}
        self.assertEqual(build_config(public, None).exec_mode, 'maker')

    def test_worker_reads_resume_trading_flag(self):
        public = {'market': '#101', 'order_size_usd': 15, 'max_position_usd': 200,
                  'exec_mode': 'maker', 'resume_trading': True}
        self.assertTrue(build_config(public, None).resume_trading)
        self.assertFalse(build_config(public, None).resume_exit)


class OutcomeSafetyGateTests(unittest.TestCase):
    def test_strict_maker_spot_blocks_market_and_crossing_orders(self):
        latch = SafetyLatch(strict_maker_spot=True)
        with self.assertRaises(SubmissionSkipped):
            latch.assert_allowed_action('market_open')
        with self.assertRaises(SubmissionSkipped):
            latch.assert_allowed_action('order', (), {'is_buy': True,
                'order_type': {'limit': {'tif': 'Ioc'}}})
        with self.assertRaises(SubmissionSkipped):
            latch.assert_allowed_action('order', (), {'is_buy': True,
                'order_type': {'limit': {'tif': 'Gtc'}}})

    def test_strict_maker_spot_allows_alp_sells_and_gates_buys(self):
        latch = SafetyLatch(strict_maker_spot=True)
        latch.assert_allowed_action('order', (), {'is_buy': True,
            'order_type': {'limit': {'tif': 'Alo'}}})
        latch.assert_allowed_action('order', (), {'is_buy': False,
            'order_type': {'limit': {'tif': 'Alo'}}})
        latch.opening_allowed = lambda: False
        with self.assertRaises(SubmissionSkipped):
            latch.assert_allowed_action('order', (), {'is_buy': True,
                'order_type': {'limit': {'tif': 'Alo'}}})
        # An authorized passive exit stays available.
        latch.assert_allowed_action('order', (), {'is_buy': False,
            'order_type': {'limit': {'tif': 'Alo'}}})

    def test_hard_stop_blocks_even_passive_sells(self):
        latch = SafetyLatch(strict_maker_spot=True)
        latch.stopped.set()
        with self.assertRaises(SubmissionSkipped):
            latch.assert_allowed_action('order', (), {'is_buy': False,
                'order_type': {'limit': {'tif': 'Alo'}}})

    def test_draining_is_published_without_silent_quoting(self):
        latch = SafetyLatch(strict_maker_spot=True)
        latch.phase = 'running'
        latch.metadata['exit_requested'] = True
        latch.metadata['stop_reason'] = 'Session stop loss: maker exit'
        snap = latch.snapshot()
        self.assertEqual(snap['phase'], 'draining')
        self.assertFalse(snap['quoting_enabled'])
        self.assertTrue(snap['exit_requested'])
        self.assertEqual(snap['stop_reason'], 'Session stop loss: maker exit')


class PostOnlyGateTests(unittest.TestCase):
    def test_post_only_rejection_is_reconciled_without_a_second_order(self):
        latch = SafetyLatch(strict_maker_spot=True)
        reconciled = []
        exchange = SimpleNamespace(order=Mock(return_value={
            'status': 'ok', 'response': {'data': {'statuses': [
                {'error': 'Post only order would have immediately matched, bbo was .30@.31'}]}}}))
        wrapped = latch.wrap(exchange, reconcile_post_only=lambda reqs, name: reconciled.append((reqs, name)))
        wrapped.order(name='coin', is_buy=True, sz=10, limit_px=.30,
                      order_type={'limit': {'tif': 'Alo'}}, reduce_only=False)
        self.assertEqual(len(reconciled), 1)
        self.assertEqual(reconciled[0][1], 'order')
        self.assertEqual(exchange.order.call_count, 1)
        self.assertFalse(latch.pending_cloids)


class OutcomeQuoteTests(unittest.TestCase):
    def test_touch_prices_are_best_bid_and_best_ask(self):
        bot = make_bot()
        st = MarketState(MarketSpec('#101', 0, balance_coin='+101'))
        st.update_bbo(.38268, .38269)
        self.assertEqual(bot.maker_quote_px(st, True), .38268)
        self.assertEqual(bot.maker_quote_px(st, False), .38269)

    def test_exit_sells_whole_inventory_at_best_ask_and_never_bids(self):
        bot = make_bot()
        st = MarketState(MarketSpec('#101', 0, balance_coin='+101'),
                         position=40, available_position=40)
        st.update_bbo(.38268, .38269)
        bot.rotation_requested = lambda: True
        bot.exit_reason = 'Session stop loss: maker exit'
        plan = bot.bilateral_requests(st)
        self.assertNotIn(True, plan)
        self.assertIn(False, plan)
        self.assertEqual(plan[False]['sz'], 40)
        self.assertEqual(plan[False]['limit_px'], .38269)
        self.assertEqual(plan[False]['order_type']['limit']['tif'], 'Alo')

    def test_stop_loss_blocks_buys_but_never_the_exit_sell(self):
        bot = make_bot()
        st = MarketState(MarketSpec('#101', 0, balance_coin='+101'),
                         position=40, available_position=40)
        st.update_bbo(.38268, .38269)
        bot.stats.gross_closed_pnl = -60
        bot.cfg.stop_loss_usd = 50
        self.assertIn('session stop loss', bot.risk_block_reason(st, closing_inventory=False))
        self.assertIsNone(bot.risk_block_reason(st, closing_inventory=True))
        plan = bot.bilateral_requests(st)
        self.assertNotIn(True, plan)   # no new exposure after the loss
        self.assertIn(False, plan)     # the maker ask stays available
        self.assertEqual(plan[False]['order_type']['limit']['tif'], 'Alo')

    def test_post_only_rejection_reconciles_and_refreshes_the_book(self):
        bot = make_bot()
        st = MarketState(MarketSpec('#101', 0, balance_coin='+101'))
        st.last_rest_book_ts = time.time()
        bot.states = {'#101': st}
        bot._reconcile_post_only([{'coin': '#101'}], 'order')
        self.assertTrue(st.reconcile_required)
        self.assertGreater(st.pending_until, time.time())
        self.assertEqual(st.last_rest_book_ts, 0.0)
        # Never a taker fallback: the plan stays Alo only.
        bot.cfg.auto_max_spread_bps = None
        st.update_bbo(.30, .31)
        self.assertEqual(bot.bilateral_requests(st)[True]['order_type']['limit']['tif'], 'Alo')

    def test_negative_pnl_does_not_create_a_crossing_exit(self):
        bot = make_bot()
        st = MarketState(MarketSpec('#101', 0, balance_coin='+101'),
                         position=10, available_position=10)
        st.update_bbo(.30, .31)
        bot.stats.gross_closed_pnl = -40
        bot.rotation_requested = lambda: True
        plan = bot.bilateral_requests(st)
        self.assertEqual(plan[False]['limit_px'], .31)
        self.assertEqual(plan[False]['order_type']['limit']['tif'], 'Alo')

    def test_l2_book_exposes_bounded_depth_for_the_dashboard(self):
        bot = make_bot()
        st = MarketState(MarketSpec('#101', 0, balance_coin='+101'))
        bot.states = {'#101': st}
        stamp = int(time.time()*1000)
        bot.on_l2_book({'data': {'coin': '#101', 'time': stamp, 'levels': [
            [{'px': '.50', 'sz': '40'}, {'px': '.49', 'sz': '30'}],
            [{'px': '.51', 'sz': '35'}, {'px': '.52', 'sz': '25'}],
        ]}})
        self.assertEqual(st.book_bids[0], (.50, 40))
        self.assertEqual(st.book_asks[0], (.51, 35))
        self.assertEqual(len(st.book_bids), 2)
        self.assertEqual(st.book_source, 'l2')
        self.assertGreater(st.book_depth_ts, 0)


class SupervisorEntryTests(unittest.TestCase):
    def supervisor(self):
        sup = OutcomeSupervisor.__new__(OutcomeSupervisor)
        sup.cache = {}
        sup.rotation = SimpleNamespace(describe=lambda sid: {})
        return sup

    def record(self, **overrides):
        row = {'session_id': 's1', 'symbols': ['#101'], 'market_name': 'Test',
               'account_kind': 'outcome', 'phase': 'running', 'config': {},
               'started': time.time() - 20, 'cleanup_confirmed': False,
               'stats_file': 'stats.json', 'pid': 1, 'market': '#101'}
        row.update(overrides)
        return row

    def test_incomplete_accounting_is_not_a_delayed_stream(self):
        sup = self.supervisor()
        sup._read_stats = lambda path: {'session_id': 's1', 'market': '#101',
                                        'ts': time.time(), 'position_usd': 0}
        info = {'proc': SimpleNamespace(pid=2, poll=lambda: None)}
        entry = sup._entry('journal', self.record(), info)
        self.assertFalse(entry['stale'])
        self.assertFalse(entry['accounting_complete'])
        self.assertTrue(entry['quoting_enabled'])

    def test_old_publication_is_delayed(self):
        sup = self.supervisor()
        sup._read_stats = lambda path: {'session_id': 's1', 'market': '#101',
                                        'ts': time.time() - 30, 'position_usd': 0}
        info = {'proc': SimpleNamespace(pid=2, poll=lambda: None)}
        entry = sup._entry('journal', self.record(), info)
        self.assertTrue(entry['stale'])
        self.assertFalse(entry['quoting_enabled'])


class SupervisorStopTests(unittest.TestCase):
    def test_stop_records_maker_exit_and_never_touches_stop_file(self):
        with tempfile.TemporaryDirectory() as folder:
            stop_file = Path(folder) / 'stop'
            sup = OutcomeSupervisor.__new__(OutcomeSupervisor)
            sup.manager = SimpleNamespace(lock=threading.RLock())
            sup.records = lambda: []
            cancel = Mock(return_value=[])
            sup.rotation = SimpleNamespace(cancel=cancel)
            proc = SimpleNamespace(poll=lambda: 0)
            sup.procs = {'s1': {'session_id': 's1', 'stop_file': str(stop_file),
                                'safety_file': str(Path(folder) / 'journal.json'),
                                'market': '#101', 'proc': proc}}
            ids = sup.stop('s1')
            self.assertEqual(ids, ['s1'])
            cancel.assert_called_once_with('s1')  # no replacement can start after Stop
            drain = Path(folder) / 'drain.json'
            self.assertTrue(drain.exists())
            self.assertFalse(stop_file.exists())
            self.assertTrue(json.loads(drain.read_text())['manual'])

    def test_stop_is_idempotent_for_a_dead_unconfirmed_session(self):
        with tempfile.TemporaryDirectory() as folder:
            stop_file = Path(folder) / 'stop'
            sup = OutcomeSupervisor.__new__(OutcomeSupervisor)
            sup.manager = SimpleNamespace(lock=threading.RLock())
            sup.procs = {}
            sup.rotation = SimpleNamespace(cancel=lambda sid: [])
            sup.records = lambda: [(str(Path(folder) / 'journal.json'),
                                    {'session_id': 's1', 'stop_file': str(stop_file),
                                     'cleanup_confirmed': False, 'phase': 'failed',
                                     'symbols': ['#101'], 'config': {}})]
            self.assertEqual(sup.stop('s1'), ['s1'])
            self.assertEqual(sup.stop('s1'), ['s1'])
            self.assertTrue((Path(folder) / 'drain.json').exists())

    def test_resume_requires_an_unconfirmed_outcome_session(self):
        sup = OutcomeSupervisor.__new__(OutcomeSupervisor)
        sup.procs = {}
        sup.manager = SimpleNamespace(lock=threading.RLock())
        sup.records = lambda: [('j', {'session_id': 's1', 'symbols': ['#101'],
                                      'account_kind': 'outcome',
                                      'cleanup_confirmed': True, 'config': {}})]
        with self.assertRaises(ValueError):
            sup.resume_exit('s1')

    def test_resume_trading_starts_bid_ask_worker_without_old_targets(self):
        with tempfile.TemporaryDirectory() as folder:
            journal = Path(folder) / 'journal.json'
            journal.write_text(json.dumps({
                'session_id': 's1', 'exposure_started': True, 'owned_oids': [7],
                'cloids': ['0xabc'], 'pending_cloids': []}))
            sup = OutcomeSupervisor.__new__(OutcomeSupervisor)
            sup.procs = {}
            sup.manager = SimpleNamespace(lock=threading.RLock())
            sup.records = lambda: [(str(journal), {
                'session_id': 's1', 'symbols': ['#101'],
                'account_kind': 'outcome', 'cleanup_confirmed': False,
                'stop_file': str(journal),
                'config': {'market': '#101', 'outcome_side': 'no',
                           'order_size_usd': 15, 'max_position_usd': 200,
                           'exec_mode': 'maker', 'selection_mode': 'auto',
                           'two_sided': True, 'auto_rotate': True,
                           'target_volume_usd': 100,
                           'session_expires_at': time.time() + 60}})]
            sup.start = Mock(return_value={'session_id': 'new'})
            result = sup.resume_trading('s1')
            self.assertEqual(result, {'session_id': 'new'})
            request = sup.start.call_args.args[0]
            self.assertEqual(request.exec_mode, 'maker')
            self.assertTrue(request.resume_trading)
            self.assertFalse(request.resume_exit)
            self.assertTrue(request.auto_rotate)
            self.assertIsNone(request.target_volume_usd)
            self.assertIsNone(request.session_expires_at)
            seed = sup.start.call_args.kwargs['seed']
            self.assertEqual(seed['resume_from'], 's1')
            self.assertTrue(seed['exposure_started'])

    def test_resume_starts_exit_only_worker_with_attribution_seed(self):
        with tempfile.TemporaryDirectory() as folder:
            journal = Path(folder) / 'journal.json'
            journal.write_text(json.dumps({
                'session_id': 's1', 'exposure_started': True, 'owned_oids': [7],
                'cloids': ['0xabc'], 'pending_cloids': []}))
            sup = OutcomeSupervisor.__new__(OutcomeSupervisor)
            sup.procs = {}
            sup.manager = SimpleNamespace(lock=threading.RLock())
            sup.records = lambda: [(str(journal), {
                'session_id': 's1', 'symbols': ['#101'],
                'account_kind': 'outcome', 'cleanup_confirmed': False,
                'stop_file': str(journal),
                'config': {'market': '#101', 'outcome_side': 'no',
                           'order_size_usd': 15, 'max_position_usd': 200,
                           'exec_mode': 'taker', 'selection_mode': 'auto',
                           'two_sided': True}})]
            sup.start = Mock(return_value={'session_id': 'new'})
            result = sup.resume_exit('s1')
            self.assertEqual(result, {'session_id': 'new'})
            request = sup.start.call_args.args[0]
            self.assertEqual(request.exec_mode, 'maker')
            self.assertEqual(request.selection_mode, 'manual')
            self.assertTrue(request.resume_exit)
            self.assertEqual(request.market, '#101')
            seed = sup.start.call_args.kwargs['seed']
            self.assertEqual(seed['resume_from'], 's1')
            self.assertTrue(seed['exposure_started'])
            self.assertEqual(seed['owned_oids'], [7])
            self.assertEqual(json.loads(journal.read_text())['resumed_session_id'], 'new')


class SupervisorAutoRecoveryTests(unittest.TestCase):
    def supervisor(self, folder, record, drain=None, stop=False):
        journal = Path(folder) / 'journal.json'
        journal.write_text(json.dumps(record))
        stop_file = Path(folder) / 'stop'
        if stop:
            stop_file.write_text('')
        if drain is not None:
            (Path(folder) / 'drain.json').write_text(json.dumps(drain))
        sup = OutcomeSupervisor.__new__(OutcomeSupervisor)
        sup.procs = {}
        sup.manager = SimpleNamespace(lock=threading.RLock())
        sup.root = Path(folder)
        sup._recovery = {}
        sup._recovery_lock = threading.RLock()
        rec = dict(record, stop_file=str(stop_file), symbols=['#101'],
                   account_kind='outcome', cleanup_confirmed=False, started=1.0)
        sup.records = lambda: [(str(journal), rec)]
        sup._resume = Mock(return_value={'session_id': 'new'})
        return sup

    def test_crash_is_resumed_as_trading_without_a_button(self):
        with tempfile.TemporaryDirectory() as folder:
            sup = self.supervisor(folder, {'session_id': 's1', 'exposure_started': True})
            sup._auto_recover()
            sup._resume.assert_called_once_with('s1', exit_only=False)

    def test_risk_exit_is_resumed_as_exit_only(self):
        with tempfile.TemporaryDirectory() as folder:
            sup = self.supervisor(folder, {'session_id': 's1', 'exposure_started': True,
                                           'exit_requested': True,
                                           'stop_reason': 'Session stop loss: maker exit'})
            sup._auto_recover()
            sup._resume.assert_called_once_with('s1', exit_only=True)

    def test_rotation_drain_keeps_the_logical_session_trading(self):
        with tempfile.TemporaryDirectory() as folder:
            sup = self.supervisor(folder, {'session_id': 's1', 'exposure_started': True},
                                  drain={'reason': 'low executed $/min'})
            sup._auto_recover()
            sup._resume.assert_called_once_with('s1', exit_only=False)

    def test_manual_stop_resumes_only_the_exit_and_hard_shutdown_never_restarts(self):
        with tempfile.TemporaryDirectory() as folder:
            sup = self.supervisor(folder, {'session_id': 's1', 'exposure_started': True},
                                  drain={'manual': True, 'reason': 'Manual stop: maker exit'})
            sup._auto_recover()
            sup._resume.assert_called_once_with('s1', exit_only=True)
        with tempfile.TemporaryDirectory() as folder:
            sup = self.supervisor(folder, {'session_id': 's1', 'exposure_started': True}, stop=True)
            sup._auto_recover()
            sup._resume.assert_not_called()

    def test_unattributed_session_is_left_unresolved(self):
        with tempfile.TemporaryDirectory() as folder:
            sup = self.supervisor(folder, {'session_id': 's1', 'exposure_started': False})
            sup._auto_recover()
            sup._resume.assert_not_called()


class WorkspaceProjectionTests(unittest.TestCase):
    def test_archived_group_never_hides_an_unconfirmed_leg(self):
        def record(sid, confirmed):
            return ({'session_id': sid, 'symbols': ['#101'], 'config': {'auto_group_id': 'group'},
                     'cleanup_confirmed': confirmed, 'started': 1.0 if sid == 'a' else 2.0})
        rows = {'a': record('a', True), 'b': record('b', False)}
        def entry(path, rec, info=None):
            return {'session_id': rec['session_id'], 'market': '#101',
                    'market_name': 'Test', 'config': rec['config'],
                    'started': rec['started'], 'phase': 'stopped' if rec['cleanup_confirmed'] else 'failed',
                    'running': False, 'cleanup_confirmed': rec['cleanup_confirmed'],
                    'stale': False, 'accounting_complete': True,
                    'volume_usd': 1, 'net_pnl': 0, 'fees': 0, 'fills': 1,
                    'position_usd': 0, 'duration_sec': 1}
        manager = SimpleNamespace(
            outcome=SimpleNamespace(records=lambda: [('a.json', rows['a']), ('b.json', rows['b'])],
                                    _entry=entry, procs={}),
            get_worldcup_stats=lambda: {'display_bots': []},
            get_grid_stats=lambda: {'bots': []},
            obi_procs={}, _session_files=lambda: [])
        runtimes = Runtimes.__new__(Runtimes)
        runtimes.root, runtimes.store, runtimes.accounts, runtimes.factory = Path('.'), None, None, None
        runtimes.items, runtimes.locks, runtimes.read_locks = {}, {}, {}
        runtimes.cache, runtimes.guard, runtimes.source, runtimes.revisions = {}, threading.RLock(), 'x', {}
        runtimes.manager = lambda owner: manager
        snapshot = runtimes.snapshot('owner')
        grouped = next(s for s in snapshot['sessions'] if s['id'] == 'group')
        self.assertFalse(grouped['cleanup_confirmed'])
        self.assertFalse(grouped['running'])
        self.assertEqual(grouped['phase'], 'failed')

    def test_session_exposes_exit_intent_and_reason(self):
        row = {'session_id': 's1', 'market': '#101', 'market_name': 'Test',
               'phase': 'draining', 'running': True, 'cleanup_confirmed': False,
               'volume_usd': None, 'net_pnl': None, 'fees': None, 'fills': None,
               'exit_requested': True, 'stop_reason': 'Manual stop: maker exit',
               'config': {}}
        session = visible_session(row, 'outcome')
        self.assertEqual(session['phase'], 'draining')
        self.assertTrue(session['exit_requested'])
        self.assertEqual(session['stop_reason'], 'Manual stop: maker exit')


if __name__ == '__main__':
    unittest.main()
