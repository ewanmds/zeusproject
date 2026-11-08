"""Per-market Outcome process supervisor and durable snapshot reader."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import uuid

from zeus import PROJECT_ROOT
from zeus.localize import localize_state
from zeus.process_control import ProcessLock, account_key
from zeus.safety import atomic_json, finite, safe_error, read_json


def duration(seconds):
    seconds = int(max(0, seconds))
    return f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"


class OutcomeSupervisor:
    def __init__(self, manager):
        self.manager = manager
        self.procs = {}
        self.cache = {}
        # Parsed worker stats keyed by path; a display tick reads each file at
        # most once and skips files whose mtime/size did not change.
        self._stats_cache = {}
        self._stats_lock = threading.Lock()
        self.temporary = None
        self.root = manager.state_dir
        from zeus.outcome_rotation import OutcomeRotation
        self.rotation = OutcomeRotation(self)

    def records(self):
        return [(p, r) for p, r in self.manager._session_files() if r.get("account_kind") == "outcome"]

    def _read_stats(self, path):
        path = Path(path)
        try:
            stat = path.stat()
        except OSError:
            return None
        key = (stat.st_mtime_ns, stat.st_size)
        with self._stats_lock:
            cached = self._stats_cache.get(path)
            if cached is not None and cached[0] == key:
                return cached[1]
        snap = read_json(path)
        with self._stats_lock:
            self._stats_cache[path] = (key, snap)
        return snap

    def start(self, request, *, rotation_group=None):
        self.rotation.assert_admission(rotation_group)
        from zeus import outcomes
        selected = str(request.market or "").strip()
        selection = None
        if request.selection_mode == "auto":
            from zeus.outcome_flow import scanner
            if request.exec_mode != "maker":
                raise ValueError("Automatic flow selection requires Maker / post-only")
            snapshot = scanner.snapshot(request.outcome_side or "no", request.order_size_usd,
                                        request.auto_max_spread_bps)
            busy = {b["market"] for b in self.status()["bots"]
                    if b.get("running") or not b.get("cleanup_confirmed")}
            selection = next((r for r in snapshot["candidates"] if r["market"] not in busy
                              and self.rotation.candidate_allowed(rotation_group, r["market"])), None)
            if not snapshot["ready"] or selection is None:
                raise ValueError("No eligible recent two-way taker flow. Open Auto and wait for observation.")
            selected = selection["market"]
        if not selected or selected.upper() == "ALL":
            raise ValueError("Select a specific Outcome market: one process per market")
        resolved = outcomes.resolve_market(selected)
        if resolved is None:
            raise ValueError("Unknown Outcome market; refresh the catalog")
        outcome_id, encoded_side = divmod(int(str(resolved["key"])[1:]), 10)
        side = request.outcome_side or ("yes" if encoded_side == 0 else "no")
        market = f"#{outcome_id * 10 + (0 if side == 'yes' else 1)}"
        resolved = outcomes.resolve_market(market)
        if resolved is None:
            raise ValueError("This Outcome side is unavailable")
        from zeus.outcome_policy import blocked_outcome
        if blocked_outcome(resolved):
            raise ValueError("Fed interest-rate markets are excluded")
        account = os.getenv("WALLET_ADDRESS_OUTCOME", "").strip()
        if not account or not os.getenv("PRIVATE_KEY_OUTCOME", "").strip():
            raise ValueError("Outcome requires WALLET_ADDRESS_OUTCOME and PRIVATE_KEY_OUTCOME in .env")
        public = request.model_dump()
        public["market"] = market
        public["outcome_side"] = side
        if selection is not None:
            public["two_sided"] = True
            public["rotation_pnl_carry"] = self.rotation.groups[rotation_group].get("pnl_carry", 0) if rotation_group else 0
            public["auto_selection"] = {**selection, "selected_at": time.time()}
            # Quote at touch instead of improving a spread chosen in a draft.
            public["maker_spread_pct"] = None
        if self.root is None:
            self.temporary = tempfile.TemporaryDirectory(prefix="zeus-offline-")
            self.root = Path(self.temporary.name)
        # Resolve/read metadata above without holding the rotation/UI lock.
        # Admission, spawn and registration below are atomic against Stop.
        with ProcessLock(self.root / "admission" / f"{account_key(account)}-{market[1:]}.lock"), self.rotation.lock, self.manager.lock:
            for info in self.procs.values():
                if info["market"] == market:
                    if info["proc"].poll() is None:
                        raise RuntimeError("A bot is already running on this Outcome market")
                    if not self.manager._session_status(info["safety_file"]).get("cleanup_confirmed"):
                        raise RuntimeError("Previous Outcome stop not confirmed on this market")
            for _, record in self.records():
                if market in record.get("symbols", []) and not record.get("cleanup_confirmed"):
                    raise RuntimeError("Previous Outcome session is not reconciled on this market")
            session_id = uuid.uuid4().hex
            if public.get("selection_mode") == "auto":
                public["auto_group_id"] = rotation_group or session_id
            runtime = self.root / "runtime" / session_id
            runtime.mkdir(parents=True, exist_ok=False)
            journal = self.manager._reserve_session("outcome:" + market, [market], "outcome")
            journal = journal or self.root / (session_id + ".json")
            stop_file, stats_file = runtime / "stop", runtime / "stats.json"
            record = {"session_id": session_id, "key": "outcome:" + market, "symbols": [market],
                      "account_kind": "outcome", "phase": "connecting", "config": public,
                      "account": account, "funds_root": str(self.root / "funds"),
                      "market_name": resolved.get("name", market), "started": time.time(),
                      "cleanup_confirmed": False, "can_start": False, "error": "", "cleanup_error": "",
                      "stop_file": str(stop_file), "stats_file": str(stats_file), "pid": os.getpid()}
            atomic_json(journal, record)
            config_file = runtime / "config.json"
            atomic_json(config_file, {"config": public, "safety_file": str(journal),
                        "funds_root": str(self.root / "funds"), "locks_root": str(self.root / "instances")})
            env = os.environ.copy()
            env["ZEUS_PARENT_PID"] = str(os.getpid())
            env["OUTCOME_HEADLESS"] = "1"
            cmd = [sys.executable, "-u", "-m", "zeus.bots.outcome_worker", "--config", str(config_file)]
            try:
                self.rotation.assert_admission(rotation_group)
                proc = subprocess.Popen(cmd, cwd=str(PROJECT_ROOT), env=env,
                            stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            except Exception as exc:
                atomic_json(journal, {**record, "phase": "failed", "error": safe_error(exc),
                                     "cleanup_confirmed": True, "can_start": True, "pid": None})
                raise
            # The child owns journal publication after launch. Do not overwrite a
            # racing first worker snapshot with the parent's connecting state.
            self.procs[session_id] = {**record, "proc": proc, "market": market, "pid": proc.pid,
                                      "safety_file": journal}
            result = {"session_id": session_id, "market": market, "pid": proc.pid, "phase": "connecting"}
            self.rotation.register(result, public, rotation_group)
        return result

    def _entry(self, journal, record, info=None):
        from zeus.server import _pid_alive
        session_id = record["session_id"]
        alive = info["proc"].poll() is None if info else _pid_alive(record.get("pid"))
        # An archived, durably verified terminal session cannot become active
        # again when the OS assigns its old PID to an unrelated application.
        if not info and record.get("cleanup_confirmed") is True and record.get("phase") in {"stopped", "failed"}:
            alive = False
        # A journal without an attached child is interrupted, never auto-attached.
        phase = record.get("phase", "failed")
        snap = {}
        if record.get("stats_file"):
            try:
                snap = self._read_stats(record["stats_file"])
                if (not isinstance(snap, dict) or snap.get("session_id") != session_id
                        or snap.get("market") not in record.get("symbols", [])):
                    snap = {}
                else:
                    finite(snap["ts"])
                    for key in ("volume_usd", "net_pnl", "fees", "fills", "duration_sec", "position_usd"):
                        if key in snap and snap[key] is not None:
                            finite(snap[key])
            except (OSError, ValueError, TypeError, KeyError):
                snap = {}
        if snap:
            self.cache[session_id] = snap
        else:
            snap = self.cache.get(session_id, {})
        # Keep metadata frozen, but use the newest real lifecycle publication.
        lifecycle = snap if snap.get("ts", 0) >= (record.get("ts") or 0) else record
        phase = lifecycle.get("phase", phase)
        if not info and alive and not record.get("cleanup_confirmed"):
            phase = "failed"
        if not alive and not record.get("cleanup_confirmed"):
            phase = "failed"
        confirmed = not alive and record.get("cleanup_confirmed") is True
        complete = all(key in snap for key in ("volume_usd", "net_pnl", "fees", "fills", "duration_sec"))
        stale = bool(alive and (not complete or time.time() - float(snap.get("ts", 0)) > 5))
        entry = {**record, **snap, "session_id": session_id, "market": record.get("symbols", [""])[0],
                 "config": record.get("config", {}), "started": record.get("started", snap.get("started", 0)),
                 "market_name": record.get("market_name", snap.get("market_name")),
                 "error": lifecycle.get("error", ""), "recovery_error": lifecycle.get("recovery_error", ""),
                 "cleanup_error": lifecycle.get("cleanup_error", ""),
                 "pid": info["proc"].pid if info else record.get("pid"), "running": alive,
                 "phase": phase, "stale": stale, "cleanup_confirmed": confirmed,
                 "can_start": confirmed, "accounting_complete": complete,
                 "quoting_enabled": bool(alive and not stale and phase == "running"),
                 "position_known": confirmed or bool(lifecycle.get("position_known") and not stale
                                                       and snap.get("position_usd") is not None)}
        for key in ("volume_usd", "net_pnl", "fees", "fills", "duration_sec"):
            entry[key] = finite(snap.get(key, 0))
        if alive and entry.get("started"):
            entry["duration_sec"] = max(0, time.time()-entry["started"])
        entry["sampled_at"] = time.time()
        entry["duration"] = duration(entry["duration_sec"])
        entry["fills"] = int(entry["fills"])
        entry["position_usd"] = 0.0 if confirmed else finite(snap["position_usd"]) if entry["position_known"] else None
        entry["cost_per_1m"] = -entry["net_pnl"] * 1e6 / entry["volume_usd"] if entry["volume_usd"] else None
        if phase == "failed" and not confirmed:
            entry["error"] = lifecycle.get("error") or "Interrupted Outcome session; stop not confirmed"
        entry.update(self.rotation.describe(session_id))
        return localize_state(entry)

    def status(self):
        with self.manager.lock:
            attached = dict(self.procs)
            records = {str(p): (p, r) for p, r in self.records()}
        for info in attached.values():
            p = info["safety_file"]
            records[str(p)] = (p, self.manager._session_status(p))
        latest = {}
        for journal, record in records.values():
            if not record.get("symbols"):
                continue
            market = record["symbols"][0]
            if market not in latest or record.get("started", 0) > latest[market][1].get("started", 0):
                latest[market] = (journal, record)
        all_entries = {r["session_id"]: self._entry(p, r, attached.get(r["session_id"]))
                       for p, r in records.values() if r.get("symbols")}
        bots = [all_entries[r["session_id"]] for _, r in latest.values()]
        result = {"type": "worldcup_stats", "bots": bots, "running": any(b["running"] for b in bots),
                  "active_markets": sum(b["running"] for b in bots), "can_start": True,
                  "phase": "running" if any(b["running"] for b in bots) else "stopped",
                  "cleanup_confirmed": all(b["cleanup_confirmed"] for b in bots),
                  "stale": False, "partial_stale": any(b["stale"] for b in bots),
                  "config": {}, "mid_price": 0, "spread_bps": 0}
        result["accounting_complete"] = all(b["accounting_complete"] for b in all_entries.values())
        for key in ("volume_usd", "net_pnl", "fees", "fills"):
            result[key] = sum(b[key] for b in all_entries.values())
        from zeus.dashboard_state import outcome_projection
        result.update(outcome_projection(list(all_entries.values()), self.rotation.display_intents()))
        result["duration"] = result["active_metrics"]["duration"]
        result["cost_per_1m"] = -result["net_pnl"] * 1e6 / result["volume_usd"] if result["volume_usd"] else None
        result["position_usd"] = sum(b["position_usd"] for b in bots) if all(b["position_known"] for b in bots) else None
        return result

    def stop(self, session_id=None):
        chain_targets = self.rotation.cancel(session_id)
        with self.manager.lock:
            targets = [info for sid, info in self.procs.items()
                       if session_id is None or sid == session_id or sid in chain_targets]
        if session_id is not None and not targets:
            matching = [r for _, r in self.records() if r.get("session_id") == session_id]
            if (matching and matching[0].get("cleanup_confirmed") is True
                    and matching[0].get("phase") in {"stopped", "failed"}):
                return [session_id]
            raise RuntimeError("Session unavailable or interrupted; reconcile its stop first")
        # Signal every target before waiting for any child (also during startup).
        for info in targets:
            if info["proc"].poll() is None:
                Path(info["stop_file"]).touch()
        def wait(info):
            try:
                info["proc"].wait(timeout=30)
            except subprocess.TimeoutExpired:
                return False
            return self.manager._session_status(info["safety_file"]).get("cleanup_confirmed") is True
        if targets:
            with ThreadPoolExecutor(max_workers=len(targets)) as pool:
                confirmed = all(list(pool.map(wait, targets)))
            if not confirmed:
                raise RuntimeError("Outcome stop not confirmed; verify orders and positions")
        if session_id is None and any(not b["cleanup_confirmed"] for b in self.status()["bots"]):
            raise RuntimeError("Outcome stop not confirmed; interrupted sessions require reconciliation")
        return [info["session_id"] for info in targets]
