import calendar
import os
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch


os.environ.setdefault("PRIVATE_KEY", "0x" + "11" * 32)
os.environ.setdefault("WALLET_ADDRESS", "0x" + "22" * 20)

import OBI_VOLUME_BOT as bot
from obi_report import build_context_report, build_ranking, load_fills
from obi_telemetry import FillLogger


class TelemetryTests(unittest.TestCase):
    def test_async_fill_and_markout_are_persisted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fills.db"
            logger = FillLogger("xyz:TEST", str(path))
            logger.log_fill({
                "tid": "fill-1",
                "ts": 1_700_000_000.0,
                "side": "B",
                "px": 100.0,
                "sz": 2.0,
                "fee": 0.01,
                "closed_pnl": 0.03,
                "crossed": 0,
                "z_ofi": 1.5,
                "z_tfi": -0.2,
                "alpha_bps": 0.4,
                "spread_bps": 1.0,
                "vol_bps": 0.5,
                "regime": "",
                "mid": 100.0,
                "position_before": 0.0,
                "dist_bbo_ticks": 2.0,
                "hour_utc": 22,
            })
            logger.log_markout("fill-1", 5.0, 0.75)
            logger.close()

            con = sqlite3.connect(path)
            try:
                self.assertEqual(con.execute("SELECT COUNT(*) FROM fills").fetchone()[0], 1)
                self.assertEqual(con.execute("SELECT value FROM metadata WHERE key='symbol'").fetchone()[0],
                                 "xyz:TEST")
                self.assertAlmostEqual(
                    con.execute("SELECT markout_bps FROM markouts").fetchone()[0], 0.75
                )
            finally:
                con.close()

            rows = load_fills(path, 0.0)
            self.assertEqual(rows[0]["symbol"], "xyz:TEST")
            report = build_context_report(rows, 1_700_000_100.0)
            self.assertEqual(report[0]["z_bucket"], "+1:+2")
            self.assertEqual(report[0]["distance_bucket"], "2-3")
            self.assertAlmostEqual(report[0]["markout_5s"], 0.75)
            ranking = build_ranking(rows, 1_700_000_100.0, 48.0)
            self.assertEqual(ranking["ranking"][0]["symbol"], "xyz:TEST")


class SessionTests(unittest.TestCase):
    @staticmethod
    def _utc(hour, minute):
        return calendar.timegm((2026, 7, 17, hour, minute, 0))

    def test_cash_session_and_open_blackout(self):
        windows = ((13 * 60 + 30, 20 * 60),)
        self.assertEqual(bot.session_state_utc(windows, self._utc(13, 20), 10), "blackout")
        self.assertEqual(bot.session_state_utc(windows, self._utc(13, 35), 10), "blackout")
        self.assertEqual(bot.session_state_utc(windows, self._utc(13, 40), 10), "open")
        self.assertEqual(bot.session_state_utc(windows, self._utc(21, 0), 10), "off")
        self.assertEqual(bot.session_state_utc((), self._utc(21, 0), 10), "open")


class QueueModelTests(unittest.TestCase):
    def test_queue_fraction_uses_displayed_size_and_own_size(self):
        instance = bot.OBIVolumeBot.__new__(bot.OBIVolumeBot)
        instance.lock = threading.Lock()
        instance.orders = {
            7: {"side": "B", "px": 100.0, "sz": 2.0, "ro": False}
        }
        instance.queue_init = {7: 10.0}
        instance.queue_ahead = {7: 10.0}

        class Signal:
            @staticmethod
            def level_size(side, px):
                return 5.0

        instance.signal = Signal()
        self.assertAlmostEqual(instance._queue_fraction("B"), 0.3)
        self.assertEqual(instance.queue_ahead[7], 3.0)

    def test_cross_dex_surface_is_absent(self):
        signal = bot.SignalEngine()
        self.assertFalse(hasattr(signal, "xdex_zscore"))


class HourlyRiskTests(unittest.TestCase):
    def test_hourly_loss_uses_opening_pause_not_hard_risk_pause(self):
        instance = bot.OBIVolumeBot.__new__(bot.OBIVolumeBot)
        instance.lock = threading.Lock()
        instance.position = 0.0
        instance.unrealized_pnl = 0.0
        instance.mid = 100.0
        instance.pos_since = 0.0
        instance.session_pnl = 0.0
        instance.total_fees = 0.0
        instance.hourly_net_events = __import__("collections").deque([(1_700_000_000.0, -3.0)])
        instance.hourly_net_pnl = 0.0
        instance.hourly_loss_latched = False
        instance.hourly_pause_until = 0.0
        instance.pause_until = 0.0
        instance.session_net_high = 0.0
        instance.protect = 0
        instance.size_mult = 1.0
        instance.upnl_peak_bps = 0.0
        instance._last_regime = None
        instance.force_quote = False
        instance.running = True
        instance.last_action = ""
        instance.last_tick_ts = 1_700_000_000.0
        instance.ws_ready = False

        class Signal:
            @staticmethod
            def vol_bps():
                return 0.0

            @staticmethod
            def zscore():
                return 0.0

            @staticmethod
            def regime():
                return None, 0

        instance.signal = Signal()
        with patch.object(bot.time, "time", return_value=1_700_000_010.0), \
                patch.object(bot, "MAX_SESSION_LOSS_USD", 5.0), \
                patch.object(bot, "HOURLY_LOSS_MULT", 0.5), \
                patch.object(bot, "HOURLY_PAUSE_SEC", 900.0):
            instance.risk_checks()

        self.assertGreater(instance.hourly_pause_until, 1_700_000_010.0)
        self.assertEqual(instance.pause_until, 0.0)
        self.assertTrue(instance.hourly_loss_latched)


if __name__ == "__main__":
    unittest.main()
