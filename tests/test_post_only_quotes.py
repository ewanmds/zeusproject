"""Expected ALO rejection races must not kill a live BBO/MID session."""
import unittest
from unittest.mock import Mock, patch

import test_reduce_only_preflight as fixtures


REJECT = {"error": "Post only order would have immediately matched, bbo was 1738.6@1738.9. asset=200001"}
OID = 592672244205


def response(statuses):
    return {"status": "ok", "response": {"data": {"statuses": statuses}}}


class PostOnlyQuoteTests(unittest.TestCase):
    position = fixtures.ReduceOnlyPreflightTests.position
    request = fixtures.ReduceOnlyPreflightTests.request

    def setUp(self):
        fixtures.ReduceOnlyPreflightTests.setUp(self)
        self.bot.position = 0
        self.position(0)
        self.bot.last_quote_ts = self.bot.last_action_ts = 0
        self.bot.exchange = self.bot.safety.wrap(self.raw,
            reconcile_reduce_only=self.bot._reconcile_reduce_only,
            reconcile_post_only=self.bot._reconcile_post_only)
        self.bot.compute_intent = Mock(return_value=((100, 1, False), (100.1, 1, False), "BBO bid/ask"))

    def mixed_batch(self):
        self.raw.bulk_orders.return_value = response([{"resting": {"oid": OID,
            "cloid": "0x928762edb46f43f4bc877f05e98301e5"}}, REJECT])
        self.bot.place_quotes([self.request(buy=True, size=1, ro=False), self.request(size=1, ro=False)])

    def test_users_exact_batch_keeps_accepted_quote_and_session_active(self):
        self.mixed_batch()
        self.assertTrue(self.bot.running)
        self.assertFalse(self.bot.safety.stopped.is_set())
        self.assertFalse(self.bot.safety.error)
        self.assertFalse(self.bot.last_error)
        self.assertFalse(self.bot.safety.pending_cloids)
        self.assertEqual(self.bot.orders[OID]["side"], "B")
        self.assertIn("Post-only", self.bot.quote_warning)
        self.raw.bulk_orders.assert_called_once()
        self.raw.bulk_cancel.assert_not_called()
        self.raw.order.assert_not_called()

    def test_only_rejected_side_retries_after_delay_at_fresh_price(self):
        with patch("zeus.bots.order_book.time.time", return_value=1000) as clock:
            self.mixed_batch()
            clock.return_value = 1000.2
            self.bot.apply_intent()
            self.raw.bulk_orders.assert_called_once()
            clock.return_value = 1001.1
            self.bot.best_ask = 100.2
            self.bot.compute_intent.return_value = ((100, 1, False), (100.2, 1, False), "BBO bid/ask")
            self.raw.bulk_orders.return_value = response([{"resting": {"oid": 456}}])
            self.bot.apply_intent()
        self.assertEqual(self.raw.bulk_orders.call_count, 2)
        retry = self.raw.bulk_orders.call_args.args[0]
        self.assertEqual(len(retry), 1)
        self.assertFalse(retry[0]["is_buy"])
        self.assertEqual(retry[0]["limit_px"], 100.2)
        self.assertIn(OID, self.bot.orders)
        self.assertIn(456, self.bot.orders)
        self.assertFalse(self.bot.quote_warning)
        self.raw.bulk_cancel.assert_not_called()
        self.raw.bulk_modify_orders_new.assert_not_called()

    def test_repeated_rejections_back_off_without_open_cancel_spam(self):
        with patch("zeus.bots.order_book.time.time", return_value=1000) as clock:
            self.mixed_batch()
            for attempt, wait in enumerate((2, 4, 8, 8), start=2):
                clock.return_value = self.bot.quote_retry_after["A"] + 0.01
                self.raw.bulk_orders.return_value = response([REJECT])
                self.bot.apply_intent()
                self.assertAlmostEqual(self.bot.quote_retry_after["A"] - clock.return_value, wait)
                count = self.raw.bulk_orders.call_count
                for _ in range(10): self.bot.apply_intent()
                self.assertEqual(self.raw.bulk_orders.call_count, count)
        self.assertTrue(self.bot.running)
        self.assertIn(OID, self.bot.orders)
        self.raw.bulk_cancel.assert_not_called()

    def test_rejected_modify_preserves_accepted_side_and_observed_old_order(self):
        self.bot.orders = {10: {"side": "B", "px": 99, "sz": 1, "ro": False},
                           20: {"side": "A", "px": 101, "sz": 1, "ro": False}}
        self.bot.safety.owned.update((10, 20))
        self.bot.info.open_orders.return_value = [
            {"coin": "io:ANTH", "oid": 11, "side": "B", "limitPx": "100", "sz": "1"},
            {"coin": "io:ANTH", "oid": 20, "side": "A", "limitPx": "101", "sz": "1"}]
        self.raw.bulk_modify_orders_new.return_value = response([{"resting": {"oid": 11}}, REJECT])
        self.assertTrue(self.bot.modify_quotes([(10, self.request(buy=True, size=1, ro=False)),
                                               (20, self.request(size=1, ro=False))]))
        self.assertEqual(set(self.bot.orders), {11, 20})
        self.assertTrue(self.bot.running)
        self.assertFalse(self.bot.last_error)
        self.raw.bulk_cancel.assert_not_called()

    def test_post_only_error_is_not_accepted_for_an_ioc_request(self):
        request = self.request(buy=True, ro=False)
        request["order_type"]["limit"]["tif"] = "Ioc"
        self.raw.bulk_orders.return_value = response([REJECT])
        with self.assertRaises(RuntimeError): self.bot.exchange.bulk_orders([request])
        self.assertTrue(self.bot.safety.stopped.is_set())

    def test_other_error_in_same_batch_still_blocks_unknown_execution(self):
        self.raw.bulk_orders.return_value = response([REJECT, {"error": "permission denied"}])
        self.bot.place_quotes([self.request(buy=True, ro=False), self.request(ro=False)])
        self.assertTrue(self.bot.safety.stopped.is_set())

    def test_stop_is_not_reversed_by_a_post_only_callback(self):
        self.bot.safety.stop()
        self.bot.place_quotes([self.request(buy=True, ro=False)])
        self.raw.bulk_orders.assert_not_called()
        self.assertFalse(self.bot.running)

    def test_risk_pull_still_wins_over_post_only_backoff(self):
        self.mixed_batch()
        self.bot.compute_intent.return_value = (None, None, "risk pause")
        self.bot.cancel_oids = Mock(return_value=True)
        self.bot.apply_intent()
        self.bot.cancel_oids.assert_called_once_with([OID])


if __name__ == "__main__": unittest.main()
