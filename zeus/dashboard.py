"""Dashboard projections, localization and the shared read broker.

One module for the whole read path: trading state and stored journals are
never edited here, and every consumer shares one cached collection.
"""

"""Pure dashboard projections. Trading state and stored journals are never edited.

Physical workers remain available in the legacy `bots` list. `display_bots`
represents active logical sessions (including Auto replacements); the overview
uses exactly these rows, so elapsed time and totals have one scope.
"""
import math
import time


def clock_text(seconds):
    seconds = int(max(0, seconds))
    return f"{seconds//3600:02d}:{seconds//60%60:02d}:{seconds%60:02d}"


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def outcome_projection(entries, intents=(), *, now=None):
    now = time.time() if now is None else now
    groups = {}
    for entry in entries:
        group_id = entry.get("auto_group_id") or entry.get("config", {}).get("auto_group_id") or entry["session_id"]
        groups.setdefault(group_id, []).append(entry)
    intents = {g["group_id"]: g for g in intents if not g["cancelled"]}
    rows = []
    for group_id, members in groups.items():
        active = [m for m in members if m.get("running") or m.get("cleanup_confirmed") is not True]
        intent = intents.get(group_id)
        if not active and not intent:
            continue
        current = next((m for m in members if intent and m["session_id"] == intent["current"]), None)
        current = current or max(active or members, key=lambda m: m.get("started", 0))
        row = dict(current)
        started = min((m["started"] for m in members if number(m.get("started"))), default=now)
        logical_running = bool(any(m.get("running") for m in active)
                               or intent and intent["state"] in ("observing", "draining", "launching"))
        live_stale = any(m.get("stale") for m in active if m.get("running"))
        complete = all(m.get("accounting_complete") is True
                       and all(number(m.get(field)) for field in ("volume_usd", "net_pnl", "fees", "fills"))
                       for m in members)
        # A verified final snapshot is valid for a closed leg; a stale live leg
        # makes the whole active total unknown instead of looking like zero.
        complete = complete and not live_stale
        row.update(session_id=group_id, worker_session_id=current["session_id"], auto_group_id=group_id,
                   running=logical_running, worker_running=current.get("running", False),
                   started=started, duration_sec=max(0, now-started) if logical_running else current.get("duration_sec", 0),
                   stale=live_stale, accounting_complete=complete,
                   sampled_at=now, cleanup_confirmed=not logical_running and all(m.get("cleanup_confirmed") for m in members))
        row["duration"] = clock_text(row["duration_sec"])
        row["market_duration_sec"] = max(0, now-current.get("started", now)) if current.get("running") else current.get("duration_sec", 0)
        for field in ("orders_sent", "cancels_sent", "rejected"):
            row[field] = sum(m[field] for m in members) if all(number(m.get(field)) for m in members) else None
        if not current.get("running"):
            row['quotes'] = []
        for field in ("volume_usd", "net_pnl", "fees", "fills"):
            row[field] = sum(m[field] for m in members) if complete and all(number(m.get(field)) for m in members) else None
        row["cost_per_1m"] = -row["net_pnl"]*1e6/row["volume_usd"] if complete and row["volume_usd"] and row["net_pnl"] is not None else None
        known = all(m.get("position_known") and number(m.get("position_usd")) for m in active)
        row["position_known"] = known
        row["position_usd"] = sum(m["position_usd"] for m in active) if known else None
        if intent and not current.get("running"):
            row.update(phase="rotating" if logical_running else "blocked", needs_attention=not logical_running,
                       auto_rotation_reason=intent.get("reason") or "Waiting for the next market", quoting_enabled=False)
        rows.append(row)
    rows.sort(key=lambda row: (row.get("started", 0), row["session_id"]))
    complete = all(row["accounting_complete"] for row in rows)
    active = [row for row in rows if row["running"]]
    metrics = {"scope": "active_sessions", "accounting_complete": complete,
               "stale": any(row["stale"] for row in rows),
               "duration_sec": max((row["duration_sec"] for row in active), default=0),
               "active_markets": len(active), "running": bool(active), "sampled_at": now}
    metrics["duration"] = clock_text(metrics["duration_sec"])
    for field in ("volume_usd", "net_pnl", "fees", "fills"):
        metrics[field] = sum(row[field] for row in rows) if complete and all(number(row.get(field)) for row in rows) else None
    metrics["cost_per_1m"] = -metrics["net_pnl"]*1e6/metrics["volume_usd"] if complete and metrics["volume_usd"] and metrics["net_pnl"] is not None else None
    metrics["position_usd"] = sum(row["position_usd"] for row in rows) if all(number(row.get("position_usd")) for row in rows) else None
    return {"display_bots": rows, "active_metrics": metrics}

# --- merged from dashboard_broker.py ---

