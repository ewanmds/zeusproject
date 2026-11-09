"""Public, read-only Outcome taker-flow scanner. Importing opens no sockets.

Rank repeated two-way *executed* flow, not resting orders or claimed bots.
One shared websocket, bounded history, bounded REST shortlist. Never signs.
"""
from collections import defaultdict, deque
import copy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import math
import statistics
import threading
import time

import requests

# Keep-alive per thread: one snapshot checks up to 24 order books, and a cold
# TLS handshake per book would dominate the auto-selection latency.
_local = threading.local()


def _http():
    session = getattr(_local, "session", None)
    if session is None:
        session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(pool_connections=24, pool_maxsize=32)
        session.mount("https://", adapter)
        _local.session = session
    return session


def flow_metrics(trades, now):
    """Five-minute conservative rates; split prints in one second are one event."""
    unique = {}
    for trade in trades:
        try:
            stamp = float(trade["time"]) / 1000
            price, size = float(trade["px"]), float(trade["sz"])
            side = trade["side"]
            if (not all(map(math.isfinite, (stamp, price, size))) or not 0 < price < 1
                    or size <= 0 or side not in ("A", "B") or not now-300 <= stamp <= now+1):
                continue
            key = (trade["time"], trade.get("tid", trade.get("hash")), side, price, size)
            unique[key] = (stamp, price, size, side)
        except (KeyError, TypeError, ValueError):
            continue
    values = sorted(unique.values())
    if not values:
        return None
    events = sorted({int(x[0]) for x in values})
    recent = [x for x in values if x[0] >= now-60]
    buy = sum(p*q for _, p, q, side in recent if side == "B")
    sell = sum(p*q for _, p, q, side in recent if side == "A")
    bins = {int((now-t)//10) for t, _, _, _ in recent}
    directions = {s: len({int(t) for t, _, _, side in recent if side == s}) for s in ("A", "B")}
    age = max(0, now-values[-1][0])
    span = values[-1][0]-values[0][0]
    gaps = [b-a for a, b in zip(events, events[1:])]
    cadence = statistics.median(gaps) if gaps else 300
    # Harmonic flow penalizes a stream that lets us enter but never exit.
    balanced = 2*buy*sell/(buy+sell) if buy+sell else 0
    coverage = min(1, len(bins)/6)
    score = balanced * coverage * math.exp(-age/20)
    eligible = (len(events) >= 6 and span >= 30 and age <= 20 and len(bins) >= 3
                and min(directions.values()) >= 2 and cadence <= 30)
    return {"score": score, "eligible": eligible, "trades_5m": len(values),
            "events_5m": len(events), "active_bins_1m": len(bins), "span_sec": span,
            "buy_usd_1m": buy, "sell_usd_1m": sell, "volume_usd_1m": buy+sell,
            "balanced_usd_1m": balanced, "last_trade_age_sec": age,
            "median_gap_sec": cadence,
            "buy_tokens_1m": sum(q for _, _, q, s in recent if s == "B"),
            "sell_tokens_1m": sum(q for _, _, q, s in recent if s == "A")}


def before_event(row, now):
    """Exclude started/expiring events; combine child and question metadata."""
    if blocked_outcome(row):
        return False
    fields = {}
    for description in (row.get("question_description", ""), row.get("description", "")):
        for item in description.split("|"):
            if ":" in item:
                key, value = item.split(":", 1)
                fields[key] = value
    for key in ("scheduledStart", "expiry", "time", "dateTime", "resolutionDeadline"):
        if key in fields:
            try:
                deadline = datetime.strptime(fields[key], "%Y%m%d-%H%M").replace(tzinfo=timezone.utc).timestamp()
            except ValueError:
                return False
            if deadline-now < 1800:
                return False
    # Sports children without their parent event schedule cannot be screened.
    return row.get("category") != "sports" or "scheduledStart" in fields


class OutcomeFlowScanner:
    def __init__(self):
        self.lock = threading.RLock()
        self.rows = {}
        self.history = defaultdict(lambda: deque(maxlen=2000))
        self.ws = None
        self.thread = None
        self.subscription_thread = None
        self.stop = threading.Event()
        self.connected = False
        self.subscribed = set()
        self.connected_at = 0
        self.catalog_at = 0
        self.updated_at = 0
        self.generation = 0
        self.error = ""
        self.books = {}
        self.book_lock = threading.Lock()
        self.snapshot_lock = threading.Lock()
        self.snapshot_cache = {}
        self.catalog_revision = 0
        # Persistent pool: worker threads keep their keep-alive HTTP session,
        # so only the first book read per thread pays the TLS handshake.
        self.book_pool = None

    def _book_executor(self):
        with self.lock:
            if self.stop.is_set():
                return None
            if self.book_pool is None:
                self.book_pool = ThreadPoolExecutor(max_workers=24, thread_name_prefix="outcome-book")
            return self.book_pool

    def configure(self, rows):
        # 450 outcomes * two trade subscriptions: below the 1000-sub limit.
        now = time.time()
        rows = {int(r["id"]): dict(r) for r in rows
                if r.get("venue", "out") == "out" and before_event(r, now)}
        if len(rows) > 450:
            raise ValueError("Outcome scanner subscription limit exceeded")
        with self.lock:
            # A shutdown that raced this configure() must not restart the
            # scanner thread after close().
            if self.stop.is_set():
                return
            if rows != self.rows:
                self.catalog_revision += 1
            self.rows, self.catalog_at = rows, now
            for coin in list(self.history):
                if int(coin[1:])//10 not in rows:
                    self.history.pop(coin, None)
                    self.books.pop(coin, None)
            self.subscribed = {c for c in self.subscribed if int(c[1:])//10 in rows}
            if self.thread is None or not self.thread.is_alive():
                self.stop.clear()
                self.thread = threading.Thread(target=self._run, name="outcome-flow", daemon=True)
                self.thread.start()

    def _run(self):
        import websocket
        while not self.stop.is_set():
            def opened(ws):
                with self.lock:
                    self.connected = True
                    self.connected_at = time.time()
                    self.generation += 1
                    self.history.clear()
                    self.subscribed.clear()
                    self.error = ""
                def subscribe():
                    # Receive snapshots while subscribing; do not burst 400 sends.
                    sent = set()
                    last_ping = time.monotonic()
                    try:
                        while not self.stop.is_set() and self.ws is ws and self.connected:
                            with self.lock:
                                wanted = {f"#{10*outcome+side}" for outcome in self.rows for side in (0, 1)}
                            # Incremental catalog updates preserve all other queues
                            # and observation history instead of restarting warmup.
                            changes = [("unsubscribe", c) for c in sorted(sent-wanted)]
                            changes += [("subscribe", c) for c in sorted(wanted-sent)]
                            # Send modest batches instead of one frame per 200 ms:
                            # 380 subscriptions used to take 76 s, delaying
                            # readiness long past the 60 s observation window.
                            for index, (method, coin) in enumerate(changes):
                                if index and index % 20 == 0:
                                    if self.stop.wait(.5) or self.ws is not ws or not self.connected:
                                        return
                                elif self.stop.is_set() or self.ws is not ws or not self.connected:
                                    return
                                with self.lock:
                                    valid = int(coin[1:])//10 in self.rows
                                if method == "subscribe" and not valid:
                                    continue
                                ws.send(json.dumps({"method": method, "subscription": {"type": "trades", "coin": coin}}))
                                if method == "subscribe": sent.add(coin)
                                else: sent.discard(coin)
                                if time.monotonic()-last_ping >= 20:
                                    ws.send(json.dumps({"method": "ping"}))
                                    last_ping = time.monotonic()
                            if time.monotonic()-last_ping >= 20:
                                ws.send(json.dumps({"method": "ping"}))
                                last_ping = time.monotonic()
                            self.stop.wait(1)
                    except Exception:
                        failed(ws, None)
                        ws.close()
                self.subscription_thread = threading.Thread(target=subscribe,
                                        name="outcome-flow-subscribe", daemon=True)
                self.subscription_thread.start()

            def message(ws, raw):
                try:
                    payload = json.loads(raw)
                    if payload.get("channel") == "subscriptionResponse":
                        subscription = payload.get("data", {}).get("subscription", {})
                        if subscription.get("type") == "trades":
                            with self.lock:
                                coin = subscription["coin"]
                                if (payload["data"].get("method") == "subscribe"
                                        and int(coin[1:])//10 in self.rows):
                                    self.subscribed.add(coin)
                                else:
                                    self.subscribed.discard(coin)
                        return
                    if payload.get("channel") != "trades" or not isinstance(payload.get("data"), list):
                        return
                    self.ingest(payload["data"])
                except (ValueError, TypeError):
                    pass

            def failed(ws, error):
                with self.lock:
                    self.connected = False
                    self.error = "Public trade stream unavailable"

            def closed(ws, *args):
                with self.lock:
                    self.connected = False

            self.ws = websocket.WebSocketApp("wss://api.hyperliquid.xyz/ws", on_open=opened,
                            on_message=message, on_error=failed, on_close=closed)
            try:
                self.ws.run_forever(ping_interval=20, ping_timeout=10)
            finally:
                with self.lock:
                    self.connected = False
                if self.subscription_thread:
                    self.subscription_thread.join(timeout=1)
            self.stop.wait(3)

    def ingest(self, trades):
        now = time.time()
        cutoff = now - 300
        with self.lock:
            for trade in trades:
                try:
                    coin = str(trade["coin"])
                    outcome, side = divmod(int(coin[1:]), 10)
                    if coin.startswith("#") and outcome in self.rows and side in (0, 1):
                        stamp = float(trade["time"]) / 1000.0
                        if not math.isfinite(stamp) or stamp < cutoff:
                            continue
                        # flow_metrics only reads the last five minutes; keeping
                        # older trades would make every snapshot scan them.
                        history = self.history[coin]
                        while history and float(history[0]["time"]) / 1000.0 < cutoff:
                            history.popleft()
                        history.append(dict(trade))
                        self.updated_at = now
                except (KeyError, TypeError, ValueError):
                    continue

    def _book(self, coin):
        with self.lock:
            cached = self.books.get(coin)
        if cached and time.time()-cached[0] < 1:
            return cached[1]
        try:
            r = _http().post("https://api.hyperliquid.xyz/info", json={"type": "l2Book", "coin": coin}, timeout=3)
            r.raise_for_status()
            book = r.json()
            with self.lock:
                self.books[coin] = (time.time(), book)
            return book
        except (requests.RequestException, ValueError):
            return None

    def snapshot(self, side="no", order_usd=50, max_spread_bps=10, *, account=None):
        from zeus.outcome_costs import maker_profile
        base_rate = maker_profile(account)
        # Share concurrent previews/rotation scans without extending freshness.
        with self.snapshot_lock:
            with self.lock:
                key = (side, order_usd, max_spread_bps, base_rate, self.generation, self.catalog_revision)
                reusable = (self.connected and time.time()-self.catalog_at < 120
                            and len(self.subscribed) == 2*len(self.rows))
            entry = self.snapshot_cache.get(key)
            cached_fresh = entry and all(
                time.time()-r['book_updated_at'] <= 5 and
                r['last_trade_age_sec']+time.time()-entry[1]['updated_at'] <= 20
                for r in entry[1].get('candidates', []))
            if reusable and entry and cached_fresh and time.monotonic()-entry[0] < .4:
                return copy.deepcopy(entry[1])
            started = time.perf_counter()
            result = self._snapshot(side, order_usd, max_spread_bps, base_rate=base_rate)
            result['scan_ms'] = (time.perf_counter()-started)*1000
            if len(self.snapshot_cache) >= 16:
                self.snapshot_cache.clear()
            self.snapshot_cache[key] = (time.monotonic(), result)
            return copy.deepcopy(result)

    def _snapshot(self, side="no", order_usd=50, max_spread_bps=10, *, base_rate=0.0004):
        from zeus.outcome_costs import candidate_cost
        if side not in ("yes", "no"):
            raise ValueError("Invalid Outcome side")
        now = time.time()
        with self.lock:
            ready = (self.connected and bool(self.rows) and len(self.subscribed) == 2*len(self.rows)
                     and now-self.connected_at >= 60 and now-self.catalog_at < 120)
            generation = self.generation
            status = {"ready": False, "connected": self.connected,
                      "warming_up": self.connected and (now-self.connected_at < 60 or len(self.subscribed) != 2*len(self.rows)),
                      "catalog_stale": now-self.catalog_at >= 120,
                      "observed_sec": max(0, now-self.connected_at) if self.connected else 0,
                      "markets_observed": len(self.subscribed)//2, "markets_total": len(self.rows), "updated_at": now,
                      "error": self.error, "candidates": [], "side": side}
            suffix = "0" if side == "yes" else "1"
            history = {}
            for coin, trades in self.history.items():
                if not coin.endswith(suffix) or not trades:
                    continue
                try:
                    newest = float(trades[-1]["time"]) / 1000.0
                except (KeyError, TypeError, ValueError):
                    continue
                # A market with no trade in five minutes cannot be eligible;
                # skip copying its (potentially large) deque entirely.
                if not math.isfinite(newest) or now - newest > 300:
                    continue
                history[coin] = list(trades)
            rows = dict(self.rows)
        if not ready:
            return status
        shortlist = []
        for coin, trades in history.items():
            row = rows.get(int(coin[1:])//10)
            if not row or not before_event(row, now):
                continue
            metric = flow_metrics(trades, now)
            if metric and metric["eligible"]:
                shortlist.append({**metric, "market": coin, "name": row["name"],
                                  "fee_scale": row.get("fee_scale"),
                                  "deployer_fee_scale": row.get("deployer_fee_scale")})
        shortlist.sort(key=lambda r: (-r["volume_usd_1m"], -r["score"], r["market"]))
        # Dollar/minute first; retain freshness, spread, depth and two-way guards.
        targets = shortlist[:24]
        pool = self._book_executor()
        if pool is None:
            return {**status, "connected": False}
        with self.book_lock:
            books = list(pool.map(self._book, [r["market"] for r in targets]))
        candidates = []
        fees_not_covered = 0
        cost_unavailable = 0
        for item, book in zip(targets, books):
            try:
                bids, asks = book["levels"]
                bid, ask = float(bids[0]["px"]), float(asks[0]["px"])
                bq, aq = float(bids[0]["sz"]), float(asks[0]["sz"])
                age = time.time()-float(book["time"])/1000
                spread = 10000*(ask-bid)/((bid+ask)/2)
                if (not all(map(math.isfinite, (bid, ask, bq, aq, age, spread)))
                        or not 0 < bid < ask < 1 or not -1 <= age <= 5
                        or min(bq*bid, aq*ask) < order_usd or spread > max_spread_bps):
                    continue
                # Queue clearance proxy, not a fill promise; use token flow so
                # quote token prices cannot distort the displayed queue size.
                wait_buy = 60*(bq+order_usd/bid)/item["sell_tokens_1m"]
                wait_sell = 60*(aq+order_usd/ask)/item["buy_tokens_1m"]
                item.update(bid=bid, ask=ask, spread_bps=spread,
                    depth_bid_usd=bq*bid, depth_ask_usd=aq*ask,
                    book_updated_at=float(book['time'])/1000,
                    queue_proxy_sec=max(wait_buy, wait_sell))
                try:
                    item.update(candidate_cost(item, base_rate))
                except (ValueError, TypeError, KeyError):
                    item.update(maker_close_fee_bps=None, net_touch_edge_bps=None,
                                fees_covered=None, cost_status='unavailable')
                    cost_unavailable += 1
                else:
                    fees_not_covered += not item['fees_covered']
                # Cost is informational in volume mode. Neither a negative
                # estimate nor unavailable fee metadata rejects eligible flow.
                item["score"] /= (1+max(wait_buy, wait_sell)/60)*(1+spread/10)
                candidates.append(item)
            except (KeyError, TypeError, ValueError, IndexError, ZeroDivisionError):
                continue
        # Fills first: the shortest estimated touch queue wins. The balanced-flow
        # score breaks ties so a dead or one-way book never leads on a stale quote.
        candidates.sort(key=lambda r: (r["queue_proxy_sec"], -r["score"],
                                       -r["volume_usd_1m"], r["market"]))
        with self.lock:
            if not self.connected or self.generation != generation or time.time()-self.catalog_at >= 120:
                return {**status, "connected": False}
            candidates = [r for r in candidates if int(r["market"][1:])//10 in self.rows]
        candidates = [r for r in candidates if r["last_trade_age_sec"]+time.time()-now <= 20]
        completed = time.time()
        for row in candidates:
            row['last_trade_age_sec'] += completed-now
        return {**status, "ready": bool(candidates), "warming_up": False, "candidates": candidates,
                "fees_not_covered": fees_not_covered, "cost_unavailable": cost_unavailable,
                "cost_policy": "informational",
                "updated_at": completed, "ranking": "queue_proxy_sec",
                "flow_candidates": len(shortlist), "books_checked": len(books)}

    def close(self):
        self.stop.set()
        if self.ws:
            self.ws.close()
        if self.thread:
            self.thread.join(timeout=4)
        with self.lock:
            self.connected = False
            pool, self.book_pool = self.book_pool, None
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)


scanner = OutcomeFlowScanner()


# --- merged from outcome_policy.py ---

"""User exclusions shared by Outcome discovery and the final worker admission."""
import re


# All children of the currently listed FOMC rate-decision question, both sides.
FED_RATE_OUTCOME_IDS = frozenset({19987, 19988, 19989, 19990})


def blocked_outcome(row):
    try:
        outcome_id = int(row.get('id', row.get('outcome_id', 0)))
    except (TypeError, ValueError):
        outcome_id = 0
    text = ' '.join(str(row.get(key) or '') for key in
                    ('name', 'raw_name', 'question_title', 'question_description', 'description'))
    fed = re.search(r'\b(?:fed|fomc|federal reserve|federal open market committee|federal funds)\b', text, re.I)
    rates = re.search(r'\b(?:rate|rates|interest|policyRate\w*)\b', text, re.I)
    return outcome_id in FED_RATE_OUTCOME_IDS or bool(fed and rates)


def assert_coin_allowed(coin):
    if coin.startswith('#') and coin[1:].isdigit() and int(coin[1:])//10 in FED_RATE_OUTCOME_IDS:
        raise ValueError('Fed interest-rate markets are excluded')

# --- merged from outcome_selection.py ---

"""Pure, bounded Auto scheduling heuristics, separate from order execution."""
import math


def no_fill_grace(config, selection):
    window = float(config.get('auto_rotation_after_sec', 180))
    queue = float((selection or {}).get('queue_proxy_sec') or 0)
    # Give a newly joined queue a reasonable chance; never extend the user's
    # configured observation window. This remains an estimate, not a promise.
    return min(window, max(60, queue*1.25))


def candidate_rank(candidate, results, now):
    previous = results.get(candidate['market'], {})
    failures = min(3, previous.get('zero_fill_attempts', 0))
    age = max(0, now-previous.get('updated_at', now))
    penalty = 1+failures*math.exp(-age/1800)
    return (float(candidate.get('queue_proxy_sec', math.inf))*penalty,
            -float(candidate.get('score', 0)), -float(candidate.get('volume_usd_1m', 0)), candidate['market'])
