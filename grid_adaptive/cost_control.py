"""Offline-testable accounting, order reconciliation and execution journal.

No credentials, SDK clients or exchange writes are created by this module.
OrderBook is owned by the bot: all access must be under the bot's state lock.
"""

import json
import math
import queue
import threading
import time
from pathlib import Path


def cost_metrics(volume, fees, realized, unrealized_delta=0.0):
    scale = 1_000_000.0 / volume if volume > 0 else None
    net = realized - fees
    return {
        "fee_cpm": fees * scale if scale is not None else None,
        "trading_loss_cpm": -realized * scale if scale is not None else None,
        "realized_cpm": -net * scale if scale is not None else None,
        "mtm_cpm": -(net + unrealized_delta) * scale if scale is not None else None,
        "net_realized": net,
        "net_mtm": net + unrealized_delta,
    }


def fill_key(fill):
    # The same execution is sent on userEvents and userFills. Missing tids
    # must not collapse every execution into the string 'None'.
    if fill.get("tid") is not None:
        return (str(fill.get("coin", "")), str(fill["tid"]))
    return tuple(str(fill.get(k, "")) for k in
                 ("coin", "hash", "oid", "time", "side", "px", "sz"))


def update_entry(position, entry, signed_size, price):
    """Local cost basis for mid-marked PnL between account REST snapshots."""
    after = position + signed_size
    if abs(after) < 1e-12:
        return 0.0
    if position == 0 or position * signed_size > 0:
        return (abs(position) * entry + abs(signed_size) * price) / abs(after)
    return price if position * after < 0 else entry


class OrderBook:
    def __init__(self):
        self.orders = {}
        self.terminal = set()
        self.filled = {}
        self.revision = 0
        self.inflight = 0
        self.uncertain = False

    def remove(self, oid):
        self.terminal.add(oid)
        self.orders.pop(oid, None)
        self.revision += 1

    def on_fill(self, oid, size):
        self.filled[oid] = self.filled.get(oid, 0.0) + size
        if oid in self.orders:
            order = self.orders[oid]
            # WS open updates may already report the remaining quantity after
            # this fill. Bound from original request size, not remaining-size
            # minus fill again (which would double-subtract reordered events).
            if "total" in order:
                computed = order["total"] - (self.filled[oid] - order["fill_base"])
                order["sz"] = max(0.0, min(order["sz"], computed))
            else:
                order["sz"] = max(0.0, order["sz"] - size)
            if order["sz"] < 1e-12:
                self.remove(oid)
        self.revision += 1

    def confirm(self, oid, req, filled_before):
        # A terminal WS event can arrive before the HTTP 'resting' response.
        if oid in self.terminal:
            return
        remaining = max(0.0, req["sz"] - (
            self.filled.get(oid, 0.0) - filled_before.get(oid, 0.0)))
        old = self.orders.get(oid)
        if old:
            remaining = min(remaining, old["sz"])
        if remaining < 1e-12:
            self.remove(oid)
            return
        self.orders[oid] = {"is_buy": req["is_buy"], "px": req["limit_px"],
                            "sz": remaining, "reduce_only": req["reduce_only"],
                            "total": req["sz"], "fill_base": filled_before.get(oid, 0.0)}
        self.revision += 1

    def on_open(self, order):
        oid = int(order["oid"])
        if oid in self.terminal:
            return
        old = self.orders.get(oid, {})
        remaining = float(order["sz"])
        if old:
            remaining = min(remaining, old["sz"])
        # WS does not always carry reduceOnly. Unknown orders are cancelled
        # conservatively, never silently treated as safe reduce-only orders.
        self.orders[oid] = {
            **old,
            "is_buy": order.get("side") == "B", "px": float(order["limitPx"]),
            "sz": remaining, "reduce_only": old.get("reduce_only", order.get("reduceOnly")),
        }
        self.revision += 1

    def snapshot(self, orders):
        previous = self.orders
        self.orders = {}
        for order in orders:
            oid = int(order["oid"])
            if oid in self.terminal:
                continue
            old = previous.get(oid, {})
            self.orders[oid] = {
                **old,
                "is_buy": order.get("side") == "B", "px": float(order["limitPx"]),
                "sz": min(float(order["sz"]), old.get("sz", math.inf)),
                "reduce_only": old.get("reduce_only", order.get("reduceOnly")),
            }
        self.uncertain = False
        self.revision += 1

    def diff(self, desired, tick, size_step):
        """Match each desired order at most once; preserve exact resting legs."""
        missing = list(desired)
        cancel = []
        for oid, current in self.orders.items():
            match = next((i for i, wanted in enumerate(missing)
                          if current["is_buy"] == wanted["is_buy"]
                          and current["reduce_only"] == wanted["reduce_only"]
                          and abs(current["px"] - wanted["limit_px"]) < tick * 0.1
                          and abs(current["sz"] - wanted["sz"]) < size_step * 0.1), None)
            if match is None:
                cancel.append(oid)
            else:
                missing.pop(match)
        return cancel, missing


class ExecutionJournal:
    """Disk IO stays off the trading/WS threads; logging failure is observable."""
    def __init__(self, directory, symbol):
        self.error = ""
        self.dropped = 0
        self.queue = queue.Queue(maxsize=20000)
        self.path = Path(directory) / (
            f"{symbol.replace(':', '_')}_{time.time_ns()}.jsonl")
        self.thread = threading.Thread(target=self._write, daemon=True)
        self.thread.start()

    def emit(self, event, **fields):
        try:
            self.queue.put_nowait({"event": event, "ts": time.time(), **fields})
        except queue.Full:
            self.dropped += 1

    def _write(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as output:
                while True:
                    item = self.queue.get()
                    if item is None:
                        break
                    output.write(json.dumps(item, ensure_ascii=False, allow_nan=False) + "\n")
                    output.flush()
        except Exception as exc:
            self.error = f"journal unavailable: {type(exc).__name__}"

    def close(self):
        try:
            self.queue.put(None, timeout=1)
        except queue.Full:
            self.dropped += 1
        self.thread.join(timeout=2)


def replay_journal(path):
    """Accounting replay only; does not simulate alternative order fills."""
    seen = set()
    volume = fees = realized = 0.0
    unrealized_delta = 0.0
    fills = 0
    with open(path, encoding="utf-8") as source:
        for line in source:
            row = json.loads(line)
            if row.get("event") == "metrics":
                unrealized_delta = row.get("unrealized_delta", 0.0)
            if row.get("event") != "fill":
                continue
            key = tuple(row["key"])
            if key in seen:
                continue
            seen.add(key)
            volume += row["px"] * row["sz"]
            fees += row["fee"]
            realized += row["closed_pnl"]
            fills += 1
    return {"fills": fills, "volume": volume, "fees": fees, "realized": realized,
            **cost_metrics(volume, fees, realized, unrealized_delta)}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Offline accounting replay (no trading)")
    parser.add_argument("journal")
    print(json.dumps(replay_journal(parser.parse_args().journal), indent=2))
