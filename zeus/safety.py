"""Execution interlock and independently verified, session-scoped cleanup.

Importing this module never starts a session or contacts an exchange.
"""
from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import re
import threading
import time
import uuid


class ExecutionStopped(RuntimeError):
    pass


class SubmissionSkipped(RuntimeError):
    """A local pre-send check proves no SDK request was sent."""
    pass


class ExecutionPaused(RuntimeError):
    pass


_SUPERVISOR_CACHE = {"pid": None, "ts": 0.0, "alive": True}
_SUPERVISOR_LOCK = threading.Lock()


def supervisor_alive():
    """Standalone runs have no supervisor; supervised children stop on its death.

    On Windows this opens a process handle on every call. The final-send gate
    calls it on the hot path, so cache the answer for 50 ms (same bound a
    watchdog thread would use).
    """
    value = os.getenv("ZEUS_PARENT_PID")
    if not value:
        return True
    now = time.monotonic()
    with _SUPERVISOR_LOCK:
        if (_SUPERVISOR_CACHE["pid"] == value
                and now - _SUPERVISOR_CACHE["ts"] < 0.05):
            return _SUPERVISOR_CACHE["alive"]
    try:
        pid = int(value)
        if pid <= 0:
            alive = False
        elif os.name == "nt":
            import ctypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.restype = ctypes.c_void_p
            kernel.CloseHandle.argtypes = [ctypes.c_void_p]
            handle = kernel.OpenProcess(0x1000, False, pid)
            if not handle:
                alive = ctypes.get_last_error() == 5
            else:
                code = ctypes.c_ulong()
                kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
                ok = kernel.GetExitCodeProcess(handle, ctypes.byref(code))
                kernel.CloseHandle(handle)
                alive = bool(ok and code.value == 259)
        else:
            os.kill(pid, 0)
            alive = True
    except PermissionError:
        alive = True
    except (ValueError, ProcessLookupError):
        alive = False
    with _SUPERVISOR_LOCK:
        _SUPERVISOR_CACHE.update(pid=value, ts=now, alive=alive)
    return alive


def safe_error(error):
    text = str(error)
    text = re.sub(r"(?:0x)?[0-9a-fA-F]{64}\b", "[redacted]", text)
    text = re.sub(r"(?i)(token|api[_-]?key|private[_-]?key|password|secret)([=: ]+)[^\s&,]+", r"\1\2[redacted]", text)
    return text[:240]


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(data, allow_nan=False), encoding="utf-8")
        for attempt in range(4):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                # Windows/antivirus can briefly hold the destination while a
                # supervisor reads it. Never truncate or discard the old file.
                if attempt == 3:
                    raise
                time.sleep(0.01 * 2 ** attempt)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path):
    for attempt in range(4):
        try:
            return json.loads(Path(path).read_bytes())
        except PermissionError:
            if attempt == 3:
                raise
            time.sleep(0.01 * 2 ** attempt)


