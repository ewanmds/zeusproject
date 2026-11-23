"""Offline Outcome lifecycle regressions: Stop, rotation, worker, resume, scanner.

No test here opens a socket, spawns a real worker, signs, or touches an
exchange/account. Every process is a fake and every filesystem lives in a
temporary directory.
"""
from concurrent.futures import ThreadPoolExecutor
import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from zeus.bots.outcome_worker import build_config, run_worker
from zeus.dashboard import outcome_projection
from zeus.outcome_flow import OutcomeFlowScanner
from zeus.outcome_rotation import OutcomeRotation
from zeus.outcome_supervisor import OutcomeSupervisor


def _supervisor():
    sup = OutcomeSupervisor.__new__(OutcomeSupervisor)
    sup.manager = SimpleNamespace(lock=threading.RLock())
    sup.procs = {}
    sup.cache = {}
    sup.records = lambda: []
    sup.rotation = SimpleNamespace(cancel=lambda sid: [], describe=lambda sid: {})
    return sup


class AsyncStopTests(unittest.TestCase):
    def test_stop_persists_manual_maker_exit_and_returns_without_waiting(self):
        with tempfile.TemporaryDirectory() as folder:
            stop_file = Path(folder) / "stop"
            sup = _supervisor()
            cancel = Mock(return_value=[])
            sup.rotation = SimpleNamespace(cancel=cancel)
            # A live process that never exits must not block the HTTP response.
            sup.procs = {"s1": {"session_id": "s1", "stop_file": str(stop_file),
                                "safety_file": str(Path(folder) / "journal.json"),
                                "market": "#101",
                                "proc": SimpleNamespace(poll=lambda: None)}}
            started = time.monotonic()
            self.assertEqual(sup.stop("s1"), ["s1"])
            self.assertLess(time.monotonic() - started, 1.0)
            cancel.assert_called_once_with("s1")
            drain = json.loads((Path(folder) / "drain.json").read_text())
            self.assertEqual(drain["reason"], "Manual stop: maker exit")
            self.assertTrue(drain["manual_stop"])
            self.assertTrue(drain["manual"])
            self.assertTrue(drain["session_finished"])
            self.assertFalse(stop_file.exists())

    def test_repeated_manual_stop_is_idempotent_and_never_touches_hard_sentinel(self):
        with tempfile.TemporaryDirectory() as folder:
            stop_file = Path(folder) / "stop"
            sup = _supervisor()
            sup.procs = {"s1": {"session_id": "s1", "stop_file": str(stop_file),
                                "safety_file": str(Path(folder) / "journal.json"),
                                "market": "#101",
                                "proc": SimpleNamespace(poll=lambda: None)}}
            self.assertEqual(sup.stop("s1"), ["s1"])
            self.assertEqual(sup.stop("s1"), ["s1"])
            drain = json.loads((Path(folder) / "drain.json").read_text())
            self.assertEqual(drain["reason"], "Manual stop: maker exit")
            self.assertTrue(drain["manual_stop"])
            self.assertFalse(stop_file.exists())

    def test_archived_confirmed_member_still_drains_active_descendant(self):
        with tempfile.TemporaryDirectory() as folder:
            stop_file = Path(folder) / "descendant-stop"
            sup = _supervisor()
            sup.procs = {"child": {"session_id": "child", "stop_file": str(stop_file),
                                   "safety_file": str(Path(folder) / "child.json"),
                                   "market": "#102", "config": {"auto_group_id": "group"},
                                   "proc": SimpleNamespace(poll=lambda: None)}}
            sup.records = lambda: [
                ("old.json", {"session_id": "old", "cleanup_confirmed": True,
                              "phase": "stopped", "symbols": ["#101"],
                              "config": {"auto_group_id": "group"}}),
                ("child.json", {"session_id": "child", "cleanup_confirmed": False,
                                "phase": "running", "symbols": ["#102"],
                                "config": {"auto_group_id": "group"}}),
            ]
            self.assertEqual(sup.stop("old"), ["child"])
            self.assertTrue((Path(folder) / "drain.json").exists())
            self.assertFalse(stop_file.exists())

    def test_stop_all_records_every_unconfirmed_journal_it_requests(self):
        with tempfile.TemporaryDirectory() as folder:
            sup = _supervisor()
            sup.records = lambda: [
                ("dead.json", {"session_id": "dead", "cleanup_confirmed": False,
                               "phase": "failed", "symbols": ["#101"],
                               "stop_file": str(Path(folder) / "dead-stop"),
                               "config": {}}),
            ]
            self.assertEqual(sup.stop(None), ["dead"])
            self.assertTrue((Path(folder) / "drain.json").exists())


