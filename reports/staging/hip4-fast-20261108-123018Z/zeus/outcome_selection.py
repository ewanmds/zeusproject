"""Pure, bounded Auto scheduling heuristics, separate from order execution."""
import math


def no_fill_grace(config, selection):
    window = float(config.get('auto_rotation_after_sec', 180))
    queue = float((selection or {}).get('queue_proxy_sec') or 0)
    # Give a newly joined queue a reasonable chance; never extend the user's
    # configured observation window. This remains an estimate, not a promise.
    return min(window, max(60, queue*1.25))


def candidate_rank(candidate, results, now):
    previous = results.get(candidate['market'], {})
    failures = min(3, previous.get('zero_fill_attempts', 0))
    age = max(0, now-previous.get('updated_at', now))
    penalty = 1+failures*math.exp(-age/1800)
    return (float(candidate.get('queue_proxy_sec', math.inf))*penalty,
            -float(candidate.get('score', 0)), -float(candidate.get('volume_usd_1m', 0)), candidate['market'])
