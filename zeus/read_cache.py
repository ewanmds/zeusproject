"""Shared public metadata and a cross-process REST read budget.

Only public structural metadata is cached. Balances/orders are never cached.
SQLite transactions are short and contain no network I/O or sleeping.
"""
import hashlib
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import threading
import time

from zeus import PROJECT_ROOT
from zeus.network import HyperliquidError, StartupReadCancelled
from zeus.process_control import ProcessLock


class ReadBudget:
    def __init__(self, root, *, limit=960, ordinary=720, clock=time.time):
        self.path = Path(root) / "read-budget.sqlite3"
        self.limit, self.ordinary, self.clock = limit, ordinary, clock
        self._cooldown_until = 0.0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("CREATE TABLE IF NOT EXISTS reads (at REAL, weight INTEGER);"
                             "CREATE TABLE IF NOT EXISTS cooldown (id INTEGER PRIMARY KEY, until REAL);")

    @contextmanager
    def connect(self):
        # Serialize schema changes and reservations across workers, including
        # connection close/checkpoint. Never include HTTP or a retry sleep here.
        try:
            with ProcessLock(self.path.with_suffix('.lock'), timeout=.25):
                db = sqlite3.connect(self.path, timeout=.25)
                try:
                    with db:
                        yield db
                finally:
                    db.close()
        except (sqlite3.OperationalError, TimeoutError) as exc:
            code = getattr(exc, 'sqlite_errorcode', 0) & 0xff
            busy = isinstance(exc, TimeoutError) or code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
            if not busy:
                raise
            # No reservation means no HTTP. Only idempotent read callers may
            # retry this typed failure; signed submissions are never replayed.
            raise HyperliquidError(
                "Compteur de requêtes occupé. Réessayez dans un instant.",
                kind='exchange_unavailable', operation='rest_read_budget',
                status=503, retry_after=.5) from exc

    def acquire(self, operation, *, critical=False, cancelled=None):
        if cancelled and cancelled():
            raise StartupReadCancelled("Read cancelled before HTTP")
        weight = 2 if operation in {"l2Book", "allMids", "clearinghouseState",
                                    "orderStatus", "spotClearinghouseState", "exchangeStatus"} else 60 if operation == "userRole" else 20
        now = self.clock()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if cancelled and cancelled():
                raise StartupReadCancelled("Read cancelled while waiting for budget")
            if self._cooldown_until > now:
                self._persist_cooldown(db, self._cooldown_until)
            db.execute("DELETE FROM reads WHERE at <= ?", (now-60,))
            blocked = db.execute("SELECT until FROM cooldown WHERE id=1").fetchone()
            used = db.execute("SELECT COALESCE(SUM(weight),0), MIN(at) FROM reads").fetchone()
            wait = max(0, blocked[0]-now) if blocked else 0
            if used[0]+weight > (self.limit if critical else self.ordinary):
                wait = max(wait, (used[1] or now)+60-now)
            if not wait:
                db.execute("INSERT INTO reads VALUES (?,?)", (now, weight))
                return
        # Never sleep under an execution/signing lock. The caller schedules it.
        raise HyperliquidError("Budget Hyperliquid: lecture reportée", kind="rate_limited",
                               operation=operation, status=429, retry_after=max(.1, wait))

    def cooldown(self, seconds):
        self._cooldown_until = max(self._cooldown_until, self.clock()+seconds)
        try:
            with self.connect() as db:
                self._persist_cooldown(db, self._cooldown_until)
        except HyperliquidError:
            # Preserve the exchange's original 429 and its Retry-After. The
            # local deadline is republished before this worker can read again.
            pass

    @staticmethod
    def _persist_cooldown(db, until):
        db.execute("INSERT INTO cooldown VALUES (1,?) ON CONFLICT(id) DO UPDATE SET until=MAX(until,excluded.until)",
                   (until,))


_budgets = {}
_lock = threading.Lock()


def network_root():
    return Path(os.environ.get("ZEUS_NETWORK_ROOT") or PROJECT_ROOT / ".zeus-state" / "network")


def read_budget():
    root = network_root()
    with _lock:
        if str(root) not in _budgets:
            _budgets[str(root)] = ReadBudget(root)
        return _budgets[str(root)]


def public_metadata(base_url, kind, fetch, *, cancelled=None, ttl=3600):
    """One fetch across workers; validate before atomically replacing the cache."""
    from zeus.process_control import ProcessLock
    from zeus.safety import atomic_json
    if kind not in {"meta", "spotMeta"}:
        raise ValueError("Only structural public metadata can be cached")
    key = hashlib.sha256((base_url+":"+kind).encode()).hexdigest()[:24]
    root = network_root() / "metadata"
    path = root / (key+".json")
    def cached():
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
            if 0 <= time.time()-row["at"] < ttl:
                return row["data"]
        except (OSError, ValueError, KeyError, TypeError):
            pass
        return None
    value = cached()
    if value is not None:
        return value
    # Retry lock acquisition in small cancellable intervals, never a long wait.
    while True:
        if cancelled and cancelled():
            raise StartupReadCancelled("Metadata read cancelled")
        guard = ProcessLock(root / (key+".lock"), timeout=0)
        try:
            guard.__enter__()
            break
        except TimeoutError:
            time.sleep(.05)
    try:
        value = cached()
        if value is not None:
            return value
        value = fetch()
        if (not isinstance(value, dict) or not isinstance(value.get("universe"), list)
                or kind == "spotMeta" and not isinstance(value.get("tokens"), list)):
            raise ValueError("Invalid SDK metadata: "+kind)
        atomic_json(path, {"at": time.time(), "data": value})
        return value
    finally:
        guard.__exit__(None, None, None)
