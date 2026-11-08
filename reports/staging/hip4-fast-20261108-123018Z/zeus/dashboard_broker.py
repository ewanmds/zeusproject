"""Shared, versioned, bounded-cost dashboard reads for REST and WebSocket.

No background loop or exchange I/O. Each source has an independent lock/cache;
multiple tabs reuse one collection instead of rereading every session file.
"""
import copy
import threading
import time
import uuid


class DashboardBroker:
    def __init__(self, sources, *, ttl=.35):
        self.sources = sources
        self.ttl = ttl
        self.source_id = uuid.uuid4().hex
        self.locks = {kind: threading.Lock() for kind in sources}
        self.entries = {}
        self.revisions = {kind: 0 for kind in sources}

    def read(self, kind, *, fresh=False):
        with self.locks[kind]:
            entry = self.entries.get(kind)
            now = time.monotonic()
            if fresh or entry is None or now-entry[0] >= self.ttl:
                started = time.perf_counter()
                data = self.sources[kind]()
                self.revisions[kind] += 1
                data = {**data, "source_id": self.source_id, "revision": self.revisions[kind],
                        "published_at": time.time(), "collection_ms": (time.perf_counter()-started)*1000}
                entry = self.entries[kind] = (time.monotonic(), data)
            return copy.deepcopy(entry[1])

    def invalidate(self, kind):
        with self.locks[kind]:
            self.entries.pop(kind, None)
