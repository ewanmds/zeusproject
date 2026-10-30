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


def supervisor_alive():
    """Standalone runs have no supervisor; supervised children stop on its death."""
    value = os.getenv("ZEUS_PARENT_PID")
    if not value:
        return True
    try:
        pid = int(value)
        if pid <= 0:
            return False
        if os.name == "nt":
            import ctypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.restype = ctypes.c_void_p
            kernel.CloseHandle.argtypes = [ctypes.c_void_p]
            handle = kernel.OpenProcess(0x1000, False, pid)
            if not handle:
                return ctypes.get_last_error() == 5
            code = ctypes.c_ulong()
            kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
            ok = kernel.GetExitCodeProcess(handle, ctypes.byref(code))
            kernel.CloseHandle(handle)
            return bool(ok and code.value == 259)
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except (ValueError, ProcessLookupError):
        return False


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
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def finite(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("Invalid non-finite account value")
    return result


class SafetyLatch:
    def __init__(self, on_stop=None, path=None, is_running=None):
        self.lock = threading.RLock()
        self.stopped = threading.Event()
        self.on_stop = on_stop or (lambda: None)
        self.is_running = is_running or (lambda: True)
        self.local = threading.local()
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

    def snapshot(self):
        return dict(phase=self.phase, error=self.error, cleanup_error=self.cleanup_error,
                    cleanup_confirmed=self.cleanup_confirmed, can_start=self.cleanup_confirmed,
                    position_known=self.account_known or self.cleanup_confirmed, session_id=self.session_id,
                    quoting_enabled=not self.stopped.is_set() and self.phase == "running")

    def publish(self):
        if self.path:
            try:
                atomic_json(self.path, {**self.metadata, **self.snapshot(), "pid": os.getpid(),
                                        "owned_oids": sorted(self.owned), "cloids": sorted(self.cloids),
                                        "exposure_started": self.exposure_started,
                                        "ts": time.time()})
            except (OSError, ValueError) as exc:
                self.stopped.set()
                self.error = self.error or "Session journal unavailable: " + safe_error(exc)
                self.cleanup_confirmed = False
                self.on_stop()
                return False
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

    def wrap(self, exchange):
        return GuardedExchange(exchange, self)

    def cleanup(self, snapshot, cancel, close, timeout=30, sleep=time.sleep):
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
                        for symbol, position, _ in rows:
                            if abs(finite(position)) > 1e-9:
                                if not self.exposure_started:
                                    raise RuntimeError("Pre-existing position; manual reconciliation required")
                                close(symbol, position)
                        rows = snapshot()
                        if any(orders or abs(finite(position)) > 1e-9 for _, position, orders in rows):
                            raise RuntimeError("Residual orders or position; stop not confirmed")
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

    def __init__(self, exchange, safety):
        self._exchange = exchange
        self._safety = safety

    def __getattr__(self, name):
        method = getattr(self._exchange, name)
        if name not in self.ACTIONS:
            return method

        def execute(*args, **kwargs):
            gate = self._safety
            with gate.lock:
                cleanup = getattr(gate.local, "cleanup", False)
                if (gate.stopped.is_set() or not gate.is_running()) and not cleanup:
                    gate.stop()
                    raise ExecutionStopped("Session halted; manual restart required")
                placing = name in {"order", "bulk_orders", "bulk_modify_orders_new"}
                try:
                    if placing:
                        gate.exposure_started = True
                        from hyperliquid.utils.types import Cloid
                        if name == "order":
                            kwargs["cloid"] = kwargs.get("cloid") or Cloid.from_str("0x" + uuid.uuid4().hex)
                            gate.cloids.add(str(kwargs["cloid"]))
                        else:
                            for item in args[0]:
                                request = item["order"] if name == "bulk_modify_orders_new" else item
                                request["cloid"] = request.get("cloid") or Cloid.from_str("0x" + uuid.uuid4().hex)
                                gate.cloids.add(str(request["cloid"]))
                    if not gate.publish():  # persist intent before the request can reach the exchange
                        raise RuntimeError("Session journal unavailable")
                    if (gate.stopped.is_set() or not gate.is_running()) and not cleanup:
                        gate.stop()
                        raise ExecutionStopped("Session halted before submission")
                    result = method(*args, **kwargs)
                    if not isinstance(result, dict) or result.get("status") != "ok":
                        raise RuntimeError(f"{name}: invalid or rejected response")
                    if name != "update_leverage":
                        statuses = result.get("response", {}).get("data", {}).get("statuses", [])
                        expected = len(args[0]) if name.startswith("bulk_") else 1
                        for status in statuses:
                            if isinstance(status, dict) and "resting" in status:
                                gate.owned.add(int(status["resting"]["oid"]))
                        gate.publish()
                        if len(statuses) != expected or any(not (
                                status == "success" or isinstance(status, dict) and
                                ("resting" in status or "filled" in status)) for status in statuses):
                            raise RuntimeError(f"{name}: rejected or incomplete acknowledgement: {safe_error(statuses)}")
                    return result
                except ExecutionStopped:
                    raise
                except Exception as exc:
                    if cleanup:
                        gate.cleanup_error = safe_error(exc)
                        if placing:
                            gate.uncertain_close = True
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
