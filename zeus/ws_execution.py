"""Persistent Hyperliquid signed-action transport, separate from market data.

The SDK still builds/signs the payload. This module only envelopes it in a WS
post request and returns the exact action payload of the matching ACK. There
is one active call, no execution queue, and no retry or HTTP fallback.
"""
from dataclasses import dataclass, field
import json
import math
import threading
import time
from urllib.parse import urlsplit, urlunsplit

from zeus.safety import SubmissionSkipped, safe_error


class UncertainActionError(RuntimeError):
    """Bytes may have left: caller must reconcile the original action, not retry."""


@dataclass
class _Pending:
    request_id: int
    done: threading.Event = field(default_factory=threading.Event)
    result: object = None
    error: object = None


def websocket_url(base_url):
    parsed = urlsplit(base_url)
    if parsed.scheme not in ("https", "http", "wss", "ws") or not parsed.netloc:
        raise ValueError("Invalid execution WebSocket URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Execution WebSocket URL must not contain credentials or query parameters")
    scheme = {"https":"wss", "http":"ws"}.get(parsed.scheme, parsed.scheme)
    path = parsed.path.rstrip("/")
    if not path.endswith("/ws"):
        path += "/ws"
    return urlunsplit((scheme, parsed.netloc, path, "", ""))