class RotationStopTests(unittest.TestCase):
    def test_cancel_is_atomic_and_prevents_any_replacement(self):
        row = {"session_id": "one", "running": True, "cleanup_confirmed": False,
               "phase": "running", "market": "#101", "volume_usd": 0, "fills": 0}
        owner = SimpleNamespace(procs={"one": {"stop_file": "unused"}},
                                status=Mock(return_value={"bots": [row]}),
                                start=Mock())
        rotation = OutcomeRotation(owner)
        with patch.object(rotation, "_loop"):
            rotation.register({"session_id": "one"},
                              {"selection_mode": "auto", "auto_rotate": True,
                               "order_size_usd": 15})
        self.assertEqual(rotation.cancel("one"), {"one"})
        with patch("zeus.outcome_flow.scanner.snapshot") as scan:
            rotation.tick()
            scan.assert_not_called()
        owner.start.assert_not_called()
        rotation.close()

    def test_user_stop_on_a_risk_drain_still_never_touches_hard_sentinel(self):
        with tempfile.TemporaryDirectory() as folder:
            stop_file = Path(folder) / "stop"
            (Path(folder) / "drain.json").write_text(json.dumps(
                {"requested_at": 1, "reason": "Session stop loss: maker exit"}))
            sup = _supervisor()
            sup.procs = {"s1": {"session_id": "s1", "stop_file": str(stop_file),
                                "safety_file": str(Path(folder) / "journal.json"),
                                "market": "#101",
                                "proc": SimpleNamespace(poll=lambda: None)}}
            self.assertEqual(sup.stop("s1"), ["s1"])
            drain = json.loads((Path(folder) / "drain.json").read_text())
            self.assertEqual(drain["reason"], "Manual stop: maker exit")
            self.assertTrue(drain["manual_stop"])
            self.assertFalse(stop_file.exists())

    def test_stop_of_group_card_cancels_the_rotation_chain(self):
        with tempfile.TemporaryDirectory() as folder:
            sup = _supervisor()
            rotation = OutcomeRotation(sup)
            with patch.object(rotation, "_loop"):
                rotation.register({"session_id": "child"},
                                  {"selection_mode": "auto", "auto_rotate": True,
                                   "order_size_usd": 15}, group_id="group")
            sup.rotation = rotation
            stop_file = Path(folder) / "stop"
            sup.procs = {"child": {"session_id": "child", "stop_file": str(stop_file),
                                   "safety_file": str(Path(folder) / "child.json"),
                                   "market": "#101", "proc": SimpleNamespace(poll=lambda: None)}}
            sup.stop("group")
            self.assertTrue(rotation.groups["group"]["cancelled"])
            self.assertTrue((Path(folder) / "drain.json").exists())
            rotation.close()


