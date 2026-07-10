"""
================================================================================
GRID MARKET MAKER - ADAPTIVE GRID EDITION  (Hyperliquid HIP-3)
================================================================================
A clean, from-scratch grid market maker that implements ONE idea from the
Tread.fi write-up "How to Match a Market-Making Bot to Market Conditions":

A grid market maker is SHORT VOLATILITY. It earns the spread when price
wiggles around a level. In this build, trend detection is active: trend or
breakout regimes cut opening quotes and force reduce-only protection.

So the whole bot is a layered defense, in order of priority:

1. REGIME FILTER (primary defense). Score the market each cycle:
    - Kaufman Efficiency Ratio: net move / total path. ~0 chop, ~1 trend.
    - Volume profile: a tight value area = compression (chop), a stretched
        one = expansion (trend).
    - Value-area breakout: price leaving VAH/VAL raises the trend score.
    GRID quotes only in chop. In trend/cooldown, flat means no quotes; with
    inventory, only reduce-only exits can rest.

2. INVENTORY LIMIT (backstop). Cap how one-sided the book may get. When net
    exposure hits the cap, stop quoting the side that makes it worse.

3. STOP LOSS / TAKE PROFIT (hard floor). Flatten the whole position if it is
    down stop_loss_pct or up take_profit_pct of margin.

4. ATR-ADAPTIVE SPREAD / RESET. Spread and reset distance widen when recent
    volatility rises, while keeping a hard fee floor.

5. Judge by NET-of-fees PnL, never volume. Volume is a cost, not a win.

Nothing else. No RSI/EMA/BB/VWAP/microstructure/markout. Just the article.
================================================================================
"""

import os
import sys
import time
import math
import signal
import threading
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
try:
    from zoneinfo import ZoneInfo          # stdlib 3.9+ (DST-aware Paris time)
except ImportError:
    ZoneInfo = None                        # fallback: naive local time
from collections import deque
from typing import Optional, List, Dict, Any, Tuple

import eth_account
from hyperliquid.info import Info
from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants

from rich.console import Console, Group
from rich.table import Table
from rich.live import Live
from rich.panel import Panel
from rich.layout import Layout
from rich.align import Align
from rich.text import Text
from rich import box

# =============================================================================
# CONFIGURATION
# =============================================================================

# --- Account / venue --- (credentials come from .env, never hardcoded)
from dotenv import load_dotenv
load_dotenv()
AGENT_PRIVATE_KEY = os.getenv("PRIVATE_KEY", "")
ACCOUNT_ADDRESS   = os.getenv("WALLET_ADDRESS", "")
if not AGENT_PRIVATE_KEY or not ACCOUNT_ADDRESS:
    sys.exit("❌ PRIVATE_KEY / WALLET_ADDRESS missing — set them in the .env file")
BASE_URL          = constants.MAINNET_API_URL

# --- Instrument --- (perps on the xyz HIP-3 dex)
PERP_DEX   = "xyz"                 # HIP-3 builder dex
LEVERAGE   = max(1, min(50, int(float(os.getenv("MM_LEVERAGE", "50")))))  # target leverage
IS_CROSS   = True                  # overridden for isolated-only instruments

# Per-token market params (picked at launch via keyboard). Tick/min come from
# the exchange meta; order_size is sized to ~$15 notional per rung so it clears
# HL's ~$10 min order value and stays under the inventory-order cap.
TOKENS = {
    "SP500": {
        "symbol": "xyz:SP500",
        "tick_size": 0.1,
        "min_size": 0.001,
        "order_size": 0.026,   # ~$195/ordre à SP500 ~7500 (doublé depuis 0.013)
    },
    "US500": {
        # Alias trading pour le contrat US500USD sur mkts dex
        "symbol": "mkts:US500",
        "tick_size": 0.01,
        "min_size": 0.001,
        "order_size": 0.02,
        "max_leverage": 25,
    },
    "US500USD": {
        "symbol": "mkts:US500",
        "tick_size": 0.01,
        "min_size": 0.001,
        "order_size": 0.02,
    },
    "XYZ100": {
        "symbol": "xyz:XYZ100",
        "tick_size": 1.0,
        "min_size": 0.0001,
        "order_size": 0.008,
    },
    "SPCX": {
        "symbol": "xyz:SPCX",
        "tick_size": 0.01,
        "min_size": 0.01,
        "order_size": 1.0,
    },
    "SKHX": {
        "symbol": "xyz:SKHX",
        "tick_size": 0.1,
        "min_size": 0.001,
        "order_size": 0.4,
    },
    "WTIOIL": {
        "symbol": "xyz:CL",
        "tick_size": 0.001,
        "min_size": 0.01,
        "order_size": 0.25,
    },
    "SILVER": {
        "symbol": "xyz:SILVER",
        "tick_size": 0.001,
        "min_size": 0.01,
        "order_size": 0.25,
    },
    "MU": {
        "symbol": "xyz:MU",
        "tick_size": 0.1,
        "min_size": 0.001,
        "order_size": 0.15,
    },
    "BRENTOIL": {
        "symbol": "xyz:BRENTOIL",
        "tick_size": 0.001,
        "min_size": 0.01,
        "order_size": 0.2,
    },
    "DRAM": {
        "symbol": "xyz:DRAM",
        "tick_size": 0.001,
        "min_size": 0.01,
        "order_size": 0.2,
    },
    "INTC": {
        "symbol": "xyz:INTC",
        "tick_size": 0.01,
        "min_size": 0.01,
        "order_size": 0.15,
    },
    "EURUSD": {
        # Hyperliquid's UI labels this pair EURUSD, while the HIP-3 API/SDK
        # identifies the same contract as xyz:EUR.
        "symbol": "xyz:EUR",
        "tick_size": 0.0001,
        "min_size": 0.1,
        "order_size": 600.0,
        "is_cross": False,  # xyz:EUR is flagged onlyIsolated by the venue
    },
    "JPYUSD": {
        "symbol": "xyz:JPY",
        "tick_size": 0.01,
        "min_size": 0.01,
        "order_size": 6.0,
    },
    "MINIMAX": {
        "symbol": "xyz:MINIMAX",
        "tick_size": 0.01,
        "min_size": 0.01,
        "order_size": 1.5,
    },
    "CRCL": {
        "symbol": "xyz:CRCL",
        "tick_size": 0.01,
        "min_size": 0.01,
        "order_size": 1.5,
    },
    "NBIS": {
        "symbol": "xyz:NBIS",
        "tick_size": 0.01,
        "min_size": 0.01,
        "order_size": 1.5,
    },
    
}

# Defaults (SP500) — overridden in __main__ by the launch prompt.
SYMBOL     = TOKENS["SP500"]["symbol"]   # coin name used for orders / subscriptions
TICK_SIZE  = TOKENS["SP500"]["tick_size"]  # price tick
MIN_SIZE   = TOKENS["SP500"]["min_size"]   # exchange min size

# --- Grid (the strategy bet: price wiggles around a level) ---
# MICRO-ACCOUNT calibration (~$10 equity). SP500 min lot 0.001 ≈ $7.3 notional,
# which is below HL's ~$10 min order value, so we quote 0.002 ≈ $14.6 per rung.
NUM_LEVELS        = 1            # rungs per side
ORDER_SIZE        = 0.010        # size per rung (contracts) — ~$14.6, clears the $10 min
BASE_SPREAD_BPS   = 00  # classic fixed half-spread per side, in bps (widened for positive edge)
BBO_OFFSET_BPS    = 0.0  # frontend mode: quote this many bps away from the live BBO
LEVEL_STEP_BPS    = 1.0             # extra spacing between rungs, in bps (only used if NUM_LEVELS>1)
GRID_RESET_BPS    = 2.0          # minimum cancel/replace distance from anchor (wider = less churn)
MAX_GRID_RESET_BPS = 2.0         # volatility-scaled reset is capped here
ATR_RESET_MULT    = 0.45         # reset distance = max(GRID_RESET_BPS, ATR * this)
GRID_QUOTE_MODE = os.getenv("MM_GRID_QUOTE_MODE", "bbo").strip().lower()
if GRID_QUOTE_MODE not in {"adaptive", "safe_bbo", "bbo", "bbo_offset", "book_center"}:
    GRID_QUOTE_MODE = "safe_bbo"
JOIN_BOOK_SPREAD_WHEN_SAFE = GRID_QUOTE_MODE == "safe_bbo"  # join BBO only if spread covers edge

# Demi-spread FIXE en ticks depuis le mid (demande utilisateur SP500: 7 ticks).
# > 0 => remplace le calcul floor/ATR/regime ET désactive le join BBO: les
# ordres restent à N ticks du centre (mid - skew d'inventaire), clampés pour ne
# jamais croiser. 7 ticks à SP500 ~7500 = 0.93 bps par côté, > frais AR 0.576.
# 0 => comportement d'origine. Override: env MM_HALF_TICKS.
FIXED_HALF_SPREAD_TICKS = float(os.getenv("MM_HALF_TICKS", "7"))

# --- Fees (for the hard spread floor) ---
MAKER_FEE_BPS     = 0.28         # maker fee per side, in bps (28.8 $/M)
ADVERSE_BUFFER_BPS = 0.35  # extra cushion for adverse selection (covers fee + slippage)
MIN_ORDER_NOTIONAL_USD = 10.0    # Hyperliquid minimum order value
TARGET_ORDER_NOTIONAL_USD = max(
    MIN_ORDER_NOTIONAL_USD * 1.2,
    float(os.getenv("MM_ORDER_NOTIONAL_USD", "15.0")),
)  # auto-sized HIP-3 orders target this notional per rung
MARGIN_SAFETY_FRACTION = 0.85    # never budget the final 15% of theoretical capacity

# --- Inventory limit (backstop) ---
MAX_INVENTORY_ORDERS = 1          # max signed position = 1 x ORDER_SIZE

# --- Inventory skew (Avellaneda-Stoikov lite) ---
INVENTORY_SKEW_BPS = 0.3          # bps of center displacement per unit of normalised inventory

# --- Stop loss / take profit (hard floor, % of margin / ROE) ---
STOP_LOSS_ROE_PCT   = 10.0        # flatten if position down this % of margin (tight for micro acct)
TAKE_PROFIT_ROE_PCT = 15.0        # flatten if position up   this % of margin
RISK_PAUSE_SEC      = 20.0        # after a SL/TP flatten, pause this long

# --- Active trend exit ---
# If a confirmed trend runs against current inventory, stop waiting for a
# passive reduce-only order and flatten immediately.
TREND_EXIT_ENABLED  = True

# --- Spread sizing via ATR ---
# NOTE: ATR is measured on 1m candles (~6-7 pts), so keep this small or the
# spread blows out. 0.1 keeps a touch of volatility scaling; raise it only if
# you want much wider quotes in fast markets.
ATR_SPREAD_MULT   = 0.25     # half-spread floor = ATR * this (price units, raised for vol protection)

# --- Regime engine ---
# 1-minute candles: history is pre-loaded from the API, then the live forming
# candle is included in every score update. We therefore keep the stable 1m
# context without waiting for the next minute close to detect a dangerous move.
BAR_SECONDS         = 60
CANDLE_INTERVAL     = "1m"        # HL candleSnapshot interval used to seed history
ER_PERIOD           = 20
VP_BARS             = 90
VP_BINS             = 24
VALUE_AREA_PCT      = 0.70
ATR_PERIOD          = 14
W_ER                = 0.45
W_VP                = 0.35
W_BREAKOUT          = 0.20
TREND_FILTER_ENABLED = False     # temporary: do not pause opening quotes in trend/breakout
ENTER_TREND_SCORE   = 0.45        # enter TREND defense earlier (was 0.52)
EXIT_TREND_SCORE    = 0.30        # exit TREND only when clearly chop (was 0.35)
VALUE_BREAK_BUFFER_ATR = 0.10    # VAH/VAL breakout buffer before raising trend score (tighter)
TREND_COOLDOWN_SEC  = 90.0       # no immediate re-entry after a trend/breakout clears (was 60)
VP_COMPRESS_LO      = 0.35        # value-area/range ratio mapped to chop
VP_COMPRESS_HI      = 0.80        # ...to expansion
MIN_BARS            = 30          # bars needed before the filter is live (pre-seeded at start)
MARKET_SCAN_ENABLED = os.getenv("MM_MARKET_SCAN", "1").lower() not in ("0", "false", "no", "off")
MARKET_SCAN_TOP_N   = max(5, int(float(os.getenv("MM_MARKET_SCAN_TOP_N", "20"))))
MARKET_SCAN_MAX_MARKETS = max(0, int(float(os.getenv("MM_MARKET_SCAN_MAX_MARKETS", "0"))))  # 0 = all
MARKET_SCAN_WORKERS = max(1, min(16, int(float(os.getenv("MM_MARKET_SCAN_WORKERS", "8")))))
MARKET_SCAN_MIN_VOLUME_USD = max(0.0, float(os.getenv("MM_MARKET_SCAN_MIN_VOLUME_USD", "100000")))

# --- Fenetre flat forcee (heure de Paris) ---
# Pendant cette fenetre on interdit toute exposition active: pas de quotes et
# aucune position ouverte. Si une position existe, le bot annule les ordres puis
# envoie un IOC reduce-only pour revenir flat.
BLACKOUT_ENABLED = True
BLACKOUT_TZ      = "Europe/Paris"
BLACKOUT_START_H = 15
BLACKOUT_START_M = 25
BLACKOUT_END_H   = 16
BLACKOUT_END_M   = 45
BLACKOUT_DAYS    = (0, 1, 2, 3, 4)  # lundi(0) -> vendredi(4)
BLACKOUT_FLATTEN_RETRY_SEC = 2.0
# Construit l'objet ZoneInfo UNE seule fois (le lookup de la base tz est coûteux
# et _in_blackout() tourne dans la hot loop + à chaque refresh dashboard).
_BLACKOUT_ZI = None
if ZoneInfo is not None:
    try:
        _BLACKOUT_ZI = ZoneInfo(BLACKOUT_TZ)
    except Exception:
        _BLACKOUT_ZI = None            # tz indispo -> heure locale machine

