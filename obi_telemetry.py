"""
Télémétrie du bot OBI — persistance des fills avec leur CONTEXTE de signal
et markouts multi-horizons (1s / 5s / 30s), en SQLite.

C'est la fondation de tout le tuning: sans attribution (quel fill, pris dans
quel contexte, a rapporté ou coûté combien N secondes plus tard), chaque
réglage du bot est une devinette. Avec elle, `obi_report.py` sort le cost/M
et le markout moyen par token × heure × bucket de signal × distance de quote.

Écriture 100% asynchrone: le chemin critique du bot pousse dans une Queue,
un thread dédié batch les INSERT — jamais de blocage I/O dans les callbacks
WebSocket ni la boucle de quoting.
"""

import os
import queue
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

MARKOUT_HORIZONS = (1.0, 5.0, 30.0)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS fills (
    tid             TEXT PRIMARY KEY,
    ts              REAL,
    side            TEXT,      -- 'B' (on a acheté) / 'A' (on a vendu)
    px              REAL,
    sz              REAL,
    fee             REAL,
    closed_pnl      REAL,
    crossed         INTEGER,   -- 1 = taker
    z_ofi           REAL,
    z_tfi           REAL,
    alpha_bps       REAL,
    spread_bps      REAL,
    vol_bps         REAL,
    regime          TEXT,
    mid             REAL,
    position_before REAL,
    dist_bbo_ticks  REAL,      -- distance du fill au BBO du même côté
    hour_utc        INTEGER
);
CREATE TABLE IF NOT EXISTS markouts (
    tid         TEXT,
    horizon_sec REAL,
    markout_bps REAL,          -- >0 = fill favorable
    PRIMARY KEY (tid, horizon_sec)
);
CREATE INDEX IF NOT EXISTS idx_fills_ts ON fills(ts);
CREATE TABLE IF NOT EXISTS metadata (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

_FILL_COLS = ("tid", "ts", "side", "px", "sz", "fee", "closed_pnl", "crossed",
              "z_ofi", "z_tfi", "alpha_bps", "spread_bps", "vol_bps", "regime",
              "mid", "position_before", "dist_bbo_ticks", "hour_utc")


def db_path_for(symbol: str) -> str:
    safe = "".join(c if c.isalnum() else "_" for c in symbol)
    return str(Path.home() / f"obi_fills_{safe}.db")


class FillLogger:
    """Writer SQLite hors chemin critique. Toutes les méthodes publiques sont
    non bloquantes; les erreurs d'I/O sont avalées (la télémétrie ne doit
    JAMAIS faire tomber le trading)."""

    def __init__(self, symbol: str, path: Optional[str] = None):
        self.symbol = symbol
        self.path = path or db_path_for(symbol)
        self._q: "queue.Queue[Optional[tuple]]" = queue.Queue(maxsize=10000)
        self._thread = threading.Thread(target=self._writer, name="obi-telemetry", daemon=True)
        self._thread.start()

    # ------------------------------------------------------------- API bot
    def log_fill(self, row: Dict[str, Any]) -> None:
        try:
            self._q.put_nowait(("fill", dict(row)))
        except Exception:
            pass

    def log_markout(self, tid: str, horizon_sec: float, markout_bps: float) -> None:
        try:
            self._q.put_nowait(("markout", (tid, horizon_sec, markout_bps)))
        except Exception:
            pass

    def close(self) -> None:
        try:
            self._q.put(None, timeout=1.0)
            self._thread.join(timeout=3)
        except Exception:
            pass

    # ------------------------------------------------------------- writer
    def _writer(self) -> None:
        try:
            con = sqlite3.connect(self.path)
            con.executescript(_SCHEMA)
            con.execute(
                "INSERT OR REPLACE INTO metadata(key, value) VALUES ('symbol', ?)",
                (self.symbol,),
            )
            con.commit()
        except Exception:
            return
        stop = False
        while not stop:
            batch = []
            try:
                item = self._q.get(timeout=1.0)
                if item is None:
                    stop = True
                else:
                    batch.append(item)
                # Vider ce qui s'est accumulé (batch d'INSERT en une passe).
                while True:
                    try:
                        item = self._q.get_nowait()
                    except queue.Empty:
                        break
                    if item is None:
                        stop = True
                        break
                    batch.append(item)
            except queue.Empty:
                continue
            if not batch:
                continue
            try:
                for kind, payload in batch:
                    if kind == "fill":
                        vals = tuple(payload.get(c) for c in _FILL_COLS)
                        con.execute(
                            f"INSERT OR IGNORE INTO fills ({','.join(_FILL_COLS)}) "
                            f"VALUES ({','.join('?' * len(_FILL_COLS))})", vals)
                    else:
                        con.execute(
                            "INSERT OR IGNORE INTO markouts (tid, horizon_sec, markout_bps) "
                            "VALUES (?,?,?)", payload)
                con.commit()
            except Exception:
                try:
                    con.rollback()
                except Exception:
                    pass
        try:
            con.close()
        except Exception:
            pass
