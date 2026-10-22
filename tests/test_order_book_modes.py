"""Offline tests of the production quoting methods; no wallet or network import."""
import ast
import math
from pathlib import Path
import threading
import tempfile
import time
import types
import unittest
from unittest.mock import Mock, patch
from typing import Any, Dict, List, Optional, Tuple


SOURCE = Path(__file__).resolve().parents[1] / "OBI_VOLUME_BOT.py"
TREE = ast.parse(SOURCE.read_text(encoding="utf-8"))
CLASS = next(n for n in TREE.body if isinstance(n, ast.ClassDef) and n.name == "OrderBookBot")
METHODS = {"_fast_quote_gates", "compute_bbo_intent", "compute_mid_intent", "compute_intent",
           "apply_intent", "place_quotes", "modify_quotes", "_is_post_only_error",
           "_emergency_pull", "_maybe_toxic_scratch", "cancel_oids"}
FUNCTIONS = {"order_book_prices", "px_down", "px_up", "round_sz", "fast_mm", "touch_mm"}


def production_methods(strategy="mid", distance=10.0):
    ns = dict(math=math, time=time, Dict=Dict, Any=Any, Tuple=Tuple, List=List,
              Optional=Optional, STRATEGY=strategy, MID_OFFSET_BPS=distance,
              PRICE_DECIMALS=4, SZ_DECIMALS=3, MIN_SIZE=0.001,
              MAX_SPREAD_TICKS=1000, ORDER_NOTIONAL_USD=100.0, MAX_INV_ORDERS=2,
              SESSION_WINDOWS_UTC=(), OPEN_BLACKOUT_MIN=0, SYMBOL="xyz:SP500",
              ACTION_COALESCE_SEC=0.08, POST_ONLY_MAX_RETRIES=2,
              dyn_tick=lambda _: 0.01, session_state_utc=lambda *args: "open")
    nodes = [n for n in TREE.body if isinstance(n, ast.FunctionDef) and n.name in FUNCTIONS]
    nodes += [n for n in CLASS.body if isinstance(n, ast.FunctionDef) and n.name in METHODS]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SOURCE), "exec"), ns)
    bot = types.SimpleNamespace(
        lock=threading.Lock(), api_lock=threading.Lock(), best_bid=99.0, best_ask=101.0,
        mid=100.0, position=0.0, size_mult=1.0, pause_until=0.0,
        hourly_pause_until=0.0, bank_until=0.0, orders={}, last_action_ts=0.0,
        live=True, force_quote=False, last_intent=None, last_error="", last_action="",
        queue_init={}, queue_ahead={}, order_placements=0, order_modifications=0,
        order_cancellations=0, order_requests=0,
        signal=types.SimpleNamespace(level_size=lambda *args: 1.0), exchange=Mock())
    for name in METHODS:
        bot.__dict__[name] = types.MethodType(ns[name], bot)
    return ns, bot


def result(*oids):
    return {"status": "ok", "response": {"data": {"statuses": [
        {"resting": {"oid": oid}} for oid in oids]}}}


