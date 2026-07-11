"""
================================================================================
OBI VOLUME BOT — Order-Book-Imbalance market maker  (Hyperliquid HIP-3)
================================================================================
Bot de volume maker-only qui se cale sur le DÉSÉQUILIBRE DU CARNET :

    OBI = (Σ w_i·bid_sz_i − Σ w_i·ask_sz_i) / (Σ w_i·bid_sz_i + Σ w_i·ask_sz_i)

sur les N premiers niveaux L2 (pondération décroissante), lissé en EWMA,
combiné au flux agresseur (trades) sur fenêtre glissante :

    signal = 0.65·OBI_ewma + 0.35·flow_imbalance          ∈ [−1, +1]

Logique de quote (100% ALO / post-only, jamais taker en entrée) :
  signal >> 0  (pression acheteuse, le prix va ticker UP)
      → quoter le BID au meilleur bid (un fill = achat juste avant la hausse,
        markout favorable) ; RETIRER l'ask (éviter d'être lifté avant la hausse)
  signal << 0  → miroir : quoter l'ASK seulement
  signal ~ 0   → quoter les DEUX côtés au BBO (volume max en carnet équilibré)
  entre les deux → les deux côtés, le côté défavorisé reculé de quelques ticks

Inventaire : cap à MAX_INV_ORDERS × taille ; le côté qui aggrave est supprimé,
le côté qui débouche est reduce-only. Position trop vieille ou markout trop
adverse → sortie passive forcée puis IOC borné.

Sécurité (mêmes règles que les autres bots du projet) :
  - sweep des ordres orphelins de NOTRE coin au démarrage
  - sticky quotes (remplacement seulement si dérive > tolérance) → budget
    rate-limit HL (10 000 + volume USDC) préservé, priorité de queue gardée
  - STOP (Ctrl+C / SIGTERM) = cancel all + flatten market, toujours
  - watchdog WS : fallback REST, reconnexion, flatten si feed mort avec position
  - verrou d'instance par symbole (pas deux bots sur le même token)

Lancement :
    python OBI_VOLUME_BOT.py                # prompt token, DRY-RUN par défaut
    python OBI_VOLUME_BOT.py --live         # ordres réels
    python OBI_VOLUME_BOT.py --token SP500 --live
Credentials : PRIVATE_KEY / WALLET_ADDRESS dans .env (partagé avec le grid).
================================================================================
"""

import os
import sys
import json
import time
import math
import signal
import argparse
import tempfile
import threading
from collections import deque
from typing import Optional, List, Dict, Any, Tuple

# Windows: stdout redirigé = cp1252 → crash sur les emojis. Forcer UTF-8.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import eth_account
from hyperliquid.info import Info
from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants

from rich.console import Console
from rich.table import Table
from rich.live import Live
from rich.panel import Panel
from rich import box

# =============================================================================
# CONFIGURATION
# =============================================================================
from dotenv import load_dotenv
load_dotenv()
AGENT_PRIVATE_KEY = os.getenv("PRIVATE_KEY", "")
ACCOUNT_ADDRESS   = os.getenv("WALLET_ADDRESS", "")
if not AGENT_PRIVATE_KEY or not ACCOUNT_ADDRESS:
    sys.exit("❌ PRIVATE_KEY / WALLET_ADDRESS manquants — mets-les dans le .env")
BASE_URL = constants.MAINNET_API_URL

PERP_DEXS = ["xyz", "mkts"]
LEVERAGE  = max(1, min(50, int(float(os.getenv("OBI_LEVERAGE", "20")))))
IS_CROSS  = True

# --- Tokens presets (tick/min viennent du meta live si absent d'ici) ---
TOKENS: Dict[str, Dict[str, Any]] = {
    "SP500":  {"symbol": "xyz:SP500"},
    "XYZ100": {"symbol": "xyz:XYZ100"},
    "SPCX":   {"symbol": "xyz:SPCX"},
    "GOLD":   {"symbol": "xyz:GOLD"},
    "SILVER": {"symbol": "xyz:SILVER"},
    "NVDA":   {"symbol": "xyz:NVDA"},
    "TSLA":   {"symbol": "xyz:TSLA"},
    "US500":  {"symbol": "mkts:US500"},
    # SK Hynix — nom wire "xyz:SKHX" (le frontend affiche SKHXUSD), max 10x
    "SKHX":    {"symbol": "xyz:SKHX"},
    "SKHXUSD": {"symbol": "xyz:SKHX"},
}
DEFAULT_TOKEN = os.getenv("OBI_TOKEN", "SP500")

# --- Sizing ---
ORDER_NOTIONAL_USD = max(10.5, float(os.getenv("OBI_ORDER_NOTIONAL_USD", "100")))
MAX_INV_ORDERS     = max(1, int(float(os.getenv("OBI_MAX_INV_ORDERS", "2"))))

# --- Signal ---
OBI_DEPTH_LEVELS   = 5      # niveaux L2 par côté
OBI_LEVEL_DECAY    = 0.85   # poids w_i = decay^i (le top of book pèse le plus)
OBI_EWMA_TAU_SEC   = 1.2    # lissage temporel du signal book
FLOW_WINDOW_SEC    = 10.0   # fenêtre du flux agresseur
OBI_WEIGHT         = 0.50
FLOW_WEIGHT        = 0.30
TILT_WEIGHT        = 0.20   # microprice tilt (déplacement du prix pondéré tailles)
TILT_NORM_BPS      = 1.0    # tilt de ±1 bps = composante saturée à ±1
TH_NEUTRAL         = 0.12   # |signal| en-dessous: carnet équilibré, 2 côtés au BBO
TH_STRONG          = 0.35   # |signal| au-dessus: 1 seul côté quoté
BACKOFF_TICKS      = 2      # recul du côté défavorisé dans la zone intermédiaire

# --- Anti-adverse-selection (le cœur de la profitabilité) ---
# 1) MARKOUT par côté: mid à +6s vs prix du fill. EWMA négative = ce côté se
#    fait courir dessus → recul, puis retrait complet (probation).
MARKOUT_HORIZON_SEC = 6.0
MARKOUT_EWMA_ALPHA  = 0.30
MARKOUT_SOFT_BPS    = -0.5   # en-dessous: quote reculée de MARKOUT_EXTRA_TICKS
MARKOUT_HARD_BPS    = -1.5   # en-dessous: côté retiré tant que l'EWMA ne remonte pas
MARKOUT_EXTRA_TICKS = 3
MARKOUT_DECAY_TAU   = 180.0  # sans nouveau fill, l'EWMA revient doucement vers 0
# 2) MOMENTUM court: on ne pose pas d'ordre OUVRANT du côté qui se fait
#    balayer (pas de bid dans un couteau qui tombe, pas d'ask dans un rip).
MOM_FAST_SEC        = 3.0
MOM_SLOW_SEC        = 12.0
MOM_GATE_BPS        = 2.5
# 3) SWEEP breaker: ≥2 fills du même côté en <5s = flux informé qui nous
#    traverse → ce côté est retiré 8s. + cooldown après chaque fill.
BURST_WINDOW_SEC    = 5.0
BURST_PAUSE_SEC     = 8.0
FILL_COOLDOWN_SEC   = 2.5
# 4) EV GATE: capture attendue (spread/2 + recul) ≥ frais + adverse prédit
#    (markout) + marge. Sinon on recule jusqu'à ce que ça paie, ou on retire.
MIN_EV_BPS          = 0.10
EV_MAX_EXTRA_TICKS  = 5
# 5) VOL widen: si la vol court-terme dépasse la moitié du spread, on recule.
VOL_TAU_SEC         = 20.0
VOL_WIDEN_MULT      = 1.0
# 6) Skew d'inventaire (A-S lite): plus on est chargé, plus le côté qui
#    ajoute est reculé.
INV_SKEW_TICKS      = 2
# 7) Sortie ancrée: le débouclage vise entry ± PROFIT_TICKS tant que la
#    position est jeune, au lieu de scratcher systématiquement au BBO.
PROFIT_TICKS            = 2
UNWIND_ANCHOR_MAX_SEC   = 60.0

