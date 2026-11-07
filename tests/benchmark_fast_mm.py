"""Identical offline MID/BBO replays, optionally loading a saved source baseline.

Fake REST and execution each sleep --delay-ms. No bot constructor, HTTP, signing
or real order. Wall-clock tails include OS scheduling and mock overhead.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import time
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from test_reduce_only_preflight import ReduceOnlyPreflightTests
from zeus.latency import LatencyMetrics


def percentiles(values):
    ordered=sorted(values)
    return {key:ordered[int((len(ordered)-1)*q)] for key,q in
            (("p50",.5),("p90",.9),("p95",.95),("p99",.99))} if ordered else None


def replay(strategy,cycles,delay_ms,scenario):
    fixture=ReduceOnlyPreflightTests();fixture.setUp();b=fixture.bot;m=fixture.mod
    b.telemetry=LatencyMetrics();b.size_mult=1;b.pause_until=b.hourly_pause_until=0
    b.last_action_ts=0;b.last_quote_ts=0;b.order_cancellations=0
    b.last_tick_ts=time.time();b.book_received_ns=time.perf_counter_ns();b.book_revision=1
    durations=[];reads=[];acks=[]
    def sleep():time.sleep(delay_ms/1000)
    def position(*args,**kwargs):
        sleep();return {"assetPositions":[{"position":{"coin":"ANTH","szi":"-1","entryPx":"100","unrealizedPnl":"0"}}],"marginSummary":{"accountValue":"100"}}
    def orders(*args,**kwargs):
        sleep();return [{"coin":"io:ANTH","oid":o,"side":d["side"],"sz":str(d["sz"])} for o,d in b.orders.items()]
    def spot(*args,**kwargs):sleep();return {"balances":[]}
    def execute(requests):
        sleep();acks.append((time.perf_counter_ns()-event_started)/1e6)
        return {"status":"ok","response":{"data":{"statuses":[{"resting":{"oid":124}} for _ in requests]}}}
    def cancel(requests):
        sleep();return {"status":"ok","response":{"data":{"statuses":["success" for _ in requests]}}}
    b.info.user_state.side_effect=position;b.info.open_orders.side_effect=orders;b.info.spot_user_state.side_effect=spot
    fixture.raw.bulk_orders.side_effect=fixture.raw.bulk_modify_orders_new.side_effect=execute
    fixture.raw.bulk_cancel.side_effect=cancel
    try:
        with patch.multiple(m,STRATEGY=strategy,MID_OFFSET_BPS=1,ORDER_NOTIONAL_USD=100,SESSION_WINDOWS_UTC=[],ACTION_COALESCE_SEC=0):
            for _ in range(cycles):
                b.position=-1;b.position_dirty=False;b._urgent_quote=True
                b.book_received_ns=time.perf_counter_ns();b.book_revision+=1
                if scenario=="conversion":
                    b.orders={99:{"side":"B","px":100,"sz":1,"ro":False,"ts":100}}
                    b.safety.owned.add(99)
                else:
                    target=b.compute_intent()[0]
                    b.orders={99:{"side":"B","px":target[0],"sz":target[1],"ro":True,"ts":100}}
                event_started=time.perf_counter_ns()
                b.apply_intent()
                durations.append((time.perf_counter_ns()-event_started)/1e6)
        # Replay repeated timer wakes without a new market event.
        b.compute_intent=lambda:(None,None,"idle")
        b.last_action_ts=0
        b.orders={};b.book_revision+=1;b.book_received_ns=time.perf_counter_ns()
        b.telemetry=LatencyMetrics()
        for _ in range(100):b.apply_intent()
        timing=b.telemetry.snapshot()["timings"].get("event_to_decision",{})
        return {"cycles":cycles,"cycle_ms":percentiles(durations),"event_to_final_execution_ack_ms":percentiles(acks),
                "execution_requests":b.order_requests,"modifications":b.order_modifications,
                "placements":b.order_placements,"cancellations":b.order_cancellations,
                "account_reads":b.info.user_state.call_count+b.info.open_orders.call_count+b.info.spot_user_state.call_count,
                "event_latency_samples_for_one_event_100_wakes":timing.get("samples",0),
                "live_signing":False,"fees":None,"net_pnl":None}
    finally:fixture.doCleanups()


if __name__=="__main__":
    parser=argparse.ArgumentParser();parser.add_argument("--source");parser.add_argument("--output",required=True)
    parser.add_argument("--cycles",type=int,default=200);parser.add_argument("--delay-ms",type=float,default=1)
    args=parser.parse_args();source=Path(args.source or "zeus/bots/order_book.py")
    # Source imports read configuration; use dummy credentials even when loading
    # the baseline before the fixture's patch.dict context. No signing occurs.
    os.environ["PRIVATE_KEY"]="1"*64
    os.environ["WALLET_ADDRESS"]="0x"+"1"*40
    if args.source:
        spec=importlib.util.spec_from_file_location("zeus.bots.order_book",source)
        module=importlib.util.module_from_spec(spec);sys.modules[spec.name]=module;spec.loader.exec_module(module)
    data={"source_sha256":hashlib.sha256(source.read_bytes()).hexdigest(),"delay_ms":args.delay_ms,
          "model":"fake REST/SDK sleeps; no HTTP, signing, constructor or matching; OS scheduling uncontrolled",
          "scenarios":{f"{mode}_{scenario}":replay(mode,args.cycles,args.delay_ms,scenario)
                       for mode in ("mid","bbo") for scenario in ("conversion","unchanged")}}
    Path(args.output).write_text(json.dumps(data,indent=2),encoding="utf-8")
    print(json.dumps(data,indent=2))