class OrderBookModeTests(unittest.TestCase):
    def test_dry_run_cannot_mutate_exchange_even_during_cleanup(self):
        _, bot = production_methods()
        bot.live = False
        bot.orders = {1: dict(side="B", px=99.9, sz=1.0, ro=False)}
        request = dict(is_buy=True, limit_px=99.9, sz=1.0, reduce_only=False)
        bot.cancel_oids([1])
        bot.place_quotes([request])
        bot.modify_quotes([(1, request)])
        self.assertEqual(bot.exchange.mock_calls, [])
        self.assertEqual((bot.order_placements, bot.order_modifications, bot.order_cancellations), (0, 0, 0))

    def test_bbo_quotes_exact_touch(self):
        _, bot = production_methods("bbo")
        bid, ask, _ = bot.compute_intent()
        self.assertEqual((bid[0], ask[0]), (99.0, 101.0))

    def test_mid_fixed_distance_even_with_wide_book(self):
        _, bot = production_methods()
        bid, ask, _ = bot.compute_intent()
        self.assertEqual((bid[0], ask[0]), (99.9, 100.1))
        self.assertEqual((bid[1], ask[1]), (1.0, 1.0))

    def test_mid_moves_symmetrically_and_rounds_outwards(self):
        ns, bot = production_methods(distance=7.3)
        bot.best_bid, bot.best_ask, bot.mid = 199.0, 201.0, 200.0
        bid, ask, _ = bot.compute_intent()
        self.assertEqual((bid[0], ask[0]), (199.85, 200.15))

    def test_zero_offset_keeps_one_tick_and_post_only(self):
        _, bot = production_methods(distance=0.0)
        bot.best_bid, bot.best_ask = 99.99, 100.01
        bid, ask, _ = bot.compute_intent()
        self.assertEqual((bid[0], ask[0]), (99.99, 100.01))

    def test_inventory_and_reduce_only_match_bbo(self):
        for mode in ("bbo", "mid"):
            with self.subTest(mode=mode):
                _, bot = production_methods(mode)
                bot.position = 2.0
                bid, ask, _ = bot.compute_intent()
                self.assertIsNone(bid)
                self.assertEqual(ask[1:], (1.0, True))

    def test_risk_pause_removes_opening_but_keeps_exit(self):
        _, bot = production_methods()
        bot.position, bot.pause_until = 0.5, time.time() + 60
        bid, ask, _ = bot.compute_intent()
        self.assertIsNone(bid)
        self.assertEqual(ask, (100.1, 0.5, True))

    def test_invalid_book_and_offset_fail_closed(self):
        for bb, ba in ((0, 101), (101, 99), (float("nan"), 101)):
            _, bot = production_methods()
            bot.best_bid, bot.best_ask = bb, ba
            self.assertEqual(bot.compute_intent()[:2], (None, None))
        for distance in (-1, float("nan"), float("inf"), 20000):
            _, bot = production_methods(distance=distance)
            self.assertEqual(bot.compute_intent()[:2], (None, None))

    def test_retired_obi_mode_is_rejected(self):
        _, bot = production_methods("obi")
        with self.assertRaises(ValueError):
            bot.compute_intent()

    def test_obi_signals_cannot_pull_classic_quotes(self):
        for mode in ("bbo", "mid"):
            _, bot = production_methods(mode)
            bot.cancel_oids = Mock()
            bot._emergency_pull("B", 1_000_000)
            bot._maybe_toxic_scratch()
            bot.cancel_oids.assert_not_called()

    def test_unchanged_mid_sends_nothing(self):
        _, bot = production_methods()
        bot.orders = {1: dict(side="B", px=99.9, sz=1.0, ro=False),
                      2: dict(side="A", px=100.1, sz=1.0, ro=False)}
        bot.apply_intent()
        bot.exchange.bulk_orders.assert_not_called()
        bot.exchange.bulk_modify_orders_new.assert_not_called()

    def test_changed_mid_modifies_both_sides_in_one_batch(self):
        _, bot = production_methods()
        bot.orders = {1: dict(side="B", px=99.0, sz=1.0, ro=False),
                      2: dict(side="A", px=101.0, sz=1.0, ro=False)}
        bot.exchange.bulk_modify_orders_new.return_value = result(3, 4)
        bot.apply_intent()
        batch = bot.exchange.bulk_modify_orders_new.call_args.args[0]
        self.assertEqual([r["order"]["limit_px"] for r in batch], [99.9, 100.1])
        bot.exchange.bulk_orders.assert_not_called()

    def test_placement_uses_latest_mid_and_fixed_distance(self):
        _, bot = production_methods()
        bot.exchange.bulk_orders.return_value = result(1, 2)
        bot.place_quotes([
            dict(is_buy=True, limit_px=80.0, sz=1.0, reduce_only=False),
            dict(is_buy=False, limit_px=120.0, sz=1.0, reduce_only=False)])
        sent = bot.exchange.bulk_orders.call_args.args[0]
        self.assertEqual([r["limit_px"] for r in sent], [99.9, 100.1])
        self.assertEqual(bot.order_placements, 2)

    def test_mid_has_same_immediate_admission_as_bbo(self):
        _, bot = production_methods()
        bot.exchange.bulk_orders.return_value = result(1, 2)
        bot.apply_intent()
        self.assertEqual(bot.order_placements, 2)


class BackendModeTests(unittest.TestCase):
    def test_backend_rejects_obi_and_invalid_distance_without_spawning(self):
        import bot_server as server
        manager = server.BotManager()
        with patch.object(server.subprocess, "Popen") as spawn:
            for config in (server.GridStartRequest(strategy="obi"),
                           server.GridStartRequest(strategy="mid", offset_bps=-1),
                           server.GridStartRequest(strategy="mid", offset_bps=10000)):
                with self.assertRaises(ValueError):
                    manager.start_grid(config)
            spawn.assert_not_called()

    def test_backend_forwards_fixed_mid_offset_to_child(self):
        import bot_server as server
        manager = server.BotManager()
        self.assertEqual(server.GridStartRequest().strategy, "bbo")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            module = types.SimpleNamespace(
                resolve_token=lambda _: "xyz:SP500",
                stats_file_path=lambda _: str(root / "stats.json"),
                stop_file_path=lambda _: str(root / "stop.flag"))
            process = Mock(pid=123)
            process.poll.return_value = None
            with patch.object(server, "BASE_DIR", root), \
                    patch.object(server, "_safe_import", return_value=module), \
                    patch.object(server.subprocess, "Popen", return_value=process) as spawn, \
                    patch.object(server.time, "sleep"):
                manager.start_grid(server.GridStartRequest(strategy="mid", offset_bps=7.5))
                cmd = spawn.call_args.args[0]
                self.assertEqual(cmd[cmd.index("--strategy") + 1], "mid")
                self.assertEqual(cmd[cmd.index("--offset-bps") + 1], "7.5")
            for entry in manager.obi_procs.values():
                entry["log_f"].close()


if __name__ == "__main__":
    unittest.main()
