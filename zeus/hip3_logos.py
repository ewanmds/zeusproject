"""Download the official HIP-3 market icons published by Hyperliquid.

Icons are served by the Hyperliquid app at::

    https://app.hyperliquid.xyz/coins/<symbol>.svg

The endpoint answers ``200 text/html`` (SPA fallback) when a market has no
icon, so every response is validated on both Content-Type and the SVG root.
Downloaded files live in ``frontend/assets/logos/hip3/<dex>/<TICKER>.svg`` and
are described by ``manifest.json`` (source URL + SHA-256).

Usage::

    python -m zeus.hip3_logos            # download missing/outdated icons
    python -m zeus.hip3_logos --force    # re-download everything
    python -m zeus.hip3_logos --check    # local manifest/file verification
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Tuple
from urllib.parse import quote

import requests

from zeus import PROJECT_ROOT
from zeus import markets

BASE_URL = "https://app.hyperliquid.xyz/coins"
LOGO_ROOT = PROJECT_ROOT / "frontend" / "assets" / "logos" / "hip3"
MANIFEST = LOGO_ROOT / "manifest.json"
MAX_BYTES = 512 * 1024
TIMEOUT = 20
RETRIES = 3
WORKERS = 4

_UNSAFE_SVG = re.compile(
    r"(<script\b|javascript:|<foreignObject\b|\son[a-zA-Z]+\s*=|"
    r"href\s*=\s*[\"']\s*https?:)",
    re.IGNORECASE,
)
_SAFE_TICKER = re.compile(r"^[A-Za-z0-9._-]+$")


def logo_url(symbol: str) -> str:
    return f"{BASE_URL}/{quote(symbol, safe=':')}.svg"


def _validate_svg(payload: bytes) -> Tuple[bool, str]:
    if not payload:
        return False, "empty response"
    if len(payload) > MAX_BYTES:
        return False, f"SVG too large ({len(payload)} bytes)"
    text = payload.decode("utf-8", "replace")
    if "<svg" not in text.lower():
        return False, "missing <svg> root"
    if _UNSAFE_SVG.search(text):
        return False, "unsafe SVG content"
    return True, ""


def fetch_icon(symbol: str) -> Dict[str, Any]:
    """Fetch one icon.  Returns a result dict, never raises."""
    url = logo_url(symbol)
    last_error = ""
    for attempt in range(1, RETRIES + 1):
        try:
            response = requests.get(
                url,
                timeout=TIMEOUT,
                headers={"User-Agent": "Zeus-HIP3-logo-fetcher/1.0"},
            )
        except requests.RequestException as exc:
            last_error = f"network error: {type(exc).__name__}"
            if attempt < RETRIES:
                time.sleep(1.5 * attempt)
                continue
            return {"ok": False, "url": url, "reason": last_error}

        content_type = (response.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if response.status_code == 200 and content_type == "image/svg+xml":
            valid, reason = _validate_svg(response.content)
            if valid:
                return {
                    "ok": True,
                    "url": url,
                    "bytes": response.content,
                    "content_type": content_type,
                }
            return {"ok": False, "url": url, "reason": reason}

        # Hyperliquid's SPA fallback for a market without an official icon.
        if response.status_code == 200 and content_type.startswith("text/html"):
            return {"ok": False, "url": url, "reason": "no official icon (HTML fallback)"}

        last_error = f"HTTP {response.status_code} ({content_type or 'unknown'})"
        if response.status_code in (429, 500, 502, 503, 504) and attempt < RETRIES:
            time.sleep(1.5 * attempt)
            continue
        break
    return {"ok": False, "url": url, "reason": last_error or "download failed"}


def _write_atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(payload)
    temporary.replace(path)


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _load_manifest() -> Dict[str, Any]:
    try:
        data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("logos", {})
    data.setdefault("missing", {})
    return data


def _market_list(force: bool = True) -> list[Dict[str, Any]]:
    markets.discover_dexes(force=force)
    live, _ = markets.catalog_snapshot(force=force)
    return live


def download(force: bool = False, prune: bool = False) -> int:
    live = _market_list(force=True)
    manifest = _load_manifest()
    previous = manifest.get("logos", {})
    logo_root_resolved = LOGO_ROOT.resolve()

    jobs = []
    for market in live:
        symbol = str(market.get("name") or "")
        dex = str(market.get("dex") or "").lower()
        ticker = str(market.get("ticker") or "")
        if not symbol or not dex or not _SAFE_TICKER.match(ticker):
            continue
        path = LOGO_ROOT / dex / f"{ticker}.svg"
        if not force and path.is_file() and symbol in previous:
            continue
        jobs.append((symbol, dex, ticker, path))

    print(f"Hyperliquid HIP-3 icons: {len(live)} live markets, {len(jobs)} to fetch")
    results: Dict[str, Dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(fetch_icon, symbol): (symbol, dex, ticker, path)
                   for symbol, dex, ticker, path in jobs}
        for future in as_completed(futures):
            symbol, dex, ticker, path = futures[future]
            result = future.result()
            if result.get("ok"):
                payload = result["bytes"]
                try:
                    if not str(path.resolve()).startswith(str(logo_root_resolved)):
                        raise OSError("path escapes logo root")
                    _write_atomic(path, payload)
                except OSError as exc:
                    results[symbol] = {
                        "ok": False, "url": result["url"],
                        "reason": f"write failed: {exc}",
                    }
                    continue
                results[symbol] = {
                    "ok": True,
                    "path": path.relative_to(LOGO_ROOT).as_posix(),
                    "sha256": _sha256(payload),
                    "bytes": len(payload),
                    "content_type": result["content_type"],
                    "url": result["url"],
                }
                print(f"  OK   {symbol:<18} {len(payload):>6} bytes")
            else:
                results[symbol] = {
                    "ok": False, "url": result["url"],
                    "reason": result.get("reason", "unknown"),
                }
                print(f"  MISS {symbol:<18} {results[symbol]['reason']}")

    now = datetime.now(timezone.utc).isoformat()
    logos = dict(previous)
    missing = dict(manifest.get("missing", {}))
    for symbol, result in results.items():
        if result.get("ok"):
            logos[symbol] = {
                "path": result["path"],
                "sha256": result["sha256"],
                "bytes": result["bytes"],
                "content_type": result["content_type"],
                "source": result["url"],
                "fetched_at": now,
            }
            missing.pop(symbol, None)
        else:
            missing[symbol] = {
                "source": result["url"],
                "reason": result["reason"],
                "checked_at": now,
            }

    live_symbols = {str(market.get("name")) for market in live}
    if prune:
        stale = [symbol for symbol in logos if symbol not in live_symbols]
        for symbol in stale:
            entry = logos.pop(symbol)
            try:
                (LOGO_ROOT / str(entry["path"])).unlink(missing_ok=True)
            except OSError:
                pass
        missing = {symbol: value for symbol, value in missing.items() if symbol in live_symbols}

    manifest = {
        "generated_at": now,
        "source": "https://app.hyperliquid.xyz/coins/<symbol>.svg",
        "counts": {
            "live": len(live),
            "downloaded": sum(1 for symbol in live_symbols if symbol in logos),
            "missing": sum(1 for symbol in live_symbols if symbol not in logos),
        },
        "logos": dict(sorted(logos.items())),
        "missing": dict(sorted(missing.items())),
    }
    LOGO_ROOT.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(
        json.dumps(manifest, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    counts = manifest["counts"]
    print(
        f"\nManifest: {counts['downloaded']}/{counts['live']} icons, "
        f"{counts['missing']} markets without a Hyperliquid icon"
    )
    print(f"Written to {MANIFEST}")
    return 0


def check() -> int:
    live = _market_list(force=True)
    manifest = _load_manifest()
    logos = manifest.get("logos", {})
    known_missing = manifest.get("missing", {})
    errors = 0
    for market in live:
        symbol = str(market.get("name") or "")
        entry = logos.get(symbol)
        if not entry:
            if symbol in known_missing:
                print(f"NO ICON  {symbol}: {known_missing[symbol].get('reason', 'no official icon')}")
                continue
            print(f"MISSING  {symbol}: no downloaded icon")
            errors += 1
            continue
        path = LOGO_ROOT / str(entry.get("path") or "")
        try:
            payload = path.read_bytes()
        except OSError:
            print(f"MISSING  {symbol}: {path} not found")
            errors += 1
            continue
        if _sha256(payload) != entry.get("sha256"):
            print(f"STALE    {symbol}: hash mismatch")
            errors += 1
    live_symbols = {str(market.get("name")) for market in live}
    for symbol in logos:
        if symbol not in live_symbols:
            print(f"STALE    {symbol}: no longer live")
            errors += 1
    print(f"\nChecked {len(live)} live markets, {errors} error(s)")
    return 1 if errors else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="re-download every icon")
    parser.add_argument("--prune", action="store_true", help="delete icons for markets no longer live")
    parser.add_argument("--check", action="store_true", help="verify manifest and local files only")
    args = parser.parse_args(argv)
    if args.check:
        return check()
    return download(force=args.force, prune=args.prune)


if __name__ == "__main__":
    sys.exit(main())
