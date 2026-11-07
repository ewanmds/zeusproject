"""Local fake account tests for modification of canceled/filled BBO orders."""
import unittest
from unittest.mock import Mock, patch

import test_reduce_only_preflight as fixtures


MISSING = {"error": "Cannot modify canceled or filled order"}


def response(statuses):
    return {"status": "ok", "response": {"data": {"statuses": statuses}}}


class MissingModifyRaceTests(unittest.TestCase):
    position = fixtures.ReduceOnlyPreflightTests.position
    request = fixtures.ReduceOnlyPreflightTests.request

    def setUp(self):
        fixtures.ReduceOnlyPreflightTests.setUp(self)
        self.bot.position = 0
        self.position(0)
        self.bot.last_quote_ts = self.bot.last_action_ts = 0
        self.bot.orders = {10: {"side": "B", "px": 99, "sz": 1, "ro": False},
                           20: {"side": "A", "px": 100.1, "sz": 1, "ro": False}}
        self.bot.safety.owned.update((10, 20))
        self.bot.queue_init = {10: 7, 20: 11}
        self.bot.queue_ahead = {10: 4, 20: 8}
        self.other = {"coin": "io:ANTH", "oid": 20, "side": "A", "sz": "1", "limitPx": "100.1"}
        self.bot.info.frontend_open_orders.return_value = [self.other]
        self.raw.bulk_modify_orders_new.return_value = response([MISSING])
        self.bot.exchange = self.bot.safety.wrap(self.raw,
            reconcile_cancel=self.bot._reconcile_missing_cancels,
            reconcile_reduce_only=self.bot._reconcile_reduce_only,
            reconcile_post_only=self.bot._reconcile_post_only,
            reconcile_modify=self.bot._reconcile_missing_modifies)
        self.bot.compute_intent = Mock(return_value=((100, 1, False), (100.1, 1, False), "BBO bid/ask"))

    def modify(self):
        return self.bot.modify_quotes([(10, self.request(buy=True, size=1, ro=False))])

    def test_users_exact_rejection_removes_only_confirmed_missing_order(self):
        self.assertTrue(self.modify())
        self.assertTrue(self.bot.running)
        self.assertFalse(self.bot.safety.stopped.is_set())
        self.assertFalse(self.bot.safety.error)
        self.assertFalse(self.bot.last_error)
        self.assertFalse(self.bot.safety.pending_cloids)
        self.assertEqual(set(self.bot.orders), {20})
        self.assertNotIn(10, self.bot.queue_ahead)
        self.assertEqual(self.bot.queue_ahead[20], 8)
        self.assertIn("Order already filled or cancelled", self.bot.quote_warning)
        self.raw.bulk_modify_orders_new.assert_called_once()
        self.raw.bulk_cancel.assert_not_called()

    def test_retry_is_a_new_quote_after_delay_not_a_modify_of_missing_oid(self):
        with patch("zeus.bots.order_book.time.time", return_value=1000) as clock:
            self.assertTrue(self.modify())
            clock.return_value = 1000.2
            self.bot.apply_intent()
            self.raw.bulk_orders.assert_not_called()
            clock.return_value = 1001.1
            self.bot.apply_intent()
        self.raw.bulk_modify_orders_new.assert_called_once()
        self.raw.bulk_orders.assert_called_once()
        sent = self.raw.bulk_orders.call_args.args[0]
        self.assertEqual(len(sent), 1)
        self.assertTrue(sent[0]["is_buy"])
        self.assertIn(20, self.bot.orders)
        self.raw.bulk_cancel.assert_not_called()

    def test_fill_updates_position_before_another_quote(self):
        self.position(1)
        self.assertTrue(self.modify())
        self.assertEqual(self.bot.position, 1)
        self.assertTrue(self.bot.position_dirty)
        self.assertTrue(self.bot.running)
        self.raw.bulk_orders.assert_not_called()

    def test_mixed_batch_keeps_successful_replacement(self):
        self.raw.bulk_modify_orders_new.return_value = response([{"resting": {"oid": 11}}, MISSING])
        self.bot.info.frontend_open_orders.return_value = [{"coin": "io:ANTH", "oid": 11,
            "side": "B", "sz": "1", "limitPx": "100"}]
        self.assertTrue(self.bot.modify_quotes([(10, self.request(buy=True, size=1, ro=False)),
                                               (20, self.request(size=1, ro=False))]))
        self.assertTrue(self.bot.running)
        self.assertEqual(set(self.bot.orders), {11})
        self.raw.bulk_cancel.assert_not_called()

    def test_order_still_open_is_not_falsely_dropped(self):
        self.bot.info.frontend_open_orders.return_value = [self.other, {"coin": "io:ANTH", "oid": 10,
            "side": "B", "sz": "1", "limitPx": "99"}]
        self.assertFalse(self.modify())
        self.assertTrue(self.bot.safety.stopped.is_set())
        self.assertIn(10, self.bot.orders)

    def test_unavailable_snapshot_does_not_allow_blind_replacement(self):
        self.bot.info.frontend_open_orders.side_effect = TimeoutError("offline")
        self.assertFalse(self.modify())
        self.assertTrue(self.bot.safety.stopped.is_set())
        self.raw.bulk_orders.assert_not_called()

    def test_other_rejection_in_mixed_batch_is_not_ignored(self):
        self.raw.bulk_modify_orders_new.return_value = response([MISSING, {"error": "permission denied"}])
        self.assertFalse(self.bot.modify_quotes([(10, self.request(buy=True, ro=False)),
                                                (20, self.request(ro=False))]))
        self.assertTrue(self.bot.safety.stopped.is_set())
        self.bot.info.frontend_open_orders.assert_not_called()

    def test_manual_stop_during_sync_remains_effective(self):
        def stopped(*args, **kwargs):
            self.bot.safety.stop()
            return [self.other]
        self.bot.info.frontend_open_orders.side_effect = stopped
        self.assertTrue(self.modify())
        self.assertFalse(self.bot.running)
        self.bot.place_quotes([self.request()])
        self.raw.bulk_orders.assert_not_called()


if __name__ == "__main__": unittest.main()
