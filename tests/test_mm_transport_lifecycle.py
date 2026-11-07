"""Lifecycle integration with fake exchange, transport and read clients only."""
import asyncio
import json
from pathlib import Path
import signal
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock,patch

import test_inventory_integration as fixtures
from zeus.safety import SafetyLatch


class TransportLifecycleTests(fixtures.BotIntegrationFixture):
    def test_expired_coalesce_deadline_is_consumed_when_action_queued(self):
        b=self.bot;b._quote_worker=Mock();b._quote_worker.state.return_value={"failed":False}
        b._quote_worker.request.return_value=True;b._quote_due_at=time.monotonic()-1
        self.assertTrue(b._request_quote_action());self.assertEqual(b._quote_due_at,0)
        future=b._quote_due_at=time.monotonic()+1
        self.assertTrue(b._request_quote_action());self.assertEqual(b._quote_due_at,future)

    def test_execution_socket_is_ready_before_first_signed_setup_action(self):
        b=self.bot;transport=b._execution_transport=Mock()
        transport.wait_ready.return_value=True;transport.state.return_value={"ready":True}
        def leverage():
            transport.start.assert_called_once()
            transport.wait_ready.assert_called()
            b.safety.stop()
        b.set_leverage=Mock(side_effect=leverage)
        with patch.dict(self.mod.os.environ,{"OBI_HEADLESS":"1"}):b._run_session()
        b.set_leverage.assert_called_once()

    def test_unavailable_socket_stops_before_setup_and_never_falls_back_to_http(self):
        b=self.bot;transport=b._execution_transport=Mock()
        transport.wait_ready.return_value=False;transport.state.return_value={"ready":False}
        b.set_leverage=Mock()
        with patch.dict(self.mod.os.environ,{"OBI_HEADLESS":"1"}),patch.object(self.mod.time,"monotonic",side_effect=[0,11]):
            b._run_session()
        b.set_leverage.assert_not_called()
        self.assertTrue(b.safety.stopped.is_set())

    def _configured_close(self, manual, quote_worker=None):
        b=self.bot;b.running=False;b.live=True
        b.safety=SafetyLatch(lambda:setattr(b,"running",False),passive_only=True)
        b.safety.exposure_started=True
        if manual:b.safety.request_manual_stop()
        b._check_stop_file=Mock();b._manual_stop_file=Mock(return_value=False)
        b.ws_info=None;b._quote_worker=quote_worker;b._maintenance=None
        b.write_stats=Mock();b.exchange=Mock();b._execution_transport=Mock()
        order=[];remaining=[.1]
        b._cleanup_snapshot=Mock(side_effect=lambda:[("io:ANTH",remaining[0],[])])
        def close(*args):
            self.assertTrue(b.safety.local.manual_close)
            order.append("close");remaining[0]=0
        b._cleanup_close=Mock(side_effect=close)
        b._final_accounting=Mock(side_effect=lambda timeout:order.append("accounting"))
        b._execution_transport.close.side_effect=lambda timeout:order.append("transport_close")
        with patch("zeus.safety.time.sleep"):b.run()
        return b,order

    def test_manual_close_runs_before_transport_closes(self):
        b,order=self._configured_close(True)
        self.assertEqual(order,["close","accounting","transport_close"])
        self.assertTrue(b.safety.cleanup_confirmed)

    def test_automatic_failure_never_closes_residual_inventory(self):
        b,order=self._configured_close(False)
        b._cleanup_close.assert_not_called()
        self.assertFalse(b.safety.cleanup_confirmed)
        self.assertIn("manual Stop",b.safety.cleanup_error)
        self.assertEqual(order,["accounting","transport_close"])

    def test_cleanup_waits_for_preflight_to_drain_before_using_shared_read_client(self):
        worker=Mock();worker.close.side_effect=[False,True,True]
        b,order=self._configured_close(True,worker)
        self.assertEqual(order,["close","accounting","transport_close"])
        self.assertTrue(b.safety.cleanup_confirmed)
        self.assertEqual(worker.close.call_count,3)
        self.assertGreater(worker.close.call_args_list[1].kwargs["timeout"],0)
        self.assertLessEqual(worker.close.call_args_list[1].kwargs["timeout"],30)

    def test_blocked_preflight_never_shares_read_client_with_cleanup(self):
        worker=Mock();worker.close.return_value=False
        b,order=self._configured_close(True,worker)
        b._cleanup_snapshot.assert_not_called()
        b._cleanup_close.assert_not_called()
        self.assertEqual(order,["accounting","transport_close"])
        self.assertFalse(b.safety.cleanup_confirmed)
        self.assertIn("worker still resolving",b.safety.cleanup_error)

    def test_headless_sigint_and_sigterm_do_not_infer_manual_authorization(self):
        b=self.bot
        with patch.dict(self.mod.os.environ,{"OBI_HEADLESS":"1"}):b._sig_shutdown(signal.SIGINT)
        self.assertFalse(b.safety.manual_stop_requested)
        b._sig_shutdown(signal.SIGTERM)
        self.assertFalse(b.safety.manual_stop_requested)

    def test_interactive_ctrl_c_records_manual_operator_intent(self):
        b=self.bot
        with patch.dict(self.mod.os.environ,{"OBI_HEADLESS":""}),patch.object(self.mod.sys.stdin,"isatty",return_value=True):
            b._sig_shutdown(signal.SIGINT)
        self.assertTrue(b.safety.manual_stop_requested)

    def test_dead_feed_read_does_not_block_main_loop_or_share_http_client(self):
        b=self.bot;b.ws_ready=True;b.last_tick_ts=time.time()-20;b.last_ws_restart=0
        entered,release=threading.Event(),threading.Event();client=Mock()
        def read(*args):
            entered.set();release.wait(2)
            return {"time":100,"levels":[[{"px":"100","sz":"1"}],[{"px":"100.1","sz":"1"}]]}
        client.post.side_effect=read;b.restart_ws=Mock()
        with patch("hyperliquid.api.API",return_value=client):
            try:
                b._check_feed_health()
                self.assertTrue(entered.wait(1))
                self.assertTrue(b._feed_recovery.state()["active"])
                b.info.l2_snapshot.assert_not_called()
                b._sig_shutdown()
                self.assertFalse(b.running)
            finally:
                release.set();b._feed_recovery.close(3)
        b.restart_ws.assert_not_called()
        client.session.close.assert_called_once()


class ServerStopOriginTests(unittest.TestCase):
    def test_stop_payload_carries_manual_origin_and_exact_session(self):
        from zeus import server
        for manual in (True,False):
            with tempfile.TemporaryDirectory() as directory:
                manager=server.BotManager.__new__(server.BotManager);manager.lock=threading.Lock()
                proc=Mock();proc.poll.return_value=0
                path=Path(directory)/"stop.json"
                manager.obi_procs={"IO_ANTH":{"proc":proc,"stop_file":str(path),"safety_file":"unused",
                    "config":{"strategy":"mid"}}}
                manager._session_status=Mock(return_value={"session_id":"session-current","cleanup_confirmed":True})
                manager.stop_grid("IO_ANTH",manual)
                self.assertEqual(json.loads(path.read_text()),{"manual":manual,"session_id":"session-current"})

    def test_server_shutdown_does_not_claim_a_manual_stop(self):
        from zeus import server
        manager=Mock()
        with patch.object(server,"manager",manager):asyncio.run(server.stop_on_server_shutdown())
        manager.stop_grid.assert_called_once_with(None,False)


if __name__=="__main__":unittest.main()
