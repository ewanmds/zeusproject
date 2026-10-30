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
        bot = self.configured_bot("maker")
        state = self.state()
        bot.exchange.order.return_value = None
        bot.place_or_requote(state)
        self.assertTrue(state.reconcile_required)
        state.pending_until = 0
        state.last_order_ts = 0
        bot.place_or_requote(state)
        self.assertEqual(bot.exchange.order.call_count, 1)

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
