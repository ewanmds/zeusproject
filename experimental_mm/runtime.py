"""Bounded recording and read-only calibration monitoring, outside order I/O."""
import hashlib
import json
import math
from pathlib import Path
import queue
import threading
import time
import uuid
from collections import deque

from .core import (Features, Markouts, OFFSETS, POLICY, POLICY_HASH, SCHEMA, SYMBOL,
                   finite, quotes, verified_fees)
from .clock import ExchangeClock

DATA_DIR = Path(__file__).resolve().parents[1] / ".zeus_experimental"


def implementation_hash():
    root = Path(__file__).resolve().parents[1]
    paths = sorted((root / "experimental_mm").glob("*.py")) + [root / "OBI_VOLUME_BOT.py"]
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.name.encode())
        digest.update(path.read_bytes().replace(b"\r\n", b"\n"))
    return digest.hexdigest()


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, allow_nan=False, indent=2), encoding="utf-8")
    temporary.replace(path)


class Journal:
    """One writer, FIFO reception order, no disk access on WS/order threads."""
    def __init__(self, directory=DATA_DIR):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / f"session-{time.time_ns()}-{uuid.uuid4().hex[:8]}.jsonl"
        self.pending = queue.Queue(maxsize=20000)
        self.error = ""
        self.closed = False
        self.writer = threading.Thread(target=self._write, name="experimental-journal", daemon=True)
        self.writer.start()
        self.record("start", dict(schema=SCHEMA, symbol=SYMBOL, policy_hash=POLICY_HASH,
                                  implementation_hash=implementation_hash()))

    def record(self, kind, data, ts=None):
        if self.closed or self.error:
            return False
        try:
            self.pending.put_nowait(dict(kind=kind, ts=time.time() if ts is None else ts, data=data))
            return True
        except queue.Full:
            self.error = "journal saturated; openings blocked"
            return False

    def _write(self):
        try:
            with self.path.open("x", encoding="utf-8", buffering=1) as stream:
                while True:
                    event = self.pending.get()
                    if event is None:
                        break
                    stream.write(json.dumps(event, allow_nan=False, separators=(",", ":")) + "\n")
        except Exception as exc:
            self.error = f"journal failure: {type(exc).__name__}"

    def close(self):
        if self.closed:
            return
        self.record("end", {"clean": not bool(self.error)})
        self.closed = True
        try:
            self.pending.put(None, timeout=2)
        except queue.Full:
            self.error = "journal failed to drain"
        self.writer.join(timeout=3)
        if self.writer.is_alive():
            self.error = "journal close timeout"


def validate_report(report, now, fees):
    """Never trust a ready label without validating its supporting metrics."""
    if report.get("schema") != SCHEMA or report.get("symbol") != SYMBOL:
        raise ValueError("report schema / market mismatch")
    if report.get("policy_hash") != POLICY_HASH or report.get("implementation_hash") != implementation_hash():
        raise ValueError("policy / implementation changed; recalibrate")
    if report.get("fee_fingerprint") != fees["fingerprint"]:
        raise ValueError("fee / precision mismatch")
    end = finite(report["data_end"])
    if not 0 <= now - end <= POLICY.report_max_age:
        raise ValueError("calibration expired / future data")
    if report.get("accepted") is not True or report.get("offset") not in OFFSETS:
        raise ValueError("calibration not accepted")
    sessions = report["sessions"]
    if len(sessions) < 3 or len({s["utc_date"] for s in sessions}) < 3:
        raise ValueError("three distinct UTC sessions required")
    previous_end = 0
    for session in sessions:
        if (finite(session["end"]) - finite(session["start"]) < 1800
                or session["start"] < previous_end or len(session["sha256"]) != 64):
            raise ValueError("invalid session provenance")
        previous_end = session["end"]
    if end != previous_end:
        raise ValueError("data end mismatch")
    cases = report["holdout"]
    if {c["latency_ms"] for c in cases} != {250, 500} or len(cases) != 2:
        raise ValueError("both latency scenarios required")
    for case in cases:
        for name in ("experimental", "bbo", "mid"):
            metrics = case[name]
            if (metrics["round_trips"] < 500 or finite(metrics["volume"]) <= 0
                    or not metrics["funding_complete"] or metrics["unclosed"] or metrics.get("invalid")):
                raise ValueError("insufficient closed, funded holdout sample")
        if finite(case["experimental"]["cpm_upper95"]) > 0:
            raise ValueError("positive upper CPM bound")
        for bound in case["difference_upper95"].values():
            if finite(bound) > 0:
                raise ValueError("CPM improvement not established")
        if set(case["difference_upper95"]) != {"bbo", "mid"}:
            raise ValueError("missing baseline comparison")
    return report["offset"]


