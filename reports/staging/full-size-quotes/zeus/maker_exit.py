"""Bounded maker exit protection using observed fees; never authorizes taker."""
import math


def exit_price(base, buy, entry, tick, opening_rate, maker_rate):
    """Break-even after estimated fees plus one tick, rounded away from loss.

    Rates are conservative maxima observed in fills on this exact market.
    Returns None when costs cannot be estimated, rather than assuming zero.
    """
    values=(base,entry,tick,opening_rate,maker_rate)
    if any(v is None or not math.isfinite(v) for v in values):
        return None
    if min(base,entry,tick)<=0 or not 0<=opening_rate<1 or not 0<=maker_rate<1:
        return None
    if buy:
        target=(entry*(1-opening_rate)-tick)/(1+maker_rate)
        return min(base, math.floor((target+tick*1e-9)/tick)*tick) if target>0 else None
    target=(entry*(1+opening_rate)+tick)/(1-maker_rate)
    return max(base, math.ceil((target-tick*1e-9)/tick)*tick)
