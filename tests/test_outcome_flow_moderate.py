"""Moderately relaxed Auto cadence still requires real two-way activity."""
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import patch

from zeus.outcome_flow import OutcomeFlowScanner, flow_metrics


NOW = 1_800_000_000.0


def trades(ages=(55, 45, 35, 25), sides=('B', 'B', 'B', 'A')):
    return [dict(coin='#101', time=(NOW-age)*1000, px='.5', sz='100', side=side, tid=i)
            for i, (age, side) in enumerate(zip(ages, sides))]


class ModerateFlowTests(unittest.TestCase):
    def scanner(self, history=None):
        scan = OutcomeFlowScanner()
        scan.connected, scan.connected_at, scan.catalog_at = True, NOW-90, NOW
        scan.rows = {10: dict(id=10, name='Offline market', fee_scale=1, deployer_fee_scale=1)}
        scan.subscribed = {'#100', '#101'}
        scan.history['#101'].extend(trades() if history is None else history)
        return scan

    def book(self, *, bid='.5', ask='.5001', quantity='1000', at=NOW):
        return dict(time=at*1000, levels=[[dict(px=bid, sz=quantity)], [dict(px=ask, sz=quantity)]])

    def run_scan(self, scan, *, book=None, order_usd=110, spread_bps=10):
        with patch('zeus.outcome_flow.time.time', return_value=NOW), ThreadPoolExecutor(2) as pool, \
                patch.object(scan, '_book_executor', return_value=pool), \
                patch.object(scan, '_book', return_value=book or self.book()):
            return scan._snapshot('no', order_usd, spread_bps)

    def test_four_spaced_prints_with_only_one_sell_event_can_quote_a_funded_book(self):
        result = self.run_scan(self.scanner())
        self.assertTrue(result['ready'])
        candidate = result['candidates'][0]
        self.assertEqual(candidate['events_5m'], 4)
        self.assertEqual(candidate['sell_events_1m'], 1)
        self.assertEqual(candidate['last_trade_age_sec'], 25)
        self.assertEqual(candidate['volume_usd_1m'], 200)
        self.assertEqual(result['order_usd'], 110)
        self.assertEqual(result['min_depth_usd'], 110)

    def test_trade_age_boundary_is_45_seconds_and_two_way_activity_is_still_recent(self):
        for age, expected in ((45, True), (46, False)):
            with self.subTest(age=age):
                # Two older observations establish cadence, two recent prints
                # establish one real event in each direction within 60 seconds.
                metric = flow_metrics(trades((age+150, age+105, 58, age), ('B', 'A', 'A', 'B')), NOW)
                self.assertEqual(metric['eligible'], expected)
                self.assertEqual(metric['volume_usd_1m'], 100)

    def test_one_sided_inactive_or_single_burst_markets_stay_excluded(self):
        scenarios = {
            'one_sided': trades(sides=('B',)*4),
            'inactive_last_minute': trades((105, 95, 85, 75)),
            'short_burst': trades((29, 28, 27, 26)),
            'no_trades': [],
        }
        for name, history in scenarios.items():
            with self.subTest(name=name):
                self.assertFalse(self.run_scan(self.scanner(history))['ready'])

    def test_high_volume_repeated_burst_is_admitted(self):
        # $800 in the last minute, split in two bursts 70 s apart. The last
        # minute has only one 10 s bin, but the 5-minute history proves the
        # market is repeatedly two-way.
        history = [
            dict(coin='#101', time=(NOW-age)*1000, px='.5', sz='800',
                 side=side, tid=index)
            for index, (age, side) in enumerate(zip((76, 75, 6, 5), ('B', 'A', 'B', 'A')))
        ]
        metric = flow_metrics(history, NOW)
        self.assertTrue(metric['eligible'])
        self.assertEqual(metric['flow_mode'], 'burst')
        self.assertEqual(metric['volume_usd_1m'], 800)
        self.assertEqual(metric['active_bins_1m'], 1)
        self.assertEqual(metric['active_bins_5m'], 2)

    def test_high_volume_single_burst_or_one_sided_flow_stays_excluded(self):
        burst = [
            dict(coin='#101', time=(NOW-age)*1000, px='.5', sz='400',
                 side=side, tid=index)
            for index, (age, side) in enumerate(zip((6, 6, 5, 5), ('B', 'A', 'B', 'A')))
        ]
        metric = flow_metrics(burst, NOW)
        self.assertFalse(metric['eligible'])
        self.assertEqual(metric['flow_mode'], 'none')
        self.assertEqual(metric['active_bins_5m'], 1)

        one_sided = [
            dict(coin='#101', time=(NOW-age)*1000, px='.5', sz='400',
                 side='B', tid=index)
            for index, age in enumerate((76, 75, 6, 5))
        ]
        metric = flow_metrics(one_sided, NOW)
        self.assertFalse(metric['eligible'])
        self.assertEqual(metric['min_side_usd_1m'], 0)

    def test_material_flow_floor_keeps_small_bursts_out(self):
        for size, expected in ((249, False), (251, True)):
            with self.subTest(size=size):
                history = [
                    dict(coin='#101', time=(NOW-age)*1000, px='.5', sz=str(size),
                         side=side, tid=index)
                    for index, (age, side) in enumerate(zip((76, 75, 6, 5),
                                                            ('B', 'A', 'B', 'A')))
                ]
                metric = flow_metrics(history, NOW)
                self.assertEqual(metric['eligible'], expected)
                if expected:
                    self.assertEqual(metric['flow_mode'], 'burst')

    def test_book_freshness_spread_and_user_depth_budget_are_unchanged(self):
        for name, book in (
            ('shallow', self.book(quantity='100')),
            ('wide_spread', self.book(ask='.51')),
            ('stale', self.book(at=NOW-6)),
            ('crossed', self.book(ask='.49')),
        ):
            with self.subTest(name=name):
                self.assertFalse(self.run_scan(self.scanner(), book=book)['ready'])
        self.assertFalse(self.run_scan(self.scanner(), order_usd=600)['ready'])

    def test_old_diagnostic_is_replaced_with_actual_cadence_criteria(self):
        result = self.run_scan(self.scanner(trades(sides=('B',)*4)))
        self.assertFalse(result['ready'])
        self.assertIn('irrégulier', result['reason'])
        self.assertNotIn('Aucun flux deux sens dans les 5', result['reason'])
        self.assertEqual(result['flow_thresholds']['max_trade_age_sec'], 45)

    def test_duplicates_and_five_minute_history_do_not_inflate_one_minute_volume(self):
        history = trades((250, 100, 55, 40), ('B', 'A', 'A', 'B'))
        metric = flow_metrics(history*20, NOW)
        self.assertTrue(metric['eligible'])
        self.assertEqual(metric['trades_5m'], 4)
        self.assertEqual(metric['volume_usd_1m'], 100)

    def test_cache_and_final_validation_accept_the_same_new_age_limit(self):
        scan = self.scanner(trades((250, 100, 55, 40), ('B', 'A', 'A', 'B')))
        with ThreadPoolExecutor(2) as pool, \
                patch('zeus.outcome_costs.maker_profile', return_value=.0004), \
                patch('zeus.outcome_flow.time.monotonic', return_value=100), \
                patch.object(scan, '_book_executor', return_value=pool), \
                patch.object(scan, '_book', return_value=self.book()) as read:
            with patch('zeus.outcome_flow.time.time', return_value=NOW):
                self.assertTrue(scan.snapshot('no', 110, 10)['ready'])
            with patch('zeus.outcome_flow.time.time', return_value=NOW+.2):
                self.assertTrue(scan.snapshot('no', 110, 10)['ready'])
            read.assert_called_once()

    def test_a_slow_book_read_cannot_admit_a_trade_which_has_aged_past_45_seconds(self):
        scan = self.scanner(trades((194, 149, 58, 44), ('B', 'A', 'A', 'B')))
        clock = [NOW]
        def book(_):
            clock[0] = NOW+2
            return self.book(at=clock[0])
        with patch('zeus.outcome_flow.time.time', side_effect=lambda: clock[0]), \
                ThreadPoolExecutor(2) as pool, \
                patch.object(scan, '_book_executor', return_value=pool), \
                patch.object(scan, '_book', side_effect=book):
            self.assertFalse(scan._snapshot('no', 110, 10)['ready'])

    def test_warmup_and_subscription_completeness_still_block_admission(self):
        for stage in ('warmup', 'missing_subscription', 'disconnected'):
            with self.subTest(stage=stage):
                scan = self.scanner()
                if stage == 'warmup':
                    scan.connected_at = NOW-30
                elif stage == 'missing_subscription':
                    scan.subscribed.remove('#100')
                else:
                    scan.connected = False
                self.assertFalse(self.run_scan(scan)['ready'])
