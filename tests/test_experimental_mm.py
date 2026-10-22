"""Quant policy + production adapter checks, all offline and without a wallet."""
import ast
from collections import deque
import copy
import json
import math
from pathlib import Path
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import Mock, patch

from experimental_mm.core import (Features, Markouts, POLICY, POLICY_HASH, SCHEMA, SYMBOL,
                                  minimum_size, quotes, retain, safe_resting, verified_fees)
from experimental_mm.replay import Simulator, calibrate, confidence_bounds, read_session
from experimental_mm.runtime import Journal, Runtime, implementation_hash, validate_report
from test_order_book_modes import CLASS, SOURCE, production_methods, result
from experimental_mm.clock import ExchangeClock


def fee_snapshot(now=None):
    return dict(maker=0.0, taker=0.0001, decimals=3, symbol=SYMBOL, fingerprint="fee",
                observed_at=time.time() if now is None else now)


def levels(bid=99.99, ask=100.01, size=2):
    return [[dict(px=bid, sz=size)], [dict(px=ask, sz=size)]]


def runtime_fixture(ready=True):
    runtime = Runtime.__new__(Runtime)
    runtime.lock = threading.RLock()
    runtime.journal = Mock(error="", path=Path("test-only.jsonl"))
    runtime.features = Features()
    runtime.require_calibration = True
    runtime.clock = Mock()
    runtime.clock.now.side_effect = time.time
    now = time.time()
    runtime.features.update(now, *levels())
    runtime.markouts = Markouts()
    runtime.fees = fee_snapshot(now)
    runtime.report = {"data_end": now} if ready else None
    runtime.offset = 1 if ready else None
    runtime.reason, runtime.state = "test-only", "collecting"
    runtime.latencies = deque()
    return runtime


def accepted_fixture(now):
    sessions = [dict(start=now - d * 86400 - 1800, end=now - d * 86400,
                     utc_date=f"day-{d}", sha256="a" * 64) for d in (2, 1, 0)]
    case = dict(experimental=dict(round_trips=500, volume=10000, funding_complete=True, unclosed=False,
                                  cpm_upper95=-1, invalid=""),
                bbo=dict(round_trips=500, volume=10000, funding_complete=True, unclosed=False, invalid=""),
                mid=dict(round_trips=500, volume=10000, funding_complete=True, unclosed=False, invalid=""),
                difference_upper95=dict(bbo=-1, mid=-1))
    return dict(schema=SCHEMA, symbol=SYMBOL, policy_hash=POLICY_HASH, implementation_hash=implementation_hash(),
                fee_fingerprint="fee", data_end=now, accepted=True, offset=1, sessions=sessions,
                holdout=[dict(latency_ms=latency, **copy.deepcopy(case)) for latency in (250, 500)])


