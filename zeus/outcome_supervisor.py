"""Per-market Outcome process supervisor and durable snapshot reader."""
import logging
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid

from zeus import PROJECT_ROOT
from zeus.dashboard import localize_state
from zeus.process_control import ProcessLock, account_key, pid_alive
from zeus.safety import atomic_json, finite, safe_error, read_json


def duration(seconds):
    seconds = int(max(0, seconds))
    return f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"


class OutcomeSupervisor:
    _fallback_cache_lock = threading.Lock()

    def _cache_guard(self):
        # Tests build supervisors with __new__; production always initializes
        # the lock in __init__.
        lock = getattr(self, "_cache_lock", None)
        if lock is None:
            lock = self._cache_lock = threading.Lock()
        return lock

    @staticmethod
    def _attribution_started(record, records):
        family = {record['session_id']}
        history = [record]
        while True:
            parents = [row for row in records if row.get('resumed_session_id') in family
                       and row.get('session_id') not in family and row.get('account') == record.get('account')
                       and row.get('symbols') == record.get('symbols')]
            if not parents:
                break
            history.extend(parents)
            family.update(row['session_id'] for row in parents)
        stamps = [row.get('attribution_started') or row.get('started') for row in history]
        return min((stamp for stamp in stamps if isinstance(stamp, (float, int))
                    and math.isfinite(stamp) and stamp > 0), default=None)

    def __init__(self, manager):
        self.manager = manager
        self.procs = {}
        self.cache = {}
        self._cache_lock = threading.Lock()
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
        # Live workers which stopped heartbeating are restarted through the
        # crash path. Keep one attempt per session per minute.
        self._stuck_restarts = {}
        self._closed = threading.Event()
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

    def close(self):
        self._closed.set()
        thread = getattr(self, "_recovery_thread", None)
        if thread is not None and thread.is_alive():
            thread.join(timeout=2)
        self.rotation.close()

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
        now = time.time()
        by_id = {r.get("session_id"): r for _, r in records}
        with self._recovery_lock:
            with self.manager.lock:
                procs = dict(self.procs)
            # Archive terminal sessions first: one malformed unstarted record
            # must never starve flat confirmation for the whole tenant.
            self._archive_flat_terminals(records, by_id, procs, now)
            # The safety gate sets exposure_started before journaling any
            # CLOID/submission. A session with no exposure, no CLOIDs and no
            # owned orders therefore never reached the exchange: archive it
            # instead of blocking its market forever as "stop unconfirmed".
            for path, record in records:
                if record.get("cleanup_confirmed") or record.get("exposure_started"):
                    continue
                try:
                    started = float(record.get("started") or 0)
                except (TypeError, ValueError):
                    continue
                if now - started < 30:
                    continue  # launch may still be between journal write and Popen
                sid = record.get("session_id")
                info = procs.get(sid)
                if info is not None and info["proc"].poll() is None:
                    continue
                successor = by_id.get(record.get("resumed_session_id"))
                if successor is not None and successor.get("cleanup_confirmed") is not True:
                    continue  # parent must not hide an unresolved child exposure
                try:
                    meta = read_json(path) if Path(path).exists() else record
                except (OSError, ValueError):
                    continue
                meta = meta if isinstance(meta, dict) else record
                if (meta.get("exposure_started") or meta.get("cleanup_confirmed")
                        or any(meta.get(k) for k in ("pending_cloids", "cloids", "owned_oids"))):
                    continue
                stats = {}
                if meta.get("stats_file"):
                    try:
                        stats = read_json(meta["stats_file"])
                    except (OSError, ValueError):
                        continue  # unknown is not flat, but do not abort others
                if isinstance(stats, dict) and (
                        finite(stats.get("fills", 0)) > 0
                        or abs(finite(stats.get("position_usd", 0))) > 1e-9):
                    continue
                try:
                    if self._orphan_alive(meta) or self._worker_pid(meta):
                        continue
                except OSError:
                    continue
                updated = {**meta, "cleanup_confirmed": True, "phase": "stopped",
                           "can_start": True, "cleanup_error": "", "error": "",
                           "cleanup_note": "Aucun ordre soumis; session archivée automatiquement"}
                try:
                    atomic_json(path, updated)
                except (OSError, ValueError, TypeError) as exc:
                    logging.getLogger(__name__).warning(
                        "Outcome unstarted archive write failed %s: %s", sid, safe_error(exc))
                    continue
                record["cleanup_confirmed"] = True
                logging.getLogger(__name__).warning(
                    "Outcome archived unstarted session %s", sid)
            groups = {}
            for path, record in records:
                if not record.get("symbols"):
                    continue
                key = ((record.get("config") or {}).get("auto_group_id")
                       or record.get("session_id"))
                current = groups.get(key)
                if current is None or record.get("started", 0) > current[1].get("started", 0):
                    groups[key] = (path, record)
            for key, (path, record) in groups.items():
                sid = record.get("session_id")
                if not sid or record.get("cleanup_confirmed"):
                    continue
                info = procs.get(sid)
                if info is not None and info["proc"].poll() is None:
                    # A live process can still be stalled: its exchange recovery
                    # may keep failing, or its trading loop may stop heartbeating
                    # while the monitor keeps the stats timestamp fresh. Restart
                    # it through the normal crash/recovery path instead of
                    # leaving a silent zombie for hours.
                    stop_path = Path(record["stop_file"]) if record.get("stop_file") else None
                    stopping = bool(stop_path and stop_path.exists()) or record.get("phase") == "draining"
                    if not stopping:
                        stuck_since = (float(record.get("recovery_since") or 0)
                                       if record.get("phase") == "recovering" else 0.0)
                        loop_at = stats_ts = 0.0
                        try:
                            stats = read_json(record.get("stats_file")) if record.get("stats_file") else {}
                            if isinstance(stats, dict):
                                loop_at = float(stats.get("loop_at") or 0)
                                stats_ts = float(stats.get("ts") or 0)
                        except (OSError, ValueError, TypeError):
                            pass
                        started = float(record.get("started") or 0)
                        stalled = ((stuck_since and now - stuck_since >= 300)
                                   or (loop_at and stats_ts and now - loop_at >= 120
                                       and now - stats_ts < 10)
                                   or (not loop_at and stats_ts and started
                                       and now - started >= 300 and now - stats_ts < 10))
                        if stalled and self._restart_stuck(sid, record, info, now):
                            continue
                    continue
                if record.get("resumed_session_id") in by_id:
                    continue
                market = (record.get("symbols") or [""])[0]
                if not market:
                    continue
                if any(i.get("market") == market and i["proc"].poll() is None
                       for i in procs.values()):
                    continue
                stop_file = Path(record["stop_file"]) if record.get("stop_file") else None
                if stop_file is None:
                    continue
                manual_taker = False
                if stop_file.exists():
                    try:
                        marker = read_json(stop_file)
                    except (OSError, ValueError):
                        marker = {}
                    marker = marker if isinstance(marker, dict) else {}
                    manual_taker = (marker.get('mode') == 'cancel_and_market'
                                    and marker.get('session_id') == sid
                                    and marker.get('market') == market)
                    if not manual_taker:
                        continue  # explicit hard shutdown, never auto-trade again
                meta = read_json(path) if Path(path).exists() else record
                meta = meta if isinstance(meta, dict) else record
                # The explicit taker grant is durable in the journal even when
                # a repeated crash leaves no marker on the successor runtime.
                manual_taker = manual_taker or bool(meta.get('manual_taker_close_requested'))
                drain_file = stop_file.with_name("drain.json")
                drain = read_json(drain_file) if drain_file.exists() else {}
                drain = drain if isinstance(drain, dict) else {}
                reason = str(meta.get("stop_reason") or drain.get("reason") or "")
                reason_l = reason.lower()
                manual = bool(drain.get("manual")) or reason_l.startswith("manual stop")
                risk_exit = reason_l.startswith(("session stop loss", "session take profit",
                                                 "inventory stop"))
                rotation = reason_l.startswith("low executed")
                resume_exit = bool((meta.get("config") or {}).get("resume_exit"))
                if resume_exit or manual_taker or manual or risk_exit:
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
                if exit_only:
                    # A terminal session with no live worker can be archived as
                    # soon as the exchange proves the account is flat. This
                    # clears "Arrêt non confirmé" without waiting for a manual
                    # reconciliation click, even when a timed-out placement
                    # left client ids that orderStatus can still dismiss.
                    verify = getattr(self.manager, "_verify_session_flat", None)
                    if verify is not None:
                        last_check = float(state.get("flat_check_at", 0) or 0)
                        if now - last_check >= 60:
                            try:
                                verify(meta)
                            except Exception as exc:
                                self._recovery[sid] = {
                                    **state, "flat_check_at": now,
                                    "error": safe_error(exc)}
                            else:
                                atomic_json(path, {
                                    **meta, "cleanup_confirmed": True,
                                    "phase": "stopped", "can_start": True,
                                    "cleanup_error": "", "error": "",
                                    "pending_cloids": [],
                                    "cleanup_note": "Compte vérifié à plat; session archivée automatiquement"})
                                self._recovery.pop(sid, None)
                                logging.getLogger(__name__).warning(
                                    "Outcome confirmed flat session %s", sid)
                                continue
                if meta.get('pending_cloids') or record.get('pending_cloids'):
                    # A fresh worker cannot resolve an unknown placement; keep
                    # one stable unconfirmed row instead of spawning repeatedly.
                    self._recovery.pop(sid, None)
                    continue
                try:
                    if manual_taker:
                        self._resume(sid, exit_only=True, manual_taker=True)
                    else:
                        self._resume(sid, exit_only=exit_only)
                    self._recovery.pop(sid, None)
                    logging.getLogger(__name__).warning(
                        "Outcome auto-recovery started %s (exit_only=%s, manual_taker=%s)",
                        sid, exit_only, manual_taker)
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

    def _restart_stuck(self, sid, record, info, now):
        """Kill a live but stalled worker; the next recovery tick resumes it."""
        key = ((record.get("config") or {}).get("auto_group_id") or sid)
        previous = float(self._stuck_restarts.get(key) or 0)
        if previous and now - previous < 60:
            return False
        self._stuck_restarts[key] = now
        logging.getLogger(__name__).warning(
            "Outcome worker %s stalled (phase=%s); restarting through recovery",
            sid, record.get("phase"))
        proc = info.get("proc")
        try:
            proc.kill() if previous else proc.terminate()
        except Exception:
            pass
        return True

    def _archive_flat_terminals(self, records, by_id, procs, now):
        """Archive any dead terminal session the exchange proves flat.

        A stop file or a manual/risk exit must never keep a market labelled
        "Arrêt non confirmé" forever once there is no worker and the account
        has no residual position or order. This is read-only verification: it
        never resumes trading and never treats unknown data as flat.
        """
        verify = getattr(self.manager, "_verify_session_flat", None)
        if verify is None:
            return
        for path, record in records:
            sid = record.get("session_id")
            if not sid or record.get("cleanup_confirmed"):
                continue
            info = procs.get(sid)
            if info is not None and info["proc"].poll() is None:
                continue
            try:
                meta = read_json(path) if Path(path).exists() else {}
            except (OSError, ValueError):
                continue
            # The journal can lag the in-memory record (the supervisor adds
            # stop/rotation fields before or after publishing it), so keep both.
            meta = ({**record, **meta}
                    if isinstance(meta, dict) and meta else record)
            if meta.get("cleanup_confirmed"):
                continue
            try:
                started = float(meta.get("started") or 0)
            except (TypeError, ValueError):
                continue
            if now - started < 30:
                continue  # launch may still be between journal write and Popen
            stop_path = Path(meta["stop_file"]) if meta.get("stop_file") else None
            # drain.json lives next to the session's stop file, not next to the
            # journal (journals are flat files in the tenant directory).
            if stop_path is not None:
                drain_path = stop_path.with_name("drain.json")
            elif self.root is not None and sid:
                drain_path = self.root / "runtime" / sid / "drain.json"
            else:
                drain_path = Path(path).with_name("drain.json")
            terminal_intent = bool(
                meta.get("exit_requested") or meta.get("stop_reason")
                or meta.get("manual_stop_requested")
                or bool((meta.get("config") or {}).get("resume_exit"))
                or meta.get("cleanup_error") or meta.get("phase") == "stopped"
                or (stop_path is not None and stop_path.exists())
                or drain_path.exists())
            successor = by_id.get(meta.get("resumed_session_id"))
            if successor is not None and successor.get("cleanup_confirmed") is not True:
                continue  # the child owns this lineage until it is archived
            lineage_confirmed = bool(
                successor is not None
                and successor.get("cleanup_confirmed") is True
                and not successor.get("pending_cloids")
                and meta.get("account") and meta.get("symbols")
                and successor.get("account") == meta.get("account")
                and successor.get("symbols") == meta.get("symbols"))
            if not lineage_confirmed and not terminal_intent:
                # An interrupted trader with no stop/exit intent is auto-resumed
                # by the loop below; archiving it here would silently stop it.
                continue
            try:
                if self._orphan_alive(meta) or self._worker_pid(meta):
                    continue
            except OSError:
                continue
            state = self._recovery.get(sid) or {}
            if not lineage_confirmed:
                try:
                    last_check = float(state.get("flat_check_at", 0) or 0)
                except (TypeError, ValueError):
                    last_check = 0.0
                if now - last_check < 60:
                    continue
                try:
                    verify(meta)
                except Exception as exc:
                    detail = safe_error(exc)
                    self._recovery[sid] = {**state, "flat_check_at": now,
                                           "error": detail}
                    # Surface the real blocker on the session card. This never
                    # confirms the stop; it only records why it is still open.
                    try:
                        atomic_json(path, {**meta, "cleanup_error": detail,
                                           "can_start": False, "ts": time.time()})
                    except (OSError, ValueError, TypeError):
                        pass
                    continue
            note = ("Successeur vérifié à plat; session parente archivée"
                    if lineage_confirmed else
                    "Compte vérifié à plat; session archivée automatiquement")
            try:
                atomic_json(path, {
                    **meta, "cleanup_confirmed": True, "phase": "stopped",
                    "can_start": True, "cleanup_error": "", "error": "",
                    "pending_cloids": [],
                    "cleanup_note": note,
                })
            except (OSError, ValueError, TypeError) as exc:
                self._recovery[sid] = {**state, "error": safe_error(exc)}
                continue
            record["cleanup_confirmed"] = True
            self._recovery.pop(sid, None)
            logging.getLogger(__name__).warning(
                "Outcome archived flat terminal session %s", sid)

    @staticmethod
    def _latest_market_record(records, market):
        """Newest journal for a market; older legs are superseded by it."""
        latest = None
        for _, record in records:
            if market not in (record.get("symbols") or []):
                continue
            if latest is None or (record.get("started") or 0) >= (latest.get("started") or 0):
                latest = record
        return latest

    def start(self, request, *, rotation_group=None, seed=None):
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
        if selection is not None:
            public["two_sided"] = True
            public["rotation_pnl_carry"] = self.rotation.groups[rotation_group].get("pnl_carry", 0) if rotation_group else 0
            public['session_volume_carry'] = self.rotation.groups[rotation_group].get('volume_carry', 0) if rotation_group else 0
            public["auto_selection"] = {**selection, "selected_at": time.time()}
            # Quote at touch instead of improving a spread chosen in a draft.
            public["maker_spread_pct"] = None
        if self.root is None:
            self.temporary = tempfile.TemporaryDirectory(prefix="zeus-offline-")
            self.root = Path(self.temporary.name)
        # Resolve/read metadata above without holding the rotation/UI lock.
        # Admission, spawn and registration below are atomic against Stop.
        with ProcessLock(self.root / "admission" / f"{account_key(account)}-{market[1:]}.lock"), self.rotation.lock, self.manager.lock:
            resume_from = (seed or {}).get("resume_from")
            records = self.records()
            replaced = {resume_from} if resume_from else set()
            # Only the exact published recovery lineage may remain unresolved.
            # Another session on this account/market still blocks admission.
            while resume_from:
                ancestors = {r['session_id'] for _, r in records
                             if r.get('resumed_session_id') in replaced
                             and r.get('account') == account and r.get('symbols') == [market]}
                if ancestors.issubset(replaced):
                    break
                replaced.update(ancestors)
            latest = self._latest_market_record(records, market)
            for info in self.procs.values():
                if info["market"] == market:
                    if info["proc"].poll() is None:
                        raise RuntimeError("A bot is already running on this Outcome market")
                    if info['session_id'] in replaced:
                        continue
                    if (latest is None
                            and not self.manager._session_status(info["safety_file"]).get("cleanup_confirmed")):
                        raise RuntimeError("Previous Outcome stop not confirmed on this market")
            if (latest is not None and latest.get("session_id") not in replaced
                    and latest.get("cleanup_confirmed") is not True):
                raise RuntimeError("Arrêt non confirmé sur ce marché : vérifiez l'ancienne session dans Terminées avant de relancer")
            session_id = uuid.uuid4().hex
            if rotation_group or public.get("selection_mode") == "auto":
                public["auto_group_id"] = rotation_group or public.get("auto_group_id") or session_id
            runtime = self.root / "runtime" / session_id
            runtime.mkdir(parents=True, exist_ok=False)
            journal = self.manager._reserve_session(
                "outcome:" + market, [market], "outcome",
                ignore_session_id=(seed or {}).get("resume_from"), ignore_session_ids=replaced)
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
            for key in ("exposure_started", "owned_oids", "cloids", "pending_cloids",
                        "manual_taker_close_requested", "stop_reason", "attribution_started"):
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
                confirmed = not bool(seed and (seed.get('exposure_started') or seed.get('pending_cloids')))
                atomic_json(journal, {**record, "phase": "failed", "error": safe_error(exc),
                                     "cleanup_confirmed": confirmed, "can_start": confirmed, "pid": None})
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
            with self._cache_guard():
                self.cache[session_id] = snap
        else:
            with self._cache_guard():
                snap = self.cache.get(session_id, {})
        # Keep metadata frozen, but use the newest real lifecycle publication.
        lifecycle = snap if snap.get("ts", 0) >= (record.get("ts") or 0) else record
        phase = lifecycle.get("phase", phase)
        if lifecycle.get("exit_requested") and phase == "running":
            phase = "draining"
        if not alive and not record.get("cleanup_confirmed"):
            phase = "failed"
        confirmed = not alive and record.get("cleanup_confirmed") is True
        complete = all(key in snap for key in ("volume_usd", "net_pnl", "fees", "fills", "duration_sec"))
        # "Stale" means the worker stopped publishing, never that accounting is
        # still being completed. A warm worker whose first snapshot lacks a
        # field is quoting normally, not delayed. Before the first snapshot,
        # the launch time is the reference with a short startup grace.
        last_ts = float(snap.get("ts", 0) or 0)
        reference = last_ts or float(record.get("started", 0) or 0)
        stale = bool(alive and reference and time.time() - reference > 10)
        entry = {**record, **snap, "session_id": session_id, "market": record.get("symbols", [""])[0],
                 "config": record.get("config", {}), "started": record.get("started", snap.get("started", 0)),
                 "market_name": record.get("market_name", snap.get("market_name")),
                 "error": lifecycle.get("error", ""), "recovery_error": lifecycle.get("recovery_error", ""),
                 "cleanup_error": lifecycle.get("cleanup_error", ""),
                 "pid": info["proc"].pid if info else record.get("pid"), "running": alive,
                 "phase": phase, "stale": stale, "cleanup_confirmed": confirmed,
                 "can_start": confirmed, "accounting_complete": complete,
                 "exit_requested": bool(lifecycle.get("exit_requested")
                                        or (alive and phase == "draining")),
                 "stop_reason": lifecycle.get("stop_reason", ""),
                 "quoting_enabled": bool(alive and not stale
                                          and phase in {"running", "draining"}),
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
            with self._cache_guard():
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
            "cleanup_confirmed": all(b["cleanup_confirmed"] for b in bots),
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

    def _drain_path(self, info):
        stop_file = info.get("stop_file")
        if stop_file:
            return Path(stop_file).with_name("drain.json")
        session_id = info.get("session_id")
        if self.root is not None and session_id:
            # Legacy/partial journal without a stop_file: the current runtime
            # layout still lets us address the durable exit intent.
            return self.root / "runtime" / session_id / "drain.json"
        return None

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
        """Record an exit-only intent. Never kills the worker and never sells taker."""
        path = self._drain_path(info)
        if path is None:
            return
        try:
            if not path.exists():
                atomic_json(path, {"requested_at": time.time(), "reason": reason,
                                   "session_finished": True, "manual": True})
        except OSError as exc:
            raise RuntimeError("Outcome exit journal unavailable: " + safe_error(exc)) from exc

    def stop(self, session_id=None):
        """Record a maker-exit request. Returns ids whose request was accepted.

        A 200 here means "exit requested", not "position closed". The worker
        keeps running and publishes phase=draining until an independent flat
        account confirms the stop. Repeated clicks are idempotent.
        """
        chain_targets = self.rotation.cancel(session_id)
        with self.manager.lock:
            targets = [info for sid, info in self.procs.items()
                       if session_id is None or sid == session_id or sid in chain_targets
                       or (info.get("config") or {}).get("auto_group_id") == session_id]
        reason = ("Manual stop: maker exit" if session_id is not None
                  else "Stop all: maker exit")
        if session_id is not None and not targets:
            matching = [r for _, r in self.records()
                        if r.get("session_id") == session_id
                        or (r.get("config") or {}).get("auto_group_id") == session_id]
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
                self._request_maker_exit(record, reason)
            return [session_id]
        for info in targets:
            self._request_maker_exit(info, reason)
        if session_id is None:
            attached = {info["session_id"] for info in targets}
            for _, record in self.records():
                if record.get("session_id") not in attached and not record.get("cleanup_confirmed"):
                    self._request_maker_exit(record, reason)
        # Let a fast, already-flat exit confirm, but never kill a worker whose
        # passive sell is still waiting for a counterparty.
        deadline = time.monotonic() + 5
        pending = [info for info in targets if info["proc"].poll() is None]
        while pending and time.monotonic() < deadline:
            time.sleep(0.2)
            pending = [info for info in pending if info["proc"].poll() is None]
        return [info["session_id"] for info in targets]

    def stop_now(self, session_id):
        """Durable one-click intent; worker cancels owned orders then closes IOC."""
        children = self.rotation.cancel(session_id)
        with self.manager.lock:
            procs = dict(self.procs)
            attached = [info for sid, info in procs.items()
                if sid == session_id or sid in children
                or (info.get('config') or {}).get('auto_group_id') == session_id]
        all_records = {row['session_id']: dict(row, safety_file=str(path)) for path, row in self.records()}
        records = [row for row in all_records.values() if row.get('session_id') == session_id
            or (row.get('config') or {}).get('auto_group_id') == session_id]
        targets = {row['session_id']: row for row in records if not row.get('cleanup_confirmed')}
        targets.update({row['session_id']: row for row in attached})
        for row in list(targets.values()):
            visited = set()
            while row.get('resumed_session_id') in all_records:
                child = all_records[row['resumed_session_id']]
                if child['session_id'] in visited or child.get('account') != row.get('account') or child.get('symbols') != row.get('symbols'):
                    break
                visited.add(child['session_id'])
                if not child.get('cleanup_confirmed'):
                    targets[child['session_id']] = procs.get(child['session_id'], child)
                row = child
        if not targets:
            if records and all(row.get('cleanup_confirmed') for row in records):
                return [session_id]
            raise RuntimeError('Session unavailable; stop cannot be attributed')
        requests = []
        for sid, info in targets.items():
            stop_path = info.get('stop_file')
            market = info.get('market') or (info.get('symbols') or [''])[0]
            if not stop_path or not market:
                raise RuntimeError('Incomplete session stop attribution')
            started = self._attribution_started(all_records.get(sid, info), all_records.values())
            requests.append((sid, stop_path, market, started))
        for sid, stop_path, market, started in requests:
            atomic_json(Path(stop_path), dict(mode='cancel_and_market', requested_at=time.time(),
                                             session_id=sid, market=market, attribution_started=started))
        for sid, info in targets.items():
            # One slow market must not delay the other members of a bot group.
            threading.Thread(target=self._finish_manual_stop, args=(sid, info),
                             name='outcome-manual-close', daemon=True).start()
        return list(targets)

    @staticmethod
    def _worker_pid(record):
        """A published worker PID, never the workspace process or a foreign one."""
        if os.name == 'nt':
            return None  # No safe command-line attestation; use tracked Popen only.
        pid = record.get('pid')
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1 or pid == os.getpid():
            return None
        if not pid_alive(pid):
            return None
        try:
            raw = Path('/proc/%d/cmdline' % pid).read_bytes().replace(b'\0', b' ').decode('utf-8', 'ignore')
        except OSError:
            return None
        sid = str(record.get('session_id') or '')
        if 'zeus.bots.outcome_worker' not in raw or (sid and sid not in raw):
            return None
        return pid

    @staticmethod
    def _terminate_worker(pid):
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            return
        deadline = time.monotonic()+2
        while time.monotonic() < deadline and pid_alive(pid):
            time.sleep(.1)
        if pid_alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                return
            deadline = time.monotonic()+2
            while time.monotonic() < deadline and pid_alive(pid):
                time.sleep(.1)

    def _finish_manual_stop(self, sid, info):
        proc = info.get('proc')
        try:
            with self._recovery_lock:
                match = next(((path, row) for path, row in self.records() if row['session_id'] == sid), None)
                latest = read_json(match[0]) if match else {}
                if latest.get('cleanup_confirmed'):
                    return
                successor = latest.get('resumed_session_id')
                if not successor and proc is not None and proc.poll() is None:
                    # A healthy monitor sees the marker in 250 ms. Anything
                    # slower is a stuck worker: stop it now and let the
                    # exit-only worker reconcile the account.
                    deadline = time.monotonic()+2
                    while time.monotonic() < deadline and not self._closed.wait(.2):
                        if proc.poll() is not None:
                            break
                    match = next(((path, row) for path, row in self.records()
                                  if row['session_id'] == sid), None)
                    latest = read_json(match[0]) if match else latest
                    if latest.get('cleanup_confirmed'):
                        return
                    successor = latest.get('resumed_session_id')
                    if proc.poll() is None:
                        try:
                            proc.terminate()
                            wait = getattr(proc, 'wait', None)
                            if callable(wait):
                                try:
                                    wait(timeout=2)
                                except Exception:
                                    pass
                        except Exception:
                            pass
                        if proc.poll() is None:
                            try:
                                proc.kill()
                                wait = getattr(proc, 'wait', None)
                                if callable(wait):
                                    try:
                                        wait(timeout=2)
                                    except Exception:
                                        pass
                            except Exception:
                                pass
                        match = next(((path, row) for path, row in self.records()
                                      if row['session_id'] == sid), None)
                        latest = read_json(match[0]) if match else latest
                        if latest.get('cleanup_confirmed'):
                            return
                        successor = latest.get('resumed_session_id')
                if not successor and proc is None:
                    # An untracked orphan after a supervisor restart: verify
                    # its exact command line before terminating the stale PID.
                    orphan_pid = self._worker_pid(latest or info)
                    if orphan_pid:
                        self._terminate_worker(orphan_pid)
                if not successor:
                    if (proc is not None and proc.poll() is None) or self._orphan_alive(info):
                        return  # its monitor observes the durable request
                    successor = self._resume(sid, exit_only=True, manual_taker=True)['session_id']
            # The exit-only successor inherits every owned OID/CLOID.
            # Delegate confirmation only for this exact account/market.
            deadline = time.monotonic()+30
            while time.monotonic() < deadline and not self._closed.wait(.2):
                rows = {row['session_id']: (path, row) for path, row in self.records()}
                parent, child = rows.get(sid), rows.get(successor)
                if not parent or not child:
                    return
                path, old = parent
                _, confirmed = child
                if confirmed.get('cleanup_confirmed') and not confirmed.get('pending_cloids'):
                    if old.get('account') != confirmed.get('account') or old.get('symbols') != confirmed.get('symbols'):
                        raise RuntimeError('Close confirmation attribution mismatch')
                    old.update(cleanup_confirmed=True, pending_cloids=[], phase='stopped',
                               cleanup_error='', can_start=True)
                    atomic_json(path, old)
                    return
        except Exception as exc:
            path = info.get('safety_file')
            if path:
                try:
                    row = read_json(path)
                    if not row.get('cleanup_confirmed'):
                        row.update(cleanup_error=safe_error(exc), cleanup_confirmed=False)
                        atomic_json(path, row)
                except (OSError, ValueError):
                    pass

    def resume_exit(self, session_id):
        """Start an exit-only worker for a stopped, unconfirmed Outcome session."""
        return self._resume(session_id, exit_only=True)

    def resume_trading(self, session_id):
        """Resume normal bid+ask trading on the same market with attribution."""
        return self._resume(session_id, exit_only=False)

    def _resume(self, session_id, *, exit_only, manual_taker=False):
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
        if not market:
            raise ValueError("Session has no Outcome market")
        with self.manager.lock:
            for info in self.procs.values():
                if info.get("market") == market and info["proc"].poll() is None:
                    raise RuntimeError("A worker is already active on this market")
            previous = self.procs.get(record.get("resumed_session_id"))
            if previous is not None and previous["proc"].poll() is None:
                raise RuntimeError("A resume is already running for this session")
        from zeus.server import WorldcupStartRequest
        config = dict(record.get("config") or {})
        side = config.get("outcome_side") or ("yes" if int(market[1:]) % 10 == 0 else "no")
        values = {k: v for k, v in config.items() if k in WorldcupStartRequest.model_fields}
        if values.get("maker_spread_pct") is None:
            values.pop("maker_spread_pct", None)
        values.update(market=market, outcome_side=side, exec_mode="maker",
                      selection_mode="manual", two_sided=True,
                      resume_exit=exit_only, resume_trading=not exit_only,
                      auto_rotate=not exit_only)
        if not exit_only:
            # A resumed trading session must not immediately finish on an old
            # duration/volume target; the user can stop it explicitly.
            values.update(session_expires_at=None, target_volume_usd=None,
                          session_volume_carry=0)
            # Sessions launched during the short-window experiment must return
            # to the standard dwell when recovered after this upgrade.
            values["auto_rotation_after_sec"] = max(
                180.0, float(values.get("auto_rotation_after_sec") or 180.0))
        old = read_json(journal_path) if Path(journal_path).exists() else {}
        old = old if isinstance(old, dict) else {}
        if not old.get("exposure_started"):
            raise ValueError("Attribution incomplète: aucune exposition publiée par cette session; réconciliation manuelle requise")
        seed = {key: old.get(key) for key in ("exposure_started", "owned_oids", "cloids", "pending_cloids")
                if key in old}
        seed["resume_from"] = session_id
        seed['attribution_started'] = self._attribution_started(old, [row for _, row in self.records()])
        seed["group_id"] = config.get("auto_group_id") or session_id
        seed["resume_rotation"] = (not exit_only) and bool(config.get("auto_rotate", True))
        if manual_taker:
            if not exit_only:
                raise ValueError('Manual taker recovery must be exit-only')
            seed['manual_taker_close_requested'] = True
            seed['stop_reason'] = 'Manual stop: cancel and market close'
        result = self.start(WorldcupStartRequest(**values), seed=seed)
        # Keep an audit link without ever claiming the old session is verified.
        try:
            journal = read_json(journal_path) if Path(journal_path).exists() else {}
            if isinstance(journal, dict):
                journal.update(
                    resumed_session_id=result["session_id"],
                    reason=("Arrêt et clôture au marché demandés" if manual_taker else
                            "Sortie maker reprise explicitement" if exit_only
                            else "Session reprise en trading"))
                atomic_json(journal_path, journal)
        except (OSError, ValueError):
            pass
        return result
