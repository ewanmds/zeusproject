import multiprocessing
import os
import tempfile
import time
import unittest
from unittest.mock import Mock,patch
from pathlib import Path
from zeus.safety import SafetyLatch,legacy_nonce_rejection
from zeus.signing import SigningCoordinator

ERROR="bulk_orders: exchange rejected: Invalid nonce: duplicate nonce 1793884066038"

def fake_sign(root,queue):
    coordinator=SigningCoordinator(root,"dummy-agent")
    for _ in range(5):
        with coordinator.transaction(): queue.put(int(time.time()*1000))

class NonceTests(unittest.TestCase):
    def test_explicit_rejection_clears_only_current_uncertainty(self):
        raw=Mock();raw.bulk_orders.return_value={"status":"err","response":"Invalid nonce: duplicate nonce 123"}
        gate=SafetyLatch();gate.pending_cloids.add("older-uncertain")
        with self.assertRaisesRegex(RuntimeError,"duplicate nonce"):
            gate.wrap(raw).bulk_orders([{"coin":"io:ANTH"}])
        self.assertEqual(gate.pending_cloids,{"older-uncertain"})
        self.assertEqual(len(gate.metadata["rejected_cloids"]),1)
        raw.bulk_orders.assert_called_once()

    def test_timeout_and_unrecognized_error_keep_uncertainty(self):
        for value in [None,{"status":"err","response":"unknown server error"}]:
            gate=SafetyLatch();raw=Mock();raw.bulk_orders.return_value=value
            with self.assertRaises(RuntimeError): gate.wrap(raw).bulk_orders([{"coin":"io:ANTH"}])
            self.assertEqual(len(gate.pending_cloids),1)

    def test_legacy_recovery_requires_exact_failure(self):
        self.assertTrue(legacy_nonce_rejection({"error":ERROR}))
        for record in [{"error":"timeout duplicate nonce"},{"error":ERROR,"recovery_error":"uncertain previous send"},{"error":ERROR+" unrelated failure"}]:
            self.assertFalse(legacy_nonce_rejection(record))

    def test_independent_snapshot_must_be_flat(self):
        from zeus.server import BotManager
        record={"account_kind":"perp","symbols":["io:ANTH"],"error":ERROR,"pending_cloids":["0x"+"1"*32]}
        info=Mock();info.post.return_value={"status":"unknownOid"}
        manager=BotManager.__new__(BotManager)
        with patch.dict(os.environ,{"WALLET_ADDRESS":"dummy-account"}),patch("hyperliquid.info.Info",return_value=info),patch("zeus.server.account_snapshot",return_value=[("io:ANTH",0,[])]):
            manager._verify_session_flat(record)
        with patch.dict(os.environ,{"WALLET_ADDRESS":"dummy-account"}),patch("hyperliquid.info.Info",return_value=info),patch("zeus.server.account_snapshot",return_value=[("io:ANTH",1,[])]):
            with self.assertRaisesRegex(RuntimeError,"Residual"):manager._verify_session_flat(record)
        with patch.dict(os.environ,{"WALLET_ADDRESS":"dummy-account"}),patch("hyperliquid.info.Info",return_value=info),patch("zeus.server.account_snapshot",side_effect=TimeoutError("read unavailable")):
            with self.assertRaises(TimeoutError):manager._verify_session_flat(record)
        with patch.dict(os.environ,{"WALLET_ADDRESS":"dummy-account"}),patch("hyperliquid.info.Info",return_value=info):
            with self.assertRaisesRegex(RuntimeError,"unconfirmed"):manager._verify_session_flat({**record,"error":"timeout"})

    def test_two_processes_do_not_reuse_signing_millisecond(self):
        with tempfile.TemporaryDirectory() as root:
            ctx=multiprocessing.get_context("spawn");queue=ctx.Queue()
            children=[ctx.Process(target=fake_sign,args=(root,queue)) for _ in range(2)]
            try:
                for p in children:p.start()
                stamps=[queue.get(timeout=15) for _ in range(10)]
                for p in children:p.join(10);self.assertEqual(p.exitcode,0)
                self.assertEqual(len(stamps),len(set(stamps)))
            finally:
                for p in children:
                    if p.is_alive():p.terminate();p.join(3)
                queue.close()

if __name__=="__main__":unittest.main()
