"""Real SQLite contention and concurrent reservations; never contacts exchange."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import multiprocessing
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from zeus.network import HyperliquidError, StartupReadCancelled, request_info, retry_read
from zeus.process_control import ProcessLock
from zeus.read_cache import ReadBudget


def reserve_in_worker(root):
    budget = retry_read('budget initialization', lambda: ReadBudget(root, limit=20, ordinary=20))
    admitted = 0
    for _ in range(10):
        try:
            budget.acquire('l2Book')
            admitted += 1
        except HyperliquidError:
            pass
    return admitted


class ReadBudgetContentionTests(unittest.TestCase):
    def test_an_open_read_transaction_does_not_block_a_reservation(self):
        with tempfile.TemporaryDirectory() as root:
            budget = ReadBudget(root)
            with closing(sqlite3.connect(budget.path)) as reader:
                reader.execute('BEGIN')
                reader.execute('SELECT * FROM reads').fetchall()
                budget.acquire('l2Book')
                # Reader keeps its snapshot while the reservation commits.
                self.assertEqual(reader.execute('SELECT COUNT(*) FROM reads').fetchone()[0], 0)
                reader.rollback()
                self.assertEqual(reader.execute('SELECT COUNT(*) FROM reads').fetchone()[0], 1)

    def test_stop_observed_after_lock_wait_prevents_http_and_reservation(self):
        with tempfile.TemporaryDirectory() as root:
            budget = ReadBudget(root)
            cancelled = Mock(side_effect=[False, True])
            http = Mock()
            with patch('zeus.read_cache.read_budget', return_value=budget), \
                    patch('zeus.network._http', return_value=http):
                with self.assertRaises(StartupReadCancelled):
                    request_info('https://example.test', {'type': 'l2Book'}, cancelled=cancelled)
            http.post.assert_not_called()
            with closing(sqlite3.connect(budget.path)) as check:
                self.assertEqual(check.execute('SELECT COUNT(*) FROM reads').fetchone()[0], 0)

    def test_sqlite_writer_lock_defers_http_and_recovers_after_release(self):
        with tempfile.TemporaryDirectory() as root:
            budget = ReadBudget(root)
            db = sqlite3.connect(budget.path)
            db.execute('BEGIN IMMEDIATE')
            http = Mock()
            try:
                with patch('zeus.read_cache.read_budget', return_value=budget), \
                        patch('zeus.network._http', return_value=http):
                    before = time.monotonic()
                    with self.assertRaises(HyperliquidError) as caught:
                        request_info('https://example.test', {'type': 'l2Book'})
                    self.assertEqual(caught.exception.operation, 'rest_read_budget')
                    self.assertEqual(caught.exception.status, 503)
                    self.assertLess(time.monotonic()-before, 1.5)
                    http.post.assert_not_called()
            finally:
                db.rollback()
                db.close()
            budget.acquire('l2Book')
            with closing(sqlite3.connect(budget.path)) as check:
                self.assertEqual(check.execute('SELECT SUM(weight) FROM reads').fetchone()[0], 2)

    def test_worker_initialization_lock_is_typed_and_bounded(self):
        with tempfile.TemporaryDirectory() as root:
            budget = ReadBudget(root)
            with ProcessLock(budget.path.with_suffix('.lock')):
                with self.assertRaises(HyperliquidError):
                    ReadBudget(root)
            ReadBudget(root).acquire('l2Book')

    def test_only_read_retry_retries_after_lock_release(self):
        with tempfile.TemporaryDirectory() as root:
            budget = ReadBudget(root)
            db = sqlite3.connect(budget.path)
            db.execute('BEGIN IMMEDIATE')
            try:
                attempts = []
                def read():
                    attempts.append(1)
                    budget.acquire('l2Book')
                    return 'read completed'
                def release(_):
                    db.rollback()
                self.assertEqual(retry_read('budget test', read, sleep=release), 'read completed')
                self.assertEqual(len(attempts), 2)
            finally:
                db.close()

    def test_parallel_processes_do_not_oversubscribe_shared_quota(self):
        with tempfile.TemporaryDirectory() as root:
            ReadBudget(root)
            with multiprocessing.get_context('spawn').Pool(3) as pool:
                admitted = pool.map(reserve_in_worker, [root]*3)
            self.assertEqual(sum(admitted), 10)
            with closing(sqlite3.connect(ReadBudget(root).path)) as check:
                self.assertEqual(check.execute('SELECT SUM(weight) FROM reads').fetchone()[0], 20)

    def test_concurrent_threads_reuse_budget_without_raw_lock_errors(self):
        with tempfile.TemporaryDirectory() as root:
            ReadBudget(root)
            with ThreadPoolExecutor(8) as pool:
                admitted = list(pool.map(reserve_in_worker, [root]*8))
            self.assertEqual(sum(admitted), 10)

    def test_cooldown_survives_a_contended_persistence_attempt(self):
        with tempfile.TemporaryDirectory() as root:
            budget = ReadBudget(root)
            db = sqlite3.connect(budget.path)
            db.execute('BEGIN IMMEDIATE')
            try:
                budget.cooldown(120)
            finally:
                db.rollback()
                db.close()
            for worker in (budget, ReadBudget(root)):
                with self.assertRaises(HyperliquidError) as caught:
                    worker.acquire('l2Book')
                self.assertEqual(caught.exception.status, 429)
                self.assertGreater(caught.exception.retry_after, 110)

    def test_non_lock_database_errors_are_not_disguised_as_transient(self):
        with tempfile.TemporaryDirectory() as root:
            budget = ReadBudget(root)
            with closing(sqlite3.connect(budget.path)) as db:
                db.execute('DROP TABLE reads')
            with self.assertRaisesRegex(sqlite3.OperationalError, 'no such table'):
                budget.acquire('l2Book')
