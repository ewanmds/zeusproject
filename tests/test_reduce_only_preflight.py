"""Local fake account/order tests for BBO/MID reduce-only races."""
import importlib
import os
import threading
import unittest
from unittest.mock import Mock, patch

from zeus.safety import SafetyLatch


def ack(status):
    return {"status": "ok", "response": {"data": {"statuses": [status]}}}


class ReduceOnlyPreflightTests(unittest.TestCase):
    def setUp(self):
        with patch.dict(os.environ, {"PRIVATE_KEY": "1" * 64, "WALLET_ADDRESS": "0x" + "1" * 40}):
            self.mod = importlib.import_module("zeus.bots.order_book")
        settings = patch.multiple(self.mod, SYMBOL="io:ANTH", STRATEGY="bbo", SZ_DECIMALS=3, MIN_SIZE=0.001)
        settings.start()
        self.addCleanup(settings.stop)
        bot = self.mod.OrderBookBot.__new__(self.mod.OrderBookBot)
        self.bot = bot
        bot.live, bot.running = True, True
        bot.lock, bot.api_lock, bot.wake = threading.Lock(), threading.Lock(), threading.Event()
        bot.safety = SafetyLatch(lambda: setattr(bot, "running", False))
        bot.safety.phase = "running"
        bot.info, self.raw = Mock(), Mock()
        bot.exchange = bot.safety.wrap(self.raw, reconcile_reduce_only=bot._reconcile_reduce_only)
        self.raw.bulk_orders.return_value = ack({"resting": {"oid": 123}})
        self.raw.bulk_modify_orders_new.return_value = ack({"resting": {"oid": 124}})
        bot.position = 2
        bot.position_revision, bot.position_dirty = 0, False
        bot.pos_since = bot.last_fill_ts = bot.spot_usdc = 0
        bot.best_bid, bot.best_ask, bot.mid = 100, 100.1, 100.05
        bot.bank_until = bot._hard_exit_at = 0
        bot.orders, bot.queue_init, bot.queue_ahead = {}, {}, {}
        bot.last_intent, bot.last_error, bot.last_action, bot.force_quote = None, "", "", False
        bot.order_placements = bot.order_modifications = bot.order_requests = 0
        bot.signal = Mock()
        bot.signal.level_size.return_value = 0
        bot.dex = "io"
        bot.info.spot_user_state.return_value = {"balances": []}
        bot.info.open_orders.return_value = []
        bot.info.frontend_open_orders.return_value = []
        self.position(1)

    def position(self, value):
        self.bot.info.user_state.return_value = {"assetPositions": [
            {"position": {"coin": "ANTH", "szi": str(value), "entryPx": "100", "unrealizedPnl": "0"}}
        ] if value else [], "marginSummary": {"accountValue": "100"}}

    def request(self, buy=False, size=2, ro=True):
        return {"coin": "io:ANTH", "is_buy": buy, "sz": size, "limit_px": 100,
                "order_type": {"limit": {"tif": "Alo"}}, "reduce_only": ro}

    def test_already_flat_skips_old_close_without_removing_reduce_only(self):
        self.position(0)
        request = self.request()
        self.bot.place_quotes([request])
        self.assertEqual(self.bot.position, 0)
        self.assertTrue(request["reduce_only"])
        self.assertTrue(self.bot.running)
        self.raw.bulk_orders.assert_not_called()

    def test_partial_fill_caps_close_to_actual_remaining_position(self):
        self.position(0.4)
        self.bot.place_quotes([self.request()])
        sent = self.raw.bulk_orders.call_args.args[0][0]
        self.assertEqual(sent["sz"], 0.4)
        self.assertFalse(sent["is_buy"])
        self.assertTrue(sent["reduce_only"])
        self.assertEqual(sent["coin"], "io:ANTH")
        self.assertTrue(self.bot.running)

    def test_reversed_position_skips_wrong_direction_exit(self):
        self.position(-0.4)
        self.bot.place_quotes([self.request()])
        self.raw.bulk_orders.assert_not_called()
        self.assertEqual(self.bot.position, -0.4)

    def test_short_position_can_only_be_closed_with_a_buy(self):
        self.position(-0.4)
        self.bot.place_quotes([self.request(buy=True)])
        sent = self.raw.bulk_orders.call_args.args[0][0]
        self.assertEqual(sent["sz"], 0.4)
        self.assertTrue(sent["is_buy"] and sent["reduce_only"])

    def test_resting_closes_are_subtracted_from_exit_capacity(self):
        self.bot.info.open_orders.return_value = [{"coin": "io:ANTH", "oid": 99, "side": "A", "sz": "0.7"}]
        self.bot.place_quotes([self.request()])
        self.assertEqual(self.raw.bulk_orders.call_args.args[0][0]["sz"], 0.3)

    def test_fully_reserved_position_does_not_get_another_close(self):
        self.bot.info.open_orders.return_value = [{"coin": "io:ANTH", "oid": 99, "side": "A", "sz": "1"}]
        self.bot.place_quotes([self.request()])
        self.raw.bulk_orders.assert_not_called()

    def test_replacement_excludes_old_order_and_caps_new_quantity(self):
        self.position(0.4)
        self.bot.orders[99] = {"side": "A", "px": 100.1, "sz": 1, "ro": True}
        self.bot.info.open_orders.return_value = [{"coin": "io:ANTH", "oid": 99, "side": "A", "sz": "1"}]
        self.assertTrue(self.bot.modify_quotes([(99, self.request())]))
        self.assertEqual(self.raw.bulk_modify_orders_new.call_args.args[0][0]["order"]["sz"], 0.4)
        self.assertEqual(self.bot.orders[124]["sz"], 0.4)

    def test_fill_during_preflight_invalidates_the_request(self):
        def fill(*args, **kwargs):
            self.bot.position_revision += 1
            self.bot.position_dirty = True
            self.bot.position = 0
            return []
        self.bot.info.open_orders.side_effect = fill
        self.bot.place_quotes([self.request()])
        self.raw.bulk_orders.assert_not_called()

    def test_rest_snapshot_does_not_overwrite_newer_fill(self):
        old = self.bot.info.user_state.return_value
        def fill(*args, **kwargs):
            self.bot.position_revision += 1
            self.bot.position_dirty = True
            self.bot.position = 0
            return old
        self.bot.info.user_state.side_effect = fill
        self.assertFalse(self.bot.sync_position())
        self.assertEqual(self.bot.position, 0)

    def test_known_reduce_only_race_reconciles_without_stopping_or_resending(self):
        def rejected(*args, **kwargs):
            self.position(0)
            return ack({"error": "Reduce only order would increase position. asset=200001"})
        self.raw.bulk_orders.side_effect = rejected
        self.bot.place_quotes([self.request()])
        self.assertEqual(self.bot.position, 0)
        self.assertTrue(self.bot.running)
        self.assertFalse(self.bot.safety.stopped.is_set())
        self.assertFalse(self.bot.safety.pending_cloids)
        self.assertFalse(self.bot.orders)
        self.raw.bulk_orders.assert_called_once()

    def test_same_error_on_an_opening_order_is_not_ignored(self):
        self.bot.position = 0
        self.raw.bulk_orders.return_value = ack({"error": "Reduce only order would increase position. asset=200001"})
        self.bot.place_quotes([self.request(ro=False)])
        self.assertTrue(self.bot.safety.stopped.is_set())

    def test_real_rejection_or_unknown_account_still_blocks_execution(self):
        self.raw.bulk_orders.return_value = ack({"error": "permission denied"})
        self.bot.place_quotes([self.request()])
        self.assertTrue(self.bot.safety.stopped.is_set())

    def test_reduce_only_rejection_with_unverifiable_position_still_blocks(self):
        def rejected(*args, **kwargs):
            self.bot.info.user_state.side_effect = TimeoutError("account offline")
            return ack({"error": "Reduce only order would increase position. asset=200001"})
        self.raw.bulk_orders.side_effect = rejected
        self.bot.place_quotes([self.request()])
        self.assertTrue(self.bot.safety.stopped.is_set())
        self.raw.bulk_orders.assert_called_once()

    def test_mixed_accepted_and_rejected_closes_keep_the_resting_order(self):
        order = {"coin": "io:ANTH", "oid": 123, "side": "A", "sz": "0.5", "limitPx": "100.1"}
        def partial(*args, **kwargs):
            self.position(0.5)
            self.bot.info.frontend_open_orders.return_value = [order]
            return {"status": "ok", "response": {"data": {"statuses": [
                {"resting": {"oid": 123}}, {"error": "Reduce only order would increase position. asset=200001"}]}}}
        self.raw.bulk_orders.side_effect = partial
        self.bot.place_quotes([self.request(size=0.5), self.request(size=0.5)])
        self.assertTrue(self.bot.running)
        self.assertEqual(self.bot.orders[123]["sz"], 0.5)
        self.assertEqual(self.bot.position, 0.5)
        self.raw.bulk_orders.assert_called_once()

    def test_unavailable_orders_never_become_zero_reserved_position(self):
        self.bot.info.open_orders.side_effect = TimeoutError("offline")
        self.bot.place_quotes([self.request()])
        self.assertTrue(self.bot.safety.stopped.is_set())
        self.raw.bulk_orders.assert_not_called()

    def test_immediate_fill_ack_requires_an_account_sync_before_next_quote(self):
        self.raw.bulk_orders.return_value = ack({"filled": {"oid": 123, "totalSz": "1", "avgPx": "100"}})
        self.bot.place_quotes([self.request()])
        self.assertTrue(self.bot.position_dirty)
        self.assertTrue(self.bot.force_quote)

    def test_stop_during_preflight_still_blocks_submission(self):
        def stop(*args, **kwargs):
            self.bot.safety.stop()
            return []
        self.bot.info.open_orders.side_effect = stop
        self.bot.place_quotes([self.request()])
        self.raw.bulk_orders.assert_not_called()
        self.assertFalse(self.bot.running)


if __name__ == "__main__": unittest.main()
