"""Offline coverage: exchange calls and bot startup are always mocked."""
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from zeus import server
from zeus.bots.worldcup import BotConfig, HyperliquidSpreadBot, MarketSpec, MarketState, SessionStats


class OutcomeExecutionTests(unittest.TestCase):
    def test_api_modes_and_legacy_default(self):
        server.app.dependency_overrides[server._require_api_token] = lambda: None
        try:
            client = TestClient(server.app)
            with patch.object(server.manager, "start_worldcup") as start:
                for payload, expected in [({}, "taker"), ({"exec_mode": "maker"}, "maker"), ({"exec_mode": "taker"}, "taker")]:
                    self.assertEqual(client.post("/api/outcomes/start", json=payload).status_code, 200)
                    self.assertEqual(start.call_args.args[0].exec_mode, expected)
                    self.assertEqual(start.call_args.args[0].maker_spread_pct, 0.005)
                self.assertEqual(client.post("/api/outcomes/start", json={"exec_mode": "maker", "maker_spread_pct": 0.007}).status_code, 200)
                self.assertEqual(start.call_args.args[0].maker_spread_pct, 0.007)
                start.reset_mock()
                self.assertEqual(client.post("/api/outcomes/start", json={"exec_mode": "invalid"}).status_code, 422)
                start.assert_not_called()
                for invalid in (-1, None, "nan", "inf", "bad"):
                    self.assertEqual(client.post("/api/outcomes/start", json={"exec_mode": "maker", "maker_spread_pct": invalid}).status_code, 422)
                start.assert_not_called()
        finally:
            server.app.dependency_overrides.pop(server._require_api_token, None)

    def configured_bot(self, mode, maker_spread_pct=100):
        config_factory = Mock(wraps=BotConfig)
        catalog = SimpleNamespace(all_market_keys=lambda: ["#14881"])
        manager = server.BotManager(state_dir=False)
        with patch.object(server, "_safe_import", side_effect=[SimpleNamespace(BotConfig=config_factory), catalog]), \
             patch.object(server.threading, "Thread"), \
             patch.dict(server.os.environ, {"WALLET_ADDRESS_OUTCOME": "offline-test", "PRIVATE_KEY_OUTCOME": "dummy"}):
            manager.start_worldcup(server.WorldcupStartRequest(exec_mode=mode, maker_spread_pct=maker_spread_pct))
        cfg = BotConfig(**config_factory.call_args.kwargs)
        self.assertEqual(cfg.exec_mode, mode)
        self.assertEqual(cfg.maker_offset_pct, 0)
        self.assertEqual(cfg.maker_spread_pct, maker_spread_pct)
        self.assertEqual(manager.get_worldcup_stats()["config"]["exec_mode"], mode)
        bot = HyperliquidSpreadBot.__new__(HyperliquidSpreadBot)
        from zeus.safety import SafetyLatch
        bot.running = True
        bot.safety = SafetyLatch(lambda: setattr(bot, "running", False))
        bot.cfg = cfg
        bot.stats = SessionStats()
        bot.lock = threading.RLock()
        bot.exchange = Mock()
        bot.can_send_action = Mock(return_value=True)
        bot.cancel_all = Mock()
        bot.states = {}
        bot.info = Mock()
        bot.wake = threading.Event()
        return bot

    def state(self, position=0):
        state = MarketState(MarketSpec("#14881", 0), position=position)
        state.update_bbo(0.4, 0.401)
        return state

    def test_maker_buy_and_sell_at_touch(self):
        bot = self.configured_bot("maker")
        for position, expected_price, buy in [(0, 0.4, True), (20, 0.401, False)]:
            req, _ = bot.desired_spot_no_order(self.state(position))
            self.assertEqual(req["limit_px"], expected_price)
            self.assertEqual(req["is_buy"], buy)
            self.assertEqual(req["order_type"], {"limit": {"tif": "Alo"}})

    def test_taker_crosses_with_existing_slippage(self):
        bot = self.configured_bot("taker")
        self.assertIn("spread", bot.risk_block_reason(self.state()))
        for position in [0, 20]:
            req, _ = bot.desired_spot_no_order(self.state(position))
            self.assertEqual(req["order_type"], {"limit": {"tif": "Ioc"}})
            if position:
                self.assertLess(req["limit_px"], 0.4)
            else:
                self.assertGreater(req["limit_px"], 0.401)

    def test_manual_percent_spread_tightens_both_prices(self):
        bot = self.configured_bot("maker", maker_spread_pct=0.05)
        state = self.state()
        state.update_bbo(0.399, 0.401)
        buy, _ = bot.desired_spot_no_order(state)
        state.position = 20
        sell, _ = bot.desired_spot_no_order(state)
        self.assertAlmostEqual(buy["limit_px"], 0.3999)
        self.assertAlmostEqual(sell["limit_px"], 0.4001)
        self.assertAlmostEqual((sell["limit_px"] - buy["limit_px"]) / state.mid * 100, 0.05)
        self.assertEqual(buy["order_type"], {"limit": {"tif": "Alo"}})
        self.assertEqual(sell["order_type"], {"limit": {"tif": "Alo"}})

    def test_tiny_target_respects_tick_and_never_crosses(self):
        bot = self.configured_bot("maker", maker_spread_pct=0.005)
        state = self.state()
        state.update_bbo(0.4999, 0.5)
        self.assertEqual(bot.maker_quote_px(state, True), state.bid)
        self.assertEqual(bot.maker_quote_px(state, False), state.ask)
        bot.cfg.maker_spread_pct = 0
        self.assertLess(bot.maker_quote_px(state, True), state.ask)
        self.assertGreater(bot.maker_quote_px(state, False), state.bid)

    def test_inside_quote_does_not_chase_its_own_bbo(self):
        bot = self.configured_bot("maker", maker_spread_pct=0.05)
        for position, buy in [(0, True), (26, False)]:
            state = self.state(position)
            state.update_bbo(0.399, 0.401)
            price = bot.maker_quote_px(state, buy)
            if buy:
                state.bid_oid, state.quoted_bid = 123, price
                state.update_bbo(price, 0.401)
            else:
                state.ask_oid, state.quoted_ask = 123, price
                state.update_bbo(0.399, price)
            for _ in range(20):
                bot.place_or_requote(state)
        bot.cancel_all.assert_not_called()
        bot.exchange.order.assert_not_called()

    def test_maker_preserves_queue_and_cancels_before_requote(self):
        bot = self.configured_bot("maker")
        state = self.state()
        state.bid_oid, state.quoted_bid = 123, state.bid
        bot.place_or_requote(state)
        bot.cancel_all.assert_not_called()
        bot.exchange.order.assert_not_called()
        state.update_bbo(0.4001, 0.401)
        bot.place_or_requote(state)
        bot.cancel_all.assert_called_once_with("maker requote", [state.spec.symbol])
        bot.exchange.order.assert_not_called()

    def test_maker_retains_stale_feed_and_inventory_guards(self):
        bot = self.configured_bot("maker")
        state = self.state()
        self.assertIsNone(bot.risk_block_reason(state))  # wide spread allowed
        state.last_book_ts = time.time() - 10
        self.assertIn("stale", bot.risk_block_reason(state))
        state = self.state()
        bot.cfg.max_position_usd = 0
        self.assertIsNone(bot.desired_spot_no_order(state)[0])

    def sync_fixture(self):
        bot = self.configured_bot("maker")
        state = self.state(position=26)
        state.spec.balance_coin = "+14881"
        state.ask_oid, state.quoted_ask = 123, state.ask
        bot.states = {state.spec.symbol: state}
        bot.info.spot_user_state.return_value = {"balances": [{"coin": "+14881", "total": "26", "hold": "26"}]}
        bot.info.open_orders.return_value = [{"coin": state.spec.symbol, "oid": 123, "side": "A", "limitPx": str(state.ask)}]
        return bot, state

    def test_held_sell_inventory_does_not_flip_to_buy_across_syncs(self):
        bot, state = self.sync_fixture()
        for _ in range(20):
            bot.sync_account()
            bot.place_or_requote(state)
            self.assertEqual(state.position, 26)
            self.assertEqual(state.ask_oid, 123)
        bot.cancel_all.assert_not_called()
        bot.exchange.order.assert_not_called()

    def test_rest_order_adoption_restores_price(self):
        bot, state = self.sync_fixture()
        state.ask_oid, state.quoted_ask = None, 0
        bot.sync_account()
        bot.place_or_requote(state)
        self.assertEqual(state.quoted_ask, state.ask)
        bot.cancel_all.assert_not_called()
        bot.exchange.order.assert_not_called()

    def test_failed_open_orders_keeps_inventory_and_order(self):
        bot, state = self.sync_fixture()
        bot.info.open_orders.side_effect = TimeoutError("offline")
        bot.info.spot_user_state.return_value = {"balances": []}
        bot.sync_account()
        self.assertEqual(state.position, 26)
        self.assertEqual(state.ask_oid, 123)
        bot.place_or_requote(state)
        bot.exchange.order.assert_not_called()

    def test_ws_fill_overtaking_rest_snapshot_is_not_overwritten(self):
        bot, state = self.sync_fixture()
        def concurrent_fill(*args, **kwargs):
            bot._record_fills([{"coin": state.spec.symbol, "side": "A", "sz": "26", "px": "0.401", "tid": "sale"}])
            return []
        bot.info.open_orders.side_effect = concurrent_fill
        bot.sync_account()
        self.assertEqual(state.position, 0)
        self.assertTrue(state.reconcile_required)

    def test_terminal_fill_waits_for_account_before_another_sale(self):
        bot, state = self.sync_fixture()
        bot.on_order_updates({"data": [{"order": {"coin": state.spec.symbol, "oid": 123}, "status": "filled"}]})
        bot.place_or_requote(state)
        self.assertIsNone(state.ask_oid)
        self.assertTrue(state.reconcile_required)
        bot.exchange.order.assert_not_called()
        bot.info.spot_user_state.return_value = {"balances": []}
        bot.info.open_orders.return_value = []
        bot.sync_account()
        self.assertEqual(state.position, 0)
        self.assertFalse(state.reconcile_required)

    def test_partial_fill_is_deduplicated_and_keeps_resting_order(self):
        bot, state = self.sync_fixture()
        fill = {"coin": state.spec.symbol, "side": "A", "sz": "5", "px": "0.401", "tid": "partial"}
        bot._record_fills([fill, fill])
        self.assertEqual(state.position, 21)
        self.assertEqual(state.ask_oid, 123)
        self.assertEqual(bot.stats.fills, 1)
        bot.place_or_requote(state)
        bot.exchange.order.assert_not_called()

    def test_perp_user_event_does_not_clear_spot_position(self):
        bot, state = self.sync_fixture()
        bot.on_user_events({"data": {"clearinghouseState": {"assetPositions": []}}})
        self.assertEqual(state.position, 26)

    def test_cancel_rejection_keeps_oid_and_backs_off(self):
        bot, state = self.sync_fixture()
        bot.cancel_all = HyperliquidSpreadBot.cancel_all.__get__(bot)
        bot.exchange.bulk_cancel.return_value = {"status": "ok", "response": {"data": {"statuses": [{"error": "not canceled"}]}}}
        bot.cancel_all("test", [state.spec.symbol])
        bot.cancel_all("test", [state.spec.symbol])
        self.assertEqual(state.ask_oid, 123)
        self.assertTrue(state.reconcile_required)
        self.assertEqual(bot.exchange.bulk_cancel.call_count, 1)

    def missing_cancel_fixture(self, statuses=None):
        bot, state = self.sync_fixture()
        raw = bot.exchange
        raw.bulk_cancel.return_value = {"status": "ok", "response": {"data": {"statuses": statuses or [
            {"error": "Order was never placed, already canceled, or filled. asset=100014801"}]}}}
        bot.exchange = bot.safety.wrap(raw, bot._reconcile_missing_cancels)
        bot.info.frontend_open_orders.return_value = []
        bot.info.open_orders.return_value = []
        bot.cancel_all = HyperliquidSpreadBot.cancel_all.__get__(bot)
        return bot, state, raw

    def test_cancel_fill_race_refreshes_inventory_without_halting(self):
        for balance in (0, 15, 26):  # filled, partial fill, or already canceled
            with self.subTest(balance=balance):
                bot, state, raw = self.missing_cancel_fixture()
                bot.info.spot_user_state.return_value = {"balances": [
                    {"coin": state.spec.balance_coin, "total": str(balance), "hold": "0"}]}
                bot.cancel_all("test")
                self.assertEqual(state.position, balance)
                self.assertIsNone(state.ask_oid)
                self.assertFalse(state.reconcile_required)
                self.assertTrue(bot.running)
                self.assertFalse(bot.safety.stopped.is_set())
                bot.cancel_all("test")
                raw.bulk_cancel.assert_called_once()
                raw.order.assert_not_called()

    def test_cancel_race_mixed_success_and_absent(self):
        missing = {"error": "Order was never placed, already canceled, or filled."}
        bot, state, raw = self.missing_cancel_fixture(["success", missing])
        state.bid_oid = 456
        bot.cancel_all("test")
        self.assertIsNone(state.bid_oid)
        self.assertIsNone(state.ask_oid)
        self.assertTrue(bot.running)
        raw.bulk_cancel.assert_called_once()

    def test_missing_buy_cancel_preserves_opening_rearm(self):
        bot, state, raw = self.missing_cancel_fixture()
        state.bid_oid, state.ask_oid = 123, None
        bot.info.spot_user_state.return_value = {"balances": []}
        bot.cancel_all("test")
        self.assertGreater(state.opening_rearm_until, time.time())
        state.pending_until = 0
        bot.place_or_requote(state)
        raw.order.assert_not_called()

    def test_cancel_race_stops_if_order_remains_or_reads_fail(self):
        for failure in ("open", "offline", "malformed", "sync", "bad_sync", "changed", "reappeared"):
            with self.subTest(failure=failure):
                bot, state, raw = self.missing_cancel_fixture()
                order = {"coin": state.spec.symbol, "oid": 123, "side": "A", "limitPx": "0.401"}
                if failure == "open":
                    bot.info.frontend_open_orders.return_value = [order]
                elif failure == "offline":
                    bot.info.frontend_open_orders.side_effect = TimeoutError("offline")
                elif failure == "malformed":
                    bot.info.frontend_open_orders.return_value = {}
                elif failure == "sync":
                    bot.info.open_orders.side_effect = TimeoutError("offline")
                elif failure == "bad_sync":
                    bot.info.open_orders.return_value = [{}]
                elif failure == "changed":
                    def overtake(*args, **kwargs):
                        bot.account_revision = getattr(bot, "account_revision", 0) + 1
                        return []
                    bot.info.open_orders.side_effect = overtake
                else:
                    bot.info.open_orders.return_value = [order]
                bot.cancel_all("test")
                self.assertFalse(bot.running)
                self.assertTrue(bot.safety.stopped.is_set())
                self.assertTrue(state.reconcile_required)
                bot.place_or_requote(state)
                raw.order.assert_not_called()

    def test_other_cancel_rejections_are_still_fatal(self):
        missing = {"error": "Order was never placed, already canceled, or filled."}
        for statuses in ([{"error": "permission denied"}], [missing, {"error": "offline"}], []):
            bot, state, raw = self.missing_cancel_fixture()
            if len(statuses) == 2:
                state.bid_oid = 456
            raw.bulk_cancel.return_value["response"]["data"]["statuses"] = statuses
            bot.cancel_all("test")
            self.assertTrue(bot.safety.stopped.is_set())
            bot.info.frontend_open_orders.assert_not_called()

    def test_reconciliation_keeps_execution_serialized_and_never_revives_stop(self):
        bot, state, raw = self.missing_cancel_fixture()
        entered, release, sent = threading.Event(), threading.Event(), threading.Event()
        def read_orders(*args, **kwargs):
            entered.set()
            if not release.wait(3):
                raise TimeoutError("test timeout")
            return []
        bot.info.frontend_open_orders.side_effect = read_orders
        cancel = threading.Thread(target=lambda: bot.cancel_all("test"))
        def send():
            from zeus.safety import ExecutionStopped
            try:
                bot.exchange.order(name=state.spec.symbol)
            except ExecutionStopped:
                sent.set()
        cancel.start()
        try:
            self.assertTrue(entered.wait(3))
            sender = threading.Thread(target=send)
            sender.start()
            raw.order.assert_not_called()
            bot.request_stop()
        finally:
            release.set()
            cancel.join(3)
        sender.join(3)
        self.assertFalse(cancel.is_alive() or sender.is_alive())
        self.assertTrue(sent.is_set())
        self.assertFalse(bot.running)
        raw.order.assert_not_called()

    def test_cancel_ack_clears_only_confirmed_orders(self):
        bot, state = self.sync_fixture()
        state.bid_oid = 456
        bot.cancel_all = HyperliquidSpreadBot.cancel_all.__get__(bot)
        bot.exchange.bulk_cancel.return_value = {"status": "ok", "response": {"data": {"statuses": ["success", {"error": "offline"}]}}}
        bot.cancel_all("test", [state.spec.symbol])
        self.assertIsNone(state.bid_oid)
        self.assertEqual(state.ask_oid, 123)
        self.assertTrue(state.reconcile_required)

    def test_unknown_placement_never_retries_without_reconciliation(self):
        bot, state = self.funding_fixture()
        bot.exchange.order.return_value = None
        bot.place_or_requote(state)
        self.assertTrue(state.reconcile_required)
        state.pending_until = 0
        state.last_order_ts = 0
        bot.place_or_requote(state)
        self.assertEqual(bot.exchange.order.call_count, 1)

    def funding_fixture(self, usdc="105.65225712", hold="0", tokens="0", token_hold="0"):
        bot, state = self.sync_fixture()
        state.ask_oid = None
        state.position = float(tokens)
        bot.info.open_orders.return_value = []
        bot.info.spot_user_state.return_value = {"balances": [
            {"coin": "USDC", "total": usdc, "hold": hold},
            {"coin": state.spec.balance_coin, "total": tokens, "hold": token_hold}]}
        return bot, state

    def test_spot_usdc_screenshot_balance_funds_normal_order(self):
        bot, state = self.funding_fixture()
        req, _ = bot.desired_spot_no_order(state)
        funded = bot._prepare_spot_request(state, req)
        self.assertIsNotNone(funded)
        self.assertLessEqual(funded["sz"] * funded["limit_px"], bot.cfg.order_usd)
        self.assertAlmostEqual(bot.available_usdc, 105.65225712)

    def test_buy_uses_free_usdc_and_actual_limit_price(self):
        for mode in ("maker", "taker"):
            bot, state = self.funding_fixture(usdc="105", hold="85")
            bot.cfg.exec_mode = mode
            bot.cfg.min_order_usd = 10
            req, _ = bot.desired_spot_no_order(state)
            funded = bot._prepare_spot_request(state, req)
            self.assertTrue(funded["is_buy"])
            self.assertLessEqual(funded["sz"] * funded["limit_px"], 20)

    def test_funded_submission_uses_fresh_sell_quantity(self):
        bot, state = self.funding_fixture(tokens="15", token_hold="0")
        state.position = 26  # the previous snapshot is stale
        bot.exchange.order.return_value = {"status": "ok", "response": {"data": {"statuses": [
            {"resting": {"oid": 789}}]}}}
        bot.place_or_requote(state)
        self.assertEqual(bot.exchange.order.call_args.kwargs["sz"], 15)
        self.assertFalse(bot.exchange.order.call_args.kwargs["is_buy"])
        self.assertEqual(state.ask_oid, 789)

    def test_not_enough_free_usdc_waits_without_order_or_halt(self):
        bot, state = self.funding_fixture(usdc="105", hold="104")
        bot.place_or_requote(state)
        self.assertIn("available spot USDC", state.last_action)
        self.assertTrue(bot.running)
        self.assertGreater(state.pending_until, time.time())
        bot.place_or_requote(state)
        bot.exchange.order.assert_not_called()
        bot.info.spot_user_state.assert_called_once()

    def test_sell_uses_free_tokens_and_keeps_total_inventory(self):
        bot, state = self.funding_fixture(tokens="26", token_hold="11")
        req, _ = bot.desired_spot_no_order(state)
        funded = bot._prepare_spot_request(state, req)
        self.assertFalse(funded["is_buy"])
        self.assertEqual(funded["sz"], 15)
        self.assertEqual(state.position, 26)

    def test_reserved_tokens_do_not_trigger_duplicate_sale(self):
        bot, state = self.funding_fixture(tokens="26", token_hold="26")
        bot.place_or_requote(state)
        self.assertIn("available Outcome tokens", state.last_action)
        self.assertTrue(bot.running)
        bot.exchange.order.assert_not_called()

    def test_stale_inventory_does_not_send_sale_after_fill(self):
        bot, state = self.funding_fixture()
        state.position = 26
        bot.place_or_requote(state)
        self.assertEqual(state.position, 0)
        self.assertTrue(bot.running)
        bot.exchange.order.assert_not_called()

    def test_new_existing_order_prevents_another_submission(self):
        bot, state = self.funding_fixture()
        bot.info.open_orders.return_value = [{"coin": state.spec.symbol, "oid": 789,
            "side": "B", "limitPx": "0.4"}]
        bot.place_or_requote(state)
        self.assertEqual(state.bid_oid, 789)
        bot.exchange.order.assert_not_called()

    def test_balance_read_failure_pauses_quotes_without_stopping_session(self):
        for failure in (TimeoutError("offline"), {"balances": [
                {"coin": "USDC", "total": "100", "hold": "nan"}]}):
            bot, state = self.funding_fixture()
            if isinstance(failure, Exception):
                bot.info.spot_user_state.side_effect = failure
            else:
                bot.info.spot_user_state.return_value = failure
            bot.place_or_requote(state)
            self.assertFalse(bot.safety.stopped.is_set())
            self.assertTrue(bot.running)
            self.assertEqual(bot.safety.phase, "recovering")
            bot.exchange.order.assert_not_called()

    def test_real_insufficient_balance_rejection_is_not_ignored(self):
        bot, state = self.funding_fixture()
        raw = bot.exchange
        raw.order.return_value = {"status": "ok", "response": {"data": {"statuses": [
            {"error": "Insufficient spot balance asset=100014751"}]}}}
        bot.exchange = bot.safety.wrap(raw, bot._reconcile_missing_cancels)
        bot.place_or_requote(state)
        self.assertTrue(bot.safety.stopped.is_set())
        self.assertIn("Insufficient spot balance", bot.safety.error)
        bot.place_or_requote(state)
        raw.order.assert_called_once()

    def test_opening_rest_does_not_delay_exit_or_risk_pull(self):
        bot = self.configured_bot("maker")
        state = self.state()
        state.bid_oid, state.quoted_bid = 123, 0.3999
        state.last_order_ts = time.time() - 0.2
        bot.place_or_requote(state)
        bot.cancel_all.assert_not_called()
        state.last_book_ts = time.time() - 10
        bot.place_or_requote(state)
        bot.cancel_all.assert_called_once()
        bot.cancel_all.reset_mock()
        state = self.state(position=26)
        state.ask_oid, state.quoted_ask = 123, 0.402
        state.last_order_ts = time.time() - 0.2
        bot.place_or_requote(state)
        bot.cancel_all.assert_called_once()

    def test_risk_flicker_does_not_cancel_reopen_repeatedly(self):
        bot = self.configured_bot("maker")
        state = self.state()
        state.bid_oid, state.quoted_bid = 123, state.bid
        bot.states = {state.spec.symbol: state}
        bot.cancel_all = HyperliquidSpreadBot.cancel_all.__get__(bot)
        bot.exchange.bulk_cancel.return_value = {"status": "ok", "response": {"data": {"statuses": ["success"]}}}
        base = time.time()
        with patch("zeus.bots.worldcup.time.time") as clock:
            for step in range(15):
                clock.return_value = base + step * 0.1
                state.last_book_ts = clock.return_value - (10 if step % 2 == 0 else 0)
                bot.place_or_requote(state)
        self.assertIsNone(state.bid_oid)
        self.assertEqual(bot.exchange.bulk_cancel.call_count, 1)
        bot.exchange.order.assert_not_called()


if __name__ == "__main__":
    unittest.main()