"""Shared, versioned, bounded-cost dashboard reads for REST and WebSocket.

No background loop or exchange I/O. Each source has an independent lock/cache;
multiple readers reuse one collection instead of rereading every session file.
Serialization happens once per revision so every WebSocket client receives the
same text instead of re-encoding the payload per connection.
"""
import copy
import json
import threading
import time
import uuid

# Internal worker bookkeeping stays server-side; the browser never reads it.
_WIRE_DROP = {"outcome": {"bots"}}
_ROW_DROP = {"cloids", "owned_oids", "pending_cloids", "funds_root", "stats_file",
             "stop_file", "safety_file", "account_kind", "account", "symbols"}


class DashboardBroker:
    def __init__(self, sources, *, ttl=.5):
        self.sources = sources
        self.ttl = ttl
        self.source_id = uuid.uuid4().hex
        self.locks = {kind: threading.Lock() for kind in sources}
        self.entries = {}
        self.serialized = {}
        self.revisions = {kind: 0 for kind in sources}

    @staticmethod
    def _lean(kind, data):
        drop = _WIRE_DROP.get(kind)
        if not drop:
            return data
        lean = {key: value for key, value in data.items() if key not in drop}
        rows = lean.get("display_bots")
        if isinstance(rows, list):
            lean["display_bots"] = [{key: value for key, value in row.items() if key not in _ROW_DROP}
                                    for row in rows]
        return lean

    def _collect(self, kind, *, fresh=False):
        """Return the current snapshot without copying it.

        The lock only covers collection, never the caller's copy or the JSON
        encoding, so a slow reader cannot block another tab.
        """
        entry = self.entries.get(kind)
        if not fresh and entry is not None and time.monotonic()-entry[0] < self.ttl:
            return entry[1], entry[2]
        with self.locks[kind]:
            entry = self.entries.get(kind)
            if not fresh and entry is not None and time.monotonic()-entry[0] < self.ttl:
                return entry[1], entry[2]
            started = time.perf_counter()
            data = self.sources[kind]()
            self.revisions[kind] += 1
            revision = self.revisions[kind]
            data = {**data, "source_id": self.source_id, "revision": revision,
                    "published_at": time.time(),
                    "collection_ms": (time.perf_counter()-started)*1000}
            self.entries[kind] = (time.monotonic(), data, revision)
            self.serialized.pop(kind, None)
            return data, revision

    def read(self, kind, *, fresh=False):
        data, _ = self._collect(kind, fresh=fresh)
        return copy.deepcopy(data)

    def payload(self, kind, *, fresh=False):
        """JSON text for WebSocket fan-out, encoded once per revision."""
        data, revision = self._collect(kind, fresh=fresh)
        cached = self.serialized.get(kind)
        if cached is not None and cached[0] == revision:
            return cached[1]
        text = json.dumps(self._lean(kind, data), separators=(",", ":"))
        if cached is None or revision >= cached[0]:
            self.serialized[kind] = (revision, text)
        return text

    def invalidate(self, kind):
        with self.locks[kind]:
            self.entries.pop(kind, None)
            self.serialized.pop(kind, None)

# --- merged from localize.py ---

"""Translate engine-provided status text into the dashboard language.

Keep this table in sync with the engine when it changes.
"""

_ENGINE_TEXT = (
    ("Comptabilite incomplete: executions finales non confirmees",
     "Incomplete accounting: final executions not confirmed"),
    ("Arret confirme: ordres et position a zero", "Stop confirmed: orders and position at zero"),
    ("Arret non confirme: ", "Stop not confirmed: "),
    ("budget API insuffisant", "insufficient API budget"),
    ("requetes restantes", "requests remaining"),
    ("quotes suspendues : ", "quotes suspended: "),
    ("fonds disponibles non confirmes", "available funds not confirmed"),
    ("attente capacite inventaire / montant executable",
     "waiting for inventory capacity / executable amount"),
    ("preparation des ordres", "preparing orders"),
    ("position non confirmee", "position not confirmed"),
    ("reponse position invalide", "invalid position response"),
    ("symbole de position invalide", "invalid position symbol"),
    ("taille de position invalide", "invalid position size"),
    ("position residuelle", "residual position"),
    ("ordres non confirmes", "orders not confirmed"),
    ("reponse ordres invalide", "invalid orders response"),
    ("symbole d'ordre invalide", "invalid order symbol"),
    ("ordres residuels", "residual orders"),
)

TEXT_FIELDS = ("action", "error", "cleanup_error", "recovery_error", "warning")


def localize_state(state):
    """Return ``state`` with engine-provided status text translated in place."""
    for key in TEXT_FIELDS:
        value = state.get(key)
        if isinstance(value, str):
            for source, target in _ENGINE_TEXT:
                value = value.replace(source, target)
            state[key] = value
    return state