# --- Loop cadence ---
# EVENT-DRIVEN: the cycle is woken instantly by every WS update (book tick or
# fill) instead of sleeping a fixed interval. LOOP_INTERVAL_SEC is only the
# fallback timeout when the feed is quiet; MIN_CYCLE_SEC throttles how fast
# back-to-back cycles may run (protects API rate limits on busy books).
LOOP_INTERVAL_SEC   = 0.03        # WS remains instant; heartbeat need not hit REST
MIN_CYCLE_SEC       = 0.005       # local CPU guard, no API action by itself
POSITION_SYNC_SEC   = 2.0         # full position/collateral REST refresh
ORDER_SYNC_SEC      = 2.0         # WS is primary; REST is only a safety fallback
ORDER_RECONCILE_GRACE_SEC = 0.35  # brief REST eventual-consistency guard
REGIME_UPDATE_SEC   = 0.10        # local only; no REST call
MIN_QUOTE_ACTION_SEC = 0.50       # signed order-action coalescer (raised to reduce churn)
PARTIAL_RETRY_SEC   = 2.0         # retry only missing/rejected legs; keep resting legs in book

# ALO safety. A passive quote can cross while it is in flight to the matching
# engine. Re-check the latest WS BBO immediately before sending, leave a small
# cushion, then retry only the rejected legs at a progressively safer price.
POST_ONLY_GUARD_TICKS = 1
POST_ONLY_MAX_RETRIES = 1         # one immediate safer retry on ALO reject
POST_ONLY_RETRY_SEC   = 0.0       # refresh BBO and retry immediately
# --- Feed watchdog ---
# A silent WS death freezes the mid and orphans the resting orders (fill
# events stop arriving -> phantom oids, sticky quotes never rebuild). If the
# book hasn't ticked for WS_STALE_SEC, verify it through REST before deciding
# that the websocket is dead.  Quiet books (notably JPY outside its active
# session) can legitimately publish no L2 update for much longer than 15s.
WS_STALE_SEC        = 15.0        # silence threshold before a REST health probe
WS_HARD_STALE_SEC   = 180.0       # pull quotes only if WS *and* REST stay unavailable
WS_RESTART_COOLDOWN = 30.0        # min spacing between two reconnect attempts

# Derived
PRICE_DECIMALS = max(0, -int(math.floor(math.log10(TICK_SIZE)))) if TICK_SIZE < 1 else 0
SIZE_DECIMALS  = max(0, -int(math.floor(math.log10(MIN_SIZE)))) if MIN_SIZE < 1 else 0

_INSTANCE_LOCK_HANDLE = None


def acquire_instance_lock(symbol: str) -> bool:
    """Allow only one bot process per instrument; the OS releases on exit."""
    global _INSTANCE_LOCK_HANDLE
    safe_symbol = "".join(c if c.isalnum() else "_" for c in symbol)
    path = os.path.join(tempfile.gettempdir(), f"hl_grid_mm_{safe_symbol}.lock")
    handle = open(path, "a+b")
    try:
        if os.path.getsize(path) == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()).encode("ascii"))
        handle.flush()
        _INSTANCE_LOCK_HANDLE = handle
        return True
    except (OSError, IOError):
        handle.close()
        return False


# =============================================================================
# SDK WORKAROUND: corrected spot meta
# =============================================================================
def build_fixed_spot_meta(base_url: str = BASE_URL) -> Optional[Dict[str, Any]]:
    """
    hyperliquid-python-sdk indexes spot_meta["tokens"] positionally, but the API
    now returns a tokens list whose positions no longer match token `index` ids
    (delisted tokens leave gaps), which makes Info()/Exchange() raise IndexError.
    Rebuild tokens as a dense list (position == index) so construction succeeds.
    """
    try:
        import requests
        raw = requests.post(base_url + "/info", json={"type": "spotMeta"}, timeout=10).json()
        tokens = raw.get("tokens", [])
        if not tokens:
            return None
        max_idx = max(int(t["index"]) for t in tokens)
        fixed: List[Any] = [None] * (max_idx + 1)
        for t in tokens:
            fixed[int(t["index"])] = t
        for i in range(len(fixed)):
            if fixed[i] is None:
                fixed[i] = {"name": f"_gap{i}", "szDecimals": 0, "weiDecimals": 0, "index": i}
        raw["tokens"] = fixed
        return raw
    except Exception as exc:
        print(f"⚠️ build_fixed_spot_meta failed ({exc}); using SDK default")
        return None


# =============================================================================
# CANDLES: aggregate mid-price ticks into OHLCV bars (volume = tick activity)
# =============================================================================
class CandleBuilder:
    def __init__(self, bar_seconds: int, max_bars: int):
        self.bar_seconds = max(1, int(bar_seconds))
        self.bars: deque = deque(maxlen=max_bars)
        self._cur: Optional[Dict[str, float]] = None
        self._cur_bucket: Optional[int] = None
        self.lock = threading.Lock()

    def on_tick(self, mid: float, ts: Optional[float] = None) -> None:
        if mid <= 0:
            return
        ts = ts if ts is not None else time.time()
        bucket = int(ts - (ts % self.bar_seconds))
        with self.lock:
            if self._cur is None or bucket != self._cur_bucket:
                if self._cur is not None:
                    self.bars.append(self._cur)
                self._cur = {"ts": float(bucket), "o": mid, "h": mid, "l": mid, "c": mid, "v": 0.0}
                self._cur_bucket = bucket
            else:
                self._cur["h"] = max(self._cur["h"], mid)
                self._cur["l"] = min(self._cur["l"], mid)
                self._cur["c"] = mid

    def on_trade(self, sz: float, ts: Optional[float] = None) -> None:
        """Accumulate REAL traded volume into the current bar — same base-asset
        units as the API candles used to seed history, so the volume profile
        never mixes tick counts with actual volume."""
        if sz <= 0:
            return
        ts = ts if ts is not None else time.time()
        bucket = int(ts - (ts % self.bar_seconds))
        with self.lock:
            if self._cur is not None and bucket == self._cur_bucket:
                self._cur["v"] += sz

    def seed(self, bars: List[Dict[str, float]]) -> None:
        """Pre-load history and preserve the API's current forming candle.

        ``candleSnapshot`` may include the still-open minute. Keeping it in
        ``bars`` would freeze that minute until it closes and later duplicate
        it when the WS builder rolls over. Store it as ``_cur`` instead so live
        ticks extend it immediately.
        """
        now = time.time()
        current_bucket = int(now - (now % self.bar_seconds))
        with self.lock:
            for b in bars:
                bar = dict(b)
                bucket = int(float(bar.get("ts", 0.0)))
                if bucket == current_bucket:
                    self._cur = bar
                    self._cur_bucket = current_bucket
                else:
                    self.bars.append(bar)

    def closed_bars(self) -> List[Dict[str, float]]:
        with self.lock:
            return [dict(b) for b in self.bars]

    def bars_for_regime(self) -> List[Dict[str, float]]:
        """Atomic history snapshot including the live, still-forming bar."""
        with self.lock:
            result = [dict(b) for b in self.bars]
            if self._cur is not None:
                current = dict(self._cur)
                if result and int(result[-1].get("ts", -1)) == int(current.get("ts", -2)):
                    result[-1] = current
                else:
                    result.append(current)
            return result


# =============================================================================
# REGIME ENGINE: efficiency ratio + volume profile -> chop / trend (hysteresis)
# =============================================================================
class RegimeEngine:
    def __init__(self, tick_size: Optional[float] = None):
        self.tick_size = tick_size
        self.ready = False
        self.is_trend = False
        self.trend_score = 0.0
        self.efficiency_ratio = 0.0
        self.vp_expansion = 0.0
        self.breakout_score = 0.0
        self.trend_dir = 0
        self.breakout_dir = 0
        self.atr = 0.0
        self.poc: Optional[float] = None
        self.vah: Optional[float] = None
        self.val: Optional[float] = None

    @staticmethod
    def _clamp01(x: float) -> float:
        return 0.0 if x < 0.0 else (1.0 if x > 1.0 else x)

    def _tick_size(self) -> float:
        return self.tick_size if self.tick_size and self.tick_size > 0 else TICK_SIZE

    def _kaufman_er(self, closes: List[float]) -> Tuple[float, int]:
        n = ER_PERIOD
        if len(closes) < n + 1:
            return 0.0, 0
        w = closes[-(n + 1):]
        net = abs(w[-1] - w[0])
        path = sum(abs(w[i] - w[i - 1]) for i in range(1, len(w)))
        if path <= 1e-12:
            return 0.0, 0
        direction = 1 if w[-1] > w[0] else (-1 if w[-1] < w[0] else 0)
        return self._clamp01(net / path), direction

    def _compute_atr(self, bars: List[Dict[str, float]]) -> float:
        n = ATR_PERIOD
        if len(bars) < n + 1:
            return 0.0
        trs = []
        for i in range(len(bars) - n, len(bars)):
            if i <= 0:
                continue
            hi, lo, pc = bars[i]["h"], bars[i]["l"], bars[i - 1]["c"]
            trs.append(max(hi - lo, abs(hi - pc), abs(lo - pc)))
        return sum(trs) / len(trs) if trs else 0.0

    def _volume_profile(self, bars: List[Dict[str, float]]) -> None:
        sample = bars[-VP_BARS:]
        lo = min(b["l"] for b in sample)
        hi = max(b["h"] for b in sample)
        total_range = hi - lo
        total_w = sum(max(0.0, b.get("v", 1.0)) for b in sample)
        if total_range <= 0 or total_w <= 0:
            self.poc = self.vah = self.val = None
            self.vp_expansion = 0.0
            return
        bins = max(4, VP_BINS)
        bin_w = total_range / bins
        hist = [0.0] * bins
        for b in sample:
            typ = (b["h"] + b["l"] + b["c"]) / 3.0
            idx = int((typ - lo) / bin_w)
            idx = 0 if idx < 0 else (bins - 1 if idx >= bins else idx)
            hist[idx] += max(0.0, b.get("v", 1.0))
        poc_idx = max(range(bins), key=lambda i: hist[i])
        # Grow the value area around the POC until it holds VALUE_AREA_PCT.
        target = total_w * VALUE_AREA_PCT
        lo_idx = hi_idx = poc_idx
        acc = hist[poc_idx]
        while acc < target and (lo_idx > 0 or hi_idx < bins - 1):
            down = hist[lo_idx - 1] if lo_idx > 0 else -1.0
            up = hist[hi_idx + 1] if hi_idx < bins - 1 else -1.0
            if up >= down:
                hi_idx += 1
                acc += hist[hi_idx]
            else:
                lo_idx -= 1
                acc += hist[lo_idx]
        self.poc = lo + (poc_idx + 0.5) * bin_w
        self.val = lo + lo_idx * bin_w
        self.vah = lo + (hi_idx + 1) * bin_w
        ratio = (self.vah - self.val) / total_range          # narrow VA = chop, wide = trend
        span = max(1e-9, VP_COMPRESS_HI - VP_COMPRESS_LO)
        self.vp_expansion = self._clamp01((ratio - VP_COMPRESS_LO) / span)

    def _value_area_breakout(self, close: float) -> None:
        self.breakout_score = 0.0
        self.breakout_dir = 0
        if self.val is None or self.vah is None or close <= 0:
            return
        tick = self._tick_size()
        buffer = max(tick, self.atr * VALUE_BREAK_BUFFER_ATR) if self.atr > 0 else tick
        upper = self.vah + buffer
        lower = self.val - buffer
        if close > upper:
            self.breakout_dir = 1
            distance = close - upper
        elif close < lower:
            self.breakout_dir = -1
            distance = lower - close
        else:
            return
        scale = max(tick, self.atr, (self.vah - self.val) * 0.5)
        self.breakout_score = self._clamp01(distance / scale)

    def update(self, bars: List[Dict[str, float]]) -> None:
        if not bars or len(bars) < MIN_BARS:
            self.ready = False
            return
        closes = [b["c"] for b in bars]
        er, direction = self._kaufman_er(closes)
        self.efficiency_ratio = er
        self.atr = self._compute_atr(bars)
        self._volume_profile(bars)
        self._value_area_breakout(closes[-1])
        self.trend_dir = self.breakout_dir or direction
        wsum = max(1e-9, W_ER + W_VP + W_BREAKOUT)
        self.trend_score = (
            W_ER * er
            + W_VP * self.vp_expansion
            + W_BREAKOUT * self.breakout_score
        ) / wsum
        # Hysteresis state machine: enter trend defensively, resume only after
        # the score has clearly compressed back into chop.
        if not TREND_FILTER_ENABLED:
            self.is_trend = False
        elif self.is_trend:
            if self.trend_score < EXIT_TREND_SCORE:
                self.is_trend = False
        else:
            if self.trend_score > ENTER_TREND_SCORE:
                self.is_trend = True
        self.ready = True

    def atr_half_spread(self) -> float:
        return self.atr * ATR_SPREAD_MULT if (self.ready and self.atr > 0) else 0.0