class Runtime:
    def __init__(self, post_info, account, directory=DATA_DIR, *, require_calibration=False):
        self.lock = threading.RLock()
        self.journal = Journal(directory)
        self.directory = Path(directory)
        self.post_info = post_info
        self.account = account  # never written to the journal
        self.require_calibration = require_calibration
        self.clock = ExchangeClock()
        self.features = Features()
        self.markouts = Markouts()
        self.fees = None
        self.report = None
        self.offset = None if require_calibration else 4
        self.reason = "calibration stricte requise" if require_calibration else "exécution directe · non calibrée"
        self.state = "collecting" if require_calibration else "connecting"
        self.last_source = 0
        self.last_book_received = 0
        self.latencies = deque(maxlen=1000)
        self.stopped = threading.Event()
        self.started = time.time()
        self.next_poll = 0
        self.worker = threading.Thread(target=self._monitor, name="experimental-monitor", daemon=True)
        self.worker.start()

    def _monitor(self):
        while not self.stopped.is_set():
            try:
                now = time.time()
                if now >= self.next_poll:
                    self.next_poll = now + 60
                    self.clock.refresh()
                    user = self.post_info({"type": "userFees", "user": self.account})
                    meta = self.post_info({"type": "meta", "dex": "xyz"})
                    fees = verified_fees(user, meta, time.time())
                    server_now = self.clock.now()
                    recorded_fees = dict(fees, observed_at=server_now) if self.require_calibration else fees
                    self.journal.record("fees", recorded_fees, server_now if self.require_calibration else None)
                    funding = self.post_info({"type": "fundingHistory", "coin": SYMBOL,
                                              "startTime": int((now - 7200) * 1000)}) if self.require_calibration else []
                    for item in funding:
                        self.journal.record("funding", dict(time=finite(item["time"]) / 1000,
                                                           rate=finite(item["fundingRate"])), self.clock.now())
                    with self.lock:
                        self.fees = fees
                with self.lock:
                    fees = self.fees
                if fees and self.require_calibration:
                    try:
                        report = json.loads((self.directory / "calibration.json").read_text(encoding="utf-8"))
                        server_now = self.clock.now()
                        if server_now is None:
                            raise ValueError("exchange clock unavailable")
                        offset = validate_report(report, server_now, fees)
                        with self.lock:
                            self.report, self.offset = report, offset
                    except (OSError, ValueError, KeyError, TypeError, StopIteration) as exc:
                        with self.lock:
                            self.report, self.offset = None, None
                            self.reason = f"échantillon insuffisant / calibration : {type(exc).__name__}: {exc}"
            except Exception as exc:
                with self.lock:
                    self.fees = None
                    self.offset = None if self.require_calibration else 4
                    self.reason = f"frais / funding non vérifiés : {type(exc).__name__}"
                    self.next_poll = time.time() + 5
            self.stopped.wait(5)

    def book(self, bids, asks, source_ts):
        now = time.time()
        # Store sanitized public market fields only, detached from the callback.
        levels = [[dict(px=finite(l["px"]), sz=finite(l["sz"])) for l in side[:20]] for side in (bids, asks)]
        with self.lock:
            server_now = self.clock.now()
            if server_now is None:
                self.features.book = None
                self.reason = "synchronisation de l'heure Hyperliquid"
                return
            if not math.isfinite(source_ts) or source_ts < self.last_source or abs(server_now - source_ts) > POLICY.book_max_age:
                self.journal.record("gap", {"reason": "source time invalid"})
                self.features.book = None
                self.reason = "horodatage carnet invalide"
                return
            if self.last_book_received and now - self.last_book_received > POLICY.book_max_age:
                self.journal.record("gap", {"reason": "book gap"})
            self.last_source, self.last_book_received = source_ts, now
            book = self.features.update(now, *levels)
            self.markouts.book(now, book["mid"])
            if self.require_calibration:
                self.journal.record("book", dict(levels=levels, source_ts=source_ts, server_now=server_now), server_now)

    def trades(self, data):
        if not self.require_calibration:
            return
        now = time.time()
        server_now = self.clock.now()
        if server_now is None:
            return
        source_started = self.started + server_now - now
        trades = [dict(tid=str(t["tid"]), side=str(t["side"]), px=finite(t["px"]),
                       sz=finite(t["sz"]), time=finite(t["time"]) / 1000) for t in data
                  if finite(t["time"]) / 1000 >= source_started]
        self.journal.record("trades", trades, server_now)

    def context(self, data):
        if not self.require_calibration:
            return
        ctx = data.get("ctx", {})
        self.journal.record("context", {"oracle": finite(ctx["oraclePx"])}, self.clock.now())

    def fill(self, ts, side, price, size, fee, pnl):
        with self.lock:
            self.markouts.fill(ts, side, price)
        self.journal.record("fill", dict(side=side, px=price, sz=size, fee=fee, realized_pnl=pnl), ts)

    def ack(self, operation, elapsed, success):
        with self.lock:
            self.latencies.append(elapsed * 1000)
        self.journal.record("ack", dict(operation=operation, latency_ms=elapsed * 1000, success=success))

    def permitted(self, now):
        with self.lock:
            if self.journal.error:
                self.state, self.reason = "blocked", self.journal.error
                return False
            if not self.fees or not 0 <= now - self.fees["observed_at"] <= POLICY.fee_max_age:
                self.state, self.reason = "blocked", "frais inconnus ou périmés"
                return False
            if self.require_calibration and (not self.report or self.offset is None):
                self.state = "collecting"
                return False
            reference_now = self.clock.now() if self.require_calibration else now
            if self.require_calibration and (reference_now is None or not 0 <= reference_now - self.report["data_end"] <= POLICY.report_max_age):
                self.state, self.reason = "blocked", "calibration expirée"
                return False
            book = self.features.book
            if not book or not 0 <= now - book["ts"] <= POLICY.book_max_age:
                self.state, self.reason = "blocked", "carnet périmé"
                return False
            self.state, self.reason = "ready", ("calibration acceptée; ouvertures autorisées" if self.require_calibration
                                               else "exécution directe · non calibrée · ouvertures autorisées")
            return True

    def decision(self, now, decimals, position=0, position_since=0):
        with self.lock:
            allowed = self.permitted(now)
            if not position and not allowed:
                return {}
            result = quotes(self.features.book, now, decimals, self.offset,
                            self.fees["maker"] if self.fees else 0, position, position_since)
            if allowed and result:
                self.state = "quoting"
            return result

    def snapshot(self):
        with self.lock:
            return dict(state=self.state, reason=self.reason, offset_ticks=self.offset,
                        execution_mode="validated" if self.require_calibration else "direct",
                        fees_verified=self.fees is not None, report_accepted=self.report is not None,
                        markouts=self.markouts.snapshot(), markouts_expired=self.markouts.expired,
                        journal_error=self.journal.error, journal=self.journal.path.name,
                        ack_latency_ms_p95=sorted(self.latencies)[int((len(self.latencies) - 1) * .95)] if self.latencies else None)

    def close(self):
        self.stopped.set()
        self.worker.join(timeout=1)
        self.journal.close()
