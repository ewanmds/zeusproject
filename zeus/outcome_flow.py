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

# A modest cadence relaxation; the one-minute rate window and all book guards
# stay unchanged. Share the age limit with cached/final candidate validation.
FLOW_LIMITS = {
    'min_event_seconds': 4,
    'min_span_sec': 20,
    'max_trade_age_sec': 45,
    'min_active_bins_1m': 2,
    'min_events_per_side_1m': 1,
    'max_median_gap_sec': 45,
    # A high-volume two-way market can look "slow" only because its last
    # minute contains one burst. Keep the steady path unchanged and add a
    # material-flow path that still requires 5-minute recurrence.
    'volume_exception_usd_1m': 250.0,
    'volume_exception_min_side_usd_1m': 25.0,
    'volume_exception_min_side_share': 0.10,
    'volume_exception_min_bins_5m': 2,
}
NO_ELIGIBLE_FLOW_REASON = (
    'Flux trop irrégulier pour Auto : dernier trade ≤45 s et activité dans les deux sens sur 60 s.'
)


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
    bins_5m = {int((now-t)//10) for t, _, _, _ in values}
    directions = {s: len({int(t) for t, _, _, side in recent if side == s}) for s in ("A", "B")}
    age = max(0, now-values[-1][0])
    span = values[-1][0]-values[0][0]
    gaps = [b-a for a, b in zip(events, events[1:])]
    cadence = statistics.median(gaps) if gaps else 300
    # Harmonic flow penalizes a stream that lets us enter but never exit.
    balanced = 2*buy*sell/(buy+sell) if buy+sell else 0
    coverage = min(1, len(bins)/6)
    score = balanced * coverage * math.exp(-age/20)
    volume = buy+sell
    min_side = min(buy, sell)
    steady = (len(events) >= FLOW_LIMITS['min_event_seconds']
              and span >= FLOW_LIMITS['min_span_sec']
              and age <= FLOW_LIMITS['max_trade_age_sec']
              and len(bins) >= FLOW_LIMITS['min_active_bins_1m']
              and min(directions.values()) >= FLOW_LIMITS['min_events_per_side_1m']
              and cadence <= FLOW_LIMITS['max_median_gap_sec'])
    # Material two-way flow: admit repeated, high-volume markets whose last
    # minute is one burst (one 10 s bin / few unique seconds). A single
    # same-second sweep is still rejected: it has one 5-minute bin and/or no
    # 20 s span. This path never relaxes age, book, spread or depth guards.
    material = (volume >= FLOW_LIMITS['volume_exception_usd_1m']
                and min_side >= max(FLOW_LIMITS['volume_exception_min_side_usd_1m'],
                                    FLOW_LIMITS['volume_exception_min_side_share']*volume)
                and span >= FLOW_LIMITS['min_span_sec']
                and age <= FLOW_LIMITS['max_trade_age_sec']
                and len(bins_5m) >= FLOW_LIMITS['volume_exception_min_bins_5m'])
    eligible = steady or material
    flow_mode = 'steady' if steady else 'burst' if material else 'none'
    return {"score": score, "eligible": eligible, "flow_mode": flow_mode,
            "trades_5m": len(values), "events_5m": len(events),
            "active_bins_1m": len(bins), "active_bins_5m": len(bins_5m),
            "span_sec": span, "min_side_usd_1m": min_side,
            "buy_usd_1m": buy, "sell_usd_1m": sell, "volume_usd_1m": volume,
            "balanced_usd_1m": balanced, "last_trade_age_sec": age,
            "buy_events_1m": directions['B'], "sell_events_1m": directions['A'],
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
        self.book_watch = {}
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
        self.book_subscribed = set()
        self.subscription_error = ""
        # A websocket book is the current state until the exchange pushes a
        # change. Its exchange timestamp is the last change, not an expiry.
        self.book_received = {}
        self.book_live = {}
        self.book_refresh_at = {}
        self.book_rate_lock = threading.Lock()
        self.book_next_rest = 0.0
        self.book_backoff_until = 0.0
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

    @staticmethod
    def _quorum(expected):
        """80% of trade subscriptions is enough; one lost ack must not block all."""
        return max(1, math.ceil(expected * 0.8)) if expected else 0

    def _subscription_ready_locked(self, now):
        expected = 2 * len(self.rows)
        return bool(self.connected and expected
                    and now - self.catalog_at < 120
                    and now - self.connected_at >= 60
                    and len(self.subscribed) >= self._quorum(expected))

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
            for coin in list(self.books):
                if int(coin[1:])//10 not in rows:
                    self.books.pop(coin, None)
                    self.book_received.pop(coin, None)
                    self.book_live.pop(coin, None)
                    self.book_refresh_at.pop(coin, None)
            self.subscribed = {c for c in self.subscribed if int(c[1:])//10 in rows}
            if self.thread is None or not self.thread.is_alive():
                self.stop.clear()
                self.thread = threading.Thread(target=self._run, name="outcome-flow", daemon=True)
                self.thread.start()

    def _run(self):
        import websocket
        while not self.stop.is_set():
            def opened(ws):
                self._mark_open()
                def subscribe():
                    # Receive snapshots while subscribing; do not burst 400 sends.
                    sent = set()
                    sent_books = {}
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
                            now = time.time()
                            with self.lock:
                                watched = {coin: at for coin, at in self.book_watch.items()
                                           if now-at < 20 and int(coin[1:])//10 in self.rows}
                                self.book_watch = watched
                                acked_books = set(self.book_subscribed)
                                # 450 outcomes * two trade subscriptions + 48
                                # L2 books stays below Hyperliquid's 1000-sub limit.
                                wanted_books = set(sorted(watched, key=watched.get, reverse=True)[:48])
                                # Quiet books legitimately send no updates. Ask
                                # the websocket for a fresh snapshot periodically
                                # instead of polling the REST API.
                                refresh_books = {
                                    coin for coin in wanted_books & acked_books
                                    if now - self.book_received.get(coin, 0) > 45
                                    and now - self.book_refresh_at.get(coin, 0) > 45}
                            # Unsubscribe from books no longer watched, including
                            # subscriptions the server never acknowledged.
                            for coin in sorted((set(sent_books) | acked_books) - wanted_books):
                                if self.stop.is_set() or self.ws is not ws or not self.connected:
                                    return
                                ws.send(json.dumps({'method': 'unsubscribe',
                                                    'subscription': {'type': 'l2Book', 'coin': coin}}))
                                sent_books.pop(coin, None)
                                with self.lock:
                                    self.book_subscribed.discard(coin)
                            # Retry a missing l2Book ack. A single dropped frame
                            # used to leave the scanner without spreads forever.
                            for coin in sorted(wanted_books - acked_books):
                                sent_at = sent_books.get(coin, 0)
                                if sent_at and now-sent_at < 5:
                                    continue
                                if self.stop.is_set() or self.ws is not ws or not self.connected:
                                    return
                                ws.send(json.dumps({'method': 'subscribe',
                                                    'subscription': {'type': 'l2Book', 'coin': coin}}))
                                sent_books[coin] = now
                                with self.lock:
                                    self.book_refresh_at[coin] = now
                            # Re-subscribing returns a full snapshot without any
                            # REST call. Staggered by the 45 s receipt window.
                            for coin in sorted(refresh_books):
                                if self.stop.is_set() or self.ws is not ws or not self.connected:
                                    return
                                ws.send(json.dumps({'method': 'subscribe',
                                                    'subscription': {'type': 'l2Book', 'coin': coin}}))
                                with self.lock:
                                    self.book_refresh_at[coin] = now
                            self.stop.wait(1)
                    except Exception:
                        failed(ws, None)
                        ws.close()
                self.subscription_thread = threading.Thread(target=subscribe,
                                        name="outcome-flow-subscribe", daemon=True)
                self.subscription_thread.start()

            def message(ws, raw):
                self._message(raw)

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

    def _mark_open(self):
        """Mark a websocket open without discarding short-gap observations."""
        now = time.time()
        with self.lock:
            self.connected = True
            # A fast reconnect keeps the original 60 s observation window and
            # all timestamped executed-flow history. A long outage restarts it.
            if not self.connected_at or now - self.connected_at > 120:
                self.connected_at = now
            self.generation += 1
            self.subscribed.clear()
            self.book_subscribed.clear()
            self.error = ""
            self.subscription_error = ""

    def _message(self, raw):
        try:
            payload = json.loads(raw)
            if payload.get('channel') == 'error':
                with self.lock:
                    self.subscription_error = str(payload.get('data') or '')[:160]
                    # Force a fresh round of l2Book subscriptions.
                    self.book_subscribed.clear()
                return
            if payload.get('channel') == 'l2Book':
                data = payload.get('data', {})
                if not isinstance(data, dict):
                    return
                coin = data.get('coin', '')
                now = time.time()
                with self.lock:
                    if (coin in self.book_watch or coin in self.book_subscribed
                            or int(coin[1:])//10 in self.rows):
                        self.books[coin] = (now, data)
                        self.book_received[coin] = now
                        self.book_live[coin] = self.generation
                        if coin in self.book_watch:
                            # A delivered book is proof the subscription works,
                            # even if its ack frame was lost.
                            self.book_subscribed.add(coin)
                return
            if payload.get("channel") == "subscriptionResponse":
                response = payload.get("data", {})
                subscription = response.get("subscription", {}) if isinstance(response, dict) else {}
                if subscription.get("type") == "l2Book":
                    with self.lock:
                        coin = subscription.get("coin", "")
                        if response.get("method") == "subscribe":
                            self.book_subscribed.add(coin)
                        else:
                            self.book_subscribed.discard(coin)
                elif subscription.get("type") == "trades":
                    with self.lock:
                        coin = subscription["coin"]
                        if (response.get("method") == "subscribe"
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

    def _rest_book_allowed(self):
        """Global REST budget: live websocket books need no HTTP polling."""
        now = time.monotonic()
        with self.book_rate_lock:
            if now < self.book_backoff_until or now < self.book_next_rest:
                return False
            self.book_next_rest = now + 1/3
            return True

    def _book(self, coin):
        now = time.time()
        with self.lock:
            cached = self.books.get(coin)
            received = self.book_received.get(coin, cached[0] if cached else 0)
            live = bool(cached and self.connected
                        and self.book_live.get(coin) == self.generation
                        and now-received <= 60)
        # An acknowledged l2Book subscription is the current book state.
        # Its exchange timestamp is the last change, not an expiry.
        if live:
            return cached[1]
        if cached and now-cached[0] < 1:
            return cached[1]
        if not self._rest_book_allowed():
            return cached[1] if cached else None
        try:
            r = _http().post("https://api.hyperliquid.xyz/info", json={"type": "l2Book", "coin": coin}, timeout=3)
            if r.status_code == 429:
                with self.book_rate_lock:
                    self.book_backoff_until = time.monotonic()+10
                return cached[1] if cached else None
            r.raise_for_status()
            book = r.json()
            received = time.time()
            with self.lock:
                self.books[coin] = (received, book)
                self.book_received[coin] = received
                self.book_live.pop(coin, None)
            return book
        except (requests.RequestException, ValueError):
            return cached[1] if cached else None

    def snapshot(self, side="no", order_usd=50, max_spread_bps=10, *, account=None):
        from zeus.outcome_costs import maker_profile
        base_rate = maker_profile(account)
        # Share concurrent previews/rotation scans without extending freshness.
        with self.snapshot_lock:
            now = time.time()
            with self.lock:
                key = (side, order_usd, max_spread_bps, base_rate, self.generation, self.catalog_revision)
                reusable = self._subscription_ready_locked(now)
            entry = self.snapshot_cache.get(key)
            cached_fresh = entry and all(
                time.time()-r['book_updated_at'] <= 5 and
                r['last_trade_age_sec']+time.time()-entry[1]['updated_at'] <= FLOW_LIMITS['max_trade_age_sec']
                for r in entry[1].get('candidates', []))
            if reusable and entry and cached_fresh and time.monotonic()-entry[0] < .4:
                return copy.deepcopy(entry[1])
            started = time.perf_counter()
            result = self._snapshot(side, order_usd, max_spread_bps, base_rate=base_rate)
            result['scan_ms'] = (time.perf_counter()-started)*1000
            with self.lock:
                if len(self.snapshot_cache) >= 16:
                    self.snapshot_cache.clear()
                self.snapshot_cache[key] = (time.monotonic(), result)
            return copy.deepcopy(result)

    def observations(self, side='no', order_usd=50, max_spread_bps=10, *, account=None):
        """Display every observed market; no HTTP, no admission changes."""
        from zeus.outcome_costs import candidate_cost, maker_profile
        now = time.time()
        suffix = '0' if side == 'yes' else '1'
        base_rate = maker_profile(account)
        with self.lock:
            rows = list(self.rows.values())
            history = {coin: list(values) for coin, values in self.history.items() if coin.endswith(suffix)}
            books = dict(self.books)
            book_received = dict(self.book_received)
            book_live = dict(self.book_live)
            generation = self.generation
            connected = self.connected
            cached = [value[1] for key, value in self.snapshot_cache.items()
                      if key[:3] == (side, order_usd, max_spread_bps)
                      and key[4:] == (self.generation, self.catalog_revision)]
        candidates = {c['market']: c for result in cached for c in result.get('candidates', [])
                      if now-c.get('book_updated_at', 0) <= 5
                      and c['last_trade_age_sec']+now-result['updated_at'] <= FLOW_LIMITS['max_trade_age_sec']}
        observed = []
        for row in rows:
            if not before_event(row, now):
                continue
            coin = f"#{10*int(row['id'])+int(suffix)}"
            metric = flow_metrics(history.get(coin, []), now)
            item = dict(market=coin, name=row['name'], volume_usd_1m=0,
                        spread_bps=None, net_touch_edge_bps=None, queue_proxy_sec=None)
            if metric:
                item.update(metric)
            reason = 'Aucun trade récent'
            if metric:
                if not metric['buy_events_1m'] or not metric['sell_events_1m']:
                    reason = 'Flux dans un seul sens ou absent sur 60 s'
                elif metric['last_trade_age_sec'] > FLOW_LIMITS['max_trade_age_sec']:
                    reason = 'Dernier trade trop ancien'
                elif not metric['eligible']:
                    reason = ('Flux trop bref (rafale unique)'
                              if metric['volume_usd_1m'] >= FLOW_LIMITS['volume_exception_usd_1m']
                              else 'Cadence encore insuffisante')
                else:
                    reason = 'Carnet à vérifier'
            book = books.get(coin)
            if book:
                try:
                    data = book[1]
                    bid, ask = data['levels'][0][0], data['levels'][1][0]
                    bp, ap = float(bid['px']), float(ask['px'])
                    bq, aq = float(bid['sz']), float(ask['sz'])
                    received = book_received.get(coin)
                    live = bool(connected and book_live.get(coin) == generation)
                    try:
                        exchange_age = now-float(data['time'])/1000
                    except (KeyError, TypeError, ValueError):
                        exchange_age = 0.0
                    book_age = max(0, now-received) if received is not None else max(0, exchange_age)
                    # A live websocket book is current until a change arrives.
                    # Display the last book for up to 60 s; a cold/REST book
                    # still needs a recent receipt for admission.
                    if 0 < bp < ap < 1 and book_age <= 60 and exchange_age >= -1:
                        item['spread_bps'] = 10000*(ap-bp)/((ap+bp)/2)
                        item['book_age_sec'] = book_age
                        if metric:
                            coverage = max(1/6, min(1, float(metric.get('active_bins_1m', 0))/6))
                            sell_tokens = (metric.get('sell_tokens_1m') or 0)*coverage
                            buy_tokens = (metric.get('buy_tokens_1m') or 0)*coverage
                            if sell_tokens > 0 and buy_tokens > 0:
                                item['queue_proxy_sec'] = max(
                                    60*(bq+order_usd/bp)/sell_tokens,
                                    60*(aq+order_usd/ap)/buy_tokens)
                        if base_rate is not None:
                            try:
                                item.update(candidate_cost(
                                    {**item, 'bid': bp, 'ask': ap,
                                     'fee_scale': row.get('fee_scale'),
                                     'deployer_fee_scale': row.get('deployer_fee_scale')},
                                    base_rate))
                            except (ValueError, TypeError, KeyError):
                                item.update(maker_close_fee_bps=None, net_touch_edge_bps=None,
                                            fees_covered=None, cost_status='unavailable')
                        if metric and metric['eligible']:
                            if not live and book_age > 5:
                                reason = 'Carnet ancien (%d s)' % round(book_age)
                            elif min(bq*bp, aq*ap) < order_usd:
                                reason = 'Profondeur insuffisante'
                            elif item['spread_bps'] > max_spread_bps:
                                reason = 'Spread trop large'
                except (KeyError, IndexError, TypeError, ValueError, ZeroDivisionError):
                    pass
            approved = candidates.get(coin) if connected and metric and metric['eligible'] else None
            if approved:
                # Fee estimates belong to this tenant's full scan.
                for key in ('spread_bps', 'queue_proxy_sec', 'net_touch_edge_bps',
                            'maker_close_fee_bps', 'fees_covered'):
                    if approved.get(key) is not None:
                        item[key] = approved.get(key)
                reason = 'Admissible'
            item.update(auto_eligible=bool(approved),
                        eligibility_reason=reason if connected else 'Flux déconnecté')
            observed.append(item)
        observed.sort(key=lambda row: (-row['volume_usd_1m'], row['market']))
        with self.lock:
            for row in observed[:24]:
                self.book_watch[row['market']] = now
        return dict(observed_markets=observed, observed_at=now,
                    observation_connected=connected)

    def _snapshot(self, side="no", order_usd=50, max_spread_bps=10, *, base_rate=0.0004):
        from zeus.outcome_costs import candidate_cost
        if side not in ("yes", "no"):
            raise ValueError("Invalid Outcome side")
        now = time.time()
        with self.lock:
            expected = 2 * len(self.rows)
            quorum = self._quorum(expected)
            observed_ids = {int(c[1:])//10 for c in self.subscribed
                            if c.startswith('#') and c[1:].isdigit()}
            warmup_done = now - self.connected_at >= 60
            ready = self._subscription_ready_locked(now)
            generation = self.generation
            status = {"ready": False, "connected": self.connected,
                      "warming_up": bool(self.connected and self.rows and not warmup_done),
                      "subscriptions_pending": bool(self.connected and self.rows and warmup_done
                                                    and len(self.subscribed) < quorum),
                      "subscriptions_ready": len(self.subscribed),
                      "subscriptions_expected": expected,
                      "subscription_error": self.subscription_error,
                      "catalog_stale": now-self.catalog_at >= 120,
                      "observed_sec": max(0, now-self.connected_at) if self.connected else 0,
                      "markets_observed": len(observed_ids), "markets_total": len(self.rows), "updated_at": now,
                      "error": self.error, "candidates": [], "side": side,
                      "flow_thresholds": dict(FLOW_LIMITS)}
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
            book_received = dict(self.book_received)
            book_live = dict(self.book_live)
            connected = status["connected"]
        def not_ready(detail=""):
            if status.get("error"):
                status["reason"] = "Scanner indisponible: " + str(status["error"])
            elif not status.get("connected"):
                status["reason"] = "Flux public déconnecté; reconnexion en cours"
            elif status.get("catalog_stale"):
                status["reason"] = "Catalogue HIP-4 ancien (plus de 120 s); rechargement"
            elif status.get("subscriptions_pending"):
                status["reason"] = ("Souscriptions en cours: %d/%d flux"
                                    % (status.get("subscriptions_ready", 0),
                                       status.get("subscriptions_expected", 0)))
            elif status.get("warming_up"):
                status["reason"] = "Observation initiale: %d s / 60 s" % int(status.get("observed_sec") or 0)
            elif detail:
                status["reason"] = "Aucun candidat: " + detail
            else:
                status["reason"] = NO_ELIGIBLE_FLOW_REASON
            status["order_usd"] = order_usd
            status["max_spread_bps"] = max_spread_bps
            status["min_depth_usd"] = order_usd
            return status

        if not ready:
            return not_ready()
        shortlist = []
        skipped_events = 0
        for coin, trades in history.items():
            row = rows.get(int(coin[1:])//10)
            if not row or not before_event(row, now):
                skipped_events += 1
                continue
            metric = flow_metrics(trades, now)
            if metric and metric["eligible"]:
                shortlist.append({**metric, "market": coin, "name": row["name"],
                                  "fee_scale": row.get("fee_scale"),
                                  "deployer_fee_scale": row.get("deployer_fee_scale")})
        shortlist.sort(key=lambda r: (-r["volume_usd_1m"], -r["score"], r["market"]))
        # Dollar/minute first; retain freshness, spread, depth and two-way guards.
        targets = shortlist[:24]
        # Mark the exact shortlist for the shared L2 subscription so the next
        # scan reads the websocket cache instead of 24 serialized REST books.
        with self.lock:
            for item in targets:
                self.book_watch[item["market"]] = now
        pool = self._book_executor()
        if pool is None:
            status["connected"] = False
            return not_ready("pool de lecture des carnets indisponible")
        with self.book_lock:
            books = list(pool.map(self._book, [r["market"] for r in targets]))
        candidates = []
        fees_not_covered = 0
        cost_unavailable = 0
        rejected_book = rejected_depth = rejected_spread = 0
        widest_spread = 0.0
        deepest_book = 0.0
        book_now = time.time()
        for item, book in zip(targets, books):
            if not isinstance(book, dict):
                rejected_book += 1
                continue
            try:
                bids, asks = book["levels"]
                bid, ask = float(bids[0]["px"]), float(asks[0]["px"])
                bq, aq = float(bids[0]["sz"]), float(asks[0]["sz"])
                coin = item["market"]
                exchange_age = book_now-float(book["time"])/1000
                received = book_received.get(coin)
                live = bool(connected and book_live.get(coin) == generation)
                # A live l2Book is current until the exchange pushes a change.
                # Only cold/REST books and old connections use a short age.
                age = book_now-received if received is not None else exchange_age
                stale = (not (live and age <= 60)) if received is not None else not (-1 <= age <= 5)
                spread = 10000*(ask-bid)/((bid+ask)/2)
                depth = min(bq*bid, aq*ask)
                if (not all(map(math.isfinite, (bid, ask, bq, aq, exchange_age, spread)))
                        or not 0 < bid < ask < 1 or exchange_age < -1 or stale):
                    rejected_book += 1
                    continue
                widest_spread = max(widest_spread, spread)
                deepest_book = max(deepest_book, depth)
                if depth < order_usd:
                    rejected_depth += 1
                    continue
                if spread > max_spread_bps:
                    rejected_spread += 1
                    continue
                # Queue clearance proxy, not a fill promise; use token flow so
                # quote token prices cannot distort the displayed queue size.
                # A one-bin burst overstates the flow that remains available.
                coverage = max(1/6, min(1, float(item.get("active_bins_1m", 0))/6))
                sell_tokens = max(1e-9, item["sell_tokens_1m"]*coverage)
                buy_tokens = max(1e-9, item["buy_tokens_1m"]*coverage)
                wait_buy = 60*(bq+order_usd/bid)/sell_tokens
                wait_sell = 60*(aq+order_usd/ask)/buy_tokens
                item.update(bid=bid, ask=ask, spread_bps=spread,
                    depth_bid_usd=bq*bid, depth_ask_usd=aq*ask,
                    book_updated_at=book_now if live else (
                        received if received is not None else float(book['time'])/1000),
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
        candidates = [r for r in candidates if
                      r["last_trade_age_sec"]+time.time()-now <= FLOW_LIMITS['max_trade_age_sec']]
        completed = time.time()
        for row in candidates:
            row['last_trade_age_sec'] += completed-now
        reason = ""
        if not candidates:
            parts = []
            if len(shortlist):
                parts.append("%d marché(s) avec flux" % len(shortlist))
            if rejected_book:
                parts.append("%d carnet(s) absent(s) ou périmé(s)" % rejected_book)
            if rejected_depth:
                parts.append("%d profondeur(s) < %g $ (max %g $)"
                             % (rejected_depth, order_usd, round(deepest_book, 2)))
            if rejected_spread:
                parts.append("%d spread(s) > %g bps (min observé %g bps)"
                             % (rejected_spread, max_spread_bps, round(widest_spread, 2)))
            if not parts and skipped_events:
                parts.append("%d marché(s) sans flux récent" % skipped_events)
            reason = ("Aucun candidat: " + "; ".join(parts)) if parts else \
                     NO_ELIGIBLE_FLOW_REASON
        return {**status, "ready": bool(candidates), "warming_up": False, "candidates": candidates,
                "reason": reason,
                "fees_not_covered": fees_not_covered, "cost_unavailable": cost_unavailable,
                "cost_policy": "informational",
                "order_usd": order_usd, "max_spread_bps": max_spread_bps,
                "min_depth_usd": order_usd,
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
