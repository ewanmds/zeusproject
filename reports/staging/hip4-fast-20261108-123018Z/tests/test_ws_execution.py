"""Persistent action transport tests: fake sockets only, no signing/network."""
import json
import queue
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock

import websocket

from zeus.latency import LatencyMetrics
from zeus.safety import SafetyLatch, SubmissionSkipped
from zeus.signing import SigningCoordinator
from zeus.ws_execution import WebSocketExecution, UncertainActionError, websocket_url


def action_ack(request_id, payload=None, kind="action"):
    if payload is None:
        payload={"status":"ok","response":{"type":"order","data":{"statuses":[{"resting":{"oid":7}}]}}}
    return json.dumps({"channel":"post","data":{"id":request_id,"response":{"type":kind,"payload":payload}}})


def signed():
    return {"action":{"type":"order","orders":[{"a":200001,"b":True,"p":"100","s":"1","r":False,
                "t":{"limit":{"tif":"Alo"}},"c":"0x"+"1"*32}],"grouping":"na", "builder":{"b":"0xabc","f":0}},
            "nonce":123456789,"signature":{"r":"fake-r","s":"fake-s","v":27},
            "vaultAddress":None,"expiresAfter":123457000}


class FakeSocket:
    def __init__(self, on_send=None):
        self.messages=queue.Queue();self.sent=[];self.on_send=on_send
        self.closed=False;self.entered=threading.Event()
    def settimeout(self,value):self.timeout=value
    def send(self,message):
        item=json.loads(message);self.sent.append(item);self.entered.set()
        if item.get("method")=="ping":
            self.messages.put('{"channel":"pong"}')
        elif self.on_send:self.on_send(self,item)
    def recv(self):
        try:
            item=self.messages.get(timeout=.005)
        except queue.Empty:
            raise websocket.WebSocketTimeoutException()
        if isinstance(item,BaseException):raise item
        return item
    def shutdown(self):self.closed=True;self.messages.put("")


