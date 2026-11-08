"""Fed rate family is banned without excluding unrelated economic markets."""
import unittest
from zeus.outcome_policy import assert_coin_allowed, blocked_outcome
from zeus.outcome_flow import before_event
from zeus.bots.outcome_worker import build_config


class OutcomePolicyTests(unittest.TestCase):
    def test_all_current_children_and_both_sides_are_blocked(self):
        for outcome in (19987, 19988, 19989, 19990):
            self.assertTrue(blocked_outcome({'id': outcome}))
            for side in (0, 1):
                with self.assertRaisesRegex(ValueError, 'excluded'):
                    assert_coin_allowed(f'#{outcome*10+side}')

    def test_generic_names_are_blocked_by_parent_metadata(self):
        for name in ('Increase', 'Decrease', 'No change', 'Other'):
            row = {'id': 99999, 'name': name, 'question_title':
                   'Federal Open Market Committee January 2027 rate decision'}
            self.assertTrue(blocked_outcome(row))
            self.assertFalse(before_event(row, 1000))

    def test_worker_rejects_before_initialization_or_submission(self):
        with self.assertRaisesRegex(ValueError, 'excluded'):
            build_config({'market': '#199901'}, None)

    def test_unrelated_economics_and_sports_are_allowed(self):
        for row in ({'name': 'Inflation rate in December'}, {'name': 'Napoli'},
                    {'name': 'Fed chair nomination'}, {'name': 'Increase', 'question_title': 'ECB rate decision'}):
            self.assertFalse(blocked_outcome(row))
        assert_coin_allowed('#257961')


if __name__ == '__main__':
    unittest.main()
