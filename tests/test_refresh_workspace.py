import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zeus.maintenance import maintenance_active
from zeus.process_control import ProcessLock

spec = importlib.util.spec_from_file_location('check_workspace_idle', Path(__file__).resolve().parents[1]/'deploy/check_workspace_idle.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class RefreshWorkspaceTests(unittest.TestCase):
    def test_stale_marker_does_not_block_but_a_live_os_lock_does(self):
        with tempfile.TemporaryDirectory() as folder, patch('zeus.maintenance._legacy_update_running', return_value=False):
            root = Path(folder)
            (root/'maintenance').write_text('Workspace update 12345\n')
            self.assertFalse(maintenance_active(root))
            with ProcessLock(root/'maintenance.lock', timeout=0):
                self.assertTrue(maintenance_active(root))
            self.assertFalse(maintenance_active(root))

    def test_legacy_live_updater_is_preserved_without_trusting_other_processes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root/'maintenance').write_text('Workspace update 12345\n')
            with patch('zeus.maintenance._legacy_update_running', return_value=True):
                self.assertTrue(maintenance_active(root))
            (root/'maintenance').write_text('broken marker')
            self.assertFalse(maintenance_active(root))

    def test_cancel_only_and_corrupt_journals_block_deployment(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root/'a.json').write_text(json.dumps({'session_id': 'a', 'cleanup_confirmed': False}))
            (root/'broken.json').write_text('not json')
            (root/'api-budget.json').write_text('{}')
            result = module.unresolved([root])
            self.assertIn('a', result)
            self.assertTrue(any('broken.json' in row for row in result))
            self.assertFalse(any('api-budget' in row for row in result))

    def test_only_a_confirmed_successor_for_the_exact_account_market_resolves(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            old = {'session_id': 'a', 'account': 'one', 'symbols': ['#101'],
                   'resumed_session_id': 'b', 'cleanup_confirmed': False}
            child = {'session_id': 'b', 'account': 'one', 'symbols': ['#101'], 'cleanup_confirmed': True}
            (root/'a.json').write_text(json.dumps(old))
            path = root/'b.json'
            path.write_text(json.dumps(child))
            self.assertEqual(module.unresolved([root]), [])
            child['symbols'] = ['#102']
            path.write_text(json.dumps(child))
            self.assertIn('a', module.unresolved([root]))
