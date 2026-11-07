"""Offline dashboard contracts: scope, rotation, uncertainty and shared reads."""
from concurrent.futures import ThreadPoolExecutor
import unittest

from zeus.dashboard_broker import DashboardBroker
from zeus.dashboard_state import outcome_projection


def worker(sid, *, started=990, running=True, group=None, volume=10, pnl=-.005):
    return dict(session_id=sid, started=started, running=running, auto_group_id=group,
                market="#11", config={"outcome_side": "no"}, phase="running" if running else "stopped",
                cleanup_confirmed=not running, accounting_complete=True, stale=False,
                volume_usd=volume, net_pnl=pnl, fees=.01, fills=2,
                duration_sec=50, position_known=True, position_usd=0)


class DashboardProjectionTests(unittest.TestCase):
    def test_old_history_never_changes_active_clock_or_totals(self):
        result = outcome_projection([worker("old", started=0, running=False, volume=9999),
                                     worker("new")], now=1000)
        self.assertEqual([b["session_id"] for b in result["display_bots"]], ["new"])
        self.assertEqual(result["active_metrics"]["volume_usd"], 10)
        self.assertEqual(result["active_metrics"]["duration"], "00:00:10")

    def test_rotation_is_one_session_with_all_executed_legs(self):
        old = worker("first", started=900, running=False, group="auto", volume=20)
        new = worker("second", started=990, group="auto", volume=30)
        result = outcome_projection([old, new], [dict(group_id="auto", current="second",
                                   state="observing", cancelled=False)], now=1000)
        row, = result["display_bots"]
        self.assertEqual((row["session_id"], row["worker_session_id"]), ("auto", "second"))
        self.assertEqual((row["volume_usd"], row["duration_sec"]), (50, 100))
        self.assertEqual(result["active_metrics"]["active_markets"], 1)

    def test_rotation_gap_stays_visible_without_claiming_quotes(self):
        result = outcome_projection([worker("first", running=False, group="auto")],
                   [dict(group_id="auto", current="first", state="observing", cancelled=False)], now=1000)
        row, = result["display_bots"]
        self.assertEqual(row["phase"], "rotating")
        self.assertTrue(row["running"])
        self.assertFalse(row["quoting_enabled"])

    def test_stale_live_values_are_unknown_instead_of_zero(self):
        entry = worker("live")
        entry.update(stale=True, position_known=False, position_usd=None)
        result = outcome_projection([entry], now=1000)
        self.assertIsNone(result["active_metrics"]["net_pnl"])
        self.assertIsNone(result["active_metrics"]["position_usd"])
        self.assertTrue(result["active_metrics"]["stale"])

    def test_empty_active_view_has_no_historical_duration(self):
        metrics = outcome_projection([worker("old", running=False)], now=1000)["active_metrics"]
        self.assertEqual(metrics["duration_sec"], 0)
        self.assertEqual(metrics["volume_usd"], 0)


class DashboardBrokerTests(unittest.TestCase):
    def test_concurrent_tabs_share_collection_and_cannot_mutate_cache(self):
        calls = []
        def collect():
            calls.append(1)
            return {"bots": [{"session_id": "a"}]}
        broker = DashboardBroker({"outcome": collect}, ttl=60)
        with ThreadPoolExecutor(max_workers=10) as pool:
            frames = list(pool.map(lambda _: broker.read("outcome"), range(40)))
        self.assertEqual(len(calls), 1)
        self.assertEqual({f["revision"] for f in frames}, {1})
        frames[0]["bots"].clear()
        self.assertEqual(len(broker.read("outcome")["bots"]), 1)
        broker.invalidate("outcome")
        self.assertEqual(broker.read("outcome")["revision"], 2)
        self.assertEqual(len(calls), 2)

    def test_source_locks_are_independent(self):
        broker = DashboardBroker({"grid": lambda: {}, "outcome": lambda: {}}, ttl=60)
        with broker.locks["grid"]:
            self.assertEqual(broker.read("outcome")["revision"], 1)


if __name__ == "__main__":
    unittest.main()
