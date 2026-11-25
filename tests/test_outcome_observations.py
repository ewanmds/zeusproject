from concurrent.futures import ThreadPoolExecutor
import json
import unittest
from unittest.mock import Mock, patch

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

    def test_subscription_quorum_tolerates_a_lost_ack(self):
        scan = OutcomeFlowScanner()
        scan.connected, scan.connected_at, scan.catalog_at = True, NOW-90, NOW
        scan.rows = {index: dict(id=index, name='M%d' % index) for index in range(10)}
        scan.subscribed = {f"#{10*index+side}" for index in range(10) for side in (0, 1)}
        self.assertTrue(scan._subscription_ready_locked(NOW))
        scan.subscribed.discard('#100')  # 19/20 = 95% stays ready
        self.assertTrue(scan._subscription_ready_locked(NOW))
        scan.subscribed = set(sorted(scan.subscribed)[:15])  # 75% is not enough
        self.assertFalse(scan._subscription_ready_locked(NOW))

    def test_missing_subscription_reason_reports_progress(self):
        scan = self.scanner()
        scan.subscribed = {'#100', '#101', '#110'}
        with patch('zeus.outcome_flow.time.time', return_value=NOW):
            result = scan._snapshot('no', 110, 10)
        self.assertFalse(result['ready'])
        self.assertIn('Souscriptions en cours', result['reason'])

    def test_l2_ack_is_tracked_and_an_error_forces_a_retry(self):
        scan = OutcomeFlowScanner()
        scan.rows = {10: dict(id=10, name='M')}
        scan._message(json.dumps({'channel': 'subscriptionResponse', 'data': {
            'method': 'subscribe', 'subscription': {'type': 'l2Book', 'coin': '#101'}}}))
        self.assertIn('#101', scan.book_subscribed)
        scan._message(json.dumps({'channel': 'l2Book', 'data': {
            'coin': '#101', 'time': int(NOW*1000),
            'levels': [[{'px': '.5', 'sz': '10'}], [{'px': '.5001', 'sz': '10'}]]}}))
        self.assertIn('#101', scan.books)
        scan._message(json.dumps({'channel': 'error', 'data': 'Too many subscriptions'}))
        self.assertFalse(scan.book_subscribed)
        self.assertEqual(scan.subscription_error, 'Too many subscriptions')

    def test_stale_book_is_displayed_but_never_admitted(self):
        scan = self.scanner()
        scan.rows[10].update(fee_scale=1, deployer_fee_scale=1)
        scan.history['#101'].clear()
        scan.history['#101'].extend(trades())
        scan.books['#101'] = (NOW-20, {
            'time': (NOW-20)*1000,
            'levels': [[{'px': '.5', 'sz': '1000'}], [{'px': '.5001', 'sz': '1000'}]]})
        stale = scan.books['#101'][1]
        with patch('zeus.outcome_flow.time.time', return_value=NOW):
            observed = scan.observations('no', 110, 10)['observed_markets'][0]
            self.assertIsNotNone(observed['spread_bps'])
            self.assertIsNotNone(observed['queue_proxy_sec'])
            self.assertIsNotNone(observed['net_touch_edge_bps'])
            self.assertFalse(observed['auto_eligible'])
            self.assertIn('Carnet ancien', observed['eligibility_reason'])
            with ThreadPoolExecutor(1) as pool, \
                    patch.object(scan, '_book_executor', return_value=pool), \
                    patch.object(scan, '_book', return_value=stale):
                snapshot = scan._snapshot('no', 110, 10, base_rate=0)
        self.assertFalse(snapshot['ready'])

    def test_history_and_warmup_survive_a_short_reconnect(self):
        scan = self.scanner()
        scan.history['#101'].extend(trades())
        before = len(scan.history['#101'])
        with patch('zeus.outcome_flow.time.time', return_value=NOW):
            scan.connected_at = NOW-90
            scan._mark_open()
            self.assertEqual(len(scan.history['#101']), before)
            self.assertEqual(scan.connected_at, NOW-90)
            scan.connected_at = NOW-200
            scan._mark_open()
            self.assertEqual(scan.connected_at, NOW)

    def test_live_ws_book_is_current_even_when_the_exchange_timestamp_is_old(self):
        scan = self.scanner()
        scan.rows[10].update(fee_scale=1, deployer_fee_scale=1)
        scan.history['#101'].clear()
        scan.history['#101'].extend(trades())
        # The exchange timestamp is the last change (90 s ago); the websocket
        # delivered this unchanged book 20 s ago on the current connection.
        book = {'time': int((NOW-90)*1000),
                'levels': [[{'px': '.5', 'sz': '1000'}],
                           [{'px': '.5001', 'sz': '1000'}]]}
        scan.generation = 3
        scan.connected = True
        scan.book_received['#101'] = NOW-20
        scan.book_live['#101'] = 3
        scan.books['#101'] = (NOW-20, book)
        with patch('zeus.outcome_flow.time.time', return_value=NOW), \
                ThreadPoolExecutor(1) as pool, \
                patch.object(scan, '_book_executor', return_value=pool), \
                patch.object(scan, '_book', return_value=book):
            result = scan.snapshot('no', 110, 10)
            observed = scan.observations('no', 110, 10)['observed_markets'][0]
        self.assertTrue(result['ready'])
        self.assertEqual(result['candidates'][0]['market'], '#101')
        self.assertTrue(observed['auto_eligible'])
        self.assertNotIn('Carnet ancien', observed['eligibility_reason'])

    def test_fresh_rest_fallback_book_is_admitted(self):
        scan = self.scanner()
        scan.rows[10].update(fee_scale=1, deployer_fee_scale=1)
        scan.history['#101'].clear()
        scan.history['#101'].extend(trades())
        book = {'time': int(NOW*1000),
                'levels': [[{'px': '.5', 'sz': '1000'}],
                           [{'px': '.5001', 'sz': '1000'}]]}
        scan.generation = 3
        scan.connected = True
        scan.book_received['#101'] = NOW
        scan.books['#101'] = (NOW, book)  # REST fallback, not a live WS book
        with patch('zeus.outcome_flow.time.time', return_value=NOW), \
                ThreadPoolExecutor(1) as pool, \
                patch.object(scan, '_book_executor', return_value=pool), \
                patch.object(scan, '_book', return_value=book):
            result = scan._snapshot('no', 110, 10, base_rate=0)
        self.assertTrue(result['ready'])
        self.assertEqual(result['candidates'][0]['market'], '#101')

    def test_rest_refresh_after_the_prefetch_snapshot_is_admitted(self):
        scan = self.scanner()
        scan.rows[10].update(fee_scale=1, deployer_fee_scale=1)
        scan.history['#101'].clear()
        scan.history['#101'].extend(trades())
        book = {'time': int((NOW-90)*1000),
                'levels': [[{'px': '.5', 'sz': '1000'}],
                           [{'px': '.5001', 'sz': '1000'}]]}
        scan.generation = 3
        scan.connected = True
        scan.book_received['#101'] = NOW-30  # old cached receipt
        scan.books['#101'] = (NOW-30, book)

        def refreshed(coin):
            scan.book_received[coin] = NOW
            scan.book_live.pop(coin, None)
            return book

        with patch('zeus.outcome_flow.time.time', return_value=NOW), \
                ThreadPoolExecutor(1) as pool, \
                patch.object(scan, '_book_executor', return_value=pool), \
                patch.object(scan, '_book', side_effect=refreshed):
            result = scan._snapshot('no', 110, 10, base_rate=0)
        self.assertTrue(result['ready'])
        self.assertEqual(result['candidates'][0]['market'], '#101')

    def test_observations_rank_admissible_markets_by_selection_order(self):
        scan = OutcomeFlowScanner()
        scan.connected, scan.connected_at, scan.catalog_at = True, NOW-90, NOW
        scan.rows = {10: dict(id=10, name='One', fee_scale=1, deployer_fee_scale=1),
                     20: dict(id=20, name='Two', fee_scale=1, deployer_fee_scale=1)}
        scan.subscribed = {'#100', '#101', '#200', '#201'}
        for coin in ('#101', '#201'):
            scan.history[coin].extend(trades())
        key = ('no', 110, 10, .0004, scan.generation, scan.catalog_revision)
        scan.snapshot_cache[key] = (0, dict(updated_at=NOW, candidates=[
            dict(market='#201', book_updated_at=NOW, last_trade_age_sec=25,
                 spread_bps=2, net_touch_edge_bps=-5, queue_proxy_sec=30, rank=1),
            dict(market='#101', book_updated_at=NOW, last_trade_age_sec=25,
                 spread_bps=2, net_touch_edge_bps=-5, queue_proxy_sec=40, rank=2),
        ]))
        with patch('zeus.outcome_flow.time.time', return_value=NOW):
            rows = scan.observations('no', 110, 10)['observed_markets']
        self.assertEqual([r['market'] for r in rows], ['#201', '#101'])
        self.assertEqual([r['rank'] for r in rows], [1, 2])

    def test_book_from_a_previous_connection_is_not_admitted(self):
        scan = self.scanner()
        scan.rows[10].update(fee_scale=1, deployer_fee_scale=1)
        scan.history['#101'].clear()
        scan.history['#101'].extend(trades())
        book = {'time': int(NOW*1000),
                'levels': [[{'px': '.5', 'sz': '1000'}],
                           [{'px': '.5001', 'sz': '1000'}]]}
        scan.generation = 4
        scan.connected = True
        scan.book_received['#101'] = NOW-20
        scan.book_live['#101'] = 3  # received before the reconnect
        scan.books['#101'] = (NOW-20, book)
        with patch('zeus.outcome_flow.time.time', return_value=NOW), \
                ThreadPoolExecutor(1) as pool, \
                patch.object(scan, '_book_executor', return_value=pool), \
                patch.object(scan, '_book', return_value=book):
            result = scan._snapshot('no', 110, 10, base_rate=0)
        self.assertFalse(result['ready'])

    def test_observations_explain_scanner_state_instead_of_blaming_the_book(self):
        book = {'time': int((NOW-20)*1000),
                'levels': [[{'px': '.5', 'sz': '1000'}],
                           [{'px': '.5001', 'sz': '1000'}]]}
        for stage, expected in (
                ('warmup', 'Observation initiale'),
                ('subscriptions', 'Souscriptions en cours'),
                ('ready', 'Scan en attente')):
            with self.subTest(stage=stage):
                scan = self.scanner()
                scan.history['#101'].clear()
                scan.history['#101'].extend(trades())
                scan.generation = 3
                scan.book_received['#101'] = NOW
                scan.book_live['#101'] = 3
                scan.books['#101'] = (NOW, book)
                if stage == 'warmup':
                    scan.connected_at = NOW-20
                elif stage == 'subscriptions':
                    scan.subscribed.remove('#100')
                with patch('zeus.outcome_flow.time.time', return_value=NOW):
                    row = scan.observations('no', 110, 10)['observed_markets'][0]
                self.assertIn(expected, row['eligibility_reason'])
                self.assertNotIn('Carnet à vérifier', row['eligibility_reason'])

    def test_rest_book_budget_prevents_api_bursts(self):
        scan = OutcomeFlowScanner()
        response = Mock()
        response.status_code = 200
        response.json.return_value = {
            'time': int(NOW*1000),
            'levels': [[{'px': '.5', 'sz': '100'}], [{'px': '.5001', 'sz': '100'}]],
        }
        with patch('zeus.outcome_flow.time.monotonic', return_value=100.0), \
                patch('zeus.outcome_flow._http') as http:
            http.return_value.post.return_value = response
            for index in range(6):
                scan._book(f'#{100+index}')
        self.assertEqual(http.return_value.post.call_count, 1)

    def test_rest_429_backs_off_before_the_next_book(self):
        scan = OutcomeFlowScanner()
        limited = Mock()
        limited.status_code = 429
        with patch('zeus.outcome_flow.time.monotonic', return_value=100.0), \
                patch('zeus.outcome_flow._http') as http:
            http.return_value.post.return_value = limited
            scan._book('#101')
            scan._book('#102')
        self.assertEqual(http.return_value.post.call_count, 1)
        self.assertGreater(scan.book_backoff_until, 100.1)
