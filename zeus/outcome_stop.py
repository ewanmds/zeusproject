"""Read-only attribution for an explicitly requested Outcome market close."""
import math
from decimal import Decimal, InvalidOperation


def attributable_inventory(info, account, symbol, owned_oids, started):
    """Sum only this session's fills, paging without silently truncating history.

    Sessions begin flat. Recovery inherits the original start and all order IDs.
    Missing or ambiguous history blocks liquidation instead of selling a wallet's
    unrelated Outcome balance. Never use a UI volume or cached WS total here.
    """
    if not isinstance(started, (float, int)) or not math.isfinite(started) or started <= 0:
        raise RuntimeError('Missing session inventory attribution timestamp')
    owned = set(map(int, owned_oids))
    cursor = max(0, int(started*1000)-1)
    quantity, seen = Decimal(0), set()
    for _ in range(10):
        rows = info.user_fills_by_time(account, cursor, aggregate_by_time=False)
        if not isinstance(rows, list):
            raise RuntimeError('Invalid session fills response')
        times = []
        for fill in rows:
            try:
                stamp = int(fill['time'])
                times.append(stamp)
                if fill['coin'] != symbol or int(fill['oid']) not in owned:
                    continue
                # tid is the exchange fill identity; hash+oid+time is NOT unique
                # across multiple partial fills in a single transaction.
                identity = (str(fill['tid']), int(fill['oid']))
                if identity in seen:
                    continue
                size = Decimal(str(fill['sz']))
                if not size.is_finite() or size < 0 or fill['side'] not in ('B', 'A'):
                    raise ValueError('Invalid fill size/direction')
                seen.add(identity)
                quantity += size if fill['side'] == 'B' else -size
            except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
                raise RuntimeError('Incomplete session fill attribution') from exc
        if len(rows) < 2000:
            if quantity < 0:
                raise RuntimeError('Session inventory attribution is negative')
            return float(quantity)
        latest = max(times)
        # Include the boundary millisecond on the next page, deduplicate by tid.
        if latest <= cursor:
            raise RuntimeError('Session fills pagination stalled; close unconfirmed')
        cursor = latest
    raise RuntimeError('Session fill history exceeds verification limit')
