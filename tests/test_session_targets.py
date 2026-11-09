"""Targets preserve maker exits and survive rotations; no real orders."""
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from zeus.session_targets import finish_reason
from zeus.outcome_rotation import OutcomeRotation
from zeus.bots.outcome_worker import build_config
from zeus.bots.worldcup import HyperliquidSpreadBot, MarketState, MarketSpec, SessionStats


class SessionTargetsTests(unittest.TestCase):
    def test_deadline_and_volume_include_prior_markets(self):
        self.assertIsNone(finish_reason({}, 10000, 1000))
        self.assertIsNone(finish_reason({'session_expires_at':1001}, 0, 1000))
        self.assertIn('duration',finish_reason({'session_expires_at':1000},None,1000))
        self.assertIsNone(finish_reason({'target_volume_usd':100,'session_volume_carry':70},29,1000))
        self.assertIn('volume',finish_reason({'target_volume_usd':100,'session_volume_carry':70},30,1000))

    def test_expired_bot_stops_bids_but_keeps_inventory_ask_passive(self):
        bot=HyperliquidSpreadBot.__new__(HyperliquidSpreadBot)
        bot.cfg=build_config({'market':'#101','order_size_usd':25,'max_position_usd':200,'exec_mode':'maker',
            'selection_mode':'auto','two_sided':True,'session_expires_at':time.time()-1},None)
        bot.stats=SessionStats();bot.available_usdc=100;bot.funds=None
        st=MarketState(MarketSpec('#101',0,balance_coin='+101'),position=10,available_position=10)
        st.update_bbo(.4,.40001)
        plan=bot.bilateral_requests(st)
        self.assertNotIn(True,plan);self.assertIn(False,plan)
        self.assertEqual(plan[False]['order_type'],{'limit':{'tif':'Alo'}})
        self.assertLess(plan[False]['sz']*plan[False]['limit_px'],25) # Final residual exit, no new minimum order.

    def test_target_finishes_without_scanner_or_replacement(self):
        with tempfile.TemporaryDirectory() as folder:
            row={'session_id':'one','running':True,'cleanup_confirmed':False,'phase':'running',
                 'market':'#101','volume_usd':100,'fills':2}
            owner=SimpleNamespace(procs={'one':{'stop_file':str(Path(folder)/'stop')}},
                                  status=Mock(side_effect=lambda:{'bots':[row]}),start=Mock())
            rotation=OutcomeRotation(owner)
            with patch.object(rotation,'_loop'):
                rotation.register({'session_id':'one'},{'selection_mode':'auto','target_volume_usd':100,'order_size_usd':25})
            with patch('zeus.outcome_flow.scanner.snapshot') as scan:
                rotation.tick();scan.assert_not_called();owner.start.assert_not_called()
                self.assertTrue((Path(folder)/'drain.json').exists())
                self.assertEqual(rotation.groups['one']['state'],'finishing')
                row.update(running=False,cleanup_confirmed=True)
                rotation.tick();self.assertTrue(rotation.groups['one']['cancelled'])
            rotation.close()

    def test_expiry_blocks_final_replacement_admission(self):
        rotation=OutcomeRotation(SimpleNamespace())
        rotation.groups['old']={'cancelled':False,'config':{'session_expires_at':time.time()-1}}
        with self.assertRaises(RuntimeError):rotation.assert_admission('old')


if __name__=='__main__':unittest.main()