class FlatOnlyRotationTests(unittest.TestCase):
    def rotation(self, folder, *, position, candidates, current_queue=200.0, current_volume=100):
        stop_file = Path(folder) / "stop"
        row = {"session_id": "one", "running": True, "cleanup_confirmed": False,
               "phase": "running", "market": "#101", "volume_usd": 0, "fills": 0,
               "position_usd": position, "stale": False, "last_fill_at": 0,
               "execution_state": "Quoting"}
        owner = SimpleNamespace(
            procs={"one": {"stop_file": str(stop_file)}},
            status=Mock(return_value={"bots": [row]}), start=Mock())
        rotation = OutcomeRotation(owner)
        with patch.object(rotation, "_loop"):
            rotation.register({"session_id": "one"},
                              {"selection_mode": "auto", "auto_rotate": True,
                               "order_size_usd": 15, "auto_rotation_after_sec": 180,
                               "auto_max_spread_bps": 10, "outcome_side": "no",
                               "auto_selection": {"queue_proxy_sec": current_queue,
                                                  "volume_usd_1m": current_volume}},
                              group_id="group")
        group = rotation.groups["group"]
        group["started"] = time.time() - 240
        group["samples"].append((group["started"], 0.0))
        return rotation, stop_file

    def snapshot(self, candidates, at=None):
        return {"ready": bool(candidates), "candidates": candidates,
                "updated_at": at if at is not None else time.time()}

    def test_inventory_present_never_starts_a_liquidation(self):
        with tempfile.TemporaryDirectory() as folder:
            rotation, stop_file = self.rotation(
                folder, position=12,
                candidates=[{"market": "#102", "queue_proxy_sec": 100}])
            with patch("zeus.outcome_flow.scanner.snapshot",
                       side_effect=lambda *a, **k: self.snapshot(
                           [{"market": "#102", "queue_proxy_sec": 100}])):
                rotation.tick()
            self.assertFalse((stop_file.parent / "drain.json").exists())
            self.assertEqual(rotation.groups["group"]["state"], "observing")
            self.assertIn("inventaire", rotation.groups["group"]["reason"])
            rotation.close()

    def test_flat_candidate_must_settle_30s_and_offer_more_volume_even_with_slower_queue(self):
        with tempfile.TemporaryDirectory() as folder:
            rotation, stop_file = self.rotation(
                folder, position=0,
                candidates=[{"market": "#102", "queue_proxy_sec": 1000, "volume_usd_1m": 150}])
            now = time.time()
            with patch("zeus.outcome_flow.scanner.snapshot",
                       side_effect=lambda *a, **k: self.snapshot(
                           [{"market": "#102", "queue_proxy_sec": 1000, "volume_usd_1m": 150}], at=now + 31)):
                rotation.tick(now=now)
                self.assertFalse((stop_file.parent / "drain.json").exists())
                self.assertEqual(rotation.groups["group"]["reason"],
                                 "Rotation: candidat en observation")
                rotation.tick(now=now + 31)
            self.assertTrue((stop_file.parent / "drain.json").exists())
            self.assertEqual(rotation.groups["group"]["state"], "draining")
            rotation.close()

    def test_flat_candidate_without_25_percent_gain_is_ignored(self):
        with tempfile.TemporaryDirectory() as folder:
            rotation, stop_file = self.rotation(
                folder, position=0,
                candidates=[{"market": "#102", "queue_proxy_sec": 10, "volume_usd_1m": 110}])
            now = time.time()
            with patch("zeus.outcome_flow.scanner.snapshot",
                       side_effect=lambda *a, **k: self.snapshot(
                           [{"market": "#102", "queue_proxy_sec": 10, "volume_usd_1m": 110}], at=now + 31)):
                rotation.tick(now=now)
                rotation.tick(now=now + 31)
            self.assertFalse((stop_file.parent / "drain.json").exists())
            self.assertIn("25%", rotation.groups["group"]["reason"])
            rotation.close()

    def test_rotation_compares_current_live_volume_instead_of_old_entry_volume(self):
        with tempfile.TemporaryDirectory() as folder:
            rotation, stop_file = self.rotation(folder, position=0, candidates=[])
            now = time.time()
            candidates = [
                {'market': '#101', 'volume_usd_1m': 1000},
                {'market': '#102', 'volume_usd_1m': 150, 'queue_proxy_sec': 1},
            ]
            with patch('zeus.outcome_flow.scanner.snapshot', return_value=self.snapshot(candidates, at=now+31)):
                rotation.tick(now=now)
                rotation.tick(now=now+31)
            self.assertFalse((stop_file.parent / 'drain.json').exists())
            self.assertIn('25%', rotation.groups['group']['reason'])
            rotation.close()

    def test_no_candidate_keeps_the_session_active_waiting(self):
        with tempfile.TemporaryDirectory() as folder:
            rotation, stop_file = self.rotation(folder, position=0, candidates=[])
            with patch("zeus.outcome_flow.scanner.snapshot",
                       side_effect=lambda *a, **k: self.snapshot([])):
                rotation.tick()
            self.assertFalse((stop_file.parent / "drain.json").exists())
            self.assertEqual(rotation.groups["group"]["state"], "observing")
            self.assertIn("waiting", rotation.groups["group"]["reason"])
            rotation.close()