# =============================================================================
# THE BOT
# =============================================================================
class GridMarketMaker:
    def __init__(self):
        self.lock = threading.Lock()
        self.wallet = eth_account.Account.from_key(AGENT_PRIVATE_KEY)
        self.dex = SYMBOL.split(":", 1)[0] if ":" in SYMBOL else PERP_DEX
        self.fixed_spot_meta = build_fixed_spot_meta(BASE_URL)
        self.info = Info(BASE_URL, skip_ws=True, spot_meta=self.fixed_spot_meta, perp_dexs=["xyz", "mkts"])
        self.exchange = Exchange(
            self.wallet, base_url=BASE_URL, account_address=ACCOUNT_ADDRESS,
            spot_meta=self.fixed_spot_meta, perp_dexs=["xyz", "mkts"],
        )

        # Live market state (from WS)
        self.best_bid = 0.0
        self.best_ask = 0.0
        self.mid = 0.0
        self.ws_ready = False
        self.last_tick_ts = 0.0    # time of the last book tick (feed watchdog)

        # Account state
        self.position = 0.0
        self.entry_price = 0.0
        self.margin_used = 0.0
        self.unrealized_pnl = 0.0
        self.equity = 0.0
        self.withdrawable = 0.0
        self.perp_equity = 0.0
        self.perp_withdrawable = 0.0
        self.spot_usdc = 0.0

        # Order tracking
        self.buy_oids: List[int] = []
        self.sell_oids: List[int] = []
        self.anchor_mid: Optional[float] = None    # mid at last grid build (reset threshold)
        self.last_intent: Optional[tuple] = None   # coarse quote "intent" (sticky quotes)

        # PnL / stats (judge by NET pnl)
        self.session_pnl = 0.0      # realized closedPnl
        self.total_fees = 0.0
        self.total_volume = 0.0
        self.fills_count = 0
        self.processed_fills: set = set()
        self.last_fill_ts = 0.0       # prevents a stale REST reply undoing a fill
        self.best_net_pnl = 0.0     # best NET (gross - fees) seen this session
        self.cur_half_spread = 0.0  # last half-spread used (price units), for the dashboard

        # Engine
        self.candles = CandleBuilder(BAR_SECONDS, VP_BARS + ATR_PERIOD + 10)
        self.regime = RegimeEngine()

        # Control
        self.running = True
        self.risk_pause_until = 0.0
        self.regime_pause_until = 0.0
        self.margin_reject_pause_until = 0.0
        self.loop_ms = 0.0          # last cycle duration (reactivity gauge)
        self.order_action_ms = 0.0  # last signed order action duration
        self.wake = threading.Event()  # set by WS callbacks -> instant cycle
        self.last_ws_restart = 0.0     # last WS rebuild (watchdog cooldown)
        self.last_quote_ts = 0.0       # last order action (guards REST oid reconciliation)
        self.last_quote_action_ts = 0.0  # action-rate coalescer (place/modify only)
        self.last_partial_retry_ts = 0.0  # avoid hammering a repeatedly rejected missing leg
        self.last_regime_update_ts = 0.0
        self.blackout_was_active = False
        self.last_blackout_flatten_ts = 0.0
        self.last_action = "init"
        self.last_order_error = ""
        self.last_order_error_ts = 0.0
        self.start_time = time.time()
        self.console = Console()

        try:
            signal.signal(signal.SIGINT, self._shutdown)
            signal.signal(signal.SIGTERM, self._shutdown)
        except ValueError:
            pass  # signal handlers only work in the main thread (e.g. dashboard server)

        self._set_leverage()
        self.seed_history()
        # Load authoritative exposure/orders before the first quote. Otherwise
        # startup can briefly assume position=0 and exceed the inventory cap.
        self.sync_position()
        self.sync_orders()
        self._start_ws()

    # ---------------------------------------------------------------- helpers
    def _shutdown(self, *_):
        self.running = False

    def seed_history(self):
        """Pre-load recent candles so the regime filter is live from the start."""
        try:
            import requests
            need_min = (VP_BARS + ATR_PERIOD + 5)
            now_ms = int(time.time() * 1000)
            start_ms = now_ms - need_min * 60_000 - 60_000
            raw = requests.post(
                BASE_URL + "/info",
                json={"type": "candleSnapshot", "req": {
                    "coin": SYMBOL, "interval": CANDLE_INTERVAL,
                    "startTime": start_ms, "endTime": now_ms}},
                timeout=10,
            ).json()
            bars = [{
                "ts": float(c["t"]) / 1000.0,
                "o": float(c["o"]), "h": float(c["h"]),
                "l": float(c["l"]), "c": float(c["c"]),
                "v": float(c.get("v", 1.0)),
            } for c in raw]
            if bars:
                self.candles.seed(bars)
                self.regime.update(self.candles.bars_for_regime())
                print(f"✅ Seeded {len(bars)} history candles — regime ready: {self.regime.ready}")
            else:
                print("⚠️ No history candles returned; regime will warm up live (~30 min).")
        except Exception as exc:
            print(f"⚠️ seed_history failed ({exc}); regime will warm up live.")

    def _set_leverage(self):
        try:
            self.exchange.update_leverage(LEVERAGE, SYMBOL, is_cross=IS_CROSS)
        except Exception as exc:
            print(f"⚠️ update_leverage failed: {exc}")

    def _round_px_down(self, px: float) -> float:
        ratio = round(px / TICK_SIZE, 7)
        return round(math.floor(ratio) * TICK_SIZE, PRICE_DECIMALS)

    def _round_px_up(self, px: float) -> float:
        ratio = round(px / TICK_SIZE, 7)
        return round(math.ceil(ratio) * TICK_SIZE, PRICE_DECIMALS)

    def _round_px_nearest(self, px: float) -> float:
        """Nearest valid tick, used by the symmetric hard-spread mode."""
        ratio = round(px / TICK_SIZE, 7)
        return round(math.floor(ratio + 0.5) * TICK_SIZE, PRICE_DECIMALS)

    @staticmethod
    def _is_post_only_match_error(value: Any) -> bool:
        text = str(value).lower().replace("-", " ")
        return ("post only" in text and
                ("match" in text or "cross" in text or "maker" in text))

    def _safe_post_only_price(self, px: float, is_buy: bool,
                            extra_guard_ticks: int = 0) -> float:
        """Clamp an ALO price to the freshest opposing BBO, on a valid tick."""
        with self.lock:
            best_bid, best_ask = self.best_bid, self.best_ask
        guard = max(1, POST_ONLY_GUARD_TICKS + extra_guard_ticks) * TICK_SIZE
        if is_buy:
            if best_ask > 0:
                px = min(px, best_ask - guard)
            return max(TICK_SIZE, self._round_px_down(px))
        if best_bid > 0:
            px = max(px, best_bid + guard)
        return max(TICK_SIZE, self._round_px_up(px))

    def _claim_quote_action_slot(self, now: float) -> bool:
        """Latest-BBO-wins coalescer for signed place/modify actions."""
        with self.lock:
            if now - self.last_quote_action_ts < MIN_QUOTE_ACTION_SEC:
                return False
            self.last_quote_action_ts = now
            return True

    def _refresh_order_price(self, req: Dict[str, Any], retry: int = 0) -> None:
        # Re-read the same-side BBO immediately before signing. Merely clamping
        # an older requested price prevents crossing but leaves a quote stale
        # when the market runs away during the REST round trip.
        req["limit_px"] = self._safe_post_only_price(
            float(req["limit_px"]), bool(req["is_buy"]), retry
        )

    def _fee_floor_half_spread(self) -> float:
        """Hard floor: half-spread must always cover round-trip fees + buffer."""
        floor_bps = MAKER_FEE_BPS + ADVERSE_BUFFER_BPS
        return max(TICK_SIZE, self.mid * floor_bps / 10000.0)

    def _adaptive_grid_reset_bps(self) -> float:
        """Volatility-aware rebuild threshold.

        A fixed tiny reset keeps the bot fast but can burn queue priority and
        fees in normal chop. ATR scaling lets the grid breathe when the range
        widens, while MAX_GRID_RESET_BPS prevents quotes from getting too stale.
        """
        mid = max(self.mid, TICK_SIZE)
        atr_reset = 0.0
        if self.regime.ready and self.regime.atr > 0:
            atr_reset = self.regime.atr * ATR_RESET_MULT / mid * 10000.0
        return min(MAX_GRID_RESET_BPS, max(GRID_RESET_BPS, atr_reset))

    def _in_blackout(self) -> bool:
        """True during the Paris-time flat window configured above."""
        if not BLACKOUT_ENABLED:
            return False
        now = datetime.now(_BLACKOUT_ZI) if _BLACKOUT_ZI else datetime.now()
        if now.weekday() not in BLACKOUT_DAYS:   # 0=lundi ... 6=dimanche
            return False
        start = now.replace(hour=BLACKOUT_START_H, minute=BLACKOUT_START_M,
                            second=0, microsecond=0)
        end = now.replace(hour=BLACKOUT_END_H, minute=BLACKOUT_END_M,
                        second=0, microsecond=0)
        return start <= now <= end

    @staticmethod
    def _blackout_label() -> str:
        return (f"lun-ven {BLACKOUT_START_H:02d}h{BLACKOUT_START_M:02d}-"
                f"{BLACKOUT_END_H:02d}h{BLACKOUT_END_M:02d} Paris")

    # ----------------------------------------------------------- websocket IO
    def _start_ws(self):
        self.console.print("[bold magenta]⚡ Connecting WebSocket feed...[/]")
        self.ws_info = Info(BASE_URL, skip_ws=False, spot_meta=self.fixed_spot_meta, perp_dexs=["xyz", "mkts"])
        # Lightweight top-of-book feed: smaller/faster than full L2 and enough
        # for this one-level touch market maker.
        self.ws_info.subscribe({"type": "bbo", "coin": SYMBOL}, self._on_bbo)
        self.ws_info.subscribe({"type": "trades", "coin": SYMBOL}, self._on_trades)
        self.ws_info.subscribe({"type": "userEvents", "user": ACCOUNT_ADDRESS}, self._on_user)
        self.ws_info.subscribe({"type": "userFills", "user": ACCOUNT_ADDRESS}, self._on_user_fills)
        self.ws_info.subscribe({"type": "orderUpdates", "user": ACCOUNT_ADDRESS}, self._on_order_updates)

    def _restart_ws(self):
        """Tear down the (presumed dead) WS connection and build a fresh one.
        The SDK does not reliably resubscribe after a silent drop, so a brand
        new Info(skip_ws=False) is the only dependable recovery."""
        try:
            self.ws_info.disconnect_websocket()
        except Exception:
            pass
        self.ws_ready = False
        try:
            self._start_ws()
        except Exception as exc:
            # last_tick_ts keeps aging, so the watchdog retries after cooldown.
            self.last_action = f"WS reconnect failed: {str(exc)[:40]}"

    def _refresh_bbo_from_rest(self) -> Tuple[bool, bool]:
        """Validate a silent L2 feed without pulling resting quotes.

        A websocket subscription receives market changes, not a guaranteed
        heartbeat.  Therefore a quiet book must not be treated as a dead feed.
        Returns ``(ok, moved)``; on success the REST snapshot becomes the fresh
        local BBO and resets the stale timer.
        """
        try:
            book = self.info.l2_snapshot(SYMBOL)
            levels = book.get("levels", []) if isinstance(book, dict) else []
            if len(levels) < 2 or not levels[0] or not levels[1]:
                return False, False
            best_bid = float(levels[0][0]["px"])
            best_ask = float(levels[1][0]["px"])
            if best_bid <= 0 or best_ask <= best_bid:
                return False, False
        except Exception:
            return False, False

        with self.lock:
            moved = (best_bid != self.best_bid or best_ask != self.best_ask)
            self.best_bid = best_bid
            self.best_ask = best_ask
            self.mid = (best_bid + best_ask) / 2.0
            self.last_tick_ts = time.time()
            self.ws_ready = True
        if moved:
            self.candles.on_tick((best_bid + best_ask) / 2.0)
            self.wake.set()
        return True, moved

    def _on_l2(self, data):
        try:
            levels = data.get("data", {}).get("levels", [])
            if len(levels) >= 2 and levels[0] and levels[1]:
                with self.lock:
                    self.best_bid = float(levels[0][0]["px"])
                    self.best_ask = float(levels[1][0]["px"])
                    self.mid = (self.best_bid + self.best_ask) / 2.0
                    self.last_tick_ts = time.time()
                    self.ws_ready = True
                self.candles.on_tick(self.mid)
                self.wake.set()    # wake the main cycle instantly
        except Exception:
            pass

    def _on_bbo(self, data):
        """Ultra-light top-of-book callback used by the hot quote path."""
        try:
            bbo = data.get("data", {}).get("bbo", [])
            if len(bbo) < 2 or not bbo[0] or not bbo[1]:
                return
            best_bid = float(bbo[0]["px"])
            best_ask = float(bbo[1]["px"])
            if best_bid <= 0 or best_ask <= best_bid:
                return
            mid = (best_bid + best_ask) / 2.0
            with self.lock:
                self.best_bid = best_bid
                self.best_ask = best_ask
                self.mid = mid
                self.last_tick_ts = time.time()
                self.ws_ready = True
            # Wake quote execution before non-critical candle bookkeeping.
            self.wake.set()
            self.candles.on_tick(mid)
        except Exception:
            pass

    def _on_trades(self, data):
        try:
            for t in data.get("data", []) or []:
                ts = float(t.get("time", 0)) / 1000.0
                self.candles.on_trade(float(t["sz"]), ts if ts > 0 else None)
        except Exception:
            pass

    def _on_user_fills(self, data):
        """Dedicated fill channel; userEvents alone can miss/lag fills."""
        try:
            payload = data.get("data", {})
            fills = payload.get("fills", []) or []
            if payload.get("isSnapshot"):
                # Subscription snapshots are historical, not session fills.
                with self.lock:
                    self.processed_fills.update(str(fill.get("tid")) for fill in fills)
                return
            self._on_user({"data": {"fills": fills}})
        except Exception:
            pass

    def _on_order_updates(self, data):
        """Remove filled/cancelled/rejected OIDs immediately and wake the loop."""
        try:
            payload = data.get("data", [])
            updates = payload if isinstance(payload, list) else payload.get("orders", [])
            changed = False
            terminal_markers = (
                "fill", "cancel", "reject", "margin", "reduceonly", "badalo", "expired"
            )
            with self.lock:
                for update in updates or []:
                    if not isinstance(update, dict):
                        continue
                    order = update.get("order", update)
                    if not self._coin_matches(order.get("coin")):
                        continue
                    oid = int(order.get("oid", update.get("oid", 0)) or 0)
                    if not oid:
                        continue
                    status = str(update.get("status", "")).lower().replace("_", "")
                    if any(marker in status for marker in terminal_markers):
                        for oid_list in (self.buy_oids, self.sell_oids):
                            if oid in oid_list:
                                oid_list.remove(oid)
                                changed = True
                    elif status in {"open", "resting"}:
                        target = self.buy_oids if order.get("side") == "B" else self.sell_oids
                        if oid not in target:
                            target.append(oid)
                            changed = True
                if changed:
                    self.anchor_mid = None
            if changed:
                self.wake.set()
        except Exception:
            pass

    @staticmethod
    def _coin_matches(coin: Optional[str]) -> bool:
        """True only for OUR instrument. HIP-3 events may carry the bare coin
        name (\"SP500\") instead of the dex-qualified one (\"xyz:SP500\"); an
        EMPTY coin must never match (str.endswith(\"\") is always True)."""
        if not coin:
            return False
        return coin == SYMBOL or SYMBOL.endswith(":" + coin)

    def _on_user(self, data):
        try:
            payload = data.get("data", {})
            has_clearinghouse_state = "clearinghouseState" in payload
            if has_clearinghouse_state:
                ch = payload["clearinghouseState"]
                found = False
                with self.lock:
                    for entry in ch.get("assetPositions", []):
                        pos = entry.get("position", {})
                        if self._coin_matches(pos.get("coin")):
                            self.position = float(pos.get("szi", 0))
                            self.unrealized_pnl = float(pos.get("unrealizedPnl", 0))
                            self.entry_price = float(pos.get("entryPx", 0) or 0)
                            self.margin_used = float(pos.get("marginUsed", 0) or 0)
                            found = True
                    if not found:
                        self.position = 0.0
                        self.unrealized_pnl = 0.0
                        self.entry_price = 0.0
                        self.margin_used = 0.0
                    self.perp_equity = float(ch.get("marginSummary", {}).get("accountValue", 0) or 0)
                    self.perp_withdrawable = float(ch.get("withdrawable", 0) or 0)
                    # Unified accounts return collateral to spot when a perp is
                    # flat. Keep the last REST-confirmed USDC balance in total
                    # equity instead of incorrectly displaying/using $0.
                    self.equity = self.perp_equity + self.spot_usdc
                    self.withdrawable = self.perp_withdrawable + self.spot_usdc
            
            with self.lock:
                for fill in payload.get("fills", []):
                    if not self._coin_matches(fill.get("coin")):
                        continue
                    self.margin_reject_pause_until = 0.0
                    tid = str(fill.get("tid"))
                    if tid in self.processed_fills:
                        continue
                    self.processed_fills.add(tid)
                    px, sz = float(fill["px"]), float(fill["sz"])
                    self.last_fill_ts = time.time()
                    # Fill events usually arrive before the next account-state
                    # snapshot. Apply their signed quantity immediately so a
                    # third same-side order cannot slip through during that lag.
                    if not has_clearinghouse_state:
                        side = str(fill.get("side", "")).upper()
                        direction = str(fill.get("dir", "")).lower()
                        if side == "B" or direction in ("open long", "close short"):
                            self.position = round(self.position + sz, SIZE_DECIMALS)
                        elif side in ("A", "S") or direction in ("open short", "close long"):
                            self.position = round(self.position - sz, SIZE_DECIMALS)
                    self.total_volume += px * sz
                    self.total_fees += float(fill.get("fee", 0))
                    self.session_pnl += float(fill.get("closedPnl", 0))
                    self.fills_count += 1
                    self.best_net_pnl = max(self.best_net_pnl, self.session_pnl - self.total_fees)
                    # Do not remove the oid on a possibly-partial fill. The
                    # dedicated orderUpdates stream tells us when it is truly
                    # terminal; this prevents duplicate replacement orders.
                    # A fill still changes inventory and wakes risk immediately.
                    self.anchor_mid = None
                    self.wake.set()    # react to the fill immediately
        except Exception:
            pass

    # --------------------------------------------------------- account / sync
    def sync_position(self):
        """Authoritative position/PnL/equity via REST (WS can lag)."""
        try:
            request_started = time.time()
            st = self.info.user_state(ACCOUNT_ADDRESS, dex=self.dex)
            try:
                spot_state = self.info.spot_user_state(ACCOUNT_ADDRESS)
                spot_usdc = sum(
                    max(0.0, float(balance.get("total", 0) or 0)
                        - float(balance.get("hold", 0) or 0))
                    for balance in spot_state.get("balances", [])
                    if balance.get("coin") == "USDC"
                )
            except Exception:
                with self.lock:
                    spot_usdc = self.spot_usdc
            target = SYMBOL.split(":")[-1]
            with self.lock:
                if self.last_fill_ts > request_started:
                    return  # this REST snapshot started before the latest fill
                self.perp_equity = float(st.get("marginSummary", {}).get("accountValue", 0) or 0)
                self.perp_withdrawable = float(st.get("withdrawable", 0) or 0)
                self.spot_usdc = spot_usdc
                self.equity = self.perp_equity + self.spot_usdc
                self.withdrawable = self.perp_withdrawable + self.spot_usdc
                for entry in st.get("assetPositions", []):
                    pos = entry.get("position", {})
                    if pos.get("coin") in (SYMBOL, target):
                        self.position = float(pos.get("szi", 0))
                        self.unrealized_pnl = float(pos.get("unrealizedPnl", 0))
                        self.entry_price = float(pos.get("entryPx", 0) or 0)
                        self.margin_used = float(pos.get("marginUsed", 0) or 0)
                        return
                self.position = 0.0
                self.unrealized_pnl = 0.0
                self.entry_price = 0.0
                self.margin_used = 0.0
        except Exception:
            pass

    def sync_orders(self):
        """Reconcile the local oid lists against the exchange's open orders.
        A missed WS fill/cancel leaves phantom oids that pin the sticky-quote
        logic forever (a '1B resting' that no longer exists)."""
        with self.lock:
            last_quote_ts = self.last_quote_ts
        if time.time() - last_quote_ts < ORDER_RECONCILE_GRACE_SEC:
            return   # just acted on orders: the REST snapshot may not include it yet
        try:
            oo = self.info.open_orders(ACCOUNT_ADDRESS, dex=self.dex)
        except Exception:
            return
        buys: List[int] = []
        sells: List[int] = []
        for o in oo:
            if self._coin_matches(o.get("coin")):
                (buys if o.get("side") == "B" else sells).append(int(o["oid"]))
        with self.lock:
            if sorted(buys) != sorted(self.buy_oids) or sorted(sells) != sorted(self.sell_oids):
                self.buy_oids, self.sell_oids = buys, sells
                self.anchor_mid = None       # force a rebuild on the next cycle
                self.wake.set()

    # ------------------------------------------------------------- order mgmt
    def cancel_all(self) -> bool:
        with self.lock:
            oids = self.buy_oids + self.sell_oids
        if not oids:
            return True
        try:
            res = self.exchange.bulk_cancel([{"coin": SYMBOL, "oid": o} for o in oids])
            if isinstance(res, dict) and res.get("status") != "ok":
                raise RuntimeError(str(res)[:160])
        except Exception as exc:
            with self.lock:
                self.last_order_error = f"cancel failed: {str(exc)[:120]}"
                self.last_order_error_ts = time.time()
                self.last_action = self.last_order_error
            return False
        action_ts = time.time()
        with self.lock:
            self.buy_oids, self.sell_oids = [], []
            self.anchor_mid = None
            self.last_intent = None
            self.last_quote_ts = action_ts
            self.last_partial_retry_ts = 0.0
        return True

    def cancel_all_rest(self):
        """Cancel what ACTUALLY rests on the exchange. The local oid lists can
        hold phantoms (missed WS fill event) — only the REST snapshot is
        authoritative. Used by the feed watchdog before reconnecting."""
        try:
            oo = self.info.open_orders(ACCOUNT_ADDRESS, dex=self.dex)
            reqs = [{"coin": SYMBOL, "oid": int(o["oid"])}
                    for o in oo if self._coin_matches(o.get("coin"))]
            if reqs:
                self.exchange.bulk_cancel(reqs)
        except Exception:
            pass
        action_ts = time.time()
        with self.lock:
            self.buy_oids, self.sell_oids = [], []
            self.anchor_mid = None
            self.last_intent = None
            self.last_quote_ts = action_ts
            self.last_partial_retry_ts = 0.0

    def place(self, bids: List[Tuple[float, float]], asks: List[Tuple[float, float]],
            bid_ro: List[bool], ask_ro: List[bool]) -> bool:
        """Place ALO quotes, repricing only legs rejected for crossing."""
        reqs = []
        for (p, s), ro in zip(bids, bid_ro):
            reqs.append({"coin": SYMBOL, "is_buy": True, "sz": s, "limit_px": p,
                        "order_type": {"limit": {"tif": "Alo"}}, "reduce_only": ro})
        for (p, s), ro in zip(asks, ask_ro):
            reqs.append({"coin": SYMBOL, "is_buy": False, "sz": s, "limit_px": p,
                        "order_type": {"limit": {"tif": "Alo"}}, "reduce_only": ro})
        if not reqs:
            return True

        pending = reqs
        errors: List[str] = []
        placed = 0
        for retry in range(POST_ONLY_MAX_RETRIES + 1):
            # Last-moment BBO clamp: build_grid() can be stale after one RTT.
            for req in pending:
                self._refresh_order_price(req, retry)
            try:
                res = self.exchange.bulk_orders(pending)
            except Exception as exc:
                errors.append(str(exc))
                break

            response = res.get("response", {}) if isinstance(res, dict) else res
            if not isinstance(res, dict) or res.get("status") != "ok" or not isinstance(response, dict):
                error = str(response if response else res)
                errors.append(error)
                if retry < POST_ONLY_MAX_RETRIES and self._is_post_only_match_error(error):
                    if POST_ONLY_RETRY_SEC > 0:
                        time.sleep(POST_ONLY_RETRY_SEC)
                    continue
                break

            statuses = response.get("data", {}).get("statuses", [])
            retry_reqs: List[Dict[str, Any]] = []
            with self.lock:
                for req, status in zip(pending, statuses):
                    if isinstance(status, dict) and "resting" in status:
                        oid = int(status["resting"]["oid"])
                        target_oids = self.buy_oids if req["is_buy"] else self.sell_oids
                        # orderUpdates can beat the REST order response by a few
                        # milliseconds; never track the same oid twice.
                        if oid not in target_oids:
                            target_oids.append(oid)
                        placed += 1
                    else:
                        error = str(status.get("error", status) if isinstance(status, dict) else status)
                        errors.append(error)
                        if retry < POST_ONLY_MAX_RETRIES and self._is_post_only_match_error(error):
                            retry_reqs.append(req)
                if len(statuses) != len(pending):
                    errors.append("incomplete order response")

            if not retry_reqs:
                break
            pending = retry_reqs
            if POST_ONLY_RETRY_SEC > 0:
                time.sleep(POST_ONLY_RETRY_SEC)

        all_placed = placed == len(reqs)
        if errors:
            with self.lock:
                prefix = "post-only repriced" if all_placed else "order err"
                self.last_order_error = errors[-1][:160]
                self.last_order_error_ts = time.time()
                self.last_action = f"{prefix}: {self.last_order_error[:60]}"
                # If we get a margin limit error, pause opening orders to stop spamming
                if any("margin" in err.lower() for err in errors):
                    self.margin_reject_pause_until = time.time() + 60.0
        elif all_placed:
            with self.lock:
                self.last_order_error = ""
                self.last_order_error_ts = 0.0
        return all_placed

    def flatten(self, reason: str):
        """Hard close: cancel everything, market-ish IOC out of the position."""
        self.cancel_all()
        self.sync_position()
        if abs(self.position) < MIN_SIZE * 0.5:
            return
        is_buy = self.position < 0
        sz = round(abs(self.position), SIZE_DECIMALS)
        px = self.best_ask + 20 * TICK_SIZE if is_buy else self.best_bid - 20 * TICK_SIZE
        px = round(max(px, TICK_SIZE), PRICE_DECIMALS)
        try:
            self.exchange.order(name=SYMBOL, is_buy=is_buy, sz=sz, limit_px=px,
                                order_type={"limit": {"tif": "Ioc"}}, reduce_only=True)
            self.last_action = f"FLATTEN ({reason})"
        except Exception as exc:
            self.last_action = f"flatten error: {str(exc)[:30]}"

    # ------------------------------------------------------- grid construction
    def build_grid(self) -> Tuple[List, List, List, List]:
        """
        Build the quote ladder for the current regime.

        In chop, quote the adaptive grid. In trend or post-trend cooldown, do
        not open new risk; keep only a reduce-only exit if inventory exists.

        Inventory cap is applied as a backstop: never exceed
        MAX_INVENTORY_ORDERS fills on either side.
        """
        bids: List[Tuple[float, float]] = []
        asks: List[Tuple[float, float]] = []
        bid_ro: List[bool] = []
        ask_ro: List[bool] = []
        eps = MIN_SIZE * 0.5
        # ---- LAYER 1: regime filter (primary defense) ----
        now = time.time()
        regime_pause = (
            TREND_FILTER_ENABLED
            and self.regime.ready
            and (self.regime.is_trend or now < self.regime_pause_until)
        )
        if regime_pause:
            # Defense first. Quote only the reducing side, as reduce-only.
            # Use POC as exit target when available (better than fixed offset).
            exit_off = max(self._fee_floor_half_spread(), self.regime.atr_half_spread())
            self.cur_half_spread = exit_off
            if self.position > eps:        # long -> sell to reduce
                if self.regime.poc is not None and self.regime.poc > self.mid:
                    # POC is above mid — target the volume-weighted fair price
                    exit_px = max(self.regime.poc, self.mid + exit_off)
                else:
                    exit_px = self.mid + exit_off
                px = self._round_px_up(max(exit_px, self.best_bid + TICK_SIZE))
                asks.append((px, round(abs(self.position), SIZE_DECIMALS)))
                ask_ro.append(True)
            elif self.position < -eps:     # short -> buy to reduce
                if self.regime.poc is not None and self.regime.poc < self.mid:
                    exit_px = min(self.regime.poc, self.mid - exit_off)
                else:
                    exit_px = self.mid - exit_off
                px = self._round_px_down(min(exit_px, self.best_ask - TICK_SIZE))
                bids.append((px, round(abs(self.position), SIZE_DECIMALS)))
                bid_ro.append(True)
            reason = "TREND" if self.regime.is_trend else "COOLDOWN"
            mode = "reduce-only" if (bids or asks) else "flat no-quotes"
            self.last_action = (f"{reason} {mode} | score={self.regime.trend_score:.2f} "
                                f"| dir={self.regime.trend_dir:+d}")
            return bids, asks, bid_ro, ask_ro

        # ---- CHOP: quote the adaptive fixed/ATR grid ----
        half_spread = max(
            TICK_SIZE,
            self._fee_floor_half_spread(),
            self.mid * BASE_SPREAD_BPS / 10000.0,
            self.regime.atr_half_spread(),
        )

        # Regime-proportional spread scaling: widen gradually as trend score
        # rises, even while still in CHOP. This reduces adverse selection in
        # the grey zone between clear chop and confirmed trend.
        if self.regime.ready and self.regime.trend_score > 0:
            regime_mult = 1.0 + 0.5 * min(self.regime.trend_score / max(ENTER_TREND_SCORE, 0.01), 2.0)
            regime_mult = min(regime_mult, 2.0)
            half_spread *= regime_mult

        # Mode fixe: N ticks du centre, point final (voir config MM_HALF_TICKS).
        if FIXED_HALF_SPREAD_TICKS > 0:
            half_spread = FIXED_HALF_SPREAD_TICKS * TICK_SIZE

        self.cur_half_spread = half_spread
        step = max(TICK_SIZE, self.mid * LEVEL_STEP_BPS / 10000.0)

        max_inventory_size = round(MAX_INVENTORY_ORDERS * ORDER_SIZE, SIZE_DECIMALS)

        # Inventory skew (Avellaneda-Stoikov lite): displace the center price
        # so that the side closing inventory is more aggressive and the side
        # adding inventory is pushed away. A long position lowers the center
        # -> the ask becomes cheaper -> attracts sellers -> returns to flat.
        inv_ratio = self.position / max(max_inventory_size, MIN_SIZE)
        inv_ratio = max(-1.0, min(1.0, inv_ratio))
        skew = inv_ratio * self.mid * INVENTORY_SKEW_BPS / 10000.0
        book_has_bbo = self.best_bid > 0 and self.best_ask > self.best_bid
        book_mid = (self.best_bid + self.best_ask) / 2.0 if book_has_bbo else self.mid
        center = book_mid if GRID_QUOTE_MODE == "book_center" else self.mid - skew

        bid_half = half_spread
        ask_half = half_spread

        # Maker-safe rung 0: fixed spread around mid, capped one tick from the
        # opposite side.
        bid_base = self._round_px_down(min(center - bid_half, self.best_ask - TICK_SIZE))
        ask_base = self._round_px_up(max(center + ask_half, self.best_bid + TICK_SIZE))

        book_spread = self.best_ask - self.best_bid if book_has_bbo else 0.0
        fixed_mode = FIXED_HALF_SPREAD_TICKS > 0
        if GRID_QUOTE_MODE == "bbo_offset" and book_has_bbo and not fixed_mode:
            ref_px = max(book_mid, self.mid, TICK_SIZE)
            offset_px = ref_px * max(0.0, BBO_OFFSET_BPS) / 10000.0
            bid_base = self._round_px_down(max(TICK_SIZE, self.best_bid - offset_px))
            ask_base = self._round_px_up(max(self.best_ask + offset_px, bid_base + TICK_SIZE))
            self.cur_half_spread = max(TICK_SIZE, (ask_base - bid_base) / 2.0)

        # En mode fixe, aucun join BBO: l'ordre reste à N ticks du centre.
        force_bbo = GRID_QUOTE_MODE == "bbo" and book_has_bbo and not fixed_mode
        join_book_safe = (
            JOIN_BOOK_SPREAD_WHEN_SAFE
            and book_has_bbo
            and not fixed_mode
            and book_spread >= 2.0 * half_spread
        )
        join_book = force_bbo or join_book_safe
        join_bid_bbo = join_book and (force_bbo or self.position <= eps)
        join_ask_bbo = join_book and (force_bbo or self.position >= -eps)
        if join_bid_bbo:
            bid_base = self._round_px_down(self.best_bid)
        if join_ask_bbo:
            ask_base = self._round_px_up(self.best_ask)

        # Margin-aware sizing. With a position already consuming all available
        # margin, opening orders are guaranteed to be rejected. Keep only a
        # reduce-only quote on the side that closes inventory. When flat, split
        # capacity between bid and ask so both can genuinely rest at once.
        gross_capacity_usd = max(0.0, self.equity * LEVERAGE * MARGIN_SAFETY_FRACTION)
        used_notional_usd = abs(self.position) * self.mid
        remaining_capacity_usd = max(0.0, gross_capacity_usd - used_notional_usd)
        opening_sides = 2 if abs(self.position) <= eps else 1
        opening_capacity_each = remaining_capacity_usd / opening_sides
        # Disable margin-based scaling so order sizes are exactly as configured by the user (ORDER_SIZE)
        opening_size = ORDER_SIZE
        if time.time() < self.margin_reject_pause_until:
            opening_size = 0.0
        size_step = 10 ** (-SIZE_DECIMALS)
        opening_size = math.floor((opening_size + 1e-12) / size_step) * size_step
        opening_size = round(opening_size, SIZE_DECIMALS)
        if opening_size < MIN_SIZE or opening_size * self.mid < MIN_ORDER_NOTIONAL_USD:
            opening_size = 0.0
        # Inventory backstop: cap signed quantity to exactly N configured fills.
        proj_long = self.position
        proj_short = self.position
        cap_eps = 10 ** (-SIZE_DECIMALS) * 0.1

        for i in range(NUM_LEVELS):
            # BID closes a short without requiring free margin. Do not size it
            # beyond the position, otherwise reduce-only would be rejected.
            if self.position < -eps and i == 0:
                bid_size = round(min(ORDER_SIZE, abs(self.position)), SIZE_DECIMALS)
                bid_is_reduce = True
            else:
                bid_size = opening_size
                bid_is_reduce = False
            if (bid_size >= MIN_SIZE and
                    (bid_is_reduce or proj_long + bid_size <= max_inventory_size + cap_eps)):
                pb = self._round_px_down(bid_base - i * step)
                if pb > 0:
                    bids.append((pb, bid_size)); bid_ro.append(bid_is_reduce)
                    proj_long += bid_size

            # ASK closes a long under the same rule.
            if self.position > eps and i == 0:
                ask_size = round(min(ORDER_SIZE, abs(self.position)), SIZE_DECIMALS)
                ask_is_reduce = True
            else:
                ask_size = opening_size
                ask_is_reduce = False
            if (ask_size >= MIN_SIZE and
                    (ask_is_reduce or proj_short - ask_size >= -max_inventory_size - cap_eps)):
                pa = self._round_px_up(ask_base + i * step)
                if pa > 0:
                    asks.append((pa, ask_size)); ask_ro.append(ask_is_reduce)
                    proj_short -= ask_size

        tag = f"{GRID_QUOTE_MODE.upper()} CHOP quoting"
        if GRID_QUOTE_MODE in {"safe_bbo", "bbo"} and book_has_bbo:
            needed_spread = 2.0 * half_spread
            if force_bbo:
                tag += f" | force-BBO {book_spread:.{PRICE_DECIMALS}f}"
            elif join_bid_bbo and join_ask_bbo:
                tag += f" | join-BBO {book_spread:.{PRICE_DECIMALS}f}/{needed_spread:.{PRICE_DECIMALS}f}"
            elif join_bid_bbo:
                tag += f" | join bid BBO {book_spread:.{PRICE_DECIMALS}f}/{needed_spread:.{PRICE_DECIMALS}f}"
            elif join_ask_bbo:
                tag += f" | join ask BBO {book_spread:.{PRICE_DECIMALS}f}/{needed_spread:.{PRICE_DECIMALS}f}"
            else:
                tag += f" | BBO tight {book_spread:.{PRICE_DECIMALS}f}<{needed_spread:.{PRICE_DECIMALS}f}"
        elif GRID_QUOTE_MODE == "bbo_offset" and book_has_bbo:
            bid_gap_bps = max(0.0, (self.best_bid - bid_base) / max(book_mid, 1e-9) * 10000.0)
            ask_gap_bps = max(0.0, (ask_base - self.best_ask) / max(book_mid, 1e-9) * 10000.0)
            tag += f" | BBO offset bid {bid_gap_bps:.2f}bps / ask {ask_gap_bps:.2f}bps"
        elif GRID_QUOTE_MODE == "book_center" and book_has_bbo:
            tag += f" | book center {book_mid:.{PRICE_DECIMALS}f}"
        if not bids:
            tag += " | inv-cap: max (no buys)"
        if not asks:
            tag += " | inv-cap: max (no sells)"
        if time.time() < self.margin_reject_pause_until:
            tag += f" | MARGIN PAUSE {self.margin_reject_pause_until - time.time():.0f}s"
        elif opening_size <= 0 and abs(self.position) > eps:
            tag += " | margin full: reduce-only"
        if self.regime.ready:
            tag = f"score={self.regime.trend_score:.2f} | " + tag
        self.last_action = tag
        return bids, asks, bid_ro, ask_ro

    def _quote_intent(self, bids, asks, bid_ro, ask_ro) -> tuple:
        """
        Coarse description of WHAT we want to quote — not the exact prices.
        Keeps quotes sticky: we only tear down the ladder when the intent
        changes (regime flip, a side gets blocked, inventory side flips, or a
        reduce-only exit appears), never on tiny mid wiggles. Exact-price drift
        is handled separately by the adaptive ATR reset threshold.
        """
        eps = MIN_SIZE * 0.5
        with self.lock:
            pos = self.position
        pos_side = 1 if pos > eps else (-1 if pos < -eps else 0)
        regime_pause = (
            TREND_FILTER_ENABLED
            and self.regime.ready
            and (self.regime.is_trend or time.time() < self.regime_pause_until)
        )
        return (
            bool(regime_pause),
            len(bids), len(asks),
            tuple(bid_ro), tuple(ask_ro),
            pos_side,
        )

    # ------------------------------------------------------------- main cycle
    def loop_once(self):
        now = time.time()

        # ---- LAYER 0: feed watchdog ----
        # A dead feed means a stale mid and phantom oids: quoting on it is
        # worse than not quoting at all. Clean the book via REST (authoritative,
        # not the local lists) and rebuild the WS connection.
        with self.lock:
            last_tick_ts = self.last_tick_ts
            last_ws_restart = self.last_ws_restart
            ws_ready = self.ws_ready
            mid = self.mid
            position = self.position
            margin_used = self.margin_used
            unrealized_pnl = self.unrealized_pnl
            risk_pause_until = self.risk_pause_until
            regime_pause_until = self.regime_pause_until
            buy_oids = list(self.buy_oids)
            sell_oids = list(self.sell_oids)
            anchor_mid = self.anchor_mid
            last_intent = self.last_intent

        if (last_tick_ts > 0 and now - last_tick_ts > WS_STALE_SEC
                and now - last_ws_restart > WS_RESTART_COOLDOWN):
            stale_age = now - last_tick_ts
            with self.lock:
                self.last_ws_restart = now
            rest_ok, book_moved = self._refresh_bbo_from_rest()
            if rest_ok:
                # No BBO change means the market is simply quiet: preserve both
                # the live order and its queue priority.  If REST saw a move that
                # WS missed, rebuild only the feed; normal reset/intent checks
                # decide whether the still-resting quote needs a rebuild.
                with self.lock:
                    self.last_action = (
                        "quiet book verified via REST - KEEP quotes"
                        if not book_moved else
                        "WS missed BBO move -> reconnect, KEEP quotes"
                    )
                if book_moved:
                    self._restart_ws()
                return

            # A single failed REST probe is not enough reason to surrender queue
            # priority.  Reconnect the feed while leaving orders resting.  Only
            # an extended loss of both WS and REST triggers the safety pull.
            if stale_age < WS_HARD_STALE_SEC:
                with self.lock:
                    self.last_action = f"feed probe failed ({stale_age:.0f}s) -> reconnect, KEEP quotes"
                self._restart_ws()
                return

            with self.lock:
                self.last_action = f"feed unavailable {stale_age:.0f}s -> safety cancel"
            self.cancel_all_rest()
            self._restart_ws()
            return

        if not ws_ready or mid <= 0:
            with self.lock:
                self.last_action = "waiting for feed"
            return

        # No REST here: position/PnL are kept fresh by the WS feed, and the
        # background thread reconciles via REST every POSITION_SYNC_SEC.

        # ---- LAYER 0b: Paris flat window (lun-ven, 15h25-16h45) ----
        # During the configured window the bot must be flat: no resting quotes
        # and no active position. On entry, clean the exchange book via REST in
        # case local oid tracking lags behind websocket updates.
        in_blackout = self._in_blackout()
        if in_blackout:
            entered_blackout = not self.blackout_was_active
            self.blackout_was_active = True
            if entered_blackout:
                self.cancel_all_rest()
                self.sync_position()
                with self.lock:
                    position = self.position
                    buy_oids = list(self.buy_oids)
                    sell_oids = list(self.sell_oids)

            if abs(position) > MIN_SIZE * 0.5:
                if entered_blackout or now - self.last_blackout_flatten_ts >= BLACKOUT_FLATTEN_RETRY_SEC:
                    self.last_blackout_flatten_ts = now
                    self.flatten(f"blackout flat {self._blackout_label()}")
                else:
                    wait = max(0.0, BLACKOUT_FLATTEN_RETRY_SEC - (now - self.last_blackout_flatten_ts))
                    with self.lock:
                        self.last_action = f"blackout flat retry in {wait:.1f}s"
                return

            if buy_oids or sell_oids:
                if not self.cancel_all():
                    return
            with self.lock:
                self.last_action = f"blackout flat active ({self._blackout_label()})"
            return
        self.blackout_was_active = False

        # ---- LAYER 3: stop loss / take profit (hard floor) ----
        if abs(position) > MIN_SIZE * 0.5 and margin_used > 0:
            roe_pct = unrealized_pnl / margin_used * 100.0
            if roe_pct <= -STOP_LOSS_ROE_PCT:
                self.flatten(f"stop loss {roe_pct:.1f}%")
                with self.lock:
                    self.risk_pause_until = now + RISK_PAUSE_SEC
                return
            if roe_pct >= TAKE_PROFIT_ROE_PCT:
                self.flatten(f"take profit {roe_pct:.1f}%")
                with self.lock:
                    self.risk_pause_until = now + RISK_PAUSE_SEC
                return

        if now < risk_pause_until:
            if not self.cancel_all():
                return
            with self.lock:
                self.last_action = f"risk pause {risk_pause_until - now:.0f}s"
            return

        # ---- update the regime from live candles ----
        if now - self.last_regime_update_ts >= REGIME_UPDATE_SEC:
            # Include the forming minute: closed bars introduced up to 60
            # seconds of lag precisely when price started to run.
            was_trend = self.regime.is_trend
            self.regime.update(self.candles.bars_for_regime())
            if TREND_FILTER_ENABLED and self.regime.ready:
                if self.regime.is_trend:
                    self.regime_pause_until = max(
                        self.regime_pause_until,
                        now + TREND_COOLDOWN_SEC,
                    )
                elif was_trend:
                    self.regime_pause_until = max(
                        self.regime_pause_until,
                        now + TREND_COOLDOWN_SEC,
                    )
            self.last_regime_update_ts = now
            with self.lock:
                regime_pause_until = self.regime_pause_until

        # ---- LAYER 1b: ACTIVE EXIT when a confirmed trend runs AGAINST inventory ----
        # This is the fix for "it dumps and the order just sits there": a passive
        # ask above a falling market never fills. If we're long in a confirmed
        # downtrend (or short in an uptrend), stop waiting and flatten by taking
        # liquidity right now.
        if (TREND_EXIT_ENABLED
                and TREND_FILTER_ENABLED
                and self.regime.ready and self.regime.is_trend
                and abs(position) > MIN_SIZE * 0.5):
            against = ((self.regime.trend_dir < 0 and position > 0) or
                    (self.regime.trend_dir > 0 and position < 0))
            if against:
                self.flatten(f"trend against position (dir {self.regime.trend_dir:+d}, "
                            f"score {self.regime.trend_score:.2f})")
                with self.lock:
                    self.risk_pause_until = now + RISK_PAUSE_SEC
                return

        # ---- REGIME CUTOFF / POST-TREND COOLDOWN ----
        # In trend/breakout, or just after one, flat means no quotes. If
        # inventory exists, build_grid() continues so only reduce-only exits
        # can rest.
        regime_pause_active = (
            TREND_FILTER_ENABLED
            and self.regime.ready
            and (self.regime.is_trend or now < regime_pause_until)
        )
        if regime_pause_active and abs(position) <= MIN_SIZE * 0.5:
            if buy_oids or sell_oids:
                if not self.cancel_all():
                    return
            with self.lock:
                remaining = max(0.0, regime_pause_until - now)
                reason = "TREND" if self.regime.is_trend else "COOLDOWN"
                self.last_action = (
                    f"{reason} QUOTES CUT | score={self.regime.trend_score:.2f} | "
                    f"cooldown {remaining:.0f}s"
                )
            return

        # ---- build & (re)place the ladder (STICKY: avoid cancel/replace churn) ----
        bids, asks, bid_ro, ask_ro = self.build_grid()
        intent = self._quote_intent(bids, asks, bid_ro, ask_ro)
        with self.lock:
            buy_count = len(self.buy_oids)
            sell_count = len(self.sell_oids)
            resting = bool(buy_count or sell_count)
        target_empty = not bids and not asks

        if target_empty:
            # Nothing should be quoted (e.g. flat in a trend). Only act to clear leftovers.
            if resting:
                if not self.cancel_all():
                    return
            with self.lock:
                self.last_intent = intent
            return

        drift_bps = (abs(mid - anchor_mid) / max(anchor_mid, 1e-9) * 10000.0
                    if anchor_mid else 1e9)
        reset_bps = self._adaptive_grid_reset_bps()
        shape_exact = buy_count == len(bids) and sell_count == len(asks)
        missing_only = (
            resting
            and buy_count <= len(bids)
            and sell_count <= len(asks)
            and (buy_count < len(bids) or sell_count < len(asks))
            and (last_intent is None or intent == last_intent)
            and (anchor_mid is None or drift_bps < reset_bps)
        )
        need_rebuild = (
            not resting
            or anchor_mid is None
            or intent != last_intent
            or not shape_exact
            or drift_bps >= reset_bps
        )
        if not need_rebuild:
            return

        if missing_only:
            with self.lock:
                retry_wait = PARTIAL_RETRY_SEC - (now - self.last_partial_retry_ts)
                if retry_wait > 0:
                    self.last_action = (
                        f"partial grid: keep {buy_count}B/{sell_count}A resting, "
                        f"retry missing in {retry_wait:.1f}s"
                    )
                    return

            if not self._claim_quote_action_slot(now):
                return

            t0 = time.time()
            missing_bids = bids[buy_count:]
            missing_asks = asks[sell_count:]
            quote_ok = self.place(
                missing_bids,
                missing_asks,
                bid_ro[buy_count:],
                ask_ro[sell_count:],
            )

            with self.lock:
                self.order_action_ms = (time.time() - t0) * 1000.0
                self.last_quote_ts = time.time()
                self.last_partial_retry_ts = self.last_quote_ts
                complete = (
                    len(self.buy_oids) == len(bids)
                    and len(self.sell_oids) == len(asks)
                )
                if quote_ok and complete:
                    self.anchor_mid = mid
                    self.last_intent = intent
                    self.last_partial_retry_ts = 0.0
                else:
                    # Preserve the queue priority of the leg that successfully
                    # rests; only the missing leg will be retried later.
                    self.anchor_mid = None
                    self.last_intent = intent
                    self.last_action = (
                        f"partial grid: keep {len(self.buy_oids)}B/{len(self.sell_oids)}A resting, "
                        f"missing {max(0, len(bids) - len(self.buy_oids))}B/"
                        f"{max(0, len(asks) - len(self.sell_oids))}A"
                    )
            return

        if not self._claim_quote_action_slot(now):
            return

        t0 = time.time()
        if resting and not self.cancel_all():
            return
        quote_ok = self.place(bids, asks, bid_ro, ask_ro)

        with self.lock:
            self.order_action_ms = (time.time() - t0) * 1000.0
            self.last_quote_ts = time.time()
            complete = (
                len(self.buy_oids) == len(bids)
                and len(self.sell_oids) == len(asks)
            )
            if quote_ok and complete:
                self.anchor_mid = mid
                self.last_intent = intent
                self.last_partial_retry_ts = 0.0
            else:
                self.anchor_mid = None
                self.last_intent = None
                self.last_partial_retry_ts = time.time()

    # --------------------------------------------------------------- dashboard
    def dashboard(self) -> Panel:
        """Live dashboard in the same style as 45_MM (logo + stats + status)."""
        with self.lock:
            mid = self.mid
            best_bid = self.best_bid
            best_ask = self.best_ask
            position = self.position
            entry_price = self.entry_price
            margin_used = self.margin_used
            unrealized_pnl = self.unrealized_pnl
            equity = self.equity
            buy_oids_len = len(self.buy_oids)
            sell_oids_len = len(self.sell_oids)
            session_pnl = self.session_pnl
            total_fees = self.total_fees
            total_volume = self.total_volume
            fills_count = self.fills_count
            best_net_pnl = self.best_net_pnl
            cur_half_spread = self.cur_half_spread
            anchor_mid = self.anchor_mid
            regime_pause_until = self.regime_pause_until
            last_action = self.last_action
            last_order_error = self.last_order_error
            last_order_error_ts = self.last_order_error_ts
            loop_ms = self.loop_ms
            order_action_ms = self.order_action_ms
            last_tick_ts = self.last_tick_ts

        logo_content = r"""[bold cyan]
██╗  ██╗██╗   ██╗██████╗ ███████╗██████╗ ██╗     ██╗ ██████╗ ██╗   ██╗██╗██████╗
██║  ██║╚██╗ ██╔╝██╔══██╗██╔════╝██╔══██╗██║     ██║██╔═══██╗██║   ██║██║██╔══██╗
███████║ ╚████╔╝ ██████╔╝█████╗  ██████╔╝██║     ██║██║   ██║██║   ██║██║██║  ██║
██╔══██║  ╚██╔╝  ██╔═══╝ ██╔══╝  ██╔══██╗██║     ██║██║▄▄ ██║██║   ██║██║██║  ██║
██║  ██║   ██║   ██║     ███████╗██║  ██║███████╗██║╚██████╔╝╚██████╔╝██║██████╔╝
╚═╝  ╚═╝   ╚═╝   ╚═╝     ╚══════╝╚═╝  ╚═╝╚══════╝╚═╝ ╚══▀▀═╝  ╚═════╝ ╚═╝╚═════╝

[/bold cyan]"""
        net = session_pnl - total_fees

        # ---- Top stats table: PAIR | COST/1M | VOLUME | FEES | NET PNL ----
        table = Table(box=box.ROUNDED, show_header=True, header_style="bold cyan")
        table.add_column("PAIR", style="bold white", justify="left")
        table.add_column("COST/1M", style="dim", justify="center")
        table.add_column("VOLUME", style="green", justify="right")
        table.add_column("FEES", style="yellow", justify="right")
        table.add_column("NET PNL", justify="right")

        net_color = "green" if net >= 0 else "red"
        net_text = (f"[{net_color}]${net:+.4f}[/{net_color}]" if abs(net) < 1
                    else f"[{net_color}]${net:+.2f}[/{net_color}]")
        # Cost per million traded (fees minus realized pnl) — the article's key metric.
        net_cost = total_fees - session_pnl
        cost_per_m = net_cost / (total_volume / 1_000_000) if total_volume > 0 else 0.0
        cost_color = "green" if cost_per_m < 200 else "yellow" if cost_per_m < 400 else "red"
        cost_text = f"[{cost_color}]${cost_per_m:.1f}[/{cost_color}]"
        fees_text = f"${total_fees:.4f}" if total_fees < 1 else f"${total_fees:.2f}"
        table.add_row(f"🔶 {SYMBOL}", cost_text, f"${total_volume:.2f}", fees_text, net_text)

        # caption: Vol/H | Fills/min | Spread | Drift
        elapsed_h = max((time.time() - self.start_time) / 3600, 1e-9)
        elapsed_m = max((time.time() - self.start_time) / 60, 1.0)
        vol_h = total_volume / elapsed_h
        fills_min = fills_count / elapsed_m
        spread_bps = (cur_half_spread * 2.0 / mid * 10000.0) if mid > 0 else 0.0
        drift_bps = (abs(mid - anchor_mid) / max(anchor_mid, 1e-9) * 10000.0
                    if anchor_mid else 0.0)
        reset_bps = self._adaptive_grid_reset_bps()
        drift_color = "red" if drift_bps >= reset_bps else "yellow" if drift_bps >= reset_bps * 0.5 else "green"
        table.caption = (f"\n⏱️  Vol/H: [bold yellow]${vol_h:.0f}[/bold yellow] | "
                        f"Fills/min: [cyan]{fills_min:.1f}[/cyan] | "
                        f"Spread: [magenta]{spread_bps:.1f}bps[/magenta] | "
                        f"Drift: [{drift_color}]{drift_bps:.1f}/{reset_bps:.1f}bps[/]")

        # ---- Status table (key/value) ----
        st = Table(box=None, show_header=False, padding=(0, 1))
        st.add_column("Key", style="dim")
        st.add_column("Value", style="bold")

        runtime = int(time.time() - self.start_time)
        h, rem = divmod(runtime, 3600)
        m, s = divmod(rem, 60)
        runtime_str = f"{h}h {m}m {s}s" if h > 0 else f"{m}m {s}s"
        launch_time = datetime.fromtimestamp(self.start_time).strftime("%H:%M:%S")
        notional = position * mid
        inventory_cap_size = round(MAX_INVENTORY_ORDERS * ORDER_SIZE, SIZE_DECIMALS)
        inventory_cap_usd = inventory_cap_size * mid
        roe = (unrealized_pnl / margin_used * 100.0) if margin_used > 0 else 0.0
        gross_capacity_usd = max(0.0, equity * LEVERAGE * MARGIN_SAFETY_FRACTION)
        used_notional_usd = abs(position) * mid
        remaining_capacity_usd = max(0.0, gross_capacity_usd - used_notional_usd)
        opening_sides = 2 if abs(position) <= MIN_SIZE * 0.5 else 1
        # Disable margin-based scaling in display
        max_opening_size = ORDER_SIZE
        size_step = 10 ** (-SIZE_DECIMALS)
        max_opening_size = math.floor((max_opening_size + 1e-12) / size_step) * size_step
        max_opening_size = max(0.0, round(max_opening_size, SIZE_DECIMALS))
        if max_opening_size < MIN_SIZE or max_opening_size * mid < MIN_ORDER_NOTIONAL_USD:
            max_opening_size = 0.0
        target_capacity_usd = used_notional_usd + ORDER_SIZE * mid * opening_sides
        equity_for_target = target_capacity_usd / max(LEVERAGE * MARGIN_SAFETY_FRACTION, 1e-9)
        size_fmt = f"{{:.{SIZE_DECIMALS}f}}"

        # Regime line
        if not self.regime.ready:
            reg_txt = "[dim]warming up... (accumulating candles)[/]"
        else:
            if TREND_FILTER_ENABLED:
                reg = "[red]● TREND[/]" if self.regime.is_trend else "[green]● CHOP[/]"
            else:
                reg = "[green]● ADAPTIVE[/]"
            va = (f"VA[{self.regime.val:.0f}-{self.regime.vah:.0f}] POC {self.regime.poc:.0f}"
                if self.regime.val and self.regime.vah else "VA[--]")
            reg_txt = (f"{reg} score {self.regime.trend_score:.2f} | ER {self.regime.efficiency_ratio:.2f} | "
                    f"VPexp {self.regime.vp_expansion:.2f} | ATR {self.regime.atr:.1f} | {va} | "
                    f"dir {self.regime.trend_dir:+d}")

        if self.regime.ready:
            cooldown_left = max(0.0, regime_pause_until - time.time())
            if TREND_FILTER_ENABLED and self.regime.is_trend:
                reg = "[red]TREND PAUSE[/]"
            elif TREND_FILTER_ENABLED and cooldown_left > 0:
                reg = f"[yellow]COOLDOWN {cooldown_left:.0f}s[/]"
            elif TREND_FILTER_ENABLED:
                reg = "[green]ADAPTIVE CHOP[/]"
            else:
                reg = "[green]ADAPTIVE DIAGNOSTIC[/]"
            va = (f"VA[{self.regime.val:.0f}-{self.regime.vah:.0f}] POC {self.regime.poc:.0f}"
                if self.regime.val and self.regime.vah else "VA[--]")
            reg_txt = (f"{reg} score {self.regime.trend_score:.2f} | ER {self.regime.efficiency_ratio:.2f} | "
                    f"VPexp {self.regime.vp_expansion:.2f} | BRK {self.regime.breakout_score:.2f} | "
                    f"ATR {self.regime.atr:.1f} | {va} | "
                    f"dir {self.regime.trend_dir:+d}")

        pos_color = "green" if position > 0 else "red" if position < 0 else "white"
        st.add_row("Runtime", f"{runtime_str} (started {launch_time})")
        st.add_row("PAIR", f"[cyan]{SYMBOL}[/]  |  Mid [white]{mid:.0f}[/] (bid {best_bid:.0f} / ask {best_ask:.0f})")
        st.add_row("Leverage", f"[yellow]{LEVERAGE}x[/]  |  Mode [white]ADAPTIVE regime-gated[/]  |  "
                            f"Quote [white]{GRID_QUOTE_MODE}[/]")
        st.add_row("Sizing", f"target {size_fmt.format(ORDER_SIZE)} | cap/side {size_fmt.format(max_opening_size)} | "
                            f"need equity ${equity_for_target:.2f}")
        st.add_row("Orders", f"[green]{buy_oids_len}B[/] / [red]{sell_oids_len}A[/]")
        st.add_row("Position", f"[{pos_color}]{position:+.4f}[/] (${notional:+.1f} / cap ${inventory_cap_usd:.1f}) | "
                            f"uPnL ${unrealized_pnl:+.3f} ({roe:+.1f}% ROE)")
        st.add_row("Regime", reg_txt)
        st.add_row("Fills", f"{fills_count} trades | Best NET: ${best_net_pnl:+.3f}")
        st.add_row("PnL", f"gross ${session_pnl:+.3f}  -  fees ${total_fees:.3f}  =  "
                        f"[{net_color}]NET ${net:+.3f}[/]")
        if GRID_QUOTE_MODE == "bbo_offset":
            configured_half_spread = mid * max(0.0, BBO_OFFSET_BPS) / 10000.0
            spread_label = f"BBO gap target {BBO_OFFSET_BPS:.2f}bps/side"
        else:
            configured_half_spread = max(
                TICK_SIZE,
                mid * BASE_SPREAD_BPS / 10000.0,
                mid * (MAKER_FEE_BPS + ADVERSE_BUFFER_BPS) / 10000.0,
                self.regime.atr_half_spread(),
            )
            active_full_spread_bps = (configured_half_spread * 2.0
                                    / max(mid, 1e-9) * 10000.0)
            spread_label = f"Spread target adaptive {active_full_spread_bps:.2f}bps full"
        st.add_row("Risk", f"SL {STOP_LOSS_ROE_PCT:.0f}% / TP {TAKE_PROFIT_ROE_PCT:.0f}% | "
                        f"{spread_label} | "
                        f"Equity ${equity:.2f}")
        if BLACKOUT_ENABLED:
            blocked = self._in_blackout()
            sess_txt = ("[red]● FENETRE FLAT[/]" if blocked
                        else "[green]● MM actif[/]")
            st.add_row("Séance", f"{sess_txt}  (zero position/ordre {self._blackout_label()})")
        loop_color = "green" if loop_ms < 200 else "yellow" if loop_ms < 600 else "red"
        action_color = "green" if order_action_ms < 400 else "yellow" if order_action_ms < 1000 else "red"
        feed_age = (time.time() - last_tick_ts) if last_tick_ts > 0 else -1.0
        feed_txt = ("[dim]--[/]" if feed_age < 0 else
                    f"[{'green' if feed_age < 3 else 'yellow' if feed_age < WS_STALE_SEC else 'red'}]{feed_age:.0f}s[/]")
        st.add_row("Cycle", f"[{loop_color}]{loop_ms:.0f}ms[/] | order action [{action_color}]{order_action_ms:.0f}ms[/] | "
                            f"feed age {feed_txt} | heartbeat {LOOP_INTERVAL_SEC*1000:.0f}ms")
        status_text = last_action
        if last_order_error and time.time() - last_order_error_ts < 60.0:
            status_text += f"\n[red]HL reject: {last_order_error}[/red]"
        st.add_row("Status", status_text)

        # ---- Layout ----
        layout = Layout()
        layout.split_column(
            Layout(Align.center(table), name="main", size=7),
            Layout(Align.center(st), name="status", size=13),
        )
        return Panel(
            Group(Align.center(Text.from_markup(logo_content)), layout),
            title="[bold cyan]🚀 GRID MM — ADAPTIVE GRID[/bold cyan]",
            subtitle=f"[dim]Press Ctrl+C to stop | Regime filter {'active' if TREND_FILTER_ENABLED else 'disabled'} | Refresh: {LOOP_INTERVAL_SEC*1000:.0f}ms[/dim]",
            border_style="cyan",
            padding=(1, 2),
        )

    def _sync_thread(self):
        """Background REST reconciliation — never blocks the trading cycle."""
        last_position_sync = 0.0
        while self.running:
            now = time.time()
            if now - last_position_sync >= POSITION_SYNC_SEC:
                self.sync_position()
                last_position_sync = now
            self.sync_orders()
            # Keep the EXCHANGE session's TCP/TLS connection warm: the order
            # endpoint shares the same host, so a cheap /info ping through the
            # exchange's own session means the next order never pays a fresh
            # handshake (~300-600ms saved on the first order after idle).
            if now - last_position_sync < ORDER_SYNC_SEC:
                try:
                    self.exchange.session.post(BASE_URL + "/info",
                                            json={"type": "meta"}, timeout=5)
                except Exception:
                    pass
            time.sleep(ORDER_SYNC_SEC)

    # -------------------------------------------------------------------- run
    def _trading_loop(self, live=None):
        """Boucle de trading. `live` = dashboard Rich (console directe) ou
        None (headless: lancé par bot_server, l'affichage est le web)."""
        last_render = 0.0
        while self.running:
            # Event-driven: a WS tick/fill wakes us instantly; otherwise
            # fall through after LOOP_INTERVAL_SEC as a heartbeat.
            self.wake.wait(timeout=LOOP_INTERVAL_SEC)
            self.wake.clear()
            t0 = time.time()
            try:
                self.loop_once()
            except Exception as exc:
                self.last_action = f"loop error: {str(exc)[:50]}"
            self.loop_ms = (time.time() - t0) * 1000.0
            # Render at 2 Hz max; quote/order events still run at full speed.
            if live is not None and time.time() - last_render >= 0.5:
                live.update(self.dashboard())
                last_render = time.time()
            # Throttle back-to-back cycles (busy book) to spare rate limits.
            spare = MIN_CYCLE_SEC - (time.time() - t0)
            if spare > 0:
                time.sleep(spare)

    def run(self):
        # Headless quand lancé par le serveur web (thread non principal):
        # deux dashboards Rich dans le même terminal se battent pour le
        # curseur -> clignotement. Le dashboard web remplace l'affichage.
        headless = threading.current_thread() is not threading.main_thread()
        if not headless:
            print("⏳ Waiting for first book update...")
        while self.running and not self.ws_ready:
            self._refresh_bbo_from_rest()
            if not self.ws_ready:
                time.sleep(0.5)
        threading.Thread(target=self._sync_thread, daemon=True).start()

        if headless:
            self._trading_loop(live=None)
        else:
            # Rendering is deliberately slower than the quote engine; Rich should
            # never compete with signing/WS callbacks on the hot path.
            with Live(self.dashboard(), console=self.console, refresh_per_second=2, screen=False) as live:
                self._trading_loop(live=live)

        # graceful exit — stop = flat: annule tout PUIS ferme la position au market.
        if not headless:
            print("\n🛑 Stopping — cancelling all orders + closing position...")
        self.cancel_all()
        try:
            self.flatten("stop")            # IOC reduce-only, ne fait rien si flat
        except Exception:
            pass
        net = self.session_pnl - self.total_fees
        if not headless:
            print(f"Session: {self.fills_count} fills | volume ${self.total_volume:,.0f} | "
                f"gross ${self.session_pnl:+.3f} | fees ${self.total_fees:.3f} | NET ${net:+.3f}")


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _clamp01(value: float) -> float:
    return 0.0 if value < 0.0 else (1.0 if value > 1.0 else value)


