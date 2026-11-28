"""Server-owned Auto rotation. Drain passively, verify flat, then reselect.

No order signing here. Stop cancels the complete rotation chain; a stopped
server never resumes old groups implicitly. Public scanner is shared by all.
"""
from collections import deque
import logging
import math
import os
from pathlib import Path
import threading
import time

from zeus.safety import atomic_json, read_json, safe_error
from zeus.outcome_flow import no_fill_grace, candidate_rank


class OutcomeRotation:
    def __init__(self, owner):
        self.owner = owner
        self.lock = threading.RLock()
        self.groups = {}
        self.closed = threading.Event()
        self.thread = None
        self.catalog_refreshed = 0.0
        self.catalog_thread = None
        self.catalog_retry_at = 0.0
        self.last_error = (0.0, "")

    def register(self, session, config, group_id=None):
        if not config.get("auto_rotate", True):
            return
        if config.get("selection_mode") != "auto" and not config.get("resume_rotation"):
            return
        with self.lock:
            if self.closed.is_set():
                raise RuntimeError("Auto rotation controller closed")
            group_id = group_id or session["session_id"]
            group = self.groups.get(group_id)
            if group is None:
                group = self.groups[group_id] = {"id": group_id, "config": dict(config),
                    "members": set(), "cancelled": False, "rotations": 0, "cooldown": {},
                    "pnl_carry": 0.0, "volume_carry": 0.0, "accounted": set()}
            now = time.time()
            selection = config.get("auto_selection") or {}
            group.update(current=session["session_id"], market=session.get("market"),
                         started=now, state="observing",
                         samples=deque([(now, 0.0)], maxlen=2000), reason="", retry_at=0.0,
                         current_selection=selection, fills_seen=0,
                         last_leg_fill_at=now, promote_check=False,
                         inventory_blocked_since=0.0,
                         last_rank_check=0.0,
                         market_rank=selection.get("rank"))
            group["members"].add(session["session_id"])
            if self.thread is None or not self.thread.is_alive():
                self.thread = threading.Thread(target=self._loop, name="outcome-auto-rotation", daemon=True)
                self.thread.start()

    def cancel(self, session_id=None):
        """Returns current children for an old card's Stop, under the same lock
        used to admit replacements. A launch cannot slip behind manual Stop.
        """
        # One lock acquisition: register() must not resize the dict while this
        # cancels a group, and a replacement cannot slip behind manual Stop.
        with self.lock:
            current = set()
            for group in self.groups.values():
                if session_id is None or session_id in group["members"] or session_id == group["id"]:
                    group["cancelled"] = True
                    current.add(group["current"])
            return current

    def assert_admission(self, group_id):
        with self.lock:
            closed = self.closed.is_set()
            group = self.groups.get(group_id) if group_id is not None else None
            cancelled = bool(group and group.get("cancelled"))
            config = dict(group.get("config", {})) if group else {}
        if closed or cancelled:
            raise RuntimeError("Auto rotation cancelled by Stop")
        if group_id is not None:
            from zeus.session_targets import finish_reason
            if finish_reason(config, None, time.time()):
                raise RuntimeError('Session duration reached')

    def candidate_allowed(self, group_id, market, now=None):
        if group_id is None:
            return True
        with self.lock:
            group = self.groups.get(group_id, {})
            return group.get("cooldown", {}).get(market, 0) <= (
                time.time() if now is None else now)

    @staticmethod
    def rotation_inventory_reason(current):
        """Low throughput may reselect a flat worker, never liquidate inventory."""
        position = current.get('position_usd')
        if (current.get('position_known') is not True or isinstance(position, bool)
                or not isinstance(position, (int, float)) or not math.isfinite(position)):
            return "Rotation: vérification de l'inventaire"
        qty = current.get('position_qty')
        unsellable = current.get('unsellable_qty')
        known_qty = (isinstance(qty, (int, float)) and not isinstance(qty, bool)
                     and math.isfinite(qty) and qty >= 0)
        if known_qty and qty > 1e-9:
            if (isinstance(unsellable, (int, float)) and not isinstance(unsellable, bool)
                    and math.isfinite(unsellable) and qty - unsellable <= 1e-9):
                return ''  # only a provably unsellable residual: rotation may proceed
            if current.get('book_price_known') is False:
                return "Rotation: vérification du prix"
            if abs(position) < 1e-9:
                return "Rotation: vérification du prix"
        if abs(position) > 1e-9:
            if known_qty and qty > 1e-9 and abs(position) < 10.0:
                return ''  # venue minimum: this residual cannot be sold
            return "Rotation différée: vente de l'inventaire en cours"
        return ''

    @staticmethod
    def _inventory_message(group, reason, now):
        since = float(group.get("inventory_blocked_since") or 0)
        if not since:
            since = group["inventory_blocked_since"] = now
        minutes = int(max(0, now-since) // 60)
        return reason + ((" depuis %d min" % minutes) if minutes >= 5 else '')

    def describe(self, session_id):
        with self.lock:
            for group in self.groups.values():
                if session_id in group["members"]:
                    now = time.time()
                    last_fill = float(group.get("last_leg_fill_at") or 0)
                    blocked = float(group.get("inventory_blocked_since") or 0)
                    return {"auto_group_id": group["id"], "auto_rotations": group["rotations"],
                            "auto_rotation_state": group["state"], "auto_rotation_reason": group["reason"],
                            "auto_fill_usd_per_min": group.get("fill_rate", 0),
                            "auto_market_rank": group.get("market_rank"),
                            "auto_best_rank": group.get("best_rank"),
                            "auto_best_market": group.get("best_market"),
                            "auto_last_leg_fill_at": last_fill or None,
                            "auto_no_fill_sec": max(0, now-last_fill) if last_fill else None,
                            "auto_inventory_blocked_sec": max(0, now-blocked) if blocked else 0,
                            "auto_evaluate_after_sec": group.get("evaluate_after_sec"),
                            "auto_evaluate_in_sec": max(0, group.get("evaluate_at", time.time())-time.time())}
        return {}

    def display_intents(self):
        with self.lock:
            return [{"group_id": group["id"], "current": group["current"], "state": group["state"],
                     "cancelled": group["cancelled"], "reason": group["reason"]}
                    for group in self.groups.values()]

    def _loop(self):
        while not self.closed.wait(2):
            try:
                with self.lock:
                    active = any(not g["cancelled"] for g in self.groups.values())
                if not active:
                    continue
                self.request_catalog_refresh()
                self.tick()
            except Exception as exc:
                with self.lock:
                    self.last_error = (time.time(), safe_error(exc))
                logging.getLogger(__name__).exception("Auto rotation tick failed")
            else:
                # Never leave a transient controller error on a card forever.
                with self.lock:
                    for group in self.groups.values():
                        if (not group["cancelled"]
                                and str(group.get("reason", "")).startswith("Auto waiting: ")):
                            group["reason"] = ""

    def request_catalog_refresh(self):
        # Metadata reads may take tens of seconds. They must not stop lifecycle
        # checks, drain completion or Stop admission for existing sessions.
        with self.lock:
            if (self.closed.is_set() or time.time()-self.catalog_refreshed < 60
                    or time.time() < self.catalog_retry_at
                    or self.catalog_thread and self.catalog_thread.is_alive()):
                return
            self.catalog_retry_at = time.time()+10
            def refresh():
                try:
                    self.refresh_catalog()
                except Exception:
                    pass  # Existing catalog still expires normally in the scanner.
            self.catalog_thread = threading.Thread(target=refresh, name="outcome-auto-catalog", daemon=True)
            self.catalog_thread.start()

    def refresh_catalog(self):
        # Dedicated background thread: network reads cannot occupy the API loop.
        # Never mark a failed fetch as a fresh catalog.
        if time.time()-self.catalog_refreshed < 60:
            return
        from zeus import outcomes
        from zeus.outcome_flow import scanner
        meta, templates, mids = outcomes._fetch_payloads()
        if self.closed.is_set():
            return
        scanner.configure(outcomes._build_rows(meta, templates, mids))
        self.catalog_refreshed = time.time()

    @staticmethod
    def _account_leg(group, session_id, current):
        """Account a finished leg once, even across a crash/relaunch."""
        if session_id in group.setdefault("accounted", set()):
            return
        group["pnl_carry"] = group.get("pnl_carry", 0) + float(current.get("net_pnl") or 0)
        group["volume_carry"] = group.get("volume_carry", 0) + float(current.get("volume_usd") or 0)
        group["accounted"].add(session_id)

    def _current_bot(self, group_id, status):
        """Follow a recovery successor so the logical group never gets lost."""
        with self.lock:
            group = self.groups.get(group_id)
            if group is None:
                return None
            current_id = group.get("current")
            members = set(group.get("members", ()))
            market = group.get("market")
        bots = status.get("bots") or []
        current = next((b for b in bots if b.get("session_id") == current_id), None)
        if current is None and market:
            current = next((b for b in bots if b.get("session_id") in members
                            and b.get("market") == market), None)
        if current is not None and current.get("session_id") != current_id:
            with self.lock:
                group = self.groups.get(group_id)
                if (group is not None and not group.get("cancelled")
                        and group.get("current") == current_id):
                    group.setdefault("members", set()).add(current["session_id"])
                    group["current"] = current["session_id"]
        return current

    @staticmethod
    def _exit_context(current, info):
        """Classify a finished worker: manual/risk exits stay terminal."""
        current = current or {}
        reason = str(current.get("stop_reason") or "")
        config = current.get("config") or {}
        manual = False
        if info and info.get("stop_file"):
            path = Path(info["stop_file"]).with_name("drain.json")
            try:
                marker = read_json(path) if path.exists() else {}
            except (OSError, ValueError):
                marker = {}
            if isinstance(marker, dict):
                manual = bool(marker.get("manual"))
                reason = reason or str(marker.get("reason") or "")
        lowered = reason.lower()
        manual = (manual or lowered.startswith(("manual stop", "stop all"))
                  or bool(current.get("manual_taker_close_requested")))
        risk = lowered.startswith(("session stop loss", "session take profit",
                                   "inventory stop", "inventory close"))
        finished = lowered.startswith(("session duration", "session volume"))
        # A supervisor-recovered exit-only leg must stay terminal even when
        # its new journal has not republished the old stop reason yet.
        recovered_exit = bool(config.get("resume_exit")) and not config.get("resume_trading")
        return manual, risk or finished or recovered_exit, reason

    def tick(self, now=None):
        now = time.time() if now is None else now
        with self.lock:
            group_ids = [key for key, group in self.groups.items()
                         if not group["cancelled"]]
        for group_id in group_ids:
            try:
                self._tick_group(group_id, now)
            except Exception as exc:
                # One logical session must not starve the others.
                with self.lock:
                    self.last_error = (time.time(), safe_error(exc))
                    group = self.groups.get(group_id)
                    if group is not None and not group.get("cancelled"):
                        group["reason"] = "Auto: reprise après erreur interne"
                logging.getLogger(__name__).exception(
                    "Auto rotation group %s failed", group_id)

    def _tick_group(self, group_id, now):
        from zeus.outcome_flow import scanner
        with self.lock:
            group = self.groups.get(group_id)
            if (group is None or group["cancelled"] or self.closed.is_set()
                    or now < group["retry_at"]):
                return
            if str(group.get("reason", "")).startswith("Auto: reprise après erreur interne"):
                group["reason"] = ""
            config = dict(group["config"])
            state = group["state"]
        status = self.owner.status()
        current = self._current_bot(group_id, status)
        if current is None:
            with self.lock:
                group = self.groups.get(group_id)
                if group is not None and not group.get("cancelled"):
                    group["reason"] = "Auto: vérification de la session"
            return
        current_id = current["session_id"]
        info = (getattr(self.owner, "procs", {}) or {}).get(current_id)
        manual, terminal, drain_reason = self._exit_context(current, info)

        from zeus.session_targets import finish_reason
        with self.lock:
            group = self.groups.get(group_id)
            if group is None or group["cancelled"] or current_id != group["current"]:
                return
            reason = finish_reason(
                {**config, "session_volume_carry": group.get("volume_carry", 0)},
                current.get("volume_usd"), now)
        if reason or state == "finishing":
            with self.lock:
                group = self.groups.get(group_id)
                if group is None or group["cancelled"] or current_id != group["current"]:
                    return
                if not current["running"] and current.get("cleanup_confirmed"):
                    group.update(cancelled=True, state="stopped", reason="")
                    return
                group.update(state="finishing", reason=reason or group["reason"])
                if info and info.get("stop_file"):
                    path = Path(info["stop_file"]).with_name("drain.json")
                    if not path.exists():
                        atomic_json(path, {"requested_at": now,
                                           "reason": group["reason"],
                                           "session_finished": True})
            return

        with self.lock:
            group = self.groups.get(group_id)
            if group is None or group["cancelled"] or current_id != group["current"]:
                return
            state = group["state"]
            if state == "blocked":
                # Old stop confirmation can arrive long after the drain.
                if current["running"]:
                    # The supervisor recovered the interrupted worker. Keep
                    # observing it instead of draining a healthy session.
                    volume = float(current.get("volume_usd") or 0)
                    group.update(state="observing", retry_at=0.0, reason="",
                                 started=now, samples=deque([(now, volume)], maxlen=2000))
                    state = "observing"
                elif current.get("cleanup_confirmed"):
                    group.update(state="draining", retry_at=0.0,
                                 reason="Rotation: flat, reselecting")
                    state = "draining"
                else:
                    group["reason"] = "Rotation blocked: old stop not confirmed"
                    return
            if state == "launching":
                # A start() which returned without register(), or a restart
                # during launch, must not strand the group forever.
                if current["running"] or current.get("cleanup_confirmed"):
                    volume = float(current.get("volume_usd") or 0)
                    group.update(state="observing", retry_at=0.0, reason="",
                                 started=now, samples=deque([(now, volume)], maxlen=2000))
                    state = "observing"
                else:
                    group.update(state="draining", retry_at=now,
                                 reason="Rotation: reprise après lancement non confirmé")
                    state = "draining"
            if state == "draining":
                if current["running"]:
                    group["reason"] = "Rotation: cancelling bids / selling remaining inventory"
                    return
                if not current.get("cleanup_confirmed"):
                    group.update(state="blocked",
                                 reason="Rotation blocked: old stop not confirmed")
                    return
                self._account_leg(group, current_id, current)
            elif not current["running"]:
                # Unexpected worker death: keep the logical group alive. A
                # manual/risk exit is terminal; a crash is retried or recovered.
                if manual or terminal:
                    group.update(state="finishing",
                                 reason=drain_reason or "Arrêt en cours; sortie maker")
                    return
                if current.get("cleanup_confirmed"):
                    self._account_leg(group, current_id, current)
                    market = current.get("market")
                    if market:
                        cooldowns = group.setdefault("cooldown", {})
                        cooldowns[market] = max(
                            cooldowns.get(market, 0),
                            now+config.get("auto_market_cooldown_sec", 300))
                    failures = int(group.get("resume_failures", 0)) + 1
                    group["resume_failures"] = failures
                    if failures > 1:
                        # A worker which dies immediately after spawning must
                        # not create a tight relaunch loop across markets.
                        group.update(state="draining",
                                     retry_at=now+min(60, 5*2**min(failures, 4)),
                                     reason="Rotation: reprise après interruption")
                        return
                    group.update(state="draining", retry_at=now,
                                 reason="Rotation: reprise après interruption")
                    state = "draining"
                else:
                    group.update(state="blocked",
                                 reason="Rotation blocked: interruption, arrêt non confirmé")
                    return
            elif current.get("stale"):
                group["reason"] = "Session muette: reprise en cours"
                return
            elif current.get("phase") == "recovering":
                detail = safe_error(current.get("recovery_error") or "")
                group["reason"] = ("Récupération exchange en cours"
                                   + (": " + detail if detail else ""))
                group["last_rank_check"] = now
                return
            elif current.get("phase") != "running":
                group["reason"] = ("Rotation: session en démarrage"
                                   if current.get("phase") == "connecting"
                                   else "Rotation: session en cours d'arrêt")
                return
            if state not in ("observing", "draining"):
                return

            window = float(config.get("auto_rotation_after_sec", 60))
            if state == "observing":
                if now - group["started"] > 30:
                    # A leg which survives its startup proves the relaunch was
                    # healthy; a later crash starts a fresh backoff window.
                    group["resume_failures"] = 0
                volume = float(current["volume_usd"])
                group["samples"].append((now, volume))
                while len(group["samples"]) > 1 and group["samples"][1][0] <= now-window:
                    group["samples"].popleft()
                first_time, first_volume = group["samples"][0]
                elapsed = now-first_time
                group["fill_rate"] = max(0, volume-first_volume)*60/max(1, elapsed)
                # The strongest signal that a selected queue is not executing
                # is a leg with no fill at all. Track it separately from the
                # carried volume so a previous leg can never mask it.
                leg_fills = int(current.get("fills", 0) or 0)
                if leg_fills > int(group.get("fills_seen", 0) or 0):
                    group["last_leg_fill_at"] = now
                group["fills_seen"] = leg_fills
                zero_fills = leg_fills <= 0
                deadline = (no_fill_grace(config, group.get("current_selection"))
                            if zero_fills else window)
                group.update(evaluate_after_sec=deadline,
                             evaluate_at=group["started"]+deadline)
                promote = False
                if zero_fills:
                    # Do not wait the whole rotation window on a market which
                    # has not filled once. As soon as the short no-fill grace
                    # expires, compare with the best market that is actually
                    # free and move there without waiting for a fill.
                    if now-group["started"] < deadline:
                        return
                    promote = True
                else:
                    if now-group["started"] < window or elapsed < window-10:
                        return
                    if current.get("execution_state") == "Insufficient USDC":
                        group["reason"] = "Available USDC below full order size"
                        return
                    if (current.get("last_fill_at")
                            and now-current["last_fill_at"] < min(30, window/3)):
                        group["reason"] = ""
                        return
                    threshold = config.get("auto_min_fill_usd_per_min")
                    threshold = config["order_size_usd"] if threshold is None else float(threshold)
                    if group["fill_rate"] >= threshold:
                        # A market that is actually producing fills keeps its
                        # queue. Rotation is only for stalled legs, not for
                        # chasing a slightly better rank and losing fills.
                        group["reason"] = ""
                        return
                    # Low executed throughput: reselect the best free market
                    # even when it is only one rank better.
                    promote = True
                group["promote_check"] = promote
                inventory_reason = self.rotation_inventory_reason(current)
                if inventory_reason:
                    # Request the exit first: the worker will cancel its bids
                    # and release USDC before the next market is opened.
                    group["reason"] = self._inventory_message(group, inventory_reason, now)
                    if inventory_reason.startswith("Rotation: "):
                        return  # unknown position/price: never authorize a move
                else:
                    group["inventory_blocked_since"] = 0.0

        context = getattr(getattr(self.owner, 'manager', None), 'account_context', None)
        position_before_scan = current.get('position_usd')
        account = context.account if context else os.getenv('WALLET_ADDRESS_OUTCOME', '').strip()
        snapshot = scanner.snapshot(config["outcome_side"], config["order_size_usd"],
                                    config.get("auto_max_spread_bps", 10),
                                    account=account)
        if not snapshot.get("ready") or now-float(snapshot.get("updated_at", now)) > 20:
            with self.lock:
                group = self.groups.get(group_id)
                if group is not None and not group.get("cancelled"):
                    group["promote_check"] = False
                    group["last_rank_check"] = now
            return
        busy = {b["market"] for b in status.get("bots", [])
                if b["running"] or not b.get("cleanup_confirmed")}
        with self.lock:
            group = self.groups.get(group_id)
            if group is None:
                return
            results = dict(group.get("market_results", {}))
        ranked = sorted(snapshot["candidates"],
                        key=lambda r: candidate_rank(r, results, now))
        top = next((r for r in ranked if r["market"] not in busy
                    and r["market"] != current["market"]
                    and self.candidate_allowed(group_id, r["market"], now)), None)
        current_candidate = next(
            (r for r in snapshot["candidates"] if r["market"] == current["market"]),
            None)
        # Scanner reads can take time. A fill during that read must not turn a
        # low-volume rotation into an involuntary inventory exit.
        latest = next((b for b in self.owner.status().get('bots', [])
                       if b['session_id'] == current_id), None)
        with self.lock:
            group = self.groups.get(group_id)
            if (group is None or group["cancelled"] or current_id != group["current"]
                    or self.closed.is_set()):
                return
            if group["state"] == "observing":
                if (not latest or not latest.get('running')
                        or latest.get('phase') != 'running' or latest.get('stale')):
                    group["promote_check"] = False
                    return
                promote = bool(group.get("promote_check"))
                group["promote_check"] = False
                inventory_reason = self.rotation_inventory_reason(latest)
                if inventory_reason:
                    group["last_rank_check"] = now
                    group['reason'] = self._inventory_message(group, inventory_reason, now)
                    if inventory_reason.startswith("Rotation: "):
                        return  # unknown position/price: never authorize a move
                else:
                    group["inventory_blocked_since"] = 0.0
                latest_pos, current_pos = latest.get('position_usd'), position_before_scan
                if (isinstance(latest_pos, (int, float)) and not isinstance(latest_pos, bool)
                        and isinstance(current_pos, (int, float)) and not isinstance(current_pos, bool)
                        and math.isfinite(latest_pos) and math.isfinite(current_pos)
                        and latest_pos > current_pos + 1e-9):
                    group['reason'] = "Rotation annulée: fill pendant le scan"
                    return
                # Publish what this leg sees so the card can show
                # "#current -> #best" instead of sitting silently.
                group["market_rank"] = (current_candidate or {}).get("rank")
                group["best_rank"] = (top or {}).get("rank")
                group["best_market"] = (top or {}).get("market")
                if top is None:
                    group["last_rank_check"] = now
                    blocked = next((r for r in ranked if r["market"] != current["market"]),
                                   None)
                    if blocked is not None:
                        group["reason"] = ("Rotation: marché #%s indisponible, attente "
                                           "d'un marché libre" % (blocked.get("rank") or "?"))
                    elif group["market_rank"] == 1:
                        group["reason"] = "Rotation: déjà sur le meilleur marché disponible"
                    else:
                        group["reason"] = "Low fills: waiting for another eligible $/min market"
                    return
                if not promote:
                    return
                current_rank = (current_candidate or {}).get("rank")
                top_rank = top.get("rank")
                better = (current_rank is None or top_rank is None
                          or int(top_rank) < int(current_rank))
                group["last_rank_check"] = now
                if not better:
                    # Never rotate to a lower-volume market: the current
                    # one already is the best free market.
                    group["reason"] = ("Rotation: déjà sur le meilleur marché "
                                       "disponible (#%s)" % current_rank)
                    return
                group["reason"] = ("Rotation: meilleur marché disponible "
                                   "(#%s → #%s)" % (
                                       current_rank if current_rank else "-",
                                       top_rank if top_rank else "?"))
                info = (getattr(self.owner, "procs", {}) or {}).get(current_id)
                if info is None:
                    return
                atomic_json(Path(info["stop_file"]).with_name("drain.json"), {
                    "requested_at": now, "reason": "low executed $/min",
                    "next_market": top["market"]})
                group.update(state="draining",
                             reason=inventory_reason or group["reason"])
                previous = group.setdefault('market_results', {}).get(current['market'], {})
                failures = (previous.get('zero_fill_attempts', 0)+1
                            if float(current.get('volume_usd', 0)) <= 0 else 0)
                group['market_results'][current['market']] = dict(
                    zero_fill_attempts=failures, updated_at=now,
                    executed_usd_per_min=float(current.get('volume_usd', 0))*60
                    / max(1, now-group['started']))
                cooldown = config.get("auto_market_cooldown_sec", 300)*min(8, 2**failures)
                group.setdefault("cooldown", {})[current["market"]] = now+cooldown
                return
            # Old worker is dead and exchange cleanup is confirmed. start()
            # reselects from a fresh scanner snapshot and preserves budgets.
            if top is None:
                # Never end the logical session on its own. Wait for the next
                # eligible market; the user keeps the Stop control.
                group["reason"] = "Rotation: flat, waiting for eligible flow"
                return
            group["wait_since"] = 0.0
            group["launch_failures"] = 0
            from zeus.server import WorldcupStartRequest
            values = {k: v for k, v in config.items() if k in WorldcupStartRequest.model_fields}
            if values.get("maker_spread_pct") is None:
                values.pop("maker_spread_pct", None)
            values.update(market=top["market"], two_sided=True)
            # Validate before advertising a launch state so a bad field can
            # never leave the group stuck in "launching".
            request = WorldcupStartRequest(**values)
            cooldowns = group.setdefault("cooldown", {})
            cooldowns[current["market"]] = max(
                cooldowns.get(current["market"], 0),
                now+config.get("auto_market_cooldown_sec", 300))
            group.update(state="launching", retry_at=now+15,
                         reason="Rotation: opening " + top["market"])
        # Metadata resolution can block. Stop remains immediate; the
        # supervisor rechecks cancellation under the final admission lock.
        try:
            self.owner.start(request, rotation_group=group_id)
            with self.lock:
                group = self.groups.get(group_id)
                if group is not None and not group["cancelled"]:
                    group["rotations"] += 1
                    group["launch_failures"] = 0
                    group.update(state="observing", retry_at=0.0, reason="")
        except Exception as exc:
            with self.lock:
                group = self.groups.get(group_id)
                if group is not None and not group['cancelled']:
                    failures = group.get("launch_failures", 0) + 1
                    group["launch_failures"] = failures
                    # Retry with a bounded backoff; never end the session
                    # on a transient launch failure.
                    group.update(state="draining",
                                 retry_at=now+min(60, 5*2**min(failures, 4)),
                                 reason="Rotation waiting: " + safe_error(exc))

    def close(self):
        self.closed.set()
        self.cancel()
        if self.thread:
            self.thread.join(timeout=2)
        if self.catalog_thread:
            self.catalog_thread.join(timeout=2)
