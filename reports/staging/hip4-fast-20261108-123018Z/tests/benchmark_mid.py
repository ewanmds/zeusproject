"""Deterministic fake MID execution benchmark. No constructor, key or HTTP used."""
import argparse
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import statistics
import sys
import time
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from test_reduce_only_preflight import ReduceOnlyPreflightTests


def percentiles(values):
    ordered=sorted(values)
    if not ordered: return None
    return {key:ordered[min(len(ordered)-1,int((len(ordered)-1)*fraction))] for key,fraction in [("p50",.5),("p95",.95),("p99",.99)]}


def replay(scenario,cycles,delay_ms):
    fixture=ReduceOnlyPreflightTests();fixture.setUp();b=fixture.bot;m=fixture.mod
    times=[];submissions=[];acks=[];preflight=[];decision=[];event_decision=[];book_ages=[];lock_wait=[];lock_hold=[];stale=0;oid=100
    b.size_mult=1;b.hourly_pause_until=0;b.pause_until=0;b.last_action_ts=0
    b.last_tick_ts=time.time();b.order_cancellations=0;b.last_quote_ts=0
    b.book_revision=0;b.book_received_ns=time.perf_counter_ns()
    from zeus.latency import LatencyMetrics
    b._stop_file=str(Path("not-a-real-stop-file"));b.telemetry=LatencyMetrics()
    import threading
    class MeasuredLock:
        def __init__(self):self.lock=threading.Lock()
        def __enter__(self):
            started=time.perf_counter_ns();self.lock.acquire();self.held=time.perf_counter_ns()
            lock_wait.append((self.held-started)/1e6)
        def __exit__(self,*args):lock_hold.append((time.perf_counter_ns()-self.held)/1e6);self.lock.release()
    b.api_lock=MeasuredLock()
    original_compute=b.compute_intent
    def compute():
        started=time.perf_counter_ns();result=original_compute();ended=time.perf_counter_ns()
        decision.append((ended-started)/1e6);event_decision.append((ended-event_start)/1e6)
        return result
    b.compute_intent=compute
    fixture.position(1)
    def rest(*args,**kwargs):
        start=time.perf_counter_ns();time.sleep(delay_ms/1000)
        if scenario=="moving_preflight":
            b.best_bid+=.2;b.best_ask+=.2;b.mid=(b.best_bid+b.best_ask)/2
            b.book_received_ns=time.perf_counter_ns();b.book_revision+=1
        preflight.append((time.perf_counter_ns()-start)/1e6)
        return {"assetPositions":[{"position":{"coin":"ANTH","szi":"1","entryPx":"100","unrealizedPnl":"0"}}],"marginSummary":{"accountValue":"100"}}
    b.info.user_state.side_effect=rest
    def open_orders(*args,**kwargs):
        return [{"oid":o,"coin":"io:ANTH","side":d["side"],"sz":str(d["sz"])} for o,d in b.orders.items()]
    b.info.open_orders.side_effect=open_orders
    def submit(requests,modify=False):
        nonlocal oid,stale
        submissions.append((time.perf_counter_ns()-event_start)/1e6)
        book_ages.append((time.perf_counter_ns()-b.book_received_ns)/1e6)
        reqs=[r["order"] for r in requests] if modify else requests
        target=b.compute_mid_intent()[1][0]
        stale+=sum(abs(r["limit_px"]-target)>1e-8 for r in reqs)
        time.sleep(delay_ms/1000)
        statuses=[]
        for r in reqs:oid+=1;statuses.append({"resting":{"oid":oid}})
        acks.append((time.perf_counter_ns()-event_start)/1e6)
        return {"status":"ok","response":{"data":{"statuses":statuses}}}
    fixture.raw.bulk_orders.side_effect=submit
    fixture.raw.bulk_modify_orders_new.side_effect=lambda requests:submit(requests,True)
    fixture.raw.bulk_cancel.side_effect=lambda requests:(time.sleep(delay_ms/1000) or {"status":"ok","response":{"data":{"statuses":["success"]*len(requests)}}})
    try:
        with patch.multiple(m,STRATEGY="mid",MID_OFFSET_BPS=2.0,SESSION_WINDOWS_UTC=[],ACTION_COALESCE_SEC=0):
            # Optional final-send hook exists only in the optimized version.
            if hasattr(b,"_prepare_mid_submission"):
                from zeus.signing import SigningCoordinator
                # Exercise the exact hook without disk/signing overhead; those
                # remain reported separately by runtime telemetry.
                class FinalSend:
                    def __getattr__(self,name):
                        raw=getattr(fixture.raw,name)
                        def call(*args,**kwargs):b._prepare_mid_submission(name,args,kwargs);return raw(*args,**kwargs)
                        return call
                b.exchange=b.safety.wrap(FinalSend())
            b.orders={99:{"side":"A","px":b.compute_mid_intent()[1][0],"sz":1,"ro":True,"ts":time.time()}}
            for i in range(cycles):
                if scenario!="unchanged":b.best_bid+=.2;b.best_ask+=.2;b.mid=(b.best_bid+b.best_ask)/2
                b.last_tick_ts=time.time();b.book_received_ns=time.perf_counter_ns();b.book_revision+=1
                event_start=time.perf_counter_ns()
                b.apply_intent()
                times.append((time.perf_counter_ns()-event_start)/1e6)
        return {"cycles":cycles,"cycle_ms":percentiles(times),"decision_ms":percentiles(decision),"event_to_decision_ms":percentiles(event_decision),"book_age_at_submit_ms":percentiles(book_ages),"api_lock_wait_ms":percentiles(lock_wait),"api_lock_hold_ms":percentiles(lock_hold),"event_to_submit_ms":percentiles(submissions),"event_to_ack_ms":percentiles(acks),"preflight_read_ms":percentiles(preflight),"account_read_requests":b.info.user_state.call_count+b.info.open_orders.call_count+b.info.spot_user_state.call_count,"placements":b.order_placements,"modifications":b.order_modifications,"cancellations":b.order_cancellations,"exchange_requests":b.order_requests,"stale_price_submissions":stale,"fills":None,"maker_ratio":None,"fees":None,"net_pnl":None,"cost_per_million":None,"runtime_metrics":b.telemetry.snapshot(),"live_signing_lock_wait_ms":None}
    finally:fixture.doCleanups()

if __name__=="__main__":
    p=argparse.ArgumentParser();p.add_argument("--output",required=True);p.add_argument("--source");p.add_argument("--cycles",type=int,default=100);p.add_argument("--delay-ms",type=float,default=1);a=p.parse_args()
    source=Path(a.source or "zeus/bots/order_book.py")
    if a.source:
        spec=importlib.util.spec_from_file_location("zeus.bots.order_book",source)
        module=importlib.util.module_from_spec(spec);sys.modules[spec.name]=module;spec.loader.exec_module(module)
    output={"model":"fake REST user_state and exchange ACK each sleep delay_ms; no matching or live network; fees/PnL unavailable", "delay_ms":a.delay_ms,"source_sha256":hashlib.sha256(source.read_bytes()).hexdigest(),"scenarios":{s:replay(s,a.cycles,a.delay_ms) for s in ["unchanged","ro_reprice","moving_preflight"]}}
    Path(a.output).write_text(json.dumps(output,indent=2),encoding="utf-8");print(json.dumps(output,indent=2))

