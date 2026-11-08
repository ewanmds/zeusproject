"""One serial action callback and one coalesced pending wake-up.

The owner supplies a no-argument callback that computes its current target at
execution time. No quote payload, exchange object, file or HTTP client is
stored here. An in-flight callback cannot be interrupted by closing the worker;
its owner still enforces Stop and request uncertainty before any exchange send.
"""
import math
import threading
import time


class LatestActionWorker:
    def __init__(self, callback, on_error=None, metrics=None, thread_name="mm-actions"):
        if not callable(callback):
            raise TypeError("Action worker callback must be callable")
        self.callback = callback
        self.on_error = on_error
        self.metrics = metrics
        self.condition = threading.Condition()
        self.stop_event = threading.Event()
        self.active = False
        self.pending = False
        self.failure = None
        self.thread = threading.Thread(target=self._run, name=thread_name, daemon=True)
        self.thread.start()

    def request(self):
        with self.condition:
            if self.stop_event.is_set():
                return False
            coalesced = self.pending
            self.pending = True
            self.condition.notify()
        if self.metrics:
            self.metrics.count("action_worker_requests")
            if coalesced:
                self.metrics.count("action_worker_coalesced")
        return True

    def state(self):
        with self.condition:
            return {"active": self.active, "pending": self.pending,
                    "alive": self.thread.is_alive(), "stopping": self.stop_event.is_set(),
                    "failed": self.failure is not None,
                    "error_type": type(self.failure).__name__ if self.failure is not None else ""}

    def close(self, timeout=1):
        timeout = float(timeout)
        if not math.isfinite(timeout) or timeout < 0:
            raise ValueError("Action worker close timeout must be finite and nonnegative")
        with self.condition:
            self.stop_event.set()
            self.pending = False
            self.condition.notify_all()
        if threading.current_thread() is self.thread:
            return False  # an error handler can close the worker, but not join itself
        self.thread.join(timeout)
        return not self.thread.is_alive()

    def _run(self):
        while True:
            with self.condition:
                self.condition.wait_for(lambda:self.pending or self.stop_event.is_set())
                if self.stop_event.is_set():
                    return
                self.pending = False
                self.active = True
            started = time.perf_counter_ns()
            try:
                self.callback()
            except BaseException as exc:
                # A failed callback is terminal for this worker. It must never
                # drain a queued quote after reporting an execution interlock.
                with self.condition:
                    self.failure = exc
                    self.stop_event.set()
                    self.pending = False
                if self.metrics:
                    self.metrics.count("action_worker_errors")
                if self.on_error:
                    try:
                        self.on_error(exc)
                    except BaseException:
                        # Preserve the original failure and stay stopped even
                        # if the owner's reporting/interlock callback also fails.
                        if self.metrics:
                            self.metrics.count("action_worker_error_handler_errors")
            finally:
                if self.metrics:
                    self.metrics.observe("action_worker_callback", (time.perf_counter_ns()-started)/1e6)
                    self.metrics.count("action_worker_callbacks")
                with self.condition:
                    self.active = False
                    self.condition.notify_all()