def finite(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("Invalid non-finite account value")
    return result


def duplicate_nonce_rejection(detail):
    return isinstance(detail, str) and re.fullmatch(r"Invalid nonce: duplicate nonce [0-9]+", detail) is not None


def legacy_nonce_rejection(record):
    """Exact persisted first failure only; never infer rejection from a timeout."""
    return (not record.get("recovery_error") and isinstance(record.get("error"), str)
            and re.fullmatch(r"(?:order|bulk_orders|bulk_modify_orders_new): exchange rejected: Invalid nonce: duplicate nonce [0-9]+", record["error"]) is not None)


def confirmed_client_order(info, account, cloid, symbols):
    result = info.post("/info", {"type": "orderStatus", "user": account, "oid": cloid})
    if not isinstance(result, dict) or result.get("status") != "order":
        raise RuntimeError("Placement still unconfirmed; awaiting client order ID")
    entry = result["order"]
    order, status = entry["order"], str(entry["status"])
    if order.get("cloid") != cloid or order.get("coin") not in symbols:
        raise ValueError("Mismatched order status response")
    oid = int(order["oid"])
    if oid <= 0:
        raise ValueError("Invalid order ID")
    if status not in {"open", "filled", "canceled"} and not status.endswith(("Canceled", "Rejected")):
        raise RuntimeError("Order status not yet settled")
    return oid, status


def acknowledged_status(status, placing):
    if status == "success":
        return not placing
    if not isinstance(status, dict):
        return False
    if sum(key in status for key in ("error","resting","filled"))!=1:
        return False
    if isinstance(status.get("error"), str) and status["error"]:
        return True
    if isinstance(status.get("resting"), dict):
        oid=status["resting"].get("oid")
        return not isinstance(oid,bool) and isinstance(oid,(int,str)) and str(oid).isdigit() and int(oid)>0
    if isinstance(status.get("filled"), dict):
        filled=status["filled"];oid=filled.get("oid")
        try:
            return (not isinstance(oid,bool) and isinstance(oid,(int,str)) and str(oid).isdigit() and int(oid)>0
                    and finite(filled["totalSz"])>0 and finite(filled["avgPx"])>0)
        except (KeyError,TypeError,ValueError,OverflowError):
            return False
    return False


def post_only_rejection(error):
    if not isinstance(error, str):
        return False
    prefix = "Error placing new order during modify: "
    if error.startswith(prefix):
        error = error[len(prefix):]
    return re.fullmatch(
        r"Post only order would have immediately matched(?:, bbo was [0-9.eE+-]+@[0-9.eE+-]+)?\.?(?: asset=\d+)?",
        error) is not None


def invalid_modify_rejection(error):
    return isinstance(error, str) and re.fullmatch(
        r"Attempted to modify to invalid new order\.?(?: asset=\d+)?", error) is not None


def missing_modify_rejection(error):
    return isinstance(error, str) and re.fullmatch(
        r"Cannot modify canceled or filled order\.?(?: asset=\d+)?", error) is not None


class SafetyLatch:
    def __init__(self, on_stop=None, path=None, is_running=None, passive_only=False):
        self.lock = threading.RLock()
        self.stopped = threading.Event()
        self.on_stop = on_stop or (lambda: None)
        self.is_running = is_running or (lambda: True)
        self.local = threading.local()
        self.passive_only = bool(passive_only)
        # An ordinary failure, signal, or cleanup never grants market execution.
        # A new runtime must observe a fresh explicit manual-stop request.
        self.manual_stop_requested = False
        self.phase = "connecting"
        self.error = ""
        self.cleanup_error = ""
        self.cleanup_confirmed = False
        self.account_known = False
        self.cleanup_done = threading.Event()
        self.cleanup_lock = threading.Lock()
        self.uncertain_close = False
        self.owned = set()
        self.cloids = set()
        self.path = Path(path) if path else None
        self.metadata = {}
        if self.path and self.path.exists():
            self.metadata = json.loads(self.path.read_text(encoding="utf-8"))
        self.session_id = self.metadata.get("session_id") or uuid.uuid4().hex
        self.owned.update(self.metadata.get("owned_oids", []))
        self.cloids.update(self.metadata.get("cloids", []))
        self.exposure_started = self.metadata.get("exposure_started", False)
        self.pending_cloids = set(self.metadata.get("pending_cloids", []))
        self.recovery_error = ""
        self.recovery_revision = 0

    def snapshot(self):
        return dict(phase=self.phase, error=self.error, cleanup_error=self.cleanup_error,
                    cleanup_confirmed=self.cleanup_confirmed, can_start=self.cleanup_confirmed,
                    position_known=self.account_known or self.cleanup_confirmed, session_id=self.session_id,
                    quoting_enabled=not self.stopped.is_set() and self.phase == "running",
                    recovery_error=self.recovery_error,
                    passive_only=self.passive_only, manual_stop_requested=self.manual_stop_requested)

    def publish(self):
        if self.path:
            started=time.perf_counter_ns()
            try:
                atomic_json(self.path, {**self.metadata, **self.snapshot(), "pid": os.getpid(),
                                        "owned_oids": sorted(self.owned), "cloids": sorted(self.cloids),
                                        "exposure_started": self.exposure_started,
                                        "pending_cloids": sorted(self.pending_cloids),
                                        "ts": time.time()})
            except (OSError, ValueError) as exc:
                self.stopped.set()
                self.error = self.error or "Session journal unavailable: " + safe_error(exc)
                self.cleanup_confirmed = False
                self.on_stop()
                return False
            finally:
                if getattr(self,"metrics",None):self.metrics.observe("durable_journal",(time.perf_counter_ns()-started)/1e6)
        return True

    def stop(self, error=None):
        # Set before acquiring the execution lock: queued actions must see it.
        self.stopped.set()
        if self.cleanup_done.is_set():
            self.on_stop()
            return
        if error is not None and not self.error:
            self.error = safe_error(error)
        self.phase = "stopping"
        self.on_stop()
        self.publish()

    def fail(self, error):
        self.account_known = False
        self.stop(error)

    def request_manual_stop(self, error=None):
        """Record the explicit user origin before closing the execution gate."""
        self.manual_stop_requested = True
        self.stop(error)

    @contextmanager
    def manual_close_scope(self):
        """Narrow capability for a manual Stop's explicit reduce-only close.

        cleanup_scope only permits requests after Stop; it does not itself
        permit crossing orders. The capability is thread-local and reentrant.
        """
        if not self.manual_stop_requested:
            raise ExecutionStopped("An explicit manual Stop is required for market closing")
        previous = getattr(self.local, "manual_close", False)
        self.local.manual_close = True
        try:
            yield
        finally:
            self.local.manual_close = previous

    def assert_allowed_action(self, name, args=(), kwargs=None):
        """Final-send policy check, independent of the strategy's quote builder.

        Reject locally before journaling/CLOIDs and permit a caller to repeat
        the same check immediately before signing or transport submission.
        """
        if not self.passive_only:
            return
        kwargs = kwargs or {}
        if name in {"market_open", "market_close"}:
            raise SubmissionSkipped("Passive-only session blocks SDK market convenience methods; use an explicit authorized reduce-only close")
        if name == "order":
            requests = [(kwargs.get("order_type", args[4] if len(args)>4 else None),
                         kwargs.get("reduce_only", args[5] if len(args)>5 else False))]
        elif name == "modify_order":
            requests = [(kwargs.get("order_type", args[5] if len(args)>5 else None),
                         kwargs.get("reduce_only", args[6] if len(args)>6 else False))]
        elif name in {"bulk_orders", "bulk_modify_orders_new"}:
            rows = args[0] if args else kwargs.get("order_requests" if name=="bulk_orders" else "modify_requests")
            if not isinstance(rows, (list,tuple)) or not rows:
                raise SubmissionSkipped("Passive-only session requires a non-empty explicit order batch")
            requests = []
            for row in rows:
                request = row.get("order") if name=="bulk_modify_orders_new" and isinstance(row,dict) else row
                if not isinstance(request,dict):
                    raise SubmissionSkipped("Passive-only session received an invalid order request")
                requests.append((request.get("order_type"),request.get("reduce_only",False)))
        else:
            return  # cancellation/leverage do not create crossing exposure
        manual = self.manual_stop_requested and getattr(self.local,"manual_close",False)
        closing_only = self.stopped.is_set() or getattr(self.local,"cleanup",False)
        for order_type,reduce_only in requests:
            if closing_only and reduce_only is not True:
                raise SubmissionSkipped("A stopped passive session permits only explicit reduce-only closing orders")
            if not isinstance(order_type,dict) or set(order_type)!={"limit"} or not isinstance(order_type["limit"],dict):
                raise SubmissionSkipped("Passive-only session blocks trigger or implicit market orders")
            tif = order_type["limit"].get("tif")
            if tif == "Alo":
                continue
            if manual and reduce_only is True and tif in {"Ioc","Gtc"}:
                continue
            raise SubmissionSkipped("Passive-only session requires post-only Alo; crossing closes require an explicit manual Stop")

    def pause(self, error, pending_cloids=()):
        """Keep the session alive while an opt-in engine reconciles read-only."""
        with self.lock:
            if self.stopped.is_set():
                return
            self.account_known = False
            self.phase = "recovering"
            self.recovery_error = self.recovery_error or safe_error(error)
            self.metadata["last_recovery_error"] = safe_error(error)
            self.pending_cloids.update(pending_cloids)
            self.recovery_revision += 1
            self.publish()

    def resume(self, revision):
        with self.lock:
            if (self.stopped.is_set() or not self.is_running() or self.phase != "recovering"
                    or revision != self.recovery_revision or self.pending_cloids):
                return False
            self.phase = "running"
            self.recovery_error = ""
            return self.publish()

    @contextmanager
    def cleanup_scope(self):
        previous = getattr(self.local, "cleanup", False)
        self.local.cleanup = True
        try:
            yield
        finally:
            self.local.cleanup = previous

    def owns(self, order):
        return int(order.get("oid", 0)) in self.owned or str(order.get("cloid", "")) in self.cloids

    def wrap(self, exchange, reconcile_cancel=None, on_error=None, reconcile_reduce_only=None, reconcile_post_only=None, reconcile_modify=None, reconcile_invalid_modify=None):
        return GuardedExchange(exchange, self, reconcile_cancel, on_error, reconcile_reduce_only, reconcile_post_only, reconcile_modify, reconcile_invalid_modify)

    def cleanup(self, snapshot, cancel, close, timeout=30, sleep=time.sleep, allow_close=True):
        """snapshot returns fresh [(symbol, position, orders)] or raises.

        The caller supplies only this session's managed symbols. Unknown
        placements are identifiable by cloid. Unrelated orders are never pulled.
        """
        if not self.cleanup_lock.acquire(blocking=False):
            self.cleanup_done.wait(timeout)
            return self.cleanup_confirmed
        try:
            if self.cleanup_done.is_set():
                return self.cleanup_confirmed
            self.stop()
            deadline = time.monotonic() + timeout
            with self.lock, self.cleanup_scope():  # drain all in-flight requests first
                for attempt, delay in enumerate((0, 2, 4)):
                    if delay:
                        sleep(min(delay, max(0, deadline - time.monotonic())))
                    if time.monotonic() >= deadline or self.uncertain_close:
                        break
                    try:
                        rows = snapshot()
                        owned = [(symbol, int(order["oid"])) for symbol, _, orders in rows
                                 for order in orders if self.owns(order)]
                        if owned:
                            cancel(owned)
                            rows = snapshot()
                        if any(self.owns(order) for _, _, orders in rows for order in orders):
                            raise RuntimeError("Session orders remain open")
                        # Other orders on the same symbol make closing ambiguous.
                        if any(orders for _, _, orders in rows):
                            raise RuntimeError("Unrelated orders on managed market; manual reconciliation required")
                        # Absence in an open-order snapshot does not settle an
                        # action still in flight on the exchange. Do not send a
                        # second liquidation while its outcome remains unknown.
                        if self.pending_cloids:
                            raise RuntimeError("Placement client IDs remain unconfirmed; no new closing order sent")
                        can_close = allow_close and (not self.passive_only or self.manual_stop_requested
                                                     and getattr(self.local,"manual_close",False))
                        if not can_close and any(abs(finite(position))>1e-9 for _,position,_ in rows):
                            raise RuntimeError("Residual position; automatic cleanup cannot market-close it, explicit manual Stop required")
                        for symbol, position, _ in rows:
                            if abs(finite(position)) > 1e-9:
                                if not self.exposure_started:
                                    raise RuntimeError("Pre-existing position; manual reconciliation required")
                                close(symbol, position)
                        rows = snapshot()
                        if any(orders or abs(finite(position)) > 1e-9 for _, position, orders in rows):
                            raise RuntimeError("Residual orders or position; stop not confirmed")
                        if self.pending_cloids:
                            raise RuntimeError("Placement client IDs remain unconfirmed")
                        self.cleanup_confirmed = True
                        self.cleanup_error = ""
                        break
                    except Exception as exc:
                        self.cleanup_error = safe_error(exc)
            self.phase = "failed" if self.error or not self.cleanup_confirmed else "stopped"
            if not self.cleanup_confirmed and not self.cleanup_error:
                self.cleanup_error = "Cleanup deadline exceeded or closing response uncertain"
            self.publish()
            self.cleanup_done.set()
            return self.cleanup_confirmed
        finally:
            self.cleanup_lock.release()


class GuardedExchange:
    ACTIONS = {"order", "bulk_orders", "bulk_modify_orders_new", "modify_order", "bulk_cancel", "cancel", "update_leverage"}

    def __init__(self, exchange, safety, reconcile_cancel=None, on_error=None, reconcile_reduce_only=None, reconcile_post_only=None, reconcile_modify=None, reconcile_invalid_modify=None):
        self._exchange = exchange
        self._safety = safety
        self._reconcile_cancel = reconcile_cancel
        self._on_error = on_error
        self._reconcile_reduce_only = reconcile_reduce_only
        self._reconcile_post_only = reconcile_post_only
        self._reconcile_modify = reconcile_modify
        self._reconcile_invalid_modify = reconcile_invalid_modify

    def __getattr__(self, name):
        method = getattr(self._exchange, name)
        if name in {"market_open", "market_close"} and self._safety.passive_only:
            def blocked(*args, **kwargs):
                self._safety.assert_allowed_action(name,args,kwargs)
            return blocked
        if name not in self.ACTIONS:
            return method

        def execute(*args, **kwargs):
            gate = self._safety
            with gate.lock:
                cleanup = getattr(gate.local, "cleanup", False)
                if (gate.stopped.is_set() or not gate.is_running()) and not cleanup:
                    gate.stop()
                    raise ExecutionStopped("Session halted; manual restart required")
                if gate.phase == "recovering" and not cleanup:
                    raise ExecutionPaused("Session active; waiting for exchange reconciliation")
                gate.assert_allowed_action(name,args,kwargs)
                placing = name in {"order", "bulk_orders", "bulk_modify_orders_new"}
                operation_cloids = set()
                acknowledged = False
                try:
                    if placing:
                        gate.exposure_started = True
                        from hyperliquid.utils.types import Cloid
                        if name == "order":
                            kwargs["cloid"] = kwargs.get("cloid") or Cloid.from_str("0x" + uuid.uuid4().hex)
                            gate.cloids.add(str(kwargs["cloid"]))
                            operation_cloids.add(str(kwargs["cloid"]))
                        else:
                            for item in args[0]:
                                request = item["order"] if name == "bulk_modify_orders_new" else item
                                request["cloid"] = request.get("cloid") or Cloid.from_str("0x" + uuid.uuid4().hex)
                                gate.cloids.add(str(request["cloid"]))
                                operation_cloids.add(str(request["cloid"]))
                    # Persist uncertainty before submission, including a child
                    # crash between sending and receiving the acknowledgement.
                    gate.pending_cloids.update(operation_cloids)
                    if not gate.publish():  # persist intent before the request can reach the exchange
                        raise RuntimeError("Session journal unavailable")
                    if (gate.stopped.is_set() or not gate.is_running()) and not cleanup:
                        gate.stop()
                        raise ExecutionStopped("Session halted before submission")
                    result = method(*args, **kwargs)
                    if not isinstance(result, dict) or result.get("status") != "ok":
                        if isinstance(result, dict) and result.get("status") == "err":
                            detail = safe_error(result.get("response") or result.get("error") or "No exchange reason provided")
                            if duplicate_nonce_rejection(result.get("response")):
                                # An explicit whole-action rejection is not an unknown send.
                                acknowledged = True
                                gate.pending_cloids.difference_update(operation_cloids)
                                gate.metadata["rejected_cloids"] = sorted(set(gate.metadata.get("rejected_cloids", [])) | operation_cloids)
                                gate.publish()
                            raise RuntimeError(f"{name}: exchange rejected: {detail}")
                        raise RuntimeError(f"{name}: invalid or rejected response ({type(result).__name__})")
                    if name == "update_leverage":
                        response = result.get("response")
                        if not isinstance(response, dict) or response.get("type") != "default":
                            raise RuntimeError("update_leverage: invalid acknowledgement (expected response.type=default)")
                    if name != "update_leverage":
                        statuses = result.get("response", {}).get("data", {}).get("statuses", [])
                        expected = len(args[0]) if name.startswith("bulk_") else 1
                        acknowledged = len(statuses) == expected and all(
                            acknowledged_status(status, placing) for status in statuses)
                        # This specific cancel race is not an execution failure if
                        # independent reads prove absence and refresh inventory.
                        # Keep the execution lock throughout reconciliation.
                        if name == "bulk_cancel" and self._reconcile_cancel and len(statuses) == expected:
                            missing = [i for i, status in enumerate(statuses)
                                       if isinstance(status, dict) and isinstance(status.get("error"), str)
                                       and re.fullmatch(r"Order was never placed, already canceled, or filled\.(?: asset=\d+)?",
                                                        status["error"])]
                            if missing and all(status == "success" or i in missing
                                               for i, status in enumerate(statuses)):
                                self._reconcile_cancel([args[0][i] for i in missing])
                                statuses = ["success" if i in missing else status
                                            for i, status in enumerate(statuses)]
                                result = {**result, "response": {**result["response"], "data": {
                                    **result["response"]["data"], "statuses": statuses}}}
                        for status in statuses:
                            if isinstance(status, dict) and acknowledged_status(status,placing):
                                detail = status.get("resting", status.get("filled", {}))
                                if isinstance(detail, dict) and detail.get("oid") is not None:
                                    gate.owned.add(int(detail["oid"]))
                        if not acknowledged:
                            raise RuntimeError(f"{name}: malformed or incomplete acknowledgement: {safe_error(statuses)}")
                        if acknowledged:
                            gate.pending_cloids.difference_update(operation_cloids)
                        gate.publish()
                        resolved_rejections = set()
                        if placing and acknowledged and not cleanup:
                            requests = [kwargs] if name == "order" else [item["order"] for item in args[0]] if name == "bulk_modify_orders_new" else args[0]
                            rejected = [i for i, status in enumerate(statuses)
                                        if self._reconcile_reduce_only and isinstance(status, dict) and requests[i].get("reduce_only") is True
                                        and isinstance(status.get("error"), str)
                                        and re.fullmatch(r"Reduce only order would increase position\.(?: asset=\d+)?", status["error"])]
                            post_only = [i for i, status in enumerate(statuses)
                                         if self._reconcile_post_only and isinstance(status, dict)
                                         and post_only_rejection(status.get("error"))
                                         and requests[i].get("order_type", {}).get("limit", {}).get("tif") == "Alo"]
                            missing_modifies = [i for i, status in enumerate(statuses)
                                                if name == "bulk_modify_orders_new" and self._reconcile_modify
                                                and isinstance(status, dict) and missing_modify_rejection(status.get("error"))]
                            invalid_modifies = [i for i, status in enumerate(statuses)
                                                if name == "bulk_modify_orders_new" and self._reconcile_invalid_modify
                                                and isinstance(status, dict) and invalid_modify_rejection(status.get("error"))
                                                and requests[i].get("order_type", {}).get("limit", {}).get("tif") == "Alo"]
                            recoverable = set(rejected + post_only + missing_modifies + invalid_modifies)
                            if recoverable and all(i in recoverable or isinstance(status, dict)
                                                   and ("resting" in status or "filled" in status)
                                                   for i, status in enumerate(statuses)):
                                if rejected:
                                    self._reconcile_reduce_only()
                                if post_only:
                                    self._reconcile_post_only([requests[i] for i in post_only], name)
                                if missing_modifies:
                                    self._reconcile_modify([args[0][i] for i in missing_modifies])
                                if invalid_modifies:
                                    self._reconcile_invalid_modify([args[0][i] for i in invalid_modifies])
                                resolved_rejections = recoverable
                        if len(statuses) != expected or any(i not in resolved_rejections and not (
                                status == "success" or isinstance(status, dict) and
                                ("resting" in status or "filled" in status)) for i, status in enumerate(statuses)):
                            raise RuntimeError(f"{name}: rejected or incomplete acknowledgement: {safe_error(statuses)}")
                    return result
                except SubmissionSkipped:
                    # A local final-send gate declined BEFORE SDK invocation.
                    gate.pending_cloids.difference_update(operation_cloids)
                    gate.publish()
                    raise
                except ExecutionStopped:
                    raise
                except Exception as exc:
                    if cleanup:
                        gate.cleanup_error = safe_error(exc)
                        if placing and not acknowledged:
                            gate.uncertain_close = True
                    else:
                        if self._on_error:
                            self._on_error(exc, () if acknowledged else operation_cloids)
                        else:
                            gate.fail(exc)
                    raise
        return execute


def account_snapshot(info, account, symbols, *, spot=False, balances=None):
    """Strict independent account/order read. Missing data is not a flat account."""
    positions = {}
    dex_orders = {}
    if spot:
        state = info.spot_user_state(account)
        if not isinstance(state, dict) or not isinstance(state.get("balances"), list):
            raise ValueError("Invalid spot account response")
        values = {str(row["coin"]): finite(row["total"]) for row in state["balances"]}
        positions = {symbol: values.get(balances[symbol], 0.0) for symbol in symbols}
    rows = []
    for symbol in symbols:
        dex = "" if spot or ":" not in symbol else symbol.split(":", 1)[0]
        if dex not in dex_orders:
            orders = info.frontend_open_orders(account, dex=dex)
            if not isinstance(orders, list) or any(not isinstance(o, dict) or "coin" not in o or "oid" not in o for o in orders):
                raise ValueError("Invalid open order response")
            dex_orders[dex] = orders
            if not spot:
                state = info.user_state(account, dex=dex)
                if not isinstance(state, dict) or not isinstance(state.get("assetPositions"), list):
                    raise ValueError("Invalid position response")
                for row in state["assetPositions"]:
                    pos = row["position"]
                    positions[(dex, str(pos["coin"]))] = finite(pos["szi"])
        matching = [o for o in dex_orders[dex] if str(o["coin"]) in {symbol, symbol.split(":")[-1]}]
        position = positions.get(symbol, 0.0) if spot else positions.get((dex, symbol), positions.get((dex, symbol.split(":")[-1]), 0.0))
        rows.append((symbol, position, matching))
    return rows