# --- Exécution / churn ---
REPLACE_TOL_TICKS   = 1     # ne remplace un ordre que si la cible dérive de + que ça
ACTION_COALESCE_SEC = 0.20  # min entre deux actions d'ordres signées
POST_ONLY_MAX_RETRIES = 2   # reprice 1 tick et retente si l'ALO croiserait
MAX_SPREAD_TICKS    = 40    # spread book au-delà: marché cassé, on ne quote pas

# --- Risque ---
STOP_ADVERSE_BPS   = 20.0   # uPnL < −20 bps du notionnel → flatten IOC + pause
STOP_PAUSE_SEC     = 60.0
MAX_HOLD_SEC       = 120.0  # position plus vieille → débouclage passif forcé
FORCE_FLAT_EXTRA   = 45.0   # ... et IOC si toujours pas flat après ce délai en +
MAX_SESSION_LOSS_USD = float(os.getenv("OBI_MAX_SESSION_LOSS_USD", "5"))
DEADMAN_FEED_SEC   = 12.0   # feed muet + position → REST refresh sinon flatten
WS_RESTART_COOLDOWN = 30.0
SYNC_EVERY_SEC     = 10.0   # resync REST position/equity
RECONCILE_EVERY_SEC = 20.0  # resync REST des ordres ouverts (anti-fantômes)
ORDER_RECONCILE_GRACE_SEC = 3.0

MAKER_FEE_BPS = 0.288       # xyz: 28.8 $/M par fill

# --- Globals instrument (résolus au lancement) ---
SYMBOL         = "xyz:SP500"
SZ_DECIMALS    = 3
MIN_SIZE       = 0.001
PRICE_DECIMALS = 1


# =============================================================================
# HELPERS
# =============================================================================
def dyn_tick(px: float) -> float:
    """Tick réel HL au prix courant : 5 chiffres significatifs, plafonné par
    (6 − szDecimals) décimales. Recalculé à chaque quote (survit aux
    changements de décade, contrairement à un tick_size codé en dur)."""
    if px <= 0:
        return 10.0 ** (-PRICE_DECIMALS)
    sig_tick = 10.0 ** (math.floor(math.log10(px)) + 1 - 5)
    return max(sig_tick, 10.0 ** (-PRICE_DECIMALS))


def px_down(px: float, tick: float) -> float:
    return round(math.floor(round(px / tick, 9)) * tick, PRICE_DECIMALS)


def px_up(px: float, tick: float) -> float:
    return round(math.ceil(round(px / tick, 9)) * tick, PRICE_DECIMALS)


def round_sz(sz: float) -> float:
    return round(sz, SZ_DECIMALS)


def _safe_symbol(symbol: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in symbol)


def stats_file_path(symbol: str) -> str:
    """Fichier JSON de stats live, écrit ~1x/s par le bot et lu par le
    dashboard (bot_server) — c'est le canal de télémétrie inter-process."""
    return os.path.join(tempfile.gettempdir(), f"hl_obi_stats_{_safe_symbol(symbol)}.json")


def stop_file_path(symbol: str) -> str:
    """Fichier sentinelle: sa présence demande un arrêt PROPRE (cancel all +
    flatten) au bot de ce symbole — utilisé par le dashboard pour stopper un
    process enfant sans le tuer brutalement."""
    return os.path.join(tempfile.gettempdir(), f"hl_obi_stop_{_safe_symbol(symbol)}.flag")


_LOCK_HANDLE = None

