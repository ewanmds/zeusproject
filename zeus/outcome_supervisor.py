"""Per-market Outcome process supervisor and durable snapshot reader."""
import logging
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import uuid

from zeus import PROJECT_ROOT
from zeus.dashboard import localize_state
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
        # Crash recovery is owned by the backend: no UI button is required.
        self._recovery = {}
        self._recovery_lock = threading.Lock()
        self._closed = threading.Event()
        self._recovery_thread = None

    def start_recovery(self):
        """Enable recovery only in a running backend, never on import/audit."""
        with self._recovery_lock:
            if self._closed.is_set() or self._recovery_thread is not None:
                return
            self._recovery_thread = threading.Thread(
                target=self._recovery_loop, name="outcome-auto-recover", daemon=True)
            self._recovery_thread.start()

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

    @staticmethod
    def _drain_intent(record):
        """Read the persisted maker-exit request, if any. Never blocks."""
        stop_file = record.get("stop_file")
        if not stop_file:
            return {}
        path = Path(stop_file).with_name("drain.json")
        try:
            if not path.exists():
                return {}
            request = read_json(path)
        except (OSError, ValueError):
            # A file racing an atomic replace is not proof of an exit request.
            return {}
        return request if isinstance(request, dict) else {}

    def close(self):
        self._closed.set()
        self.rotation.close()  # close replacement admission before waiting on I/O
        thread = getattr(self, "_recovery_thread", None)
        if thread is not None and thread.is_alive():
            thread.join(timeout=2)

    def _recovery_loop(self):
        log = logging.getLogger(__name__)
        while not self._closed.wait(5):
            try:
                self._auto_recover()
            except Exception:
                log.exception("Outcome auto-recovery loop failed")

    def _auto_recover(self):
        """Restart an interrupted Outcome worker without any user action.

        The latest leg of each logical session is considered. A manual Stop or
        a hard shutdown is respected; a crash/restart is resumed as trading,
        or as an exit when an exit was already requested.
        """
        records = list(self.records())
        if not records:
            return
        by_id = {r.get("session_id"): r for _, r in records}
        groups = {}
        for path, record in records:
            if not record.get("symbols"):
                continue
            key = ((record.get("config") or {}).get("auto_group_id")
                   or record.get("session_id"))
            current = groups.get(key)
            if current is None or record.get("started", 0) > current[1].get("started", 0):
                groups[key] = (path, record)
        now = time.time()
        with self._recovery_lock:
            for key, (path, record) in groups.items():
                sid = record.get("session_id")
                if not sid or record.get("cleanup_confirmed"):
                    continue
                info = self.procs.get(sid)
                if info is not None and info["proc"].poll() is None:
                    continue
                if record.get("resumed_session_id") in by_id:
                    continue
                market = (record.get("symbols") or [""])[0]
                if not market:
                    continue
                if any(i.get("market") == market and i["proc"].poll() is None
                       for i in self.procs.values()):
                    continue
                stop_file = Path(record["stop_file"]) if record.get("stop_file") else None
                if stop_file is None:
                    continue
                if stop_file.exists():
                    continue  # explicit hard shutdown, never auto-trade again
                meta = read_json(path) if Path(path).exists() else record
                meta = meta if isinstance(meta, dict) else record
                drain_file = stop_file.with_name("drain.json")
                drain = read_json(drain_file) if drain_file.exists() else {}
                drain = drain if isinstance(drain, dict) else {}
                reason = str(meta.get("stop_reason") or drain.get("reason") or "")
                reason_l = reason.lower()
                manual = bool(drain.get("manual")) or reason_l.startswith("manual stop")
                risk_exit = reason_l.startswith(("session stop loss", "session take profit",
                                                 "inventory stop"))
                rotation = reason_l.startswith("low executed")
                if manual or risk_exit:
                    exit_only = True
                elif rotation:
                    # A rotation drain must not end the logical session after a
                    # restart: resume the same market in bid+ask and keep rotating.
                    exit_only = False
                else:
                    exit_only = bool(meta.get("exit_requested")) or bool(drain)
                if manual and not exit_only:
                    continue
                if not meta.get("exposure_started"):
                    continue  # attribution is not provable; leave it unresolved
                state = self._recovery.get(sid) or {}
                if now < state.get("next_at", 0):
                    continue
                try:
                    self._resume(sid, exit_only=exit_only)
                    self._recovery.pop(sid, None)
                    logging.getLogger(__name__).warning(
                        "Outcome auto-recovery started %s (exit_only=%s)", sid, exit_only)
                except Exception as exc:
                    failures = int(state.get("failures", 0)) + 1
                    detail = safe_error(exc)
                    self._recovery[sid] = {
                        "failures": failures,
                        "next_at": now + min(300, 5 * 2 ** min(failures, 6)),
                        "error": detail,
                    }
                    logging.getLogger(__name__).warning(
                        "Outcome auto-recovery %s failed (%s): %s", sid, failures, detail)

    def start(self, request, *, rotation_group=None, seed=None):
        if (PROJECT_ROOT/'.zeus-state'/'maintenance').exists() and not getattr(request, 'resume_exit', False):
            raise ValueError('Mise à jour du workspace en cours ; nouvelles entrées suspendues')
        self.rotation.assert_admission(rotation_group)
        from zeus import outcomes
        if getattr(request, "exec_mode", "maker") != "maker":
            raise ValueError("Outcome sessions are post-only maker only")
        selected = str(request.market or "").strip()
        selection = None
        context = getattr(self.manager, 'account_context', None)
        account = context.account if context else os.getenv("WALLET_ADDRESS_OUTCOME", "").strip()
        if request.selection_mode == "auto":
            from zeus.outcome_flow import scanner
            if request.exec_mode != "maker":
                raise ValueError("Automatic flow selection requires Maker / post-only")
            snapshot = scanner.snapshot(request.outcome_side or "no", request.order_size_usd,
                                        request.auto_max_spread_bps, account=account)
            busy = {b["market"] for b in self.status()["bots"]
                    if b.get("running") or not b.get("cleanup_confirmed")}
            selection = next((r for r in snapshot["candidates"] if r["market"] not in busy
                              and self.rotation.candidate_allowed(rotation_group, r["market"])), None)
            if not snapshot["ready"]:
                raise ValueError(snapshot.get('reason') or snapshot.get('error')
                                 or "No eligible recent two-way flow within the spread and depth limits. Wait for observation.")
            if selection is None:
                raise ValueError("Tous les marchés éligibles sont déjà occupés par une session")
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
        from zeus.outcome_flow import blocked_outcome
        if blocked_outcome(resolved):
            raise ValueError("Fed interest-rate markets are excluded")
        if not account or not (context.private_key if context else os.getenv("PRIVATE_KEY_OUTCOME", "").strip()):
            raise ValueError("Outcome requires WALLET_ADDRESS_OUTCOME and PRIVATE_KEY_OUTCOME in .env")
        public = request.model_dump()
        if seed:
            # A recovered leg keeps the logical session identity and may opt
            # back into low-fill rotation even with an explicit market.
            if seed.get("group_id"):
                public["auto_group_id"] = seed["group_id"]
            if seed.get("resume_rotation"):
                public["resume_rotation"] = True
        if public.get('session_duration_sec') and not public.get('session_expires_at'):
            public['session_expires_at'] = time.time()+public['session_duration_sec']
        public["market"] = market
        public["outcome_side"] = side
        # Every Outcome leg is an exit-capable post-only maker quoting at the
        # touch; drafts cannot select taker or a wider offset.
        public["exec_mode"] = "maker"
        public["maker_spread_pct"] = None
        if selection is not None:
            public["two_sided"] = True
            public["rotation_pnl_carry"] = self.rotation.groups[rotation_group].get("pnl_carry", 0) if rotation_group else 0
            public['session_volume_carry'] = self.rotation.groups[rotation_group].get('volume_carry', 0) if rotation_group else 0
            public["auto_selection"] = {**selection, "selected_at": time.time()}
            # Quote at touch instead of improving a spread chosen in a draft.
            public["maker_spread_pct"] = None
        if seed and "rotation_pnl_carry" in seed:
            public["rotation_pnl_carry"] = finite(seed["rotation_pnl_carry"])
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
            resume_from = (seed or {}).get("resume_from")
            for _, record in self.records():
                if resume_from and record.get("session_id") == resume_from:
                    continue  # the explicit resume replaces this interrupted session
                if market in record.get("symbols", []) and not record.get("cleanup_confirmed"):
                    raise RuntimeError("Previous Outcome session is not reconciled on this market")
            session_id = uuid.uuid4().hex
            if rotation_group or public.get("selection_mode") == "auto":
                public["auto_group_id"] = rotation_group or public.get("auto_group_id") or session_id
            runtime = self.root / "runtime" / session_id
            runtime.mkdir(parents=True, exist_ok=False)
            journal = self.manager._reserve_session(
                "outcome:" + market, [market], "outcome",
                ignore_session_id=(seed or {}).get("resume_from"))
            journal = journal or self.root / (session_id + ".json")
            stop_file, stats_file = runtime / "stop", runtime / "stats.json"
            record = {"session_id": session_id, "key": "outcome:" + market, "symbols": [market],
                      "account_kind": "outcome", "phase": "connecting", "config": public,
                      "account": account, "funds_root": str(self.root / "funds"),
                      "market_name": resolved.get("name", market), "started": time.time(),
                      "cleanup_confirmed": False, "can_start": False, "error": "", "cleanup_error": "",
                      "stop_file": str(stop_file), "stats_file": str(stats_file), "pid": os.getpid()}
            # A resumed exit must be able to claim exactly the inventory and
            # client IDs the interrupted session already published.
            for key in ("exposure_started", "owned_oids", "cloids", "pending_cloids", "inventory_since", "exit_started_at"):
                if seed and key in seed:
                    record[key] = seed[key]
            atomic_json(journal, record)
            config_file = runtime / "config.json"
            atomic_json(config_file, {"config": public, "safety_file": str(journal),
                        "funds_root": str(self.root / "funds"), "locks_root": str(self.root / "instances")})
            env = os.environ.copy()
            if context:
                env.update(context.environment())
                env['ZEUS_SIGNING_ROOT'] = str(PROJECT_ROOT / '.zeus-state' / 'signing')
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
            self.rotation.register(result, public,
                                   rotation_group or (seed or {}).get("group_id"))
        return result

    def _entry(self, journal, record, info=None):
        session_id = record["session_id"]
        # Only a child attached to this server is displayed as live. An
        # unattached journal needs reconciliation; trusting a recycled OS PID
        # would show a phantom running session after a crash or reboot.
        alive = info["proc"].poll() is None if info else False
        # An archived, durably verified terminal session cannot become active
        # again when the OS assigns its old PID to an unrelated application.
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
        drain = self._drain_intent(record)
        drain_reason = str(drain.get("reason") or "")
        hard_stop = bool(record.get("stop_file") and Path(record["stop_file"]).exists())
        confirmed = not alive and record.get("cleanup_confirmed") is True
        exit_requested = bool(lifecycle.get("exit_requested") or drain) and not confirmed
        # The persisted drain request is authoritative even before the worker
        # publishes its own exit intent. A hard-stop sentinel keeps precedence.
        if alive and exit_requested and not hard_stop and phase != "failed":
            phase = "draining"
        if lifecycle.get("exit_requested") and phase == "running":
            phase = "draining"
        if not alive and not record.get("cleanup_confirmed"):
            phase = "failed"
        complete = all(key in snap for key in ("volume_usd", "net_pnl", "fees", "fills", "duration_sec"))
        # "Stale" means the worker stopped publishing, never that accounting is
        # still being completed. A warm worker whose first snapshot lacks a
        # field is quoting normally, not delayed. Before the first snapshot,
        # the launch time is the reference with a short startup grace.
        last_ts = float(snap.get("ts", 0) or 0)
        reference = last_ts or float(record.get("started", 0) or 0)
        if phase == "connecting" and lifecycle.get("startup_wait") and not lifecycle.get("exposure_started"):
            reference = float(lifecycle.get("ts", reference))
        stale = bool(alive and reference and time.time() - reference > 10)
        entry = {**record, **snap, "session_id": session_id, "market": record.get("symbols", [""])[0],
                 "config": record.get("config", {}), "started": record.get("started", snap.get("started", 0)),
                 "market_name": record.get("market_name", snap.get("market_name")),
                 "error": lifecycle.get("error", ""), "recovery_error": lifecycle.get("recovery_error", ""),
                 "cleanup_error": lifecycle.get("cleanup_error", ""),
                 "pid": info["proc"].pid if info else record.get("pid"), "running": alive,
                 "phase": phase, "stale": stale, "cleanup_confirmed": confirmed,
                 "can_start": confirmed, "accounting_complete": complete,
                 "exit_requested": bool(exit_requested or (alive and phase == "draining")),
                 "stop_reason": (drain_reason or lifecycle.get("stop_reason")
                                 or record.get("stop_reason") or ""),
                 # A draining session may still sell inventory passively, but
                 # it must never be presented as quoting normally.
                 "quoting_enabled": bool(alive and not stale and phase == "running"
                                          and not exit_requested),
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

    @staticmethod
    def _tracked(record):
        """Confirmed terminal sessions can no longer change."""
        if not record.get("symbols"):
            return False
        return not (record.get("cleanup_confirmed") is True
                    and record.get("phase") in {"stopped", "failed"})

    def _prune(self, kept):
        """Keep the process/cache registries bounded on long-lived servers."""
        with self.manager.lock:
            if len(self.procs) > 80:
                dead = [sid for sid, info in self.procs.items() if info["proc"].poll() is not None]
                for sid in dead[:max(0, len(self.procs)-80)]:
                    if str(self.procs[sid]["safety_file"]) not in kept:
                        self.procs.pop(sid, None)
        if len(self.cache) > 2048:
            live = {record["session_id"] for _, record in kept.values()}
            for sid in [sid for sid in self.cache if sid not in live]:
                self.cache.pop(sid, None)
        # Parsed stats files are only useful while their session is tracked.
        with self._stats_lock:
            if len(self._stats_cache) > 512:
                live_files = {str(record.get("stats_file")) for _, record in kept.values()
                              if record.get("stats_file")}
                for path in [p for p in self._stats_cache if str(p) not in live_files]:
                    self._stats_cache.pop(path, None)

    def status(self):
        # Filesystem scans happen outside the manager lock so Start/Stop never
        # waits for a dashboard collection.
        with self.manager.lock:
            attached = dict(self.procs)
        records = {str(p): (p, r) for p, r in self.records()}
        for info in attached.values():
            p = info["safety_file"]
            records[str(p)] = (p, self.manager._session_status(p))

        # Only live, unconfirmed, or just-closed rows are read. Confirmed
        # history is excluded, which keeps a tick bounded over months.
        kept = {key: value for key, value in records.items() if self._tracked(value[1])}
        groups = {r.get("auto_group_id") or r.get("config", {}).get("auto_group_id")
                  for _, r in kept.values()}
        groups.discard(None)
        # A drained worker that is already confirmed drops out of `kept`, but
        # its rotation group still needs it to observe the stop and launch the
        # next market. Keep every non-cancelled rotation group visible.
        intents = self.rotation.display_intents()
        groups.update(intent["group_id"] for intent in intents if not intent.get("cancelled"))
        for key, (path, record) in records.items():
            gid = record.get("auto_group_id") or record.get("config", {}).get("auto_group_id")
            if gid in groups:
                kept.setdefault(key, (path, record))

        latest = {}
        for journal, record in kept.values():
            if not record.get("symbols"):
                continue
            market = record["symbols"][0]
            if market not in latest or record.get("started", 0) > latest[market][1].get("started", 0):
                latest[market] = (journal, record)
        all_entries = {r["session_id"]: self._entry(p, r, attached.get(r["session_id"]))
                       for p, r in kept.values() if r.get("symbols")}
        bots = [all_entries[r["session_id"]] for _, r in latest.values()]
        from zeus.dashboard import outcome_projection
        projection = outcome_projection(list(all_entries.values()), intents)
        metrics = projection["active_metrics"]
        result = {
            "type": "worldcup_stats",
            "bots": bots,
            "display_bots": projection["display_bots"],
            "active_metrics": metrics,
            "running": metrics["running"],
            "active_markets": metrics["active_markets"],
            "can_start": not self.manager._corrupt_journals,
            "phase": "running" if metrics["running"] else "stopped",
            # Aggregate every tracked member, including an older leg that a
            # newer confirmed sibling must never hide.
            "cleanup_confirmed": all(e["cleanup_confirmed"] for e in all_entries.values()),
            "stale": metrics["stale"],
            "partial_stale": any(b["stale"] for b in bots),
            "accounting_complete": metrics["accounting_complete"],
            "duration": metrics["duration"],
            "duration_sec": metrics["duration_sec"],
            "volume_usd": metrics["volume_usd"],
            "net_pnl": metrics["net_pnl"],
            "fees": metrics["fees"],
            "fills": metrics["fills"],
            "cost_per_1m": metrics["cost_per_1m"],
            "position_usd": metrics["position_usd"],
            "config": {}, "mid_price": 0, "spread_bps": 0,
            "journal_error": "; ".join(f"{name}: {error}"
                                        for name, error in self.manager._corrupt_journals.items()),
        }
        self._prune(kept)
        return result

    @staticmethod
    def _drain_path(info):
        stop_file = info.get("stop_file")
        return Path(stop_file).with_name("drain.json") if stop_file else None

    def _orphan_alive(self, record):
        """A confirmed journal is not proof that an untracked worker is gone."""
        account = record.get("account")
        market = (record.get("symbols") or [""])[0]
        if not account or not market or self.root is None:
            return False
        from zeus.process_control import account_key
        path = self.root / "instances" / f"{account_key(account)}-{market[1:]}.lock"
        try:
            with ProcessLock(path, timeout=0):
                return False
        except TimeoutError:
            return True

    def _request_maker_exit(self, info, reason):
        """Persist an exit-only intent. Never kills the worker or sells taker."""
        path = self._drain_path(info)
        if path is None:
            return
        try:
            # Rewriting the same durable request is idempotent. Include both
            # `manual_stop` (task contract) and `manual` (existing consumers).
            atomic_json(path, {"requested_at": time.time(), "reason": reason,
                               "session_finished": True, "manual_stop": True,
                               "manual": True})
        except OSError as exc:
            raise RuntimeError("Outcome exit journal unavailable: " + safe_error(exc)) from exc

    def _stop_or_force(self, info, reason, *, targeted=False, mode="graceful"):
        """Graceful persists a maker exit; cancel_only closes the execution gate.

        `graceful` is idempotent: repeated clicks rewrite the same request and
        never change the policy. `cancel_only` touches the hard-stop sentinel,
        which cancels owned orders without any taker sale and leaves any
        residual inventory unresolved and visible.
        """
        if mode == "cancel_only":
            stop_file = info.get("stop_file")
            if stop_file:
                try:
                    Path(stop_file).touch()
                except OSError as exc:
                    raise RuntimeError("Outcome stop signal unavailable: "
                                       + safe_error(exc)) from exc
            return "cancelled"
        self._request_maker_exit(info, reason)
        return "requested"

    def stop(self, session_id=None, mode="graceful"):
        """Record an exit request and return without waiting for the worker.

        `graceful` means "stop and exit": cancel orders, sell inventory maker,
        and fall back to a bounded IOC after the configured delay. `cancel_only`
        means "immediate stop": close entries, cancel owned orders, and report
        residual inventory without any taker sale. A 200 only records the
        request; the snapshot proves the account state.
        """
        if mode not in {"graceful", "cancel_only"}:
            raise ValueError("Unknown Outcome stop mode")
        # cancel() takes the same lock used for replacement admission, so no
        # rotation launch can slip behind this Stop.
        chain_targets = self.rotation.cancel(session_id)
        # An archived confirmed leg may still own an active descendant through
        # its logical auto group. Include that whole chain, not just the exact
        # worker id that was clicked.
        related = [(p, r) for p, r in self.records()
                   if session_id is None or r.get("session_id") == session_id
                   or (r.get("config") or {}).get("auto_group_id") == session_id]
        group_ids = {session_id} if session_id is not None else set()
        for _, record in related:
            group = (record.get("config") or {}).get("auto_group_id")
            if group:
                group_ids.add(group)
        if group_ids:
            related = [(p, r) for p, r in self.records()
                       if r.get("session_id") in group_ids
                       or (r.get("config") or {}).get("auto_group_id") in group_ids]
        with self.manager.lock:
            targets = [info for sid, info in self.procs.items()
                       if session_id is None or sid == session_id or sid in chain_targets
                       or (info.get("config") or {}).get("auto_group_id") in group_ids]
        reason = ("Immediate cancel: cancel orders" if mode == "cancel_only"
                  else "Manual stop: maker exit")
        if session_id is not None and not targets:
            matching = [r for _, r in related]
            if not matching:
                raise RuntimeError("Session unavailable or interrupted; reconcile its stop first")
            # A grouped row must surface its unconfirmed leg, never a hidden
            # confirmed sibling.
            pending = [r for r in matching if r.get("cleanup_confirmed") is not True]
            if not pending:
                if any(self._orphan_alive(r) for r in matching):
                    raise RuntimeError("An Outcome worker is still active on this market; stop it first")
                return [session_id]
            for record in pending:
                self._stop_or_force(record, reason, targeted=True, mode=mode)
            return list(dict.fromkeys([session_id]
                                      + [r.get("session_id") for r in pending
                                         if r.get("session_id")]))
        requested_ids = []
        for info in targets:
            self._stop_or_force(info, reason, targeted=session_id is not None, mode=mode)
            requested_ids.append(info["session_id"])
        if session_id is None:
            attached = {info["session_id"] for info in targets}
            for _, record in self.records():
                if record.get("session_id") not in attached and not record.get("cleanup_confirmed"):
                    self._stop_or_force(record, reason, mode=mode)
                    requested_ids.append(record.get("session_id"))
        return list(dict.fromkeys(sid for sid in requested_ids if sid))

    def resume_exit(self, session_id):
        """Start an exit-only worker for a stopped, unconfirmed Outcome session."""
        return self._resume(session_id, exit_only=True)

    def resume_trading(self, session_id):
        """Resume normal bid+ask trading on the same market with attribution."""
        return self._resume(session_id, exit_only=False)

    def _resume(self, session_id, *, exit_only):
        matching = [(p, r) for p, r in self.records()
                    if r.get("session_id") == session_id
                    or (r.get("config") or {}).get("auto_group_id") == session_id]
        if not matching:
            raise KeyError("Session not found")
        target = next(((p, r) for p, r in matching
                       if r.get("cleanup_confirmed") is not True), None)
        if target is None:
            raise ValueError("Session already stopped and verified")
        journal_path, record = target
        if record.get("account_kind") != "outcome":
            raise ValueError("Only Outcome sessions can be resumed")
        market = (record.get("symbols") or [""])[0]
        if (not market or not market.startswith("#") or not market[1:].isdigit()
                or int(market[1:]) % 10 not in (0, 1)):
            raise ValueError("Session has no exact Outcome market")
        if record.get("phase") and record.get("phase") not in {
                "connecting", "running", "draining", "recovering", "stopping",
                "failed", "stopped"}:
            raise ValueError("Unknown Outcome session state; reconcile manually")
        context = getattr(self.manager, "account_context", None)
        expected_account = (context.account if context
                            else os.getenv("WALLET_ADDRESS_OUTCOME", "").strip())
        record_account = str(record.get("account") or "").strip()
        if expected_account and record_account and record_account.lower() != expected_account.lower():
            raise ValueError("Session belongs to a different Outcome account")
        with self.manager.lock:
            for info in self.procs.values():
                if info.get("market") == market and info["proc"].poll() is None:
                    raise RuntimeError("A worker is already active on this market")
            previous = self.procs.get(record.get("resumed_session_id"))
            if previous is not None and previous["proc"].poll() is None:
                raise RuntimeError("A resume is already running for this session")
        # A dead journal can still describe a live orphan child. Check both the
        # last published PID and the worker-lifetime lock before spawning.
        if "pid" in record:
            try:
                pid = int(record.get("pid"))
            except (TypeError, ValueError):
                raise ValueError("Unknown worker PID; reconcile manually") from None
            if pid <= 0:
                raise ValueError("Unknown worker PID; reconcile manually")
            from zeus.process_control import pid_alive
            if pid_alive(pid):
                raise RuntimeError("The Outcome worker is still active; stop it first")
        if record_account and getattr(self, "root", None) is not None:
            from zeus.process_control import ProcessLock, account_key
            lock = Path(self.root) / "instances" / f"{account_key(record_account)}-{market[1:]}.lock"
            try:
                with ProcessLock(lock, timeout=0):
                    pass
            except TimeoutError:
                raise RuntimeError("An Outcome worker is still active on this market") from None
        for _, other in self.records():
            if other.get("session_id") == record.get("session_id"):
                continue
            if (other.get("cleanup_confirmed") is not True
                    and market in (other.get("symbols") or [])):
                raise RuntimeError("Another Outcome session is still active on this market")
        from zeus.server import WorldcupStartRequest
        config = dict(record.get("config") or {})
        if config.get("market") and config.get("market") != market:
            raise ValueError("Session config and market do not match")
        if config.get("outcome_side") not in (None, "yes", "no"):
            raise ValueError("Unknown Outcome side; reconcile manually")
        side = config.get("outcome_side") or ("yes" if int(market[1:]) % 10 == 0 else "no")
        values = {k: v for k, v in config.items() if k in WorldcupStartRequest.model_fields}
        if values.get("maker_spread_pct") is None:
            values.pop("maker_spread_pct", None)
        values.update(market=market, outcome_side=side, exec_mode="maker",
                      selection_mode="manual", two_sided=True,
                      resume_exit=exit_only, resume_trading=not exit_only,
                      auto_rotate=not exit_only)
        # Resume preserves stop-loss, budgets, duration/volume objectives, the
        # original deadline and counters. A target already reached is finished,
        # not silently reset.
        old = read_json(journal_path) if Path(journal_path).exists() else {}
        old = old if isinstance(old, dict) else {}
        if old.get("session_id") and old.get("session_id") != record.get("session_id"):
            raise ValueError("Session journal identity does not match; reconcile manually")
        if old.get("symbols") and market not in old.get("symbols"):
            raise ValueError("Session journal market does not match; reconcile manually")
        if not old.get("exposure_started"):
            raise ValueError("Attribution incomplète: aucune exposition publiée par cette session; réconciliation manuelle requise")
        seed = {key: old.get(key) for key in ("exposure_started", "owned_oids", "cloids", "pending_cloids", "inventory_since", "exit_started_at")
                if key in old}
        accounting = old.get("session_accounting") or {}
        stats_path = record.get("stats_file")
        if stats_path and Path(stats_path).exists():
            stats = read_json(stats_path)
            if stats.get("session_id") == record.get("session_id") and stats.get("market") == market:
                accounting = stats
        values["session_volume_carry"] = finite(config.get("session_volume_carry", 0))+finite(accounting.get("volume_usd", 0))
        seed["rotation_pnl_carry"] = finite(config.get("rotation_pnl_carry", 0))+finite(accounting.get("net_pnl", 0))
        seed["resume_from"] = session_id
        seed["group_id"] = config.get("auto_group_id") or session_id
        seed["resume_rotation"] = (not exit_only) and bool(config.get("auto_rotate", True))
        result = self.start(WorldcupStartRequest(**values), seed=seed)
        if exit_only and getattr(self, "root", None) is not None and result.get("session_id"):
            # Persist the exit intent before the new process can publish. The
            # engine also enforces resume_exit at startup, independently.
            try:
                atomic_json(Path(self.root) / "runtime" / result["session_id"] / "drain.json",
                            {"requested_at": time.time(),
                             "reason": "Explicit resume-exit: maker exit",
                             "session_finished": True, "manual_stop": True})
            except OSError as exc:
                raise RuntimeError("Resume-exit journal unavailable: "
                                   + safe_error(exc)) from exc
        # Keep an audit link without ever claiming the old session is verified.
        try:
            journal = read_json(journal_path) if Path(journal_path).exists() else {}
            if isinstance(journal, dict):
                journal.update(
                    resumed_session_id=result["session_id"],
                    reason=("Sortie maker reprise explicitement" if exit_only
                            else "Session reprise en trading"))
                atomic_json(journal_path, journal)
        except (OSError, ValueError):
            pass
        return result