class WorkerLifecycleTests(unittest.TestCase):
    def fake_bot(self):
        class Safety:
            def __init__(self):
                self.cleanup_confirmed = True
                self.failures = []

            def snapshot(self):
                return {"phase": "running", "cleanup_confirmed": self.cleanup_confirmed,
                        "position_known": True, "exit_requested": False, "stop_reason": ""}

            def fail(self, error):
                self.failures.append(str(error))

        class Bot:
            def __init__(self, cfg):
                self.cfg, self.safety = cfg, Safety()
                self.exit_reasons, self.stopped, self.ran = [], False, False
                self.wake = threading.Event()

            def request_maker_exit(self, reason):
                self.exit_reasons.append(reason)

            def request_stop(self):
                self.stopped = True

            def export_dashboard_stats(self):
                return {"volume_usd": 0, "net_pnl": 0, "fees": 0, "fills": 0,
                        "duration_sec": 0, "position_usd": 0}

            def run(self):
                self.ran = True

            def shutdown(self):
                pass

        return Bot

    def runtime(self, folder, **extra):
        runtime = Path(folder) / "runtime" / "s1"
        runtime.mkdir(parents=True)
        journal = runtime / "journal.json"
        journal.write_text(json.dumps({"session_id": "s1", "symbols": ["#101"],
                                       "account_kind": "outcome",
                                       "cleanup_confirmed": False}))
        public = {"market": "#101", "order_size_usd": 15, "max_position_usd": 200,
                  "exec_mode": "maker", "selection_mode": "manual"}
        config = runtime / "config.json"
        config.write_text(json.dumps({"config": public, "safety_file": str(journal),
                                      "funds_root": str(Path(folder) / "funds"),
                                      "locks_root": str(Path(folder) / "instances")}))
        return runtime, config, journal

    def test_drain_observed_before_run_requests_maker_exit(self):
        with tempfile.TemporaryDirectory() as folder:
            runtime, config, _ = self.runtime(folder)
            (runtime / "drain.json").write_text(json.dumps(
                {"reason": "Manual stop: maker exit", "session_finished": True}))
            created = []

            def factory(cfg):
                bot = self.fake_bot()(cfg)
                created.append(bot)
                return bot

            with patch.dict(os.environ, {"WALLET_ADDRESS_OUTCOME": "0x" + "1" * 40,
                                         "PRIVATE_KEY_OUTCOME": "offline-test"}):
                code = run_worker(config, bot_factory=factory)
            self.assertEqual(code, 0)
            self.assertTrue(created[0].ran)
            self.assertIn("Manual stop: maker exit", created[0].exit_reasons)
            self.assertFalse(created[0].stopped)

    def test_stop_during_startup_never_enters_the_trading_run(self):
        with tempfile.TemporaryDirectory() as folder:
            runtime, config, _ = self.runtime(folder)
            (runtime / "stop").write_text("")
            created = []

            def factory(cfg):
                bot = self.fake_bot()(cfg)
                created.append(bot)
                return bot

            with patch.dict(os.environ, {"WALLET_ADDRESS_OUTCOME": "0x" + "1" * 40,
                                         "PRIVATE_KEY_OUTCOME": "offline-test"}):
                run_worker(config, bot_factory=factory)
            self.assertFalse(created[0].ran)
            self.assertTrue(created[0].stopped)

    def test_transient_telemetry_permission_error_is_not_a_fatal_stop(self):
        with tempfile.TemporaryDirectory() as folder:
            runtime, config, _ = self.runtime(folder)
            created = []
            original = self.fake_bot()

            class SlowBot(original):
                def run(self):
                    self.ran = True
                    time.sleep(0.6)

            class FailingTelemetry:
                def __init__(self, path):
                    self.path = path

                def write(self, data):
                    raise PermissionError("stats file temporarily locked")

                def final(self, data, timeout=2):
                    raise PermissionError("stats file temporarily locked")

            def factory(cfg):
                bot = SlowBot(cfg)
                created.append(bot)
                return bot

            with patch.dict(os.environ, {"WALLET_ADDRESS_OUTCOME": "0x" + "1" * 40,
                                         "PRIVATE_KEY_OUTCOME": "offline-test"}), \
                 patch("zeus.bots.outcome_worker.OutcomeTelemetry", FailingTelemetry):
                run_worker(config, bot_factory=factory)
            self.assertTrue(created[0].ran)
            self.assertEqual(created[0].safety.failures, [])


