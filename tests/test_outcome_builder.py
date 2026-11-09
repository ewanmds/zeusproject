"""Outcome builder-code attribution must follow every order."""
from __future__ import annotations

import ast
import inspect
import unittest

from zeus.builder import OUTCOME_BUILDER_ADDRESS, outcome_builder


class RecordingExchange:
    def __init__(self):
        self.calls = []

    def order(self, **kwargs):
        self.calls.append(kwargs)
        return {"status": "ok"}


class OutcomeBuilderTests(unittest.TestCase):
    def test_address_matches_published_campaign_code(self):
        self.assertEqual(
            OUTCOME_BUILDER_ADDRESS,
            "0xab5dbc057628bc18523c4cdfc0e1e2ebdbecb704",
        )

    def test_builder_is_zero_fee_and_fresh(self):
        first, second = outcome_builder(), outcome_builder()
        self.assertEqual(first, {"b": OUTCOME_BUILDER_ADDRESS, "f": 0})
        self.assertIsNot(first, second)

    def test_submit_order_attaches_builder(self):
        from zeus.bots.worldcup import HyperliquidSpreadBot

        bot = object.__new__(HyperliquidSpreadBot)
        bot.exchange = RecordingExchange()
        bot._submit_order(
            name="#225741",
            is_buy=True,
            sz=10,
            limit_px=0.5,
            order_type={"limit": {"tif": "Alo"}},
        )
        call = bot.exchange.calls[0]
        self.assertEqual(call["builder"], {"b": OUTCOME_BUILDER_ADDRESS, "f": 0})
        self.assertEqual(call["name"], "#225741")
        self.assertTrue(call["is_buy"])

    def test_explicit_builder_is_not_overridden(self):
        from zeus.bots.worldcup import HyperliquidSpreadBot

        bot = object.__new__(HyperliquidSpreadBot)
        bot.exchange = RecordingExchange()
        custom = {"b": "0x" + "1" * 40, "f": 1}
        bot._submit_order(
            name="#1",
            is_buy=False,
            sz=1,
            limit_px=0.5,
            order_type={"limit": {"tif": "Ioc"}},
            builder=custom,
        )
        self.assertEqual(bot.exchange.calls[0]["builder"], custom)

    def test_worldcup_routes_every_order_through_helper(self):
        from zeus.bots import worldcup

        def calls_exchange_order(function):
            for node in ast.walk(function):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if (isinstance(func, ast.Attribute) and func.attr == "order"
                        and isinstance(func.value, ast.Attribute)
                        and func.value.attr == "exchange"
                        and isinstance(func.value.value, ast.Name)
                        and func.value.value.id == "self"):
                    return True
            return False

        functions = [n for n in ast.walk(ast.parse(inspect.getsource(worldcup)))
                     if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        offenders = [f.name for f in functions
                     if f.name != "_submit_order" and calls_exchange_order(f)]
        self.assertEqual(offenders, [], "orders must go through _submit_order")
        helper = [f for f in functions if f.name == "_submit_order"]
        self.assertTrue(helper and calls_exchange_order(helper[0]))


if __name__ == "__main__":
    unittest.main()
