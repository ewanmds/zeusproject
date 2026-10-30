"""
Trading Bot Dashboard — FastAPI Backend Server
================================================
Wraps the BBO/MID Order Book market maker and the Outcome (HIP-4) spread bot
and exposes REST API + WebSocket endpoints for a frontend dashboard.
(L'onglet "Grid" du frontend pilote le moteur BBO/MID.)
"""

import os
import sys

# Reconfigure standard streams to UTF-8 to prevent UnicodeEncodeErrors on Windows consoles when printing emojis
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


import json
import time
import math
import threading
import asyncio
import logging
import signal
import secrets
import uuid
from contextlib import asynccontextmanager
import subprocess
from pathlib import Path
from typing import Optional, Dict, Any, Literal
from urllib.parse import quote

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import uvicorn
from zeus import PROJECT_ROOT
from zeus.safety import atomic_json, account_snapshot, safe_error


def _pid_alive(pid):
    if not pid:
        return False
    if os.name == "nt":
        import ctypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.OpenProcess(0x1000, False, int(pid))
        if not handle:
            return ctypes.get_last_error() == 5
        code = ctypes.c_ulong()
        kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        ok = kernel.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel.CloseHandle(handle)
        return bool(ok and code.value == 259)
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True

GRID_NUM_LEVELS = 1
logger = logging.getLogger("zeus.server")

# ---------------------------------------------------------------------------
# .env loader (manual, no external dependency)
# ---------------------------------------------------------------------------
def load_dotenv(path: Path) -> None:
    """Read a .env file and populate os.environ (does not overwrite)."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        # Strip surrounding quotes if present
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        os.environ.setdefault(key, value)

# Load .env at import time so bot modules can see the env vars
BASE_DIR = PROJECT_ROOT
load_dotenv(BASE_DIR / ".env")

BOT_API_TOKEN = os.getenv("BOT_API_TOKEN", "").strip()
BOT_CORS_ORIGINS = [
    origin.strip().rstrip("/")
    for origin in os.getenv("BOT_CORS_ORIGINS", "*").split(",")
    if origin.strip()
]


def _require_api_token(request: Request) -> None:
    """Protect control/status HTTP endpoints when BOT_API_TOKEN is configured."""
    if not BOT_API_TOKEN:
        return

    provided = request.headers.get("x-api-token", "").strip()
    if not provided:
        authorization = request.headers.get("authorization", "")
        if authorization.lower().startswith("bearer "):
            provided = authorization[7:].strip()

    if not provided or not secrets.compare_digest(provided, BOT_API_TOKEN):
        raise HTTPException(status_code=401, detail="Invalid API token")

# ---------------------------------------------------------------------------
# Pydantic request models
# ---------------------------------------------------------------------------
class GridStartRequest(BaseModel):
    """Formulaire minimal: le marché, le notional, et pour MID l'offset au mid."""
    token: str = "SP500"
    strategy: str = "bbo"
    mode: str = "live"
    order_size_usd: float = 100
    # Champ legacy tolere: le Grid reste cape a 1 ordre max.
    max_position_usd: Optional[float] = None
    # Champs legacy tolérés (vieux onglets) — ignorés.
    spread_ticks: Optional[float] = None
    offset_ticks: Optional[float] = None
    spread_bps: Optional[float] = None
    offset_bps: Optional[float] = None  # utilisé par strategy=mid (distance au mid)
    grid_reset_bps: Optional[float] = None
    num_levels: int = 1


class GridStopRequest(BaseModel):
    """Stop ciblé: token précis, ou tous les bots si omis."""
    token: Optional[str] = None


class WorldcupStartRequest(BaseModel):
    exec_mode: Literal["maker", "taker"] = "taker"
    maker_spread_pct: float = Field(default=0.005, ge=0, allow_inf_nan=False)
    order_size_usd: float = 50
    # En %, même convention que --max-spread-pct (0.02 = 0.02% = 2 bps).
    max_spread_pct: Optional[float] = None
    # Legacy (vieux onglets): interprété en bps si max_spread_pct absent.
    max_spread_bps: float = 2.0
    max_position_usd: float = 250
    # "ALL" = every live ``out`` outcome, otherwise an exact outcome coin
    # ("#14881") or an outcome id/name resolved through zeus.outcomes.
    market: str = "ALL"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _fmt_duration(elapsed_seconds: float) -> str:
    """Format elapsed seconds as HH:MM:SS."""
    total = int(elapsed_seconds)
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def _json_float(value: Any, ndigits: int = 2, default: float = 0.0) -> float:
    """Coerce a numeric value to a JSON-safe float."""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(num):
        return default
    return round(num, ndigits)


def _safe_import(module_name: str):
    """Import a module by name, returning None on failure."""
    try:
        import sys
        import importlib
        return importlib.import_module(module_name)
    except (Exception, SystemExit) as exc:
        logger.error("Module %s unavailable: %s", module_name, safe_error(exc))
        return None


LOGO_ROOT = PROJECT_ROOT / "frontend" / "assets" / "logos" / "hip3"


def _token_logo_text(ticker: str) -> str:
    """Deterministic fallback mark when Hyperliquid publishes no icon."""
    cleaned = "".join(ch for ch in str(ticker).upper() if ch.isalnum())
    return (cleaned[:2] or "?").ljust(2, " ")


def _safe_path_part(value: str, *, allowed: str = "._-") -> str:
    raw = str(value or "")
    cleaned = "".join(ch for ch in raw if ch.isalnum() or ch in allowed)
    return cleaned if cleaned == raw else ""


def _logo_url(dex: str, ticker: str) -> Optional[str]:
    """Return the locally cached Hyperliquid icon URL, or None."""
    safe_dex = _safe_path_part(dex.lower())
    safe_ticker = _safe_path_part(ticker)
    if not safe_dex or not safe_ticker:
        return None
    path = LOGO_ROOT / safe_dex / f"{safe_ticker}.svg"
    try:
        if path.is_file() and path.stat().st_size > 0:
            return f"/frontend/assets/logos/hip3/{safe_dex}/{quote(safe_ticker)}.svg"
    except OSError:
        return None
    return None


