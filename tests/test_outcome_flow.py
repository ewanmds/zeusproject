"""Offline flow, precision and admission checks. No live orders or credentials."""
from decimal import Decimal
import math
import threading
import time
import unittest
from unittest.mock import Mock, patch, AsyncMock

from zeus.outcome_flow import OutcomeFlowScanner, flow_metrics, before_event
from zeus.bots.worldcup import round_outcome_buy_px, round_outcome_sell_px


def tape(now, coin="#101", step=5, count=12, size=100):
    return [{"coin": coin, "time": int((now-i*step)*1000), "tid": i,
             "px": "0.34833", "sz": str(size), "side": "A" if i % 2 else "B"}
            for i in range(count)]


class OutcomePrecisionTests(unittest.TestCase):
    def test_touch_and_screenshot_prices_are_preserved(self):
        for price in (0.34833, 0.34834, 0.78717, 0.82804, 0.013457, 0.0012345, 0.99999, 1e-8):
            self.assertEqual(round_outcome_buy_px(price), price)
            self.assertEqual(round_outcome_sell_px(price), price)

    def test_rounding_direction_and_wire_limits(self):
        for price in (0.348335, 0.09999999, 0.10000003, 0.000123456, 0.999999):
            lower, upper = round_outcome_sell_px(price), round_outcome_buy_px(price)
            if price <= .99999:
                self.assertLessEqual(lower, price)
                self.assertGreaterEqual(upper, price)
            for value in (lower, upper):
                dec = Decimal(str(value)).normalize()
                self.assertLessEqual(len(dec.as_tuple().digits), 5)
                self.assertGreaterEqual(dec.as_tuple().exponent, -8)
                self.assertTrue(0 < value < 1)
        for bad in (0, -1, math.inf, math.nan):
            with self.assertRaises(ValueError):
                round_outcome_buy_px(bad)


