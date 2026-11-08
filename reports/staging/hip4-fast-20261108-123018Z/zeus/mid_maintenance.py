"""Read-only MID/BBO maintenance. The trading loop alone applies its results."""

from __future__ import annotations

from dataclasses import dataclass, replace
import threading
import time

from zeus.safety import finite, safe_error


@dataclass(frozen=True)
class MaintenanceRequest:
    session_id: str
    revision: int
    in_flight: bool
    position: bool = False
    orders: bool = False
    # The position component is fenced independently from order actions: a
    # quote place/cancel does not change inventory, so it must not discard a
    # fresh position read. 0 keeps backward compatibility for manual callers.
    position_revision: int = 0
    # Monotonic counter bumped by every authoritative position read. A newer
    # foreground ::sync_position invalidates a background result captured before it.
    position_generation: int = 0
    # Tracked-order fingerprint at request time. Order-action begin/end bumps
    # must not discard an order snapshot; only an actual tracking change does.
    orders_fingerprint: tuple = ()


@dataclass(frozen=True)
class PositionRead:
    quantity: float
    entry_price: float
    unrealized_pnl: float
    perp_equity: float


@dataclass(frozen=True)
class OrderRead:
    oid: int
    side: str
    price: float
    size: float
    cloid: str | None = None
    reduce_only: bool | None = None


@dataclass(frozen=True)
class MaintenanceResult:
    request: MaintenanceRequest
    position: PositionRead | None = None
    orders: tuple[OrderRead, ...] | None = None
    position_started: float | None = None
    orders_started: float | None = None
    spot_usdc: float | None = None
    spot_error: str = ""
    error: str = ""


def parse_position(state, symbol):
    if not isinstance(state, dict) or not isinstance(state.get("assetPositions"), list):
        raise ValueError("Invalid maintenance position response")
    matches = []
    for row in state["assetPositions"]:
        pos = row.get("position") if isinstance(row, dict) else None
        if not isinstance(pos, dict) or not isinstance(pos.get("coin"), str):
            raise ValueError("Invalid maintenance position row")
        if pos["coin"] in (symbol, symbol.split(":")[-1]):
            matches.append(pos)
    if len(matches) > 1:
        raise ValueError("Duplicate maintenance position")
    pos = matches[0] if matches else {}
    margin = state.get("marginSummary", {})
    if not isinstance(margin, dict):
        raise ValueError("Invalid maintenance margin summary")
    return PositionRead(finite(pos.get("szi", 0)), finite(pos.get("entryPx", 0) or 0),
                        finite(pos.get("unrealizedPnl", 0)), finite(margin.get("accountValue", 0)))


def parse_orders(rows, symbol):
    if not isinstance(rows, list):
        raise ValueError("Invalid maintenance orders response")
    result = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("coin"), str):
            raise ValueError("Invalid maintenance order row")
        if row["coin"] not in (symbol, symbol.split(":")[-1]):
            continue
        raw_oid = row.get("oid")
        if isinstance(raw_oid, bool) or not str(raw_oid).isdigit():
            raise ValueError("Invalid maintenance order identity")
        oid = int(raw_oid)
        side, price, size = row.get("side"), finite(row["limitPx"]), finite(row["sz"])
        if oid <= 0 or oid in seen or side not in {"B", "A"} or price <= 0 or size < 0:
            raise ValueError("Invalid maintenance order")
        seen.add(oid)
        reduce_only = row.get("reduceOnly")
        result.append(OrderRead(oid, side, price, size, row.get("cloid"),
                                reduce_only if isinstance(reduce_only, bool) else None))
    return tuple(result)


class MidMaintenance:
    """One active read, one coalesced request and one immutable result slot.

    The HTTP client is created, used and closed by this thread only. There is
    no exchange client or reference to the mutable trading state here.
    """

    def __init__(self, base_url, account, symbol, wake, metrics=None, client_factory=None, halt_event=None):
        self.base_url, self.account, self.symbol = base_url, account, symbol
        self.dex = symbol.split(":")[0] if ":" in symbol else ""
        self.wake, self.metrics = wake, metrics
        self.client_factory = client_factory
        self.halt_event = halt_event
        self.condition = threading.Condition()
        self.stop_event = threading.Event()
        self.pending = self.active = self.result = None
        self.thread = threading.Thread(target=self._run, name="mm-rest-maintenance", daemon=True)
        self.thread.start()

    def _halted(self):
        return self.stop_event.is_set() or self.halt_event is not None and self.halt_event.is_set()

    def request(self, request):
        with self.condition:
            if self._halted():
                return False
            if self.pending is not None:
                request = replace(request, position=request.position or self.pending.position,
                                  orders=request.orders or self.pending.orders)
                if self.metrics:
                    self.metrics.count("maintenance_requests_coalesced")
            self.pending = request
            self.condition.notify()
        return True

    def poll(self):
        with self.condition:
            result, self.result = self.result, None
            return result

    def state(self):
        with self.condition:
            return {"active": self.active is not None, "pending": self.pending is not None,
                    "result_waiting": self.result is not None, "alive": self.thread.is_alive(),
                    "stopping": self._halted()}

    def close(self, timeout=1):
        with self.condition:
            self.stop_event.set()
            self.pending = self.result = None
            self.condition.notify_all()
        self.thread.join(timeout)
        return not self.thread.is_alive()

    def _post(self, client, kind, **extra):
        if self._halted():
            raise InterruptedError("Maintenance stopped before read")
        started = time.perf_counter_ns()
        try:
            return client.post("/info", {"type": kind, "user": self.account, **extra})
        finally:
            if self.metrics:
                self.metrics.observe("maintenance_" + kind, (time.perf_counter_ns() - started) / 1e6)
                self.metrics.count("maintenance_read_requests")

    def _read(self, client, request):
        position = orders = spot = position_started = orders_started = None
        spot_error = ""
        if request.position:
            position_started = time.monotonic()
            position = parse_position(self._post(client, "clearinghouseState", dex=self.dex), self.symbol)
        if request.orders:
            orders_started = time.monotonic()
            orders = parse_orders(self._post(client, "openOrders", dex=self.dex), self.symbol)
        if request.position:
            try:
                state = self._post(client, "spotClearinghouseState")
                if not isinstance(state, dict) or not isinstance(state.get("balances"), list):
                    raise ValueError("Invalid maintenance spot response")
                spot = sum(max(0, finite(row["total"]) - finite(row.get("hold", 0)))
                           for row in state["balances"] if row.get("coin") == "USDC")
            except Exception as exc:
                # Optional display data never changes critical account certainty.
                spot_error = safe_error(exc)
        return MaintenanceResult(request, position, orders, position_started, orders_started, spot, spot_error)

    def _run(self):
        client = None
        try:
            while True:
                with self.condition:
                    self.condition.wait_for(lambda: self.pending is not None or self.stop_event.is_set())
                    if self._halted():
                        return
                    request, self.pending = self.pending, None
                    self.active = request
                try:
                    if client is None:
                        if self.client_factory:
                            client = self.client_factory()
                        else:
                            from hyperliquid.api import API
                            client = API(self.base_url, timeout=10)
                    result = self._read(client, request)
                except Exception as exc:
                    result = MaintenanceResult(request, error=safe_error(exc))
                with self.condition:
                    self.active = None
                    if self._halted():
                        return
                    if self.result is not None and self.metrics:
                        self.metrics.count("maintenance_results_superseded")
                    self.result = result
                self.wake.set()
        finally:
            if client is not None:
                try:
                    client.session.close()
                except Exception:
                    pass
