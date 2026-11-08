"""Outcome recovery tests: every exchange request is a local fake."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from zeus import server
from zeus.safety import ExecutionPaused, ExecutionStopped, SafetyLatch
import test_outcome_execution as outcome_tests


def response(status):
    return {"status": "ok", "response": {"data": {"statuses": [status]}}}


class OutcomeRecoveryTests(unittest.TestCase):
    def bot(self):
        bot, state = outcome_tests.OutcomeExecutionTests().funding_fixture()
        raw = bot.exchange
        bot.safety.phase = "running"
        bot.info.frontend_open_orders.return_value = []
        bot.exchange = bot.safety.wrap(raw, bot._reconcile_missing_cancels, bot._pause_execution)
        return bot, state, raw

    def recover(self, bot):
        bot.recovery_next = 0
        return bot._try_recovery()

    def test_rejection_keeps_session_alive_and_recovers_after_cooldown(self):
        for error in ("Insufficient spot balance asset=100014751", "Post only order would have immediately matched"):
            with self.subTest(error=error):
                bot, state, raw = self.bot()
                raw.order.return_value = response({"error": error})
                bot.place_or_requote(state)
                self.assertTrue(bot.running)
                self.assertFalse(bot.safety.stopped.is_set())
                self.assertEqual(bot.safety.phase, "recovering")
                self.assertFalse(bot.safety.snapshot()["quoting_enabled"])
                self.assertIn(error, bot.safety.recovery_error)
                self.assertFalse(bot.safety.pending_cloids)
                bot.place_or_requote(state)
                raw.order.assert_called_once()
                self.assertFalse(bot._try_recovery())
                bot.info.frontend_open_orders.assert_not_called()
                self.assertTrue(self.recover(bot))
                self.assertEqual(bot.safety.phase, "running")
                self.assertFalse(bot.safety.recovery_error)
                self.assertGreater(state.pending_until, 0)
                bot.place_or_requote(state)
                raw.order.assert_called_once()
                state.pending_until = state.opening_rearm_until = state.last_order_ts = 0
                raw.order.return_value = response({"resting": {"oid": 789}})
                bot.place_or_requote(state)
                self.assertEqual(raw.order.call_count, 2)
                self.assertEqual(state.bid_oid, 789)
                raw.bulk_cancel.assert_not_called()

    def test_account_outage_backs_off_without_cleanup_or_resend(self):
        bot, state, raw = self.bot()
        bot.info.spot_user_state.side_effect = TimeoutError("account offline")
        bot.place_or_requote(state)
        self.assertTrue(bot.running)
        self.assertEqual(bot.safety.phase, "recovering")
        for _ in range(4):
            self.assertFalse(self.recover(bot))
            self.assertLessEqual(bot.recovery_delay, 30)
            raw.order.assert_not_called()
            raw.bulk_cancel.assert_not_called()
        bot.info.spot_user_state.side_effect = None
        self.assertTrue(self.recover(bot))
        self.assertTrue(bot.running)

    def timeout(self, bot, state, raw):
        raw.order.side_effect = TimeoutError("response lost")
        bot.place_or_requote(state)
        self.assertTrue(bot.safety.pending_cloids)
        return next(iter(bot.safety.pending_cloids))

    def order_status(self, state, cloid, status):
        order = {"coin": state.spec.symbol, "oid": 789, "cloid": cloid,
                 "side": "B", "limitPx": "0.4"}
        return {"status": "order", "order": {"status": status, "order": order}}, order

    def test_unknown_timeout_does_not_resume_from_empty_balances_alone(self):
        bot, state, raw = self.bot()
        self.timeout(bot, state, raw)
        bot.info.post.return_value = {"status": "unknownOid"}
        for _ in range(3):
            self.assertFalse(self.recover(bot))
            self.assertTrue(bot.running)
            self.assertEqual(bot.safety.phase, "recovering")
            bot.place_or_requote(state)
        raw.order.assert_called_once()
        raw.bulk_cancel.assert_not_called()

    def test_late_resting_order_is_adopted_without_duplicate(self):
        bot, state, raw = self.bot()
        cloid = self.timeout(bot, state, raw)
        result, order = self.order_status(state, cloid, "open")
        bot.info.post.return_value = result
        bot.info.frontend_open_orders.return_value = [order]
        bot.info.open_orders.return_value = [order]
        self.assertTrue(self.recover(bot))
        self.assertEqual(state.bid_oid, 789)
        self.assertFalse(bot.safety.pending_cloids)
        state.pending_until = 0
        bot.place_or_requote(state)
        raw.order.assert_called_once()

    def test_late_filled_order_refreshes_inventory_before_resume(self):
        bot, state, raw = self.bot()
        cloid = self.timeout(bot, state, raw)
        bot.info.post.return_value = self.order_status(state, cloid, "filled")[0]
        bot.info.spot_user_state.return_value["balances"][1]["total"] = "125"
        self.assertTrue(self.recover(bot))
        self.assertEqual(state.position, 125)
        self.assertFalse(bot.safety.pending_cloids)
        raw.order.assert_called_once()

    def test_mismatched_or_malformed_status_stays_paused(self):
        for result in ({}, {"status": "order", "order": {}}, {"status": "unknownOid"}):
            bot, state, raw = self.bot()
            self.timeout(bot, state, raw)
            bot.info.post.return_value = result
            self.assertFalse(self.recover(bot))
            self.assertTrue(bot.running)
            self.assertTrue(bot.safety.pending_cloids)

    def test_stop_during_recovery_cannot_be_reversed(self):
        bot, state, raw = self.bot()
        bot._pause_execution("temporary failure")
        def stop_during_read(*args, **kwargs):
            bot.request_stop()
            return []
        bot.info.frontend_open_orders.side_effect = stop_during_read
        self.assertFalse(self.recover(bot))
        self.assertFalse(bot.running)
        self.assertTrue(bot.safety.stopped.is_set())
        with self.assertRaises(ExecutionStopped):
            bot.exchange.order(name=state.spec.symbol)
        raw.order.assert_not_called()

    def test_stop_cancels_late_confirmed_order_while_recovering(self):
        bot, state, raw = self.bot()
        cloid = self.timeout(bot, state, raw)
        result, order = self.order_status(state, cloid, "open")
        bot.info.post.return_value = result
        orders = [order]
        bot.info.frontend_open_orders.side_effect = lambda *args, **kwargs: list(orders)
        raw.bulk_cancel.side_effect = lambda requests: (orders.clear() or response("success"))
        bot.shutdown()
        self.assertFalse(bot.running)
        self.assertTrue(bot.safety.cleanup_confirmed)
        self.assertFalse(bot.safety.pending_cloids)
        raw.bulk_cancel.assert_called_once()
        raw.order.assert_called_once()  # no second placement or close needed

    def test_malformed_placement_ack_keeps_client_id_pending(self):
        bot, state, raw = self.bot()
        raw.order.return_value = response({"resting": {}})
        bot.place_or_requote(state)
        self.assertTrue(bot.running)
        self.assertEqual(bot.safety.phase, "recovering")
        self.assertTrue(bot.safety.pending_cloids)

    def test_gate_blocks_direct_calls_while_paused(self):
        bot, state, raw = self.bot()
        bot._pause_execution("temporary failure")
        for action, args in (("order", ()), ("bulk_cancel", ([{"coin": state.spec.symbol, "oid": 789}],))):
            with self.assertRaises(ExecutionPaused):
                getattr(bot.exchange, action)(*args)
        raw.order.assert_not_called()
        raw.bulk_cancel.assert_not_called()

    def test_pending_placement_is_persisted_and_blocks_flat_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.json"
            gate = SafetyLatch(path=path)
            gate.pause("response lost", ["0x" + "1" * 32])
            restored = SafetyLatch(path=path)
            self.assertEqual(restored.pending_cloids, gate.pending_cloids)
            self.assertFalse(gate.cleanup(lambda: [("#1", 0, [])], Mock(), Mock(), sleep=lambda _: None))
            self.assertFalse(gate.cleanup_confirmed)

    def test_server_does_not_confirm_flat_unknown_placement(self):
        bot, state, raw = self.bot()
        cloid = self.timeout(bot, state, raw)
        bot.info.post.return_value = {"status": "unknownOid"}
        record = {"account_kind": "outcome", "symbols": [state.spec.symbol], "pending_cloids": [cloid]}
        with patch("hyperliquid.info.Info", return_value=bot.info), \
                patch.dict(server.os.environ, {"WALLET_ADDRESS_OUTCOME": "offline-test"}):
            with self.assertRaises(RuntimeError):
                server.BotManager(state_dir=False)._verify_session_flat(record)

    def test_runtime_exception_keeps_loop_alive_until_manual_stop(self):
        bot, state, raw = self.bot()
        bot.sync_account = Mock(return_value=True)
        bot.restart_ws_if_needed = Mock(side_effect=RuntimeError("temporary loop error"))
        bot.refresh_rest_bbos = Mock()
        def stop_on_wait(*args):
            self.assertTrue(bot.running)
            self.assertEqual(bot.safety.phase, "recovering")
            bot.request_stop()
        bot.wake.wait = Mock(side_effect=stop_on_wait)
        bot._trading_loop(0.1)
        raw.order.assert_not_called()
        raw.bulk_cancel.assert_not_called()


if __name__ == "__main__":
    unittest.main()
