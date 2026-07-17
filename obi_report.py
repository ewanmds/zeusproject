"""Rapport d'attribution des fills OBI et ranking glissant multi-tokens.

Exemples::

    python obi_report.py
    python obi_report.py --hours 24
    python obi_report.py --db ~/obi_fills_xyz_SP500.db --ranking-out ranking.json

Sans ``--db``, toutes les bases ``~/obi_fills_*.db`` sont analysées. Le
rapport groupe les résultats par token, heure UTC, bucket de z_OFI et distance
au BBO. ``ranking.json`` est écrit atomiquement pour le dashboard.
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


DEFAULT_DB_GLOB = "obi_fills_*.db"
HORIZONS = (1.0, 5.0, 30.0)


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _symbol_from_path(path: Path) -> str:
    stem = path.stem
    raw = stem[len("obi_fills_"):] if stem.startswith("obi_fills_") else stem
    dex, sep, coin = raw.partition("_")
    return f"{dex}:{coin}" if sep else raw


def _symbol(con: sqlite3.Connection, path: Path) -> str:
    try:
        row = con.execute(
            "SELECT value FROM metadata WHERE key = 'symbol' LIMIT 1"
        ).fetchone()
        if row and row[0]:
            return str(row[0])
    except sqlite3.Error:
        pass
    return _symbol_from_path(path)


def discover_databases(paths: Sequence[str]) -> List[Path]:
    if paths:
        found = [Path(p).expanduser().resolve() for p in paths]
    else:
        found = sorted(Path.home().glob(DEFAULT_DB_GLOB))
    return [path for path in found if path.is_file()]


def load_fills(path: Path, cutoff: float) -> List[Dict[str, Any]]:
    """Charge une ligne par fill avec les markouts pivotés par horizon."""
    uri = f"file:{path.as_posix()}?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=5.0)
    con.row_factory = sqlite3.Row
    try:
        symbol = _symbol(con, path)
        query = """
            WITH mo AS (
                SELECT tid,
                       MAX(CASE WHEN horizon_sec = 1.0 THEN markout_bps END) AS mo_1,
                       MAX(CASE WHEN horizon_sec = 5.0 THEN markout_bps END) AS mo_5,
                       MAX(CASE WHEN horizon_sec = 30.0 THEN markout_bps END) AS mo_30
                  FROM markouts
                 GROUP BY tid
            )
            SELECT f.ts, f.side, f.px, f.sz, f.fee, f.closed_pnl, f.crossed,
                   f.z_ofi, f.z_tfi, f.alpha_bps, f.spread_bps, f.vol_bps,
                   f.regime, f.dist_bbo_ticks, f.hour_utc,
                   mo.mo_1, mo.mo_5, mo.mo_30
              FROM fills AS f
              LEFT JOIN mo ON mo.tid = f.tid
             WHERE f.ts >= ?
             ORDER BY f.ts
        """
        rows = []
        for row in con.execute(query, (cutoff,)):
            item = dict(row)
            item["symbol"] = symbol
            rows.append(item)
        return rows
    finally:
        con.close()


def z_bucket(value: Any) -> str:
    z = _finite(value)
    if z < -2.0:
        return "<-2"
    if z < -1.0:
        return "-2:-1"
    if z <= 1.0:
        return "-1:+1"
    if z <= 2.0:
        return "+1:+2"
    return ">+2"


def distance_bucket(value: Any) -> str:
    distance = max(0.0, _finite(value))
    if distance < 0.5:
        return "0"
    if distance < 1.5:
        return "1"
    if distance < 3.5:
        return "2-3"
    return "4+"


def _empty_metrics() -> Dict[str, Any]:
    return {
        "fills": 0,
        "maker_fills": 0,
        "volume": 0.0,
        "fees": 0.0,
        "closed_pnl": 0.0,
        "first_ts": float("inf"),
        "last_ts": 0.0,
        "markout_sum": {h: 0.0 for h in HORIZONS},
        "markout_n": {h: 0 for h in HORIZONS},
    }


def _add(metrics: Dict[str, Any], row: Dict[str, Any]) -> None:
    metrics["fills"] += 1
    metrics["maker_fills"] += 0 if int(_finite(row.get("crossed"))) else 1
    metrics["volume"] += abs(_finite(row.get("px")) * _finite(row.get("sz")))
    metrics["fees"] += _finite(row.get("fee"))
    metrics["closed_pnl"] += _finite(row.get("closed_pnl"))
    ts = _finite(row.get("ts"))
    metrics["first_ts"] = min(metrics["first_ts"], ts)
    metrics["last_ts"] = max(metrics["last_ts"], ts)
    for horizon, column in ((1.0, "mo_1"), (5.0, "mo_5"), (30.0, "mo_30")):
        value = row.get(column)
        if value is not None and math.isfinite(_finite(value, float("nan"))):
            metrics["markout_sum"][horizon] += float(value)
            metrics["markout_n"][horizon] += 1


def _finalize(metrics: Dict[str, Any], now: float) -> Dict[str, Any]:
    volume = metrics["volume"]
    net = metrics["closed_pnl"] - metrics["fees"]
    elapsed = max(1.0, min(now, metrics["last_ts"]) - metrics["first_ts"])
    result = {
        "fills": metrics["fills"],
        "maker_pct": (100.0 * metrics["maker_fills"] / metrics["fills"]
                      if metrics["fills"] else 0.0),
        "volume": volume,
        "fees": metrics["fees"],
        "closed_pnl": metrics["closed_pnl"],
        "net_pnl": net,
        "cost_per_m": ((metrics["fees"] - metrics["closed_pnl"])
                       / (volume / 1_000_000.0) if volume > 0 else 0.0),
        "pnl_per_hour": net / (elapsed / 3600.0),
    }
    for horizon in HORIZONS:
        count = metrics["markout_n"][horizon]
        result[f"markout_{int(horizon)}s"] = (
            metrics["markout_sum"][horizon] / count if count else None
        )
    return result


def build_context_report(rows: Iterable[Dict[str, Any]], now: float) -> List[Dict[str, Any]]:
    groups: Dict[Tuple[str, int, str, str], Dict[str, Any]] = defaultdict(_empty_metrics)
    for row in rows:
        hour = int(_finite(row.get("hour_utc"), time.gmtime(_finite(row["ts"])).tm_hour))
        key = (str(row["symbol"]), hour, z_bucket(row.get("z_ofi")),
               distance_bucket(row.get("dist_bbo_ticks")))
        _add(groups[key], row)
    report = []
    for (symbol, hour, z_name, distance), metrics in sorted(groups.items()):
        report.append({
            "symbol": symbol,
            "hour_utc": hour,
            "z_bucket": z_name,
            "distance_bucket": distance,
            **_finalize(metrics, now),
        })
    return report


def build_ranking(rows: Iterable[Dict[str, Any]], now: float, hours: float) -> Dict[str, Any]:
    groups: Dict[str, Dict[str, Any]] = defaultdict(_empty_metrics)
    for row in rows:
        _add(groups[str(row["symbol"])], row)
    entries = []
    for symbol, metrics in groups.items():
        entries.append({"symbol": symbol, **_finalize(metrics, now)})
    entries.sort(key=lambda row: (-row["pnl_per_hour"], row["cost_per_m"], row["symbol"]))
    for index, entry in enumerate(entries, 1):
        entry["rank"] = index
    return {
        "generated_at": datetime.fromtimestamp(now, timezone.utc).isoformat(),
        "window_hours": hours,
        "ranking": entries,
    }


def write_json_atomic(path: Path, payload: Dict[str, Any]) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _fmt(value: Optional[float], digits: int = 2) -> str:
    return "—" if value is None else f"{value:+.{digits}f}"


def print_report(report: Sequence[Dict[str, Any]]) -> None:
    headers = ("TOKEN", "H UTC", "Z OFI", "DIST", "FILLS", "VOL $", "COST/M",
               "NET $", "MO 1s", "MO 5s", "MO 30s")
    print("  ".join(headers))
    for row in report:
        print(
            f"{row['symbol']:<14}  {row['hour_utc']:>5}  {row['z_bucket']:<6}  "
            f"{row['distance_bucket']:<4}  {row['fills']:>5}  {row['volume']:>9.0f}  "
            f"{row['cost_per_m']:>+8.1f}  {row['net_pnl']:>+7.2f}  "
            f"{_fmt(row['markout_1s']):>7}  {_fmt(row['markout_5s']):>7}  "
            f"{_fmt(row['markout_30s']):>7}"
        )


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Attribution et ranking du bot OBI")
    parser.add_argument("--db", action="append", default=[], help="base SQLite (répétable)")
    parser.add_argument("--hours", type=float, default=48.0, help="fenêtre glissante")
    parser.add_argument(
        "--ranking-out",
        default=str(Path(__file__).with_name("ranking.json")),
        help="sortie JSON du ranking",
    )
    parser.add_argument("--json", action="store_true", help="imprimer le rapport en JSON")
    args = parser.parse_args(argv)
    hours = max(0.25, float(args.hours))
    now = time.time()
    cutoff = now - hours * 3600.0
    databases = discover_databases(args.db)
    rows: List[Dict[str, Any]] = []
    for path in databases:
        try:
            rows.extend(load_fills(path, cutoff))
        except (OSError, sqlite3.Error) as exc:
            print(f"warning: {path}: {exc}", file=sys.stderr)

    context = build_context_report(rows, now)
    ranking = build_ranking(rows, now, hours)
    write_json_atomic(Path(args.ranking_out), ranking)
    if args.json:
        print(json.dumps({"context": context, **ranking}, ensure_ascii=False, indent=2))
    else:
        if context:
            print_report(context)
        else:
            print("Aucun fill dans la fenêtre sélectionnée.")
        print(f"\nRanking écrit: {Path(args.ranking_out).expanduser().resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
