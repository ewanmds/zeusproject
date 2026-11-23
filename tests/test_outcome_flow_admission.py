"""Flow is informational; valid fresh books remain eligible without prints."""
from concurrent.futures import ThreadPoolExecutor
import json
import time
import unittest
from unittest.mock import patch

from zeus.outcome_flow import OutcomeFlowScanner, candidate_rank


class FlowAdmissionTests(unittest.TestCase):
    def scanner(self, count=1):
        scan = OutcomeFlowScanner()
        now = time.time()
        scan.connected = True
        scan.connected_at = now
        scan.catalog_at = now
        scan.rows = {i: {'id': i, 'name': f'Market {i}', 'fee_scale': 1,
                          'deployer_fee_scale': 1} for i in range(10, 10+count)}
        return scan

    def book(self, coin, *, stale=False):
        return {'coin': coin, 'time': (time.time()-(30 if stale else 0))*1000,
                'levels': [[{'px': '.50', 'sz': '1000'}],
                           [{'px': '.5001', 'sz': '1000'}]]}

    def run_scan(self, scan, book=None):
        with ThreadPoolExecutor(2) as pool, patch.object(scan, '_book_executor', return_value=pool), \
                patch.object(scan, '_book', side_effect=book or self.book):
            return scan._snapshot('no', 70, 10)

    def test_no_trades_and_no_sixty_second_warmup_still_admits_fresh_book(self):
        scan = self.scanner()
        result = self.run_scan(scan)
        self.assertTrue(result['ready'])
        self.assertEqual(result['flow_policy'], 'informational')
        self.assertEqual(result['rejections']['flow'], 0)
        candidate = result['candidates'][0]
        self.assertEqual(candidate['volume_usd_1m'], 0)
        self.assertIsNone(candidate['queue_proxy_sec'])
        candidate_rank(candidate, {}, time.time())
        json.dumps(result, allow_nan=False)

    def test_one_sided_or_old_flow_is_not_a_hidden_admission_filter(self):
        for age in (1, 100):
            scan = self.scanner()
            scan.history['#101'].append({'time': (time.time()-age)*1000, 'px': '.5',
                                        'sz': '10', 'side': 'B', 'tid': 1})
            result = self.run_scan(scan)
            self.assertTrue(result['ready'])
            self.assertFalse(result['candidates'][0]['flow_eligible'])
            self.assertTrue(result['candidates'][0]['eligible'])
            self.assertEqual(result['rejections']['flow'], 0)

    def test_stale_book_still_blocks_without_recent_flow(self):
        result = self.run_scan(self.scanner(), lambda coin: self.book(coin, stale=True))
        self.assertFalse(result['ready'])
        self.assertEqual(result['rejections']['stale'], 1)
        self.assertNotIn('flux', result['reason'])

    def test_discovery_explores_more_than_the_first_twenty_four_without_flow(self):
        scan = self.scanner(50)
        seen = set()
        def book(coin):
            seen.add(coin)
            return self.book(coin, stale=int(coin[1:])//10 < 50)
        first = self.run_scan(scan, book)
        scan.book_selection_at -= 6
        second = self.run_scan(scan, book)
        self.assertFalse(first['ready'])
        self.assertTrue(second['ready'])
        self.assertGreater(len(seen), 24)
        self.assertLessEqual(len(scan.book_targets), 24)
