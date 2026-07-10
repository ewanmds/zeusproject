"""
Trading Bot Dashboard — FastAPI Backend Server
================================================
Wraps two trading bots (Grid Adaptive & WorldCup Spread) and exposes
REST API + WebSocket endpoints for a frontend dashboard.
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
from pathlib import Path
from typing import Optional, Dict, Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("bot_server")
GRID_NUM_LEVELS = 1

# ---------------------------------------------------------------------------
# .env loader (manual, no external dependency)
# ---------------------------------------------------------------------------
def load_dotenv(path: Path) -> None:
    """Read a .env file and populate os.environ (does not overwrite)."""
    if not path.exists():
        logger.warning(".env file not found at %s", path)
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
load_dotenv(Path(__file__).parent / ".env")

# ---------------------------------------------------------------------------
# Pydantic request models
# ---------------------------------------------------------------------------
class GridStartRequest(BaseModel):
    """Formulaire minimal: le marché et le notional par ordre. La stratégie
    (spread 5 ticks du mid, reset 2 bps, cap 1 ordre) vit dans le bot."""
    token: str = "SP500"
    order_size_usd: float = 100
    # Champ legacy tolere: le Grid reste cape a 1 ordre max.
    max_position_usd: Optional[float] = None
    # Champs legacy tolérés (vieux onglets) — ignorés.
    spread_ticks: Optional[float] = None
    offset_ticks: Optional[float] = None
    spread_bps: Optional[float] = None
    offset_bps: Optional[float] = None
    grid_reset_bps: Optional[float] = None
    num_levels: int = 1


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
            logger.info("Forcing reload of module: %s", module_name)
            return importlib.reload(sys.modules[module_name])
        return __import__(module_name)
    except Exception as exc:
        logger.error("Failed to import %s: %s", module_name, exc)
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
        # Grid Adaptive bot state
        self.grid_bot = None          # GridMarketMaker instance
        self.grid_thread: Optional[threading.Thread] = None
        self.grid_running: bool = False
        self.grid_config: Dict[str, Any] = {}

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
    # Grid Adaptive Bot
    # -----------------------------------------------------------------------
    def start_grid(self, config: GridStartRequest) -> None:
        with self.lock:
            if self.grid_running:
                raise RuntimeError("Grid bot is already running")

            grid_mod = _safe_import("MM_GRID_ADAPTIVE_version_originale")
            if grid_mod is None:
                raise RuntimeError(
                    "Cannot import MM_GRID_ADAPTIVE_version_originale — "
                    "make sure the file is in the same directory"
                )

            # Order sizing influences dynamic token size derivation.
            grid_mod.TARGET_ORDER_NOTIONAL_USD = max(12.0, config.order_size_usd)

            # Look up token configuration. If the token is not in the local
            # presets, refresh the live HIP-3 catalog and merge it into TOKENS.
            token_key = _grid_token_key(config.token)
            if token_key not in grid_mod.TOKENS:
                try:
                    _refresh_grid_token_catalog(grid_mod)
                except Exception:
                    logger.exception("Failed to refresh HIP-3 token catalog")
            if token_key not in grid_mod.TOKENS:
                raise ValueError(
                    f"Unknown token '{config.token}'. "
                    f"Available: {list(grid_mod.TOKENS.keys())}"
                )
            token_cfg = grid_mod.TOKENS[token_key]

            # Resolve current mid price for sizing
            mid = float(token_cfg.get("api_mid", 0.0))
            if mid <= 0:
                try:
                    mids = grid_mod._fetch_all_mids()
                    mid = float(mids.get(token_cfg["symbol"], 0.0))
                except Exception:
                    pass
            if mid <= 0:
                mid = 7500.0 if "SP500" in token_cfg["symbol"] or "US500" in token_cfg["symbol"] else 1.0

            # Calculate ORDER_SIZE dynamically based on order_size_usd
            sz_decimals = token_cfg.get("sz_decimals")
            if sz_decimals is None:
                min_size = token_cfg.get("min_size", 0.001)
                sz_decimals = max(0, -math.floor(math.log10(min_size)))
            else:
                min_size = 10.0 ** (-sz_decimals) if sz_decimals > 0 else 1.0

            raw_size = config.order_size_usd / mid
            order_size = max(min_size, round(raw_size, sz_decimals))

            # Set module-level globals
            grid_mod.SYMBOL = token_cfg["symbol"]
            grid_mod.TICK_SIZE = token_cfg["tick_size"]
            grid_mod.MIN_SIZE = min_size
            grid_mod.ORDER_SIZE = order_size

            # La stratégie N'EST PAS pilotée par l'UI: la version originale
            # quote à FIXED_HALF_SPREAD_TICKS (5 ticks) du mid avec son reset
            # 2 bps — mêmes réglages qu'un lancement console.
            grid_mod.NUM_LEVELS = GRID_NUM_LEVELS

            # IS_CROSS / LEVERAGE from token config if available
            grid_mod.IS_CROSS = token_cfg.get("is_cross", True)
            grid_mod.LEVERAGE = max(1, min(50, int(token_cfg.get("max_leverage", 50) or 50)))

            # Derived precision
            grid_mod.PRICE_DECIMALS = max(
                0, -math.floor(math.log10(grid_mod.TICK_SIZE))
            ) if grid_mod.TICK_SIZE < 1 else 0
            grid_mod.SIZE_DECIMALS = max(
                0, -math.floor(math.log10(grid_mod.MIN_SIZE))
            ) if grid_mod.MIN_SIZE < 1 else 0

            # Cap d'inventaire force: position max autorisee = 1 ordre.
            grid_mod.MAX_INVENTORY_ORDERS = 1

            self.grid_config = config.model_dump()
            self.grid_config["num_levels"] = GRID_NUM_LEVELS

            def run_grid():
                try:
                    logger.info("Grid bot thread starting for %s", token_key)
                    self.grid_bot = grid_mod.GridMarketMaker()
                    self.grid_bot.run()
                except Exception:
                    logger.exception("Grid bot crashed")
                finally:
                    self.grid_running = False
                    logger.info("Grid bot thread exited")

            self.grid_thread = threading.Thread(
                target=run_grid, name="grid-bot", daemon=True
            )
            self.grid_thread.start()
            self.grid_running = True
            logger.info("Grid bot started: token=%s", token_key)

    def stop_grid(self) -> None:
        with self.lock:
            if not self.grid_running or self.grid_bot is None:
                logger.info("Grid bot is not running, nothing to stop")
                return
            logger.info("Stopping Grid bot …")
            self.grid_bot.running = False

        # Join outside the lock to avoid blocking other ops
        if self.grid_thread is not None:
            self.grid_thread.join(timeout=10)
        self.grid_running = False
        logger.info("Grid bot stopped")

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
        if not self.grid_running or self.grid_bot is None:
            return default

        bot = self.grid_bot
        try:
            with bot.lock:
                elapsed = time.time() - bot.start_time
                duration = _fmt_duration(elapsed)

                net_pnl = bot.session_pnl - bot.total_fees
                volume = bot.total_volume
                cost_per_1m = (
                    (bot.total_fees - bot.session_pnl) / (volume / 1_000_000)
                    if volume > 0
                    else 0.0
                )
                spread_bps = (
                    (bot.cur_half_spread * 2 / bot.mid * 10_000)
                    if bot.mid > 0
                    else 0.0
                )
                position_usd = bot.position * bot.mid if bot.mid > 0 else 0.0

                result = {
                    "type": "grid_stats",
                    "running": True,
                    "duration": duration,
                    "volume_usd": _json_float(volume, 2),
                    "net_pnl": _json_float(net_pnl, 4),
                    "fees": _json_float(bot.total_fees, 4),
                    "cost_per_1m": _json_float(cost_per_1m, 2),
                    "fills": int(bot.fills_count),
                    "mid_price": _json_float(bot.mid, 4),
                    "spread_bps": _json_float(spread_bps, 2),
                    "position_usd": _json_float(position_usd, 2),
                    "net_passive_capture": _json_float(getattr(bot, 'net_passive_capture', 0.0), 4),
                    "net_adverse_lost": _json_float(getattr(bot, 'net_adverse_lost', 0.0), 4),
                    "adverse_halt": getattr(bot, 'adverse_halt', False),
                    "config": self.grid_config,
                }
                self._grid_stats_cache = result
                return result
        except Exception:
            logger.exception("Error reading Grid stats")
            return self._grid_stats_cache or default

    # -----------------------------------------------------------------------
    # WorldCup Spread Bot
    # -----------------------------------------------------------------------
    def start_worldcup(self, config: WorldcupStartRequest) -> None:
        with self.lock:
            if self.wc_running:
                raise RuntimeError("WorldCup bot is already running")

            wc_mod = _safe_import("hyperliquid_worldcup_spread_bot")
            if wc_mod is None:
                raise RuntimeError(
                    "Cannot import hyperliquid_worldcup_spread_bot — "
                    "make sure the file is in the same directory"
                )

            try:
                from hyperliquid.utils import constants
                base_url = constants.MAINNET_API_URL
            except ImportError:
                logger.warning(
                    "hyperliquid SDK not found, using default mainnet URL"
                )
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
                    logger.info("WorldCup bot thread starting")
                    self.wc_bot = wc_mod.HyperliquidSpreadBot(cfg)
                    self.wc_bot.run()
                except Exception:
                    logger.exception("WorldCup bot crashed")
                finally:
                    self.wc_running = False
                    logger.info("WorldCup bot thread exited")

            self.wc_thread = threading.Thread(
                target=run_wc, name="worldcup-bot", daemon=True
            )
            self.wc_thread.start()
            self.wc_running = True
            logger.info("WorldCup bot started")

    def stop_worldcup(self) -> None:
        with self.lock:
            if not self.wc_running or self.wc_bot is None:
                logger.info("WorldCup bot is not running, nothing to stop")
                return
            logger.info("Stopping WorldCup bot …")

        # shutdown() sets running=False and cancels open orders
        try:
            self.wc_bot.shutdown()
        except Exception:
            logger.exception("Error during WorldCup shutdown")

        if self.wc_thread is not None:
            self.wc_thread.join(timeout=10)
        self.wc_running = False
        logger.info("WorldCup bot stopped")

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
            logger.exception("Error reading WorldCup stats")
            return self._wc_stats_cache or default


# ---------------------------------------------------------------------------
# FastAPI Application
# ---------------------------------------------------------------------------
app = FastAPI(title="Trading Bot Dashboard")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

manager = BotManager()

# ---------------------------------------------------------------------------
# Frontend serving
# ---------------------------------------------------------------------------
frontend_dir = Path(__file__).parent / "frontend"


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
@app.get("/api/grid/tokens")
async def get_grid_tokens():
    """Return available HIP-3 token metadata for the Grid bot picker."""
    try:
        import MM_GRID_ADAPTIVE_version_originale as grid_mod
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
        logger.exception("Failed to load HIP-3 token catalog")
        # Fallback list when the module isn't available
        fallback = [
            "SP500", "US500", "XYZ100", "SKHX", "SPCX",
            "WTIOIL", "SILVER", "MU", "BRENTOIL", "DRAM",
            "INTC", "EURUSD", "JPYUSD", "MINIMAX", "CRCL", "NBIS",
        ]
        return {"dex": "xyz", "count": len(fallback), "tokens": [
            {"key": key, "symbol": f"xyz:{key}", "logo": _token_logo_text(key)}
            for key in fallback
        ]}


@app.post("/api/grid/start")
async def start_grid(req: GridStartRequest):
    try:
        manager.start_grid(req)
        return {"status": "ok", "message": f"Grid bot started on {req.token}"}
    except Exception as e:
        logger.exception("Failed to start Grid bot")
        return JSONResponse(
            status_code=400,
            content={"status": "error", "message": str(e)},
        )


@app.post("/api/grid/stop")
async def stop_grid():
    try:
        manager.stop_grid()
        return {"status": "ok", "message": "Grid bot stopped"}
    except Exception as e:
        logger.exception("Failed to stop Grid bot")
        return JSONResponse(
            status_code=400,
            content={"status": "error", "message": str(e)},
        )

@app.post("/api/grid/restart")
async def restart_grid():
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
        logger.exception("Failed to restart Grid bot")
        return JSONResponse(
            status_code=400,
            content={"status": "error", "message": str(e)},
        )


@app.get("/api/grid/status")
async def grid_status():
    return manager.get_grid_stats()


# ---------------------------------------------------------------------------
# REST API — WorldCup Bot
# ---------------------------------------------------------------------------
@app.post("/api/worldcup/start")
async def start_worldcup(req: WorldcupStartRequest):
    try:
        manager.start_worldcup(req)
        return {"status": "ok", "message": "WorldCup bot started"}
    except Exception as e:
        logger.exception("Failed to start WorldCup bot")
        return JSONResponse(
            status_code=400,
            content={"status": "error", "message": str(e)},
        )


@app.post("/api/worldcup/stop")
async def stop_worldcup():
    try:
        manager.stop_worldcup()
        return {"status": "ok", "message": "WorldCup bot stopped"}
    except Exception as e:
        logger.exception("Failed to stop WorldCup bot")
        return JSONResponse(
            status_code=400,
            content={"status": "error", "message": str(e)},
        )


@app.get("/api/worldcup/status")
async def worldcup_status():
    return manager.get_worldcup_stats()


# ---------------------------------------------------------------------------
# WebSocket — live stats push
# ---------------------------------------------------------------------------
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    manager.ws_clients.add(websocket)
    logger.info("WebSocket client connected (%d total)", len(manager.ws_clients))
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
        logger.info("WebSocket client disconnected")
    except Exception:
        logger.debug("WebSocket error", exc_info=True)
    finally:
        manager.ws_clients.discard(websocket)
        logger.info("WebSocket clients remaining: %d", len(manager.ws_clients))


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print()
    print("=" * 60)
    print("  * Trading Bot Dashboard *")
    print("  http://localhost:8080")
    print("=" * 60)
    print()

    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="info")