class EntryDrainTests(unittest.TestCase):
    def test_drain_file_is_visible_before_the_engine_publishes_it(self):
        with tempfile.TemporaryDirectory() as folder:
            stop_file = Path(folder) / "stop"
            (Path(folder) / "drain.json").write_text(json.dumps(
                {"reason": "Manual stop: maker exit", "session_finished": True}))
            sup = _supervisor()
            sup._read_stats = lambda path: {"session_id": "s1", "market": "#101",
                                            "ts": time.time(), "position_usd": 0}
            record = {"session_id": "s1", "symbols": ["#101"], "config": {},
                      "phase": "running", "started": time.time() - 20,
                      "cleanup_confirmed": False, "stats_file": "stats.json",
                      "stop_file": str(stop_file), "pid": 1}
            info = {"proc": SimpleNamespace(pid=2, poll=lambda: None)}
            entry = sup._entry("journal", record, info)
            self.assertEqual(entry["phase"], "draining")
            self.assertFalse(entry["quoting_enabled"])
            self.assertTrue(entry["exit_requested"])
            self.assertEqual(entry["stop_reason"], "Manual stop: maker exit")
            self.assertFalse(entry["stale"])
            self.assertFalse(entry["accounting_complete"])

    def test_stale_draining_keeps_stale_and_unknown_accounting_truthful(self):
        with tempfile.TemporaryDirectory() as folder:
            stop_file = Path(folder) / "stop"
            (Path(folder) / "drain.json").write_text(json.dumps(
                {"reason": "Manual stop: maker exit"}))
            sup = _supervisor()
            sup._read_stats = lambda path: {"session_id": "s1", "market": "#101",
                                            "ts": time.time() - 30, "position_usd": 0}
            record = {"session_id": "s1", "symbols": ["#101"], "config": {},
                      "phase": "running", "started": time.time() - 60,
                      "cleanup_confirmed": False, "stats_file": "stats.json",
                      "stop_file": str(stop_file), "pid": 1}
            entry = sup._entry("journal", record,
                               {"proc": SimpleNamespace(pid=2, poll=lambda: None)})
            self.assertEqual(entry["phase"], "draining")
            self.assertTrue(entry["stale"])
            self.assertFalse(entry["quoting_enabled"])
            self.assertFalse(entry["accounting_complete"])


