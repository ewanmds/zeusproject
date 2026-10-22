"""Conservative L2 replay. No exchange client, wallet import or network calls."""
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import random

from .core import (Features, OFFSETS, POLICY, POLICY_HASH, SCHEMA, SYMBOL,
                   finite, minimum_size, price_round, quotes, retain, safe_resting, tick_size)
from .runtime import atomic_json, implementation_hash


def read_session(path):
    path = Path(path)
    raw = path.read_bytes()
    events = [json.loads(line) for line in raw.decode("utf-8").splitlines()]
    if not events or events[0]["kind"] != "start" or events[-1]["kind"] != "end":
        raise ValueError(f"session not closed: {path.name}")
    header = events[0]["data"]
    if (header["schema"] != SCHEMA or header["symbol"] != SYMBOL or header["policy_hash"] != POLICY_HASH
            or header.get("implementation_hash") != implementation_hash()):
        raise ValueError("session implementation / policy mismatch")
    if not events[-1]["data"].get("clean") or any(e["kind"] == "gap" for e in events):
        raise ValueError("session contains recording / feed gaps")
    # Callback threads may enqueue in a different order. Stable receipt-time
    # sort is causal; exchange source times are never used to move a trade back.
    events = sorted(events[1:-1], key=lambda event: finite(event["ts"]))
    books = [e for e in events if e["kind"] == "book"]
    if len(books) < 2 or books[-1]["ts"] - books[0]["ts"] < 1800:
        raise ValueError("at least 30 minutes of L2 required")
    for previous, current in zip(books, books[1:]):
        if current["ts"] - previous["ts"] > POLICY.book_max_age:
            raise ValueError("L2 reception gap")
    for event in books:
        if abs(finite(event["data"].get("server_now", event["ts"])) - finite(event["data"]["source_ts"])) > POLICY.book_max_age:
            raise ValueError("stale / future L2 source")
    fees = [e["data"] for e in events if e["kind"] == "fees"]
    if not fees or len({f["fingerprint"] for f in fees}) != 1:
        raise ValueError("verified, consistent fees required throughout session")
    start, end = books[0]["ts"], books[-1]["ts"]
    if datetime.fromtimestamp(start, timezone.utc).date() != datetime.fromtimestamp(end, timezone.utc).date():
        raise ValueError("split recordings at UTC midnight")
    info = dict(name=path.name, start=start, end=end, utc_date=datetime.fromtimestamp(start, timezone.utc).date().isoformat(),
                sha256=hashlib.sha256(raw).hexdigest(), fee_fingerprint=fees[0]["fingerprint"])
    return events, info


