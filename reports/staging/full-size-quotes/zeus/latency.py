"""Bounded local timing samples; no files or HTTP on the measurement path."""
from collections import Counter,deque
import math
import threading
import time

class LatencyMetrics:
    def __init__(self,capacity=1024):
        self.started=time.perf_counter();self.capacity=capacity;self.samples={};self.counts=Counter();self.lock=threading.Lock()
    def observe(self,name,milliseconds):
        if not math.isfinite(milliseconds) or milliseconds<0:return
        with self.lock:self.samples.setdefault(name,deque(maxlen=self.capacity)).append(milliseconds)
    def count(self,name,value=1):
        with self.lock:self.counts[name]+=value
    def snapshot(self):
        with self.lock:
            samples={k:list(v) for k,v in self.samples.items()};counts=dict(self.counts)
        result={}
        for key,values in samples.items():
            ordered=sorted(values);n=len(ordered)
            result[key]={"samples":n,**{p:ordered[min(n-1,int((n-1)*q))] for p,q in [("p50",.5),("p90",.9),("p95",.95),("p99",.99)]}}
        return {"metric_version":2,"elapsed_seconds":time.perf_counter()-self.started,"unit":"ms","window_capacity":self.capacity,"timings":result,"counts":counts}
