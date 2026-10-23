"""Credential-free HIP-3 catalog shared by BBO/MID, Freqtrade and the dashboard.

The static ``TOKENS`` presets below are only a safety net / source of market
specific defaults (session windows, half spread).  The live catalog is
discovered from Hyperliquid's ``perpDexs`` + ``metaAndAssetCtxs`` endpoints and
merged on top of the presets.  Every consumer (server, child bots) can resolve
a bare ticker (``SP500``) or a dex-qualified market (``XYZ:COPPER``,
``PARA:10Y``) without relying on a shared in-memory cache.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

from zeus.network import post_info

logger = logging.getLogger(__name__)

HL_API = "https://api.hyperliquid.xyz"
CATALOG_TTL_SEC = 60.0
DEX_TTL_SEC = 300.0

# Fallback when the discovery endpoint is unreachable.  Order matters: a bare
# ticker is resolved on the first dex that lists it.
PERP_DEXS = ["xyz", "para", "mkts", "io"]

TOKENS: Dict[str, Dict[str, Any]] = {
    "SP500": {"symbol": "xyz:SP500", "half_spread_bps": 0.7,
              "session_utc": ((810, 1200),)},
    "XYZ100": {"symbol": "xyz:XYZ100"},
    "CL": {"symbol": "xyz:CL"},
    "BRENTOIL": {"symbol": "xyz:BRENTOIL"},
    "AAPL": {"symbol": "xyz:AAPL"},
    "JPY": {"symbol": "xyz:JPY"},
    "SPCX": {"symbol": "xyz:SPCX"},
    "GOLD": {"symbol": "xyz:GOLD"},
    "SILVER": {"symbol": "xyz:SILVER"},
    "NVDA": {"symbol": "xyz:NVDA"},
    "TSLA": {"symbol": "xyz:TSLA"},
    "US500": {"symbol": "mkts:US500", "session_utc": ((810, 1200),)},
    "SKHX": {"symbol": "xyz:SKHX", "half_spread_bps": 0.8},
    "SKHXUSD": {"symbol": "xyz:SKHX", "half_spread_bps": 0.8},
    # Entropy symbols use distinct UI keys: xyz:SNDK is a different market.
    "IO_SNDK": {"symbol": "io:SNDK"},
    "IO_ANTH": {"symbol": "io:ANTH"},
}

_lock = threading.RLock()
_catalog_cache: Dict[str, Any] = {"ts": 0.0, "markets": [], "mids": {}}
_dex_cache: Dict[str, Any] = {"ts": 0.0, "dexes": list(PERP_DEXS)}


def _finite(value: Any, default: float = 0.0) -> float:
    """Coerce an API number without letting NaN/inf reach the JSON encoder."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _clean_dex(dex: Any) -> str:
    return str(dex or "").strip().lower()


def _clean_ticker(ticker: Any) -> str:
    return str(ticker or "").strip().upper()


def _split_symbol(symbol: str) -> Tuple[str, str]:
    """Split ``dex:TICKER`` (case-insensitive) into ``(dex, TICKER)``."""
    raw = str(symbol or "").strip()
    if ":" not in raw:
        raise ValueError(f"Symbole HIP-3 invalide (dex:TICKER attendu) : {symbol!r}")
    dex, ticker = raw.split(":", 1)
    dex, ticker = _clean_dex(dex), _clean_ticker(ticker)
    if not dex or not ticker:
        raise ValueError(f"Symbole HIP-3 invalide (dex:TICKER attendu) : {symbol!r}")
    return dex, ticker


def discover_dexes(force: bool = False) -> List[str]:
    """Return every HIP-3 dex advertised by Hyperliquid (cached)."""
    now = time.monotonic()
    with _lock:
        if not force and now - _dex_cache["ts"] < DEX_TTL_SEC:
            return list(_dex_cache["dexes"])
    try:
        rows = post_info(HL_API, {"type": "perpDexs"})
        names: List[str] = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            name = _clean_dex(row.get("name"))
            if name and name not in names:
                names.append(name)
    except Exception as exc:  # noqa: BLE001 - discovery must never break the UI
        logger.warning("HIP-3 dex discovery failed: %s", exc)
        names = []
    if not names:
        names = list(PERP_DEXS)
    with _lock:
        _dex_cache.update(ts=now, dexes=names)
    return names


