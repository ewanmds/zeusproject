"""Local fake exchange coverage of BBO/MID cancel/fill races."""
import unittest
from unittest.mock import Mock

import test_reduce_only_preflight as fixtures


MISSING = {"error": "Order was never placed, already canceled, or filled. asset=200001"}


class BboCancelRaceTests(unittest.TestCase):
    position = fixtures.ReduceOnlyPreflightTests.position
    request = fixtures.ReduceOnlyPreflightTests.request

    def setUp(self):
        fixtures.ReduceOnlyPreflightTests.setUp(self)
        self.bot.order_cancellations = 0
        self.bot.last_quote_ts = 0
        self.bot.orders = {1: {"side": "B", "px": 100, "sz": 1, "ro": False},
                           2: {"side": "A", "px": 100.1, "sz": 1, "ro": False}}
        self.bot.safety.owned.update((1, 2, 3))
        self.bot.exchange = self.bot.safety.wrap(self.raw,
            reconcile_cancel=self.bot._reconcile_missing_cancels,
            reconcile_reduce_only=self.bot._reconcile_reduce_only,
            reconcile_post_only=self.bot._reconcile_post_only)
        self.raw.bulk_cancel.return_value = {"status": "ok", "response": {"data": {"statuses": ["success", MISSING]}}}

    def test_users_exact_mixed_batch_does_not_stop_or_resend(self):
        self.position(0)
        self.assertTrue(self.bot.cancel_oids([1, 2]))
        self.assertTrue(self.bot.running)
        self.assertFalse(self.bot.safety.stopped.is_set())
        self.assertFalse(self.bot.safety.error)
        self.assertFalse(self.bot.last_error)
        self.assertFalse(self.bot.orders)
        self.assertEqual(self.bot.position, 0)
        self.bot.cancel_all()
        self.raw.bulk_cancel.assert_called_once()
        self.raw.bulk_orders.assert_not_called()
        self.raw.order.assert_not_called()

    def test_missing_cancel_refreshes_partially_filled_position(self):
        self.position(0.4)
        self.assertTrue(self.bot.cancel_oids([1, 2]))
        self.assertEqual(self.bot.position, 0.4)
        self.assertTrue(self.bot.running)

    def test_fill_between_account_reads_defers_quotes_instead_of_stopping(self):
        before = self.bot.info.user_state.return_value
        self.position(0)
        after = self.bot.info.user_state.return_value
        self.bot.info.user_state.side_effect = [before, after]
        self.assertTrue(self.bot.cancel_oids([1, 2]))
        self.assertTrue(self.bot.running)
        self.assertFalse(self.bot.safety.stopped.is_set())
        self.assertEqual(self.bot.position, 0)
        self.assertTrue(self.bot.position_dirty)
        self.raw.bulk_orders.assert_not_called()

    def test_other_owned_resting_quote_is_preserved(self):
        self.bot.info.frontend_open_orders.return_value = [{"coin": "io:ANTH", "oid": 3,
            "side": "A", "sz": "0.4", "limitPx": "100.1"}]
        self.assertTrue(self.bot.cancel_oids([1, 2]))
        self.assertEqual(set(self.bot.orders), {3})
        self.raw.bulk_cancel.assert_called_once()

    def test_order_still_open_is_not_falsely_confirmed_absent(self):
        self.bot.info.frontend_open_orders.return_value = [{"coin": "io:ANTH", "oid": 2,
            "side": "A", "sz": "0.4", "limitPx": "100.1"}]
        self.assertFalse(self.bot.cancel_oids([1, 2]))
        self.assertTrue(self.bot.safety.stopped.is_set())
        self.raw.bulk_cancel.assert_called_once()

    def test_unavailable_snapshot_is_not_treated_as_empty(self):
        self.bot.info.frontend_open_orders.side_effect = TimeoutError("offline")
        self.assertFalse(self.bot.cancel_oids([1, 2]))
        self.assertTrue(self.bot.safety.stopped.is_set())

    def test_other_rejection_in_batch_retains_interlock(self):
        self.raw.bulk_cancel.return_value["response"]["data"]["statuses"] = ["success", {"error": "permission denied"}]
        self.assertFalse(self.bot.cancel_oids([1, 2]))
        self.assertTrue(self.bot.safety.stopped.is_set())
        self.bot.info.frontend_open_orders.assert_not_called()

    def test_stop_during_verification_cannot_restart_session(self):
        def stopped(*args, **kwargs):
            self.bot.safety.stop()
            return []
        self.bot.info.frontend_open_orders.side_effect = stopped
        self.assertTrue(self.bot.cancel_oids([1, 2]))
        self.assertFalse(self.bot.running)
        self.bot.place_quotes([self.request()])
        self.raw.bulk_orders.assert_not_called()


if __name__ == "__main__": unittest.main()
