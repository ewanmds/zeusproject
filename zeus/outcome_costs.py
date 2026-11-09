"""Read-only, account-scoped Outcome fee estimates. No signing or orders.

Opening a long token is free; selling it pays the spot rate multiplied by
the Outcome and deployer scales. A positive touch edge is an estimate,
not protection against a later price move or an emergency taker exit.
"""
import math
import threading
import time

import requests

MIN_NET_EDGE_BPS = 1.0
_profiles = {}
_pending = set()
_retry_at = {}
_lock = threading.Lock()


def base_maker_rate(payload):
    if any(isinstance(payload[key], bool) for key in ('userSpotAddRate', 'activeReferralDiscount')):
        raise ValueError('Invalid account fee response')
    rate = float(payload['userSpotAddRate'])
    discount = float(payload['activeReferralDiscount'])
    if not math.isfinite(rate) or not math.isfinite(discount) or not 0 <= discount <= 1 or abs(rate) >= 1:
        raise ValueError('Invalid account fee response')
    # Outcome markets never pay rebates. userSpotAddRate already includes the
    # account's staking tier; referral reduction is a separate multiplier.
    return max(0.0, rate) * (1-discount)


def closing_rate(base_rate, fee_scale, deployer_scale):
    if any(isinstance(v, bool) for v in (base_rate, fee_scale, deployer_scale)):
        raise ValueError('Invalid Outcome fee metadata')
    values = [float(base_rate), float(fee_scale), float(deployer_scale)]
    if not all(math.isfinite(v) and v >= 0 for v in values):
        raise ValueError('Outcome fee metadata unavailable')
    base, scale, deployer = values
    rate = base * scale * (deployer + max(deployer, 1.0))
    if rate >= 1:
        raise ValueError('Invalid Outcome fee rate')
    return rate


def net_edge_bps(bid, ask, rate):
    bid, ask, rate = float(bid), float(ask), float(rate)
    if not all(math.isfinite(v) for v in (bid, ask, rate)) or not 0 < bid < ask < 1 or not 0 <= rate < 1:
        raise ValueError('Invalid cost estimate inputs')
    return 10000 * (ask * (1-rate) - bid) / ((bid+ask)/2)


def _read_profile(account):
    response = requests.post('https://api.hyperliquid.xyz/info',
                             json={'type': 'userFees', 'user': account}, timeout=3)
    response.raise_for_status()
    return base_maker_rate(response.json())


def _refresh(account):
    try:
        rate = _read_profile(account)
        with _lock:
            _profiles[account] = (time.monotonic(), rate)
    except (requests.RequestException, ValueError, KeyError, TypeError):
        pass  # Expired/unknown fees block new entries, never position exits.
    finally:
        with _lock:
            _pending.discard(account)


def maker_profile(account=None):
    """Cached nonblocking read; initial/missing data returns None until ready.

    Public scans without a wallet use the conservative base spot maker tier.
    Cached account values refresh in the background after 60s and expire at
    5min. Neither order-book callbacks nor API polling wait on this request.
    """
    if not account:
        return 0.0004
    account = account.lower()
    with _lock:
        cached = _profiles.get(account)
        age = time.monotonic()-cached[0] if cached else math.inf
        if age > 60 and account not in _pending and time.monotonic() >= _retry_at.get(account, 0):
            _pending.add(account)
            _retry_at[account] = time.monotonic()+10
            threading.Thread(target=_refresh, args=(account,), name='outcome-fees', daemon=True).start()
        return cached[1] if cached and age <= 300 else None


def candidate_cost(candidate, base_rate):
    rate = closing_rate(base_rate, candidate['fee_scale'], candidate['deployer_fee_scale'])
    edge = net_edge_bps(candidate['bid'], candidate['ask'], rate)
    return {'maker_close_fee_bps': rate*10000, 'net_touch_edge_bps': edge,
            'cost_eligible': edge >= MIN_NET_EDGE_BPS}
