"""Credential-free HIP-4 Outcome catalog for the dashboard and the bot.

Hyperliquid publishes every outcome market through the public ``outcomeMeta``
endpoint, together with the templates needed to render human-readable names
(``outcomeTemplates``).  Prices live in ``allMids`` under the synthetic coin
``#<outcome_id * 10 + side>``.

The catalog is intentionally venue-scoped: the Outcome page only exposes the
``out`` deployer (the official Hyperliquid outcome venue).  Everything is
cached for 60 seconds and can be used by the API, the bot and the icon
downloader without sharing an in-memory instance.
"""

from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple
from urllib.parse import quote

from zeus import PROJECT_ROOT
from zeus.network import post_info

logger = logging.getLogger(__name__)

HL_API = "https://api.hyperliquid.xyz"
VENUE = "out"
CATALOG_TTL_SEC = 60.0

LOGO_ROOT = PROJECT_ROOT / "frontend" / "assets" / "logos" / "hip4"
LOGO_MANIFEST = LOGO_ROOT / "manifest.json"
LOGO_URL_PREFIX = "/frontend/assets/logos/hip4"

CATEGORY_ORDER = ["sports", "price", "culture", "economics", "technology", "tradfi", "crypto"]
CATEGORY_LABELS = {
    "sports": "Sports",
    "price": "Prix",
    "culture": "Culture",
    "economics": "Économie",
    "technology": "Technologie",
    "tradfi": "TradFi",
    "crypto": "Crypto",
    "other": "Autres",
}
SPORT_ICONS = {
    "association football": "soccer_outcome",
    "football/soccer": "soccer_outcome",
    "soccer": "soccer_outcome",
    "football": "football_outcome",
    "basketball": "basketball_outcome",
    "baseball": "baseball_outcome",
    "esports": "esports_outcome",
}
CATEGORY_ICONS = {
    "culture": "culture_outcome",
    "economics": "economics_outcome",
    "technology": "technology_outcome",
}

_lock = threading.RLock()
_cache: Dict[str, Any] = {"ts": 0.0, "rows": [], "logo_index": {}, "by_key": {}, "by_id": {}}