class ResumeTests(unittest.TestCase):
    def record(self, journal, **overrides):
        row = {"session_id": "s1", "symbols": ["#101"], "account_kind": "outcome",
               "cleanup_confirmed": False, "phase": "failed",
               "config": {"market": "#101", "outcome_side": "no",
                          "order_size_usd": 15, "max_position_usd": 200,
                          "exec_mode": "maker", "selection_mode": "manual"},
               "stop_file": str(Path(journal).with_name("stop"))}
        row.update(overrides)
        return row

    def journal(self, folder, **overrides):
        path = Path(folder) / "journal.json"
        data = {"session_id": "s1", "exposure_started": True,
                "owned_oids": [7], "cloids": ["0xabc"], "pending_cloids": ["0xdef"],
                "symbols": ["#101"]}
        data.update(overrides)
        path.write_text(json.dumps(data))
        return path

    def test_resume_exit_reuses_attribution_and_persists_exit_intent(self):
        with tempfile.TemporaryDirectory() as folder:
            journal = self.journal(folder)
            sup = _supervisor()
            sup.root = Path(folder)
            sup.manager.account_context = SimpleNamespace(account="0x" + "1" * 40)
            sup.records = lambda: [(str(journal), self.record(journal, account="0x" + "1" * 40))]
            sup.start = Mock(return_value={"session_id": "new"})
            result = sup.resume_exit("s1")
            self.assertEqual(result, {"session_id": "new"})
            request = sup.start.call_args.args[0]
            self.assertEqual(request.exec_mode, "maker")
            self.assertTrue(request.resume_exit)
            self.assertFalse(request.auto_rotate)
            seed = sup.start.call_args.kwargs["seed"]
            self.assertEqual(seed["resume_from"], "s1")
            self.assertTrue(seed["exposure_started"])
            self.assertEqual(seed["owned_oids"], [7])
            self.assertEqual(seed["cloids"], ["0xabc"])
            self.assertEqual(seed["pending_cloids"], ["0xdef"])
            drain = json.loads((Path(folder) / "runtime" / "new" / "drain.json").read_text())
            self.assertTrue(drain["session_finished"])

    def test_resume_rejects_confirmed_live_orphan_and_unattributed_sessions(self):
        with tempfile.TemporaryDirectory() as folder:
            journal = self.journal(folder)
            # Already verified: nothing to resume.
            sup = _supervisor()
            sup.records = lambda: [(str(journal), self.record(journal, cleanup_confirmed=True))]
            with self.assertRaises(ValueError):
                sup.resume_exit("s1")
            # A live worker PID is an orphan, not a dead session.
            sup = _supervisor()
            sup.records = lambda: [(str(journal), self.record(journal, pid=os.getpid()))]
            with self.assertRaises(RuntimeError):
                sup.resume_exit("s1")
            # Unknown persisted state is refused.
            sup = _supervisor()
            sup.records = lambda: [(str(journal), self.record(journal, phase="mystery"))]
            with self.assertRaises(ValueError):
                sup.resume_exit("s1")
            # No provenance means pre-existing/unattributed inventory.
            unattributed = self.journal(folder, exposure_started=False)
            sup = _supervisor()
            sup.records = lambda: [(str(unattributed), self.record(unattributed))]
            with self.assertRaises(ValueError):
                sup.resume_exit("s1")

    def test_resume_rejects_a_mismatched_account_or_journal(self):
        with tempfile.TemporaryDirectory() as folder:
            journal = self.journal(folder)
            sup = _supervisor()
            sup.manager.account_context = SimpleNamespace(account="0x" + "2" * 40)
            sup.records = lambda: [(str(journal), self.record(
                journal, account="0x" + "1" * 40))]
            with self.assertRaises(ValueError):
                sup.resume_exit("s1")

            mismatch = self.journal(folder, session_id="other")
            sup = _supervisor()
            sup.manager.account_context = SimpleNamespace(account="0x" + "1" * 40)
            sup.records = lambda: [(str(mismatch), self.record(
                mismatch, account="0x" + "1" * 40))]
            with self.assertRaises(ValueError):
                sup.resume_exit("s1")


class GroupProjectionTests(unittest.TestCase):
    def test_projection_never_hides_an_unresolved_older_leg(self):
        entries = [
            {"session_id": "old", "market": "#101", "config": {"auto_group_id": "g"},
             "started": 1.0, "running": False, "cleanup_confirmed": False,
             "phase": "failed", "stale": False, "accounting_complete": True,
             "volume_usd": 10, "net_pnl": 0, "fees": 0, "fills": 1,
             "position_usd": None, "position_known": False},
            {"session_id": "new", "market": "#101", "config": {"auto_group_id": "g"},
             "started": 2.0, "running": False, "cleanup_confirmed": True,
             "phase": "stopped", "stale": False, "accounting_complete": True,
             "volume_usd": 20, "net_pnl": 0, "fees": 0, "fills": 2,
             "position_usd": 0, "position_known": True},
        ]
        row = outcome_projection(entries)["display_bots"][0]
        self.assertFalse(row["cleanup_confirmed"])
        self.assertFalse(row["running"])


