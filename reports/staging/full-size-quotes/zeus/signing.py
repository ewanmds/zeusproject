"""Serialize Python signed actions per agent across HIP-3 market processes."""
from contextlib import contextmanager
from pathlib import Path
import threading
import time
from zeus.process_control import ProcessLock, account_key
from zeus.safety import GuardedExchange, atomic_json, read_json

class SigningCoordinator:
    def __init__(self, root, signer, metrics=None):
        self.root=Path(root)/account_key(signer)
        self.lock=threading.RLock()
        self.metrics=metrics
        self.local=threading.local()

    def instrument_http(self, exchange):
        """Instrument this dedicated SDK instance, without changing its payload/signing."""
        original = exchange.post
        def post(path, payload=None):
            if path != "/exchange" or self.metrics is None:
                return original(path, payload)
            started = time.perf_counter_ns()
            sdk_started = getattr(self.local, "sdk_started_ns", None)
            if sdk_started is not None:
                self.metrics.observe("sdk_prepare_sign", (started-sdk_started)/1e6)
            received = False
            try:
                response = original(path, payload)
                received = True
                return response
            finally:
                self.metrics.observe("exchange_http" if received else "exchange_http_error",
                                     (time.perf_counter_ns()-started)/1e6)
        exchange.post = post

    def instrument_transport(self, exchange, transport):
        """Route signed SDK /exchange payloads over the selected WS transport.

        All non-exchange calls keep their original HTTP path. There is no HTTP
        fallback for exchange actions; transport owns uncertainty classification.
        """
        original = exchange.post
        def post(path, payload=None):
            if path != "/exchange":
                return original(path, payload)
            started = time.perf_counter_ns()
            sdk_started = getattr(self.local, "sdk_started_ns", None)
            if self.metrics and sdk_started is not None:
                self.metrics.observe("sdk_prepare_sign", (started-sdk_started)/1e6)
            received = False
            try:
                result = transport.post_action(payload)
                received = True
                return result
            finally:
                if self.metrics:
                    self.metrics.observe("exchange_ws" if received else "exchange_ws_error",
                                         (time.perf_counter_ns()-started)/1e6)
        exchange.post = post

    @contextmanager
    def transaction(self):
        waiting=time.perf_counter_ns()
        with self.lock, ProcessLock(self.root/"signing.lock"):
            if self.metrics:self.metrics.observe("signing_lock_wait",(time.perf_counter_ns()-waiting)/1e6)
            path=self.root/"nonce.json"
            previous=int(read_json(path)["last_submit_ms"]) if path.exists() else 0
            deadline=time.monotonic()+10
            while int(time.time()*1000)<=previous:
                if time.monotonic()>=deadline:
                    raise TimeoutError("Signing clock has not advanced; no action sent")
                time.sleep(0.002)
            try:
                yield
            finally:
                atomic_json(path,{"last_submit_ms":max(previous,int(time.time()*1000))})

    def wrap(self, exchange, before_send=None, after_send=None, before_action=None, after_action=None):
        coordinator=self
        class SerializedExchange:
            def __getattr__(self,name):
                method=getattr(exchange,name)
                if name not in GuardedExchange.ACTIONS: return method
                def call(*args, **kwargs):
                    began = False
                    try:
                        with coordinator.transaction():
                            if before_action:
                                before_action(name)
                                began = True
                            if before_send:
                                preparing = time.perf_counter_ns()
                                try:
                                    before_send(name, args, kwargs)
                                finally:
                                    if coordinator.metrics:
                                        coordinator.metrics.observe("submission_prepare", (time.perf_counter_ns()-preparing)/1e6)
                            started = time.perf_counter_ns()
                            coordinator.local.sdk_started_ns = started
                            received = False
                            try:
                                result = method(*args, **kwargs)
                                received = True
                                return result
                            finally:
                                coordinator.local.sdk_started_ns = None
                                if coordinator.metrics:
                                    coordinator.metrics.observe("sdk_to_response" if received else "sdk_error_elapsed",
                                                                (time.perf_counter_ns()-started)/1e6)
                                if after_send:
                                    after_send(name, received)
                    finally:
                        if began and after_action:
                            after_action(name)
                return call
        return SerializedExchange()
