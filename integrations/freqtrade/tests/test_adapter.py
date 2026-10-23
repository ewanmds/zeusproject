"""Offline Zeus/Freqtrade regressions. All account and exchange I/O is mocked."""
import io
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from zeus.bots import freqtrade as ft

META = [{'universe': [{'name': 'xyz:SP500', 'szDecimals': 3, 'maxLeverage': 6}]}, [{'midPx': '100'}]]


class AdapterTests(unittest.TestCase):
    def test_source_profile_and_single_market_budget(self):
        with patch.dict(os.environ, {'OBI_LEVERAGE': '20'}):
            config = ft.session_config('xyz:SP500', 40, META)
        self.assertNotIn('env', config)
        self.assertNotIn('wallet_address', config)
        self.assertEqual(config['strategy']['assets'], ['xyz:SP500'])
        self.assertEqual(config['market_making']['quote_notional'], 40)
        self.assertEqual(config['market_making']['ladder_levels'], 1)
        self.assertFalse(config['market_making']['adaptive_notional_enabled'])
        self.assertTrue(config['market_making']['edge_gate_enabled'])
        self.assertTrue(config['signal_pauses_enabled'])  # parité config.live.yaml du Bureau
        self.assertFalse(config['postmortem']['enabled'])
        self.assertTrue(config['risk']['session_flatten_on_shutdown'])
        self.assertEqual(config['risk']['max_position_notional'], 40)
        self.assertEqual(config['risk']['max_spread_exposure'], 44)
        self.assertEqual(config['risk']['max_open_orders'], 4)
        self.assertEqual(config['risk']['max_drawdown_pct'], 100)
        self.assertEqual(config['risk']['max_consecutive_halts'], 0)
        self.assertEqual(config['risk']['session_stop_loss_bps'], 0)
        self.assertEqual(config['execution']['leverage'], 6)
        self.assertTrue(config['rate_limit']['disabled'])
        self.assertEqual(config['risk']['fail_closed_cooldown_sec'], 15)
        self.assertEqual(config['risk']['max_fail_closed_retries'], 5)

    def test_full_budget_uses_one_order_per_side(self):
        config = ft.session_config('xyz:SP500', 100, META)
        self.assertEqual(config['market_making']['quote_notional'], 100)
        self.assertEqual(config['market_making']['ladder_levels'], 1)
        self.assertEqual(config['risk']['max_open_orders'], 4)
        config = ft.session_config('xyz:SP500', 400, META)
        self.assertEqual(config['market_making']['quote_notional'], 400)
        self.assertEqual(config['market_making']['ladder_levels'], 1)
        self.assertEqual(config['risk']['max_open_orders'], 4)

    def test_invalid_and_unexecutable_budget_is_rejected(self):
        for amount in (float('nan'), float('inf'), 9, 10.01, 39.99):
            # Zeus retains a conservative 40 USD minimum for full-budget quotes.
            meta = [{'universe': [{'name': 'xyz:SP500', 'szDecimals': 0, 'maxLeverage': 6}]}, [{'midPx': '100'}]]
            with self.subTest(amount=amount), self.assertRaises(ValueError):
                ft.session_config('xyz:SP500', amount, meta)

    def test_market_eligibility_warns_on_razor_thin_book(self):
        thin = {'levels': [[{'px': '100.00'}], [{'px': '100.0013'}]]}  # ~0.13 bps full spread
        with patch.object(ft, 'post_info', return_value=thin):
            edge = ft.market_eligibility('xyz:SP500')
        self.assertTrue(edge['warning'])
        wide = {'levels': [[{'px': '99.95'}], [{'px': '100.05'}]]}  # ~10 bps full spread
        with patch.object(ft, 'post_info', return_value=wide):
            edge = ft.market_eligibility('io:ANTH')
        self.assertGreater(edge['venue_half_bps'], 0)
        self.assertFalse(edge['warning'])

    def test_snapshot_identity_and_secret_whitelist(self):
        raw = dict(protocol=1, session_id='session', symbol='xyz:SP500', volume=10, PRIVATE_KEY='never-export')
        self.assertEqual(ft.accept_snapshot(raw, 'session', 'xyz:SP500'), {'volume': 10})
        self.assertIsNone(ft.accept_snapshot(raw, 'other', 'xyz:SP500'))
        raw['volume'] = float('nan')
        self.assertIsNone(ft.accept_snapshot(raw, 'session', 'xyz:SP500'))

    def test_missing_binary_fails_before_spawn(self):
        with patch.object(ft, 'binary_path', return_value=Path('missing-binary')), self.assertRaises(ValueError):
            ft.require_binary()

    def test_unverified_binary_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'bin').mkdir()
            binary = root / 'bin' / 'test.exe'
            binary.write_bytes(b'MZtest')
            binary.chmod(0o755)
            with patch.object(ft, 'ROOT', root), patch.object(ft, 'binary_path', return_value=binary):
                with self.assertRaisesRegex(ValueError, 'non vérifié.*--build'):
                    ft.require_binary()

    def test_stale_binary_is_reported_separately(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'bin').mkdir()
            binary = root / 'bin' / 'test.exe'
            binary.write_bytes(b'MZtest')
            binary.chmod(0o755)
            manifest = {'source': 'old-source', 'binaries': {binary.name: hashlib.sha256(b'MZtest').hexdigest()}}
            (root / 'bin' / 'build.json').write_text(json.dumps(manifest), encoding='utf-8')
            with patch.object(ft, 'ROOT', root), patch.object(ft, 'binary_path', return_value=binary), \
                 patch.object(ft, 'engine_digest', return_value='new-source'):
                with self.assertRaisesRegex(ValueError, 'périmé.*--build'):
                    ft.require_binary()

    def test_flat_confirmation_requires_valid_fresh_rest_state(self):
        cases = [({'assetPositions': []}, [], True),
                 ({'assetPositions': [{'position': {'coin': 'SP500', 'szi': '0.1'}}]}, [], False),
                 ({'assetPositions': []}, [{'coin': 'xyz:SP500'}], False),
                 ({'assetPositions': [None]}, [], False),
                 ({'assetPositions': [{'position': None}]}, [], False),
                 ({}, [], False)]
        for state, orders, expected in cases:
            with self.subTest(state=state, orders=orders), patch.object(ft, 'post_info', side_effect=[state, orders]):
                self.assertEqual(ft.confirm_flat('xyz:SP500', 'test-account'), expected)

    def test_snapshot_busy_windows_reader_is_nonfatal(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'stats.json'
            path.write_text('{"fills":2}')
            with patch.object(ft.os, 'replace', side_effect=PermissionError('busy')), patch.object(ft.time, 'sleep'):
                self.assertFalse(ft.publish(path, {'fills': 3}))
            self.assertEqual(json.loads(path.read_text()), {'fills': 2})
            self.assertEqual(list(Path(directory).glob('*.tmp')), [])
            self.assertTrue(ft.publish(path, {'fills': 4}))

    @unittest.skipUnless(os.name == 'nt', 'Windows test')
    def test_real_windows_reader(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'stats.json'
            path.write_text('{"fills":2}')
            with path.open('r'):
                ft.publish(path, {'fills': 3})
            self.assertTrue(ft.publish(path, {'fills': 4}))

    def test_preflight_can_be_cancelled_without_running_live(self):
        stop = threading.Event()
        stop.set()
        proc = Mock()
        proc.poll.return_value = None
        proc.__enter__ = Mock(return_value=proc)
        proc.__exit__ = Mock(return_value=False)
        with patch.object(ft.subprocess, 'Popen', return_value=proc) as spawn:
            with self.assertRaises(RuntimeError):
                ft.preflight(Path('fake.exe'), Path('config.json'), '-check-account', stop)
        self.assertEqual(spawn.call_args.args[0][-1], '-check-account')
        proc.terminate.assert_called_once()

    def test_runtime_consumes_protocol_and_confirms_stop_without_disk_logs(self):
        session = 'test-session'
        raw = dict(protocol=1, session_id=session, symbol='xyz:SP500', phase='stopped', running=False,
                   position_known=True, position=0, position_usd=0, volume=100, fees=0.01, rpnl=0.02,
                   fills=2, cost_per_1m=-100)
        child = Mock(returncode=0)
        child.poll.return_value = 0
        child.stdout = io.StringIO(json.dumps(raw) + '\n')
        snapshots = []
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(ft, 'acquire_lock'), patch.object(ft, 'require_binary', return_value=Path('fake.exe')), \
             patch.object(ft, 'post_info', side_effect=[META, {'levels': [[{'px': '99.9'}], [{'px': '100.1'}]]}]), \
             patch.object(ft, 'preflight') as checks, \
             patch.object(ft.uuid, 'uuid4', return_value=SimpleNamespace(hex=session)), \
             patch.object(ft.signal, 'signal'), patch('dotenv.load_dotenv'), \
             patch.object(ft, 'stop_file_path', return_value=str(Path(directory) / 'stop.flag')), \
             patch.object(ft, 'publish', side_effect=lambda _, data: snapshots.append(dict(data))), \
             patch.object(ft.subprocess, 'Popen', return_value=child) as spawn:
            self.assertEqual(ft.run('SP500', 40), 0)
        self.assertEqual([call.args[2] for call in checks.call_args_list], ['-check-account'])
        self.assertTrue(snapshots[-1]['position_known'])
        self.assertEqual(snapshots[-1]['phase'], 'stopped')
        args = spawn.call_args.args[0]
        self.assertIn('-zeus-stop-file', args)
        self.assertNotIn('PRIVATE_KEY', ' '.join(args))
        self.assertFalse(Path(args[2]).exists(), 'session configuration was not cleaned up')

    def test_cli_requires_explicit_live(self):
        with patch.object(sys, 'argv', ['freqtrade']), patch.object(ft, 'run') as run:
            with self.assertRaises(SystemExit):
                ft.main()
        run.assert_not_called()


class ServerTests(unittest.TestCase):
    def test_actual_import_and_xyz100_resolution(self):
        from zeus import server
        module = server._safe_import('zeus.bots.freqtrade')
        self.assertEqual(module.__name__, 'zeus.bots.freqtrade')
        self.assertEqual(module.resolve_token('XYZ100'), 'xyz:XYZ100')

    def test_dispatch_and_same_symbol_conflict(self):
        from zeus import server
        with tempfile.TemporaryDirectory() as directory:
            module = SimpleNamespace(resolve_token=lambda _: 'xyz:SP500', require_binary=Mock(),
                stop_file_path=lambda _: str(Path(directory) / 'stop.flag'),
                stats_file_path=lambda _: str(Path(directory) / 'stats.json'))
            proc = Mock(pid=123, returncode=None)
            proc.poll.return_value = None
            manager = server.BotManager()
            with patch.object(server, '_safe_import', return_value=module), \
                 patch.object(server.subprocess, 'Popen', return_value=proc) as spawn, patch.object(server.time, 'sleep'):
                manager.start_grid(server.GridStartRequest(strategy='freqtrade', order_size_usd=40))
                self.assertEqual(spawn.call_args.args[0][3], 'zeus.bots.freqtrade')
                self.assertEqual(spawn.call_args.kwargs['stdout'], subprocess.DEVNULL)
                with self.assertRaises(RuntimeError):
                    manager.start_grid(server.GridStartRequest(strategy='bbo', token='SP500_ALIAS'))
                self.assertEqual(spawn.call_count, 1)

    def test_unknown_position_is_not_zero_and_stop_failure_is_retained(self):
        from zeus import server
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            proc = Mock(returncode=1)
            proc.poll.return_value = 1
            manager = server.BotManager()
            started = time.time()
            (root / 'stats.json').write_text(json.dumps(dict(symbol='xyz:SP500', start_time=started,
                phase='failed', position_known=False, error='Network unavailable')))
            manager.obi_procs['SP500'] = dict(proc=proc, symbol='xyz:SP500', started=started,
                stats_file=str(root / 'stats.json'), stop_file=str(root / 'stop.flag'), config={'strategy': 'freqtrade', 'mode': 'live'})
            self.assertIsNone(manager.get_grid_stats()['bots'][0]['position_usd'])
            with self.assertRaises(RuntimeError):
                manager.stop_grid('SP500')
            self.assertIn('SP500', manager.obi_procs)

    def test_incomplete_accounting_hides_session_and_total_cpm(self):
        from zeus import server
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'stats.json'
            started = time.time()
            path.write_text(json.dumps(dict(symbol='xyz:SP500', start_time=started,
                phase='running', position_known=True, accounting_complete=False,
                volume=100, fees=0.01, rpnl=0)))
            proc = Mock(returncode=None)
            proc.poll.return_value = None
            manager = server.BotManager()
            manager.obi_procs['SP500'] = dict(proc=proc, symbol='xyz:SP500', started=started,
                stats_file=str(path), config={'strategy': 'freqtrade', 'mode': 'live'})
            result = manager.get_grid_stats()
            self.assertIsNone(result['cost_per_1m'])
            self.assertIsNone(result['bots'][0]['cost_per_1m'])


if __name__ == '__main__':
    unittest.main()
