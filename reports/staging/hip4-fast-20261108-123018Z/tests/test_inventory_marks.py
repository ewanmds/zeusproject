"""Pure inventory and risk mark regressions: no account, HTTP or execution."""
from dataclasses import FrozenInstanceError
import math
import unittest

from zeus.inventory import InventoryDesyncError, apply_fill, mark_inventory


class FillBasisTests(unittest.TestCase):
    def test_open_long_and_short_use_the_actual_fill_price(self):
        for side, quantity in (("B", .058), ("A", -.058)):
            state = apply_fill(0, 0, side, 1709.9, .058, quantity_decimals=3)
            self.assertEqual(state.new_position, quantity)
            self.assertEqual(state.entry_price, 1709.9)
            self.assertTrue(state.starts_new_position)
            self.assertFalse(state.became_flat)

    def test_same_direction_partial_fills_weight_entry_by_quantity(self):
        for sign, side in ((1,"B"),(-1,"A")):
            state = apply_fill(sign*.02, 100, side, 102, .03, quantity_decimals=3)
            self.assertAlmostEqual(state.new_position, sign*.05)
            self.assertAlmostEqual(state.entry_price, 101.2)
            self.assertFalse(state.starts_new_position)

    def test_partial_close_keeps_cost_basis_and_only_reduces_pnl_quantity(self):
        state = apply_fill(.058, 1709.9, "A", 1709.05, .049, quantity_decimals=3)
        self.assertEqual(state.new_position, .009)
        self.assertEqual(state.entry_price, 1709.9)
        self.assertFalse(state.starts_new_position)
        self.assertFalse(state.became_flat)
        mark = mark_inventory(state.new_position, state.entry_price, 1709, 1709.1)
        self.assertAlmostEqual(mark.unrealized_pnl, -.0081)
        self.assertGreater(mark.pnl_bps, -6)

    def test_full_close_resets_entry_and_mark_even_without_a_new_book(self):
        state = apply_fill(-.058, 1709.9, "B", 1709.8, .058, quantity_decimals=3)
        self.assertEqual((state.new_position,state.entry_price),(0,0))
        self.assertTrue(state.became_flat)
        self.assertFalse(state.starts_new_position)
        mark = mark_inventory(state.new_position,state.entry_price,0,0)
        self.assertEqual((mark.unrealized_pnl,mark.pnl_bps,mark.source),(0,0,"flat"))

    def test_direct_flip_starts_residual_at_fill_price(self):
        for sign, side in ((1,"A"),(-1,"B")):
            state = apply_fill(sign*.058, 1709.9, side, 1706.6, .116, quantity_decimals=3)
            self.assertEqual(state.new_position, -sign*.058)
            self.assertEqual(state.entry_price,1706.6)
            self.assertTrue(state.starts_new_position)
            self.assertFalse(state.became_flat)

    def test_close_and_reopen_cannot_reuse_the_previous_loss(self):
        flat = apply_fill(.058,1709.9,"A",1706.6,.058,quantity_decimals=3)
        opened = apply_fill(flat.new_position,flat.entry_price,"B",1706.6,.058,quantity_decimals=3)
        mark = mark_inventory(opened.new_position,opened.entry_price,1706.5,1706.7,source="mid")
        self.assertAlmostEqual(mark.unrealized_pnl,0)
        self.assertTrue(opened.starts_new_position)

    def test_late_or_duplicate_fill_must_reconcile_instead_of_double_applying(self):
        with self.assertRaises(InventoryDesyncError):
            apply_fill(.058,1706.6,"B",1706.6,.058,quantity_decimals=3,reported_start_position=0)
        valid = apply_fill(0,0,"B",1706.6,.058,quantity_decimals=3,reported_start_position="0.0")
        self.assertEqual(valid.new_position,.058)

    def test_invalid_fills_and_unknown_existing_basis_are_rejected(self):
        cases = ((0,0,"buy",100,1),(0,0,"B",0,1),(0,0,"B",100,0),
                 (0,0,"B",100,-1),(1,0,"B",100,1),(0,-1,"B",100,1),
                 (math.nan,100,"B",100,1),(0,0,"B",math.inf,1),
                 (0,0,"B",100,True))
        for args in cases:
            with self.subTest(args=args), self.assertRaises(ValueError):
                apply_fill(*args)

    def test_size_precision_is_validated_not_silently_rounded_to_zero(self):
        with self.assertRaises(ValueError):
            apply_fill(0,0,"B",100,.0004,quantity_decimals=3)
        for invalid in (-1,13,True,3.0):
            with self.assertRaises(ValueError):
                apply_fill(0,0,"B",100,1,quantity_decimals=invalid)

    def test_transition_is_immutable_and_accepts_wire_numbers(self):
        state = apply_fill("0","0","B","100",".058",quantity_decimals=3)
        with self.assertRaises(FrozenInstanceError):
            state.entry_price=1

    def test_smallest_supported_quantum_is_not_discarded(self):
        state=apply_fill(0,0,"B",100,1e-12,quantity_decimals=12)
        self.assertEqual(state.new_position,1e-12)
        with self.assertRaises(InventoryDesyncError):
            apply_fill(1e-12,100,"B",100,1e-12,quantity_decimals=12,reported_start_position=0)


class CurrentMarkTests(unittest.TestCase):
    def test_long_and_short_use_conservative_sides(self):
        long=mark_inventory(1,100,99,101)
        short=mark_inventory(-1,100,99,101)
        self.assertEqual((long.mark_price,long.unrealized_pnl,long.source),(99,-1,"best_bid"))
        self.assertEqual((short.mark_price,short.unrealized_pnl,short.source),(101,-1,"best_ask"))

    def test_explicit_mid_mark_is_distinct_from_close_side_mark(self):
        mid=mark_inventory(1,100,99,101,source="mid")
        self.assertEqual((mid.mark_price,mid.unrealized_pnl,mid.source),(100,0,"mid"))

    def test_mark_reports_denominator_used_by_bps(self):
        mark=mark_inventory(2,100,99,100)
        self.assertEqual(mark.notional,198)
        self.assertAlmostEqual(mark.pnl_bps,-2/198*10000)

    def test_old_invalid_or_missing_book_never_becomes_a_zero_loss(self):
        for args, kwargs in (((1,100,0,0),{}),((1,100,101,100),{}),
                ((1,0,99,101),{}),((1,100,math.nan,101),{}),
                ((1,100,99,101),{"book_age":13,"max_book_age":12}),
                ((1,100,99,101),{"book_age":None,"max_book_age":12}),
                ((1,100,99,101),{"book_age":-1,"max_book_age":12})):
            with self.subTest(args=args,kwargs=kwargs), self.assertRaises(ValueError):
                mark_inventory(*args,**kwargs)

    def test_fresh_mark_and_zero_position_are_explicit(self):
        self.assertEqual(mark_inventory(1,100,99,101,book_age=.01,max_book_age=1).source,"best_bid")
        self.assertEqual(mark_inventory(0,100,None,None).source,"flat")

    def test_invalid_source_and_overflow_are_rejected(self):
        with self.assertRaises(ValueError):mark_inventory(1,100,99,101,source="REST")
        with self.assertRaises(ValueError):mark_inventory(1e308,1,1e308,1.1e308)


if __name__=="__main__":unittest.main()