def _fmt_usd(value: float) -> str:
    if value >= 1_000_000:
        return f"${value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"${value / 1_000:.0f}k"
    return f"${value:.0f}"


def _fmt_bps(value: Optional[float]) -> str:
    if value is None or not math.isfinite(value):
        return "-"
    return f"{value:.1f}"


def _market_key(symbol: str) -> str:
    return str(symbol).split(":", 1)[-1].upper()


def _market_mid(mids: Dict[str, Any], symbol: str) -> float:
    base = _market_key(symbol)
    return _safe_float(mids.get(symbol), 0.0) or _safe_float(mids.get(base), 0.0)


def _derive_tick_size(mid: float) -> float:
    """Approximate Hyperliquid perp price tick from the 5-significant-figure rule."""
    if mid <= 0 or not math.isfinite(mid):
        return 0.01
    exp = math.floor(math.log10(abs(mid))) - 4
    return max(1e-6, 10.0 ** exp)


def _round_up_to_step(value: float, step: float, decimals: int) -> float:
    if step <= 0:
        return value
    rounded = math.ceil((value - 1e-12) / step) * step
    return round(rounded, max(0, decimals))


def _dynamic_token_config(item: Dict[str, Any], mids: Dict[str, Any]) -> Optional[Tuple[str, Dict[str, Any]]]:
    symbol = str(item.get("name", "")).strip()
    if not symbol or item.get("isDelisted", False):
        return None
    key = _market_key(symbol)
    sz_decimals = max(0, int(_safe_float(item.get("szDecimals"), 4.0)))
    min_size = 10.0 ** (-sz_decimals) if sz_decimals > 0 else 1.0
    mid = _market_mid(mids, symbol)
    tick_size = _derive_tick_size(mid)
    raw_size = (TARGET_ORDER_NOTIONAL_USD / mid) if mid > 0 else min_size
    order_size = max(min_size, _round_up_to_step(raw_size, min_size, sz_decimals))
    is_cross = not bool(item.get("onlyIsolated")) and str(item.get("marginMode", "")).lower() != "nocross"
    cfg = {
        "symbol": symbol,
        "tick_size": tick_size,
        "min_size": min_size,
        "order_size": order_size,
        "is_cross": is_cross,
        "max_leverage": int(_safe_float(item.get("maxLeverage"), LEVERAGE) or LEVERAGE),
        "api_mid": mid,
        "sz_decimals": sz_decimals,
    }
    if key in TOKENS:
        merged = dict(TOKENS[key])
        for cfg_key, cfg_value in cfg.items():
            if cfg_key in ("tick_size", "min_size", "order_size"):
                merged.setdefault(cfg_key, cfg_value)
            else:
                merged[cfg_key] = cfg_value
        cfg = merged
    return key, cfg


