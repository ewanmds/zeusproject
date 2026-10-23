"""Download the official HIP-4 Outcome icons published by Hyperliquid.

Hyperliquid's app resolves an outcome icon with this precedence:

1. the underlying perp coin (``HYPE.svg``, ``xyz:SILVER.svg`` …),
2. the parent question (``<question_id>_question.svg``),
3. the outcome itself (``<name>_outcome.svg``),
4. a category icon (``soccer_outcome.svg``, ``basketball_outcome.svg`` …),
5. ``missing.svg``.

All candidates are served by ``https://app.hyperliquid.xyz/coins/<symbol>.svg``.
When a symbol has no icon the app returns its SPA HTML page with a ``200``
status, so every response is validated on both ``Content-Type`` and the SVG
root before it is written to ``frontend/assets/logos/hip4/``.

Usage::

    python -m zeus.hip4_logos            # download missing/outdated icons
    python -m zeus.hip4_logos --force    # re-download everything
    python -m zeus.hip4_logos --check    # local manifest/file verification
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
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple
from urllib.parse import quote

import requests

from zeus import outcomes

BASE_URL = "https://app.hyperliquid.xyz/coins"
LOGO_ROOT = outcomes.LOGO_ROOT
MANIFEST = outcomes.LOGO_MANIFEST
MAX_BYTES = 512 * 1024
TIMEOUT = 20
RETRIES = 3
WORKERS = 8
# Question/outcome specific icons usually do not exist.  Remember misses so a
# normal run only retries them after a month (``--force`` bypasses the TTL).
MISSING_TTL_SEC = 30 * 24 * 3600

_UNSAFE_SVG = re.compile(
    r"(<script\b|javascript:|<foreignObject\b|\son[a-zA-Z]+\s*=|"
    r"href\s*=\s*[\"']\s*https?:)",
    re.IGNORECASE,
)


def icon_url(symbol: str) -> str:
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
    """Fetch one official icon.  Returns a result dict, never raises."""
    url = icon_url(symbol)
    last_error = ""
    for attempt in range(1, RETRIES + 1):
        try:
            response = requests.get(
                url,
                timeout=TIMEOUT,
                headers={"User-Agent": "Zeus-HIP4-logo-fetcher/1.0"},
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

        if response.status_code == 200 and content_type.startswith("text/html"):
            return {"ok": False, "url": url, "reason": "no official icon (HTML fallback)"}

        last_error = f"HTTP {response.status_code} ({content_type or 'unknown'})"
        if response.status_code in (429, 500, 502, 503, 504) and attempt < RETRIES:
            time.sleep(1.5 * attempt)
            continue
        break
    return {"ok": False, "url": url, "reason": last_error or "download failed"}


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _write_atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(payload)
    temporary.replace(path)


def _load_manifest() -> Dict[str, Any]:
    try:
        data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("icons", {})
    data.setdefault("missing", {})
    data.setdefault("outcomes", {})
    return data


def _candidate_symbols(rows: Iterable[Dict[str, Any]]) -> Dict[str, str]:
    symbols: Dict[str, str] = {}
    for row in rows:
        for kind, symbol in outcomes.icon_candidates(row):
            symbols.setdefault(symbol, kind)
    symbols.setdefault("missing", "missing")
    return symbols


def _assign_paths(symbols: Iterable[str]) -> Dict[str, str]:
    """Map symbols to collision-free SVG filenames."""
    used: Dict[str, str] = {}
    paths: Dict[str, str] = {}
    for symbol in sorted(symbols):
        filename = outcomes.safe_logo_filename(symbol)
        key = filename.lower()
        if key in used and used[key] != symbol:
            stem = Path(filename).stem
            filename = f"{stem}-{hashlib.sha1(symbol.encode('utf-8')).hexdigest()[:8]}.svg"
            key = filename.lower()
        used[key] = symbol
        paths[symbol] = filename
    return paths


def _entry_is_current(entry: Dict[str, Any], path: Path, url: str) -> bool:
    if str(entry.get("source") or "") != url:
        return False
    try:
        payload = path.read_bytes()
    except OSError:
        return False
    return _sha256(payload) == entry.get("sha256")


def _missing_is_fresh(entry: Dict[str, Any]) -> bool:
    try:
        checked = datetime.fromisoformat(str(entry.get("checked_at") or ""))
    except (TypeError, ValueError):
        return False
    if checked.tzinfo is None:
        checked = checked.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - checked).total_seconds() < MISSING_TTL_SEC


def download(force: bool = False, prune: bool = False) -> int:
    rows = outcomes.catalog_snapshot(force=True)
    manifest = _load_manifest()
    previous_icons = manifest.get("icons", {})
    previous_missing = manifest.get("missing", {})
    candidates = _candidate_symbols(rows)
    paths = _assign_paths(candidates.keys())

    jobs: List[Tuple[str, str, str, Path]] = []
    skipped_missing = 0
    for symbol, kind in sorted(candidates.items()):
        path = LOGO_ROOT / paths[symbol]
        url = icon_url(symbol)
        entry = previous_icons.get(symbol)
        if not force and isinstance(entry, dict) and _entry_is_current(entry, path, url):
            continue
        miss = previous_missing.get(symbol)
        if not force and isinstance(miss, dict) and _missing_is_fresh(miss):
            skipped_missing += 1
            continue
        jobs.append((symbol, kind, url, path))

    print(
        f"Hyperliquid HIP-4 icons: {len(rows)} live outcomes, "
        f"{len(candidates)} candidate icons, {len(jobs)} to fetch"
        + (f" ({skipped_missing} known missing skipped)" if skipped_missing else "")
    )
    fetched: Dict[str, Dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(fetch_icon, symbol): (symbol, kind, url, path)
                   for symbol, kind, url, path in jobs}
        for future in as_completed(futures):
            symbol, kind, url, path = futures[future]
            result = future.result()
            if result.get("ok"):
                payload = result["bytes"]
                try:
                    resolved_root = LOGO_ROOT.resolve()
                    if not str(path.resolve()).startswith(str(resolved_root)):
                        raise OSError("path escapes logo root")
                    _write_atomic(path, payload)
                except OSError as exc:
                    fetched[symbol] = {
                        "ok": False, "url": url, "kind": kind,
                        "reason": f"write failed: {exc}",
                    }
                    continue
                fetched[symbol] = {
                    "ok": True,
                    "kind": kind,
                    "url": url,
                    "path": paths[symbol],
                    "sha256": _sha256(payload),
                    "bytes": len(payload),
                    "content_type": result["content_type"],
                }
                print(f"  OK   {symbol:<42} {len(payload):>6} bytes")
            else:
                fetched[symbol] = {
                    "ok": False,
                    "kind": kind,
                    "url": url,
                    "reason": result.get("reason", "unknown"),
                }
                print(f"  MISS {symbol:<42} {fetched[symbol]['reason']}")

    now = datetime.now(timezone.utc).isoformat()
    icons: Dict[str, Dict[str, Any]] = dict(previous_icons)
    missing: Dict[str, Dict[str, Any]] = dict(previous_missing)
    for symbol, result in fetched.items():
        if result.get("ok"):
            icons[symbol] = {
                "path": result["path"],
                "sha256": result["sha256"],
                "bytes": result["bytes"],
                "content_type": result["content_type"],
                "kind": result["kind"],
                "source": result["url"],
                "fetched_at": now,
            }
            missing.pop(symbol, None)
        else:
            icons.pop(symbol, None)
            missing[symbol] = {
                "kind": result["kind"],
                "source": result["url"],
                "reason": result["reason"],
                "checked_at": now,
            }

    current_symbols = set(candidates)
    if prune:
        for symbol in list(icons):
            if symbol in current_symbols:
                continue
            entry = icons.pop(symbol)
            try:
                (LOGO_ROOT / str(entry.get("path") or "")).unlink(missing_ok=True)
            except OSError:
                pass
        missing = {symbol: value for symbol, value in missing.items() if symbol in current_symbols}

    # Resolve every live outcome against the icons that are actually on disk.
    available = set(icons)
    outcome_icons: Dict[str, Dict[str, Any]] = {}
    unresolved: List[Dict[str, Any]] = []
    for row in rows:
        choice = next(
            ((kind, symbol) for kind, symbol in outcomes.icon_candidates(row)
             if symbol in available),
            None,
        )
        if choice is None:
            unresolved.append({
                "id": row["id"],
                "key": row["key"],
                "name": row["name"],
                "category": row["category"],
                "subcategory": row.get("subcategory"),
            })
            continue
        kind, symbol = choice
        outcome_icons[str(row["id"])] = {
            "key": row["key"],
            "name": row["name"],
            "icon_kind": kind,
            "icon_symbol": symbol,
            "file": icons[symbol]["path"],
        }

    manifest = {
        "generated_at": now,
        "source": "https://app.hyperliquid.xyz/coins/<symbol>.svg",
        "venue": outcomes.VENUE,
        "counts": {
            "live": len(rows),
            "covered": len(outcome_icons),
            "missing": len(unresolved),
            "icons": len(icons),
        },
        "icons": dict(sorted(icons.items())),
        "outcomes": dict(sorted(outcome_icons.items(), key=lambda pair: int(pair[0]))),
        "missing": dict(sorted(missing.items())),
        "unresolved": unresolved,
    }
    LOGO_ROOT.mkdir(parents=True, exist_ok=True)
    temporary = MANIFEST.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(manifest, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(MANIFEST)

    counts = manifest["counts"]
    print(
        f"\nManifest: {counts['covered']}/{counts['live']} outcomes covered, "
        f"{counts['icons']} official icons, {counts['missing']} unresolved"
    )
    print(f"Written to {MANIFEST}")
    return 1 if unresolved else 0


def check() -> int:
    rows = outcomes.catalog_snapshot(force=True)
    manifest = _load_manifest()
    icons = manifest.get("icons", {})
    outcome_icons = manifest.get("outcomes", {})
    errors = 0
    valid_icons: Set[str] = set()

    for symbol, entry in sorted(icons.items()):
        if not isinstance(entry, dict):
            print(f"MISSING  {symbol}: malformed manifest entry")
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
            continue
        valid_icons.add(symbol)

    # Outcomes are dynamic (rolling 15-minute price markets rotate IDs), so
    # resolve each live row against the icon precedence instead of requiring a
    # frozen per-outcome snapshot in the manifest.
    for row in rows:
        choice = next(
            ((kind, symbol) for kind, symbol in outcomes.icon_candidates(row)
             if symbol in valid_icons),
            None,
        )
        if choice is None:
            print(f"NO ICON  #{row['id']} {row['name']}: no resolved icon")
            errors += 1

    live_ids = {str(row["id"]) for row in rows}
    historical = [outcome_id for outcome_id in outcome_icons if outcome_id not in live_ids]
    if historical:
        print(f"INFO     {len(historical)} historical outcome(s) rotated out")
    print(f"\nChecked {len(rows)} live outcomes, {len(icons)} icons, {errors} error(s)")
    return 1 if errors else 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="re-download every icon")
    parser.add_argument("--prune", action="store_true", help="delete icons for symbols no longer used")
    parser.add_argument("--check", action="store_true", help="verify manifest and local files only")
    args = parser.parse_args(argv)
    if args.check:
        return check()
    return download(force=args.force, prune=args.prune)


if __name__ == "__main__":
    sys.exit(main())
