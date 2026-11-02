"""Explicitly fake worker for subprocess tests. Never contacts an exchange."""
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import requests


def forbidden(*args, **kwargs):
    raise AssertionError("Network is forbidden in the offline worker")


requests.sessions.Session.request = forbidden
from zeus.bots.outcome_worker import run_worker
from zeus.safety import SafetyLatch, atomic_json


class FakeBot:
    def __init__(self, cfg):
        self.cfg = cfg
        self.running = True
        self.runtime = Path(cfg.stop_file).parent
        self.events = []
        self.started = time.time()
        self.safety = SafetyLatch(lambda: setattr(self, "running", False), cfg.safety_file,
                                  is_running=lambda: self.running and not Path(cfg.stop_file).exists())
        self.safety.account_known = True
        if os.getenv("ZEUS_TEST_SLOW_CONSTRUCTOR") == "1":
            (self.runtime / "constructing").touch()
            deadline = time.time() + 10
            while not (self.runtime / "release").exists() and time.time() < deadline:
                time.sleep(0.02)

    def log(self, event):
        self.events.append({"event": event, "market": self.cfg.symbols[0], "pid": os.getpid()})
        atomic_json(self.runtime / "events.json", self.events)

    def request_stop(self):
        self.safety.stop()

    def run(self):
        self.safety.phase = "running"
        self.safety.publish()
        self.log("run")
        while self.running:
            if (self.runtime / "recover").exists() and self.safety.phase == "running":
                self.safety.pause("offline test recovery")
            if (self.runtime / "resume").exists() and self.safety.phase == "recovering":
                (self.runtime / "recover").unlink(missing_ok=True)
                self.safety.account_known = True
                self.safety.resume(self.safety.recovery_revision)
            time.sleep(0.02)

    def shutdown(self):
        if not self.safety.cleanup_done.is_set():
            self.safety.cleanup(lambda: [(self.cfg.symbols[0], 0, [])], forbidden, forbidden,
                                sleep=lambda _: None)
            self.log("cleanup")

    def export_dashboard_stats(self):
        return {"duration_sec": time.time() - self.started, "volume_usd": 100,
                "net_pnl": -0.1, "fees": 0.1, "fills": 2, "position_usd": 0,
                "mid_price": 0.5, "spread_bps": 1}


if __name__ == "__main__":
    raise SystemExit(run_worker(sys.argv[sys.argv.index("--config") + 1], bot_factory=FakeBot))