def _fetch_hip3_meta() -> List[Dict[str, Any]]:
    import requests
    res = []
    for d in [PERP_DEX, "mkts"]:
        try:
            raw = requests.post(BASE_URL + "/info", json={"type": "meta", "dex": d}, timeout=12).json()
            universe = raw.get("universe", []) if isinstance(raw, dict) else []
            res.extend([
                item for item in universe
                if isinstance(item, dict) and item.get("name") and not item.get("isDelisted", False)
            ])
        except Exception:
            pass
    return res


def _fetch_all_mids() -> Dict[str, Any]:
    import requests
    res = {}
    for d in [PERP_DEX, "mkts"]:
        try:
            raw = requests.post(BASE_URL + "/info", json={"type": "allMids", "dex": d}, timeout=12).json()
            if isinstance(raw, dict):
                res.update(raw)
        except Exception:
            pass
    return res


def _fetch_candles(symbol: str) -> List[Dict[str, float]]:
    import requests
    need_min = VP_BARS + ATR_PERIOD + 5
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - need_min * 60_000 - 60_000
    raw = requests.post(
        BASE_URL + "/info",
        json={"type": "candleSnapshot", "req": {
            "coin": symbol,
            "interval": CANDLE_INTERVAL,
            "startTime": start_ms,
            "endTime": now_ms,
        }},
        timeout=12,
    ).json()
    if not isinstance(raw, list):
        return []
    bars: List[Dict[str, float]] = []
    for c in raw:
        try:
            bars.append({
                "ts": float(c["t"]) / 1000.0,
                "o": float(c["o"]),
                "h": float(c["h"]),
                "l": float(c["l"]),
                "c": float(c["c"]),
                "v": float(c.get("v", 0.0)),
                "n": float(c.get("n", 0.0)),
            })
        except (KeyError, TypeError, ValueError):
            continue
    return bars


