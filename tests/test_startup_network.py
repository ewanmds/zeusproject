"""Startup failures are bounded, visible and never replay trade submissions."""
import ast
import json
from pathlib import Path
import tempfile
import time
import types
import unittest
from unittest.mock import Mock, patch

import requests
from startup_network import StartupNetworkError, post_info, retry_read


class StartupNetworkTests(unittest.TestCase):
    def test_connection_reset_then_success(self):
        read = Mock(side_effect=[requests.ConnectionError("reset"), {"ok": True}])
        delay = Mock()
        self.assertEqual(retry_read("metadata", read, sleep=delay), {"ok": True})
        self.assertEqual(read.call_count, 2)
        delay.assert_called_once_with(1)

    def test_repeated_resets_stop_after_four_attempts(self):
        read = Mock(side_effect=requests.ConnectionError("reset"))
        delay = Mock()
        with self.assertRaisesRegex(StartupNetworkError, "4 tentatives"):
            retry_read("metadata", read, sleep=delay)
        self.assertEqual(read.call_count, 4)
        self.assertEqual([c.args[0] for c in delay.call_args_list], [1, 2, 4])

    def test_invalid_config_is_not_retried(self):
        read = Mock(side_effect=ValueError("invalid symbol"))
        with self.assertRaises(ValueError):
            retry_read("metadata", read, sleep=Mock())
        read.assert_called_once()

    def test_throttling_is_retried_but_bad_request_is_not(self):
        for status, expected_calls in ((429, 4), (503, 4), (400, 1)):
            response = requests.Response()
            response.status_code = status
            read = Mock(side_effect=requests.HTTPError(response=response))
            with self.assertRaises((StartupNetworkError, requests.HTTPError)):
                retry_read("metadata", read, sleep=Mock())
            self.assertEqual(read.call_count, expected_calls)

    def test_public_read_checks_status_and_has_timeout(self):
        response = Mock()
        response.json.return_value = [{"universe": []}, []]
        with patch("startup_network.requests.post", return_value=response) as request:
            post_info("https://example.invalid", {"type": "metaAndAssetCtxs"})
        response.raise_for_status.assert_called_once()
        self.assertEqual(request.call_args.kwargs["timeout"], 10)

    def test_failed_child_remains_visible_on_multiple_status_polls(self):
        import bot_server as server
        manager = server.BotManager()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / "bot.log"
            log.write_text("Hyperliquid inaccessible : ConnectionError\n", encoding="utf-8")
            process = Mock()
            process.poll.return_value = 1
            manager.obi_procs["SP500"] = dict(
                proc=process, symbol="xyz:SP500", stats_file=str(root / "missing.json"),
                log=str(log), log_start=0, log_f=open(log, "ab"),
                started=time.time(), config={"strategy": "mid", "mode": "live"})
            for _ in range(2):
                card = manager.get_grid_stats()["bots"][0]
                self.assertFalse(card["running"])
                self.assertEqual(card["phase"], "failed")
                self.assertIn("Hyperliquid inaccessible", card["error"])

    def test_websocket_thread_starts_only_after_metadata_succeeds(self):
        source = Path(__file__).resolve().parents[1] / "OBI_VOLUME_BOT.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "OrderBookBot")
        method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "start_ws")
        info = Mock()
        factory = Mock(side_effect=[requests.ConnectionError("reset"), info])
        namespace = dict(Info=factory, BASE_URL="https://example.invalid", PERP_DEXS=["xyz"],
                         SYMBOL="xyz:SP500", ACCOUNT_ADDRESS="test", MACRO_ENABLE=False,
                         retry_read=lambda label, op: retry_read(label, op, sleep=Mock()))
        exec(compile(ast.Module(body=[method], type_ignores=[]), str(source), "exec"), namespace)
        bot = types.SimpleNamespace(fixed_spot_meta={}, _on_l2=Mock(), _on_trades=Mock(),
                                    _on_user_fills=Mock(), _on_order_updates=Mock(), experimental=None)
        with patch("hyperliquid.websocket_manager.WebsocketManager") as websocket:
            namespace["start_ws"](bot)
            websocket.assert_called_once()
            websocket.return_value.start.assert_called_once()
        self.assertEqual(factory.call_count, 2)
        self.assertTrue(factory.call_args.kwargs["skip_ws"])
        self.assertEqual(info.subscribe.call_count, 4)


if __name__ == "__main__":
    unittest.main()