class TransportTests(unittest.TestCase):
    def setUp(self):self.transports=[]
    def tearDown(self):
        for transport in self.transports:transport.close(.5)
    def transport(self,socket=None,**kwargs):
        socket=socket or FakeSocket(lambda s,item:s.messages.put(action_ack(item["id"])))
        transport=WebSocketExecution("https://api.example.invalid",socket_factory=lambda *a,**k:socket,
            request_timeout=kwargs.pop("request_timeout",.15),reconnect_delay=.01,max_reconnect_delay=.04,**kwargs)
        self.transports.append(transport);transport.start()
        self.assertTrue(transport.wait_ready(.5))
        return transport,socket
    def test_payload_is_unchanged_and_connection_reused_with_unique_ids(self):
        t,s=self.transport();payload=signed()
        result=t.post_action(payload);t.post_action(payload)
        self.assertEqual(result["response"]["data"]["statuses"],[{"resting":{"oid":7}}])
        self.assertEqual([m["id"] for m in s.sent],[1,2])
        self.assertEqual(s.sent[0]["request"],{"type":"action","payload":payload})
        self.assertEqual(payload,signed())
        self.assertEqual(t.state()["generation"],1)
    def test_mixed_status_response_is_not_rewritten(self):
        payload={"status":"ok","response":{"type":"order","data":{"statuses":[
            {"resting":{"oid":1}},{"error":"Post only order would have immediately matched, bbo was 100@100.1."}]}}}
        t,_=self.transport(FakeSocket(lambda s,item:s.messages.put(action_ack(item["id"],payload))))
        self.assertEqual(t.post_action(signed()),payload)
    def test_unknown_ack_ids_are_ignored_without_a_map_or_queue(self):
        def respond(s,item):
            for n in range(100):s.messages.put(action_ack(item["id"]+100+n))
            s.messages.put(action_ack(item["id"]))
        metrics=LatencyMetrics();t,_=self.transport(FakeSocket(respond),metrics=metrics)
        t.post_action(signed())
        self.assertIsNone(t.pending)
        self.assertEqual(metrics.snapshot()["counts"]["ws_unmatched_acks"],100)
    def test_delayed_ack_completes_only_once(self):
        timer=[]
        def respond(s,item):
            work=threading.Timer(.02,lambda:s.messages.put(action_ack(item["id"])))
            work.start();timer.append(work)
        t,s=self.transport(FakeSocket(respond))
        try:self.assertEqual(t.post_action(signed())["status"],"ok")
        finally:
            for work in timer:work.join(.5)
        self.assertEqual(len(s.sent),1)
    def test_timeout_is_uncertain_and_never_resends(self):
        t,s=self.transport(FakeSocket(),request_timeout=.025)
        with self.assertRaisesRegex(UncertainActionError,"timeout"):
            t.post_action(signed())
        self.assertEqual(len(s.sent),1);self.assertTrue(s.closed);self.assertIsNone(t.pending)
    def test_send_failure_is_always_uncertain_even_if_socket_raised_immediately(self):
        def fail(s,item):raise OSError("broken pipe")
        t,s=self.transport(FakeSocket(fail))
        with self.assertRaisesRegex(UncertainActionError,"outcome is unknown"):
            t.post_action(signed())
        self.assertEqual(len(s.sent),1)
    def test_disconnect_after_send_is_uncertain(self):
        t,s=self.transport(FakeSocket(lambda s,item:s.messages.put(ConnectionError("lost"))))
        with self.assertRaises(UncertainActionError):t.post_action(signed())
        self.assertEqual(len(s.sent),1)
    def test_malformed_matching_ack_is_uncertain(self):
        cases=[lambda i:'{"channel":"post","data":null}',
               lambda i:json.dumps({"channel":"post","data":{"id":i,"response":{"type":"info","payload":{}}}}),
               lambda i:action_ack(i,{"status":"ok"}),lambda i:'not-json']
        for response in cases:
            t,s=self.transport(FakeSocket(lambda s,item:s.messages.put(response(item["id"]))))
            with self.assertRaises(UncertainActionError):t.post_action(signed())
            self.assertEqual(len(s.sent),1);t.close(.5)
    def test_ws_error_string_becomes_sdk_error_envelope_without_retry(self):
        t,s=self.transport(FakeSocket(lambda s,item:s.messages.put(action_ack(item["id"],"422 invalid request",kind="error"))))
        self.assertEqual(t.post_action(signed()),{"status":"err","response":"422 invalid request"})
        self.assertEqual(len(s.sent),1)
    def test_not_ready_never_connects_on_the_signed_request_path(self):
        factory=Mock(side_effect=AssertionError("must not connect"))
        t=WebSocketExecution("https://unused.invalid",socket_factory=factory)
        self.transports.append(t)
        with self.assertRaises(SubmissionSkipped):t.post_action(signed())
        factory.assert_not_called()
    def test_final_gate_runs_on_calling_thread_and_stop_skips_before_send(self):
        caller=threading.get_ident();seen=[]
        def stop(payload):
            seen.append((threading.get_ident(),payload))
            raise SubmissionSkipped("manual stop")
        t,s=self.transport(final_gate=stop)
        with self.assertRaisesRegex(SubmissionSkipped,"manual stop"):t.post_action(signed())
        self.assertEqual(seen,[(caller,signed())]);self.assertFalse(s.sent)
        self.assertIsNone(t.pending);self.assertTrue(t.state()["ready"])
    def test_close_during_final_gate_is_known_unsent(self):
        t,s=self.transport();t.final_gate=lambda payload:t.close(.1)
        with self.assertRaises(SubmissionSkipped):t.post_action(signed())
        self.assertFalse(s.sent)
    def test_second_concurrent_action_is_skipped_not_queued(self):
        t,s=self.transport(FakeSocket());result=[]
        worker=threading.Thread(target=lambda:result.append(t.post_action(signed())))
        worker.start();self.assertTrue(s.entered.wait(.5))
        with self.assertRaisesRegex(SubmissionSkipped,"busy"):t.post_action(signed())
        s.messages.put(action_ack(1));worker.join(.5)
        self.assertFalse(worker.is_alive());self.assertEqual(len(result),1);self.assertEqual(len(s.sent),1)
    def test_close_unblocks_pending_action_and_reader_with_bounded_wait(self):
        t,s=self.transport(FakeSocket(),request_timeout=2);errors=[]
        def call():
            try:t.post_action(signed())
            except Exception as exc:errors.append(exc)
        worker=threading.Thread(target=call);worker.start();self.assertTrue(s.entered.wait(.5))
        started=time.monotonic();self.assertTrue(t.close(.5));worker.join(.5)
        self.assertLess(time.monotonic()-started,.8)
        self.assertTrue(errors);self.assertIsInstance(errors[0],UncertainActionError)
        self.assertFalse(worker.is_alive());self.assertTrue(s.closed)
    def test_connection_failures_back_off_without_action_or_fallback(self):
        attempts=[]
        def fail(*args,**kwargs):attempts.append(time.perf_counter());raise OSError("unavailable")
        t=WebSocketExecution("https://unused.invalid",socket_factory=fail,reconnect_delay=.02,max_reconnect_delay=.08)
        self.transports.append(t);t.start()
        self.assertFalse(t.wait_ready(.12));t.close(.5)
        self.assertGreaterEqual(len(attempts),2);self.assertLessEqual(len(attempts),4)
        self.assertGreaterEqual(attempts[1]-attempts[0],.015)
    def test_rapid_server_closes_also_back_off(self):
        attempts=[]
        def closed(*args,**kwargs):
            attempts.append(time.perf_counter());s=FakeSocket();s.messages.put("");return s
        t=WebSocketExecution("https://unused.invalid",socket_factory=closed,reconnect_delay=.02,max_reconnect_delay=.08)
        self.transports.append(t);t.start();time.sleep(.12);t.close(.5)
        self.assertGreaterEqual(len(attempts),2);self.assertLessEqual(len(attempts),4)
        self.assertGreaterEqual(attempts[1]-attempts[0],.015)
        # Attempt count also detects a reconnect storm without a tight timer.
        if len(attempts)>2:self.assertGreaterEqual(attempts[2]-attempts[1],.02)
    def test_close_during_connect_is_bounded_and_late_socket_is_never_published(self):
        entered,release=threading.Event(),threading.Event();s=FakeSocket()
        def blocked(*args,**kwargs):entered.set();release.wait(1);return s
        t=WebSocketExecution("https://unused.invalid",socket_factory=blocked)
        self.transports.append(t);t.start();self.assertTrue(entered.wait(.5))
        started=time.monotonic()
        try:
            self.assertFalse(t.close(.01));self.assertLess(time.monotonic()-started,.2)
        finally:release.set();t.thread.join(.5)
        self.assertFalse(t.state()["ready"]);self.assertTrue(s.closed)
    def test_guard_clears_only_unsent_cloid_on_final_gate_skip(self):
        def stop(payload):raise SubmissionSkipped("manual stop before socket send")
        t,s=self.transport(final_gate=stop);raw=Mock();raw.bulk_orders.side_effect=lambda rows:t.post_action(signed())
        gate=SafetyLatch();gate.pending_cloids.add("older-unknown")
        with self.assertRaises(SubmissionSkipped):gate.wrap(raw).bulk_orders([{"coin":"io:ANTH"}])
        self.assertEqual(gate.pending_cloids,{"older-unknown"});self.assertFalse(gate.error)
        self.assertFalse(s.sent)
    def test_heartbeat_keeps_idle_connection_alive_without_action(self):
        t,s=self.transport(heartbeat_interval=.025)
        self.assertTrue(s.entered.wait(.3))
        self.assertEqual(s.sent[0],{"method":"ping"})
        self.assertTrue(t.state()["ready"]);self.assertIsNone(t.pending)
    def test_invalid_local_json_is_skipped_without_uncertainty(self):
        t,s=self.transport()
        with self.assertRaises(SubmissionSkipped):t.post_action({"nonce":float("nan")})
        self.assertFalse(s.sent);self.assertIsNone(t.pending)
    def test_explicit_new_action_after_reconnect_does_not_consume_stale_ack(self):
        first=FakeSocket();second=FakeSocket(lambda s,item:(s.messages.put(action_ack(1)),s.messages.put(action_ack(item["id"]))))
        sockets=iter((first,second));t=WebSocketExecution("https://unused.invalid",socket_factory=lambda *a,**k:next(sockets),
            request_timeout=.025,reconnect_delay=.01)
        self.transports.append(t);t.start();self.assertTrue(t.wait_ready(.5))
        with self.assertRaises(UncertainActionError):t.post_action(signed())
        self.assertTrue(t.wait_ready(.5))
        self.assertEqual(t.post_action(signed())["status"],"ok")
        self.assertEqual(second.sent[0]["id"],2)
    def test_sdk_helper_preserves_http_info_and_never_falls_back_for_action(self):
        t,s=self.transport();metrics=LatencyMetrics()
        with tempfile.TemporaryDirectory() as directory:
            coordinator=SigningCoordinator(directory,"fake",metrics)
            class SDK:
                def __init__(self):self.http=[]
                def post(self,path,payload=None):self.http.append((path,payload));return {"http":True}
                def bulk_orders(self,rows):return self.post("/exchange",signed())
            sdk=SDK();coordinator.instrument_transport(sdk,t);wrapped=coordinator.wrap(sdk)
            self.assertEqual(sdk.post("/info",{"type":"meta"}),{"http":True})
            self.assertEqual(wrapped.bulk_orders([])["status"],"ok")
            self.assertEqual(sdk.http,[("/info",{"type":"meta"})])
            timings=metrics.snapshot()["timings"]
            self.assertIn("sdk_prepare_sign",timings);self.assertIn("exchange_ws",timings)
    def test_sdk_helper_cannot_fall_back_to_http_after_ack_timeout(self):
        t,s=self.transport(FakeSocket(),request_timeout=.025)
        with tempfile.TemporaryDirectory() as directory:
            coordinator=SigningCoordinator(directory,"fake")
            class SDK:
                def __init__(self):self.http=[]
                def post(self,path,payload=None):self.http.append((path,payload));return {}
                def bulk_orders(self,rows):return self.post("/exchange",signed())
            sdk=SDK();coordinator.instrument_transport(sdk,t)
            with self.assertRaises(UncertainActionError):coordinator.wrap(sdk).bulk_orders([])
            self.assertFalse(sdk.http);self.assertEqual(len(s.sent),1)
    def test_guard_keeps_unknown_cloid_after_ws_timeout(self):
        t,s=self.transport(FakeSocket(),request_timeout=.025);raw=Mock()
        raw.bulk_orders.side_effect=lambda rows:t.post_action(signed())
        gate=SafetyLatch()
        with self.assertRaises(UncertainActionError):gate.wrap(raw).bulk_orders([{"coin":"io:ANTH"}])
        self.assertEqual(len(gate.pending_cloids),1);raw.bulk_orders.assert_called_once()
        self.assertEqual(len(s.sent),1)
    def test_ws_url_derivation_does_not_put_credentials_in_socket_address(self):
        self.assertEqual(websocket_url("https://api.example.invalid"),"wss://api.example.invalid/ws")
        self.assertEqual(websocket_url("http://localhost:8080/"),"ws://localhost:8080/ws")
        self.assertEqual(websocket_url("wss://example.invalid/ws"),"wss://example.invalid/ws")
        for url in ("https://u:p@example.invalid","https://example.invalid?secret=foo","ftp://example.invalid"):
            with self.assertRaises(ValueError):websocket_url(url)
    def test_unbounded_or_invalid_transport_timeouts_are_rejected(self):
        for name in ("request_timeout","connect_timeout","poll_timeout","reconnect_delay","max_reconnect_delay","heartbeat_interval"):
            for value in (0,-1,float("nan"),float("inf")):
                with self.subTest(name=name,value=value),self.assertRaises(ValueError):
                    WebSocketExecution("https://unused.invalid",**{name:value})


if __name__=="__main__":unittest.main()
