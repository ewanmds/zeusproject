"""
Trading Bot Dashboard — FastAPI Backend Server
================================================
Wraps the BBO/MID Order Book market maker and WorldCup Spread and exposes
REST API + WebSocket endpoints for a frontend dashboard.
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
import signal
import secrets
import subprocess
from pathlib import Path
from typing import Optional, Dict, Any

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn
from zeus import PROJECT_ROOT

GRID_NUM_LEVELS = 1

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
    order_size_usd: float = 50
    # En %, même convention que --max-spread-pct (0.02 = 0.02% = 2 bps).
    max_spread_pct: Optional[float] = None
    # Legacy (vieux onglets): interprété en bps si max_spread_pct absent.
    max_spread_bps: float = 2.0
    max_position_usd: float = 250
    # "ALL" = tous les marchés découverts, sinon un pays (ex "France").
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
        if module_name in sys.modules:
            # Force reload the module from disk to apply new edits/constants
            return importlib.reload(sys.modules[module_name])
        return __import__(module_name)
    except Exception as exc:
        return None


def _grid_token_key(token: str) -> str:
    """Normalize a HIP-3 coin name or dex-qualified symbol to a UI key."""
    return str(token or "").split(":", 1)[-1].upper()


def _token_logo_text(key: str) -> str:
    """Small deterministic fallback mark; Hyperliquid HIP-3 meta has no image URL."""
    cleaned = "".join(ch for ch in str(key).upper() if ch.isalnum())
    return (cleaned[:2] or "?").ljust(2, " ")


def _refresh_grid_token_catalog(grid_mod) -> Dict[str, Dict[str, Any]]:
    """Merge local presets with the live HIP-3 universe exposed by the bot module."""
    token_map: Dict[str, Dict[str, Any]] = dict(getattr(grid_mod, "TOKENS", {}))
    if all(
        hasattr(grid_mod, attr)
        for attr in ("_fetch_hip3_meta", "_fetch_all_mids", "_dynamic_token_config")
    ):
        markets = grid_mod._fetch_hip3_meta()
        mids = grid_mod._fetch_all_mids()
        for item in markets:
            built = grid_mod._dynamic_token_config(item, mids)
            if built is None:
                continue
            key, cfg = built
            token_map[key] = cfg
        grid_mod.TOKENS.update(token_map)
    return token_map


def _grid_token_payload(key: str, cfg: Dict[str, Any]) -> Dict[str, Any]:
    symbol = str(cfg.get("symbol", key))
    return {
        "key": key,
        "symbol": symbol,
        "logo": _token_logo_text(key),
        "mid": cfg.get("api_mid", 0.0),
        "max_leverage": cfg.get("max_leverage"),
        "is_cross": cfg.get("is_cross", True),
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

    def __init__(self) -> None:
        # OBI bots: un PROCESS ENFANT par token. token_key -> {proc, symbol,
        # stats_file, stop_file, started, config}
        self.obi_procs: Dict[str, Dict[str, Any]] = {}
        self.grid_config: Dict[str, Any] = {}   # dernière config (restart)

        # WorldCup Spread bot state
        self.wc_bot = None            # HyperliquidSpreadBot instance
        self.wc_thread: Optional[threading.Thread] = None
        self.wc_running: bool = False
        self.wc_config: Dict[str, Any] = {}

        # Connected WebSocket clients
        self.ws_clients: set = set()

        # Last good stats payloads (used when bot lock is busy)
        self._grid_stats_cache: Dict[str, Any] = {}
        self._wc_stats_cache: Dict[str, Any] = {}

        # Manager-level lock
        self.lock = threading.Lock()

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
        if strategy not in ("bbo", "mid"):
            raise ValueError(f"Unknown market making strategy: {config.strategy}")
        offset_bps = None
        if strategy == "mid":
            offset_bps = float(config.offset_bps if config.offset_bps is not None else 1.0)
            if not math.isfinite(offset_bps) or not 0 <= offset_bps < 10000:
                raise ValueError("offset_bps must be finite, at least 0 and less than 10000")
            offset_bps = max(0.0, offset_bps)
        obi_mod = _safe_import("zeus.bots.order_book")
        if obi_mod is None:
            raise RuntimeError(
                "Cannot import zeus.bots.order_book — check the Zeus installation"
            )
        try:
            symbol = obi_mod.resolve_token(token_key)
        except BaseException as exc:  # resolve_token lève SystemExit si inconnu
            raise ValueError(f"Unknown token '{config.token}': {exc}")
        order_usd = max(10.5, float(config.order_size_usd))

        with self.lock:
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
            cmd = [
                sys.executable, "-u", "-m", "zeus.bots.order_book",
                "--token", token_key, "--live", "--notional", str(order_usd),
                "--strategy", strategy,
            ]
            if offset_bps is not None:
                cmd.extend(["--offset-bps", str(offset_bps)])
            proc = subprocess.Popen(
                cmd, cwd=str(BASE_DIR), env=env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            self.obi_procs[token_key] = {
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
            return

        for key, info in targets:
            try:
                Path(info["stop_file"]).write_text("stop", encoding="utf-8")
            except Exception:
                pass

        # Laisser le temps du cancel all + flatten market avant d'escalader.
        deadline = time.time() + 30
        for key, info in targets:
            proc = info["proc"]
            while proc.poll() is None and time.time() < deadline:
                time.sleep(0.4)
            if proc.poll() is None:
                proc.terminate()
            try:
                os.remove(info["stop_file"])
            except OSError:
                pass
        with self.lock:
            for key, _ in targets:
                self.obi_procs.pop(key, None)

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
            if not items:
                return default

            bots = []
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
                phase = str(st.get("phase") or ("running" if st else "connecting"))
                error = str(st.get("error", ""))
                if not alive and phase != "stopped":
                    phase = "failed"
                    if not error:
                        error = f"Le processus s'est arrêté (code {info['proc'].returncode})."
                entry = {
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
                    "cost_per_1m": _json_float((fees_v - rpnl) / (vol / 1_000_000), 2) if vol > 0 else 0.0,
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
                    "mid_offset_bps": _json_float(st.get("mid_offset_bps", 0), 2),
                    "position_usd": _json_float(st.get("position_usd", 0), 2),
                    "upnl": _json_float(st.get("upnl", 0), 4),
                    "action": str(st.get("action", "")),
                    "error": error,
                }
                bots.append(entry)
                if alive and entry['mode'] == 'live':
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
                "cost_per_1m": _json_float((tot_fees - tot_rpnl) / (tot_vol / 1_000_000), 2) if tot_vol > 0 else 0.0,
                "fills": tot_fills,
                "mid_price": single["mid_price"] if single else 0.0,
                "spread_bps": single["spread_bps"] if single else 0.0,
                "position_usd": _json_float(tot_pos, 2),
                "net_passive_capture": 0.0,
                "net_adverse_lost": 0.0,
                "adverse_halt": False,
                "bots": bots,
                "config": self.grid_config,
            }
            self._grid_stats_cache = result
            return result
        except Exception:
            return self._grid_stats_cache or default

    # -----------------------------------------------------------------------
    # WorldCup Spread Bot
    # -----------------------------------------------------------------------
    def start_worldcup(self, config: WorldcupStartRequest) -> None:
        with self.lock:
            if self.wc_running:
                raise RuntimeError("WorldCup bot is already running")

            wc_mod = _safe_import("zeus.bots.worldcup")
            if wc_mod is None:
                raise RuntimeError(
                    "Cannot import zeus.bots.worldcup — check the Zeus installation"
                )

            try:
                from hyperliquid.utils import constants
                base_url = constants.MAINNET_API_URL
            except ImportError:
                base_url = "https://api.hyperliquid.xyz"

            cfg = wc_mod.BotConfig(
                base_url=base_url,
                perp_dex="xyz",
                live=True,
                symbols=[],
                keywords=["WORLD", "CUP", "2026", "CHAMPION", "WINNER"],
                max_symbols=8,
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
                account_address=(
                    os.getenv("WALLET_ADDRESS_OUTCOME")
                    or os.getenv("WALLET_ADDRESS", "")
                ),
                private_key=(
                    os.getenv("PRIVATE_KEY_OUTCOME")
                    or os.getenv("PRIVATE_KEY", "")
                ),
                scan_only=False,
                worldcup=True,
                countries=(
                    [
                        "France", "Argentina", "Spain", "England", "Portugal",
                        "Germany", "Brazil", "Netherlands", "USA", "Norway",
                        "Morocco",
                    ]
                    if str(config.market).strip().upper() in ("", "ALL")
                    else [str(config.market).strip()]
                ),
                outcome_side="no",
            )

            self.wc_config = config.model_dump()

            def run_wc():
                try:
                    self.wc_bot = wc_mod.HyperliquidSpreadBot(cfg)
                    self.wc_bot.run()
                except Exception:
                    pass
                finally:
                    self.wc_running = False

            self.wc_thread = threading.Thread(
                target=run_wc, name="worldcup-bot", daemon=True
            )
            self.wc_thread.start()
            self.wc_running = True

    def stop_worldcup(self) -> None:
        with self.lock:
            if not self.wc_running or self.wc_bot is None:
                return

        # shutdown() sets running=False and cancels open orders
        try:
            self.wc_bot.shutdown()
        except Exception:
            pass

        if self.wc_thread is not None:
            self.wc_thread.join(timeout=10)
        self.wc_running = False

    def get_worldcup_stats(self) -> Dict[str, Any]:
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
        if not self.wc_running or self.wc_bot is None:
            return default

        bot = self.wc_bot
        try:
            snap = bot.export_dashboard_stats()
            if snap is None:
                if self._wc_stats_cache:
                    cached = dict(self._wc_stats_cache)
                    cached["running"] = True
                    return cached
                return {**default, "running": True}

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
            self._wc_stats_cache = result
            return result
        except Exception:
            return self._wc_stats_cache or default


# ---------------------------------------------------------------------------
# FastAPI Application
# ---------------------------------------------------------------------------
app = FastAPI(title="Trading Bot Dashboard")

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
        tokens = [
            _grid_token_payload(key, cfg)
            for key, cfg in sorted(token_map.items())
            if isinstance(cfg, dict) and cfg.get("symbol")
        ]
        return {
            "dex": getattr(grid_mod, "PERP_DEX", "xyz"),
            "count": len(tokens),
            "tokens": tokens,
        }
    except Exception:
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
        return {"dex": "multi", "count": len(fallback), "tokens": [
            {"key": key, "symbol": symbol, "logo": _token_logo_text(symbol.split(":")[-1])}
            for key, symbol in fallback.items()
        ]}


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
            content={"status": "error", "message": str(e)},
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
            content={"status": "error", "message": str(e)},
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
        manager.stop_grid()
        time.sleep(1) # Ensure connection closed properly
        manager.start_grid(req)
        return {"status": "ok", "message": "Grid bot restarted"}
    except Exception as e:
        return JSONResponse(
            status_code=400,
            content={"status": "error", "message": str(e)},
        )


@app.get("/api/grid/status")
async def grid_status(_: None = Depends(_require_api_token)):
    return manager.get_grid_stats()


# ---------------------------------------------------------------------------
# REST API — WorldCup Bot
# ---------------------------------------------------------------------------
@app.post("/api/worldcup/start")
async def start_worldcup(req: WorldcupStartRequest, _: None = Depends(_require_api_token)):
    try:
        manager.start_worldcup(req)
        return {"status": "ok", "message": "WorldCup bot started"}
    except Exception as e:
        return JSONResponse(
            status_code=400,
            content={"status": "error", "message": str(e)},
        )


@app.post("/api/worldcup/stop")
async def stop_worldcup(_: None = Depends(_require_api_token)):
    try:
        manager.stop_worldcup()
        return {"status": "ok", "message": "WorldCup bot stopped"}
    except Exception as e:
        return JSONResponse(
            status_code=400,
            content={"status": "error", "message": str(e)},
        )


@app.get("/api/worldcup/status")
async def worldcup_status(_: None = Depends(_require_api_token)):
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