class WebSocketExecution:
    """One daemon reader, one socket, one pending action; no signed data logged.

    start()/wait_ready() may be used during startup to prewarm the connection.
    post_action() never connects, waits for a handshake, or retries a request.
    final_gate(payload) runs immediately before send, on the calling/signing
    thread. It must validate only: changing a signed payload is invalid.
    """
    def __init__(self, base_url, metrics=None, final_gate=None, socket_factory=None,
                 request_timeout=10.0, connect_timeout=3.0, poll_timeout=.25,
                 reconnect_delay=.5, max_reconnect_delay=8.0, heartbeat_interval=20.0):
        values=(request_timeout,connect_timeout,poll_timeout,reconnect_delay,max_reconnect_delay,heartbeat_interval)
        if any(not math.isfinite(value) or value<=0 for value in values):
            raise ValueError("Execution transport timeouts must be finite and positive")
        self.url = websocket_url(base_url)
        self.metrics, self.final_gate = metrics, final_gate
        self.socket_factory = socket_factory
        self.request_timeout, self.connect_timeout = request_timeout, connect_timeout
        self.poll_timeout = poll_timeout
        self.reconnect_delay, self.max_reconnect_delay = reconnect_delay, max(reconnect_delay, max_reconnect_delay)
        self.heartbeat_interval = heartbeat_interval
        self.condition = threading.Condition()
        self.action_lock, self.send_lock = threading.Lock(), threading.Lock()
        self.stop_event = threading.Event()
        self.socket = self.thread = self.pending = None
        self.next_id = 0
        self.generation = 0
        self.last_error = ""

    def _count(self, name):
        if self.metrics:
            self.metrics.count(name)

    def start(self):
        with self.condition:
            if self.stop_event.is_set():
                raise RuntimeError("Execution transport is closed")
            if self.thread is None:
                self.thread = threading.Thread(target=self._run, name="mm-ws-execution", daemon=True)
                self.thread.start()
        return self

    def wait_ready(self, timeout=3.0):
        with self.condition:
            self.condition.wait_for(lambda:self.socket is not None or self.stop_event.is_set(), timeout)
            return self.socket is not None and not self.stop_event.is_set()

    def state(self):
        with self.condition:
            return {"kind":"websocket", "ready":self.socket is not None and not self.stop_event.is_set(),
                    "in_flight":self.pending is not None, "generation":self.generation,
                    "alive":self.thread is not None and self.thread.is_alive(),
                    "stopping":self.stop_event.is_set(), "last_error":self.last_error}

    @staticmethod
    def _shutdown(socket):
        if socket is not None:
            try:
                socket.shutdown()  # closes immediately; no blocking close handshake
            except Exception:
                pass

    def _finish_locked(self, pending, result=None, error=None):
        if self.pending is pending:
            self.pending = None
            pending.result, pending.error = result, error
            pending.done.set()

    def _drop(self, socket, reason):
        with self.condition:
            if self.socket is socket:
                self.socket = None
                self.last_error = safe_error(reason)
                if self.pending is not None:
                    self._finish_locked(self.pending, error=UncertainActionError("Execution disconnected after submission; reconcile original action"))
                self.condition.notify_all()
        self._shutdown(socket)

    def close(self, timeout=1.0):
        self.stop_event.set()
        with self.condition:
            socket, self.socket = self.socket, None
            if self.pending is not None:
                self._finish_locked(self.pending, error=UncertainActionError("Execution closed with action in flight; reconcile original action"))
            thread = self.thread
            self.condition.notify_all()
        self._shutdown(socket)
        if thread is not None and thread is not threading.current_thread():
            thread.join(max(0, timeout))
        return thread is None or not thread.is_alive()

    def post_action(self, payload):
        if not self.action_lock.acquire(blocking=False):
            raise SubmissionSkipped("Execution busy; no additional action sent")
        pending = socket = None
        started = time.perf_counter_ns()
        try:
            # Serialization is local and must finish before marking an action sent.
            with self.condition:
                if self.socket is None or self.stop_event.is_set():
                    raise SubmissionSkipped("Execution WebSocket not ready; no action sent")
                if self.pending is not None:
                    raise SubmissionSkipped("An execution action is already pending; no additional action sent")
                socket, generation = self.socket, self.generation
                self.next_id += 1
                request_id = self.next_id
            try:
                packet = json.dumps({"method":"post", "id":request_id,
                                     "request":{"type":"action", "payload":payload}},
                                    separators=(",", ":"), allow_nan=False)
            except (TypeError, ValueError) as exc:
                raise SubmissionSkipped("Execution payload could not be serialized; no action sent") from exc
            with self.send_lock:
                with self.condition:
                    if self.socket is not socket or self.generation != generation or self.stop_event.is_set():
                        raise SubmissionSkipped("Execution connection changed before send")
                if self.final_gate:
                    gate_started = time.perf_counter_ns()
                    try:
                        self.final_gate(payload)
                    finally:
                        if self.metrics:
                            self.metrics.observe("ws_final_gate", (time.perf_counter_ns()-gate_started)/1e6)
                with self.condition:
                    if self.socket is not socket or self.generation != generation or self.stop_event.is_set():
                        raise SubmissionSkipped("Execution stopped before send")
                    if (time.perf_counter_ns()-started)/1e9 >= self.request_timeout:
                        raise SubmissionSkipped("Local execution deadline elapsed before send")
                    pending = self.pending = _Pending(request_id)
                # From this point a partial send is possible. Never classify a
                # send exception as a safe local skip, even if the socket says 0.
                try:
                    socket.send(packet)
                except Exception as exc:
                    self._count("ws_send_errors")
                    self._drop(socket, type(exc).__name__)
                    raise UncertainActionError("Execution send failed; original action outcome is unknown") from exc
            self._count("ws_action_requests")
            remaining = max(0, self.request_timeout-(time.perf_counter_ns()-started)/1e9)
            if not pending.done.wait(remaining):
                with self.condition:
                    if not pending.done.is_set():
                        self._finish_locked(pending, error=UncertainActionError("Execution ACK timeout; reconcile original action"))
                self._count("ws_ack_timeouts")
                self._drop(socket, "Execution ACK timeout")
            if pending.error is not None:
                raise pending.error
            if self.metrics:
                self.metrics.observe("ws_action_roundtrip", (time.perf_counter_ns()-started)/1e6)
            return pending.result
        except BaseException:
            if pending is not None and not pending.done.is_set():
                self._drop(socket, "Execution caller interrupted")
            raise
        finally:
            self.action_lock.release()

    def _message(self, socket, raw):
        if raw == "Websocket connection established.":
            return False
        if not isinstance(raw, (str, bytes)) or len(raw) > 2_000_000:
            raise ValueError("Invalid execution frame")
        obj = json.loads(raw)
        if not isinstance(obj, dict):
            raise ValueError("Invalid execution frame")
        if obj.get("channel") == "pong":
            return True
        if obj.get("channel") != "post":
            return False
        data = obj.get("data")
        if not isinstance(data, dict) or isinstance(data.get("id"), bool) or not isinstance(data.get("id"), int):
            raise ValueError("Invalid execution ACK identity")
        with self.condition:
            pending = self.pending
            if self.socket is not socket or pending is None or pending.request_id != data["id"]:
                self._count("ws_unmatched_acks")
                return False
            response = data.get("response")
            if not isinstance(response, dict):
                raise ValueError("Invalid execution ACK response")
            kind, payload = response.get("type"), response.get("payload")
            if kind == "error" and isinstance(payload, str):
                # Preserve the SDK-style envelope; GuardedExchange keeps pending
                # CLOIDs for errors whose non-execution cannot be established.
                result = {"status":"err", "response":payload}
            elif kind == "action" and isinstance(payload, dict) and payload.get("status") in ("ok", "err"):
                if payload["status"] == "ok" and not isinstance(payload.get("response"), dict):
                    raise ValueError("Invalid execution action payload")
                result = payload
            else:
                raise ValueError("Invalid execution ACK type or payload")
            self._finish_locked(pending, result=result)
        self._count("ws_action_acks")
        return False

    def _run(self):
        delay = self.reconnect_delay
        while not self.stop_event.is_set():
            socket = None
            connected_at = None
            try:
                if self.socket_factory is None:
                    import websocket
                    factory = websocket.create_connection
                else:
                    factory = self.socket_factory
                self._count("ws_connect_attempts")
                socket = factory(self.url, timeout=self.connect_timeout, enable_multithread=True)
                socket.settimeout(self.poll_timeout)
                connected_at = time.monotonic()
                with self.condition:
                    if self.stop_event.is_set():
                        return
                    self.socket = socket
                    self.generation += 1
                    self.last_error = ""
                    self.condition.notify_all()
                last_ping, pong_deadline = time.monotonic(), None
                while not self.stop_event.is_set():
                    try:
                        raw = socket.recv()
                        if raw in ("", b""):
                            raise ConnectionError("Execution WebSocket closed")
                        if self._message(socket, raw):
                            pong_deadline = None
                    except Exception as exc:
                        import websocket
                        if not isinstance(exc, (websocket.WebSocketTimeoutException, TimeoutError)):
                            raise
                    now = time.monotonic()
                    if pong_deadline is not None and now >= pong_deadline:
                        raise ConnectionError("Execution heartbeat timed out")
                    if now-last_ping >= self.heartbeat_interval:
                        with self.send_lock:
                            with self.condition:
                                current = self.socket is socket and not self.stop_event.is_set()
                            if current:
                                socket.send('{"method":"ping"}')
                        last_ping = now
                        pong_deadline = now+min(10.0, self.heartbeat_interval)
            except Exception as exc:
                self._count("ws_disconnects")
                self._drop(socket, type(exc).__name__)
            finally:
                self._shutdown(socket)
            if connected_at is not None and time.monotonic()-connected_at >= 5.0:
                delay = self.reconnect_delay
            if self.stop_event.wait(delay):
                return
            delay = min(self.max_reconnect_delay, delay*2)
