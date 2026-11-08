"""Cross-process budget tests against a local JSON wallet, never an exchange."""
import json
import multiprocessing
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from zeus.process_control import OutcomeFunds, ProcessLock
from zeus.safety import SafetyLatch, atomic_json, read_json


def funded_child(root, number, start, result):
    coordinator = OutcomeFunds(root, "offline-account", str(number))
    wallet_path = Path(root) / "fake-wallet.json"
    class Exchange:
        def order(self, **kwargs):
            available = json.loads(wallet_path.read_text())["available"]
            atomic_json(wallet_path, {"available": available - kwargs["sz"] * kwargs["limit_px"]})
            return {"status": "ok", "response": {"data": {"statuses": [{"resting": {"oid": number + 1}}]}}}
    exchange = coordinator.wrap(Exchange())
    start.wait(10)
    with coordinator.transaction():
        available = json.loads(wallet_path.read_text())["available"] - coordinator.reserved_usdc()
        if available < 50:
            result.put("waiting")
            return
        exchange.order(name="#1", is_buy=True, sz=50, limit_px=1,
                       cloid="0x" + f"{number + 1:032x}")
        result.put("sent")


class OutcomeFundsTests(unittest.TestCase):
    def test_three_concurrent_processes_do_not_spend_the_same_usdc(self):
        with tempfile.TemporaryDirectory() as root:
            atomic_json(Path(root) / "fake-wallet.json", {"available": 105})
            ctx = multiprocessing.get_context("spawn")
            start, result = ctx.Event(), ctx.Queue()
            children = [ctx.Process(target=funded_child, args=(root, n, start, result)) for n in range(3)]
            try:
                for child in children: child.start()
                start.set()
                answers = [result.get(timeout=15) for _ in children]
                for child in children: child.join(15)
                self.assertEqual(answers.count("sent"), 2)
                self.assertEqual(answers.count("waiting"), 1)
                self.assertTrue(all(child.exitcode == 0 for child in children))
                self.assertEqual(json.loads((Path(root) / "fake-wallet.json").read_text())["available"], 5)
                self.assertEqual(OutcomeFunds(root, "offline-account", "new").reserved_usdc(), 0)
            finally:
                for child in children:
                    if child.is_alive():
                        child.kill()  # only explicitly fake test children
                        child.join(3)
                result.close()

    def test_timeout_reservation_survives_process_restart_until_confirmed(self):
        with tempfile.TemporaryDirectory() as root:
            funds = OutcomeFunds(root, "offline-account", "one")
            raw = Mock()
            raw.order.side_effect = TimeoutError("response lost")
            with self.assertRaises(TimeoutError):
                funds.wrap(raw).order(is_buy=True, sz=50, limit_px=1, cloid="0x123")
            other = OutcomeFunds(root, "offline-account", "two")
            self.assertEqual(other.reserved_usdc(), 50)
            other.resolve(["different-cloid"])
            self.assertEqual(other.reserved_usdc(), 50)
            other.resolve(["0x123"])
            self.assertEqual(other.reserved_usdc(), 0)

    def test_known_rejection_releases_reservation(self):
        with tempfile.TemporaryDirectory() as root:
            funds = OutcomeFunds(root, "offline-account", "one")
            raw = Mock()
            raw.order.return_value = {"status": "ok", "response": {"data": {"statuses": [
                {"error": "Insufficient spot balance asset=1"}]}}}
            funds.wrap(raw).order(is_buy=True, sz=50, limit_px=1, cloid="0x123")
            self.assertEqual(funds.reserved_usdc(), 0)

    def test_wallet_reservations_do_not_affect_other_account(self):
        with tempfile.TemporaryDirectory() as root:
            funds = OutcomeFunds(root, "one-account", "one")
            raw = Mock()
            raw.order.side_effect = TimeoutError("response lost")
            with self.assertRaises(TimeoutError): funds.wrap(raw).order(is_buy=True, sz=50, limit_px=1, cloid="id")
            self.assertEqual(OutcomeFunds(root, "other-account", "two").reserved_usdc(), 0)

    def test_invalid_shared_journal_never_becomes_zero_funds_reserved(self):
        with tempfile.TemporaryDirectory() as root:
            funds = OutcomeFunds(root, "one-account", "one")
            atomic_json(funds.root / "funds.json", {"reserved": {"id": {"usd": "nan"}}})
            with self.assertRaises(ValueError): funds.reserved_usdc()

    def test_process_lock_prevents_second_owner_and_releases_after_exit(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "instance.lock"
            with ProcessLock(path, timeout=0):
                with self.assertRaises(TimeoutError):
                    with ProcessLock(path, timeout=0): self.fail("duplicate owner")
            with ProcessLock(path, timeout=0): pass

    def test_pending_intent_is_persisted_before_exchange_call(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "session.json"
            gate = SafetyLatch(path=path)
            raw = Mock()
            def inspect(**kwargs):
                saved = json.loads(path.read_text())
                self.assertIn(str(kwargs["cloid"]), saved["pending_cloids"])
                return {"status": "ok", "response": {"data": {"statuses": [{"resting": {"oid": 123}}]}}}
            raw.order.side_effect = inspect
            gate.wrap(raw).order(name="#1", sz=50, limit_px=1, is_buy=True)
            self.assertFalse(json.loads(path.read_text())["pending_cloids"])

    def test_transient_windows_replace_and_read_locks_retry_but_fail_closed(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "status.json"
            import os
            replace = os.replace
            with patch("zeus.safety.os.replace") as mocked, \
                    patch("zeus.safety.time.sleep"):
                calls = [0]
                def retry_replace(source, target):
                    calls[0] += 1
                    if calls[0] == 1: raise PermissionError("sharing")
                    return replace(source, target)
                mocked.side_effect = retry_replace
                atomic_json(path, {"phase": "running"})
            with patch("pathlib.Path.read_bytes", side_effect=[PermissionError("sharing"), b'{"phase":"running"}']), \
                    patch("zeus.safety.time.sleep"):
                self.assertEqual(read_json(path)["phase"], "running")
            with patch("pathlib.Path.read_bytes", side_effect=PermissionError("denied")), patch("zeus.safety.time.sleep"):
                with self.assertRaises(PermissionError): read_json(path)

    def test_pending_reservation_reduces_next_markets_buy_budget(self):
        import test_outcome_execution as outcome_tests
        with tempfile.TemporaryDirectory() as root:
            bot, state = outcome_tests.OutcomeExecutionTests().funding_fixture()
            bot.funds = OutcomeFunds(root, "offline-account", "one")
            raw = Mock()
            raw.order.side_effect = TimeoutError("response lost")
            with self.assertRaises(TimeoutError):
                bot.funds.wrap(raw).order(is_buy=True, sz=80, limit_px=1, cloid="pending")
            # Account reports 105 USDC, but 80 is still reserved by another request.
            request, _ = bot.desired_spot_no_order(state)
            self.assertIsNone(bot._prepare_spot_request(state, request))
            self.assertTrue(bot.running)
            bot.exchange.order.assert_not_called()

    def test_server_reconciliation_frees_only_confirmed_pending_funds(self):
        from zeus import server
        with tempfile.TemporaryDirectory() as root:
            funds = OutcomeFunds(root, "offline-account", "one")
            raw = Mock()
            raw.order.side_effect = TimeoutError("response lost")
            with self.assertRaises(TimeoutError): funds.wrap(raw).order(is_buy=True, sz=50, limit_px=1, cloid="pending")
            info = Mock()
            info.post.return_value = {"status": "order", "order": {"status": "canceled", "order": {
                "coin": "#1", "cloid": "pending", "oid": 123}}}
            info.spot_user_state.return_value = {"balances": []}
            info.frontend_open_orders.return_value = []
            record = {"account_kind": "outcome", "account": "offline-account", "symbols": ["#1"],
                      "funds_root": root, "session_id": "one", "pending_cloids": ["pending"]}
            with patch("hyperliquid.info.Info", return_value=info), patch.dict(server.os.environ, {"WALLET_ADDRESS_OUTCOME": "offline-account"}):
                server.BotManager(state_dir=False)._verify_session_flat(record)
            self.assertEqual(funds.reserved_usdc(), 0)


if __name__ == "__main__":
    unittest.main()
