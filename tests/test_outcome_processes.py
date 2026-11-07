"""Real OS-process lifecycle tests using fake workers and dummy credentials."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from zeus import server
from zeus.safety import atomic_json


def wait_until(check, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.05)
    raise AssertionError("Offline worker condition timed out")


class OutcomeArchivedPidTests(unittest.TestCase):
    def test_reused_pid_does_not_resurrect_verified_stopped_session(self):
        with tempfile.TemporaryDirectory() as folder:
            manager = server.BotManager(state_dir=folder)
            record = dict(session_id='archived', key='outcome:#101', account_kind='outcome', symbols=['#101'],
                          pid=os.getpid(), started=1, phase='stopped', cleanup_confirmed=True)
            atomic_json(Path(folder)/'archived.json', record)
            state = manager.get_worldcup_stats()
            self.assertFalse(state['bots'][0]['running'])
            self.assertTrue(state['bots'][0]['cleanup_confirmed'])
            self.assertEqual(state['display_bots'], [])
            self.assertEqual(manager.stop_worldcup('archived'), ['archived'])
            manager.outcome.rotation.close()

    def test_unverified_session_is_never_hidden_as_a_pid_reuse_fix(self):
        with tempfile.TemporaryDirectory() as folder:
            manager = server.BotManager(state_dir=folder)
            record = dict(session_id='unknown', key='outcome:#101', account_kind='outcome', symbols=['#101'],
                          pid=os.getpid(), started=1, phase='failed', cleanup_confirmed=False)
            atomic_json(Path(folder)/'unknown.json', record)
            state = manager.get_worldcup_stats()
            self.assertFalse(state['bots'][0]['cleanup_confirmed'])
            self.assertEqual(len(state['display_bots']), 1)
            with self.assertRaises(RuntimeError):
                manager.stop_worldcup('unknown')
            manager.outcome.rotation.close()


class OutcomeProcessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.manager = server.BotManager(state_dir=self.temp.name)
        self.popen = subprocess.Popen
        self.override_parent = None
        self.env = patch.dict(os.environ, {"WALLET_ADDRESS_OUTCOME": "offline-test",
                                           "PRIVATE_KEY_OUTCOME": "dummy", "ZEUS_TEST_SLOW_CONSTRUCTOR": "0"})
        self.env.start()
        self.catalog = patch("zeus.outcomes.resolve_market", side_effect=lambda value: {
            "key": "#14881" if value == "alias-one" else value, "name": "Test " + value})
        self.catalog.start()
        self.spawn_patch = patch("zeus.outcome_supervisor.subprocess.Popen", side_effect=self.fake_spawn)
        self.spawn = self.spawn_patch.start()

    def tearDown(self):
        self.manager.outcome.rotation.close()
        for info in self.manager.outcome.procs.values():
            Path(info["stop_file"]).touch()
            (Path(info["stop_file"]).parent / "release").touch()
        for info in self.manager.outcome.procs.values():
            try:
                info["proc"].wait(timeout=12)
            except subprocess.TimeoutExpired:
                info["proc"].kill()  # only this test's explicitly fake process
                info["proc"].wait(timeout=3)
        self.spawn_patch.stop()
        self.catalog.stop()
        self.env.stop()
        self.temp.cleanup()

    def fake_spawn(self, cmd, **kwargs):
        self.assertEqual(cmd[3], "zeus.bots.outcome_worker")
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertNotIn("dummy", " ".join(cmd))
        if self.override_parent:
            kwargs["env"]["ZEUS_PARENT_PID"] = str(self.override_parent)
        return self.popen([sys.executable, "-u", str(Path(__file__).with_name("offline_outcome_worker.py")),
                           "--config", cmd[-1]], **kwargs)

    def start(self, market="#14881", **kwargs):
        return self.manager.start_worldcup(server.WorldcupStartRequest(market=market, **kwargs))

    def ready(self, count=1):
        wait_until(lambda: len(self.manager.get_worldcup_stats()["bots"]) == count
                   and all(b["phase"] == "running" and not b["stale"] for b in self.manager.get_worldcup_stats()["bots"]))

    def test_two_processes_parameters_stats_and_targeted_stop(self):
        one = self.start(exec_mode="maker", maker_spread_pct=0.007)
        two = self.start("#14751", order_size_usd=80, exec_mode="taker")
        self.ready(2)
        self.assertNotEqual(one["pid"], two["pid"])
        self.assertNotEqual(one["session_id"], two["session_id"])
        infos = list(self.manager.outcome.procs.values())
        self.assertNotEqual(infos[0]["stop_file"], infos[1]["stop_file"])
        self.assertNotEqual(infos[0]["stats_file"], infos[1]["stats_file"])
        stats = self.manager.get_worldcup_stats()
        self.assertEqual(stats["volume_usd"], 200)
        self.assertEqual(stats["fills"], 4)
        self.assertAlmostEqual(stats["cost_per_1m"], 1000)
        configs = {b["market"]: b["config"] for b in stats["bots"]}
        self.assertEqual(configs["#14881"]["maker_spread_pct"], 0.007)
        self.assertEqual(configs["#14751"]["order_size_usd"], 80)
        self.manager.stop_worldcup(one["session_id"])
        self.manager.stop_worldcup(one["session_id"])
        bots = {b["market"]: b for b in self.manager.get_worldcup_stats()["bots"]}
        self.assertTrue(bots["#14881"]["cleanup_confirmed"])
        self.assertFalse(bots["#14881"]["running"])
        self.assertTrue(bots["#14751"]["running"])
        self.assertEqual(bots["#14881"]["volume_usd"], 100)
        self.assertFalse(Path(infos[1]["stop_file"]).exists())
        events = json.loads((Path(infos[0]["stop_file"]).parent / "events.json").read_text())
        self.assertEqual([r["event"] for r in events], ["run", "cleanup"])
        self.assertTrue(all(row["market"] == "#14881" for row in events))

    def test_auto_pins_chosen_market_and_metrics_to_its_worker(self):
        chosen = {"market": "#14881", "name": "Repeated takers", "score": 123,
                  "volume_usd_1m": 500, "queue_proxy_sec": 12}
        with patch("zeus.outcome_flow.scanner.snapshot", return_value={"ready": True, "candidates": [chosen]}) as scan:
            result = self.start("#14751", selection_mode="auto", outcome_side="no", exec_mode="maker")
        self.ready()
        self.assertEqual(result["market"], "#14881")
        bot = self.manager.get_worldcup_stats()["bots"][0]
        self.assertEqual(bot["config"]["selection_mode"], "auto")
        self.assertEqual(bot["config"]["outcome_side"], "no")
        self.assertEqual(bot["config"]["auto_selection"]["score"], 123)
        self.assertIsNone(bot["config"]["maker_spread_pct"])
        from zeus.bots.outcome_worker import build_config
        self.assertIsNone(build_config(bot["config"], None).maker_spread_pct)
        self.manager.stop_worldcup(result["session_id"])

    def test_auto_rotation_replaces_only_after_old_worker_stops_and_stop_tracks_chain(self):
        from collections import deque
        with patch("zeus.outcome_rotation.OutcomeRotation._loop", return_value=None), \
             patch("zeus.outcome_flow.scanner.snapshot") as scan:
            scan.return_value = {"ready": True, "candidates": [{"market": "#14881", "score": 10}]}
            old = self.start(selection_mode="auto", exec_mode="maker", order_size_usd=10, max_position_usd=200)
            self.ready()
            controller = self.manager.outcome.rotation
            group = controller.groups[old["session_id"]]
            now = time.time()
            group["started"] = now-200
            group["samples"] = deque([(now-180, 100)])
            scan.return_value = {"ready": True, "candidates": [{"market": "#14751", "score": 20, "volume_usd_1m": 1000}]}
            controller.tick(now)
            self.assertEqual(group["state"], "draining")
            self.assertEqual(self.spawn.call_count, 1)
            old_info = self.manager.outcome.procs[old["session_id"]]
            wait_until(lambda: old_info["proc"].poll() is not None and
                       self.manager._session_status(old_info["safety_file"]).get("cleanup_confirmed"))
            controller.tick()
            self.assertEqual(self.spawn.call_count, 2)
            new_id = group["current"]
            self.assertNotEqual(new_id, old["session_id"])
            wait_until(lambda: any(b["session_id"] == new_id and b["phase"] == "running"
                       for b in self.manager.get_worldcup_stats()["bots"]))
            cfg = self.manager._session_status(self.manager.outcome.procs[new_id]["safety_file"])["config"]
            self.assertEqual((cfg["market"], cfg["order_size_usd"], cfg["max_position_usd"], cfg["outcome_side"]),
                             ("#14751", 10, 200, "no"))
            self.assertTrue(cfg["two_sided"])
            self.manager.stop_worldcup(old["session_id"])
            self.assertTrue(group["cancelled"])
            self.assertFalse(self.manager.get_worldcup_stats()["running"])

    def test_yes_and_no_get_separate_processes_and_scoped_stop(self):
        yes = self.start(outcome_side="yes", exec_mode="maker")
        no = self.start(outcome_side="no")
        self.ready(2)
        self.assertEqual(yes["market"], "#14880")
        self.assertEqual(no["market"], "#14881")
        self.assertNotEqual(yes["pid"], no["pid"])
        configs = {b["market"]: b["config"] for b in self.manager.get_worldcup_stats()["bots"]}
        self.assertEqual(configs["#14880"]["outcome_side"], "yes")
        self.assertEqual(configs["#14881"]["outcome_side"], "no")
        with self.assertRaises(RuntimeError): self.start("alias-one", outcome_side="yes")
        self.manager.stop_worldcup(yes["session_id"])
        states = {b["market"]: b for b in self.manager.get_worldcup_stats()["bots"]}
        self.assertTrue(states["#14880"]["cleanup_confirmed"])
        self.assertTrue(states["#14881"]["running"])

    def test_unavailable_side_does_not_launch_a_worker(self):
        with patch("zeus.outcomes.resolve_market", side_effect=[{"key": "#14881"}, None]):
            with self.assertRaises(ValueError): self.start(outcome_side="yes")
        self.spawn.assert_not_called()

    def test_concurrent_alias_start_launches_once(self):
        results, errors = [], []
        def start(market):
            try:
                results.append(self.start(market))
            except RuntimeError as exc:
                errors.append(str(exc))
        threads = [threading.Thread(target=start, args=(market,)) for market in ("#14881", "alias-one")]
        for thread in threads: thread.start()
        for thread in threads: thread.join(5)
        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)
        self.assertEqual(self.spawn.call_count, 1)

    def test_all_and_empty_rejected_without_process(self):
        for market in ("ALL", "", "all"):
            with self.assertRaises(ValueError): self.start(market)
        self.spawn.assert_not_called()

    def test_stop_during_constructor_prevents_run(self):
        with patch.dict(os.environ, {"ZEUS_TEST_SLOW_CONSTRUCTOR": "1"}):
            result = self.start()
        info = self.manager.outcome.procs[result["session_id"]]
        runtime = Path(info["stop_file"]).parent
        wait_until(lambda: (runtime / "constructing").exists())
        errors = []
        def stop():
            try: self.manager.stop_worldcup(result["session_id"])
            except Exception as exc: errors.append(exc)
        thread = threading.Thread(target=stop)
        thread.start()
        wait_until(lambda: Path(info["stop_file"]).exists())
        (runtime / "release").touch()
        thread.join(12)
        self.assertFalse(thread.is_alive())
        self.assertFalse(errors)
        events = json.loads((runtime / "events.json").read_text())
        self.assertEqual([event["event"] for event in events], ["cleanup"])

    def test_restart_reads_durable_stats_without_auto_start(self):
        result = self.start()
        self.ready()
        restored = server.BotManager(state_dir=self.temp.name)
        self.assertEqual(len(restored.get_worldcup_stats()["bots"]), 1)
        self.assertEqual(restored.get_worldcup_stats()["bots"][0]["phase"], "failed")
        self.assertEqual(self.spawn.call_count, 1)
        with self.assertRaises(RuntimeError): restored.start_worldcup(server.WorldcupStartRequest(market="#14881"))
        self.manager.stop_worldcup(result["session_id"])
        stats = restored.get_worldcup_stats()
        self.assertTrue(stats["bots"][0]["cleanup_confirmed"])
        self.assertEqual(stats["volume_usd"], 100)

    def test_stale_or_wrong_snapshot_never_becomes_current(self):
        result = self.start()
        self.ready()
        info = self.manager.outcome.procs[result["session_id"]]
        self.manager.stop_worldcup(result["session_id"])
        data = json.loads(Path(info["stats_file"]).read_text())
        atomic_json(info["stats_file"], {**data, "session_id": "other-session", "volume_usd": 9000})
        # Last known final accounting is preserved; an unrelated snapshot is ignored.
        self.assertNotEqual(self.manager.get_worldcup_stats()["volume_usd"], 9000)

    def test_unconfirmed_market_does_not_block_other_market(self):
        result = self.start()
        self.ready()
        self.manager.stop_worldcup(result["session_id"])
        info = self.manager.outcome.procs[result["session_id"]]
        record = self.manager._session_status(info["safety_file"])
        atomic_json(info["safety_file"], {**record, "cleanup_confirmed": False})
        with self.assertRaises(RuntimeError): self.start()
        self.start("#14751")
        self.assertEqual(self.spawn.call_count, 2)

    def test_targeted_stop_api_preserves_other_running_session(self):
        one = self.start()
        two = self.start("#14751")
        self.ready(2)
        server.app.dependency_overrides[server._require_api_token] = lambda: None
        try:
            with patch.object(server, "manager", self.manager):
                result = TestClient(server.app).post("/api/outcomes/stop", json={"session_id": one["session_id"]})
                self.assertEqual(result.status_code, 200, result.text)
                self.assertTrue(result.json()["state"]["running"])
        finally:
            server.app.dependency_overrides.pop(server._require_api_token, None)

    def test_stop_all_signals_every_worker(self):
        self.start()
        self.start("#14751")
        self.ready(2)
        self.manager.stop_worldcup()
        self.assertTrue(self.manager.get_worldcup_stats()["cleanup_confirmed"])
        self.assertFalse(self.manager.get_worldcup_stats()["running"])

    def test_blocked_process_does_not_report_stopped_or_get_forgotten(self):
        result = self.start()
        self.ready()
        info = self.manager.outcome.procs[result["session_id"]]
        with patch.object(info["proc"], "wait", side_effect=subprocess.TimeoutExpired("fake-worker", 30)):
            with self.assertRaises(RuntimeError): self.manager.stop_worldcup(result["session_id"])
        self.assertTrue(Path(info["stop_file"]).exists())
        self.assertIn(result["session_id"], self.manager.outcome.procs)
        self.assertFalse(self.manager.get_worldcup_stats()["cleanup_confirmed"])

    def test_recovery_of_one_process_does_not_suspend_another(self):
        one = self.start()
        self.start("#14751")
        self.ready(2)
        info = self.manager.outcome.procs[one["session_id"]]
        runtime = Path(info["stop_file"]).parent
        (runtime / "recover").touch()
        wait_until(lambda: any(b["phase"] == "recovering" for b in self.manager.get_worldcup_stats()["bots"]))
        states = {b["market"]: b for b in self.manager.get_worldcup_stats()["bots"]}
        self.assertEqual(states["#14751"]["phase"], "running")
        self.assertTrue(states["#14881"]["running"])
        (runtime / "resume").touch()
        self.ready(2)

    def test_losing_supervisor_triggers_worker_cleanup(self):
        parent = self.popen([sys.executable, "-c", "import time; time.sleep(30)"],
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        try:
            self.override_parent = parent.pid
            result = self.start()
            self.ready()
            parent.terminate()  # only this test's sleeping supervisor stub
            parent.wait(3)
            info = self.manager.outcome.procs[result["session_id"]]
            info["proc"].wait(12)
            self.assertTrue(self.manager.get_worldcup_stats()["bots"][0]["cleanup_confirmed"])
        finally:
            if parent.poll() is None:
                parent.terminate()
                parent.wait(3)

    def test_process_instance_lock_rejects_duplicate_worker(self):
        result = self.start()
        self.ready()
        info = self.manager.outcome.procs[result["session_id"]]
        public = json.loads((Path(info["stop_file"]).parent / "config.json").read_text())
        runtime = Path(self.temp.name) / "duplicate"
        journal = Path(self.temp.name) / "duplicate.json"
        record = self.manager._session_status(info["safety_file"])
        atomic_json(journal, {**record, "session_id": "duplicate", "phase": "connecting", "cleanup_confirmed": False})
        public["safety_file"] = str(journal)
        config = runtime / "config.json"
        atomic_json(config, public)
        proc = self.popen([sys.executable, str(Path(__file__).with_name("offline_outcome_worker.py")),
                           "--config", str(config)], env=os.environ.copy(),
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            self.assertEqual(proc.wait(12), 1)
            self.assertFalse((runtime / "events.json").exists())
            self.assertIsNone(info["proc"].poll())
            self.assertIn("Process lock busy", json.loads(journal.read_text())["error"])
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(3)

    def test_stale_one_worker_does_not_block_new_market_globally(self):
        self.start()
        self.ready()
        with patch("zeus.outcome_supervisor.time.time", return_value=time.time() + 10):
            stats = self.manager.get_worldcup_stats()
        self.assertTrue(stats["bots"][0]["stale"])
        self.assertTrue(stats["partial_stale"])
        self.assertFalse(stats["stale"])
        self.assertTrue(stats["can_start"])

    def test_reconciliation_cannot_clear_a_session_still_being_launched(self):
        entered, release, reconciled = threading.Event(), threading.Event(), threading.Event()
        errors = []
        def slow_spawn(cmd, **kwargs):
            entered.set()
            if not release.wait(5): raise TimeoutError("test launch blocked")
            return self.fake_spawn(cmd, **kwargs)
        self.spawn.side_effect = slow_spawn
        def start():
            try: self.start()
            except Exception as exc: errors.append(exc)
        def reconcile():
            try: self.manager.reconcile_sessions()
            except Exception as exc: errors.append(exc)
            finally: reconciled.set()
        with patch.object(self.manager, "_verify_session_flat", side_effect=AssertionError("live launch must not be cleared")) as verify:
            starter = threading.Thread(target=start)
            reader = threading.Thread(target=reconcile)
            starter.start()
            try:
                self.assertTrue(entered.wait(5))
                reader.start()
                self.assertFalse(reconciled.wait(0.1))
            finally:
                release.set()
                starter.join(6)
                if reader.ident is not None: reader.join(6)
            self.assertFalse(errors)
            self.assertFalse(starter.is_alive() or reader.is_alive())
            verify.assert_not_called()


if __name__ == "__main__":
    unittest.main()