def _refresh_grid_token_catalog(grid_mod) -> Dict[str, Dict[str, Any]]:
    """Merge presets with the live HIP-3 universe (60 s cache)."""
    refresh = getattr(grid_mod, "refresh_catalog", None)
    if callable(refresh):
        try:
            return refresh()
        except Exception as exc:  # noqa: BLE001 - keep the picker usable offline
            logger.warning("HIP-3 catalog refresh failed: %s", exc)

    # Legacy fallback for an older markets module: merge whatever is available.
    token_map: Dict[str, Dict[str, Any]] = {
        str(key).upper(): dict(cfg)
        for key, cfg in getattr(grid_mod, "TOKENS", {}).items()
        if isinstance(cfg, dict) and cfg.get("symbol")
    }
    try:
        if all(
            hasattr(grid_mod, attr)
            for attr in ("_fetch_hip3_meta", "_fetch_all_mids", "_dynamic_token_config")
        ):
            markets = grid_mod._fetch_hip3_meta()
            mids = grid_mod._fetch_all_mids()
            covered = {str(cfg["symbol"]).lower() for cfg in token_map.values()}
            for item in markets:
                built = grid_mod._dynamic_token_config(item, mids)
                if built is None:
                    continue
                key, cfg = built
                symbol = str(cfg.get("symbol", "")).lower()
                if not symbol or symbol in covered:
                    continue
                covered.add(symbol)
                token_map[key] = cfg
    except Exception as exc:  # noqa: BLE001
        logger.warning("HIP-3 legacy catalog merge failed: %s", exc)
    return token_map


def _grid_token_payload(key: str, cfg: Dict[str, Any]) -> Dict[str, Any]:
    symbol = str(cfg.get("symbol", key))
    dex, _, ticker = symbol.partition(":")
    dex = str(cfg.get("dex") or dex or "xyz").lower()
    ticker = str(cfg.get("ticker") or ticker or symbol).upper()
    mid = _json_float(cfg.get("api_mid"), 8)
    return {
        "key": key,
        "symbol": symbol,
        "dex": dex,
        "ticker": ticker,
        "name": cfg.get("name") or symbol,
        "logo": _token_logo_text(ticker),
        "logo_url": _logo_url(dex, ticker),
        "mid": mid,
        "max_leverage": cfg.get("max_leverage"),
        "is_cross": cfg.get("is_cross", True),
        "day_volume": _json_float(cfg.get("day_volume"), 2),
        "sz_decimals": cfg.get("sz_decimals"),
    }


# ---------------------------------------------------------------------------
# Monkey-patch signal.signal so threaded bots don't crash
# ---------------------------------------------------------------------------
_original_signal = signal.signal

def _safe_signal(signum, handler):
    """Wrapper around signal.signal that silently ignores calls from non-main threads."""
    try:
        return _original_signal(signum, handler)
    except ValueError:
        # "signal only works in main thread" — harmless when bots run in daemon threads
        return None

signal.signal = _safe_signal


