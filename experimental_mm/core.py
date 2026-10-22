"""Pure, versioned policy shared by live execution and chronological replay."""
from collections import deque
from dataclasses import asdict, dataclass
import hashlib
import json
import math

SYMBOL = "xyz:SP500"
SCHEMA = 1


@dataclass(frozen=True)
class Policy:
    min_notional: float = 10.5
    min_rest: float = 2.5
    replace_ticks: int = 2
    ofi_limit: float = 0.5
    edge_buffer_bps: float = 0.2
    book_max_age: float = 2.0
    fee_max_age: float = 300.0
    exit_improve_age: float = 8.0
    max_hold: float = 120.0
    stop_bps: float = 6.0
    maker_exit_window: float = 4.0
    report_max_age: float = 86400.0


POLICY = Policy()
POLICY_HASH = hashlib.sha256(json.dumps(asdict(POLICY), sort_keys=True).encode()).hexdigest()
OFFSETS = (0, 1, 2, 4)
HORIZONS = (0.25, 1.0, 3.0, 6.0)


def finite(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("non-finite numeric input")
    return number


def tick_size(price, decimals):
    return max(10.0 ** (math.floor(math.log10(price)) + 1 - 5), 10.0 ** (-max(0, 6 - decimals)))


def price_round(price, tick, buy, decimals):
    integer = math.floor(round(price / tick, 9)) if buy else math.ceil(round(price / tick, 9))
    return round(integer * tick, max(0, 6 - decimals))


def minimum_size(price, decimals):
    unit = 10.0 ** -decimals
    return round(math.ceil(POLICY.min_notional / price / unit - 1e-10) * unit, decimals)


def verified_fees(user_fees, meta, observed_at):
    """Conservative positive fee bound; never assume unknown HIP-3 metadata.

    No referral/aligned discount and no maker rebate are credited. Rates are
    fractions, not bps. USDC collateral only, as required by this v1 ledger.
    """
    if meta.get("collateralToken") != 0:
        raise ValueError("USDC collateral metadata required")
    asset = next(a for a in meta["universe"] if a["name"] == SYMBOL)
    growth = asset.get("growthMode")
    if growth not in ("enabled", "disabled", True, False):
        raise ValueError("unknown growthMode")
    scale = finite(asset["deployerFeeScale"])
    if not 0 <= scale <= 10 or asset.get("isDelisted"):
        raise ValueError("invalid fee scale / delisted asset")
    decimals = asset["szDecimals"]
    if type(decimals) is not int or not 0 <= decimals <= 6:
        raise ValueError("invalid size precision")
    multiplier = (1 + scale if scale < 1 else 2 * scale) * (0.1 if growth in ("enabled", True) else 1)
    maker, taker = finite(user_fees["userAddRate"]), finite(user_fees["userCrossRate"])
    if not -0.01 <= maker <= 0.01 or not 0 <= taker <= 0.01:
        raise ValueError("invalid user fee rates")
    result = dict(maker=max(0.0, maker) * multiplier, taker=taker * multiplier,
                  decimals=decimals, symbol=SYMBOL, observed_at=finite(observed_at),
                  basis="conservative_bound_no_referral_aligned_or_rebate")
    result["fingerprint"] = hashlib.sha256(json.dumps(
        {k: v for k, v in result.items() if k != "observed_at"}, sort_keys=True).encode()).hexdigest()
    return result


class Features:
    def __init__(self):
        self.book = None
        self.ofi = deque()

    def update(self, ts, bids, asks):
        if not bids or not asks:
            raise ValueError("empty book")
        bb, bsz = finite(bids[0]["px"]), finite(bids[0]["sz"])
        ba, asz = finite(asks[0]["px"]), finite(asks[0]["sz"])
        if bb <= 0 or ba <= bb or bsz < 0 or asz < 0:
            raise ValueError("invalid book")
        if self.book:
            prev = self.book
            if ts < prev["ts"]:
                raise ValueError("out-of-order book")
            if ts - prev["ts"] > POLICY.book_max_age:
                self.ofi.clear()
            delta = ((bsz if bb >= prev["bid"] else 0) - (prev["bid_size"] if bb <= prev["bid"] else 0)
                     - (asz if ba <= prev["ask"] else 0) + (prev["ask_size"] if ba >= prev["ask"] else 0))
            self.ofi.append((ts, delta))
        while self.ofi and self.ofi[0][0] < ts - 1:
            self.ofi.popleft()
        depth = max(bsz + asz, 1e-9)
        self.book = dict(ts=ts, bid=bb, ask=ba, mid=(bb + ba) / 2, bid_size=bsz, ask_size=asz,
                         ofi=max(-10, min(10, sum(item[1] for item in self.ofi) / depth)),
                         microprice=(ba * bsz + bb * asz) / depth)
        return self.book


def quotes(book, now, decimals, offset, maker_rate, position=0.0, position_since=0.0):
    """Return side -> (price, size, reduce_only). A stale book never opens risk."""
    if not book or now < book["ts"] or now - book["ts"] > POLICY.book_max_age:
        return {}
    bb, ba, mid = book["bid"], book["ask"], book["mid"]
    tick = tick_size(mid, decimals)
    if position:
        buy = position < 0
        price = bb if buy else ba
        if now - position_since >= POLICY.exit_improve_age:
            price = ba - tick if buy else bb + tick
        price = price_round(price, tick, buy, decimals)
        return {"B" if buy else "A": (price, round(abs(position), decimals), True)}
    if offset not in OFFSETS or not math.isfinite(maker_rate) or maker_rate < 0:
        return {}
    bid = price_round(bb - offset * tick, tick, True, decimals)
    ask = price_round(ba + offset * tick, tick, False, decimals)
    if bid <= 0 or (ask - bid) / mid * 1e4 < 2 * maker_rate * 1e4 + POLICY.edge_buffer_bps:
        return {}
    # Use the lowest candidate price so that either side still clears $10.50.
    size = minimum_size(bid, decimals)
    result = {}
    if book["ofi"] > -POLICY.ofi_limit:
        result["B"] = (bid, size, False)
    if book["ofi"] < POLICY.ofi_limit:
        result["A"] = (ask, size, False)
    return result


def retain(old, want, now, tick):
    """Only opening quotes use hysteresis. Pulls and exits bypass resting time."""
    if want is None or old.get("ro") != want[2] or want[2]:
        return False
    return now - old["ts"] < POLICY.min_rest or abs(old["px"] - want[0]) < POLICY.replace_ticks * tick - tick * 1e-6


def safe_resting(old, want, book, maker_rate):
    if not want or old["ro"] != want[2] or want[2]:
        return False
    signed_edge = (book["mid"] - old["px"]) / book["mid"] * 1e4 * (1 if old["side"] == "B" else -1)
    return signed_edge >= maker_rate * 1e4 + POLICY.edge_buffer_bps / 2


class Markouts:
    def __init__(self):
        self.pending = deque()
        self.stats = {str(h): {side: {"count": 0, "sum_bps": 0.0} for side in ("B", "A")} for h in HORIZONS}
        self.expired = 0

    def fill(self, ts, side, price):
        if side in ("B", "A") and price > 0:
            self.pending.append([ts, side, price, set(HORIZONS)])

    def book(self, ts, mid):
        keep = deque()
        for stamp, side, price, remaining in self.pending:
            for horizon in tuple(remaining):
                if ts < stamp + horizon:
                    continue
                remaining.remove(horizon)
                # Do not invent a 250ms markout from a several-second-old gap.
                if ts - stamp - horizon > POLICY.book_max_age:
                    self.expired += 1
                    continue
                stat = self.stats[str(horizon)][side]
                stat["count"] += 1
                stat["sum_bps"] += (mid - price) / price * 1e4 * (1 if side == "B" else -1)
            if remaining:
                keep.append([stamp, side, price, remaining])
        self.pending = keep

    def snapshot(self):
        return {h: {side: dict(count=s["count"], mean_bps=s["sum_bps"] / s["count"] if s["count"] else None)
                    for side, s in sides.items()} for h, sides in self.stats.items()}