class ScannerDiagnosticsTests(unittest.TestCase):
    def scanner(self, *, age=100, history=True, ask=".38269"):
        scan = OutcomeFlowScanner()
        now = time.time()
        scan.connected = True
        scan.connected_at = now - age
        scan.catalog_at = now
        scan.rows = {10: {"id": 10, "name": "Tight", "fee_scale": 1,
                          "deployer_fee_scale": 1}}
        scan.subscribed = {"#100", "#101"}
        if history:
            scan.history["#101"].extend(
                {"time": (now - seconds) * 1000, "px": .38268, "sz": 100,
                 "side": "B" if i % 2 else "A", "tid": i}
                for i, seconds in enumerate(range(59, 0, -4)))
        return scan, {"time": now * 1000,
                      "levels": [[{"px": ".38268", "sz": "100"}],
                                 [{"px": ask, "sz": "100"}]]}, now

    def run_scan(self, scan, book, *, order_usd=15, spread_bps=10, base_rate=.000384):
        with ThreadPoolExecutor(1) as pool, \
             patch.object(scan, "_book_executor", return_value=pool), \
             patch.object(scan, "_book", return_value=book):
            return scan._snapshot("no", order_usd, spread_bps, base_rate=base_rate)

    def test_cost_stays_informational_and_rejections_are_reported(self):
        scan, book, _ = self.scanner(ask=".38269")
        result = self.run_scan(scan, book)
        self.assertTrue(result["ready"])
        self.assertEqual(result["cost_policy"], "informational")
        self.assertEqual(result["fees_not_covered"], 1)
        self.assertIn("rejections", result)
        self.assertIn("thresholds", result)

    def test_genuine_spread_depth_and_staleness_block_with_counts(self):
        cases = [
            ("spread", dict(spread_bps=0.1), "spread"),
            ("depth", dict(order_usd=500), "depth"),
        ]
        for name, options, category in cases:
            scan, book, _ = self.scanner()
            result = self.run_scan(scan, book, **options)
            self.assertFalse(result["ready"], name)
            self.assertGreater(result["rejections"][category], 0, name)
            self.assertTrue(result["reason"])
        scan, book, now = self.scanner()
        book = dict(book, time=(now - 100) * 1000)
        result = self.run_scan(scan, book)
        self.assertFalse(result["ready"])
        self.assertGreater(result["rejections"]["stale"], 0)

    def test_flow_observation_and_missing_trades_do_not_block_fresh_books(self):
        scan, book, _ = self.scanner(age=5)
        result = self.run_scan(scan, book)
        self.assertTrue(result["ready"])
        self.assertEqual(result["rejections"]["warmup"], 0)
        self.assertFalse(result["warming_up"])

        scan, book, _ = self.scanner(history=False)
        result = self.run_scan(scan, book)
        self.assertTrue(result["ready"])
        self.assertFalse(result["ready"] and result.get("catalog_stale"))
        self.assertIn("rejections", result)
        self.assertEqual(result["reason"], "")
        self.assertIsNone(result["candidates"][0]["queue_proxy_sec"])


class LegacyStopRouteTests(unittest.TestCase):
    def response(self, bots):
        from zeus import server

        manager = SimpleNamespace(
            stop_worldcup=Mock(return_value=["s1"]),
            outcome=SimpleNamespace(records=lambda: [("j", {"session_id": "s1",
                                                             "cleanup_confirmed": False})]))
        dashboard = SimpleNamespace(read=Mock(return_value={"bots": bots,
                                                            "display_bots": []}))
        with patch.object(server, "manager", manager), \
             patch.object(server, "dashboard", dashboard):
            return asyncio.run(server.stop_outcome(server.OutcomeStopRequest(session_id="s1")))

    def test_draining_stop_is_pending_and_only_confirmed_ids_are_stopped(self):
        response = self.response([{"session_id": "s1", "running": True,
                                   "cleanup_confirmed": False, "phase": "draining"}])
        self.assertEqual(response["status"], "pending")
        self.assertEqual(response["requested_session_ids"], ["s1"])
        self.assertEqual(response["stopped_session_ids"], [])
        self.assertEqual(response["state"]["bots"][0]["phase"], "draining")

    def test_independently_confirmed_stop_lists_the_stopped_id(self):
        response = self.response([{"session_id": "s1", "running": False,
                                   "cleanup_confirmed": True, "phase": "stopped"}])
        self.assertEqual(response["status"], "ok")
        self.assertEqual(response["requested_session_ids"], ["s1"])
        self.assertEqual(response["stopped_session_ids"], ["s1"])


class MakerConfigTests(unittest.TestCase):
    def test_build_config_always_quotes_at_touch_and_defaults_maker(self):
        public = {"market": "#101", "order_size_usd": 15, "max_position_usd": 200,
                  "maker_spread_pct": 0.5, "maker_offset_pct": 0.4}
        cfg = build_config(public, None)
        self.assertEqual(cfg.exec_mode, "maker")
        self.assertIsNone(cfg.maker_spread_pct)
        self.assertEqual(cfg.maker_offset_pct, 0)
        self.assertFalse(cfg.resume_exit)
        with self.assertRaises(ValueError):
            build_config({**public, "exec_mode": "taker"}, None)


if __name__ == "__main__":
    unittest.main()
