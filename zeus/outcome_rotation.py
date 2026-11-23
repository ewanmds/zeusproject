"""Server-owned Auto rotation. Drain passively, verify flat, then reselect.

No order signing here. Stop cancels the complete rotation chain; a stopped
server never resumes old groups implicitly. Public scanner is shared by all.
"""
from collections import deque
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
            group.update(current=session["session_id"], started=now, state="observing",
                         samples=deque([(now, 0.0)], maxlen=2000), reason="", retry_at=0.0,
                         current_selection=config.get("auto_selection", {}))
            group["members"].add(session["session_id"])
            if self.thread is None or not self.thread.is_alive():
                self.thread = threading.Thread(target=self._loop, name="outcome-auto-rotation", daemon=True)
                self.thread.start()

    def cancel(self, session_id=None):
        """Returns current children for an old card's Stop, under the same lock
        used to admit replacements. A launch cannot slip behind manual Stop.
        """
        # Mark cancellation under the same lock used by every replacement
        # admission check. tick() rechecks this under the lock immediately
        # before start(), and start() checks again under the admission lock.
        with self.lock:
            current = set()
            for group in self.groups.values():
                if session_id is None or session_id in group["members"] or session_id == group["id"]:
                    group["cancelled"] = True
                    if group.get("current"):
                        current.add(group["current"])
            return current

    def assert_admission(self, group_id):
        if self.closed.is_set() or group_id is not None and self.groups[group_id]["cancelled"]:
            raise RuntimeError("Auto rotation cancelled by Stop")
        if group_id is not None:
            from zeus.session_targets import finish_reason
            if finish_reason(self.groups[group_id]['config'], None, time.time()):
                raise RuntimeError('Session duration reached')

    def candidate_allowed(self, group_id, market, now=None):
        if group_id is None:
            return True
        group = self.groups[group_id]
        return group.get("cooldown", {}).get(market, 0) <= (time.time() if now is None else now)

    @staticmethod
    def _inventory_present(current):
        """Unknown inventory is not flat. Never rotate while exposure exists."""
        position = current.get("position_usd")
        if position is None:
            return True
        try:
            return float(position) > 1e-9
        except (TypeError, ValueError):
            return True

    def describe(self, session_id):
        with self.lock:
            for group in self.groups.values():
                if session_id in group["members"]:
                    return {"auto_group_id": group["id"], "auto_rotations": group["rotations"],
                            "auto_rotation_state": group["state"], "auto_rotation_reason": group["reason"],
                            "auto_fill_usd_per_min": group.get("fill_rate", 0),
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
                    for group in self.groups.values():
                        if not group["cancelled"]:
                            group["reason"] = "Auto waiting: " + safe_error(exc)

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

    def tick(self, now=None):
        from zeus.outcome_flow import scanner
        now = time.time() if now is None else now
        # No network under the controller lock except final admission. Stop may
        # cancel while public books are being fetched; recheck before all writes.
        with self.lock:
            group_ids = [key for key, group in self.groups.items() if not group["cancelled"]]
        for group_id in group_ids:
            with self.lock:
                group = self.groups[group_id]
                if group["cancelled"] or self.closed.is_set() or now < group["retry_at"]:
                    continue
                config, current_id = dict(group["config"]), group["current"]
            status = self.owner.status()
            current = next((b for b in status["bots"] if b["session_id"] == current_id), None)
            if current is None:
                continue
            with self.lock:
                group = self.groups[group_id]
                if group["cancelled"] or current_id != group["current"] or self.closed.is_set():
                    continue
                from zeus.session_targets import finish_reason
                reason = finish_reason({**config, 'session_volume_carry': group.get('volume_carry', 0)},
                                       current.get('volume_usd'), now)
                if reason or group['state'] == 'finishing':
                    if not current['running'] and current.get('cleanup_confirmed'):
                        group.update(cancelled=True, state='stopped', reason='')
                    else:
                        group.update(state='finishing', reason=reason or group['reason'])
                        info = self.owner.procs.get(current_id)
                        if info:
                            path = Path(info['stop_file']).with_name('drain.json')
                            if not path.exists():
                                atomic_json(path, {'requested_at': now, 'reason': group['reason'], 'session_finished': True})
                    continue
                if group["state"] == "draining":
                    if current["running"] and self._inventory_present(current):
                        # A fill arrived during the cancellation window. Abandon
                        # the market change and manage this inventory normally.
                        info = self.owner.procs.get(current_id)
                        path = (Path(info["stop_file"]).with_name("drain.json")
                                if info and info.get("stop_file") else None)
                        rotation_drain = False
                        if path and path.exists():
                            try:
                                request = read_json(path)
                            except (OSError, ValueError):
                                request = {}
                            rotation_drain = str((request or {}).get("reason", "")).lower().startswith(
                                "low executed")
                        if rotation_drain:
                            try:
                                path.unlink()
                            except OSError:
                                pass
                            group.update(
                                state="observing", started=now,
                                samples=deque([(now, float(current.get("volume_usd") or 0))],
                                              maxlen=2000),
                                candidate_market=None, candidate_since=0,
                                reason="Rotation annulée: inventaire présent")
                            continue
                    if current["running"]:
                        group["reason"] = "Rotation: cancelling bids / selling remaining inventory"
                        continue
                    if not current.get("cleanup_confirmed"):
                        group.update(state="blocked", reason="Rotation blocked: old stop not confirmed")
                        continue
                    # Flat is verified independently (zero orders and inventory);
                    # a prior error/failed phase no longer blocks the next leg.
                    if current_id not in group.setdefault("accounted", set()):
                        group["pnl_carry"] = group.get("pnl_carry", 0)+float(current.get("net_pnl", 0))
                        group['volume_carry'] = group.get('volume_carry', 0)+float(current.get('volume_usd', 0))
                        group["accounted"].add(current_id)
                elif not current["running"]:
                    group.update(cancelled=True, state="stopped", reason="")
                    continue
                elif current.get("stale") or current.get("phase") != "running":
                    continue
                if group["state"] not in ("observing", "draining"):
                    continue
                window = float(config.get("auto_rotation_after_sec", 60))
                if group["state"] == "observing":
                    volume = float(current["volume_usd"])
                    group["samples"].append((now, volume))
                    while len(group["samples"]) > 1 and group["samples"][1][0] <= now-window:
                        group["samples"].popleft()
                    first_time, first_volume = group["samples"][0]
                    elapsed = now-first_time
                    group["fill_rate"] = max(0, volume-first_volume)*60/max(1, elapsed)
                    zero_fills = volume <= 0 and not current.get("fills", 0)
                    deadline = no_fill_grace(config, group.get("current_selection")) if zero_fills else window
                    group.update(evaluate_after_sec=deadline, evaluate_at=group['started']+deadline)
                    if now-group["started"] < deadline or elapsed < deadline-10:
                        continue
                    if current.get("execution_state") == "Insufficient USDC":
                        group["reason"] = "Available USDC below full order size"
                        continue
                    if current.get("last_fill_at") and now-current['last_fill_at'] < min(30, window/3):
                        group["reason"] = ""
                        continue
                    threshold = config.get("auto_min_fill_usd_per_min")
                    threshold = config["order_size_usd"] if threshold is None else float(threshold)
                    if group["fill_rate"] >= threshold:
                        group["reason"] = ""
                        continue
            context = getattr(getattr(self.owner, 'manager', None), 'account_context', None)
            account = context.account if context else os.getenv('WALLET_ADDRESS_OUTCOME', '').strip()
            snapshot = scanner.snapshot(config["outcome_side"], config["order_size_usd"],
                                        config.get("auto_max_spread_bps", 10),
                                        account=account)
            if not snapshot.get("ready"):
                with self.lock:
                    group = self.groups.get(group_id)
                    if group and not group["cancelled"] and group["state"] == "observing":
                        group["reason"] = "Rotation: waiting for eligible flow"
                        group["candidate_since"] = 0
                        group["candidate_market"] = None
                continue
            if now-float(snapshot.get("updated_at", now)) > 20:
                with self.lock:
                    group = self.groups.get(group_id)
                    if group and not group["cancelled"] and group["state"] == "observing":
                        group["reason"] = "Rotation: market data delayed"
                        group["candidate_since"] = 0
                        group["candidate_market"] = None
                continue
            busy = {b["market"] for b in status["bots"] if b["running"] or not b.get("cleanup_confirmed")}
            ranked = sorted(snapshot["candidates"], key=lambda r: candidate_rank(r, group.get('market_results', {}), now))
            top = next((r for r in ranked if r["market"] not in busy
                        and r["market"] != current["market"]
                        and self.candidate_allowed(group_id, r["market"], now)), None)
            with self.lock:
                group = self.groups[group_id]
                if group["cancelled"] or current_id != group["current"] or self.closed.is_set():
                    continue
                if group["state"] == "observing":
                    if top is None:
                        group.update(candidate_market=None, candidate_since=0,
                                     reason="Low fills: waiting for another eligible $/min market")
                        continue
                    if self._inventory_present(current):
                        # Low volume is a re-evaluation signal, never permission
                        # to liquidate inventory. Stay and manage the sale.
                        group.update(candidate_market=None, candidate_since=0,
                                     reason="Low fills: inventaire présent, vente gérée")
                        continue
                    # A better market must stay eligible for 30 s and offer a
                    # cycle at least 25% shorter; otherwise wait.
                    if group.get("candidate_market") != top["market"]:
                        group.update(candidate_market=top["market"], candidate_since=now,
                                     reason="Rotation: candidat en observation")
                        continue
                    if now - float(group.get("candidate_since") or now) < 30:
                        group["reason"] = "Rotation: candidat en observation"
                        continue
                    current_cycle = float((group.get("current_selection") or {}).get(
                        "queue_proxy_sec") or 0)
                    next_cycle = float(top.get("queue_proxy_sec") or 0)
                    if current_cycle > 0 and next_cycle > current_cycle * 0.75:
                        group["reason"] = "Rotation: le prochain marché n'est pas 25% plus rapide"
                        continue
                    info = self.owner.procs.get(current_id)
                    if info is None:
                        continue
                    atomic_json(Path(info["stop_file"]).with_name("drain.json"), {
                        "requested_at": now, "reason": "low executed $/min", "next_market": top["market"]})
                    group.update(state="draining", reason="Rotation: draining current market")
                    previous = group.setdefault('market_results', {}).get(current['market'], {})
                    failures = previous.get('zero_fill_attempts', 0)+1 if float(current.get('volume_usd', 0)) <= 0 else 0
                    group['market_results'][current['market']] = dict(zero_fill_attempts=failures, updated_at=now,
                        executed_usd_per_min=float(current.get('volume_usd', 0))*60/max(1, now-group['started']))
                    cooldown = config.get("auto_market_cooldown_sec", 300)*min(8, 2**failures)
                    group.setdefault("cooldown", {})[current["market"]] = now+cooldown
                    continue
                # Old worker is dead and exchange cleanup is confirmed. start()
                # reselects from a fresh scanner snapshot and preserves budgets.
                if top is None:
                    # Never end the logical session on its own. Wait for the
                    # next eligible market; the user keeps the Stop control.
                    group["reason"] = "Rotation: flat, waiting for eligible flow"
                    continue
                group["wait_since"] = 0.0
                group["launch_failures"] = 0
                group["candidate_market"] = None
                group["candidate_since"] = 0
                from zeus.server import WorldcupStartRequest
                values = {k: v for k, v in config.items() if k in WorldcupStartRequest.model_fields}
                if values.get("maker_spread_pct") is None:
                    values.pop("maker_spread_pct", None)
                values.update(market=top["market"], two_sided=True)
                cooldowns = group.setdefault("cooldown", {})
                cooldowns[current["market"]] = max(cooldowns.get(current["market"], 0), now+config.get("auto_market_cooldown_sec", 300))
                group.update(state="launching", retry_at=now+15)
                request = WorldcupStartRequest(**values)
            # Metadata resolution can block. Stop remains immediate; the
            # supervisor rechecks cancellation under the final admission lock.
            try:
                self.owner.start(request, rotation_group=group_id)
                with self.lock:
                    group["rotations"] += 1
            except Exception as exc:
                with self.lock:
                    if not group['cancelled']:
                        failures = group.get("launch_failures", 0) + 1
                        group["launch_failures"] = failures
                        # Retry with a bounded backoff; never end the session
                        # on a transient launch failure.
                        group.update(state="draining", retry_at=now+min(60, 5*2**min(failures, 4)),
                                     reason="Rotation waiting: " + safe_error(exc))

    def close(self):
        self.closed.set()
        self.cancel()
        if self.thread:
            self.thread.join(timeout=2)
        if self.catalog_thread:
            self.catalog_thread.join(timeout=2)
