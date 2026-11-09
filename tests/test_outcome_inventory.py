"""Regressions found while auditing adverse Outcome executions. No live I/O."""
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from zeus.bots.outcome_worker import build_config
from zeus.bots.worldcup import HyperliquidSpreadBot, MarketState, MarketSpec, SessionStats


class OutcomeInventoryTests(unittest.TestCase):
    def bot(self, total=0, entry=None):
        bot = HyperliquidSpreadBot.__new__(HyperliquidSpreadBot)
        bot.cfg = build_config({'market': '#101', 'order_size_usd': 15, 'max_position_usd': 200,
                               'exec_mode': 'maker', 'selection_mode': 'auto', 'two_sided': True,
                               'maker_spread_pct': None}, None)
        bot.cfg.account_address = 'offline-test-account'
        bot.stats = SessionStats(start_ts=time.time()-10)
        bot.lock, bot.wake = threading.RLock(), threading.Event()
        bot.safety = SimpleNamespace(account_known=False)
        bot.funds, bot.account_reader, bot.account_revision = None, None, 0
        bot.available_usdc = 100
        bot.outcome_fee_scales = {10: (1, 1)}
        bal = {'coin': '+101', 'total': str(total), 'hold': '0'}
        if entry is not None:
            bal['entryNtl'] = str(entry)
        bot.info = SimpleNamespace(spot_user_state=Mock(return_value={'balances': [
            {'coin': 'USDC', 'total': '100', 'hold': '0', 'entryNtl': '0'}, bal]}),
            open_orders=Mock(return_value=[]))
        bot._pause_execution = Mock()
        st = MarketState(MarketSpec('#101', 0, balance_coin='+101'))
        st.update_bbo(.5, .50045)
        bot.states = {'#101': st}
        return bot, st

    def fill(self, side, qty, before, px, tid=1):
        return {'coin': '#101', 'side': side, 'sz': str(qty), 'px': str(px),
                'startPosition': str(before), 'tid': tid, 'time': int(time.time()*1000),
                'closedPnl': '0', 'fee': '0'}

    def test_resume_restores_exchange_basis_and_enables_existing_stop(self):
        bot, st = self.bot(35, 35*.64364)
        self.assertTrue(bot.sync_account())
        self.assertAlmostEqual(st.avg_entry_px, .64364)
        self.assertEqual(st.entry_basis_source, 'exchange')
        st.update_bbo(.56329, .5633)
        self.assertIn('inventory stop', bot.inventory_risk_reason(st))

    def test_buy_fill_already_in_rest_does_not_double_position_or_average(self):
        bot, st = self.bot(30, 14)  # 20 @ .5 plus 10 @ .4 already reconciled.
        self.assertTrue(bot.sync_account())
        f = self.fill('B', 10, 20, .4)
        bot._record_fills([f, f])
        self.assertEqual(st.position, 30)
        self.assertAlmostEqual(st.avg_entry_px, 14/30)
        self.assertEqual(bot.stats.fills, 1)
        self.assertTrue(st.reconcile_required)

    def test_sell_fill_already_in_rest_is_not_subtracted_again(self):
        bot, st = self.bot(20, 10)
        self.assertTrue(bot.sync_account())
        bot._record_fills([self.fill('A', 10, 30, .6)])
        self.assertEqual(st.position, 20)
        self.assertEqual(st.avg_entry_px, .5)

    def test_new_partial_fills_keep_weighted_basis_until_final_flat(self):
        bot, st = self.bot()
        bot._record_fills([self.fill('B', 10, 0, .5), self.fill('B', 5, 10, .4, 2)])
        self.assertEqual(st.position, 15)
        self.assertAlmostEqual(st.avg_entry_px, 7/15)
        bot._record_fills([self.fill('A', 5, 15, .6, 3)])
        self.assertAlmostEqual(st.avg_entry_px, 7/15)
        bot._record_fills([self.fill('A', 10, 10, .6, 4)])
        self.assertEqual(st.avg_entry_px, 0)
        self.assertEqual(st.entry_basis_source, 'unknown')

    def test_unknown_entry_is_not_invented_from_current_price(self):
        bot, st = self.bot(35)
        self.assertTrue(bot.sync_account())
        self.assertEqual(st.position, 35)
        self.assertEqual(st.avg_entry_px, 0)
        self.assertEqual(st.entry_basis_source, 'unknown')

    def test_invalid_entry_data_pauses_sync_without_partial_position_commit(self):
        for invalid in ['NaN', '-1', 'infinity']:
            bot, st = self.bot(35, invalid)
            self.assertFalse(bot.sync_account())
            self.assertEqual(st.position, 0)
            bot._pause_execution.assert_called_once()

    def test_stop_cooldown_blocks_only_buys_and_keeps_inventory_asks(self):
        bot, st = self.bot(40, 20)
        self.assertTrue(bot.sync_account())
        now = time.time()
        st.inventory_stop_until = now+5
        with patch('zeus.outcome_costs.maker_profile', return_value=.000384):
            plan = bot.bilateral_requests(st)
            self.assertNotIn(True, plan)
            self.assertIn(False, plan)
            self.assertEqual(plan[False]['order_type']['limit']['tif'], 'Alo')
            self.assertIsNone(bot.risk_block_reason(st, closing_inventory=True))
            st.last_book_ts = now+6
            with patch('zeus.bots.worldcup.time.time', return_value=now+6):
                self.assertIn(True, bot.bilateral_requests(st))
        self.assertEqual(bot.cfg.inventory_stop_bps, 200)
        self.assertEqual(bot.cfg.order_usd, 15)

    def test_forced_exit_retains_reason_and_distinguishes_unknown_submission(self):
        for failure in [False, True]:
            bot, st = self.bot(35, 35*.64364)
            self.assertTrue(bot.sync_account())
            st.update_bbo(.56329, .5633)
            bot.running, bot.status = True, 'offline-test'
            bot.safety = SimpleNamespace(stopped=threading.Event(), phase='running', lock=threading.RLock())
            bot.exchange = SimpleNamespace(order=Mock(return_value={'status': 'ok'}))
            if failure:
                bot.exchange.order.side_effect = TimeoutError('offline test')
            bot.cancel_all = Mock()
            bot.sync_account = Mock(return_value=True)
            bot._parse_order_response = Mock()
            self.assertTrue(bot.enforce_inventory_stop(st))
            self.assertIn('inventory stop', st.last_inventory_exit['reason'])
            self.assertAlmostEqual(st.last_inventory_exit['entry_px'], .64364)
            self.assertEqual(st.last_inventory_exit['quantity'], 35)
            self.assertEqual(st.last_inventory_exit['submission'], 'unconfirmed' if failure else 'response_received')
            # Fills may replace last_action, but cannot erase the risk context.
            st.last_action = 'fill A 35 @ .56329'
            snap = bot.export_dashboard_stats()
            self.assertEqual(snap['inventory_risk_context'][0]['last_forced_exit'], st.last_inventory_exit)
            self.assertEqual(snap['gross_closed_pnl'], 0)


if __name__ == '__main__':
    unittest.main()
