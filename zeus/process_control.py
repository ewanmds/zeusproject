"""Portable process locks and shared spot-fund reservations. No network on import."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import threading
import time

from zeus.safety import atomic_json, acknowledged_status, finite, GuardedExchange


class ProcessLock:
    def __init__(self, path, timeout=10):
        self.path, self.timeout, self.file = Path(path), timeout, None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+b")
        if self.file.tell() == 0:
            self.file.write(b"0")
            self.file.flush()
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    self.file.seek(0)
                    msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError:
                if time.monotonic() >= deadline:
                    self.file.close()
                    self.file = None
                    raise TimeoutError("Process lock busy; waiting for other session")
                time.sleep(0.05)

    def __exit__(self, *_):
        if self.file:
            if os.name == "nt":
                import msvcrt
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
            self.file.close()
            self.file = None


def account_key(account):
    return hashlib.sha256(account.strip().lower().encode()).hexdigest()[:24]


def pid_alive(pid):
    """True when the OS process still exists. Cheap; safe on Windows."""
    if not pid:
        return False
    if os.name == "nt":
        import ctypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.OpenProcess(0x1000, False, int(pid))
        if not handle:
            return ctypes.get_last_error() == 5
        code = ctypes.c_ulong()
        kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        ok = kernel.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel.CloseHandle(handle)
        return bool(ok and code.value == 259)
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


class OutcomeFunds:
    def __init__(self, root, account, session_id):
        self.root = Path(root) / account_key(account)
        self.session_id = session_id
        self.thread_lock = threading.RLock()
        self.local = threading.local()

    @contextmanager
    def transaction(self):
        with self.thread_lock:
            if getattr(self.local, "active", False):
                yield
                return
            with ProcessLock(self.root / "execution.lock"):
                self.local.active = True
                try:
                    yield
                finally:
                    self.local.active = False

    def read(self):
        path = self.root / "funds.json"
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"reserved": {}, "last_submit_ms": 0}
        if not isinstance(data.get("reserved"), dict):
            raise ValueError("Invalid shared spot-fund journal")
        for reservation in data["reserved"].values():
            if finite(reservation["usd"]) < 0:
                raise ValueError("Invalid spot-fund reservation")
        return data

    def reserved_usdc(self):
        with self.transaction():
            return sum(finite(row["usd"]) for row in self.read()["reserved"].values())

    def resolve(self, cloids):
        with self.transaction():
            data = self.read()
            for cloid in cloids:
                data["reserved"].pop(str(cloid), None)
            atomic_json(self.root / "funds.json", data)

    def wrap(self, exchange):
        coordinator = self

        class CoordinatedExchange:
            def __getattr__(self, name):
                method = getattr(exchange, name)
                if name not in GuardedExchange.ACTIONS:
                    return method

                def execute(*args, **kwargs):
                    with coordinator.transaction():
                        data = coordinator.read()
                        # The SDK signs using the wall-clock millisecond. Ensure
                        # two processes sharing an agent never reuse that nonce.
                        deadline = time.monotonic() + 10
                        while int(time.time() * 1000) <= int(data["last_submit_ms"]):
                            if time.monotonic() >= deadline:
                                raise TimeoutError("Clock has not advanced beyond previous signing nonce")
                            time.sleep(0.002)
                        requests = [kwargs] if name == "order" else args[0] if name == "bulk_orders" else []
                        reserved = []
                        for request in requests:
                            if request.get("is_buy"):
                                cloid = str(request["cloid"])
                                data["reserved"][cloid] = {"session_id": coordinator.session_id,
                                    "usd": finite(request["sz"]) * finite(request["limit_px"])}
                                reserved.append(cloid)
                        atomic_json(coordinator.root / "funds.json", data)
                        try:
                            result = method(*args, **kwargs)
                            statuses = result.get("response", {}).get("data", {}).get("statuses", []) if isinstance(result, dict) else []
                            if (isinstance(result, dict) and result.get("status") == "ok" and len(statuses) == len(requests)
                                    and all(acknowledged_status(s, True) for s in statuses)):
                                for cloid in reserved:
                                    data["reserved"].pop(cloid, None)
                            return result
                        finally:
                            data["last_submit_ms"] = max(int(data["last_submit_ms"]), int(time.time() * 1000))
                            atomic_json(coordinator.root / "funds.json", data)
                return execute
        return CoordinatedExchange()
