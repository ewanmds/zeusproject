"""Costs inform Auto without blocking volume mode. No real orders."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from zeus.outcome_costs import base_maker_rate, closing_rate, net_edge_bps, maker_profile
from zeus.outcome_flow import OutcomeFlowScanner
from zeus.bots.outcome_worker import build_config
from zeus.bots.worldcup import HyperliquidSpreadBot, MarketState, MarketSpec, SessionStats


class OutcomeCostsTests(unittest.TestCase):
    def test_actual_maker_fill_fee_and_negative_one_tick_round_trip(self):
        base = base_maker_rate({'userSpotAddRate': '0.0004', 'activeReferralDiscount': '0.04'})
        rate = closing_rate(base, '1', '1')
        # Austria fill: 40 tokens sold at .38269, .01175623 USDC fee.
        self.assertAlmostEqual(.38269*40*rate, .01175623, delta=1e-8)
        self.assertLess(net_edge_bps(.38268, .38269, rate), 0)
        self.assertAlmostEqual(closing_rate(base, 1, 3), base*6)
        self.assertEqual(base_maker_rate({'userSpotAddRate': '-.00001', 'activeReferralDiscount': '0'}), 0)

    def test_missing_invalid_metadata_never_becomes_zero_fees(self):
        for args in [(None, 1, 1), (.001, None, 1), (.001, 1, float('nan')), (.001, 1, -1), (.001, False, 1)]:
            with self.assertRaises((ValueError, TypeError)):
                closing_rate(*args)
        with self.assertRaises(KeyError):
            base_maker_rate({})

    def test_account_cache_is_scoped_and_refresh_never_blocks_quotes(self):
        from zeus import outcome_costs as c
        with patch.object(c, '_profiles', {'a': (time.monotonic(), .0002), 'b': (time.monotonic()-301, .0004)}), \
             patch.object(c, '_pending', {'b'}):
            self.assertEqual(maker_profile('A'), .0002)
            self.assertIsNone(maker_profile('B'))
            self.assertEqual(maker_profile('A'), .0002)

    def test_failed_fee_reads_have_a_retry_backoff(self):
        from zeus import outcome_costs as c
        with patch.object(c, '_profiles', {}), patch.object(c, '_pending', set()), \
             patch.object(c, '_retry_at', {}), patch.object(c.threading, 'Thread') as thread:
            self.assertIsNone(maker_profile('test-wallet'))
            c._pending.clear()  # Simulate a fast HTTP error completing.
            self.assertIsNone(maker_profile('test-wallet'))
            self.assertEqual(thread.call_count, 1)

    def bot(self):
        bot = HyperliquidSpreadBot.__new__(HyperliquidSpreadBot)
        bot.cfg = build_config({'market': '#101', 'order_size_usd': 15, 'max_position_usd': 200,
                                'exec_mode': 'maker', 'selection_mode': 'auto', 'two_sided': True,
                                'maker_spread_pct': None}, None)
        bot.stats = SessionStats()
        bot.available_usdc = 100
        bot.funds = None
        st = MarketState(MarketSpec('#101', 0, balance_coin='+101'), position=40, available_position=40)
        st.update_bbo(.38268, .38269)
        return bot, st

    def test_negative_cost_estimate_does_not_block_funded_quotes(self):
        bot, st = self.bot()
        with patch('zeus.outcome_costs.maker_profile', side_effect=AssertionError('Trading must not wait for fee estimates')):
            plan = bot.bilateral_requests(st)
            self.assertIn(True, plan)
            self.assertIn(False, plan)
            self.assertEqual(plan[False]['order_type']['limit']['tif'], 'Alo')
            self.assertIsNone(bot.risk_block_reason(st, closing_inventory=True))
            self.assertEqual(bot.cfg.order_usd, 15)

    def test_unknown_fees_do_not_block_exit_or_risk_guards(self):
        bot, st = self.bot()
        with patch('zeus.outcome_costs.maker_profile', return_value=None):
            plan = bot.bilateral_requests(st)
            self.assertIn(True, plan)
            self.assertIn(False, plan)
            st.avg_entry_px = .5
            self.assertIn('inventory stop', bot.inventory_risk_reason(st))

    def test_partial_bid_keeps_its_queue_despite_negative_cost_estimate(self):
        bot, st = self.bot()
        st.bid_oid, st.bid_quantity, st.quoted_bid = 123, 2, .38268
        with patch('zeus.outcome_costs.maker_profile', return_value=.000384):
            self.assertEqual(bot.bilateral_requests(st)[True]['sz'], 2)
            st.update_bbo(.38268, .38302)
            st.quoted_bid = .38295
            self.assertEqual(bot.bilateral_requests(st)[True]['limit_px'], .38295)
        # Manual Outcome quotes and HIP-3 strict BBO retain their policy.
        bot.cfg.auto_max_spread_bps = None
        self.assertIsNone(bot.risk_block_reason(st))

    def test_catalog_keeps_fee_scales_including_explicit_zero(self):
        from zeus.outcomes import _build_rows
        rows = _build_rows({'feeScale': '1', 'outcomes': [{'outcome': 10, 'venue': 'out',
                           'name': 'Example', 'deployerFeeScale': '0',
                           'sideSpecs': [{'name': 'Yes'}, {'name': 'No'}]}]}, {}, {})
        self.assertEqual(rows[0]['fee_scale'], '1')
        self.assertEqual(rows[0]['deployer_fee_scale'], '0')

    def ready_scanner(self):
        scan = OutcomeFlowScanner()
        now = time.time()
        scan.connected = True
        scan.connected_at = now-100
        scan.catalog_at = now
        scan.rows = {10: {'id': 10, 'name': 'Tight', 'fee_scale': 1, 'deployer_fee_scale': 1},
                     20: {'id': 20, 'name': 'Covers fees', 'fee_scale': 1, 'deployer_fee_scale': 1}}
        scan.subscribed = {'#100', '#101', '#200', '#201'}
        for coin in ['#101', '#201']:
            scan.history[coin].extend({'time': (now-age)*1000, 'px': .38268, 'sz': 100,
                                      'side': 'B' if i % 2 else 'A', 'tid': i}
                                     for i, age in enumerate(range(59, 0, -4)))
        def book(coin):
            ask = '.38269' if coin == '#101' else '.38302'
            return {'time': now*1000, 'levels': [[{'px': '.38268', 'sz': '100'}], [{'px': ask, 'sz': '100'}]]}
        return scan, book

    def test_scanner_keeps_active_markets_even_when_spread_does_not_cover_fees(self):
        scan, book = self.ready_scanner()
        with ThreadPoolExecutor(2) as pool, patch.object(scan, '_book_executor', return_value=pool), patch.object(scan, '_book', side_effect=book):
            result = scan._snapshot('no', 15, 10, base_rate=.000384)
        self.assertTrue(result['ready'])
        self.assertEqual({r['market'] for r in result['candidates']}, {'#101', '#201'})
        self.assertEqual(result['fees_not_covered'], 1)
        self.assertEqual(result['cost_policy'], 'informational')
        tight = next(r for r in result['candidates'] if r['market']=='#101')
        self.assertLess(tight['net_touch_edge_bps'], 0)
        self.assertFalse(tight['fees_covered'])
        self.assertFalse(result['error'])

    def test_unknown_fee_profile_or_metadata_does_not_hide_eligible_flow(self):
        for base in [None, .000384]:
            scan, book = self.ready_scanner()
            for row in scan.rows.values():
                row.pop('fee_scale')
            with ThreadPoolExecutor(2) as pool, patch.object(scan, '_book_executor', return_value=pool), patch.object(scan, '_book', side_effect=book):
                result = scan._snapshot('no', 15, 10, base_rate=base)
            self.assertTrue(result['ready'])
            self.assertFalse(result['error'])
            self.assertEqual(result['cost_unavailable'], 2)
            self.assertTrue(all(r['net_touch_edge_bps'] is None for r in result['candidates']))
            self.assertTrue(all(r['cost_status']=='unavailable' for r in result['candidates']))

    def test_spread_depth_and_freshness_still_limit_scanner(self):
        for reason in ['spread', 'depth', 'stale', 'crossed']:
            scan, original = self.ready_scanner()
            def book(coin):
                row = original(coin)
                if reason == 'stale':
                    row['time'] -= 10000
                if reason == 'crossed':
                    row['levels'][1][0]['px'] = '.38'
                return row
            with ThreadPoolExecutor(2) as pool, patch.object(scan, '_book_executor', return_value=pool), patch.object(scan, '_book', side_effect=book):
                result = scan._snapshot('no', 50 if reason=='depth' else 15,
                                        .1 if reason=='spread' else 10, base_rate=None)
            self.assertFalse(result['ready'], reason)

    def test_unknown_fee_profile_alone_keeps_estimates_unknown_without_blocking(self):
        scan, book = self.ready_scanner()
        with ThreadPoolExecutor(2) as pool, patch.object(scan, '_book_executor', return_value=pool), patch.object(scan, '_book', side_effect=book):
            result = scan._snapshot('no', 15, 10, base_rate=None)
        self.assertTrue(result['ready'])
        self.assertEqual(result['cost_unavailable'], 2)

    def test_funds_inventory_and_spread_guards_still_block_new_bids(self):
        bot, st = self.bot()
        bot.available_usdc = 0
        self.assertNotIn(True, bot.bilateral_requests(st))
        bot.available_usdc = 100
        bot.cfg.max_position_usd = st.position_usd+14
        self.assertNotIn(True, bot.bilateral_requests(st))
        bot.cfg.max_position_usd = 200
        st.update_bbo(.38, .39)
        self.assertNotIn(True, bot.bilateral_requests(st))
        st.update_bbo(.38268, .38269)
        st.last_book_ts = time.time()-10
        self.assertIn('stale', bot.risk_block_reason(st))

    def test_auto_launch_admits_negative_or_unknown_cost_without_real_process(self):
        from zeus.outcome_supervisor import OutcomeSupervisor
        from zeus.server import WorldcupStartRequest
        for base in [.000384, None]:
            scan, book = self.ready_scanner()
            with ThreadPoolExecutor(2) as pool, patch.object(scan, '_book_executor', return_value=pool), patch.object(scan, '_book', side_effect=book):
                snapshot = scan._snapshot('no', 15, 10, base_rate=base)
            # Regression case: the only available market has negative or
            # unknown estimated edge; it must still be admitted for volume.
            snapshot['candidates'] = [r for r in snapshot['candidates'] if r['market']=='#101']
            with tempfile.TemporaryDirectory() as folder:
                supervisor = OutcomeSupervisor.__new__(OutcomeSupervisor)
                supervisor.root, supervisor.procs = Path(folder), {}
                context = SimpleNamespace(account='0x'+'1'*40, private_key='offline-test', environment=Mock(return_value={}))
                supervisor.manager = SimpleNamespace(account_context=context, lock=threading.RLock(), _reserve_session=Mock(return_value=None))
                supervisor.rotation = SimpleNamespace(assert_admission=Mock(), candidate_allowed=Mock(return_value=True),
                                                       lock=threading.RLock(), register=Mock())
                supervisor.status = Mock(return_value={'bots': []})
                supervisor.records = Mock(return_value=[])
                request = WorldcupStartRequest(selection_mode='auto', exec_mode='maker', outcome_side='no',
                                               order_size_usd=15, max_position_usd=200)
                with patch('zeus.outcome_flow.scanner.snapshot', return_value=snapshot), \
                     patch('zeus.outcomes.resolve_market', return_value={'key': '#101', 'name': 'Test'}), \
                     patch('zeus.outcome_supervisor.subprocess.Popen', return_value=SimpleNamespace(pid=123)) as spawn:
                    result = supervisor.start(request)
                self.assertEqual(result['market'], '#101')
                spawn.assert_called_once()
                public = supervisor.procs[result['session_id']]['config']
                self.assertEqual(public['order_size_usd'], 15)
                self.assertEqual(public['max_position_usd'], 200)
                edge = public['auto_selection']['net_touch_edge_bps']
                self.assertTrue(edge is None if base is None else edge < 0)


if __name__ == '__main__':
    unittest.main()