# ---------------------------------------------------------------------------
# Bot Manager
# ---------------------------------------------------------------------------
class BotManager:
    """Manages lifecycle and stats for both trading bots."""

    def __init__(self, state_dir=None) -> None:
        # OBI bots: un PROCESS ENFANT par token. token_key -> {proc, symbol,
        # stats_file, stop_file, started, config}
        self.obi_procs: Dict[str, Dict[str, Any]] = {}
        self.grid_config: Dict[str, Any] = {}   # dernière config (restart)

        # WorldCup Spread bot state
        self.wc_bot = None            # HyperliquidSpreadBot instance
        self.wc_thread: Optional[threading.Thread] = None
        self.wc_running: bool = False
        self.wc_config: Dict[str, Any] = {}
        self.wc_stop_event = threading.Event()
        self.wc_state_file = None
        self.wc_lifecycle = {"phase": "stopped", "cleanup_confirmed": True, "can_start": True, "error": "", "cleanup_error": ""}
        self.state_dir = None if state_dir is False else Path(state_dir or PROJECT_ROOT / ".zeus-state")

        # Connected WebSocket clients
        self.ws_clients: set = set()

        # Last good stats payloads (used when bot lock is busy)
        self._grid_stats_cache: Dict[str, Any] = {}
        self._wc_stats_cache: Dict[str, Any] = {}

        # Manager-level lock
        self.lock = threading.Lock()

    def _session_files(self):
        if self.state_dir is None:
            return []
        result = []
        for path in self.state_dir.glob("*.json"):
            try:
                result.append((path, json.loads(path.read_text(encoding="utf-8"))))
            except (OSError, ValueError):
                raise RuntimeError("Session journal unreadable; manual verification required")
        return result

    def _verify_session_flat(self, record):
        from hyperliquid.info import Info
        from zeus.safety import confirmed_client_order
        kind = record["account_kind"]
        account = os.getenv("WALLET_ADDRESS_OUTCOME" if kind == "outcome" else "WALLET_ADDRESS", "").strip()
        if not account:
            raise RuntimeError("Account unavailable for stop verification")
        info = Info("https://api.hyperliquid.xyz", skip_ws=True, timeout=5)
        symbols = record["symbols"]
        for cloid in record.get("pending_cloids", []):
            _, status = confirmed_client_order(info, account, cloid, symbols)
            if status == "open":
                raise RuntimeError("Placement still open; stop not confirmed")
        rows = account_snapshot(info, account, symbols, spot=kind == "outcome",
                                balances={s: "+" + s.lstrip("#") for s in symbols})
        if any(position or orders for _, position, orders in rows):
            raise RuntimeError("Residual orders/position; manual reconciliation required")

    def _reserve_session(self, key, symbols, kind):
        for _, record in self._session_files():
            if record.get("key") == key and not record.get("cleanup_confirmed"):
                raise RuntimeError("Previous session stop not confirmed; reconcile before starting")
        if self.state_dir is None:
            return None
        path = self.state_dir / (uuid.uuid4().hex + ".json")
        atomic_json(path, {"session_id": path.stem, "key": key, "symbols": symbols,
                           "account_kind": kind, "phase": "connecting", "pid": os.getpid(),
                           "cleanup_confirmed": False, "can_start": False, "ts": time.time()})
        return path

    def _session_status(self, path, fallback=None):
        if path:
            try:
                return json.loads(Path(path).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return {"phase": "failed", "cleanup_confirmed": False, "can_start": False,
                        "cleanup_error": "Session status unreadable", "stale": True}
        return fallback or {}

    def reconcile_sessions(self):
        unresolved = []
        for path, record in self._session_files():
            if record.get("cleanup_confirmed"):
                continue
            owned = any(str(info.get("safety_file")) == str(path) and info["proc"].poll() is None
                        for info in self.obi_procs.values())
            if str(self.wc_state_file) == str(path) and self.wc_thread and self.wc_thread.is_alive():
                owned = True
            if owned or record.get("pid") != os.getpid() and _pid_alive(record.get("pid")) or _pid_alive(record.get("child_pid")):
                unresolved.append(record["key"])
                continue
            try:
                self._verify_session_flat(record)
                atomic_json(path, {**record, "phase": "failed", "cleanup_confirmed": True,
                                   "can_start": True, "cleanup_error": "", "ts": time.time()})
                if str(path) == str(self.wc_state_file) and self.wc_bot is not None:
                    self.wc_bot.safety.cleanup_confirmed = True
                    self.wc_bot.safety.cleanup_error = ""
                    self.wc_bot.safety.phase = "failed"
            except Exception as exc:
                unresolved.append(record["key"])
                atomic_json(path, {**record, "phase": "failed", "cleanup_confirmed": False,
                                   "can_start": False, "cleanup_error": safe_error(exc), "ts": time.time()})
        return {"unresolved": unresolved, "can_start": not unresolved}

    def _orphans(self, kind):
        attached = {str(info.get("safety_file")) for info in self.obi_procs.values()}
        if self.wc_state_file:
            attached.add(str(self.wc_state_file))
        return [record for path, record in self._session_files()
                if str(path) not in attached and record.get("account_kind") == kind and not record.get("cleanup_confirmed")]

    # -----------------------------------------------------------------------
    # Market-making bots
    # -----------------------------------------------------------------------
    def start_grid(self, config: GridStartRequest) -> None:
        """Lance UN process enfant `python -m zeus.bots.order_book --token X --live`
        par token — plusieurs tokens peuvent tourner en parallèle."""
        if config.mode not in ('shadow', 'live'):
            raise ValueError('mode must be shadow or live')
        if config.mode != 'live':
            raise ValueError('Market making requires mode=live')
        token_key = (config.token or "SP500").strip().upper()
        strategy = (config.strategy or "bbo").strip().lower()
        if strategy not in ("bbo", "mid", "freqtrade"):
            raise ValueError(f"Unknown market making strategy: {config.strategy}")
        offset_bps = None
        if strategy == "mid":
            offset_bps = float(config.offset_bps if config.offset_bps is not None else 1.0)
            if not math.isfinite(offset_bps) or not 0 <= offset_bps < 10000:
                raise ValueError("offset_bps must be finite, at least 0 and less than 10000")
            offset_bps = max(0.0, offset_bps)
        module_name = "zeus.bots.freqtrade" if strategy == "freqtrade" else "zeus.bots.order_book"
        obi_mod = _safe_import(module_name)
        if obi_mod is None:
            raise RuntimeError(
                f"Cannot import {module_name} — check the Zeus installation"
            )
        try:
            symbol = obi_mod.resolve_token(token_key)
        except BaseException as exc:  # resolve_token lève SystemExit si inconnu
            raise ValueError(f"Unknown token '{config.token}': {exc}")
        order_usd = max(10.5, float(config.order_size_usd))
        if strategy == "freqtrade":
            order_usd = float(config.order_size_usd)
            if not math.isfinite(order_usd) or order_usd < 40:
                raise ValueError("Montant Freqtrade minimal : 40 USD (100 % par côté, un ordre par côté), valeur finie requise")
            obi_mod.require_binary()

        with self.lock:
            needs_confirmation = any(info["symbol"] == symbol and info.get("stop_unconfirmed") for info in self.obi_procs.values())
        confirmed_flat = False
        if needs_confirmation:
            from zeus.bots.freqtrade import confirm_flat
            confirmed_flat = confirm_flat(symbol, os.getenv("WALLET_ADDRESS", ""))

        with self.lock:
            if any(info["symbol"] == symbol and info["proc"].poll() is None for info in self.obi_procs.values()):
                raise RuntimeError(f"Un bot tourne déjà sur {symbol}")
            if not confirmed_flat and any(info["symbol"] == symbol and info.get("stop_unconfirmed") for info in self.obi_procs.values()):
                raise RuntimeError("La mise à plat précédente n'est pas confirmée ; aucun nouveau bot lancé")
            existing = self.obi_procs.get(token_key)
            if existing is not None and existing["proc"].poll() is None:
                raise RuntimeError(f"Un bot tourne déjà sur {token_key}")

            # Stopfile périmé d'un run précédent: le supprimer avant de lancer.
            stop_file = obi_mod.stop_file_path(symbol)
            try:
                os.remove(stop_file)
            except OSError:
                pass

            env = os.environ.copy()
            env["OBI_HEADLESS"] = "1"   # pas de Rich Live dans un process enfant
            env["ZEUS_PARENT_PID"] = str(os.getpid())
            safety_file = self._reserve_session("grid:" + symbol, [symbol], "perp")
            if safety_file:
                env["ZEUS_SAFETY_FILE"] = str(safety_file)
            cmd = [
                sys.executable, "-u", "-m", module_name,
                "--token", token_key, "--live", "--notional", str(order_usd),
                "--strategy", strategy,
            ]
            if offset_bps is not None:
                cmd.extend(["--offset-bps", str(offset_bps)])
            proc = subprocess.Popen(
                cmd, cwd=str(BASE_DIR), env=env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            if safety_file:
                record = self._session_status(safety_file)
                atomic_json(safety_file, {**record, "pid": proc.pid})
            self.obi_procs[token_key] = {
                "safety_file": safety_file,
                "proc": proc,
                "symbol": symbol,
                "stats_file": obi_mod.stats_file_path(symbol),
                "stop_file": stop_file,
                "started": time.time(),
                "config": {"token": token_key, "strategy": strategy, "mode": "live",
                           "order_size_usd": order_usd},
            }
            if offset_bps is not None:
                self.obi_procs[token_key]["config"]["offset_bps"] = offset_bps
            self.grid_config = config.model_dump()

        # Détection de crash immédiat (verrou d'instance déjà pris, etc.)
        time.sleep(1.5)
        if proc.poll() is not None:
            tail = ""
            try:
                status = json.loads(Path(self.obi_procs[token_key]["stats_file"]).read_bytes())
                if status.get("start_time", 0) >= self.obi_procs[token_key]["started"] - 5:
                    tail = str(status.get("error", ""))
            except (OSError, ValueError):
                pass
            if not tail:
                tail = f"Code de sortie {proc.returncode}"
            raise RuntimeError(f"Le bot {token_key} s'est arrêté immédiatement: {tail}")

    def stop_grid(self, token: Optional[str] = None) -> None:
        """Arrêt PROPRE via stopfile (le bot fait cancel all + flatten avant
        de sortir). token=None → stoppe tous les bots."""
        with self.lock:
            if token:
                keys = [token.strip().upper()]
            else:
                keys = list(self.obi_procs.keys())
            targets = [(k, self.obi_procs[k]) for k in keys if k in self.obi_procs]
        if not targets:
            if self._orphans("perp"):
                raise RuntimeError("Interrupted session: reconcile orders/positions before confirming stop")
            return

        failures = []
        stop_started = time.monotonic()
        for key, info in targets:
            try:
                Path(info["stop_file"]).write_text("stop", encoding="utf-8")
            except Exception as exc:
                info["stop_unconfirmed"] = True
                failures.append(key)
        for key, info in targets:
            deadline = stop_started + (95 if info["config"].get("strategy") == "freqtrade" else 30)
            proc = info["proc"]
            while proc.poll() is None and time.monotonic() < deadline:
                time.sleep(0.1)
            lifecycle = self._session_status(info.get("safety_file"))
            confirmed = proc.poll() is not None and lifecycle.get("cleanup_confirmed") is True
            info["stop_unconfirmed"] = not confirmed
            if not confirmed:
                failures.append(key)
            # Keep final records/statistics and the sentinel until verified.
        if failures:
            raise RuntimeError("Stop not confirmed: " + ", ".join(sorted(set(failures))))

    def get_grid_stats(self) -> Dict[str, Any]:
        default = {
            "type": "grid_stats",
            "running": False,
            "duration": "00:00:00",
            "volume_usd": 0.0,
            "net_pnl": 0.0,
            "fees": 0.0,
            "cost_per_1m": 0.0,
            "fills": 0,
            "mid_price": 0.0,
            "spread_bps": 0.0,
            "position_usd": 0.0,
            "net_passive_capture": 0.0,
            "net_adverse_lost": 0.0,
            "adverse_halt": False,
            "config": self.grid_config,
        }
        default["bots"] = []
        try:
            now = time.time()
            with self.lock:
                items = list(self.obi_procs.items())
            bots = [{"token": r["symbols"][0].split(":")[-1], "symbol": r["symbols"][0],
                     "strategy": r.get("strategy", "bbo"), "running": _pid_alive(r.get("pid")),
                     "phase": "failed", "error": r.get("error") or "Interrupted session; stop not confirmed",
                     "cleanup_error": r.get("cleanup_error", ""), "cleanup_confirmed": False,
                     "can_start": False, "position_known": False, "position_usd": None,
                     "stale": True, "volume_usd": 0, "fills": 0, "fees": 0, "accounting_complete": False}
                    for r in self._orphans("perp")]
            tot_vol = tot_fees = tot_rpnl = tot_pos = 0.0
            tot_fills = 0
            earliest = None
            for key, info in items:
                alive = info["proc"].poll() is None
                st: Dict[str, Any] = {}
                try:
                    # Close the Windows file handle before parsing the JSON,
                    # so the publisher can replace the snapshot immediately.
                    st = json.loads(Path(info["stats_file"]).read_bytes())
                    if (st.get("symbol") != info["symbol"]
                            or float(st.get("start_time", 0) or 0) < info["started"] - 5):
                        st = {}   # fichier d'un autre run/token
                except Exception:
                    st = {}
                vol = float(st.get("volume", 0) or 0)
                fees_v = float(st.get("fees", 0) or 0)
                rpnl = float(st.get("rpnl", 0) or 0)
                start = float(st.get("start_time", info["started"]) or info["started"])
                elapsed_hours = max((now - start) / 3600.0, 1.0 / 3600.0)
                has_action_metrics = all(k in st for k in (
                    "order_placements", "order_modifications", "order_cancellations"))
                placements = int(st.get("order_placements", 0) or 0)
                modifications = int(st.get("order_modifications", 0) or 0)
                cancellations = int(st.get("order_cancellations", 0) or 0)
                action_count = placements + modifications + cancellations
                lifecycle = self._session_status(info.get("safety_file"), st)
                phase = str(lifecycle.get("phase") or st.get("phase") or "connecting")
                error = str(st.get("error", ""))
                error = str(lifecycle.get("error") or error)
                if info.get("stop_unconfirmed"):
                    phase = "failed"
                    error = error or "Arrêt non confirmé ; ordres ou position à vérifier"
                if not alive and phase != "stopped":
                    phase = "failed"
                    if not error:
                        error = f"Le processus s'est arrêté (code {info['proc'].returncode})."
                confirmed = lifecycle.get("cleanup_confirmed") is True and not alive
                position_known = bool(st.get("position_known")) if alive else confirmed
                entry = {
                    "cleanup_confirmed": confirmed,
                    "cleanup_error": lifecycle.get("cleanup_error", ""),
                    "can_start": not alive and confirmed,
                    "stale": bool(alive and (not st or now - float(st.get("ts", 0)) > 5)),
                    "token": key,
                    "strategy": info["config"].get("strategy", "bbo"),
                    "offset_bps": _json_float(info["config"].get("offset_bps", 0.0), 2),
                    "mode": info["config"].get("mode", "live"),
                    "symbol": info["symbol"],
                    "running": alive,
                    "phase": phase,
                    "duration": _fmt_duration(max(0.0, now - start)),
                    "volume_usd": _json_float(vol, 2),
                    "net_pnl": _json_float(rpnl - fees_v, 4),
                    "fees": _json_float(fees_v, 4),
                    "realized_pnl": _json_float(rpnl, 4),
                    "accounting_complete": st.get("accounting_complete") is not False,
                    "cost_per_1m": (_json_float((fees_v - rpnl) / (vol / 1_000_000), 2)
                                    if vol > 0 and st.get("accounting_complete") is not False else None),
                    "fills": int(st.get("fills", 0) or 0),
                    "order_placements": placements if has_action_metrics else None,
                    "order_modifications": modifications if has_action_metrics else None,
                    "order_cancellations": cancellations if has_action_metrics else None,
                    "order_requests": int(st.get("order_requests", 0) or 0) if has_action_metrics else None,
                    "order_actions_per_hour": _json_float(action_count / elapsed_hours, 1) if has_action_metrics else None,
                    "fills_per_hour": _json_float(int(st.get("fills", 0) or 0) / elapsed_hours, 1),
                    "mid_price": _json_float(st.get("mid", 0), 4),
                    "spread_bps": _json_float(st.get("spread_bps", 0), 2),
                    "markout_bid_bps": _json_float(st.get("markout_bid_bps", 0), 3),
                    "markout_ask_bps": _json_float(st.get("markout_ask_bps", 0), 3),
                    "quote_half_spread_bps": _json_float(st.get("quote_half_spread_bps", 0), 2),
                    "edge_required_bps": _json_float(st.get("edge_required_bps", 0), 2),
                    "edge_margin_bps": _json_float(st.get("edge_margin_bps", 0), 2),
                    "fee_bps": (_json_float(st.get("fee_bps"), 2)
                                if st.get("fee_bps") is not None else None),
                    "mid_offset_bps": _json_float(st.get("mid_offset_bps", 0), 2),
                    "position_known": position_known,
                    "position_usd": _json_float(st.get("position_usd", 0), 2) if position_known else None,
                    "upnl": _json_float(st.get("upnl", 0), 4),
                    "action": str(st.get("action", "")),
                    "error": error,
                }
                bots.append(entry)
                if entry['mode'] == 'live':
                    tot_vol += vol
                    tot_fees += fees_v
                    tot_rpnl += rpnl
                    tot_pos += float(st.get("position_usd", 0) or 0)
                    tot_fills += entry["fills"]
                    earliest = start if earliest is None else min(earliest, start)

            alive_bots = [b for b in bots if b["running"]]
            single = alive_bots[0] if len(alive_bots) == 1 else None
            result = {
                "type": "grid_stats",
                "running": bool(alive_bots),
                "duration": _fmt_duration(max(0.0, now - earliest)) if earliest else "00:00:00",
                "volume_usd": _json_float(tot_vol, 2),
                "net_pnl": _json_float(tot_rpnl - tot_fees, 4),
                "fees": _json_float(tot_fees, 4),
                "cost_per_1m": (_json_float((tot_fees - tot_rpnl) / (tot_vol / 1_000_000), 2)
                                if tot_vol > 0 and all(b["accounting_complete"] for b in alive_bots) else None),
                "fills": tot_fills,
                "mid_price": single["mid_price"] if single else 0.0,
                "spread_bps": single["spread_bps"] if single else 0.0,
                "position_known": all(b["position_known"] for b in alive_bots),
                "position_usd": _json_float(tot_pos, 2) if all(b["position_known"] for b in alive_bots) else None,
                "net_passive_capture": 0.0,
                "net_adverse_lost": 0.0,
                "adverse_halt": False,
                "bots": bots,
                "config": self.grid_config,
            }
            self._grid_stats_cache = result
            return result
        except Exception as exc:
            return {**(self._grid_stats_cache or default), "stale": True, "error": safe_error(exc), "can_start": False}

    # -----------------------------------------------------------------------
    # Outcome (HIP-4) Spread Bot
    # -----------------------------------------------------------------------
    def start_worldcup(self, config: WorldcupStartRequest) -> None:
        with self.lock:
            if self.wc_running or self.wc_thread is not None and self.wc_thread.is_alive():
                raise RuntimeError("Outcome bot is already running")
            if not self.wc_lifecycle.get("can_start", False):
                raise RuntimeError("Previous Outcome session is not reconciled")

            wc_mod = _safe_import("zeus.bots.worldcup")
            if wc_mod is None:
                raise RuntimeError(
                    "Cannot import zeus.bots.worldcup — check the Zeus installation"
                )

            outcome_mod = _safe_import("zeus.outcomes")
            if outcome_mod is None:
                raise RuntimeError(
                    "Cannot import zeus.outcomes — check the Zeus installation"
                )

            selected = str(config.market or "").strip()
            if selected.upper() in ("", "ALL"):
                symbols = outcome_mod.all_market_keys()
                if not symbols:
                    raise RuntimeError(
                        "Outcome catalog unavailable: no live HIP-4 markets"
                    )
            else:
                resolved = outcome_mod.resolve_market(selected)
                if resolved is None:
                    raise RuntimeError(
                        f"Unknown Outcome market: {selected}. "
                        "Refresh the Outcome market list and try again."
                    )
                symbols = [str(resolved["key"])]

            try:
                from hyperliquid.utils import constants
                base_url = constants.MAINNET_API_URL
            except ImportError:
                base_url = "https://api.hyperliquid.xyz"

            outcome_account = os.getenv("WALLET_ADDRESS_OUTCOME", "").strip()
            outcome_key = os.getenv("PRIVATE_KEY_OUTCOME", "").strip()
            if not outcome_account or not outcome_key:
                raise RuntimeError(
                    "Outcome requires WALLET_ADDRESS_OUTCOME and PRIVATE_KEY_OUTCOME in .env"
                )

            self.wc_state_file = self._reserve_session("outcome", symbols, "outcome")
            self.wc_stop_event = threading.Event()
            self.wc_lifecycle = {"phase": "connecting", "cleanup_confirmed": False, "can_start": False, "error": "", "cleanup_error": ""}
            cfg = wc_mod.BotConfig(
                base_url=base_url,
                perp_dex="xyz",
                live=True,
                symbols=symbols,
                keywords=[],
                max_symbols=max(1, len(symbols)),
                order_usd=config.order_size_usd,
                min_order_usd=config.order_size_usd,
                close_min_order_usd=0,
                max_spread_pct=(
                    config.max_spread_pct
                    if config.max_spread_pct is not None
                    else config.max_spread_bps / 100.0
                ),
                max_position_usd=config.max_position_usd,
                min_action_interval_sec=0.03,
                stale_sec=1.2,
                refresh_hz=12,
                stop_loss_usd=50,
                take_profit_usd=0,
                rest_bbo_hz=4,
                rest_workers=24,
                rest_max_per_sweep=2,
                rest_429_backoff_sec=2.5,
                market_slippage_bps=5,
                market_cycle_cooldown_sec=0.12,
                account_sync_sec=2,
                account_address=outcome_account,
                private_key=outcome_key,
                scan_only=False,
                worldcup=True,
                countries=[],
                outcome_side="no",
                exec_mode=config.exec_mode,
                maker_offset_pct=0.0,
                maker_spread_pct=config.maker_spread_pct,
                safety_file=str(self.wc_state_file) if self.wc_state_file else None,
            )

            self.wc_bot = None
            self.wc_config = config.model_dump()

            def run_wc():
                bot = None
                try:
                    bot = wc_mod.HyperliquidSpreadBot(cfg)
                    self.wc_bot = bot
                    if self.wc_stop_event.is_set():
                        bot.request_stop()
                    else:
                        bot.run()
                except BaseException as exc:
                    self.wc_lifecycle.update(error=safe_error(exc), phase="failed")
                    if bot is not None:
                        bot.safety.fail(exc)
                finally:
                    if bot is not None:
                        bot.shutdown()
                        self.wc_lifecycle = bot.safety.snapshot()
                    else:
                        self.wc_lifecycle.update(phase="failed", cleanup_confirmed=False, can_start=False)
                        if self.wc_state_file:
                            record = self._session_status(self.wc_state_file)
                            atomic_json(self.wc_state_file, {**record, **self.wc_lifecycle})
                    self.wc_running = False

            self.wc_thread = threading.Thread(target=run_wc, name="worldcup-bot", daemon=True)
            self.wc_running = True
            self.wc_thread.start()

    def stop_worldcup(self) -> None:
        with self.lock:
            self.wc_stop_event.set()
            thread, bot = self.wc_thread, self.wc_bot
            if thread is None:
                if self._orphans("outcome") or not self.wc_lifecycle.get("cleanup_confirmed"):
                    raise RuntimeError("Previous stop not confirmed")
                return
            if thread.is_alive():
                self.wc_lifecycle["phase"] = "stopping"
                if bot is not None:
                    bot.request_stop()
        thread.join(timeout=30)
        if thread.is_alive() or not self.wc_lifecycle.get("cleanup_confirmed"):
            raise RuntimeError("Outcome stop not confirmed; orders/position require verification")
        self.wc_running = False

    def get_worldcup_stats(self) -> Dict[str, Any]:
        if self.wc_bot is None:
            orphans = self._orphans("outcome")
            if orphans:
                self.wc_lifecycle = {**orphans[-1], "phase": "failed", "can_start": False,
                                     "error": orphans[-1].get("error") or "Interrupted session; stop not confirmed"}
            elif self.wc_thread is None:
                self.wc_lifecycle.update(cleanup_confirmed=True, can_start=True)
        default = {
            "type": "worldcup_stats",
            "running": False,
            "duration": "00:00:00",
            "volume_usd": 0.0,
            "net_pnl": 0.0,
            "fees": 0.0,
            "cost_per_1m": 0.0,
            "fills": 0,
            "mid_price": 0.0,
            "spread_bps": 0.0,
            "position_usd": 0.0,
            "active_markets": 0,
            "config": self.wc_config,
        }
        lifecycle = self.wc_bot.safety.snapshot() if self.wc_bot is not None else self.wc_lifecycle
        active = bool(self.wc_thread and self.wc_thread.is_alive())
        default.update(lifecycle, running=active)
        if self.wc_bot is None:
            return {**self._wc_stats_cache, **default}

        bot = self.wc_bot
        try:
            snap = bot.export_dashboard_stats()
            if snap is None:
                if self._wc_stats_cache:
                    cached = dict(self._wc_stats_cache)
                    cached.update(lifecycle, running=active, stale=True)
                    return cached
                return {**default, "stale": True}

            result = {
                "type": "worldcup_stats",
                "running": True,
                "duration": _fmt_duration(snap["duration_sec"]),
                "volume_usd": _json_float(snap["volume_usd"], 2),
                "net_pnl": _json_float(snap["net_pnl"], 4),
                "fees": _json_float(snap["fees"], 4),
                "cost_per_1m": _json_float(snap["cost_per_1m"], 2),
                "fills": int(snap["fills"]),
                "mid_price": _json_float(snap["mid_price"], 4),
                "spread_bps": _json_float(snap["spread_bps"], 2),
                "position_usd": _json_float(snap["position_usd"], 2),
                "active_markets": int(snap["active_markets"]),
                "config": self.wc_config,
            }
            result.update(lifecycle, running=active, stale=False)
            self._wc_stats_cache = result
            return result
        except Exception:
            return {**(self._wc_stats_cache or default), **lifecycle, "running": active, "stale": True}


# ---------------------------------------------------------------------------
# FastAPI Application
# ---------------------------------------------------------------------------
@asynccontextmanager
async def _server_lifespan(_app):
    await reconcile_on_startup()
    try:
        yield
    finally:
        await stop_on_server_shutdown()


app = FastAPI(title="Trading Bot Dashboard", lifespan=_server_lifespan)


async def reconcile_on_startup():
    try:
        await asyncio.to_thread(manager.reconcile_sessions)
    except Exception as exc:
        logger.error("Session recovery failed: %s", safe_error(exc))


async def stop_on_server_shutdown():
    results = await asyncio.gather(asyncio.to_thread(manager.stop_worldcup),
                                   asyncio.to_thread(manager.stop_grid), return_exceptions=True)
    for result in results:
        if isinstance(result, BaseException):
            logger.error("Shutdown not confirmed: %s", safe_error(result))

app.add_middleware(
    CORSMiddleware,
    allow_origins=BOT_CORS_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

manager = BotManager()

# ---------------------------------------------------------------------------
# Frontend serving
# ---------------------------------------------------------------------------
frontend_dir = BASE_DIR / "frontend"


# no-store: le navigateur gardait d'anciennes versions de app.js/index.html en
# cache, ce qui rendait les nouveaux composants (picker marché...) invisibles.
_NO_CACHE = {"Cache-Control": "no-store, must-revalidate"}


@app.get("/")
async def serve_frontend():
    index = frontend_dir / "index.html"
    if index.exists():
        return FileResponse(str(index), headers=_NO_CACHE)
    return JSONResponse(
        status_code=404,
        content={"detail": "frontend/index.html not found"},
    )


@app.get("/index.css")
async def serve_css():
    css_file = frontend_dir / "index.css"
    if css_file.exists():
        return FileResponse(str(css_file), media_type="text/css", headers=_NO_CACHE)
    return JSONResponse(status_code=404, content={"detail": "index.css not found"})


@app.get("/app.js")
async def serve_js():
    js_file = frontend_dir / "app.js"
    if js_file.exists():
        return FileResponse(str(js_file), media_type="application/javascript", headers=_NO_CACHE)
    return JSONResponse(status_code=404, content={"detail": "app.js not found"})


@app.get("/config.js")
async def serve_config():
    config_file = frontend_dir / "config.js"
    if config_file.exists():
        return FileResponse(str(config_file), media_type="application/javascript", headers=_NO_CACHE)
    return JSONResponse(status_code=404, content={"detail": "config.js not found"})


# Mount static directory for any other assets (images, fonts, etc.)
if frontend_dir.exists():
    app.mount(
        "/frontend",
        StaticFiles(directory=str(frontend_dir)),
        name="frontend",
    )

# ---------------------------------------------------------------------------
# REST API — Grid Bot
# ---------------------------------------------------------------------------
@app.get("/health")
async def health():
    return {"status": "ok", "service": "zeus-bot"}


@app.get("/api/grid/tokens")
async def get_grid_tokens(_: None = Depends(_require_api_token)):
    """Return available HIP-3 token metadata for the Grid bot picker."""
    try:
        from zeus import markets as grid_mod
        token_map = _refresh_grid_token_catalog(grid_mod)
        tokens = []
        seen_symbols = set()
        for key, cfg in sorted(token_map.items()):
            if not isinstance(cfg, dict) or not cfg.get("symbol"):
                continue
            symbol = str(cfg["symbol"]).lower()
            # Legacy aliases (SKHX / SKHXUSD) must not create duplicate rows.
            if symbol in seen_symbols:
                continue
            seen_symbols.add(symbol)
            tokens.append(_grid_token_payload(key, cfg))
        return {
            "dex": "multi",
            "dexes": list(getattr(grid_mod, "PERP_DEXS", [])),
            "count": len(tokens),
            "tokens": tokens,
        }
    except Exception as exc:
        logger.exception("HIP-3 token catalog unavailable: %s", exc)
        # Fallback list when the module isn't available
        # Keep the catalog aligned with markets the current bot can resolve.
        fallback = {
            "SP500": "xyz:SP500", "US500": "mkts:US500",
            "XYZ100": "xyz:XYZ100", "SKHX": "xyz:SKHX",
            "CL": "xyz:CL", "BRENTOIL": "xyz:BRENTOIL",
            "AAPL": "xyz:AAPL", "JPY": "xyz:JPY",
            "SPCX": "xyz:SPCX", "GOLD": "xyz:GOLD",
            "SILVER": "xyz:SILVER", "NVDA": "xyz:NVDA",
            "TSLA": "xyz:TSLA", "IO_SNDK": "io:SNDK",
            "IO_ANTH": "io:ANTH",
        }
        tokens = []
        for key, symbol in fallback.items():
            dex, _, ticker = symbol.partition(":")
            tokens.append({
                "key": key,
                "symbol": symbol,
                "dex": dex,
                "ticker": ticker,
                "name": symbol,
                "logo": _token_logo_text(ticker),
                "logo_url": _logo_url(dex, ticker),
                "mid": 0,
                "max_leverage": None,
                "is_cross": True,
                "day_volume": 0,
                "sz_decimals": None,
            })
        return {"dex": "multi", "dexes": ["xyz", "mkts", "io"],
                "count": len(tokens), "tokens": tokens}


@app.get("/api/outcomes/markets")
async def get_outcome_markets(_: None = Depends(_require_api_token)):
    """Return every live HIP-4 outcome from Hyperliquid's ``out`` deployer."""
    try:
        from zeus import outcomes as outcome_mod
        rows = outcome_mod.catalog_snapshot()
        return outcome_mod.market_payload(rows)
    except Exception as exc:
        logger.exception("HIP-4 outcome catalog unavailable: %s", exc)
        return {
            "venue": "out",
            "count": 0,
            "categories": [],
            "outcomes": [],
            "error": str(exc),
        }


@app.get("/api/health")
async def health(_: None = Depends(_require_api_token)):
    """Valide à la fois la joignabilité du backend ET le code d'accès
    (401 si BOT_API_TOKEN est configuré et que le code est faux)."""
    return {"status": "ok", "ts": time.time()}


@app.post("/api/grid/start")
async def start_grid(req: GridStartRequest, _: None = Depends(_require_api_token)):
    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, manager.start_grid, req)
        return {"status": "starting", "message": f"Connexion Hyperliquid sur {req.token}", "mode": req.mode}
    except Exception as e:
        return JSONResponse(
            status_code=400,
            content={"status": "error", "message": safe_error(e)},
        )


@app.post("/api/grid/stop")
async def stop_grid(req: Optional[GridStopRequest] = None, _: None = Depends(_require_api_token)):
    try:
        token = req.token if req else None
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, manager.stop_grid, token)
        return {"status": "ok", "message": f"Bot stopped ({token or 'all'})"}
    except Exception as e:
        return JSONResponse(
            status_code=400,
            content={"status": "error", "message": safe_error(e)},
        )

@app.post("/api/grid/restart")
async def restart_grid(_: None = Depends(_require_api_token)):
    try:
        if not manager.grid_config:
            return JSONResponse(
                status_code=400,
                content={"status": "error", "message": "No previous configuration to restart"},
            )
        req = GridStartRequest(**manager.grid_config)
        await asyncio.to_thread(manager.stop_grid)
        await asyncio.to_thread(manager.start_grid, req)
        return {"status": "ok", "message": "Grid bot restarted"}
    except Exception as e:
        return JSONResponse(
            status_code=400,
            content={"status": "error", "message": safe_error(e)},
        )


@app.get("/api/grid/status")
async def grid_status(_: None = Depends(_require_api_token)):
    return manager.get_grid_stats()


# ---------------------------------------------------------------------------
# REST API — Outcome (HIP-4) Bot
# ---------------------------------------------------------------------------
@app.post("/api/outcomes/start")
@app.post("/api/worldcup/start")
async def start_outcome(req: WorldcupStartRequest, _: None = Depends(_require_api_token)):
    try:
        await asyncio.to_thread(manager.start_worldcup, req)
        return {"status": "ok", "message": "Outcome bot started"}
    except Exception as e:
        return JSONResponse(
            status_code=400,
            content={"status": "error", "message": safe_error(e)},
        )


@app.post("/api/outcomes/stop")
@app.post("/api/worldcup/stop")
async def stop_outcome(_: None = Depends(_require_api_token)):
    try:
        await asyncio.to_thread(manager.stop_worldcup)
        state = manager.get_worldcup_stats()
        if state.get("running") or state.get("cleanup_confirmed") is not True:
            raise RuntimeError("Outcome stop not confirmed")
        return {"status": "ok", "message": "Outcome stop verified", "state": state}
    except Exception as e:
        return JSONResponse(
            status_code=400,
            content={"status": "error", "message": safe_error(e)},
        )


@app.get("/api/outcomes/status")
@app.get("/api/worldcup/status")
async def outcome_status(_: None = Depends(_require_api_token)):
    return manager.get_worldcup_stats()


# ---------------------------------------------------------------------------
# WebSocket — live stats push
# ---------------------------------------------------------------------------
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    if BOT_API_TOKEN:
        provided = websocket.query_params.get("token", "").strip()
        if not provided or not secrets.compare_digest(provided, BOT_API_TOKEN):
            await websocket.close(code=1008, reason="Invalid API token")
            return
    await websocket.accept()
    manager.ws_clients.add(websocket)
    loop = asyncio.get_running_loop()
    try:
        while True:
            grid_stats, wc_stats = await asyncio.gather(
                loop.run_in_executor(None, manager.get_grid_stats),
                loop.run_in_executor(None, manager.get_worldcup_stats),
            )
            await websocket.send_json(grid_stats)
            await websocket.send_json(wc_stats)
            # 1 s: le chrono du dashboard doit avancer seconde par seconde
            await asyncio.sleep(1)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        manager.ws_clients.discard(websocket)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
def main():
    """Serve the dashboard without starting a trading session."""
    uvicorn.run(
        app,
        host=os.getenv("BOT_HOST", "0.0.0.0"),
        port=int(os.getenv("BOT_PORT", "8080")),
        log_config=None,
        access_log=False,
    )


if __name__ == "__main__":
    main()
@app.post("/api/sessions/reconcile")
async def reconcile_sessions(_: None = Depends(_require_api_token)):
    return await asyncio.to_thread(manager.reconcile_sessions)