class Simulator:
    def __init__(self, strategy, latency_ms, offset=0):
        self.strategy, self.offset, self.delay = strategy, offset, latency_ms / 1000
        self.features = Features()
        self.fees = None
        self.orders = {}
        self.pending = []
        self.next_id = 0
        self.position = self.entry = self.position_since = 0.0
        self.volume = self.fee_cost = self.realized = self.funding = 0.0
        self.round_trips = self.fills = self.actions = self.rejects = 0
        self.blocks = defaultdict(lambda: [0.0, 0.0])  # volume, all-in cost
        self.last_decision = 0
        self.hard_exit = None
        self.seen = set()
        self.funding_seen = set()
        self.inventory_history = []
        self.contexts = []
        self.funding_events = {}
        self.funding_complete = True
        self.invalid = ""
        self.max_size = 0.0

    def fill(self, side, price, size, now, maker=True):
        if size <= 0:
            return
        signed = size if side == "B" else -size
        old = self.position
        realized = 0.0
        if old * signed < 0:
            realized = min(abs(old), size) * (price - self.entry) * (1 if old > 0 else -1)
        new = round(old + signed, self.fees["decimals"])
        if old == 0 or old * new < 0:
            self.entry = price
            self.position_since = now
        elif old * signed > 0:
            self.entry = (self.entry * abs(old) + price * size) / (abs(old) + size)
        if old and (not new or old * new < 0):
            self.round_trips += 1
        self.position = new
        if not new:
            self.position_since = 0
            self.hard_exit = None
        self.inventory_history.append((now, new))
        if self.max_size and abs(new) > self.max_size + 10 ** -self.fees["decimals"] / 2:
            self.invalid = "inventory cap exceeded under latency"
        fee = price * size * self.fees["maker" if maker else "taker"]
        self.volume += price * size
        self.fee_cost += fee
        self.realized += realized
        self.fills += 1
        block = self.blocks[int(now // 60)]
        block[0] += price * size
        block[1] += fee - realized

    def _ack(self, now):
        remaining = []
        book = self.features.book
        for due, kind, payload in self.pending:
            if due > now:
                remaining.append((due, kind, payload))
                continue
            if kind == "cancel":
                self.orders.pop(payload, None)
            elif kind == "place":
                order = payload
                buy = order["side"] == "B"
                if not book or (buy and order["px"] >= book["ask"]) or (not buy and order["px"] <= book["bid"]):
                    self.rejects += 1  # ALO rejection; no pretend execution.
                    continue
                level = 0 if buy else 1
                prices = self.levels[level]
                pxs = [finite(p["px"]) for p in prices]
                visible = bool(pxs) and (order["px"] >= min(pxs) if buy else order["px"] <= max(pxs))
                order["ahead"] = sum(finite(p["sz"]) for p in prices if abs(finite(p["px"]) - order["px"]) < 1e-8) if visible else math.inf
                self.next_id += 1
                order["ts"] = now
                self.orders[self.next_id] = order
            elif kind == "ioc" and self.position and book:
                side = "A" if self.position > 0 else "B"
                left = abs(self.position)
                for level in self.levels[0 if side == "A" else 1]:
                    size = min(left, finite(level["sz"]))
                    self.fill(side, finite(level["px"]), size, now, maker=False)
                    left -= size
                    if left < 1e-9:
                        break
                if left > 1e-9:
                    self.invalid = "insufficient visible liquidation depth"
        self.pending = remaining

    def _desired(self, now):
        book = self.features.book
        decimals = self.fees["decimals"]
        if self.strategy == "experimental":
            return quotes(book, now, decimals, self.offset, self.fees["maker"], self.position, self.position_since)
        mid, bb, ba = book["mid"], book["bid"], book["ask"]
        tick = tick_size(mid, decimals)
        bid, ask = bb, ba
        if self.strategy == "mid":
            distance = max(tick, mid / 10000)
            bid, ask = price_round(mid - distance, tick, True, decimals), price_round(mid + distance, tick, False, decimals)
        size = minimum_size(min(bid, bb), decimals)
        if self.position:
            return {"A" if self.position > 0 else "B": (ask if self.position > 0 else bid, abs(self.position), True)}
        self.max_size = size
        return {"B": (bid, size, False), "A": (ask, size, False)}

    def _decide(self, now):
        book = self.features.book
        if not book or not self.fees or self.pending or now - self.last_decision < 0.08:
            return
        if not 0 <= now - self.fees["observed_at"] <= POLICY.fee_max_age:
            self.invalid = "fee gap during replay"
            return
        self.last_decision = now
        if self.position:
            adverse = (book["mid"] - self.entry) / self.entry * 1e4 * (1 if self.position > 0 else -1)
            if adverse <= -POLICY.stop_bps or now - self.position_since >= POLICY.max_hold:
                self.hard_exit = self.hard_exit or now + POLICY.maker_exit_window
            if adverse <= -2 * POLICY.stop_bps or self.hard_exit and now >= self.hard_exit:
                for oid in self.orders:
                    self.pending.append((now + self.delay, "cancel", oid))
                    self.actions += 1
                self.pending.append((now + self.delay, "ioc", None))
                self.actions += 1
                return
        desired = self._desired(now)
        tick = tick_size(book["mid"], self.fees["decimals"])
        for side in ("B", "A"):
            want = desired.get(side)
            old = [(oid, o) for oid, o in self.orders.items() if o["side"] == side]
            if len(old) == 1 and want:
                order = old[0][1]
                if (order["ro"] == want[2] and abs(order["px"] - want[0]) < tick / 2
                        and abs(order["sz"] - want[1]) < 10 ** -self.fees["decimals"] / 2):
                    continue
                if self.strategy == "experimental" and safe_resting(order, want, book, self.fees["maker"]) and retain(order, want, now, tick):
                    continue
            for oid, _ in old:
                self.pending.append((now + self.delay, "cancel", oid))
                self.actions += 1
            if want:
                if not self.position:
                    self.max_size = want[1]
                self.pending.append((now + self.delay, "place", dict(side=side, px=want[0], sz=want[1], ro=want[2])))
                self.actions += 1

    def process(self, event):
        now, kind, data = finite(event["ts"]), event["kind"], event["data"]
        if kind == "fees":
            self.fees = data
        if kind == "context":
            self.contexts.append((now, finite(data["oracle"])))
        if kind == "funding":
            stamp, rate = finite(data["time"]), finite(data["rate"])
            if stamp in self.funding_events and self.funding_events[stamp] != rate:
                self.invalid = "conflicting funding rates"
            self.funding_events[stamp] = rate
        if kind == "book":
            self.levels = data["levels"]
            self.features.update(now, *self.levels)
        if kind not in ("book", "trades"):
            return
        self._ack(now)
        if kind == "trades" and self.fees:
            for trade in data:
                tid = trade["tid"]
                if tid in self.seen:
                    continue
                self.seen.add(tid)
                if abs(now - finite(trade["time"])) > POLICY.book_max_age:
                    self.invalid = "stale trade source"
                    continue
                side = "A" if trade["side"] == "B" else "B"
                remaining = finite(trade["sz"])
                if remaining <= 0 or trade["side"] not in ("B", "A"):
                    self.invalid = "invalid trade"
                    continue
                for oid, order in list(self.orders.items()):
                    if (order["side"] != side or finite(trade["time"]) < order["ts"]
                            or abs(order["px"] - finite(trade["px"])) > 1e-8):
                        continue
                    consumed = min(remaining, order["ahead"])
                    order["ahead"] -= consumed
                    remaining -= consumed
                    fill_size = min(remaining, order["sz"])
                    if order["ro"]:
                        fill_size = min(fill_size, abs(self.position)) if self.position * (1 if side == "B" else -1) < 0 else 0
                    if fill_size:
                        self.fill(side, order["px"], fill_size, now)
                        order["sz"] -= fill_size
                        remaining -= fill_size
                    if order["sz"] < 1e-9:
                        self.orders.pop(oid, None)
        self._decide(now)

    def finish(self, start, end):
        # Settlement uses position immediately before the funding timestamp.
        # Missing funding OR a nearby oracle with an open position is unknown.
        for stamp in range((int(start) // 3600 + 1) * 3600, int(end) + 1, 3600):
            position = 0.0
            for timestamp, value in self.inventory_history:
                if timestamp >= stamp:
                    break
                position = value
            if not position:
                continue
            oracles = [oracle for ts, oracle in self.contexts if 0 <= stamp - ts <= POLICY.book_max_age]
            if stamp not in self.funding_events or not oracles:
                self.funding_complete = False
                continue
            cost = position * oracles[-1] * self.funding_events[stamp]
            self.funding += cost
            self.blocks[stamp // 60][1] += cost
        return dict(round_trips=self.round_trips, fills=self.fills, volume=self.volume,
                    fees=self.fee_cost, realized_pnl=self.realized, funding=self.funding,
                    cpm=(self.fee_cost - self.realized) / self.volume * 1e6 if self.volume else None,
                    cpm_all_in=(self.fee_cost + self.funding - self.realized) / self.volume * 1e6 if self.volume else None,
                    actions=self.actions, rejects=self.rejects, funding_complete=self.funding_complete,
                    unclosed=bool(self.position), invalid=self.invalid, blocks=dict(self.blocks))


def run(events, info, strategy, latency, offset=0):
    simulator = Simulator(strategy, latency, offset)
    for event in events:
        simulator.process(event)
    return simulator.finish(info["start"], info["end"])


def confidence_bounds(metrics, baselines):
    """Paired 60-second block bootstrap; no per-fill independence assumption."""
    keys = sorted(set(metrics["blocks"]).union(*(b["blocks"] for b in baselines.values())))
    if len(keys) < 30:
        return math.inf, {name: math.inf for name in baselines}
    rng = random.Random(742)
    absolute, differences = [], {name: [] for name in baselines}
    for _ in range(1000):
        chosen = [rng.choice(keys) for _ in keys]
        def cpm(m):
            volume = sum(m["blocks"].get(k, [0, 0])[0] for k in chosen)
            return sum(m["blocks"].get(k, [0, 0])[1] for k in chosen) / volume * 1e6 if volume else math.inf
        value = cpm(metrics)
        absolute.append(value)
        for name, baseline in baselines.items():
            other = cpm(baseline)
            differences[name].append(value - other if math.isfinite(value) and math.isfinite(other) else math.inf)
    upper = lambda values: sorted(values)[math.ceil(0.95 * len(values)) - 1]
    return upper(absolute), {name: upper(values) for name, values in differences.items()}


def calibrate(paths, output):
    sessions = sorted((read_session(path) for path in paths), key=lambda item: item[1]["start"])
    if len(sessions) != 3 or len({info["utc_date"] for _, info in sessions}) != 3:
        raise ValueError("provide exactly three sessions on distinct UTC dates")
    if any(b[1]["start"] <= a[1]["end"] for a, b in zip(sessions, sessions[1:])):
        raise ValueError("sessions overlap")
    if len({info["fee_fingerprint"] for _, info in sessions}) != 1:
        raise ValueError("fees / precision changed between sessions")
    scores = {}
    for offset in OFFSETS:
        outcomes = [run(events, info, "experimental", latency, offset)
                    for events, info in sessions[:2] for latency in (250, 500)]
        scores[offset] = max((m["cpm_all_in"] if m["volume"] and m["funding_complete"] and not m["unclosed"] and not m["invalid"] else math.inf)
                             for m in outcomes)
    offset = min(OFFSETS, key=lambda candidate: (scores[candidate], candidate))
    holdout = []
    reasons = []
    if not math.isfinite(scores[offset]):
        reasons.append("training insufficient / invalid")
    events, info = sessions[-1]
    for latency in (250, 500):
        metrics = {name: run(events, info, name, latency, offset) for name in ("bbo", "mid", "experimental")}
        upper, differences = confidence_bounds(metrics["experimental"], {name: metrics[name] for name in ("bbo", "mid")})
        metrics["experimental"]["cpm_upper95"] = upper if math.isfinite(upper) else None
        differences = {name: value if math.isfinite(value) else None for name, value in differences.items()}
        for name, result in metrics.items():
            if result["round_trips"] < 500 or not result["volume"]:
                reasons.append(f"{latency}ms {name}: fewer than 500 round trips")
            if not result["funding_complete"] or result["unclosed"] or result["invalid"]:
                reasons.append(f"{latency}ms {name}: incomplete / invalid execution or funding")
            result.pop("blocks")
        if upper > 0 or any(value is None or value > 0 for value in differences.values()):
            reasons.append(f"{latency}ms: CPM bound / improvement not established")
        holdout.append(dict(latency_ms=latency, **metrics, difference_upper95=differences))
    report = dict(schema=SCHEMA, symbol=SYMBOL, policy_hash=POLICY_HASH, implementation_hash=implementation_hash(),
                  generated_at=datetime.now(timezone.utc).timestamp(), data_end=info["end"],
                  sessions=[info for _, info in sessions], fee_fingerprint=info["fee_fingerprint"],
                  accepted=not reasons, reasons=reasons, offset=offset, holdout=holdout,
                  training_scores={str(k): v if math.isfinite(v) else None for k, v in scores.items()})
    atomic_json(output, report)
    return report