class PolicyTests(unittest.TestCase):
    def test_clock_refresh_never_rewinds_the_recording_timeline(self):
        clock = ExchangeClock()
        clock.observe("Thu, 22 Oct 2026 09:47:55 GMT", 1792662475500, 100, 100.2)
        previous = clock.anchor
        clock.observe("Thu, 22 Oct 2026 09:47:55 GMT", 1792662475500, 100.3, 100.4)
        self.assertGreaterEqual(clock.anchor, previous + .2 - 1e-6)

    def test_direct_mode_does_not_require_calibration_but_keeps_fee_and_feed_gates(self):
        runtime = runtime_fixture(False)
        runtime.require_calibration, runtime.offset = False, 4
        now = time.time()
        self.assertTrue(runtime.decision(now, 3))
        self.assertEqual(runtime.snapshot()["execution_mode"], "direct")
        runtime.fees = None
        self.assertEqual(runtime.decision(now, 3), {})

    def test_exchange_clock_tolerates_windows_wall_clock_skew_but_not_stale_feed(self):
        clock = ExchangeClock()
        clock.observe("Thu, 22 Oct 2026 09:47:55 GMT", 1792662475500, 100, 100.2)
        with patch("experimental_mm.clock.time.monotonic", return_value=101.2), \
                patch("experimental_mm.clock.time.time", return_value=0):
            self.assertAlmostEqual(clock.now(), 1792662476.6)
        with self.assertRaises(ValueError):
            clock.observe("Thu, 22 Oct 2026 09:47:55 GMT", 1792662400000, 100, 100.2)

    def test_direct_book_accepts_clock_skew_and_rejects_truly_stale_source(self):
        runtime = runtime_fixture(False)
        runtime.require_calibration = False
        now = time.time()
        runtime.clock = Mock()
        runtime.clock.now.return_value = now + 15
        runtime.last_source, runtime.last_book_received = 0, 0
        runtime.book(*levels(), now + 15)
        self.assertIsNotNone(runtime.features.book)
        runtime.book(*levels(), now - 20)
        self.assertIsNone(runtime.features.book)

    def test_fee_bound_accounts_for_hip3_growth_and_never_credits_rebates(self):
        meta = dict(collateralToken=0, universe=[dict(name=SYMBOL, szDecimals=3, growthMode="enabled", deployerFeeScale="1")])
        fees = verified_fees(dict(userAddRate="0.00015", userCrossRate="0.00045"), meta, 100)
        self.assertAlmostEqual(fees["maker"], .00003)
        self.assertAlmostEqual(fees["taker"], .00009)
        self.assertEqual(verified_fees(dict(userAddRate="-0.00001", userCrossRate="0.00045"), meta, 100)["maker"], 0)
        for key in ("growthMode", "deployerFeeScale", "szDecimals"):
            bad = copy.deepcopy(meta)
            del bad["universe"][0][key]
            with self.assertRaises((KeyError, ValueError)):
                verified_fees(dict(userAddRate="0.00015", userCrossRate="0.00045"), bad, 100)

    def test_minimum_size_rounds_up_and_quotes_respect_cost_and_flow(self):
        feature = Features()
        book = feature.update(100, *levels())
        wanted = quotes(book, 100, 3, 1, 0)
        self.assertGreaterEqual(wanted["B"][0] * wanted["B"][1], 10.5)
        self.assertAlmostEqual(minimum_size(5800, 3), .002)
        self.assertEqual(quotes(book, 100, 3, 0, .001), {})
        book["ofi"] = -1
        self.assertNotIn("B", quotes(book, 100, 3, 1, 0))
        self.assertIn("A", quotes(book, 100, 3, 1, 0))
        self.assertEqual(quotes(book, 103, 3, 1, 0), {})

    def test_rest_is_sticky_but_never_protects_a_dangerous_quote_or_exit(self):
        old = dict(side="B", px=99.98, sz=.106, ro=False, ts=100)
        book = Features().update(100, *levels())
        self.assertTrue(retain(old, (99.97, .107, False), 104, .01))
        self.assertFalse(retain(old, (99.95, .107, False), 104, .01))
        self.assertTrue(retain(old, (99.95, .107, False), 101, .01))
        self.assertFalse(retain(old, None, 101, .01))
        self.assertFalse(retain(old, (99.97, .1, True), 101, .01))
        self.assertFalse(safe_resting(dict(old, px=100.02), (99.98, .106, False), book, 0))

    def test_exits_are_reduce_only_and_improve_without_crossing(self):
        book = Features().update(100, *levels(99, 101))
        for position, side in ((.05, "A"), (-.05, "B")):
            early = quotes(book, 100, 3, None, 0, position, 99)[side]
            later = quotes(book, 100, 3, None, 0, position, 90)[side]
            self.assertEqual(early[1:], (.05, True))
            self.assertEqual(later[1:], (.05, True))
            self.assertGreater(later[0], 99)
            self.assertLess(later[0], 101)

    def test_markouts_are_signed_multiple_horizons_and_missing_is_unknown(self):
        values = Markouts()
        self.assertIsNone(values.snapshot()["0.25"]["B"]["mean_bps"])
        values.fill(100, "B", 100)
        values.fill(100, "A", 100)
        values.book(100.25, 101)
        self.assertAlmostEqual(values.snapshot()["0.25"]["B"]["mean_bps"], 100)
        self.assertAlmostEqual(values.snapshot()["0.25"]["A"]["mean_bps"], -100)
        values.book(120, 101)
        self.assertEqual(values.snapshot()["1.0"]["B"]["count"], 0)
        self.assertEqual(values.expired, 6)

    def test_readiness_fails_closed_and_never_blocks_exits(self):
        runtime = runtime_fixture(False)
        now = time.time()
        self.assertEqual(runtime.decision(now, 3), {})
        self.assertTrue(runtime.decision(now, 3, .05, now))
        runtime.report, runtime.offset = {"data_end": now}, 1
        self.assertTrue(runtime.decision(now, 3))
        runtime.fees["observed_at"] = now - 301
        self.assertEqual(runtime.decision(now, 3), {})
        runtime.fees["observed_at"] = now
        runtime.journal.error = "disk full"
        self.assertEqual(runtime.decision(now, 3), {})
        self.assertTrue(runtime.decision(now, 3, .05, now))


