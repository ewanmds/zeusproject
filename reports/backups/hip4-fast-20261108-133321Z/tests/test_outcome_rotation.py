"""No real accounts/processes: low fills -> drain -> flat confirmation -> reselect."""
from collections import deque
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from zeus.outcome_rotation import OutcomeRotation


class RotationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = dict(selection_mode="auto", exec_mode="maker", outcome_side="no",
            market="#101", order_size_usd=10, max_position_usd=200, auto_max_spread_bps=10,
            auto_rotation_after_sec=180, maker_spread_pct=None)
        self.current = dict(session_id="old", market="#101", running=True, phase="running",
                            stale=False, cleanup_confirmed=False, volume_usd=0)
        self.owner = SimpleNamespace(status=Mock(side_effect=lambda: {"bots": [self.current]}),
            procs={"old": {"stop_file": str(Path(self.temp.name)/"stop")}}, start=Mock())
        self.rotation = OutcomeRotation(self.owner)
        self.group = dict(id="group", current="old", config=self.config, started=800,
            state="observing", samples=deque([(820, 0)]), members={"old"},
            cancelled=False, rotations=0, reason="", retry_at=0)
        self.rotation.groups["group"] = self.group
        self.snapshot = {"ready": True, "updated_at": 1000, "candidates": [
            {"market": "#201", "volume_usd_1m": 1000}, {"market": "#101", "volume_usd_1m": 100}]}
        self.scanner = patch("zeus.outcome_flow.scanner.snapshot", return_value=self.snapshot)
        self.scan = self.scanner.start()

    def tearDown(self):
        self.rotation.close()
        self.scanner.stop()
        self.temp.cleanup()

    def test_low_fills_request_passive_drain_before_any_new_launch(self):
        self.rotation.tick(1000)
        self.assertEqual(self.group["state"], "draining")
        self.assertTrue((Path(self.temp.name)/"drain.json").exists())
        self.owner.start.assert_not_called()
        self.rotation.tick(1005)
        self.owner.start.assert_not_called()  # Even a flat snapshot is insufficient while alive.

    def test_one_minute_underfilling_window_drains_without_waiting_three_minutes(self):
        self.config['auto_rotation_after_sec'] = 60
        self.group.update(started=940, samples=deque([(945, 0)]))
        self.rotation.tick(1000)
        self.assertEqual(self.group['state'], 'draining')
        self.owner.start.assert_not_called()

    def test_one_minute_window_does_not_rotate_before_observation_finishes(self):
        self.config['auto_rotation_after_sec'] = 60
        self.group.update(started=950, samples=deque([(950, 0)]))
        self.rotation.tick(1000)
        self.assertEqual(self.group['state'], 'observing')
        self.scan.assert_not_called()

    def test_verified_dead_flat_worker_allows_new_top_with_identical_budgets_and_side(self):
        self.group["state"] = "draining"
        self.current.update(running=False, cleanup_confirmed=True)
        self.rotation.tick(1000)
        request = self.owner.start.call_args.args[0]
        self.assertEqual(request.market, "#201")
        self.assertTrue(request.two_sided)
        self.assertEqual((request.order_size_usd, request.max_position_usd, request.outcome_side), (10, 200, "no"))
        self.assertEqual(self.owner.start.call_args.kwargs["rotation_group"], "group")

    def test_unconfirmed_stop_or_stale_stats_cannot_rotate(self):
        self.group["state"] = "draining"
        self.current.update(running=False, cleanup_confirmed=False)
        self.rotation.tick(1000)
        self.assertEqual(self.group["state"], "blocked")
        self.owner.start.assert_not_called()
        self.group["state"] = "observing"
        self.current.update(running=True, stale=True)
        self.rotation.tick(1000)
        self.scan.assert_not_called()

    def test_failed_session_cannot_auto_resume_even_when_flat(self):
        self.group["state"] = "draining"
        self.current.update(running=False, cleanup_confirmed=True, phase="failed", error="session stop loss")
        self.rotation.tick(1000)
        self.assertEqual(self.group["state"], "blocked")
        self.owner.start.assert_not_called()

    def test_old_session_pnl_is_carried_once_across_failed_replacement_attempts(self):
        self.group["state"] = "draining"
        self.current.update(running=False, cleanup_confirmed=True, phase="stopped", net_pnl=-3)
        self.owner.start.side_effect = RuntimeError("No flow")
        self.rotation.tick(1000)
        self.rotation.tick(1020)
        self.assertEqual(self.group["pnl_carry"], -3)

    def test_sufficient_real_executed_volume_keeps_existing_queues(self):
        self.current["volume_usd"] = 60
        self.rotation.tick(1000)
        self.scan.assert_not_called()
        self.assertFalse((Path(self.temp.name)/"drain.json").exists())

    def test_underfilling_current_top_is_skipped_for_next_highest_eligible_market(self):
        self.snapshot["candidates"].reverse()
        self.rotation.tick(1000)
        self.assertEqual(self.group["state"], "draining")
        self.assertFalse(self.rotation.candidate_allowed("group", "#101", 1001))

    def test_no_switch_without_an_alternative_or_during_market_cooldown(self):
        self.group["cooldown"] = {"#201": 2000}
        self.rotation.tick(1000)
        self.assertEqual(self.group["state"], "observing")
        self.assertIn("waiting for another", self.group["reason"])

    def test_stop_during_slow_public_scan_prevents_drain_and_later_launch(self):
        def stop_while_scanning(*args):
            self.rotation.cancel("old")
            return self.snapshot
        self.scan.side_effect = stop_while_scanning
        self.rotation.tick(1000)
        self.assertFalse((Path(self.temp.name)/"drain.json").exists())
        self.owner.start.assert_not_called()
        with self.assertRaises(RuntimeError):
            self.rotation.assert_admission("group")

    def test_stale_scanner_is_not_used_to_move_money(self):
        self.snapshot["updated_at"] = 900
        self.rotation.tick(1000)
        self.assertFalse((Path(self.temp.name)/"drain.json").exists())
        self.owner.start.assert_not_called()

    def test_stop_old_session_cancels_current_replacement(self):
        self.group.update(current="new", members={"old", "new"})
        self.assertEqual(self.rotation.cancel("old"), {"new"})
        self.assertTrue(self.group["cancelled"])


if __name__ == "__main__":
    unittest.main()
