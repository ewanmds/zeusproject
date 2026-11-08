"""Real Windows file-lock regression and bounded UI-only retry behavior."""
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from zeus.safety import SafetyLatch, atomic_json, read_json
from zeus.outcome_telemetry import OutcomeTelemetry


class TelemetryTests(unittest.TestCase):
    def test_locked_dashboard_snapshot_recovers_after_reader_closes(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'stats.json'
            atomic_json(path, {'revision': 1})
            publisher = OutcomeTelemetry(path)
            with path.open('rb') as old_reader:
                published = publisher.write({'revision': 2})
                self.assertEqual(published, os.name != 'nt')
                self.assertIn(b'1', old_reader.read())
                self.assertEqual(read_json(path)['revision'], 1 if os.name == 'nt' else 2)
            self.assertTrue(publisher.write({'revision': 3}))
            self.assertEqual(read_json(path)['revision'], 3)

    def test_transient_permission_error_keeps_old_timestamp_then_recovers(self):
        write = Mock(side_effect=[PermissionError('sharing violation'), None])
        publisher = OutcomeTelemetry('offline', publish=write)
        self.assertFalse(publisher.write({'ts': 1}))
        self.assertTrue(publisher.write({'ts': 2}))
        self.assertEqual(write.call_count, 2)
        self.assertEqual(write.call_args.args[1], {'ts': 2, 'telemetry_write_retries': 1})
        self.assertIsNone(publisher.failed_since)

    def test_permanent_permission_error_remains_fail_closed(self):
        now = [0]
        publisher = OutcomeTelemetry('offline', publish=Mock(side_effect=PermissionError('locked')),
                                     clock=lambda: now[0])
        self.assertFalse(publisher.write({'ts': 1}))
        now[0] = 6
        with self.assertRaises(PermissionError):
            publisher.write({'ts': 2})

    def test_other_disk_errors_are_not_suppressed(self):
        publisher = OutcomeTelemetry('offline', publish=Mock(side_effect=OSError('disk full')))
        with self.assertRaises(OSError):
            publisher.write({'ts': 1})

    def test_success_resets_the_failure_grace_period(self):
        now = [0]
        write = Mock(side_effect=[PermissionError('locked'), None, PermissionError('locked')])
        publisher = OutcomeTelemetry('offline', publish=write, clock=lambda: now[0])
        self.assertFalse(publisher.write({'ts': 1}))
        now[0] = 4
        self.assertTrue(publisher.write({'ts': 2}))
        now[0] = 20
        self.assertFalse(publisher.write({'ts': 3}))
        self.assertEqual(publisher.failed_since, 20)

    def test_final_snapshot_retries_without_losing_cleanup_confirmation(self):
        write = Mock(side_effect=[PermissionError('locked'), None])
        publisher = OutcomeTelemetry('offline', publish=write, sleep=lambda _: None)
        self.assertTrue(publisher.final({'cleanup_confirmed': True}))
        self.assertTrue(write.call_args.args[1]['cleanup_confirmed'])

    def test_final_snapshot_does_not_retry_a_permanent_lock_forever(self):
        now = [0]
        def sleep(seconds):
            now[0] += seconds
        publisher = OutcomeTelemetry('offline', publish=Mock(side_effect=PermissionError('locked')),
                                     clock=lambda: now[0], sleep=sleep)
        with self.assertRaises(PermissionError):
            publisher.final({'cleanup_confirmed': True}, timeout=.25)
        self.assertEqual(now[0], .25)

    def test_worker_does_not_halt_for_transient_ui_lock(self):
        from zeus.bots.outcome_worker import run_worker
        injected, recovered = threading.Event(), threading.Event()
        def publish(path, data):
            if data.get('phase') == 'running':
                if not injected.is_set():
                    injected.set()
                    raise PermissionError('offline snapshot lock')
                atomic_json(path, data)
                recovered.set()
            else:
                atomic_json(path, data)

        class MiniBot:
            def __init__(self, cfg):
                self.cfg = cfg
                self.safety = SafetyLatch(path=cfg.safety_file)

            def run(self):
                self.safety.phase = 'running'
                self.safety.publish()
                if not injected.wait(3) or not recovered.wait(3):
                    raise AssertionError('Expected injected failure and recovery')
                if self.safety.stopped.is_set():
                    raise AssertionError('UI lock halted execution')

            def request_stop(self):
                self.safety.stop()

            def shutdown(self):
                self.safety.cleanup(lambda: [(self.cfg.symbols[0], 0, [])],
                                    Mock(side_effect=AssertionError('Unexpected cancel')),
                                    Mock(side_effect=AssertionError('Unexpected close')),
                                    sleep=lambda _: None)

            def export_dashboard_stats(self):
                return {'volume_usd': 0, 'fills': 0, 'position_usd': 0}

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            journal = root/'journal.json'
            config = root/'config.json'
            atomic_json(journal, {'session_id': 'offline-telemetry'})
            atomic_json(config, {'safety_file': str(journal), 'funds_root': str(root/'funds'),
                                 'locks_root': str(root/'locks'),
                                 'config': {'market': '#101', 'order_size_usd': 25, 'max_position_usd': 200}})
            with patch.dict(os.environ, {'WALLET_ADDRESS_OUTCOME': 'offline', 'PRIVATE_KEY_OUTCOME': 'dummy'}), \
                    patch('zeus.bots.outcome_worker.supervisor_alive', return_value=True), \
                    patch('zeus.bots.outcome_worker.OutcomeTelemetry',
                          side_effect=lambda path: OutcomeTelemetry(path, publish=publish)):
                self.assertEqual(run_worker(config, bot_factory=MiniBot), 0)
            self.assertTrue(recovered.is_set())
            self.assertTrue(read_json(journal)['cleanup_confirmed'])
            self.assertEqual(read_json(journal)['error'], '')
            snapshot = read_json(root/'stats.json')
            self.assertTrue(snapshot['cleanup_confirmed'])
            self.assertEqual(snapshot['telemetry_write_retries'], 1)


if __name__ == '__main__':
    unittest.main()
