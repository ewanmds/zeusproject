"""Every exchange and account query in this suite is a local fake."""
import importlib
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from fastapi.testclient import TestClient

from zeus.safety import SafetyLatch, ExecutionStopped, account_snapshot, supervisor_alive
from zeus import server
import test_outcome_execution as outcome_tests


def accepted(oid=123):
    return {"status": "ok", "response": {"data": {"statuses": [{"resting": {"oid": oid}}]}}}


def canceled():
    return {"status": "ok", "response": {"data": {"statuses": ["success"]}}}


class SafetyTests(unittest.TestCase):
    def test_stop_blocks_queued_send_and_preserves_first_error(self):
        gate = SafetyLatch()
        raw = Mock()
        exchange = gate.wrap(raw)
        gate.fail("first error")
        gate.fail("second error")
        with self.assertRaises(ExecutionStopped):
            exchange.order(name="#1", sz=1)
        self.assertEqual(gate.error, "first error")
        raw.order.assert_not_called()

    def test_inflight_response_drains_before_cleanup_and_is_canceled(self):
        gate = SafetyLatch()
        entered, release = threading.Event(), threading.Event()
        raw = Mock()
        orders = []
        def send(**kwargs):
            entered.set()
            if not release.wait(2):
                raise TimeoutError("test thread timed out")
            orders.append({"oid": 123, "coin": "#1"})
            return accepted()
        raw.order.side_effect = send
        raw.bulk_cancel.side_effect = lambda reqs: (orders.clear() or canceled())
        exchange = gate.wrap(raw)
        sender = threading.Thread(target=lambda: exchange.order(name="#1", sz=1))
        sender.start()
        self.assertTrue(entered.wait(2))
        gate.stop()
        snapshot = Mock(side_effect=lambda: [("#1", 0, list(orders))])
        cleaner = threading.Thread(target=lambda: gate.cleanup(snapshot,
            lambda rows: exchange.bulk_cancel([{"coin": s, "oid": oid} for s, oid in rows]), Mock(), sleep=lambda _: None))
        cleaner.start()
        self.assertFalse(gate.cleanup_done.is_set())
        release.set()
        sender.join(2); cleaner.join(2)
        self.assertFalse(sender.is_alive() or cleaner.is_alive())
        self.assertTrue(gate.cleanup_confirmed)
        raw.bulk_cancel.assert_called_once()
        gate.stop()
        self.assertEqual(gate.phase, "stopped")

    def test_failed_ack_latches_even_after_rest_recovers(self):
        gate = SafetyLatch()
        raw = Mock()
        raw.bulk_orders.return_value = {"status": "ok", "response": {"data": {"statuses": [{"error": "Post only would match"}]}}}
        exchange = gate.wrap(raw)
        with self.assertRaises(RuntimeError):
            exchange.bulk_orders([{"coin": "#1", "is_buy": True}])
        self.assertTrue(gate.stopped.is_set())
        gate.cleanup(lambda: [("#1", 0, [])], Mock(), Mock(), sleep=lambda _: None)
        self.assertTrue(gate.cleanup_confirmed)
        self.assertEqual(gate.phase, "failed")
        with self.assertRaises(ExecutionStopped):
            exchange.order(name="#1")

    def test_partial_fills_close_only_fresh_residual(self):
        gate = SafetyLatch()
        gate.exposure_started = True
        position = [10]
        quantities = []
        def close(symbol, qty):
            quantities.append(qty)
            position[0] = 4 if qty == 10 else 0
        self.assertTrue(gate.cleanup(lambda: [("#1", position[0], [])], Mock(), close, sleep=lambda _: None))
        self.assertEqual(quantities, [10, 4])

    def test_uncertain_close_is_never_retried_blindly(self):
        gate = SafetyLatch()
        gate.exposure_started = True
        raw = Mock()
        raw.order.side_effect = TimeoutError("response lost")
        exchange = gate.wrap(raw)
        result = gate.cleanup(lambda: [("#1", 10, [])], Mock(),
                              lambda symbol, qty: exchange.order(name=symbol, sz=qty), sleep=lambda _: None)
        self.assertFalse(result)
        self.assertEqual(raw.order.call_count, 1)
        self.assertFalse(gate.snapshot()["can_start"])

    def test_unrelated_orders_and_preexisting_positions_are_not_touched(self):
        for row in [("#1", 0, [{"oid": 999}]), ("#1", 10, [])]:
            gate = SafetyLatch()
            cancel, close = Mock(), Mock()
            self.assertFalse(gate.cleanup(lambda: [row], cancel, close, sleep=lambda _: None))
            cancel.assert_not_called(); close.assert_not_called()

    def test_unreadable_account_never_confirms_flat(self):
        gate = SafetyLatch()
        snapshot = Mock(side_effect=TimeoutError("offline"))
        self.assertFalse(gate.cleanup(snapshot, Mock(), Mock(), sleep=lambda _: None))
        self.assertEqual(snapshot.call_count, 3)
        self.assertFalse(gate.cleanup_confirmed)
        info = Mock()
        info.spot_user_state.return_value = {}
        with self.assertRaises(ValueError):
            account_snapshot(info, "dummy", ["#1"], spot=True, balances={"#1": "+1"})

    def test_journal_failure_prevents_send_and_redacts_error(self):
        with tempfile.TemporaryDirectory() as directory:
            gate = SafetyLatch(path=Path(directory) / "session.json")
            raw = Mock()
            with patch("zeus.safety.atomic_json", side_effect=OSError("denied")):
                with self.assertRaises(RuntimeError):
                    gate.wrap(raw).order(name="#1")
            raw.order.assert_not_called()
        gate = SafetyLatch()
        gate.fail("private_key=" + "a" * 64)
        self.assertNotIn("a" * 64, gate.error)

    def test_persistent_unconfirmed_session_blocks_restart_until_verified(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = server.BotManager(state_dir=directory)
            path = manager._reserve_session("outcome", ["#1"], "outcome")
            with self.assertRaises(RuntimeError):
                manager._reserve_session("outcome", ["#1"], "outcome")
            with patch.object(manager, "_verify_session_flat", side_effect=TimeoutError("offline")), patch.object(server, "_pid_alive", return_value=False):
                self.assertFalse(manager.reconcile_sessions()["can_start"])
            with patch.object(manager, "_verify_session_flat"), patch.object(server, "_pid_alive", return_value=False):
                self.assertTrue(manager.reconcile_sessions()["can_start"])
            self.assertTrue(manager._session_status(path)["cleanup_confirmed"])

    def test_outcome_stopped_cannot_quote_and_exception_runs_cleanup(self):
        bot = outcome_tests.OutcomeExecutionTests().configured_bot("maker")
        bot.running = False
        bot.place_or_requote(outcome_tests.OutcomeExecutionTests().state())
        bot.exchange.order.assert_not_called()
        bot.running = True
        bot.console = Mock(); bot.scan_table = Mock(); bot._launch_prompt = Mock()
        bot._cleanup_snapshot = Mock(return_value=[])
        bot.sync_account = Mock()
        bot.start_ws = Mock(side_effect=RuntimeError("websocket init failed"))
        bot.ws_info = None
        bot.run()
        self.assertEqual(bot.safety.phase, "failed")
        self.assertTrue(bot.safety.cleanup_confirmed)
        bot.shutdown()
        self.assertEqual(bot.safety.phase, "failed")

    def test_stop_while_constructing_does_not_claim_success(self):
        manager = server.BotManager(state_dir=False)
        manager.wc_thread = Mock()
        manager.wc_thread.is_alive.return_value = True
        manager.wc_lifecycle["cleanup_confirmed"] = False
        with self.assertRaises(RuntimeError):
            manager.stop_worldcup()
        self.assertTrue(manager.wc_stop_event.is_set())

    def test_late_constructor_cannot_start_after_stop(self):
        manager = server.BotManager(state_dir=False)
        entered, release = threading.Event(), threading.Event()
        bot = Mock()
        bot.safety = SafetyLatch()
        def finish():
            bot.safety.stop()
            bot.safety.cleanup(lambda: [], Mock(), Mock(), sleep=lambda _: None)
        bot.shutdown.side_effect = finish
        def construct(cfg):
            entered.set()
            if not release.wait(2): raise TimeoutError("test constructor timed out")
            return bot
        from types import SimpleNamespace
        from zeus.bots.worldcup import BotConfig
        module = SimpleNamespace(BotConfig=BotConfig, HyperliquidSpreadBot=construct)
        errors = []
        def stop():
            try: manager.stop_worldcup()
            except Exception as exc: errors.append(exc)
        with patch.object(server, "_safe_import", side_effect=[module, SimpleNamespace(all_market_keys=lambda:["#1"])]), \
             patch.dict(os.environ, {"WALLET_ADDRESS_OUTCOME":"offline", "PRIVATE_KEY_OUTCOME":"dummy"}):
            manager.start_worldcup(server.WorldcupStartRequest())
            self.assertTrue(entered.wait(2))
            stopper = threading.Thread(target=stop)
            stopper.start()
            self.assertTrue(manager.wc_stop_event.wait(2))
            release.set(); stopper.join(2)
        self.assertFalse(stopper.is_alive())
        self.assertFalse(errors)
        bot.run.assert_not_called()
        bot.request_stop.assert_called_once()

    def test_parent_monitor_and_last_moment_running_check(self):
        with patch.dict(os.environ, {"ZEUS_PARENT_PID":str(os.getpid())}):
            self.assertTrue(supervisor_alive())
        with patch.dict(os.environ, {"ZEUS_PARENT_PID":"0"}):
            self.assertFalse(supervisor_alive())
        gate = SafetyLatch(is_running=lambda:False)
        raw = Mock()
        with self.assertRaises(ExecutionStopped):
            gate.wrap(raw).order(name="#1")
        raw.order.assert_not_called()

    def test_stop_api_never_returns_success_without_confirmation(self):
        server.app.dependency_overrides[server._require_api_token] = lambda: None
        try:
            with patch.object(server.manager, "stop_worldcup"), patch.object(server.manager, "get_worldcup_stats", return_value={"running": False, "cleanup_confirmed": False}):
                response = TestClient(server.app).post("/api/outcomes/stop")
                self.assertEqual(response.status_code, 400)
                self.assertIn("not confirmed", response.json()["message"])
        finally:
            server.app.dependency_overrides.pop(server._require_api_token, None)

    def test_cost_per_million_is_positive_for_a_loss(self):
        bot = outcome_tests.OutcomeExecutionTests().configured_bot("maker")
        bot.stats.volume_usd = 100
        bot.stats.fees_paid = 0.1
        bot.stats.gross_closed_pnl = 0.01
        self.assertAlmostEqual(bot.export_dashboard_stats()["cost_per_1m"], 900)

    def test_bbo_entrypoints_and_finally_cleanup(self):
        with patch.dict(os.environ, {"PRIVATE_KEY": "1" * 64, "WALLET_ADDRESS": "0x" + "1" * 40}):
            mod = importlib.import_module("zeus.bots.order_book")
        bot = mod.OrderBookBot.__new__(mod.OrderBookBot)
        bot.running = False
        bot.live = True
        bot.safety = SafetyLatch(lambda: setattr(bot, "running", False))
        bot.exchange = Mock()
        bot.place_quotes([{"coin": "fake"}])
        self.assertFalse(bot.modify_quotes([(1, {})]))
        bot.exchange.bulk_orders.assert_not_called()
        bot.running = True
        bot._check_stop_file = Mock()
        bot._cleanup_snapshot = Mock(return_value=[])
        bot._run_session = Mock(side_effect=RuntimeError("unexpected loop error"))
        bot.ws_info = None
        bot.write_stats = Mock()
        bot.run()
        self.assertTrue(bot.safety.cleanup_confirmed)
        self.assertEqual(bot.safety.phase, "failed")
        bot.write_stats.assert_called_with(running=False)


if __name__ == "__main__":
    unittest.main()
