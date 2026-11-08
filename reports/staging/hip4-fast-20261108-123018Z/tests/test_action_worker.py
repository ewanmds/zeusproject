"""Bounded action scheduling and shutdown; no real exchange or network."""
import threading
import time
import unittest
from unittest.mock import Mock

from zeus.action_worker import LatestActionWorker
from zeus.latency import LatencyMetrics


class LatestActionWorkerTests(unittest.TestCase):
    def test_idle_worker_never_submits_spontaneously(self):
        callback=Mock();worker=LatestActionWorker(callback)
        try:
            self.assertFalse(worker.state()["active"])
            self.assertFalse(worker.state()["pending"])
            callback.assert_not_called()
        finally:self.assertTrue(worker.close())

    def test_blocked_callback_and_thousand_updates_keep_one_pending_latest_state(self):
        entered=threading.Event();release=threading.Event();second=threading.Event()
        calls=[];latest=[0];active=[0];peak=[0]
        def callback():
            active[0]+=1;peak[0]=max(peak[0],active[0])
            calls.append(latest[0])
            if len(calls)==1:
                entered.set();release.wait(3)
            else:second.set()
            active[0]-=1
        metrics=LatencyMetrics();worker=LatestActionWorker(callback,metrics=metrics)
        try:
            self.assertTrue(worker.request());self.assertTrue(entered.wait(1))
            for i in range(1,1001):
                latest[0]=i;self.assertTrue(worker.request())
            self.assertEqual(calls,[0])
            self.assertTrue(worker.state()["active"])
            self.assertTrue(worker.state()["pending"])
            release.set();self.assertTrue(second.wait(1))
            self.assertEqual(calls,[0,1000]);self.assertEqual(peak[0],1)
            self.assertEqual(metrics.snapshot()["counts"]["action_worker_coalesced"],999)
        finally:release.set();worker.close()

    def test_callback_does_not_hold_the_condition_or_block_owner_state_reads(self):
        entered=threading.Event();release=threading.Event()
        worker=LatestActionWorker(lambda:(entered.set(),release.wait(3)))
        try:
            worker.request();self.assertTrue(entered.wait(1))
            acquired=worker.condition.acquire(timeout=.1)
            self.assertTrue(acquired)
            if acquired:worker.condition.release()
            self.assertTrue(worker.state()["active"])
        finally:release.set();worker.close()

    def test_close_is_bounded_discards_pending_and_never_restarts_after_late_completion(self):
        entered=threading.Event();release=threading.Event();calls=[]
        def callback():calls.append(1);entered.set();release.wait(3)
        worker=LatestActionWorker(callback)
        try:
            worker.request();self.assertTrue(entered.wait(1));worker.request()
            started=time.monotonic()
            self.assertFalse(worker.close(timeout=.05))
            self.assertLess(time.monotonic()-started,.5)
            self.assertFalse(worker.state()["pending"])
            self.assertFalse(worker.request())
            release.set();worker.thread.join(1)
            self.assertFalse(worker.thread.is_alive())
            self.assertEqual(calls,[1]);self.assertFalse(worker.request())
        finally:release.set();worker.close()

    def test_pending_request_before_close_is_discarded_without_any_followup(self):
        entered=threading.Event();release=threading.Event();calls=[]
        def callback():calls.append(1);entered.set();release.wait(3)
        worker=LatestActionWorker(callback)
        try:
            worker.request();self.assertTrue(entered.wait(1))
            worker.request();worker.close(0)
            self.assertFalse(worker.state()["pending"])
            release.set();self.assertTrue(worker.close(1))
            self.assertEqual(len(calls),1)
            self.assertTrue(worker.close(1))
        finally:release.set();worker.close()

    def test_callback_failure_interlock_runs_once_and_queued_work_is_not_replayed(self):
        entered=threading.Event();release=threading.Event();reported=threading.Event()
        errors=[];calls=[]
        def callback():
            calls.append(1);entered.set();release.wait(3);raise RuntimeError("failing action")
        def on_error(exc):errors.append(exc);reported.set()
        worker=LatestActionWorker(callback,on_error=on_error)
        try:
            worker.request();self.assertTrue(entered.wait(1));worker.request()
            release.set();self.assertTrue(reported.wait(1));worker.thread.join(1)
            self.assertEqual(len(errors),1);self.assertEqual(len(calls),1)
            self.assertTrue(worker.state()["failed"])
            self.assertEqual(worker.state()["error_type"],"RuntimeError")
            self.assertFalse(worker.request())
        finally:release.set();worker.close()

    def test_error_handler_failure_preserves_original_error_and_remains_stopped(self):
        original=RuntimeError("original")
        def callback():raise original
        def on_error(exc):raise ValueError("reporting also failed")
        worker=LatestActionWorker(callback,on_error=on_error)
        worker.request();worker.thread.join(1)
        self.assertIs(worker.failure,original)
        self.assertTrue(worker.state()["stopping"])
        self.assertFalse(worker.request());self.assertTrue(worker.close())

    def test_system_exit_in_action_is_captured_as_terminal_worker_failure(self):
        reported=Mock()
        def callback():raise SystemExit("terminal")
        worker=LatestActionWorker(callback,on_error=reported)
        worker.request();worker.thread.join(1)
        reported.assert_called_once()
        self.assertEqual(worker.state()["error_type"],"SystemExit")
        self.assertFalse(worker.request());worker.close()

    def test_error_callback_can_close_without_joining_its_own_thread(self):
        completed=threading.Event();result=[];holder=[]
        def callback():raise ValueError("bad callback")
        def on_error(exc):result.append(holder[0].close());completed.set()
        worker=LatestActionWorker(callback,on_error=on_error);holder.append(worker)
        worker.request();self.assertTrue(completed.wait(1));worker.thread.join(1)
        self.assertEqual(result,[False]);self.assertTrue(worker.close())

    def test_invalid_timeouts_and_callback_are_rejected(self):
        with self.assertRaises(TypeError):LatestActionWorker(None)
        worker=LatestActionWorker(lambda:None)
        try:
            for value in (-1,float("nan"),float("inf")):
                with self.assertRaises(ValueError):worker.close(value)
        finally:worker.close()


if __name__=="__main__":unittest.main()
