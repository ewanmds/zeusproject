from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import patch

from zeus.outcome_flow import OutcomeFlowScanner
from test_outcome_flow_moderate import NOW, trades


class ObservationsTests(unittest.TestCase):
    def scanner(self):
        scan = OutcomeFlowScanner()
        scan.connected, scan.connected_at, scan.catalog_at = True, NOW-90, NOW
        scan.rows = {10: dict(id=10, name='One sided'), 11: dict(id=11, name='No prints')}
        scan.subscribed = {'#100', '#101', '#110', '#111'}
        scan.history['#101'].extend(trades(sides=('B',)*4))
        return scan

    def test_rejected_and_zero_volume_markets_stay_visible_without_http(self):
        scan = self.scanner()
        with patch('zeus.outcome_flow.time.time', return_value=NOW), \
                patch('zeus.outcome_flow._http') as http:
            result = scan.observations('no', 110, 10)
        http.assert_not_called()
        self.assertEqual(len(result['observed_markets']), 2)
        first, second = result['observed_markets']
        self.assertEqual(first['volume_usd_1m'], 200)
        self.assertFalse(first['auto_eligible'])
        self.assertIn('sens', first['eligibility_reason'])
        self.assertEqual(second['volume_usd_1m'], 0)
        self.assertIsNone(second['spread_bps'])

    def test_displaying_rejected_flow_does_not_admit_it(self):
        scan = self.scanner()
        with patch('zeus.outcome_flow.time.time', return_value=NOW), ThreadPoolExecutor(1) as pool, \
                patch.object(scan, '_book_executor', return_value=pool):
            self.assertEqual(scan._snapshot('no', 110, 10)['candidates'], [])
            self.assertEqual(len(scan.observations('no', 110, 10)['observed_markets']), 2)

    def test_admission_requires_current_matching_scan_and_expires_with_book(self):
        scan = self.scanner()
        scan.history['#101'].clear()
        scan.history['#101'].extend(trades())
        candidate = dict(market='#101', book_updated_at=NOW, last_trade_age_sec=25,
                         spread_bps=2, net_touch_edge_bps=-5, queue_proxy_sec=30)
        key = ('no', 110, 10, .0004, scan.generation, scan.catalog_revision)
        scan.snapshot_cache[key] = (0, dict(updated_at=NOW, candidates=[candidate]))
        with patch('zeus.outcome_flow.time.time', return_value=NOW):
            self.assertTrue(scan.observations('no', 110, 10)['observed_markets'][0]['auto_eligible'])
            self.assertFalse(scan.observations('no', 150, 10)['observed_markets'][0]['auto_eligible'])
        with patch('zeus.outcome_flow.time.time', return_value=NOW+6):
            self.assertFalse(scan.observations('no', 110, 10)['observed_markets'][0]['auto_eligible'])

    def test_updated_trade_changes_rate_and_disconnection_removes_admission(self):
        scan = self.scanner()
        with patch('zeus.outcome_flow.time.time', return_value=NOW):
            before = scan.observations('no', 110, 10)['observed_markets'][0]['volume_usd_1m']
            scan.ingest([dict(coin='#101', time=NOW*1000, px='.5', sz='20', side='A', tid=99)])
            self.assertEqual(scan.observations('no', 110, 10)['observed_markets'][0]['volume_usd_1m'], before+10)
            scan.connected = False
            result = scan.observations('no', 110, 10)
            self.assertFalse(result['observation_connected'])
            self.assertTrue(all(not row['auto_eligible'] for row in result['observed_markets']))
