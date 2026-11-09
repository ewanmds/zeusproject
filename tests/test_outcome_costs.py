"""Fee-aware Auto admission and exits, with no exchange or real orders."""
from concurrent.futures import ThreadPoolExecutor
import time
import unittest
from unittest.mock import patch

from zeus.outcome_costs import base_maker_rate, closing_rate, net_edge_bps, candidate_cost, maker_profile
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
        bot.outcome_fee_scales = {10: (1, 1)}
        st = MarketState(MarketSpec('#101', 0, balance_coin='+101'), position=40, available_position=40)
        st.update_bbo(.38268, .38269)
        return bot, st

    def test_negative_edge_pulls_bid_keeps_passive_inventory_exit(self):
        bot, st = self.bot()
        with patch('zeus.outcome_costs.maker_profile', return_value=.000384):
            plan = bot.bilateral_requests(st)
            self.assertNotIn(True, plan)
            self.assertIn(False, plan)
            self.assertEqual(plan[False]['order_type']['limit']['tif'], 'Alo')
            self.assertIsNone(bot.risk_block_reason(st, closing_inventory=True))
            st.update_bbo(.38268, .38302)  # <10bps, covers fees + 1bp.
            self.assertIn(True, bot.bilateral_requests(st))
            self.assertEqual(bot.cfg.order_usd, 15)

    def test_unknown_fees_do_not_block_exit_or_risk_guards(self):
        bot, st = self.bot()
        with patch('zeus.outcome_costs.maker_profile', return_value=None):
            plan = bot.bilateral_requests(st)
            self.assertNotIn(True, plan)
            self.assertIn(False, plan)
            st.avg_entry_px = .5
            self.assertIn('inventory stop', bot.inventory_risk_reason(st))

    def test_held_partial_bid_does_not_bypass_changed_fee_economics(self):
        bot, st = self.bot()
        st.bid_oid, st.bid_quantity, st.quoted_bid = 123, 2, .38268
        with patch('zeus.outcome_costs.maker_profile', return_value=.000384):
            self.assertNotIn(True, bot.bilateral_requests(st))
            st.update_bbo(.38268, .38302)
            st.quoted_bid = .38295
            self.assertNotIn(True, bot.bilateral_requests(st))
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

    def test_scanner_rejects_fast_queue_with_negative_net_edge(self):
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
        with ThreadPoolExecutor(2) as pool, patch.object(scan, '_book_executor', return_value=pool), patch.object(scan, '_book', side_effect=book):
            result = scan._snapshot('no', 15, 10, base_rate=.000384)
        self.assertEqual([r['market'] for r in result['candidates']], ['#201'])
        self.assertEqual(result['cost_rejected'], 1)
        self.assertGreaterEqual(result['candidates'][0]['net_touch_edge_bps'], 1)
        # Crossing the configured maximum spread is still excluded.
        self.assertFalse(candidate_cost({'bid': .4, 'ask': .40001, 'fee_scale': 1, 'deployer_fee_scale': 1}, .000384)['cost_eligible'])


if __name__ == '__main__':
    unittest.main()
