import unittest
from test_adapter import META
from zeus.bots import freqtrade as ft

class MakerExitConfigTests(unittest.TestCase):
    def test_no_routine_strategy_ioc_in_zeus(self):
        cfg=ft.session_config("xyz:SP500",100,META)
        self.assertEqual(cfg["market_making"]["price_shock_favorable_ioc_frac"],0)
        self.assertFalse(cfg["market_making"]["shock_chain_enabled"])
        self.assertTrue(cfg["risk"]["session_flatten_on_shutdown"])
        self.assertEqual(cfg["risk"]["max_position_notional"],100)
