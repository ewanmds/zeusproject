"""Offline regression tests for locked telemetry; no wallet or exchange client."""
import ast
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import stats_io


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.target = self.root / 'stats.json'
        self.target.write_text('{"fills": 2}', encoding='utf-8')

    def test_transient_sharing_violation_retries_closed_unique_file(self):
        original = os.replace
        sources = []
        def replace(source, target):
            sources.append(Path(source))
            if len(sources) < 3:
                raise PermissionError(13, 'Windows reader busy')
            # Real Windows replace also verifies the temporary handle is closed.
            original(source, target)
        with patch.object(stats_io.os, 'replace', side_effect=replace), patch.object(stats_io.time, 'sleep'):
            self.assertTrue(stats_io.write_json_snapshot(self.target, {'fills': 3}))
        self.assertEqual(json.loads(self.target.read_text()), {'fills': 3})
        self.assertEqual(len(sources), 3)
        self.assertEqual(sources[0].parent, self.target.parent)
        self.assertNotEqual(sources[0], self.target.with_suffix('.json.tmp'))
        self.assertEqual(list(self.root.glob('*.tmp')), [])

    def test_persistent_lock_retains_previous_json_then_recovers(self):
        with patch.object(stats_io.os, 'replace', side_effect=PermissionError(13, 'busy')) as replace, \
             patch.object(stats_io.time, 'sleep'), patch.object(stats_io._logger, 'warning'):
            self.assertFalse(stats_io.write_json_snapshot(self.target, {'fills': 3}))
        self.assertEqual(replace.call_count, 3)
        self.assertEqual(json.loads(self.target.read_text()), {'fills': 2})
        self.assertEqual(list(self.root.glob('*.tmp')), [])
        self.assertTrue(stats_io.write_json_snapshot(self.target, {'fills': 4}))
        self.assertEqual(json.loads(self.target.read_text()), {'fills': 4})

    def test_write_permission_error_is_nonfatal(self):
        with patch.object(stats_io.tempfile, 'NamedTemporaryFile', side_effect=PermissionError(13, 'denied')), \
             patch.object(stats_io._logger, 'warning'):
            self.assertFalse(stats_io.write_json_snapshot(self.target, {'fills': 3}))
        self.assertEqual(json.loads(self.target.read_text()), {'fills': 2})

    def test_io_failure_without_existing_snapshot_is_nonfatal(self):
        absent = self.root / 'absent.json'
        with patch.object(stats_io.os, 'replace', side_effect=OSError(28, 'disk full')), \
             patch.object(stats_io._logger, 'warning'):
            self.assertFalse(stats_io.write_json_snapshot(absent, {'phase': 'connecting'}))
        self.assertFalse(absent.exists())
        self.assertEqual(list(self.root.glob('*.tmp')), [])

    def test_serialization_failure_does_not_replace_good_snapshot(self):
        with self.assertRaises(ValueError):
            stats_io.write_json_snapshot(self.target, {'mid': float('nan')})
        self.assertEqual(json.loads(self.target.read_text()), {'fills': 2})

    def test_failures_are_rate_limited_in_logs(self):
        with patch.object(stats_io.os, 'replace', side_effect=OSError(13, 'denied')), \
             patch.object(stats_io.time, 'monotonic', side_effect=[100, 101, 130]), \
             patch.object(stats_io._logger, 'warning') as warning:
            for _ in range(3):
                self.assertFalse(stats_io.write_json_snapshot(self.target, {'fills': 3}))
        self.assertEqual(warning.call_count, 2)

    @unittest.skipUnless(os.name == 'nt', 'Windows sharing semantics')
    def test_real_windows_open_reader_cannot_crash_publisher(self):
        with self.target.open('r', encoding='utf-8') as reader, patch.object(stats_io._logger, 'warning'):
            published = stats_io.write_json_snapshot(self.target, {'fills': 3})
            self.assertEqual(json.load(reader), {'fills': 2})
        # Filesystem may allow delete sharing; both outcomes must remain valid.
        self.assertEqual(json.loads(self.target.read_text()), {'fills': 3 if published else 2})
        self.assertTrue(stats_io.write_json_snapshot(self.target, {'fills': 4}))

    def test_production_loop_keeps_trading_when_snapshot_is_locked(self):
        # Compile the production control/run methods, avoiding credential imports.
        path = Path(__file__).resolve().parents[1] / 'grid_adaptive/runtime.py'
        tree = ast.parse(path.read_text(encoding='utf-8'))
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'ZeusGrid')
        methods = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in {'control', 'run'}]
        namespace = dict(time=time, threading=threading, engine=SimpleNamespace(LOOP_INTERVAL_SEC=0, MIN_SIZE=1))
        exec(compile(ast.Module(body=methods, type_ignores=[]), str(path), 'exec'), namespace)
        bot = SimpleNamespace(running=True, ws_ready=True, last_stats_write=0,
                              stop_path=self.root / 'stop.flag', wake=threading.Event(),
                              position=0, order_book=SimpleNamespace(orders={}, uncertain=False),
                              integration_error='', _sync_thread=Mock(), flatten=Mock(),
                              sync_recent_fills=Mock(), sync_position=Mock(), _record_metrics=Mock(),
                              ws_info=Mock(), journal=Mock())
        bot.control = lambda: namespace['control'](bot)
        bot.write_stats = lambda phase: stats_io.write_json_snapshot(self.target, {'phase': phase})
        def cycle():
            bot.running = False
        bot.loop_once = Mock(side_effect=cycle)
        with patch.object(stats_io.os, 'replace', side_effect=PermissionError(13, 'reader busy')), \
             patch.object(stats_io.time, 'sleep'), patch.object(stats_io._logger, 'warning'), \
             patch.object(threading, 'Thread'):
            namespace['run'](bot)
        bot.loop_once.assert_called_once()
        self.assertEqual(bot.integration_error, '')
        bot.flatten.assert_called_once_with('stop')
        self.assertEqual(json.loads(self.target.read_text()), {'fills': 2})


if __name__ == '__main__':
    unittest.main()
