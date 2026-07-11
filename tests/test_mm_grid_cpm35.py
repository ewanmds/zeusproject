import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from mm_grid_cpm35_core import (
    AdaptiveMarketState,
    AdaptiveQuoteEngine,
    MarketSnapshot,
    PerformanceTracker,
    PositionLedger,
    StrategyConfig,
    in_paris_blackout,
)


def market(**overrides):
    values = {
        "best_bid": 99.9,
        "best_ask": 100.1,
        "bid_size": 10.0,
        "ask_size": 10.0,
        "mid": 100.0,
        "microprice": 100.0,
        "spread_bps": 20.0,
        "volatility_bps": 0.2,
        "momentum_bps": 0.0,
        "book_imbalance": 0.0,
        "trade_imbalance": 0.0,
        "toxicity": 0.1,
        "timestamp": 1_000.0,
    }
    values.update(overrides)
    return MarketSnapshot(**values)


class QuoteEngineTests(unittest.TestCase):
    def setUp(self):
        self.config = StrategyConfig(cpm_min_volume_usd=100.0)
        self.engine = AdaptiveQuoteEngine(self.config, tick_size=0.01, price_decimals=2)

    def test_flat_plan_is_post_only_and_two_sided(self):
        plan = self.engine.build_plan(market(), 0.0, 1.0, None, 0.0)
        self.assertFalse(plan.paused)
        self.assertIsNotNone(plan.bid_px)
        self.assertIsNotNone(plan.ask_px)
        self.assertLess(plan.bid_px, plan.ask_px)
        self.assertLess(plan.bid_px, 100.1)
        self.assertGreater(plan.ask_px, 99.9)
        self.assertFalse(plan.bid_reduce_only)
        self.assertFalse(plan.ask_reduce_only)

    def test_inventory_switches_to_reduce_only(self):
        long_plan = self.engine.build_plan(market(), 1.0, 1.0, None, 0.0)
        self.assertIsNone(long_plan.bid_px)
        self.assertEqual(long_plan.ask_px, 100.1)
        self.assertTrue(long_plan.ask_reduce_only)

        short_plan = self.engine.build_plan(market(), -1.0, 1.0, None, 0.0)
        self.assertEqual(short_plan.bid_px, 99.9)
        self.assertIsNone(short_plan.ask_px)
        self.assertTrue(short_plan.bid_reduce_only)

    def test_bad_cpm_widens_then_hard_stops(self):
        base = self.engine.build_plan(market(spread_bps=1.0), 0.0, 1.0, None, 0.0)
        wider = self.engine.build_plan(market(spread_bps=1.0), 0.0, 1.0, 70.0, 150.0)
        self.assertGreater(wider.half_spread_bps, base.half_spread_bps)

        stopped = self.engine.build_plan(market(), 0.0, 1.0, 105.0, 200.0)
        self.assertTrue(stopped.paused)
        self.assertIsNone(stopped.bid_px)
        self.assertIsNone(stopped.ask_px)
        self.assertIn("CPM", stopped.reason)

    def test_toxic_flow_pauses_opening_risk(self):
        plan = self.engine.build_plan(market(toxicity=0.95), 0.0, 1.0, None, 0.0)
        self.assertTrue(plan.paused)
        self.assertIsNone(plan.bid_px)
        self.assertIsNone(plan.ask_px)

    def test_inventory_cap_is_intentionally_one_order(self):
        with self.assertRaises(ValueError):
            StrategyConfig(max_inventory_orders=2)


class MarketStateTests(unittest.TestCase):
    def test_volatility_and_momentum_react_to_book_moves(self):
        state = AdaptiveMarketState(StrategyConfig())
        state.update_book(99.9, 100.1, 10.0, 10.0, 1_000.0)
        snapshot = state.update_book(100.9, 101.1, 15.0, 5.0, 1_001.0)
        self.assertGreater(snapshot.volatility_bps, 0.0)
        self.assertGreater(snapshot.momentum_bps, 0.0)
        self.assertGreater(snapshot.book_imbalance, 0.0)
        self.assertGreater(snapshot.microprice, snapshot.mid)


class AccountingTests(unittest.TestCase):
    def test_position_ledger_round_trip(self):
        ledger = PositionLedger()
        ledger.apply_fill(True, 100.0, 1.0)
        realized = ledger.apply_fill(False, 101.0, 1.0)
        self.assertAlmostEqual(realized, 1.0)
        self.assertAlmostEqual(ledger.realized_pnl, 1.0)
        self.assertEqual(ledger.position, 0.0)

    def test_quality_cpm_uses_markout_edge(self):
        tracker = PerformanceTracker(markout_seconds=5.0, quality_window_seconds=60.0)
        tracker.record_fill(True, 100.0, 1.0, 0.01, 1_000.0)
        tracker.mark_to_market(101.0, 1_006.0)
        expected = (0.01 - 1.0) / 100.0 * 1_000_000.0
        self.assertAlmostEqual(tracker.quality_cpm, expected)

    def test_negative_fee_rebate_is_preserved(self):
        tracker = PerformanceTracker(markout_seconds=1.0)
        tracker.record_fill(True, 100.0, 1.0, -0.01, 1_000.0)
        self.assertAlmostEqual(tracker.total_fees, -0.01)


class BlackoutTests(unittest.TestCase):
    def test_weekday_blackout(self):
        paris = ZoneInfo("Europe/Paris")
        self.assertTrue(in_paris_blackout(datetime(2026, 7, 13, 15, 30, tzinfo=paris)))
        self.assertFalse(in_paris_blackout(datetime(2026, 7, 13, 16, 45, tzinfo=paris)))
        self.assertFalse(in_paris_blackout(datetime(2026, 7, 13, 16, 46, tzinfo=paris)))

    def test_weekend_is_not_blackout(self):
        paris = ZoneInfo("Europe/Paris")
        self.assertFalse(in_paris_blackout(datetime(2026, 7, 12, 15, 30, tzinfo=paris)))


if __name__ == "__main__":
    unittest.main()