def _score_grid_market(item: Dict[str, Any], mids: Dict[str, Any]) -> Dict[str, Any]:
    symbol = str(item.get("name", ""))
    key = _market_key(symbol)
    mid = _market_mid(mids, symbol)
    tick_size = _derive_tick_size(mid)
    result: Dict[str, Any] = {
        "key": key,
        "symbol": symbol,
        "score": 0.0,
        "verdict": "NO_DATA",
        "mid": mid,
        "volume_usd": 0.0,
        "trades": 0.0,
        "trend_score": None,
        "er": None,
        "vp_expansion": None,
        "atr_bps": None,
        "va_bps": None,
        "range_bps": None,
        "max_leverage": int(_safe_float(item.get("maxLeverage"), 0.0)),
        "is_cross": not bool(item.get("onlyIsolated")) and str(item.get("marginMode", "")).lower() != "nocross",
    }
    try:
        bars = _fetch_candles(symbol)
        if len(bars) < MIN_BARS:
            result["verdict"] = f"NO_DATA {len(bars)} bars"
            return result

        engine = RegimeEngine(tick_size=tick_size)
        engine.update(bars)
        closes = [b["c"] for b in bars if b.get("c", 0.0) > 0]
        ref_mid = mid or (closes[-1] if closes else 0.0)
        sample = bars[-VP_BARS:]
        volume_usd = sum(max(0.0, b.get("v", 0.0)) * max(0.0, b.get("c", 0.0)) for b in sample)
        trades = sum(max(0.0, b.get("n", 0.0)) for b in sample)
        lo = min(b["l"] for b in sample)
        hi = max(b["h"] for b in sample)
        range_bps = ((hi - lo) / ref_mid * 10000.0) if ref_mid > 0 else None
        atr_bps = (engine.atr / ref_mid * 10000.0) if ref_mid > 0 and engine.atr > 0 else None
        va_bps = ((engine.vah - engine.val) / ref_mid * 10000.0
                if ref_mid > 0 and engine.vah is not None and engine.val is not None else None)

        compression = 1.0 - engine.vp_expansion
        chop = 1.0 - engine.trend_score
        er_chop = 1.0 - engine.efficiency_ratio
        breakout_safety = 1.0 - engine.breakout_score
        vol_score = _clamp01((math.log10(volume_usd + 1.0) - 3.0) / 4.0)
        trade_score = _clamp01((math.log10(trades + 1.0) - 1.0) / 3.0)
        activity = 0.70 * vol_score + 0.30 * trade_score
        if atr_bps is None or atr_bps <= 0:
            atr_score = 0.0
        elif atr_bps < 0.3:
            atr_score = 0.50 * atr_bps / 0.3
        elif atr_bps <= 8.0:
            atr_score = 1.0
        else:
            atr_score = max(0.0, 1.0 - (atr_bps - 8.0) / 20.0)

        score = 100.0 * (
            0.32 * compression +
            0.27 * chop +
            0.16 * er_chop +
            0.12 * breakout_safety +
            0.08 * activity +
            0.05 * atr_score
        )
        if volume_usd < MARKET_SCAN_MIN_VOLUME_USD:
            score -= 25.0
        if trades < 5:
            score -= 15.0
        if engine.trend_score >= ENTER_TREND_SCORE:
            score -= 15.0
        if engine.breakout_score > 0.25:
            score -= 10.0
        score = max(0.0, min(100.0, score))

        if volume_usd < MARKET_SCAN_MIN_VOLUME_USD:
            verdict = "THIN"
        elif engine.trend_score > 0.55 or engine.breakout_score > 0.40:
            verdict = "AVOID trend"
        elif engine.vp_expansion < 0.35 and engine.efficiency_ratio < 0.35 and engine.trend_score < ENTER_TREND_SCORE:
            verdict = "GOOD compression"
        elif engine.trend_score < ENTER_TREND_SCORE:
            verdict = "OK range"
        else:
            verdict = "MIXED"

        result.update({
            "score": score,
            "verdict": verdict,
            "mid": ref_mid,
            "volume_usd": volume_usd,
            "trades": trades,
            "trend_score": engine.trend_score,
            "er": engine.efficiency_ratio,
            "vp_expansion": engine.vp_expansion,
            "atr_bps": atr_bps,
            "va_bps": va_bps,
            "range_bps": range_bps,
        })
        return result
    except Exception as exc:
        result["verdict"] = f"ERR {str(exc)[:28]}"
        return result


