"""Pure fill cost basis and explicit book marks for perpetual inventory.

No network, clocks, order submission or mutable session state. A caller must
still reconcile authoritative positions before sending any exposure-changing
request. Exchange fills with a mismatched start position are not blindly added
to a position that REST may already have advanced.
"""
from dataclasses import dataclass
import math


class InventoryDesyncError(ValueError):
    """A fill does not start from the inventory state supplied by the caller."""


def _number(value, name):
    if isinstance(value, bool):
        raise ValueError(name + " must be numeric, not boolean")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(name + " must be finite") from exc
    if not math.isfinite(result):
        raise ValueError(name + " must be finite")
    return result


def _quantity(value, name, decimals):
    result = _number(value, name)
    if decimals is not None:
        rounded = round(result, decimals)
        tolerance = max(1e-14, 10.0 ** (-decimals) * 1e-7)
        if abs(result - rounded) > tolerance:
            raise ValueError(name + " exceeds quantity precision")
        result = rounded
    return 0.0 if result == 0 else result


@dataclass(frozen=True)
class InventoryTransition:
    new_position: float
    entry_price: float
    starts_new_position: bool
    became_flat: bool


def apply_fill(previous_position, previous_entry, side, price, size, *,
               quantity_decimals=None, reported_start_position=None):
    """Apply one validated fill without reusing the old position's cost basis.

    Same-direction additions use a quantity-weighted average entry. A partial
    reduction keeps the entry. Flat resets entry to zero. A sign flip starts
    the residual position at this fill's price, independent of the closed lot.
    Fees are accounted separately and do not change this exchange-style entry.

    If a feed supplies startPosition, a mismatch is an explicit reconciliation
    requirement (for example a late fill after an authoritative REST snapshot).
    No mutation or deduplication takes place in this helper on such a mismatch.
    """
    if quantity_decimals is not None and (isinstance(quantity_decimals, bool)
            or not isinstance(quantity_decimals, int) or not 0 <= quantity_decimals <= 12):
        raise ValueError("quantity_decimals must be an integer from 0 to 12")
    previous = _quantity(previous_position, "previous position", quantity_decimals)
    quantity = _quantity(size, "fill size", quantity_decimals)
    entry = _number(previous_entry, "previous entry")
    fill_price = _number(price, "fill price")
    if side not in ("B", "A"):
        raise ValueError("fill side must be B or A")
    if quantity <= 0 or fill_price <= 0:
        raise ValueError("fill size and price must be positive")
    if entry < 0 or previous != 0 and entry <= 0:
        raise ValueError("non-flat inventory requires a known positive entry")
    if reported_start_position is not None:
        reported = _quantity(reported_start_position, "reported start position", quantity_decimals)
        tolerance = max(1e-14, 10.0 ** (-quantity_decimals) * 1e-7) if quantity_decimals is not None else max(1e-14, abs(previous)*1e-12)
        if abs(reported - previous) > tolerance:
            raise InventoryDesyncError("Fill start position differs from local inventory; reconcile before applying")
    signed = quantity if side == "B" else -quantity
    current = _quantity(previous + signed, "resulting position", quantity_decimals)
    if quantity_decimals is None and previous*signed < 0 and math.isclose(abs(previous),quantity,rel_tol=1e-12,abs_tol=0):
        current = 0.0
    starts = current != 0 and (previous == 0 or previous * current < 0)
    became_flat = previous != 0 and current == 0
    if current == 0:
        new_entry = 0.0
    elif starts:
        new_entry = fill_price
    elif previous * signed > 0:
        new_entry = (abs(previous) * entry + quantity * fill_price) / abs(current)
    else:
        new_entry = entry
    if not math.isfinite(new_entry):
        raise ValueError("Resulting entry is not finite")
    return InventoryTransition(current, new_entry, starts, became_flat)


@dataclass(frozen=True)
class InventoryMark:
    mark_price: float
    unrealized_pnl: float
    notional: float
    pnl_bps: float
    source: str


def mark_inventory(position, entry_price, bid, ask, *, source="liquidation",
                   book_age=None, max_book_age=None):
    """Return a transparent current-book PnL, never a cached REST uPnL.

    liquidation: best bid for a long, best ask for a short, excluding fees and
    market impact beyond level one. mid: midpoint, also excluding fees/impact.
    pnl_bps is normalized by the notional at the selected mark. It is not ROE.
    Freshness is enforceable only when the caller provides both age and limit.
    A flat position has zero inventory PnL even before any book is available.
    """
    quantity = _number(position, "position")
    entry = _number(entry_price, "entry price")
    if source not in ("liquidation", "mid"):
        raise ValueError("mark source must be liquidation or mid")
    if entry < 0:
        raise ValueError("entry price must not be negative")
    if quantity == 0:
        return InventoryMark(0.0, 0.0, 0.0, 0.0, "flat")
    if entry <= 0:
        raise ValueError("non-flat inventory requires a known positive entry")
    best_bid, best_ask = _number(bid, "bid"), _number(ask, "ask")
    if best_bid <= 0 or best_ask <= best_bid:
        raise ValueError("mark requires a valid uncrossed book")
    if max_book_age is not None:
        limit = _number(max_book_age, "maximum book age")
        age = _number(book_age, "book age")
        if limit <= 0 or age < 0 or age > limit:
            raise ValueError("mark book is stale or has invalid age")
    elif book_age is not None:
        if _number(book_age, "book age") < 0:
            raise ValueError("book age must not be negative")
    if source == "mid":
        selected = (best_bid + best_ask) / 2.0
        label = "mid"
    elif quantity > 0:
        selected, label = best_bid, "best_bid"
    else:
        selected, label = best_ask, "best_ask"
    pnl = quantity * (selected - entry)
    notional = abs(quantity) * selected
    if not all(math.isfinite(v) for v in (selected, pnl, notional)) or notional <= 0:
        raise ValueError("inventory mark overflows")
    return InventoryMark(selected, pnl, notional, pnl / notional * 10000.0, label)