def _market_from_row(dex: str, row: Dict[str, Any], ctx: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    name = str(row.get("name") or "").strip()
    if not name:
        return None
    if ":" not in name:
        name = f"{dex}:{name}"
    row_dex, ticker = _split_symbol(name)
    if row.get("isDelisted"):
        return None
    mid = _finite(ctx.get("midPx"))
    if mid <= 0:
        mid = _finite(ctx.get("markPx"))
    return {
        "name": name,
        "dex": row_dex,
        "ticker": ticker,
        "sz_decimals": int(row.get("szDecimals") or 0),
        "max_leverage": int(row.get("maxLeverage") or 0),
        "only_isolated": bool(row.get("onlyIsolated")),
        "is_cross": not bool(row.get("onlyIsolated")),
        "mid": mid,
        "mark": _finite(ctx.get("markPx")),
        "day_volume": _finite(ctx.get("dayNtlVlm")),
        "open_interest": _finite(ctx.get("openInterest")),
    }


def _load_dex(dex: str) -> List[Dict[str, Any]]:
    """Fetch one dex universe.  Raises on transport/API errors."""
    raw = post_info(HL_API, {"type": "metaAndAssetCtxs", "dex": dex})
    if not isinstance(raw, list) or len(raw) < 2:
        raise ValueError(f"metaAndAssetCtxs invalide pour {dex}")
    universe = (raw[0] or {}).get("universe") or []
    contexts = raw[1] or []
    markets: List[Dict[str, Any]] = []
    for index, row in enumerate(universe):
        if not isinstance(row, dict):
            continue
        ctx = contexts[index] if index < len(contexts) and isinstance(contexts[index], dict) else {}
        market = _market_from_row(dex, row, ctx)
        if market is not None:
            markets.append(market)
    return markets


def catalog_snapshot(force: bool = False) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Return ``(live_markets, mids)`` for every HIP-3 dex, cached for 60 s."""
    now = time.monotonic()
    with _lock:
        if not force and now - _catalog_cache["ts"] < CATALOG_TTL_SEC:
            return list(_catalog_cache["markets"]), dict(_catalog_cache["mids"])

    markets: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for dex in discover_dexes():
        try:
            dex_markets = _load_dex(dex)
        except Exception as exc:  # noqa: BLE001 - one bad dex must not hide the rest
            logger.warning("HIP-3 meta unavailable for dex %s: %s", dex, exc)
            continue
        for market in dex_markets:
            key = market["name"].lower()
            if key in seen:
                continue
            seen.add(key)
            markets.append(market)

    markets.sort(key=lambda item: (item["dex"], item["ticker"]))
    mids = {market["name"]: market["mid"] for market in markets if market["mid"] > 0}
    with _lock:
        _catalog_cache.update(ts=now, markets=list(markets), mids=dict(mids))
    return markets, mids


def _fetch_hip3_meta() -> List[Dict[str, Any]]:
    """Compatibility helper used by ``zeus.server``."""
    markets, _ = catalog_snapshot()
    return markets


def _fetch_all_mids() -> Dict[str, Any]:
    """Compatibility helper used by ``zeus.server``."""
    _, mids = catalog_snapshot()
    return mids


def _dynamic_token_config(item: Dict[str, Any],
                          mids: Optional[Dict[str, Any]] = None) -> Optional[Tuple[str, Dict[str, Any]]]:
    """Build a ``TOKENS`` entry for one live market.

    Keys are dex-qualified (``XYZ:COPPER``, ``PARA:10Y``) so two markets that
    share a ticker (``xyz:SNDK`` / ``io:SNDK``) can never collide.
    """
    if not isinstance(item, dict):
        return None
    name = str(item.get("name") or "").strip()
    if not name:
        return None
    try:
        dex, ticker = _split_symbol(name)
    except ValueError:
        return None
    mid = _finite(item.get("mid"))
    if mid <= 0 and mids:
        mid = _finite(mids.get(name) or mids.get(f"{dex}:{ticker}"))
    key = f"{dex.upper()}:{ticker}"
    cfg = {
        "symbol": name,
        "dex": dex,
        "ticker": ticker,
        "name": name,
        "sz_decimals": int(item.get("sz_decimals") or 0),
        "max_leverage": int(item.get("max_leverage") or 0),
        "is_cross": bool(item.get("is_cross", True)),
        "api_mid": mid,
        "day_volume": _finite(item.get("day_volume")),
        "logo": ticker,
    }
    return key, cfg


def refresh_catalog() -> Dict[str, Dict[str, Any]]:
    """Merge presets + live markets and update ``TOKENS`` in place.

    Presets keep their legacy keys (``SP500``, ``IO_SNDK``) and win on a symbol
    collision, so existing bookmarks/running sessions keep working.  Dynamic
    markets are exposed with an uppercase dex-qualified key.
    """
    markets, mids = catalog_snapshot()
    with _lock:
        merged: Dict[str, Dict[str, Any]] = {
            str(key).upper(): dict(cfg)
            for key, cfg in TOKENS.items()
            if isinstance(cfg, dict) and cfg.get("symbol")
        }
        covered = {str(cfg["symbol"]).lower() for cfg in merged.values()}
        for item in markets:
            built = _dynamic_token_config(item, mids)
            if built is None:
                continue
            key, cfg = built
            symbol = str(cfg["symbol"]).lower()
            if symbol in covered:
                # Presets keep their defaults (session windows, half spread) but
                # receive the live mid/volume/leverage/logo metadata.
                for existing in merged.values():
                    if str(existing.get("symbol", "")).lower() != symbol:
                        continue
                    for field in ("dex", "ticker", "name", "sz_decimals",
                                  "max_leverage", "is_cross", "api_mid", "day_volume"):
                        existing[field] = cfg[field]
                continue
            covered.add(symbol)
            merged[key] = cfg
        TOKENS.clear()
        TOKENS.update(merged)
        return dict(merged)


def token_config(token: str) -> Dict[str, Any]:
    """Return the config for a key, a symbol or a dex-qualified market."""
    raw = str(token or "").strip()
    if not raw:
        return {}
    key = raw.upper()
    with _lock:
        cfg = TOKENS.get(key)
    if isinstance(cfg, dict) and cfg.get("symbol"):
        return dict(cfg)
    # Match on the exact Hyperliquid symbol (case-insensitive).
    with _lock:
        for candidate in TOKENS.values():
            if isinstance(candidate, dict) and str(candidate.get("symbol", "")).lower() == raw.lower():
                return dict(candidate)
    try:
        refresh_catalog()
    except Exception as exc:  # noqa: BLE001 - fall back to static presets
        logger.warning("HIP-3 catalog refresh failed: %s", exc)
    with _lock:
        cfg = TOKENS.get(key)
        if isinstance(cfg, dict) and cfg.get("symbol"):
            return dict(cfg)
        for candidate in TOKENS.values():
            if isinstance(candidate, dict) and str(candidate.get("symbol", "")).lower() == raw.lower():
                return dict(candidate)
    return {}


def _find_live_market(dex: str, ticker: str,
                      markets: Iterable[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    wanted = f"{_clean_dex(dex)}:{_clean_ticker(ticker)}".lower()
    for market in markets:
        if str(market.get("name", "")).lower() == wanted:
            return market
    return None


def resolve_symbol(token: str, *, force: bool = False) -> str:
    """Resolve a preset key, bare ticker or ``DEX:TICKER`` to the exact API name.

    Raises ``ValueError`` when the market is unknown or delisted.  Unknown
    dex-qualified markets trigger one forced refresh so a market listed after
    the last cache fill is still accepted.
    """
    raw = str(token or "").strip()
    if not raw:
        raise ValueError("Token vide")

    cfg = token_config(raw)
    if cfg.get("symbol"):
        symbol = str(cfg["symbol"])
        dex, ticker = _split_symbol(symbol)
        markets, _ = catalog_snapshot()
        live = _find_live_market(dex, ticker, markets)
        if live is not None:
            return str(live["name"])
        # The preset exists but is not in the live universe: keep the historical
        # behaviour for markets temporarily hidden from meta (preflight will
        # decide), instead of breaking a previously working session.
        return symbol

    markets, _ = catalog_snapshot(force=force)
    if ":" in raw:
        dex, ticker = _split_symbol(raw)
        live = _find_live_market(dex, ticker, markets)
        if live is None and not force:
            markets, _ = catalog_snapshot(force=True)
            live = _find_live_market(dex, ticker, markets)
        if live is None:
            raise ValueError(f"Marché inconnu ou delisté : {dex}:{ticker}")
        return str(live["name"])

    ticker = _clean_ticker(raw)
    for market in markets:
        if market.get("ticker") == ticker:
            return str(market["name"])
    raise ValueError(f"Marché inconnu : {ticker}")
