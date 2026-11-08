"""Bounded retries for UI snapshots only. Durable risk journals stay strict."""
import time
from zeus.safety import atomic_json


class OutcomeTelemetry:
    def __init__(self, path, *, grace=5, publish=atomic_json, clock=time.monotonic, sleep=time.sleep):
        self.path, self.grace, self.publish = path, grace, publish
        self.clock, self.sleep = clock, sleep
        self.failed_since = None
        self.retries = 0

    def write(self, data):
        try:
            self.publish(self.path, {**data, 'telemetry_write_retries': self.retries})
        except PermissionError:
            self.retries += 1
            now = self.clock()
            if self.failed_since is None:
                self.failed_since = now
            if now-self.failed_since >= self.grace:
                raise
            return False  # Keep the last valid file and its original timestamp.
        self.failed_since = None
        return True

    def final(self, data, timeout=2):
        until = self.clock()+timeout
        while True:
            try:
                self.publish(self.path, {**data, 'telemetry_write_retries': self.retries})
                self.failed_since = None
                return True
            except PermissionError:
                self.retries += 1
                if self.clock() >= until:
                    raise
                self.sleep(min(.1, max(0, until-self.clock())))