# ---------------------------------------------------------------------------
# Small parsing helpers
# ---------------------------------------------------------------------------
def _finite(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _fields_of(description: Any) -> Dict[str, str]:
    """Parse Hyperliquid's ``key:value|key:value`` description format."""
    fields: Dict[str, str] = {}
    for part in str(description or "").split("|"):
        if ":" not in part:
            continue
        key, value = part.split(":", 1)
        key = key.strip()
        if key:
            fields[key] = value.strip()
    return fields


def _template_id(raw_name: Any) -> Optional[str]:
    raw = str(raw_name or "").strip()
    if raw.startswith("template:"):
        return raw.split(":", 1)[1].strip() or None
    return None


def _format_value(value: str, hint: str) -> str:
    if hint == "dateTime" and re.fullmatch(r"\d{8}-\d{4}", value or ""):
        try:
            moment = datetime.strptime(value, "%Y%m%d-%H%M")
            return moment.strftime("%b %d, %Y %H:%M UTC")
        except ValueError:
            return value
    if hint == "uInt":
        try:
            return f"{int(value):,}"
        except (TypeError, ValueError):
            return value
    if hint == "uDecimal":
        whole, dot, fraction = str(value).partition(".")
        try:
            whole = f"{int(whole):,}"
        except (TypeError, ValueError):
            return value
        return f"{whole}.{fraction}" if dot else whole
    return value


def _render(template: Optional[Dict[str, Any]], fields: Dict[str, str]) -> Optional[str]:
    """Render a name template, or ``None`` when one of its fields is absent."""
    if not isinstance(template, dict):
        return None
    name = template.get("name")
    if not isinstance(name, str) or not name:
        return None
    keywords = template.get("keywords") or []
    values: Dict[str, str] = {}
    for entry in keywords:
        if not isinstance(entry, (list, tuple)) or len(entry) < 2:
            continue
        key, hint = str(entry[0]), str(entry[1])
        if key not in fields:
            return None
        values[key] = _format_value(fields[key], hint)
    if "{" in name and not values:
        return None
    for key, value in values.items():
        name = name.replace("{" + key + "}", value)
    return name if "{" not in name else None


def _metadata(template: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """Extract the ``metadata=`` payload from a template description."""
    if not isinstance(template, dict):
        return {}
    description = str(template.get("description") or "")
    marker = description.rfind("metadata=")
    if marker < 0:
        return {}
    payload = description[marker + len("metadata="):]
    result: Dict[str, str] = {}
    for part in payload.split("|"):
        if ":" not in part:
            continue
        key, value = part.split(":", 1)
        result[key.strip()] = value.strip()
    return result


def _category_from(template: Optional[Dict[str, Any]], fields: Dict[str, str]) -> Tuple[str, str]:
    meta = _metadata(template)
    category = meta.get("category", "").strip().lower()
    subcategory = meta.get("subCategory", "").strip()
    if subcategory and "{" in subcategory:
        rendered = _render(
            {"name": subcategory, "keywords": template.get("keywords", [])},
            fields,
        )
        if rendered:
            subcategory = rendered
    return category, subcategory


def category_icon(category: str, subcategory: str) -> Optional[str]:
    """Mirror Hyperliquid's official ``/coins/<icon>.svg`` fallback scheme."""
    category = str(category or "").strip().lower()
    subcategory = str(subcategory or "").strip().lower()
    if category in CATEGORY_ICONS:
        return CATEGORY_ICONS[category]
    if category == "sports":
        return SPORT_ICONS.get(subcategory)
    return None


def safe_logo_filename(symbol: str) -> str:
    """Deterministic, traversal-free filename for one official icon symbol."""
    raw = str(symbol or "").strip()
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", raw)
    cleaned = cleaned.strip("._-")[:180]
    return f"{cleaned or 'icon'}.svg"


def icon_candidates(row: Dict[str, Any]) -> List[Tuple[str, str]]:
    """Return ``(kind, symbol)`` in Hyperliquid's icon precedence order."""
    candidates: List[Tuple[str, str]] = []
    underlying = str(row.get("underlying") or "").strip()
    if underlying:
        candidates.append(("underlying", underlying))
    question_id = row.get("question_id")
    if question_id is not None:
        candidates.append(("question", f"{int(question_id)}_question"))
    name = str(row.get("name") or "").strip()
    if name and not row.get("is_fallback"):
        candidates.append(("outcome", f"{name}_outcome"))
    category_icon_symbol = category_icon(row.get("category", ""), row.get("subcategory", ""))
    if category_icon_symbol:
        candidates.append(("category", category_icon_symbol))
    candidates.append(("missing", "missing"))
    # De-duplicate while preserving order.
    seen: Set[str] = set()
    unique: List[Tuple[str, str]] = []
    for kind, symbol in candidates:
        if symbol in seen:
            continue
        seen.add(symbol)
        unique.append((kind, symbol))
    return unique


def _initials(name: str) -> str:
    words = re.findall(r"[A-Za-z0-9]+", str(name or ""))
    if len(words) >= 2:
        return (words[0][0] + words[1][0]).upper()
    cleaned = "".join(ch for ch in str(name or "").upper() if ch.isalnum())
    return (cleaned[:2] or "?").upper()


def _fallback_label(template_id: Optional[str]) -> str:
    if template_id == "sportsTournamentWinner":
        return "No winner"
    return "Other"


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------
def _fetch_payloads() -> Tuple[Dict[str, Any], Dict[str, Dict[str, Any]], Dict[str, Any]]:
    meta = post_info(HL_API, {"type": "outcomeMeta"})
    templates: Dict[str, Dict[str, Any]] = {}
    try:
        raw_templates = post_info(HL_API, {"type": "outcomeTemplates"})
        if isinstance(raw_templates, list):
            templates = {
                str(item.get("id")): item
                for item in raw_templates
                if isinstance(item, dict) and item.get("id")
            }
    except Exception as exc:  # noqa: BLE001 - names still work without templates
        logger.warning("HIP-4 outcomeTemplates unavailable: %s", exc)
    mids: Dict[str, Any] = {}
    try:
        raw_mids = post_info(HL_API, {"type": "allMids"})
        if isinstance(raw_mids, dict):
            mids = raw_mids
    except Exception as exc:  # noqa: BLE001 - prices are optional for the catalog
        logger.warning("HIP-4 allMids unavailable: %s", exc)
    if not isinstance(meta, dict):
        raise ValueError("outcomeMeta returned an unexpected payload")
    return meta, templates, mids


def _build_rows(
    meta: Dict[str, Any],
    templates: Dict[str, Dict[str, Any]],
    mids: Dict[str, Any],
) -> List[Dict[str, Any]]:
    raw_outcomes = meta.get("outcomes") or []
    raw_questions = meta.get("questions") or []

    question_by_outcome: Dict[int, Dict[str, Any]] = {}
    fallback_ids: Set[int] = set()
    for question in raw_questions:
        if not isinstance(question, dict):
            continue
        linked: List[Any] = [question.get("fallbackOutcome")]
        linked.extend(question.get("namedOutcomes") or [])
        for value in linked:
            try:
                outcome_id = int(value)
            except (TypeError, ValueError):
                continue
            question_by_outcome.setdefault(outcome_id, question)
        try:
            fallback_ids.add(int(question.get("fallbackOutcome")))
        except (TypeError, ValueError):
            pass

    rows: List[Dict[str, Any]] = []
    for raw in raw_outcomes:
        if not isinstance(raw, dict):
            continue
        if str(raw.get("venue") or "").strip().lower() != VENUE:
            continue
        try:
            outcome_id = int(raw.get("outcome"))
        except (TypeError, ValueError):
            continue
        if outcome_id <= 0:
            continue

        raw_name = str(raw.get("name") or "").strip()
        description = str(raw.get("description") or "").strip()
        fields = _fields_of(description)
        template_id = _template_id(raw_name)
        template = templates.get(template_id) if template_id else None
        question = question_by_outcome.get(outcome_id)
        question_template_id = _template_id(question.get("name")) if question else None
        question_template = templates.get(question_template_id) if question_template_id else None
        question_fields = _fields_of(question.get("description")) if question else {}
        is_fallback = outcome_id in fallback_ids or description.lower() == "other"

        if is_fallback:
            name = _fallback_label(question_template_id)
        else:
            name = _render(template, fields) or raw_name or f"Outcome {outcome_id}"

        question_title = None
        if question is not None:
            question_title = (
                _render(question_template, question_fields)
                or str(question.get("name") or "").strip()
                or None
            )

        category, subcategory = _category_from(template, fields)
        if not category:
            category, question_subcategory = _category_from(question_template, question_fields)
            subcategory = subcategory or question_subcategory
        category = category or "other"

        underlying = ""
        if isinstance(template, dict):
            for entry in template.get("keywords") or []:
                if not isinstance(entry, (list, tuple)) or len(entry) < 2:
                    continue
                if str(entry[1]) == "hlPerp":
                    underlying = fields.get(str(entry[0]), "")
                    break
        if not underlying and question is not None and isinstance(question_template, dict):
            for entry in question_template.get("keywords") or []:
                if not isinstance(entry, (list, tuple)) or len(entry) < 2:
                    continue
                if str(entry[1]) == "hlPerp":
                    underlying = question_fields.get(str(entry[0]), "")
                    break

        sides: List[str] = []
        for side in raw.get("sideSpecs") or []:
            side_name = str((side or {}).get("name") or "").strip()
            side_template_id = _template_id(side_name)
            if side_template_id:
                side_template = templates.get(side_template_id)
                side_name = _render(side_template, fields) or side_template_id
            if side_name:
                sides.append(side_name)
        if len(sides) < 2:
            sides = ["Yes", "No"]

        side_index = 1 if len(sides) > 1 else 0
        coin = f"#{outcome_id * 10 + side_index}"
        mid0 = _finite(mids.get(f"#{outcome_id * 10}"))
        mid1 = _finite(mids.get(coin))
        mid = mid1 if mid1 > 0 else (1.0 - mid0 if mid0 > 0 else 0.0)

        rows.append({
            "id": outcome_id,
            "outcome_id": outcome_id,
            "key": coin,
            "name": name,
            "raw_name": raw_name,
            "question_id": int(question.get("question")) if question and question.get("question") is not None else None,
            "question_title": question_title,
            "category": category,
            "category_label": CATEGORY_LABELS.get(category, category.replace("_", " ").title()),
            "subcategory": subcategory or None,
            "description": description,
            "venue": VENUE,
            "underlying": underlying or None,
            "is_fallback": is_fallback,
            "sides": sides,
            "side_index": side_index,
            "side_name": sides[side_index],
            "mid0": round(mid0, 8),
            "mid1": round(mid1, 8),
            "mid": round(mid, 8),
            "logo": _initials(name),
        })

    rows.sort(key=_sort_key)
    _attach_logos(rows)
    return rows


def _sort_key(row: Dict[str, Any]) -> Tuple[Any, ...]:
    category = str(row.get("category") or "other").lower()
    try:
        category_rank = CATEGORY_ORDER.index(category)
    except ValueError:
        category_rank = len(CATEGORY_ORDER)
    group = str(row.get("question_title") or row.get("name") or "").lower()
    return (
        category_rank,
        group,
        1 if row.get("is_fallback") else 0,
        str(row.get("name") or "").lower(),
        int(row.get("id") or 0),
    )


def _logo_index() -> Dict[str, Dict[str, str]]:
    """Return ``symbol -> {path, url}`` for icons present on disk."""
    index: Dict[str, Dict[str, str]] = {}
    try:
        manifest = json.loads(LOGO_MANIFEST.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        manifest = {}
    icons = manifest.get("icons") if isinstance(manifest, dict) else None
    if isinstance(icons, dict):
        for symbol, entry in icons.items():
            if not isinstance(entry, dict):
                continue
            relative = str(entry.get("path") or "").strip()
            if not relative:
                continue
            path = LOGO_ROOT / relative
            try:
                if path.is_file() and path.stat().st_size > 0:
                    index[str(symbol)] = {
                        "path": relative,
                        "url": f"{LOGO_URL_PREFIX}/{quote(path.name)}",
                    }
            except OSError:
                continue
    return index


def _attach_logos(rows: List[Dict[str, Any]]) -> None:
    index = _logo_index()
    for row in rows:
        chosen: Optional[Tuple[str, str, str]] = None
        for kind, symbol in icon_candidates(row):
            entry = index.get(symbol)
            if entry:
                chosen = (kind, symbol, entry["url"])
                break
            # A manifest may be absent while the deterministic files are
            # already on disk (e.g. first boot after a manual download).
            path = LOGO_ROOT / safe_logo_filename(symbol)
            try:
                if path.is_file() and path.stat().st_size > 0:
                    chosen = (kind, symbol, f"{LOGO_URL_PREFIX}/{quote(path.name)}")
                    break
            except OSError:
                continue
        if chosen:
            row["icon_kind"], row["icon_symbol"], row["logo_url"] = chosen
        else:
            row["icon_kind"] = None
            row["icon_symbol"] = None
            row["logo_url"] = None


def catalog_snapshot(force: bool = False) -> List[Dict[str, Any]]:
    """Return all ``out`` HIP-4 outcomes, cached for 60 seconds."""
    now = time.monotonic()
    with _lock:
        if not force and now - _cache["ts"] < CATALOG_TTL_SEC:
            return [dict(row) for row in _cache["rows"]]
    try:
        meta, templates, mids = _fetch_payloads()
        rows = _build_rows(meta, templates, mids)
    except Exception as exc:  # noqa: BLE001 - keep the last good catalog
        logger.warning("HIP-4 catalog refresh failed: %s", exc)
        with _lock:
            return [dict(row) for row in _cache["rows"]]

    by_key = {str(row["key"]): row for row in rows}
    by_id = {int(row["id"]): row for row in rows}
    with _lock:
        _cache.update(
            ts=now,
            rows=rows,
            logo_index=_logo_index(),
            by_key=by_key,
            by_id=by_id,
        )
    return [dict(row) for row in rows]


def resolve_market(value: Any) -> Optional[Dict[str, Any]]:
    """Resolve ``ALL``-independent selectors to one catalog row.

    Accepted forms: ``#14881`` (exact coin, any side), ``1488`` (outcome id),
    or an outcome display name.  ``None`` means the outcome is unknown or does
    not belong to the ``out`` venue.
    """
    raw = str(value or "").strip()
    if not raw:
        return None
    rows = catalog_snapshot()
    if not rows:
        return None
    by_id = {int(row["id"]): row for row in rows}

    if raw.startswith("#"):
        digits = raw[1:]
        if not digits.isdigit():
            return None
        encoded = int(digits)
        outcome_id, side_index = divmod(encoded, 10)
        row = by_id.get(outcome_id)
        if row is None:
            return None
        if side_index == row["side_index"]:
            return dict(row)
        sides = row.get("sides") or []
        if side_index >= len(sides):
            return None
        selected = dict(row)
        selected["key"] = raw
        selected["side_index"] = side_index
        selected["side_name"] = sides[side_index]
        selected["mid"] = row["mid0"] if side_index == 0 else row["mid1"]
        return selected

    if raw.isdigit():
        row = by_id.get(int(raw))
        return dict(row) if row else None

    needle = raw.casefold()
    for row in rows:
        if str(row.get("name") or "").casefold() == needle:
            return dict(row)
    for row in rows:
        haystack = f"{row.get('name', '')} {row.get('question_title', '')}".casefold()
        if needle in haystack:
            return dict(row)
    return None


def all_market_keys() -> List[str]:
    """Default side of every live ``out`` outcome (used by the ALL basket)."""
    return [str(row["key"]) for row in catalog_snapshot()]


def category_counts(rows: Optional[Iterable[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    rows = list(rows if rows is not None else catalog_snapshot())
    counts: Dict[str, int] = {}
    for row in rows:
        category = str(row.get("category") or "other")
        counts[category] = counts.get(category, 0) + 1
    ordered = [key for key in CATEGORY_ORDER if key in counts]
    ordered.extend(sorted(key for key in counts if key not in CATEGORY_ORDER))
    return [
        {"key": key, "label": CATEGORY_LABELS.get(key, key.title()), "count": counts[key]}
        for key in ordered
    ]


def market_payload(rows: Optional[Iterable[Dict[str, Any]]] = None) -> Dict[str, Any]:
    rows = list(rows if rows is not None else catalog_snapshot())
    return {
        "venue": VENUE,
        "count": len(rows),
        "categories": category_counts(rows),
        "outcomes": rows,
    }
