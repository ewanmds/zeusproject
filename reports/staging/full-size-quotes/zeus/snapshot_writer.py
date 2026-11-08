"""One background publisher with a single replaceable pending snapshot."""
import threading
import time
from zeus.safety import atomic_json

class LatestSnapshotWriter:
    def __init__(self,path,metrics=None,publish=atomic_json):
        self.path,self.metrics,self.publish=path,metrics,publish
        self.condition=threading.Condition();self.pending=None;self.stopping=False
        self.error=""
        self.thread=threading.Thread(target=self._run,name="mid-statistics",daemon=True)
        self.thread.start()
    def submit(self,data):
        with self.condition:
            if self.stopping:return False
            if self.pending is not None and self.metrics:self.metrics.count("stats_coalesced")
            self.pending=data;self.condition.notify()
        return True
    def _run(self):
        while True:
            with self.condition:
                self.condition.wait_for(lambda:self.pending is not None or self.stopping)
                if self.pending is None and self.stopping:return
                data=self.pending;self.pending=None
            started=time.perf_counter_ns()
            try:self.publish(self.path,data);self.error=""
            except Exception as exc:
                self.error=type(exc).__name__
                if self.metrics:self.metrics.count("stats_write_failures")
            finally:
                if self.metrics:self.metrics.observe("stats_write",(time.perf_counter_ns()-started)/1e6)
    def close(self,final,timeout=2):
        with self.condition:
            self.pending=final;self.stopping=True;self.condition.notify()
        self.thread.join(timeout)
        return not self.thread.is_alive() and not self.error
