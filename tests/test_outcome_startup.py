"""Startup retries are read-only, bounded and cancellable; no live API calls."""
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import requests
from zeus.bots.outcome_worker import build_config
from zeus.bots.worldcup import HyperliquidSpreadBot
from zeus.network import StartupNetworkError, StartupReadCancelled


class OutcomeStartupTests(unittest.TestCase):
    def bare(self):
        bot = HyperliquidSpreadBot.__new__(HyperliquidSpreadBot)
        bot.running = True
        bot.cfg = SimpleNamespace(stop_file=None)
        bot.safety = SimpleNamespace(stopped=threading.Event())
        bot.wake = threading.Event()
        return bot

    def test_transient_timeout_recovers_without_any_order_submission(self):
        bot = self.bare()
        operation = Mock(side_effect=[requests.ReadTimeout('temporary'), {'ok': True}])
        with patch.object(bot, '_startup_retry_wait') as wait:
            self.assertEqual(bot._startup_read('metadata', operation), {'ok': True})
        self.assertEqual(operation.call_count, 2)
        wait.assert_called_once_with(1)

    def test_permanent_network_outage_has_four_attempts(self):
        bot = self.bare()
        operation = Mock(side_effect=requests.ReadTimeout('offline'))
        with patch.object(bot, '_startup_retry_wait'), self.assertRaises(StartupNetworkError):
            bot._startup_read('metadata', operation)
        self.assertEqual(operation.call_count, 4)

    def test_invalid_configuration_is_not_retried(self):
        bot = self.bare()
        operation = Mock(side_effect=ValueError('invalid metadata'))
        with patch.object(bot, '_startup_retry_wait') as wait, self.assertRaises(ValueError):
            bot._startup_read('metadata', operation)
        self.assertEqual(operation.call_count, 1)
        wait.assert_not_called()

    def test_stop_file_prevents_next_attempt(self):
        bot = self.bare()
        with tempfile.TemporaryDirectory() as folder:
            bot.cfg.stop_file = str(Path(folder)/'stop')
            operation = Mock(side_effect=requests.ReadTimeout('offline'))
            with patch.object(bot, '_startup_retry_wait', side_effect=lambda _: Path(bot.cfg.stop_file).touch()), \
                    self.assertRaises(StartupReadCancelled):
                bot._startup_read('metadata', operation)
            self.assertEqual(operation.call_count, 1)

    def test_stop_interrupts_backoff_immediately(self):
        bot = self.bare()
        bot.safety.stopped.set()
        with self.assertRaises(StartupReadCancelled):
            bot._startup_retry_wait(8)

    def test_sdk_constructor_and_outcome_metadata_both_retry(self):
        cfg = build_config(dict(market='#101', order_size_usd=25, max_position_usd=200), None)
        cfg.live = False
        info = SimpleNamespace(post=Mock(side_effect=[requests.ReadTimeout('outcome'), {'outcomes': []}]))
        with patch('zeus.bots.worldcup.Info', side_effect=[requests.ReadTimeout('SDK'), info]) as sdk, \
                patch.object(HyperliquidSpreadBot, '_startup_retry_wait'), \
                patch.object(HyperliquidSpreadBot, 'discover_markets', return_value=[]), \
                patch('zeus.bots.worldcup.signal.signal'), patch('zeus.bots.worldcup.supervisor_alive', return_value=True):
            bot = HyperliquidSpreadBot(cfg)
        self.assertEqual(sdk.call_count, 2)
        self.assertEqual(info.post.call_count, 2)
        self.assertIsNone(bot.exchange)
        self.assertIsNone(sdk.call_args.kwargs['perp_dexs'])

    def test_exchange_constructor_retries_metadata_not_orders(self):
        bot = self.bare()
        bot.cfg = SimpleNamespace(private_key='dummy', account_address='offline', base_url='offline', worldcup=True, stop_file=None)
        bot.outcome_meta = None
        exchange = Mock()
        with patch('eth_account.Account.from_key', return_value=Mock()), \
                patch('zeus.bots.worldcup.Exchange', side_effect=[requests.ReadTimeout('SDK'), exchange]) as sdk, \
                patch.object(bot, '_startup_retry_wait'):
            bot._init_exchange(None)
        self.assertEqual(sdk.call_count, 2)
        exchange.order.assert_not_called()
        exchange.bulk_orders.assert_not_called()

    def test_live_sdk_metadata_is_loaded_once_and_reused_by_all_clients(self):
        cfg = build_config(dict(market='#101', order_size_usd=25, max_position_usd=200), None)
        cfg.private_key, cfg.account_address = 'dummy', 'offline'
        meta, spot = {'universe': []}, {'tokens': [], 'universe': []}
        info, exchange = Mock(), Mock()
        info.post.return_value = {'outcomes': []}
        with patch('zeus.outcome_reads.read_info', side_effect=[meta, spot]) as reads, \
                patch('zeus.bots.worldcup.Info', return_value=info) as sdk, \
                patch('zeus.bots.worldcup.Exchange', return_value=exchange) as exchange_sdk, \
                patch('eth_account.Account.from_key', return_value=Mock()), \
                patch.object(HyperliquidSpreadBot, 'discover_markets', return_value=[]), \
                patch('zeus.bots.worldcup.signal.signal'), \
                patch('zeus.bots.worldcup.supervisor_alive', return_value=True):
            bot = HyperliquidSpreadBot(cfg)
            try:
                bot.start_ws()
                self.assertEqual(reads.call_count, 2)
                self.assertEqual(sdk.call_count, 2)
                for call in sdk.call_args_list + exchange_sdk.call_args_list:
                    self.assertIs(call.kwargs['meta'], meta)
                    self.assertIs(call.kwargs['spot_meta'], spot)
            finally:
                bot.account_reader.close()

    def test_exact_outcome_startup_does_not_refetch_global_catalog(self):
        bot = self.bare()
        bot.cfg = build_config(dict(market='#101', order_size_usd=25, max_position_usd=200,
            auto_selection={'name': 'Pinned market'}), None)
        bot.outcome_meta = {'outcomes': [{'outcome': 10, 'venue': 'out'}]}
        with patch('zeus.outcomes.catalog_snapshot', side_effect=AssertionError('Duplicate catalog fetch')):
            spec, = bot.discover_worldcup_markets()
        self.assertEqual((spec.symbol, spec.balance_coin, spec.display_name), ('#101', '+101', 'Pinned market'))

    def test_delisted_exact_outcome_fails_before_trading(self):
        bot = self.bare()
        bot.cfg = build_config(dict(market='#101', order_size_usd=25, max_position_usd=200), None)
        bot.outcome_meta = {'outcomes': [{'outcome': 99, 'venue': 'out'}]}
        with self.assertRaisesRegex(ValueError, 'unavailable'):
            bot.discover_worldcup_markets()


if __name__ == '__main__':
    unittest.main()
