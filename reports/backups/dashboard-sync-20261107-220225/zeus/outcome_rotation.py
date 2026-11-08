"""Server-owned Auto rotation. Drain passively, verify flat, then reselect.

No order signing here. Stop cancels the complete rotation chain; a stopped
server never resumes old groups implicitly. Public scanner is shared by all.
"""
from collections import deque
from pathlib import Path
import threading
import time

from zeus.safety import atomic_json, safe_error


class OutcomeRotation:
    def __init__(self, owner):
        self.owner = owner
        self.lock = threading.RLock()
        self.groups = {}
        self.closed = threading.Event()
        self.thread = None
        self.catalog_refreshed = 0.0

    def register(self, session, config, group_id=None):
        if config.get("selection_mode") != "auto" or not config.get("auto_rotate", True):
            return
        with self.lock:
            if self.closed.is_set():
                raise RuntimeError("Auto rotation controller closed")
            group_id = group_id or session["session_id"]
            group = self.groups.get(group_id)
            if group is None:
                group = self.groups[group_id] = {"id": group_id, "config": dict(config),
                    "members": set(), "cancelled": False, "rotations": 0, "cooldown": {},
                    "pnl_carry": 0.0, "accounted": set()}
            group.update(current=session["session_id"], started=time.time(), state="observing",
                         samples=deque(maxlen=2000), reason="", retry_at=0.0)
            group["members"].add(session["session_id"])
            if self.thread is None or not self.thread.is_alive():
                self.thread = threading.Thread(target=self._loop, name="outcome-auto-rotation", daemon=True)
                self.thread.start()

    def cancel(self, session_id=None):
        """Returns current children for an old card's Stop, under the same lock
        used to admit replacements. A launch cannot slip behind manual Stop.
        """
        # Close admission immediately, even if a replacement is resolving its
        # catalog under the controller lock. The supervisor checks again at spawn.
        for group in self.groups.copy().values():
            if session_id is None or session_id in group["members"]:
                group["cancelled"] = True
        with self.lock:
            current = set()
            for group in self.groups.values():
                if session_id is None or session_id in group["members"]:
                    group["cancelled"] = True
                    current.add(group["current"])
            return current

    def assert_admission(self, group_id):
        if group_id is not None and (self.closed.is_set() or self.groups[group_id]["cancelled"]):
            raise RuntimeError("Auto rotation cancelled by Stop")

    def candidate_allowed(self, group_id, market, now=None):
        if group_id is None:
            return True
        group = self.groups[group_id]
        return group.get("cooldown", {}).get(market, 0) <= (time.time() if now is None else now)

    def describe(self, session_id):
        with self.lock:
            for group in self.groups.values():
                if session_id == group["current"]:
                    return {"auto_rotations": group["rotations"], "auto_rotation_state": group["state"],
                            "auto_rotation_reason": group["reason"], "auto_fill_usd_per_min": group.get("fill_rate", 0)}
        return {}

    def _loop(self):
        while not self.closed.wait(5):
            try:
                with self.lock:
                    active = any(not g["cancelled"] for g in self.groups.values())
                if not active:
                    continue
                self.refresh_catalog()
                self.tick()
            except Exception as exc:
                with self.lock:
                    for group in self.groups.values():
                        if not group["cancelled"]:
                            group["reason"] = "Auto waiting: " + safe_error(exc)

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
                if group["state"] == "draining":
                    if current["running"]:
                        group["reason"] = "Rotation: cancelling bids / selling remaining inventory"
                        continue
                    if not current.get("cleanup_confirmed"):
                        group.update(state="blocked", reason="Rotation blocked: old stop not confirmed")
                        continue
                    if current.get("phase") == "failed" or current.get("error"):
                        group.update(state="blocked", reason="Rotation blocked: previous session failed")
                        continue
                    if current_id not in group.setdefault("accounted", set()):
                        group["pnl_carry"] = group.get("pnl_carry", 0)+float(current.get("net_pnl", 0))
                        group["accounted"].add(current_id)
                elif not current["running"]:
                    group.update(cancelled=True, state="stopped", reason="")
                    continue
                elif current.get("stale") or current.get("phase") != "running":
                    continue
                if group["state"] not in ("observing", "draining"):
                    continue
                window = float(config.get("auto_rotation_after_sec", 180))
                if group["state"] == "observing":
                    volume = float(current["volume_usd"])
                    group["samples"].append((now, volume))
                    while len(group["samples"]) > 1 and group["samples"][1][0] <= now-window:
                        group["samples"].popleft()
                    first_time, first_volume = group["samples"][0]
                    elapsed = now-first_time
                    group["fill_rate"] = max(0, volume-first_volume)*60/max(1, elapsed)
                    if now-group["started"] < window or elapsed < window-10:
                        continue
                    threshold = config.get("auto_min_fill_usd_per_min")
                    threshold = config["order_size_usd"] if threshold is None else float(threshold)
                    if group["fill_rate"] >= threshold:
                        group["reason"] = ""
                        continue
            snapshot = scanner.snapshot(config["outcome_side"], config["order_size_usd"],
                                        config.get("auto_max_spread_bps", 10))
            if not snapshot.get("ready") or now-float(snapshot.get("updated_at", now)) > 20:
                continue
            busy = {b["market"] for b in status["bots"] if b["running"] or not b.get("cleanup_confirmed")}
            top = next((r for r in snapshot["candidates"] if r["market"] not in busy
                        and r["market"] != current["market"]
                        and self.candidate_allowed(group_id, r["market"], now)), None)
            with self.lock:
                group = self.groups[group_id]
                if group["cancelled"] or current_id != group["current"] or self.closed.is_set():
                    continue
                if group["state"] == "observing":
                    if top is None:
                        group["reason"] = "Low fills: waiting for another eligible $/min market"
                        continue
                    info = self.owner.procs.get(current_id)
                    if info is None:
                        continue
                    atomic_json(Path(info["stop_file"]).with_name("drain.json"), {
                        "requested_at": now, "reason": "low executed $/min", "next_market": top["market"]})
                    group.update(state="draining", reason="Rotation: draining current market")
                    group.setdefault("cooldown", {})[current["market"]] = now+config.get("auto_market_cooldown_sec", 600)
                    continue
                # Old worker is dead and exchange cleanup is confirmed. start()
                # reselects from a fresh scanner snapshot and preserves budgets.
                if top is None:
                    group["reason"] = "Rotation: flat, waiting for eligible flow"
                    continue
                from zeus.server import WorldcupStartRequest
                values = {k: v for k, v in config.items() if k in WorldcupStartRequest.model_fields}
                if values.get("maker_spread_pct") is None:
                    values.pop("maker_spread_pct", None)
                values.update(market=top["market"], two_sided=True)
                group.setdefault("cooldown", {})[current["market"]] = now+config.get("auto_market_cooldown_sec", 600)
                try:
                    self.owner.start(WorldcupStartRequest(**values), rotation_group=group_id)
                    group["rotations"] += 1
                except Exception as exc:
                    group["retry_at"] = now+15
                    group["reason"] = "Rotation waiting: " + safe_error(exc)

    def close(self):
        self.closed.set()
        self.cancel()
        if self.thread:
            self.thread.join(timeout=2)
