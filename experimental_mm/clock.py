"""Server clock anchored to monotonic time; independent of Windows wall drift."""
from email.utils import parsedate_to_datetime
import math
import time
import threading


class ExchangeClock:
    def __init__(self):
        self.anchor = None
        self.monotonic_anchor = None
        self.lock = threading.Lock()

    def observe(self, date_header, book_time_ms, sent_monotonic, received_monotonic):
        rtt = received_monotonic - sent_monotonic
        server = parsedate_to_datetime(date_header).timestamp()
        book = float(book_time_ms) / 1000
        if not (math.isfinite(book) and 0 <= rtt <= 2 and abs(book - server) <= 2 + rtt):
            raise ValueError("unreliable exchange clock / cached book")
        # HTTP Date has one-second precision. Use the centre of its interval
        # and compensate half of the measured round trip.
        with self.lock:
            if self.monotonic_anchor is not None and received_monotonic < self.monotonic_anchor:
                return
            estimate = server + .5 + rtt / 2
            if self.anchor is not None:
                estimate = max(estimate, self.anchor + received_monotonic - self.monotonic_anchor)
            self.anchor = estimate
            self.monotonic_anchor = received_monotonic

    def now(self):
        with self.lock:
            if self.anchor is None:
                return None
            elapsed = time.monotonic() - self.monotonic_anchor
            return self.anchor + elapsed if 0 <= elapsed <= 300 else None

    def refresh(self):
        import requests
        before = time.monotonic()
        response = requests.post("https://api.hyperliquid.xyz/info",
                                 json={"type": "l2Book", "coin": "xyz:SP500"}, timeout=3)
        after = time.monotonic()
        response.raise_for_status()
        if int(response.headers.get("Age", "0")) > 0:
            raise ValueError("cached exchange clock response")
        data = response.json()
        self.observe(response.headers["Date"], data["time"], before, after)
        return data