def acquire_instance_lock(symbol: str) -> bool:
    """Un seul bot par symbole (verrou fichier partagé avec la convention du
    grid bot: préfixe différent pour ne pas bloquer le grid sur un AUTRE
    token, mais on refuse le même coin en double)."""
    global _LOCK_HANDLE
    safe = "".join(c if c.isalnum() else "_" for c in symbol)
    path = os.path.join(tempfile.gettempdir(), f"hl_obi_bot_{safe}.lock")
    try:
        if os.name == "nt":
            import msvcrt
            _LOCK_HANDLE = open(path, "w")
            msvcrt.locking(_LOCK_HANDLE.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            _LOCK_HANDLE = open(path, "w")
            fcntl.flock(_LOCK_HANDLE, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def build_fixed_spot_meta() -> Optional[Dict[str, Any]]:
    """Workaround SDK: tokens spot ré-indexés dense (les delistings créent des
    trous d'index qui font planter Info()/Exchange())."""
    try:
        import requests
        raw = requests.post(BASE_URL + "/info", json={"type": "spotMeta"}, timeout=10).json()
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
        print(f"⚠️ build_fixed_spot_meta: {exc}; SDK par défaut")
        return None


def fetch_token_meta(symbol: str) -> Dict[str, Any]:
    """szDecimals + mid live depuis metaAndAssetCtxs du dex concerné."""
    import requests
    dex, bare = symbol.split(":", 1)
    data = requests.post(BASE_URL + "/info",
                         json={"type": "metaAndAssetCtxs", "dex": dex}, timeout=15).json()
    universe = data[0].get("universe", [])
    ctxs = data[1] if len(data) > 1 else []
    for i, u in enumerate(universe):
        name = str(u.get("name", ""))
        if name.upper().replace(f"{dex.upper()}:", "") == bare.upper():
            ctx = ctxs[i] if i < len(ctxs) else {}
            mid = float(ctx.get("midPx") or 0)
            return {"szDecimals": int(u.get("szDecimals", 0)), "mid": mid,
                    "maxLeverage": int(u.get("maxLeverage", 50) or 50)}
    raise ValueError(f"{symbol} introuvable dans le meta {dex}")


# =============================================================================
# SIGNAL ENGINE — OBI + flux agresseur
# =============================================================================
class SignalEngine:
    def __init__(self):
        self.lock = threading.Lock()
        self.obi_ewma = 0.0
        self._last_obi_ts = 0.0
        self.obi_raw = 0.0
        self.tilt_ewma = 0.0           # microprice tilt en bps (EWMA rapide)
        self.trades: deque = deque()   # (ts, signed_notional)
        self.flow = 0.0
        self.mids: deque = deque(maxlen=2000)   # (ts, mid) pour momentum/vol
        self.vol_bps_ewma = 0.0        # |Δmid 1s| en bps, lissé
        self._last_vol_ts = 0.0

    def on_l2(self, bids: List[Dict], asks: List[Dict]) -> None:
        num = 0.0
        den = 0.0
        for i in range(min(OBI_DEPTH_LEVELS, len(bids))):
            w = OBI_LEVEL_DECAY ** i
            num += w * float(bids[i]["sz"])
            den += w * float(bids[i]["sz"])
        for i in range(min(OBI_DEPTH_LEVELS, len(asks))):
            w = OBI_LEVEL_DECAY ** i
            num -= w * float(asks[i]["sz"])
            den += w * float(asks[i]["sz"])
        if den <= 0:
            return
        raw = max(-1.0, min(1.0, num / den))
        now = time.time()

        # Microprice tilt: où le prix "penche" selon les tailles au top of book.
        b0_px, b0_sz = float(bids[0]["px"]), float(bids[0]["sz"])
        a0_px, a0_sz = float(asks[0]["px"]), float(asks[0]["sz"])
        mid = (b0_px + a0_px) / 2.0
        tilt_bps = 0.0
        if b0_sz + a0_sz > 0 and mid > 0:
            micro = (b0_px * a0_sz + a0_px * b0_sz) / (b0_sz + a0_sz)
            tilt_bps = (micro - mid) / mid * 10000.0

        with self.lock:
            dt = max(1e-3, now - self._last_obi_ts) if self._last_obi_ts else OBI_EWMA_TAU_SEC
            alpha = 1.0 - math.exp(-dt / OBI_EWMA_TAU_SEC)
            self.obi_ewma += alpha * (raw - self.obi_ewma)
            self.tilt_ewma += alpha * (tilt_bps - self.tilt_ewma)
            self.obi_raw = raw
            self._last_obi_ts = now
            self.mids.append((now, mid))
            # Vol court-terme: |variation du mid sur ~1s| en bps, EWMA.
            if now - self._last_vol_ts >= 1.0:
                ref = None
                for ts, m in reversed(self.mids):
                    if now - ts >= 1.0:
                        ref = m
                        break
                if ref and ref > 0:
                    r = abs(mid - ref) / ref * 10000.0
                    a = 1.0 - math.exp(-max(1.0, now - self._last_vol_ts) / VOL_TAU_SEC)
                    self.vol_bps_ewma += a * (r - self.vol_bps_ewma)
                self._last_vol_ts = now

    def on_trade(self, side: str, px: float, sz: float, ts: float) -> None:
        # "B" = agresseur acheteur (trade au ask), sinon agresseur vendeur.
        signed = px * sz if side == "B" else -px * sz
        with self.lock:
            self.trades.append((ts, signed))

    def _refresh_flow(self) -> None:
        cutoff = time.time() - FLOW_WINDOW_SEC
        while self.trades and self.trades[0][0] < cutoff:
            self.trades.popleft()
        tot = sum(abs(v) for _, v in self.trades)
        self.flow = (sum(v for _, v in self.trades) / tot) if tot > 0 else 0.0

    def _combined_locked(self) -> float:
        tilt_c = max(-1.0, min(1.0, self.tilt_ewma / TILT_NORM_BPS))
        return max(-1.0, min(1.0, OBI_WEIGHT * self.obi_ewma
                             + FLOW_WEIGHT * self.flow + TILT_WEIGHT * tilt_c))

    def combined(self) -> float:
        with self.lock:
            self._refresh_flow()
            return self._combined_locked()

    def momentum_bps(self, window_sec: float) -> float:
        """Δmid sur la fenêtre, en bps (+ = ça monte)."""
        now = time.time()
        with self.lock:
            if not self.mids:
                return 0.0
            latest = self.mids[-1][1]
            ref = None
            for ts, m in self.mids:
                if now - ts <= window_sec:
                    ref = m
                    break
            if ref is None or ref <= 0:
                return 0.0
            return (latest - ref) / ref * 10000.0

    def vol_bps(self) -> float:
        with self.lock:
            return self.vol_bps_ewma

    def snapshot(self) -> Tuple[float, float, float, float]:
        with self.lock:
            self._refresh_flow()
            return self.obi_ewma, self.flow, self.tilt_ewma, self._combined_locked()


# =============================================================================
# LE BOT
# =============================================================================
class OBIVolumeBot:
    def __init__(self, live: bool):
        self.live = live
        self.lock = threading.Lock()
        self.wallet = eth_account.Account.from_key(AGENT_PRIVATE_KEY)
        self.dex = SYMBOL.split(":", 1)[0]
        self.fixed_spot_meta = build_fixed_spot_meta()
        self.info = Info(BASE_URL, skip_ws=True, spot_meta=self.fixed_spot_meta, perp_dexs=PERP_DEXS)
        self.exchange = Exchange(self.wallet, base_url=BASE_URL, account_address=ACCOUNT_ADDRESS,
                                 spot_meta=self.fixed_spot_meta, perp_dexs=PERP_DEXS)
        self.signal = SignalEngine()

        # Market state (WS)
        self.best_bid = 0.0
        self.best_ask = 0.0
        self.mid = 0.0
        self.ws_ready = False
        self.last_tick_ts = 0.0
        self.last_ws_restart = 0.0

        # Account state
        self.position = 0.0
        self.entry_price = 0.0
        self.unrealized_pnl = 0.0
        self.equity = 0.0
        self.spot_usdc = 0.0
        self.pos_since = 0.0          # depuis quand la position est non nulle

        # Anti-adverse: markout par côté + sweep breaker
        self.markout_bps: Dict[str, float] = {"B": 0.0, "A": 0.0}
        self.pending_markouts: deque = deque()          # (ts, side, px)
        self.side_fills: Dict[str, deque] = {"B": deque(), "A": deque()}
        self.side_pause_until: Dict[str, float] = {"B": 0.0, "A": 0.0}
        self.last_side_fill_ts: Dict[str, float] = {"B": 0.0, "A": 0.0}
        self.last_markout_decay = time.time()

        # Orders: oid -> {"side": "B"/"A", "px": float, "sz": float}
        self.orders: Dict[int, Dict[str, Any]] = {}
        self.last_intent: Optional[tuple] = None
        self.last_quote_ts = 0.0
        self.last_action_ts = 0.0
        self.last_sync_ts = 0.0
        self.last_reconcile_ts = 0.0
        self.pause_until = 0.0

        # Stats
        self.session_pnl = 0.0
        self.total_fees = 0.0
        self.total_volume = 0.0
        self.fills_count = 0
        self.maker_fills = 0
        self.processed_fills: set = set()
        self.last_fill_ts = 0.0
        self.last_action = "init"
        self.last_error = ""
        self.start_time = time.time()

        self.running = True
        self.wake = threading.Event()
        self.console = Console()

        # Canal inter-process avec le dashboard: stats sortantes + stop entrant.
        self._stats_file = stats_file_path(SYMBOL)
        self._stop_file = stop_file_path(SYMBOL)
        self._last_stats_write = 0.0
        try:
            if os.path.exists(self._stop_file):
                os.remove(self._stop_file)   # stopfile d'un run précédent
        except Exception:
            pass
        try:
            signal.signal(signal.SIGINT, self._sig_shutdown)
            signal.signal(signal.SIGTERM, self._sig_shutdown)
        except ValueError:
            pass  # pas dans le main thread (lancé par un dashboard)

    # ------------------------------------------------------------------ setup
    def _sig_shutdown(self, *_):
        self.running = False
        self.wake.set()

    def set_leverage(self):
        if not self.live:
            return
        try:
            self.exchange.update_leverage(LEVERAGE, SYMBOL, is_cross=IS_CROSS)
        except Exception as exc:
            print(f"⚠️ update_leverage: {exc}")

    def startup_sweep(self):
        """Annule les ordres orphelins de NOTRE coin laissés par un run
        précédent (crash / kill) — ce process ne les connaît pas et les
        laisserait pourrir au book sinon."""
        if not self.live:
            return
        try:
            oo = self.info.open_orders(ACCOUNT_ADDRESS, dex=self.dex)
            reqs = [{"coin": SYMBOL, "oid": int(o["oid"])}
                    for o in oo if self._coin_matches(o.get("coin"))]
            if reqs:
                self.exchange.bulk_cancel(reqs)
                print(f"🧹 {len(reqs)} ordre(s) orphelin(s) annulé(s) au démarrage")
        except Exception as exc:
            print(f"⚠️ startup sweep: {exc}")

    @staticmethod
    def _coin_matches(coin: Optional[str]) -> bool:
        if not coin:
            return False
        return coin == SYMBOL or SYMBOL.endswith(":" + coin)

    # ------------------------------------------------------------------- ws io
    def start_ws(self):
        self.ws_info = Info(BASE_URL, skip_ws=False, spot_meta=self.fixed_spot_meta, perp_dexs=PERP_DEXS)
        self.ws_info.subscribe({"type": "l2Book", "coin": SYMBOL}, self._on_l2)
        self.ws_info.subscribe({"type": "trades", "coin": SYMBOL}, self._on_trades)
        self.ws_info.subscribe({"type": "userFills", "user": ACCOUNT_ADDRESS}, self._on_user_fills)
        self.ws_info.subscribe({"type": "orderUpdates", "user": ACCOUNT_ADDRESS}, self._on_order_updates)

    def restart_ws(self):
        try:
            self.ws_info.disconnect_websocket()
        except Exception:
            pass
        self.ws_ready = False
        try:
            self.start_ws()
        except Exception as exc:
            self.last_action = f"WS reconnect failed: {str(exc)[:40]}"

    def _on_l2(self, data):
        try:
            levels = data.get("data", {}).get("levels", [])
            if len(levels) < 2 or not levels[0] or not levels[1]:
                return
            bb = float(levels[0][0]["px"])
            ba = float(levels[1][0]["px"])
            if bb <= 0 or ba <= bb:
                return
            with self.lock:
                self.best_bid, self.best_ask = bb, ba
                self.mid = (bb + ba) / 2.0
                self.last_tick_ts = time.time()
                self.ws_ready = True
            self.signal.on_l2(levels[0], levels[1])
            self.wake.set()
        except Exception:
            pass

    def _on_trades(self, data):
        try:
            for t in data.get("data", []) or []:
                ts = float(t.get("time", 0)) / 1000.0 or time.time()
                self.signal.on_trade(str(t.get("side", "")), float(t["px"]), float(t["sz"]), ts)
        except Exception:
            pass

    def _on_user_fills(self, data):
        try:
            payload = data.get("data", {})
            fills = payload.get("fills", []) or []
            if payload.get("isSnapshot"):
                with self.lock:
                    self.processed_fills.update(str(f.get("tid")) for f in fills)
                return
            with self.lock:
                for f in fills:
                    if not self._coin_matches(f.get("coin")):
                        continue
                    tid = str(f.get("tid"))
                    if tid in self.processed_fills:
                        continue
                    self.processed_fills.add(tid)
                    px, sz = float(f["px"]), float(f["sz"])
                    side = str(f.get("side", "")).upper()
                    direction = str(f.get("dir", "")).lower()
                    if side == "B" or direction in ("open long", "close short"):
                        new_pos = round(self.position + sz, SZ_DECIMALS)
                        sd = "B"
                    else:
                        new_pos = round(self.position - sz, SZ_DECIMALS)
                        sd = "A"
                    now_f = time.time()
                    # Markout différé + détection de sweep (≥2 fills même côté
                    # dans la fenêtre = flux informé, on retire ce côté).
                    self.pending_markouts.append((now_f, sd, px))
                    self.last_side_fill_ts[sd] = now_f
                    q = self.side_fills[sd]
                    q.append(now_f)
                    while q and q[0] < now_f - BURST_WINDOW_SEC:
                        q.popleft()
                    if len(q) >= 2:
                        self.side_pause_until[sd] = now_f + BURST_PAUSE_SEC
                    if abs(self.position) < MIN_SIZE / 2 and abs(new_pos) >= MIN_SIZE / 2:
                        self.pos_since = time.time()
                    self.position = new_pos
                    if abs(self.position) < MIN_SIZE / 2:
                        self.pos_since = 0.0
                    self.total_volume += px * sz
                    fee = float(f.get("fee", 0))
                    self.total_fees += fee
                    self.session_pnl += float(f.get("closedPnl", 0))
                    self.fills_count += 1
                    if not bool(f.get("crossed", False)):
                        self.maker_fills += 1
                    self.last_fill_ts = time.time()
            self.wake.set()
        except Exception:
            pass

    def _on_order_updates(self, data):
        try:
            payload = data.get("data", [])
            updates = payload if isinstance(payload, list) else payload.get("orders", [])
            terminal = ("fill", "cancel", "reject", "margin", "reduceonly", "badalo", "expired")
            changed = False
            with self.lock:
                for u in updates or []:
                    if not isinstance(u, dict):
                        continue
                    order = u.get("order", u)
                    if not self._coin_matches(order.get("coin")):
                        continue
                    oid = int(order.get("oid", u.get("oid", 0)) or 0)
                    if not oid:
                        continue
                    status = str(u.get("status", "")).lower().replace("_", "")
                    if any(m in status for m in terminal):
                        if oid in self.orders:
                            self.orders.pop(oid, None)
                            changed = True
                    elif status in {"open", "resting"} and oid not in self.orders:
                        self.orders[oid] = {
                            "side": "B" if order.get("side") == "B" else "A",
                            "px": float(order.get("limitPx", 0) or 0),
                            "sz": float(order.get("sz", 0) or 0),
                        }
                        changed = True
                if changed:
                    self.last_intent = None
            if changed:
                self.wake.set()
        except Exception:
            pass

    def refresh_bbo_rest(self) -> bool:
        try:
            book = self.info.l2_snapshot(SYMBOL)
            levels = book.get("levels", []) if isinstance(book, dict) else []
            if len(levels) < 2 or not levels[0] or not levels[1]:
                return False
            bb, ba = float(levels[0][0]["px"]), float(levels[1][0]["px"])
            if bb <= 0 or ba <= bb:
                return False
            with self.lock:
                self.best_bid, self.best_ask = bb, ba
                self.mid = (bb + ba) / 2.0
                self.last_tick_ts = time.time()
                self.ws_ready = True
            self.signal.on_l2(levels[0], levels[1])
            return True
        except Exception:
            return False

    # ------------------------------------------------------------ account sync
    def sync_position(self):
        try:
            started = time.time()
            st = self.info.user_state(ACCOUNT_ADDRESS, dex=self.dex)
            try:
                spot = self.info.spot_user_state(ACCOUNT_ADDRESS)
                spot_usdc = sum(max(0.0, float(b.get("total", 0) or 0) - float(b.get("hold", 0) or 0))
                                for b in spot.get("balances", []) if b.get("coin") == "USDC")
            except Exception:
                spot_usdc = self.spot_usdc
            target = SYMBOL.split(":")[-1]
            with self.lock:
                if self.last_fill_ts > started:
                    return
                perp_eq = float(st.get("marginSummary", {}).get("accountValue", 0) or 0)
                self.spot_usdc = spot_usdc
                self.equity = perp_eq + spot_usdc
                for entry in st.get("assetPositions", []):
                    pos = entry.get("position", {})
                    if pos.get("coin") in (SYMBOL, target):
                        old = self.position
                        self.position = float(pos.get("szi", 0))
                        self.unrealized_pnl = float(pos.get("unrealizedPnl", 0))
                        self.entry_price = float(pos.get("entryPx", 0) or 0)
                        if abs(old) < MIN_SIZE / 2 and abs(self.position) >= MIN_SIZE / 2 and not self.pos_since:
                            self.pos_since = time.time()
                        return
                self.position = 0.0
                self.unrealized_pnl = 0.0
                self.entry_price = 0.0
                self.pos_since = 0.0
        except Exception:
            pass

    def reconcile_orders(self):
        """Le snapshot REST des ordres ouverts est la seule vérité — les oids
        locaux peuvent contenir des fantômes (event WS raté)."""
        with self.lock:
            if time.time() - self.last_quote_ts < ORDER_RECONCILE_GRACE_SEC:
                return
        try:
            oo = self.info.open_orders(ACCOUNT_ADDRESS, dex=self.dex)
        except Exception:
            return
        real: Dict[int, Dict[str, Any]] = {}
        for o in oo:
            if self._coin_matches(o.get("coin")):
                real[int(o["oid"])] = {
                    "side": "B" if o.get("side") == "B" else "A",
                    "px": float(o.get("limitPx", 0) or 0),
                    "sz": float(o.get("sz", 0) or 0),
                }
        with self.lock:
            if set(real) != set(self.orders):
                self.orders = real
                self.last_intent = None
                self.wake.set()

    # ------------------------------------------------------------- order mgmt
    def cancel_oids(self, oids: List[int]) -> bool:
        if not oids:
            return True
        try:
            res = self.exchange.bulk_cancel([{"coin": SYMBOL, "oid": o} for o in oids])
            if isinstance(res, dict) and res.get("status") != "ok":
                raise RuntimeError(str(res)[:150])
        except Exception as exc:
            with self.lock:
                self.last_error = f"cancel: {str(exc)[:100]}"
            return False
        # Ne retirer du tracking que les cancels CONFIRMÉS par statut — un
        # cancel refusé laissé dans le vide devient un ordre fantôme dupliqué.
        statuses = []
        if isinstance(res, dict):
            statuses = res.get("response", {}).get("data", {}).get("statuses", [])
        with self.lock:
            for oid, status in zip(oids, statuses):
                ok = status == "success"
                if not ok and isinstance(status, dict):
                    err = str(status.get("error", "")).lower()
                    ok = "already" in err or "never placed" in err or "filled" in err
                if ok:
                    self.orders.pop(oid, None)
            self.last_quote_ts = time.time()
        return True

    def cancel_all(self) -> bool:
        with self.lock:
            oids = list(self.orders.keys())
        return self.cancel_oids(oids)

    @staticmethod
    def _is_post_only_error(err: str) -> bool:
        e = err.lower()
        return "post only" in e or "alo" in e or "immediately match" in e or "would have matched" in e

    def place_quotes(self, reqs: List[Dict[str, Any]]) -> None:
        if not reqs:
            return
        pending = reqs
        for retry in range(POST_ONLY_MAX_RETRIES + 1):
            # Clamp de dernière milliseconde au BBO le plus frais (jamais croiser).
            with self.lock:
                bb, ba = self.best_bid, self.best_ask
            tick = dyn_tick(self.mid)
            for r in pending:
                if r["is_buy"] and ba > 0:
                    r["limit_px"] = min(r["limit_px"], px_down(ba - tick, tick))
                elif not r["is_buy"] and bb > 0:
                    r["limit_px"] = max(r["limit_px"], px_up(bb + tick, tick))
            try:
                res = self.exchange.bulk_orders(pending)
            except Exception as exc:
                with self.lock:
                    self.last_error = f"place: {str(exc)[:100]}"
                return
            if not isinstance(res, dict) or res.get("status") != "ok":
                with self.lock:
                    self.last_error = f"place: {str(res)[:100]}"
                return
            statuses = res.get("response", {}).get("data", {}).get("statuses", [])
            retry_reqs = []
            with self.lock:
                self.last_quote_ts = time.time()
                for req, status in zip(pending, statuses):
                    if isinstance(status, dict) and "resting" in status:
                        oid = int(status["resting"]["oid"])
                        if oid not in self.orders:
                            self.orders[oid] = {"side": "B" if req["is_buy"] else "A",
                                                "px": req["limit_px"], "sz": req["sz"]}
                    elif isinstance(status, dict) and "filled" in status:
                        pass  # fill immédiat (rare en ALO), userFills s'en charge
                    else:
                        err = str(status.get("error", status) if isinstance(status, dict) else status)
                        self.last_error = err[:100]
                        if retry < POST_ONLY_MAX_RETRIES and self._is_post_only_error(err):
                            retry_reqs.append(req)
            if not retry_reqs:
                return
            pending = retry_reqs

    def flatten(self, reason: str):
        """Cancel all + sortie market IOC reduce-only (règle projet: un stop
        ferme TOUT, position comprise)."""
        self.cancel_all()
        self.sync_position()
        with self.lock:
            pos = self.position
            bb, ba = self.best_bid, self.best_ask
        if abs(pos) < MIN_SIZE / 2:
            return
        if not self.live:
            self.last_action = f"[dry] flatten {reason}"
            return
        tick = dyn_tick(self.mid)
        is_buy = pos < 0
        sz = round_sz(abs(pos))
        px = (ba + 20 * tick) if is_buy else (bb - 20 * tick)
        px = px_up(max(px, tick), tick) if is_buy else px_down(max(px, tick), tick)
        try:
            self.exchange.order(name=SYMBOL, is_buy=is_buy, sz=sz, limit_px=px,
                                order_type={"limit": {"tif": "Ioc"}}, reduce_only=True)
            self.last_action = f"flatten ({reason})"
        except Exception as exc:
            with self.lock:
                self.last_error = f"flatten: {str(exc)[:100]}"

    # ---------------------------------------------------------------- quoting
    def compute_intent(self) -> Tuple[Optional[Tuple[float, float, bool]],
                                      Optional[Tuple[float, float, bool]], str]:
        """Retourne (bid, ask, tag) où bid/ask = (px, sz, reduce_only) ou None.

        Empilement de couches, dans l'ordre:
          1. signal directionnel (OBI + flow + tilt)        → côté / recul
          2. momentum court (openings)                       → pas de quote côté balayé
          3. sweep breaker + cooldown post-fill (openings)   → retrait temporaire
          4. probation markout (openings)                    → recul / retrait
          5. EV gate (openings)                              → recule jusqu'à EV≥0 ou retire
          6. vol widen + skew d'inventaire (openings)        → recul
          7. inventaire: cap, reduce-only, sortie ancrée entry ± profit
        """
        now = time.time()
        with self.lock:
            bb, ba, mid = self.best_bid, self.best_ask, self.mid
            pos = self.position
            entry = self.entry_price
            pos_since = self.pos_since
            mo_b = self.markout_bps["B"]
            mo_a = self.markout_bps["A"]
            pause_b = self.side_pause_until["B"]
            pause_a = self.side_pause_until["A"]
            last_fill_b = self.last_side_fill_ts["B"]
            last_fill_a = self.last_side_fill_ts["A"]
        if bb <= 0 or ba <= bb or mid <= 0:
            return None, None, "no book"
        tick = dyn_tick(mid)
        tick_bps = tick / mid * 10000.0
        spread_bps = (ba - bb) / mid * 10000.0
        if (ba - bb) / tick > MAX_SPREAD_TICKS:
            return None, None, "spread too wide"
        if now < self.pause_until:
            return None, None, "risk pause"

        sig = self.signal.combined()
        mom_f = self.signal.momentum_bps(MOM_FAST_SEC)
        mom_s = self.signal.momentum_bps(MOM_SLOW_SEC)
        vol = self.signal.vol_bps()

        order_sz = round_sz(max(MIN_SIZE, ORDER_NOTIONAL_USD / mid))
        if order_sz * mid < 10.0:
            order_sz = round_sz(math.ceil((10.5 / mid) / (10 ** -SZ_DECIMALS)) * (10 ** -SZ_DECIMALS))
        max_pos = MAX_INV_ORDERS * order_sz
        eps = MIN_SIZE / 2

        quote_bid = True
        quote_ask = True
        extra_bid = 0.0   # recul (en ticks) du bid ouvrant sous le BBO
        extra_ask = 0.0
        tag = f"sig {sig:+.2f}"

        # ---- 1. Signal directionnel
        if sig >= TH_STRONG:
            quote_ask = False
            tag += " | BIDonly"
        elif sig <= -TH_STRONG:
            quote_bid = False
            tag += " | ASKonly"
        elif sig > TH_NEUTRAL:
            extra_ask += BACKOFF_TICKS
        elif sig < -TH_NEUTRAL:
            extra_bid += BACKOFF_TICKS

        # Un côté est "ouvrant" s'il augmente l'exposition; le côté qui
        # déboucle une position échappe aux couches 2-6 (il DOIT rester).
        bid_is_opening = pos > -eps
        ask_is_opening = pos < eps

        # ---- 2. Momentum: pas de quote ouvrante du côté qui se fait courir dessus
        if bid_is_opening and quote_bid and (
                mom_f <= -MOM_GATE_BPS
                or (mom_f <= -0.6 * MOM_GATE_BPS and mom_s <= -MOM_GATE_BPS)):
            quote_bid = False
            tag += " | mom↓"
        if ask_is_opening and quote_ask and (
                mom_f >= MOM_GATE_BPS
                or (mom_f >= 0.6 * MOM_GATE_BPS and mom_s >= MOM_GATE_BPS)):
            quote_ask = False
            tag += " | mom↑"

        # ---- 3. Sweep breaker + cooldown post-fill
        if bid_is_opening and quote_bid and (
                now < pause_b or now - last_fill_b < FILL_COOLDOWN_SEC):
            quote_bid = False
            tag += " | B pause"
        if ask_is_opening and quote_ask and (
                now < pause_a or now - last_fill_a < FILL_COOLDOWN_SEC):
            quote_ask = False
            tag += " | A pause"

        # ---- 4. Probation markout
        if bid_is_opening and quote_bid:
            if mo_b <= MARKOUT_HARD_BPS:
                quote_bid = False
                tag += " | moB✗"
            elif mo_b <= MARKOUT_SOFT_BPS:
                extra_bid += MARKOUT_EXTRA_TICKS
                tag += " | moB−"
        if ask_is_opening and quote_ask:
            if mo_a <= MARKOUT_HARD_BPS:
                quote_ask = False
                tag += " | moA✗"
            elif mo_a <= MARKOUT_SOFT_BPS:
                extra_ask += MARKOUT_EXTRA_TICKS
                tag += " | moA−"

        # ---- 5. EV gate: capture attendue ≥ frais + adverse prédit + marge.
        # Reculer la quote augmente la capture d'autant; si le recul nécessaire
        # dépasse le cap, le côté ne paie pas → retiré.
        if bid_is_opening and quote_bid:
            adverse_pred = max(0.0, -mo_b)
            need_bps = (MIN_EV_BPS + MAKER_FEE_BPS + adverse_pred) - (spread_bps / 2 + extra_bid * tick_bps)
            if need_bps > 0:
                add = math.ceil(need_bps / max(tick_bps, 1e-9))
                if extra_bid + add > EV_MAX_EXTRA_TICKS:
                    quote_bid = False
                    tag += " | EV✗bid"
                else:
                    extra_bid += add
        if ask_is_opening and quote_ask:
            adverse_pred = max(0.0, -mo_a)
            need_bps = (MIN_EV_BPS + MAKER_FEE_BPS + adverse_pred) - (spread_bps / 2 + extra_ask * tick_bps)
            if need_bps > 0:
                add = math.ceil(need_bps / max(tick_bps, 1e-9))
                if extra_ask + add > EV_MAX_EXTRA_TICKS:
                    quote_ask = False
                    tag += " | EV✗ask"
                else:
                    extra_ask += add

        # ---- 6. Vol widen + skew d'inventaire
        vol_extra = int(min(3.0, max(0.0, (vol * VOL_WIDEN_MULT - spread_bps / 2) / max(tick_bps, 1e-9))))
        if vol_extra > 0:
            if bid_is_opening:
                extra_bid += vol_extra
            if ask_is_opening:
                extra_ask += vol_extra
            tag += f" | vol+{vol_extra}"
        inv_ratio = max(-1.0, min(1.0, pos / max(max_pos, MIN_SIZE)))
        if inv_ratio > 0 and bid_is_opening:
            extra_bid += round(INV_SKEW_TICKS * inv_ratio)
        elif inv_ratio < 0 and ask_is_opening:
            extra_ask += round(INV_SKEW_TICKS * (-inv_ratio))

        # Prix ouvrants
        bid_px = px_down(bb - extra_bid * tick, tick)
        ask_px = px_up(ba + extra_ask * tick, tick)

        # ---- 7. Inventaire: cap, reduce-only, sortie ancrée
        bid_ro = False
        ask_ro = False
        bid_sz = order_sz
        ask_sz = order_sz
        if pos >= eps:                      # long → l'ask déboucle
            ask_sz = round_sz(min(order_sz, abs(pos)))
            ask_ro = ask_sz >= MIN_SIZE
            quote_ask = ask_ro
            age = now - pos_since if pos_since else 1e9
            if age <= UNWIND_ANCHOR_MAX_SEC and entry > 0:
                # Sortie ancrée: viser au moins entry + profit, pas un scratch.
                ask_px = max(px_up(ba, tick), px_up(entry + PROFIT_TICKS * tick, tick))
                tag += " | exit@entry+"
            else:
                ask_px = px_up(ba, tick)   # trop vieux: rejoint le BBO
                if age > MAX_HOLD_SEC:
                    tag += " | forced unwind"
            if pos + order_sz > max_pos + eps:
                quote_bid = False
                tag += " | invcap"
        elif pos <= -eps:                   # short → le bid déboucle
            bid_sz = round_sz(min(order_sz, abs(pos)))
            bid_ro = bid_sz >= MIN_SIZE
            quote_bid = bid_ro
            age = now - pos_since if pos_since else 1e9
            if age <= UNWIND_ANCHOR_MAX_SEC and entry > 0:
                bid_px = min(px_down(bb, tick), px_down(entry - PROFIT_TICKS * tick, tick))
                tag += " | exit@entry-"
            else:
                bid_px = px_down(bb, tick)
                if age > MAX_HOLD_SEC:
                    tag += " | forced unwind"
            if -pos + order_sz > max_pos + eps:
                quote_ask = False
                tag += " | invcap"

        bid = (bid_px, bid_sz, bid_ro) if (quote_bid and bid_sz >= MIN_SIZE) else None
        ask = (ask_px, ask_sz, ask_ro) if (quote_ask and ask_sz >= MIN_SIZE) else None
        return bid, ask, tag

    def apply_intent(self):
        now = time.time()
        if now - self.last_action_ts < ACTION_COALESCE_SEC:
            return
        bid, ask, tag = self.compute_intent()
        tick = dyn_tick(self.mid if self.mid > 0 else 1.0)
        # Intent quantifié pour la comparaison sticky
        intent = (
            None if bid is None else (round(bid[0], PRICE_DECIMALS), bid[1], bid[2]),
            None if ask is None else (round(ask[0], PRICE_DECIMALS), ask[1], ask[2]),
        )
        with self.lock:
            resting_bids = {o: d for o, d in self.orders.items() if d["side"] == "B"}
            resting_asks = {o: d for o, d in self.orders.items() if d["side"] == "A"}
            last_intent = self.last_intent

        def side_ok(want, resting: Dict[int, Dict]) -> bool:
            """Sticky: le côté est déjà bien quoté si UN ordre reste dans la
            tolérance — on garde alors sa priorité de queue."""
            if want is None:
                return not resting
            if len(resting) != 1:
                return False
            d = next(iter(resting.values()))
            return abs(d["px"] - want[0]) <= REPLACE_TOL_TICKS * tick + 1e-12

        if side_ok(intent[0], resting_bids) and side_ok(intent[1], resting_asks) \
                and last_intent == intent:
            return

        to_cancel: List[int] = []
        reqs: List[Dict[str, Any]] = []
        if not side_ok(intent[0], resting_bids):
            to_cancel += list(resting_bids.keys())
            if intent[0] is not None:
                reqs.append({"coin": SYMBOL, "is_buy": True, "sz": intent[0][1],
                             "limit_px": intent[0][0],
                             "order_type": {"limit": {"tif": "Alo"}},
                             "reduce_only": intent[0][2]})
        if not side_ok(intent[1], resting_asks):
            to_cancel += list(resting_asks.keys())
            if intent[1] is not None:
                reqs.append({"coin": SYMBOL, "is_buy": False, "sz": intent[1][1],
                             "limit_px": intent[1][0],
                             "order_type": {"limit": {"tif": "Alo"}},
                             "reduce_only": intent[1][2]})

        self.last_action_ts = now
        self.last_action = tag
        if not self.live:
            with self.lock:
                self.last_intent = intent
            return
        if to_cancel and not self.cancel_oids(to_cancel):
            return  # cancel raté: ne PAS reposer par-dessus (anti-doublons)
        # Vérifier qu'aucun fantôme du même côté ne reste avant de reposer
        with self.lock:
            still_b = any(d["side"] == "B" for d in self.orders.values())
            still_a = any(d["side"] == "A" for d in self.orders.values())
        reqs = [r for r in reqs if not (r["is_buy"] and still_b) and not (not r["is_buy"] and still_a)]
        if reqs:
            self.place_quotes(reqs)
        with self.lock:
            self.last_intent = intent

    # ---------------------------------------------------------------- markout
    def update_markouts(self):
        """Score les fills arrivés à maturité (+MARKOUT_HORIZON_SEC): mid
        actuel vs prix du fill, signé pour que négatif = adverse. EWMA par
        côté + décote temporelle vers 0 (pardon progressif sans fills)."""
        now = time.time()
        with self.lock:
            mid = self.mid
            # Décote temporelle
            dt = now - self.last_markout_decay
            if dt >= 1.0:
                k = math.exp(-dt / MARKOUT_DECAY_TAU)
                self.markout_bps["B"] *= k
                self.markout_bps["A"] *= k
                self.last_markout_decay = now
            if mid <= 0:
                return
            while self.pending_markouts and now - self.pending_markouts[0][0] >= MARKOUT_HORIZON_SEC:
                _, sd, px = self.pending_markouts.popleft()
                mo = (mid - px) / px * 10000.0 if sd == "B" else (px - mid) / px * 10000.0
                cur = self.markout_bps[sd]
                self.markout_bps[sd] = cur + MARKOUT_EWMA_ALPHA * (mo - cur)

    # ------------------------------------------------------------------- risk
    def risk_checks(self):
        with self.lock:
            pos = self.position
            upnl = self.unrealized_pnl
            mid = self.mid
            pos_since = self.pos_since
        notional = abs(pos) * mid
        if notional > 0 and upnl < 0 and (abs(upnl) / notional) * 10000.0 >= STOP_ADVERSE_BPS:
            self.flatten(f"stop {STOP_ADVERSE_BPS}bps")
            self.pause_until = time.time() + STOP_PAUSE_SEC
            return
        if pos_since and time.time() - pos_since > MAX_HOLD_SEC + FORCE_FLAT_EXTRA and abs(pos) >= MIN_SIZE:
            self.flatten("max hold")
            self.pause_until = time.time() + STOP_PAUSE_SEC / 2
            return
        net = self.session_pnl + self.unrealized_pnl - self.total_fees
        if net <= -MAX_SESSION_LOSS_USD:
            self.last_action = f"SESSION LOSS LIMIT ${net:+.2f} — arrêt"
            self.running = False

        # Deadman feed
        if time.time() - self.last_tick_ts > DEADMAN_FEED_SEC and self.ws_ready:
            if not self.refresh_bbo_rest():
                if abs(pos) >= MIN_SIZE:
                    self.flatten("dead feed")
                self.cancel_all()
            if time.time() - self.last_ws_restart > WS_RESTART_COOLDOWN:
                self.last_ws_restart = time.time()
                self.restart_ws()

    # -------------------------------------------------- IPC dashboard (files)
    def _check_stop_file(self):
        try:
            if os.path.exists(self._stop_file):
                os.remove(self._stop_file)
                self.running = False
                self.last_action = "stop demandé par le dashboard"
        except Exception:
            pass

    def write_stats(self, running: bool = True):
        """Snapshot JSON atomique pour le dashboard (multi-process)."""
        try:
            with self.lock:
                mid = self.mid
                spread_bps = ((self.best_ask - self.best_bid) / mid * 10000.0
                              if mid > 0 and self.best_ask > self.best_bid > 0 else 0.0)
                data = {
                    "symbol": SYMBOL,
                    "running": running,
                    "live": self.live,
                    "ts": time.time(),
                    "start_time": self.start_time,
                    "mid": mid,
                    "spread_bps": spread_bps,
                    "position": self.position,
                    "position_usd": self.position * mid if mid > 0 else 0.0,
                    "entry": self.entry_price,
                    "upnl": self.unrealized_pnl,
                    "equity": self.equity,
                    "volume": self.total_volume,
                    "fees": self.total_fees,
                    "rpnl": self.session_pnl,
                    "fills": self.fills_count,
                    "maker_fills": self.maker_fills,
                    "notional": ORDER_NOTIONAL_USD,
                    "action": self.last_action,
                    "error": self.last_error,
                }
            tmp = self._stats_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp, self._stats_file)
        except Exception:
            pass

    # -------------------------------------------------------------- dashboard
    def dashboard(self) -> Panel:
        obi, flow, tilt, comb = self.signal.snapshot()
        mom = self.signal.momentum_bps(MOM_FAST_SEC)
        volat = self.signal.vol_bps()
        with self.lock:
            bb, ba, mid = self.best_bid, self.best_ask, self.mid
            pos, entry, upnl = self.position, self.entry_price, self.unrealized_pnl
            vol, fees, rpnl = self.total_volume, self.total_fees, self.session_pnl
            fills, makers = self.fills_count, self.maker_fills
            n_orders = len(self.orders)
            action, err = self.last_action, self.last_error
            eq = self.equity
            mo_b, mo_a = self.markout_bps["B"], self.markout_bps["A"]
        net = rpnl + upnl - fees
        # Même métrique que MM_GRID_ADAPTIVE: (fees − PnL réalisé) / M de volume,
        # uPnL exclu — chiffres comparables entre les deux dashboards.
        cost_m = ((fees - rpnl) / (vol / 1_000_000)) if vol > 0 else 0.0
        maker_pct = (makers / fills * 100) if fills else 100.0
        t = Table(box=box.SIMPLE_HEAD, expand=True)
        t.add_column("Market", justify="left")
        t.add_column("Signal", justify="left")
        t.add_column("Position", justify="left")
        t.add_column("Session", justify="left")
        sig_color = "green" if comb > TH_NEUTRAL else "red" if comb < -TH_NEUTRAL else "white"
        mo_b_c = "red" if mo_b <= MARKOUT_SOFT_BPS else "green" if mo_b > 0 else "white"
        mo_a_c = "red" if mo_a <= MARKOUT_SOFT_BPS else "green" if mo_a > 0 else "white"
        t.add_row(
            f"{SYMBOL}\nbid {bb:.{PRICE_DECIMALS}f}\nask {ba:.{PRICE_DECIMALS}f}\nmid {mid:.{PRICE_DECIMALS}f}\nvol {volat:.2f}bps",
            f"OBI {obi:+.2f} flow {flow:+.2f}\ntilt {tilt:+.2f}bps\n[{sig_color}]comb {comb:+.2f}[/] mom {mom:+.1f}\n"
            f"mo [{mo_b_c}]B {mo_b:+.2f}[/]/[{mo_a_c}]A {mo_a:+.2f}[/]\nordres {n_orders}",
            f"pos {pos:+.{SZ_DECIMALS}f}\nentry {entry:.{PRICE_DECIMALS}f}\nuPnL ${upnl:+.2f}\nequity ${eq:.2f}",
            f"vol ${vol:,.0f}\nfills {fills} ({maker_pct:.0f}% maker)\nfees ${fees:.2f} | net ${net:+.2f}\ncost/M ${cost_m:+.0f}",
        )
        mode = "[green]LIVE[/]" if self.live else "[yellow]DRY-RUN[/]"
        sub = f"{mode} | {action}"
        if err:
            sub += f" | [red]{err}[/]"
        return Panel(t, title=f"OBI VOLUME BOT — {SYMBOL}", subtitle=sub, box=box.ROUNDED)

    # -------------------------------------------------------------------- run
    def _trading_loop(self, live_display=None):
        last_render = 0.0
        while self.running:
            self.wake.wait(timeout=0.5)
            self.wake.clear()
            if not self.running:
                break
            now = time.time()
            self._check_stop_file()
            if not self.running:
                break
            if now - self.last_sync_ts > SYNC_EVERY_SEC:
                self.last_sync_ts = now
                self.sync_position()
            if now - self.last_reconcile_ts > RECONCILE_EVERY_SEC:
                self.last_reconcile_ts = now
                self.reconcile_orders()
            self.update_markouts()
            self.risk_checks()
            if now - self._last_stats_write > 1.0:
                self._last_stats_write = now
                self.write_stats(running=True)
            if not self.running:
                break
            if self.ws_ready:
                self.apply_intent()
            if live_display is not None and now - last_render > 0.5:
                last_render = now
                live_display.update(self.dashboard())

    def run(self):
        # Headless: lancé dans un thread (dashboard in-process) OU en process
        # enfant du dashboard (OBI_HEADLESS=1) — pas de Rich Live ni de prompt.
        headless = (threading.current_thread() is not threading.main_thread()
                    or os.getenv("OBI_HEADLESS", "") == "1")
        if not headless:
            print(f"⏳ {SYMBOL} — attente du premier book L2...")
        self.set_leverage()
        self.startup_sweep()
        self.sync_position()
        self.start_ws()
        deadline = time.time() + 15
        while not self.ws_ready and time.time() < deadline:
            time.sleep(0.2)
        if not self.ws_ready:
            self.refresh_bbo_rest()

        if headless:
            self._trading_loop(None)
        else:
            with Live(self.dashboard(), console=self.console, refresh_per_second=2, screen=False) as live:
                self._trading_loop(live)

        # STOP = tout couper: cancel all + flatten market (règle projet).
        if not headless:
            print("\n🛑 Stop — cancel all + flatten...")
        try:
            self.cancel_all()
        except Exception:
            pass
        try:
            self.flatten("stop")
        except Exception:
            pass
        try:
            self.ws_info.disconnect_websocket()
        except Exception:
            pass
        self.write_stats(running=False)   # dernier snapshot pour le dashboard
        if not headless:
            print("✅ Terminé.")


# =============================================================================
# MAIN
# =============================================================================
def resolve_token(name: str) -> str:
    key = name.strip().upper()
    if key in TOKENS:
        return TOKENS[key]["symbol"]
    # Token hors presets: essayer xyz puis mkts via le meta live
    for dex in PERP_DEXS:
        try:
            fetch_token_meta(f"{dex}:{key}")
            return f"{dex}:{key}"
        except Exception:
            continue
    raise SystemExit(f"❌ Token inconnu: {name}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="OBI volume bot (HIP-3)")
    parser.add_argument("--token", default=None, help="ex: SP500, XYZ100, SPCX")
    parser.add_argument("--live", action="store_true", help="ordres réels (défaut: dry-run)")
    parser.add_argument("--notional", type=float, default=None, help="USD par ordre")
    args = parser.parse_args()

    if args.notional:
        ORDER_NOTIONAL_USD = max(10.5, args.notional)

    token = args.token
    if not token:
        print("Tokens: " + ", ".join(TOKENS))
        token = input(f"Token ? [{DEFAULT_TOKEN}] : ").strip() or DEFAULT_TOKEN

    SYMBOL = resolve_token(token)
    meta = fetch_token_meta(SYMBOL)
    SZ_DECIMALS = meta["szDecimals"]
    MIN_SIZE = 10.0 ** (-SZ_DECIMALS) if SZ_DECIMALS > 0 else 1.0
    PRICE_DECIMALS = max(0, 6 - SZ_DECIMALS)
    LEVERAGE = min(LEVERAGE, meta.get("maxLeverage", LEVERAGE))

    if not acquire_instance_lock(SYMBOL):
        sys.exit(f"❌ Un bot tourne déjà sur {SYMBOL} (verrou d'instance)")

    mode = "LIVE" if args.live else "DRY-RUN (aucun ordre envoyé — ajoute --live)"
    print(f"🚀 OBI VOLUME BOT | {SYMBOL} | {LEVERAGE}x | ~${ORDER_NOTIONAL_USD:.0f}/ordre | {mode}")
    OBIVolumeBot(live=args.live).run()