class OutcomeFlowTests(unittest.TestCase):
    def test_regular_two_way_flow_is_eligible_and_duplicates_do_not_inflate(self):
        trades = tape(1000)
        a, b = flow_metrics(trades, 1000), flow_metrics(trades*4, 1000)
        self.assertTrue(a["eligible"])
        self.assertEqual(a, b)
        self.assertEqual(a["events_5m"], 12)

    def test_one_timestamp_burst_one_way_and_stale_are_ineligible(self):
        for trades in (tape(1000, step=0), [{**t, "side": "A"} for t in tape(1000)], tape(975)):
            self.assertFalse(flow_metrics(trades, 1000)["eligible"])
        self.assertIsNone(flow_metrics(tape(500), 1000))

    def test_opposing_flow_is_required_even_with_large_volume(self):
        large = [{**t, "side": "A", "sz": "100000"} for t in tape(1000)]
        self.assertGreater(flow_metrics(tape(1000), 1000)["score"], flow_metrics(large, 1000)["score"])

    def test_missing_and_nonfinite_trades_do_not_poison_rankings(self):
        self.assertIsNone(flow_metrics([{}, {"px": "nan", "sz": 1, "time": 1000000, "side": "A"}], 1000))

    def test_sports_children_require_parent_schedule_and_expiry_is_checked(self):
        now = 1794058200
        child = {"category": "sports", "description": "participant:X"}
        self.assertFalse(before_event(child, now))
        self.assertTrue(before_event({**child, "question_description": "scheduledStart:20261109-0000"}, now))
        self.assertFalse(before_event({**child, "question_description": "scheduledStart:20261107-0000"}, now))
        self.assertFalse(before_event({"description": "expiry:broken"}, now))

    def scanner(self):
        now = time.time()
        scanner = OutcomeFlowScanner()
        scanner.connected = True
        scanner.connected_at = now-90
        scanner.catalog_at = now
        scanner.rows = {10: {"id": 10, "name": "Regular", "category": "price"},
                        20: {"id": 20, "name": "Large queue", "category": "price"}}
        scanner.subscribed = {"#100", "#101", "#200", "#201"}
        scanner.ingest(tape(now, "#101") + tape(now, "#201"))
        return scanner

    def book(self, coin, queue=100, spread=.00001, age=0):
        return {"coin": coin, "time": (time.time()-age)*1000, "levels": [
            [{"px": ".34833", "sz": str(queue)}],
            [{"px": str(.34833+spread), "sz": str(queue)}]]}

    def test_queue_depth_changes_ranking_and_preserves_selected_side(self):
        scanner = self.scanner()
        scanner._book = lambda c: self.book(c, 100 if c == "#101" else 10000)
        snap = scanner.snapshot("no", 10, 10)
        self.assertTrue(snap["ready"])
        self.assertEqual(snap["candidates"][0]["market"], "#101")
        self.assertFalse(scanner.snapshot("yes", 10, 10)["ready"])

    def test_shorter_queue_ranks_ahead_of_a_higher_volume_market(self):
        scanner = self.scanner()
        scanner.history["#201"].clear()
        scanner.ingest(tape(time.time(), "#201", size=300))
        scanner._book = lambda c: self.book(c, 100 if c == "#101" else 10000)
        snap = scanner.snapshot("no", 10, 10)
        self.assertEqual(snap["ranking"], "queue_proxy_sec")
        # Fills first: a deep queue loses even when its public $/min is higher.
        self.assertEqual(snap["candidates"][0]["market"], "#101")
        self.assertGreater(snap["candidates"][1]["volume_usd_1m"],
                           snap["candidates"][0]["volume_usd_1m"])
        self.assertGreater(snap["candidates"][1]["queue_proxy_sec"],
                           snap["candidates"][0]["queue_proxy_sec"])

    def test_auto_defaults_use_documented_window_and_shorter_cooldown(self):
        from zeus.server import WorldcupStartRequest
        request = WorldcupStartRequest(selection_mode="auto", exec_mode="maker")
        self.assertEqual(request.auto_rotation_after_sec, 180)
        self.assertEqual(request.auto_market_cooldown_sec, 300)

    def test_stale_disconnected_warming_wide_and_thin_fail_closed(self):
        for kind in ("disconnected", "warming", "catalog", "book", "wide", "thin", "subscriptions"):
            with self.subTest(kind=kind):
                scanner = self.scanner()
                scanner._book = lambda c: self.book(c)
                if kind == "disconnected": scanner.connected = False
                if kind == "warming": scanner.connected_at = time.time()
                if kind == "catalog": scanner.catalog_at = time.time()-200
                if kind == "book": scanner._book = lambda c: self.book(c, age=10)
                if kind == "wide": scanner._book = lambda c: self.book(c, spread=.01)
                if kind == "thin": scanner._book = lambda c: self.book(c, queue=1)
                if kind == "subscriptions": scanner.subscribed.clear()
                self.assertFalse(scanner.snapshot("no", 10, 10)["ready"])

    def test_reconnect_during_book_reads_discards_result(self):
        scanner = self.scanner()
        def book(coin):
            scanner.generation += 1
            return self.book(coin)
        scanner._book = book
        self.assertFalse(scanner.snapshot("no", 10, 10)["ready"])

    def test_catalog_updates_keep_valid_observation_and_drop_expired_markets(self):
        scanner = self.scanner()
        scanner.thread = Mock()
        scanner.thread.is_alive.return_value = True
        scanner.ws = Mock()
        connected_at = scanner.connected_at
        scanner.configure([scanner.rows[10], {"id": 30, "name": "Expired",
                          "description": "expiry:20200101-0000"}])
        self.assertEqual(set(scanner.rows), {10})
        self.assertIn("#101", scanner.history)
        self.assertNotIn("#201", scanner.history)
        self.assertEqual(scanner.connected_at, connected_at)
        scanner.ws.close.assert_not_called()

    def test_auto_rejects_taker_and_no_flow_before_resolving_or_spawning(self):
        from zeus.outcome_supervisor import OutcomeSupervisor
        from zeus.server import WorldcupStartRequest
        supervisor = OutcomeSupervisor(Mock(state_dir=None))
        with patch("zeus.outcomes.resolve_market") as resolve, patch("zeus.outcome_flow.scanner.snapshot", return_value={"ready": False, "candidates": []}):
            supervisor.status = lambda: {"bots": []}
            for mode in ("taker", "maker"):
                with self.assertRaises(ValueError):
                    supervisor.start(WorldcupStartRequest(selection_mode="auto", exec_mode=mode))
            resolve.assert_not_called()

    def test_preview_endpoint_uses_public_scanner_and_excludes_busy_market(self):
        from zeus import server
        from fastapi.testclient import TestClient
        server.app.dependency_overrides[server._require_api_token] = lambda: None
        try:
            with patch.object(server.public_catalogs, "snapshot", new=AsyncMock(return_value={"outcomes": [], "catalog_stale": False})), \
                 patch("zeus.outcome_flow.scanner.configure") as configure, \
                 patch("zeus.outcome_flow.scanner.snapshot", return_value={"ready": True, "candidates": [{"market": "#101"}]}), \
                 patch.object(server.manager, "get_worldcup_stats", return_value={"bots": [{"market": "#101", "running": True}]}):
                client = TestClient(server.app)
                response = client.get("/api/outcomes/auto?side=no&order_usd=10")
                self.assertEqual(response.status_code, 200)
                self.assertFalse(response.json()["ready"])
                self.assertEqual(response.json()["candidates"], [])
                configure.assert_called_once()
                self.assertEqual(client.get("/api/outcomes/auto?order_usd=nan").status_code, 422)
                self.assertEqual(client.get("/api/outcomes/auto?side=both").status_code, 422)
        finally:
            server.app.dependency_overrides.pop(server._require_api_token, None)


if __name__ == "__main__":
    unittest.main()
