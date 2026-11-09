"""Outcome builder-code attribution for Hyperliquid orders.

Outcome's liquidity-rewards campaign only scores orders that carry its
builder code. The fee is allowed to be zero - what matters is the code
itself - so attribution costs the trader nothing.
"""

from __future__ import annotations

from typing import Dict

# Published in https://docs.outcome.xyz/outcome-liquidity-rewards
OUTCOME_BUILDER_ADDRESS = "0xab5dbc057628bc18523c4cdfc0e1e2ebdbecb704"

# Tenths of a basis point. Zero keeps every order fee-free for the trader.
OUTCOME_BUILDER_FEE = 0


def outcome_builder(fee: int = OUTCOME_BUILDER_FEE) -> Dict[str, object]:
    """Return a fresh BuilderInfo dict so callers never share mutable state."""
    return {"b": OUTCOME_BUILDER_ADDRESS, "f": int(fee)}
