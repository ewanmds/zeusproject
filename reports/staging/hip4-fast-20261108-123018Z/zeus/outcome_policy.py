"""User exclusions shared by Outcome discovery and the final worker admission."""
import re


# All children of the currently listed FOMC rate-decision question, both sides.
FED_RATE_OUTCOME_IDS = frozenset({19987, 19988, 19989, 19990})


def blocked_outcome(row):
    try:
        outcome_id = int(row.get('id', row.get('outcome_id', 0)))
    except (TypeError, ValueError):
        outcome_id = 0
    text = ' '.join(str(row.get(key) or '') for key in
                    ('name', 'raw_name', 'question_title', 'question_description', 'description'))
    fed = re.search(r'\b(?:fed|fomc|federal reserve|federal open market committee|federal funds)\b', text, re.I)
    rates = re.search(r'\b(?:rate|rates|interest|policyRate\w*)\b', text, re.I)
    return outcome_id in FED_RATE_OUTCOME_IDS or bool(fed and rates)


def assert_coin_allowed(coin):
    if coin.startswith('#') and coin[1:].isdigit() and int(coin[1:])//10 in FED_RATE_OUTCOME_IDS:
        raise ValueError('Fed interest-rate markets are excluded')