class EvidenceTests(unittest.TestCase):
    def test_report_requires_all_evidence_not_just_an_accepted_flag(self):
        now = time.time()
        report = accepted_fixture(now)
        self.assertEqual(validate_report(report, now, fee_snapshot(now)), 1)
        cases = [({"offset": 9}, "offset"), ({"fee_fingerprint": "other"}, "fees"),
                 ({"policy_hash": "other"}, "policy"), ({"data_end": now - 86401}, "expired"),
                 ({"data_end": now + 10}, "future"), ({"accepted": False}, "accepted")]
        for changes, name in cases:
            with self.subTest(name=name), self.assertRaises(ValueError):
                validate_report(dict(report, **changes), now, fee_snapshot(now))
        for key, value in (("round_trips", 499), ("funding_complete", False), ("unclosed", True), ("invalid", "gap"), ("cpm_upper95", 1)):
            modified = copy.deepcopy(report)
            modified["holdout"][0]["experimental"][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_report(modified, now, fee_snapshot(now))
        modified = copy.deepcopy(report)
        modified["holdout"].pop()
        with self.assertRaises(ValueError):
            validate_report(modified, now, fee_snapshot(now))

    def test_paired_block_bootstrap_and_unknown_zero_volume(self):
        experimental = dict(blocks={k: [1000, -.01] for k in range(30)})
        baseline = dict(blocks={k: [1000, .01] for k in range(30)})
        bound, differences = confidence_bounds(experimental, dict(bbo=baseline, mid=baseline))
        self.assertLess(bound, 0)
        self.assertLess(differences["bbo"], 0)
        self.assertTrue(math.isinf(confidence_bounds(dict(blocks={}), {})[0]))

    def test_journal_is_closed_and_contains_no_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = Journal(directory)
            journal.record("book", {"levels": levels(), "source_ts": time.time()})
            journal.close()
            events = [json.loads(line) for line in journal.path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([e["kind"] for e in events], ["start", "book", "end"])
            self.assertTrue(events[-1]["data"]["clean"])
            self.assertNotIn("PRIVATE_KEY", journal.path.read_text())

    def test_end_to_end_insufficient_calibration_never_approves(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = []
            for day in range(3):
                start = 1767225600 + day * 86400 + 3600
                events = [dict(kind="start", ts=start, data=dict(schema=SCHEMA, symbol=SYMBOL,
                          policy_hash=POLICY_HASH, implementation_hash=implementation_hash()))]
                for second in range(1801):
                    now = start + second
                    if second % 60 == 0:
                        events.append(dict(kind="fees", ts=now, data=fee_snapshot(now)))
                    events.append(dict(kind="book", ts=now, data=dict(levels=levels(), source_ts=now)))
                events.append(dict(kind="end", ts=start + 1801, data={"clean": True}))
                path = Path(directory) / f"session-{day}.jsonl"
                path.write_text("\n".join(json.dumps(e) for e in events), encoding="utf-8")
                paths.append(path)
            report = calibrate(paths, Path(directory) / "calibration.json")
            self.assertFalse(report["accepted"])
            self.assertTrue(any("500" in reason for reason in report["reasons"]))
            incomplete = Path(directory) / "incomplete.jsonl"
            incomplete.write_text(paths[0].read_text().rsplit("\n", 1)[0])
            with self.assertRaises(ValueError):
                read_session(incomplete)


class ReplayTests(unittest.TestCase):
    def event(self, kind, ts, data):
        return dict(kind=kind, ts=ts, data=data)

    def simulator(self, latency=250):
        sim = Simulator("experimental", latency, 0)
        sim.process(self.event("fees", 100, fee_snapshot(100)))
        sim.process(self.event("book", 100, dict(levels=levels(), source_ts=100)))
        return sim

    def trade(self, ts, size, tid="1"):
        return self.event("trades", ts, [dict(tid=tid, side="A", px=99.99, sz=size, time=ts)])

    def test_touch_is_not_fill_and_latency_delays_activation(self):
        fast, slow = self.simulator(250), self.simulator(500)
        event = self.event("book", 100.3, dict(levels=levels(), source_ts=100.3))
        fast.process(event)
        slow.process(event)
        self.assertEqual(len(fast.orders), 2)
        self.assertEqual(len(slow.orders), 0)
        self.assertEqual(fast.fills, 0)

    def test_queue_partial_fill_deduplication_and_pending_cancel(self):
        sim = self.simulator()
        sim.process(self.event("book", 100.3, dict(levels=levels(), source_ts=100.3)))
        sim.process(self.trade(100.4, 2.05))
        self.assertAlmostEqual(sim.position, .05)
        self.assertEqual(sim.fills, 1)
        sim.process(self.trade(100.41, 2.05))
        self.assertAlmostEqual(sim.position, .05)
        sim.process(self.trade(100.5, .01, "2"))  # cancellation still in flight
        self.assertAlmostEqual(sim.position, .06)

    def test_funding_uses_settlement_inventory_and_missing_is_unknown(self):
        sim = self.simulator()
        sim.inventory_history = [(3500, .1), (3610, 0)]
        sim.contexts = [(3599, 100)]
        sim.funding_events = {3600: .001}
        report = sim.finish(3500, 3700)
        self.assertAlmostEqual(report["funding"], .01)
        self.assertTrue(report["funding_complete"])
        sim.funding_events = {}
        self.assertFalse(sim.finish(3500, 3700)["funding_complete"])


class ProductionAdapterTests(unittest.TestCase):
    def bot(self, ready=True):
        namespace, bot = production_methods("experimental")
        names = ("compute_experimental_intent", "_experimental_submission_ok", "_experimental_retain")
        nodes = [node for node in CLASS.body if isinstance(node, ast.FunctionDef) and node.name in names]
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SOURCE), "exec"), namespace)
        for name in names:
            setattr(bot, name, types.MethodType(namespace[name], bot))
        bot.experimental = runtime_fixture(ready)
        bot.best_bid, bot.best_ask, bot.mid = 99.99, 100.01, 100
        bot.last_tick_ts, bot.pos_since, bot.entry_price = time.time(), time.time(), 100
        bot._experimental_uncertain, bot._hard_exit_at = False, 0
        return bot

    def test_no_calibration_no_exchange_order(self):
        bot = self.bot(False)
        bot.apply_intent()
        self.assertEqual(bot.exchange.mock_calls, [])

    def test_experimental_prices_survive_submission_and_noop_is_sticky(self):
        bot = self.bot()
        bot.exchange.bulk_orders.return_value = result(1, 2)
        bot.apply_intent()
        sent = bot.exchange.bulk_orders.call_args.args[0]
        self.assertAlmostEqual(sent[0]["limit_px"], 99.98)
        self.assertAlmostEqual(sent[1]["limit_px"], 100.02)
        bot.last_action_ts = 0
        bot.apply_intent()
        self.assertEqual(bot.exchange.bulk_orders.call_count, 1)
        bot.exchange.bulk_modify_orders_new.assert_not_called()

    def test_fill_before_submission_blocks_opening_but_keeps_exit(self):
        bot = self.bot(False)
        bot.position = .05
        bot.place_quotes([dict(is_buy=True, limit_px=99.98, sz=.106, reduce_only=False)])
        bot.exchange.bulk_orders.assert_not_called()
        bot.exchange.bulk_orders.return_value = result(1)
        bot.apply_intent()
        sent = bot.exchange.bulk_orders.call_args.args[0]
        self.assertEqual(len(sent), 1)
        self.assertTrue(sent[0]["reduce_only"])
        self.assertEqual(sent[0]["sz"], .05)

    def test_risk_change_pulls_immediately_even_during_minimum_rest(self):
        bot = self.bot()
        bot.orders = {1: dict(side="B", px=99.98, sz=.106, ro=False, ts=time.time())}
        bot.exchange.bulk_cancel.return_value = {"status": "ok", "response": {"data": {"statuses": ["success"]}}}
        bot.pause_until, bot.last_action_ts = time.time() + 10, time.time()
        bot.apply_intent()
        bot.exchange.bulk_cancel.assert_called_once()
        bot.exchange.bulk_orders.assert_not_called()

    def test_ambiguous_response_latches_entries_until_manual_restart(self):
        bot = self.bot()
        bot.exchange.bulk_orders.side_effect = TimeoutError("ambiguous")
        bot.apply_intent()
        self.assertTrue(bot._experimental_uncertain)
        bot.last_action_ts = 0
        bot.apply_intent()
        self.assertEqual(bot.exchange.bulk_orders.call_count, 1)

    def test_backend_market_and_size_are_enforced(self):
        import bot_server as server
        with patch.object(server.subprocess, "Popen") as spawn:
            with self.assertRaises(ValueError):
                server.BotManager().start_grid(server.GridStartRequest(strategy="experimental", token="GOLD"))
            spawn.assert_not_called()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            module = types.SimpleNamespace(resolve_token=lambda _: SYMBOL, stats_file_path=lambda _: str(root / "stats"),
                                           stop_file_path=lambda _: str(root / "stop"))
            process = Mock(pid=123)
            process.poll.return_value = None
            manager = server.BotManager()
            with patch.object(server, "BASE_DIR", root), patch.object(server, "_safe_import", return_value=module), \
                    patch.object(server.subprocess, "Popen", return_value=process) as spawn, patch.object(server.time, "sleep"):
                manager.start_grid(server.GridStartRequest(strategy="experimental", order_size_usd=10000))
                command = spawn.call_args.args[0]
                self.assertEqual(command[command.index("--notional") + 1], "10.5")
                self.assertEqual(command[command.index("--strategy") + 1], "experimental")
            for entry in manager.obi_procs.values():
                entry["log_f"].close()


if __name__ == "__main__":
    unittest.main()