def scan_hip3_markets() -> List[Dict[str, Any]]:
    markets = _fetch_hip3_meta()
    if MARKET_SCAN_MAX_MARKETS:
        markets = markets[:MARKET_SCAN_MAX_MARKETS]
    mids = _fetch_all_mids()

    dynamic_tokens: Dict[str, Dict[str, Any]] = {}
    for item in markets:
        built = _dynamic_token_config(item, mids)
        if built is not None:
            key, cfg = built
            dynamic_tokens[key] = cfg
    TOKENS.update(dynamic_tokens)

    results: List[Dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=MARKET_SCAN_WORKERS) as pool:
        futures = {pool.submit(_score_grid_market, item, mids): item for item in markets}
        for future in as_completed(futures):
            results.append(future.result())
    results.sort(key=lambda row: (-float(row.get("score", 0.0)), str(row.get("key", ""))))
    return results


def render_market_scan(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    liquid_results = [
        row for row in results
        if float(row.get("volume_usd", 0.0)) >= MARKET_SCAN_MIN_VOLUME_USD
    ]
    shown = liquid_results[:MARKET_SCAN_TOP_N]
    dropped = len(results) - len(liquid_results)
    console = Console(width=150)
    table = Table(
        title=(
            f"Recap avant choix - HIP-3 {PERP_DEX} "
            f"({len(results)} analyses, {dropped} retires < {_fmt_usd(MARKET_SCAN_MIN_VOLUME_USD)} / 90m)"
        ),
        box=box.ASCII,
        safe_box=True,
    )
    table.add_column("#", justify="right", no_wrap=True)
    table.add_column("Market", no_wrap=True)
    table.add_column("View", no_wrap=True)
    table.add_column("Score", justify="right", no_wrap=True)
    table.add_column("VPexp", justify="right", no_wrap=True)
    table.add_column("ER", justify="right", no_wrap=True)
    table.add_column("Trend", justify="right", no_wrap=True)
    table.add_column("ATR bps", justify="right", no_wrap=True)
    table.add_column("VA bps", justify="right", no_wrap=True)
    table.add_column("Vol 90m", justify="right", no_wrap=True)
    table.add_column("Lev", justify="right", no_wrap=True)

    for idx, row in enumerate(shown, 1):
        verdict = str(row.get("verdict", ""))
        style = "green" if verdict.startswith("GOOD") else ("yellow" if verdict.startswith("OK") else "red")
        mode = "C" if row.get("is_cross", True) else "I"
        lev = int(_safe_float(row.get("max_leverage"), 0.0))
        table.add_row(
            str(idx),
            str(row.get("symbol", row.get("key", ""))),
            f"[{style}]{verdict}[/{style}]",
            f"{float(row.get('score', 0.0)):.0f}",
            _fmt_bps(row.get("vp_expansion")),
            _fmt_bps(row.get("er")),
            _fmt_bps(row.get("trend_score")),
            _fmt_bps(row.get("atr_bps")),
            _fmt_bps(row.get("va_bps")),
            _fmt_usd(float(row.get("volume_usd", 0.0))),
            f"{lev}x {mode}",
        )
    console.print(table)
    console.print(
        f"[dim]Filtre volume: Vol 90m >= {_fmt_usd(MARKET_SCAN_MIN_VOLUME_USD)}. "
        "VPexp bas + ER bas = compression/range. ATR bps calibre le spread. C=cross, I=isolated.[/dim]"
    )
    return shown


def print_all_market_names() -> None:
    keys = sorted(TOKENS.keys())
    print("Marches disponibles:")
    for i in range(0, len(keys), 8):
        print("  " + "  ".join(f"{k:>10}" for k in keys[i:i + 8]))


def _resolve_token_choice(choice: str, shown: List[Dict[str, Any]]) -> Optional[str]:
    text = choice.strip().upper()
    if not text and shown:
        return str(shown[0]["key"])
    try:
        idx = int(text)
        if 1 <= idx <= len(shown):
            return str(shown[idx - 1]["key"])
    except ValueError:
        pass

    aliases = {
        "SKHXUSD": "SKHX",
        "EURUSD": "EUR",
        "JPYUSD": "JPY",
        "WTIOIL": "CL",
    }
    alias_target = aliases.get(text)
    if alias_target in TOKENS:
        return alias_target
    if text in TOKENS:
        return text
    if ":" in text:
        base = _market_key(text)
        if base in TOKENS:
            return base
    for key, cfg in TOKENS.items():
        if str(cfg.get("symbol", "")).upper() == text:
            return key
    return None


def prompt_token() -> str:
    """Ask at launch which token to trade. Returns a key of TOKENS."""
    shown: List[Dict[str, Any]] = []
    scan_succeeded = False
    if MARKET_SCAN_ENABLED:
        try:
            print(f"Scan des marches HIP-3 {PERP_DEX} via API (meta + candles + ATR/VP)...")
            scan_results = scan_hip3_markets()
            scan_succeeded = True
            shown = render_market_scan(scan_results)
        except Exception as exc:
            print(f"Scan API indisponible ({exc}); retour a la liste locale.")
    else:
        print("Scan API desactive par MM_MARKET_SCAN=0.")

    if not shown and not scan_succeeded:
        print("Choix du token :")
        token_keys = list(TOKENS.keys())
        for i, tok in enumerate(token_keys, 1):
            print(f"  [{i}] {tok}")
    elif shown:
        print("Choix: numero du recap, nom exact (ex: AAPL ou xyz:AAPL), L pour tout lister.")
    else:
        print(f"Aucun marche ne passe le filtre volume {_fmt_usd(MARKET_SCAN_MIN_VOLUME_USD)} / 90m.")
        print("Baisse MM_MARKET_SCAN_MIN_VOLUME_USD ou tape un symbole exact pour forcer.")

    while True:
        if shown:
            prompt = f"Marche ? [1-{len(shown)} / NOM / Enter=meilleur] : "
        elif scan_succeeded:
            prompt = "Marche exacte ? [NOM ou L] : "
        else:
            prompt = f"Token ? [1-{len(TOKENS)} ou NOM] : "
        choice = input(prompt).strip().upper()
        if choice == "L":
            print_all_market_names()
            continue
        if not shown:
            token_keys = list(TOKENS.keys())
            try:
                idx = int(choice)
                if 1 <= idx <= len(token_keys):
                    return token_keys[idx - 1]
            except ValueError:
                pass
        resolved = _resolve_token_choice(choice, shown)
        if resolved:
            return resolved
        print("Entree invalide - tape un numero du recap, un symbole exact, ou L.")

    print("Choix du token :")
    token_keys = list(TOKENS.keys())
    for i, tok in enumerate(token_keys, 1):
        print(f"  [{i}] {tok}")
    while True:
        choice = input(f"Token ? [1-{len(token_keys)} ou NOM] : ").strip().upper()
        if choice in TOKENS:
            return choice
        # Allow specifying by SKHXUSD if SKHX is wanted, etc.
        if choice == "SKHXUSD":
            return "SKHX"
        try:
            idx = int(choice)
            if 1 <= idx <= len(token_keys):
                return token_keys[idx - 1]
        except ValueError:
            pass
        print(f"⚠️  Entrée invalide — tape un numéro (1-{len(token_keys)}) ou le nom exact.")


if __name__ == "__main__":
    # Keyboard choice of the traded token (sets the per-token market params).
    token = prompt_token()
    cfg = TOKENS[token]
    SYMBOL     = cfg["symbol"]
    TICK_SIZE  = cfg["tick_size"]
    MIN_SIZE   = cfg["min_size"]
    ORDER_SIZE = cfg["order_size"]
    IS_CROSS   = cfg.get("is_cross", True)
    max_lev = int(_safe_float(cfg.get("max_leverage"), LEVERAGE) or LEVERAGE)
    LEVERAGE = max(1, min(LEVERAGE, max_lev))
    # Recompute the derived rounding precisions for the chosen token.
    PRICE_DECIMALS = max(0, -int(math.floor(math.log10(TICK_SIZE)))) if TICK_SIZE < 1 else 0
    SIZE_DECIMALS  = max(0, -int(math.floor(math.log10(MIN_SIZE)))) if MIN_SIZE < 1 else 0

    if not acquire_instance_lock(SYMBOL):
        sys.exit(f"Another MM_GRID_ADAPTIVE instance already trades {SYMBOL}; launch refused.")

    if len(sys.argv) > 1:
        print("Extra mode arguments are ignored; this build runs adaptive regime-gated only.")
    mode_name = f"ADAPTIVE regime-gated / quote {GRID_QUOTE_MODE}"
    print("=" * 70)
    print(f" GRID MARKET MAKER - ADAPTIVE GRID  (regime filter {'active' if TREND_FILTER_ENABLED else 'disabled'})")
    margin_mode = "cross" if IS_CROSS else "isolated"
    if GRID_QUOTE_MODE == "bbo_offset":
        spread_mode = f"BBO offset {BBO_OFFSET_BPS}bps/side"
    else:
        spread_mode = f"{GRID_QUOTE_MODE} max({BASE_SPREAD_BPS}bps, fee floor, ATR)"
    print(f" {SYMBOL} | {LEVERAGE}x {margin_mode} | mode {mode_name} | grid {NUM_LEVELS}x{ORDER_SIZE} | "
          f"spread {spread_mode} | cap {MAX_INVENTORY_ORDERS} orders/side")
    print(f" tick {TICK_SIZE} | min_size {MIN_SIZE} | order_notional_target ${TARGET_ORDER_NOTIONAL_USD:.2f}")
    print("=" * 70)
    GridMarketMaker().run()
