"""Strict final fill aggregation scoped to exchange order IDs of one session."""
from zeus.safety import finite


def summarize_fills(rows, symbol, owned, expected=None, *, allow_large=False):
    # A single userFillsByTime page is capped at 2000 rows. The caller that
    # pages the history may pass allow_large=True; a raw single-page response
    # of >=2000 rows is still treated as truncated.
    if not isinstance(rows,list) or (not allow_large and len(rows)>=2000):
        raise ValueError("Final fills unavailable or truncated")
    fills={}
    for row in rows:
        if not isinstance(row,dict):
            raise ValueError("Invalid final fill row")
        if row.get("coin") not in (symbol,symbol.split(":")[-1]) or int(row.get("oid",0)) not in owned:
            continue
        if row.get("tid") is None or row.get("feeToken","USDC")!="USDC":
            raise ValueError("Final fill identity or fee currency unconfirmed")
        price,size,fee,pnl=(finite(row[k]) for k in ("px","sz","fee","closedPnl"))
        if price<=0 or size<=0:
            raise ValueError("Invalid final fill size/price")
        key=str(row["tid"])
        item=(int(row["oid"]),price,size,fee,pnl,row.get("crossed"))
        if key in fills and fills[key]!=item:
            raise ValueError("Conflicting duplicate final fill")
        fills[key]=item
    for oid,size in (expected or {}).items():
        observed=sum(item[2] for item in fills.values() if item[0]==oid)
        if observed+1e-9<size:
            raise ValueError("Closing fills not yet confirmed in history")
    return {"volume":sum(px*sz for _,px,sz,_,_,_ in fills.values()),
            "fees":sum(fee for _,_,_,fee,_,_ in fills.values()),
            "rpnl":sum(pnl for _,_,_,_,pnl,_ in fills.values()),
            "fills":len(fills),
            "maker":sum(role is False for *_,role in fills.values()),
            "taker":sum(role is True for *_,role in fills.values()),
            "unknown":sum(role is not True and role is not False for *_,role in fills.values()),
            "tids":tuple(fills)}
