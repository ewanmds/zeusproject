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

# Télémétrie (fills + markouts en SQLite, hors chemin critique). Optionnelle:
# si le module manque, le bot trade normalement sans persistance.
try:
    from obi_telemetry import FillLogger, MARKOUT_HORIZONS
except Exception:
    FillLogger = None
    MARKOUT_HORIZONS = (1.0, 5.0, 30.0)

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
    # half_spread_bps optionnel: distance de base par token (sinon défaut global).
    # SP500: book très serré (0.1-0.3 bps) → 0.7 bps ≈ 5 ticks, plus proche
    # que le défaut 1.5, toujours > frais+EV (0.388 bps).
    "SP500":  {"symbol": "xyz:SP500", "half_spread_bps": 0.7,
               "session_utc": ((810, 1200),)},
    "XYZ100": {"symbol": "xyz:XYZ100"},
    "SPCX":   {"symbol": "xyz:SPCX"},
    "GOLD":   {"symbol": "xyz:GOLD"},
    "SILVER": {"symbol": "xyz:SILVER"},
    "NVDA":   {"symbol": "xyz:NVDA"},
    "TSLA":   {"symbol": "xyz:TSLA"},
    "US500":  {"symbol": "mkts:US500", "session_utc": ((810, 1200),)},
    # SK Hynix — nom wire "xyz:SKHX" (le frontend affiche SKHXUSD), max 10x
    "SKHX":    {"symbol": "xyz:SKHX", "session_utc": ((0, 390),)},
    "SKHXUSD": {"symbol": "xyz:SKHX", "session_utc": ((0, 390),)},
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

# --- Couche prix: OFI (Cont-Kukanov-Stoikov) + micro-price (Stoikov) ---
# L'alpha n'est PLUS le déséquilibre statique du book (bruité, peu prédictif)
# mais l'ORDER FLOW IMBALANCE: le flux signé de CHANGEMENTS des queues au
# meilleur bid/ask (arrivées − annulations − trades), bucketé par seconde.
# CKS (J. Financial Econometrics 2014): relation LINÉAIRE OFI → Δprix.
#   fair = micro_price × (1 + BETA×z_OFI/1e4)
# où BETA est AUTO-CALIBRÉ en ligne: régression glissante (OLS décayée) du
# rendement forward du mid (horizon BETA_HORIZON_SEC) sur le z de l'OFI.
# Si l'OFI ne prédit rien sur un token, beta→0 et le bot quote symétrique —
# fini le c1 codé en dur qui penche à tort.
ZSCORE_WINDOW_SEC  = float(os.getenv("OBI_ZSCORE_WINDOW_SEC", "3600"))
ZSCORE_WARMUP_SEC  = 60.0   # z=0 tant que l'historique est trop court
ZSCORE_PULL        = 2.5    # |z| au-delà: le côté adverse est retiré
# --- Chemin d'urgence microseconde (anti-adverse) ---
# 1) SWEEP EN DIRECT: un burst agresseur ≥ EMERG_SWEEP_MULT × notre notional
#    dans UN batch de trades → cancel immédiat des quotes ouvrantes du côté
#    visé, DEPUIS le thread WebSocket (zéro attente de boucle: c'est le
#    chemin le plus court réalisable — l'ordre est retiré avant que le sweep
#    n'atteigne notre niveau, au lieu d'être sa prochaine victime).
EMERG_SWEEP_MULT   = float(os.getenv("OBI_EMERG_SWEEP_MULT", "3.0"))
EMERG_COOLDOWN_SEC = 1.0
# 2) SCRATCH TOXIQUE: un fill OUVRANT reçu alors que l'alpha est franchement
#    contre la nouvelle position (|z| ≥ TOXIC_Z ou régime PUMP/DUMP adverse)
#    → sortie IOC immédiate. Payer 0.83 bps de taker vaut mieux que subir
#    les 3-10 bps du move qui suit un fill informé.
TOXIC_SCRATCH_Z    = 1.5
SCRATCH_COOLDOWN_SEC = 2.0
# --- Queue position model (étape 3 du plan) ---
# La rentabilité d'un ordre passif dépend de sa place dans la file: devant,
# on capture le bruit (fills bénins); au fond, on ne reçoit QUE les sweeps.
QUEUE_MODEL         = os.getenv("OBI_QUEUE_MODEL", "1") == "1"
QUEUE_FRONT_FRAC    = 0.30   # ≤30% de la file initiale devant nous = front → sticky renforcé
QUEUE_DEEP_FRAC     = 0.80   # ≥80% devant nous = fond de file
QUEUE_ADV_ALPHA_BPS = 0.30   # fond de file + alpha contre nous → replace gratuit
# --- Sizing par conviction (étape 4 du plan, Kelly-lite) ---
# Taille × [1 .. 2] du côté favorisé par l'alpha, × [1 .. 0.5] du côté
# défavorisé — grosse taille seulement quand le signal paie.
SIZING_CONVICTION   = os.getenv("OBI_SIZING", "1") == "1"
OFI_BUCKET_SEC     = 1.0    # agrégation de l'OFI (cadence du tuto hftbacktest)
BETA_HORIZON_SEC   = float(os.getenv("OBI_BETA_HORIZON_SEC", "5"))
BETA_TAU_SEC       = 1800.0 # mémoire de la régression (décroissance exp.)
BETA_MIN_SAMPLES   = 60     # avant: fallback C1_BPS
BETA_MAX_BPS       = 2.5    # clamp du beta appris (bps de fair par unité de z)
ALPHA_CAP_BPS      = 2.5    # clamp de l'ajustement de fair total
C1_BPS             = float(os.getenv("OBI_C1_BPS", "0.5"))   # beta de warm-up
SKEW_BPS           = float(os.getenv("OBI_SKEW_BPS", "1.2")) # bps de skew par unité d'inventaire
# Demi-spread de base autour de la reservation (mode PROFIT du tuto: quoter
# À DISTANCE du mid, et laisser l'alpha rapprocher le côté favorisé du BBO —
# coller en permanence au BBO = adverse selection maximale, cost/M qui explose).
HALF_SPREAD_BPS    = float(os.getenv("OBI_HALF_SPREAD_BPS", "1.5"))

# --- Pump/dump detector (fast-tick) ---
PUMP_MOM_BPS       = float(os.getenv("OBI_PUMP_MOM_BPS", "3.0"))  # |Δmid 1s| déclencheur
PUMP_VOL_MULT      = 3.0    # vol instantanée ≥ N× la vol EWMA
PUMP_FLOW_TH       = 0.8    # ...ou flux agresseur 3s quasi one-sided
REGIME_QUIET_SEC   = 2.0    # fin d'événement: 2s sans nouvel extrême

# --- Take-profit actif (encaisser au lieu de laisser s'évaporer) ---
TP_MIN_BPS         = float(os.getenv("OBI_TP_MIN_BPS", "2.0"))  # peak uPnL minimal
TP_RETRACE         = 0.40   # retracement du peak qui déclenche le bank IOC

# --- Protection PnL session: resserrement progressif, JAMAIS d'arrêt ---
AHEAD_USD          = float(os.getenv("OBI_AHEAD_USD", "1.0"))   # net ≥ → PROTECT1

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
# 6) (le skew d'inventaire est désormais CONTINU via la reservation price —
#    voir SKEW_BPS plus haut; l'ancien INV_SKEW_TICKS par paliers est retiré)
# 7) Sortie ancrée: le débouclage vise entry ± PROFIT_TICKS tant que la
#    position est jeune, au lieu de scratcher systématiquement au BBO.
PROFIT_TICKS            = 2
UNWIND_ANCHOR_MAX_SEC   = 60.0

# --- Exécution / churn ---
REPLACE_TOL_TICKS   = 1     # plancher de tolérance sticky, en ticks
# Anti-churn: la reservation price bouge en continu (z-score, mid) — sans
# hystérésis les ordres seraient replacés à chaque tick et ne REPOSERAIENT
# jamais (aucun fill, priorité de queue perdue en boucle, budget rate-limit
# brûlé). Un ordre ne bouge que si la cible a dérivé de REQUOTE_TOL_BPS ET
# qu'il a reposé au moins MIN_REST_SEC — sauf urgence (fill, pull, cross).
REQUOTE_TOL_BPS     = float(os.getenv("OBI_REQUOTE_TOL_BPS", "0.8"))
MIN_REST_SEC        = float(os.getenv("OBI_MIN_REST_SEC", "2.5"))
ACTION_COALESCE_SEC = 0.08  # min entre deux actions SAUF urgence (fill/BBO traversé)
POST_ONLY_MAX_RETRIES = 2   # reprice 1 tick et retente si l'ALO croiserait
MAX_SPREAD_TICKS    = 40    # spread book au-delà: marché cassé, on ne quote pas

# --- Risque ---
STOP_ADVERSE_BPS   = 20.0   # uPnL < −20 bps du notionnel → flatten IOC + pause
STOP_PAUSE_SEC     = 60.0
MAX_HOLD_SEC       = 120.0  # position plus vieille → débouclage passif forcé
FORCE_FLAT_EXTRA   = 45.0   # ... et IOC si toujours pas flat après ce délai en +
MAX_SESSION_LOSS_USD = float(os.getenv("OBI_MAX_SESSION_LOSS_USD", "5"))
HOURLY_LOSS_MULT     = float(os.getenv("OBI_HOURLY_LOSS_MULT", "0.5"))
HOURLY_PAUSE_SEC     = float(os.getenv("OBI_HOURLY_PAUSE_SEC", "900"))
DEADMAN_FEED_SEC   = 12.0   # feed muet + position → REST refresh sinon flatten
WS_RESTART_COOLDOWN = 30.0
SYNC_EVERY_SEC     = 5.0    # resync REST position/equity (uPnL frais pour le TP)
RECONCILE_EVERY_SEC = 10.0  # resync REST des ordres ouverts (anti-fantômes)
ORDER_RECONCILE_GRACE_SEC = 3.0
STALE_ORDERUPDATE_SEC = 15.0  # ordres au repos sans le moindre event WS depuis
                              # N s → réconciliation immédiate (ordre fantôme)

MAKER_FEE_BPS = 0.288       # xyz: 28.8 $/M par fill

SESSION_WINDOWS_UTC: Tuple[Tuple[int, int], ...] = ()
OPEN_BLACKOUT_MIN = 10

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


def session_state_utc(
    windows: Tuple[Tuple[int, int], ...],
    ts: Optional[float] = None,
    blackout_min: int = OPEN_BLACKOUT_MIN,
) -> str:
    """Retourne ``open``, ``blackout`` ou ``off`` pour des fenêtres UTC.

    Les bornes sont exprimées en minutes depuis minuit et peuvent traverser
    minuit. Sans calendrier, le marché est considéré ouvert en permanence.
    """
    if not windows:
        return "open"
    utc = time.gmtime(time.time() if ts is None else ts)
    minute = utc.tm_hour * 60 + utc.tm_min
    for start, end in windows:
        start %= 1440
        end %= 1440
        in_window = (start <= minute < end if start < end
                     else minute >= start or minute < end)
        if not in_window:
            continue
        since_open = (minute - start) % 1440
        return "blackout" if since_open < max(0, blackout_min) else "open"
    # Le book HIP-3 reste coté hors cash session: protéger aussi les minutes
    # qui précèdent l'open, pas seulement celles qui le suivent.
    if blackout_min > 0:
        for start, _ in windows:
            until_open = (int(start) % 1440 - minute) % 1440
            if 0 < until_open <= blackout_min:
                return "blackout"
    return "off"


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
        self.obi_prev_raw = 0.0        # OBI brut du tick précédent (accélération)
        self.tilt_ewma = 0.0           # microprice tilt en bps (EWMA rapide)
        self.trades: deque = deque()   # (ts, signed_notional)
        self.flow = 0.0
        self.mids: deque = deque(maxlen=2000)   # (ts, mid) pour momentum/vol
        self.vol_bps_ewma = 0.0        # |Δmid 1s| en bps, lissé
        self._last_vol_ts = 0.0
        # Z-score de l'OFI bucketé: historique glissant + sommes O(1).
        self.sig_hist: deque = deque()   # (ts, ofi_bucket)
        self._sig_s1 = 0.0
        self._sig_s2 = 0.0
        self._first_sig_ts = 0.0
        # OFI (Cont-Kukanov-Stoikov): état du top of book précédent + bucket.
        self._prev_bb_px = 0.0
        self._prev_bb_sz = 0.0
        self._prev_ba_px = 0.0
        self._prev_ba_sz = 0.0
        self._ofi_accum = 0.0
        self._ofi_bucket_ts = 0.0
        self.micro_px = 0.0            # micro-price de Stoikov (fair de base)
        # Betas auto-calibrés: OLS décayées de ret_forward(bps) sur z_OFI (1)
        # et z_TFI (2, flux agresseur bucketé) — deux prédicteurs, deux pentes.
        self._beta_sxy = 0.0
        self._beta_sxx = 0.0
        self._beta2_sxy = 0.0
        self._beta2_sxx = 0.0
        self._beta_n = 0
        self._beta_last_decay = 0.0
        self._beta_pending: deque = deque()   # (ts, z1, z2, mid)
        self.beta_bps_val = 0.0
        self.beta2_bps_val = 0.0
        # TFI bucketé: z-score du flux agresseur signé par seconde.
        self.tfi_hist: deque = deque()
        self._tfi_s1 = 0.0
        self._tfi_s2 = 0.0
        self._tfi_bucket_start = 0.0
        # Détecteur pump/dump avec hystérésis
        self._regime: Optional[str] = None   # "PUMP" | "DUMP" | None
        self._regime_ts = 0.0                # dernier nouvel extrême
        self._regime_ext = 0.0               # mid extrême atteint pendant l'event
        # Snapshot des niveaux L2 (px → sz) pour le queue position model.
        self.book_bid_szs: Dict[float, float] = {}
        self.book_ask_szs: Dict[float, float] = {}

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

        # Micro-price de Stoikov (fair de base) + tilt pour le composite.
        b0_px, b0_sz = float(bids[0]["px"]), float(bids[0]["sz"])
        a0_px, a0_sz = float(asks[0]["px"]), float(asks[0]["sz"])
        mid = (b0_px + a0_px) / 2.0
        tilt_bps = 0.0
        micro = mid
        if b0_sz + a0_sz > 0 and mid > 0:
            micro = (b0_px * a0_sz + a0_px * b0_sz) / (b0_sz + a0_sz)
            tilt_bps = (micro - mid) / mid * 10000.0

        # OFI événementiel (Cont-Kukanov-Stoikov, éq. 10 du papier):
        #   e_n =  qb·1{Pb≥Pb⁻} − qb⁻·1{Pb≤Pb⁻} − qa·1{Pa≤Pa⁻} + qa⁻·1{Pa≥Pa⁻}
        # >0: la demande se renforce (queue bid qui monte / ask qui recule).
        ofi_evt = 0.0
        if self._prev_bb_px > 0:
            if b0_px >= self._prev_bb_px:
                ofi_evt += b0_sz
            if b0_px <= self._prev_bb_px:
                ofi_evt -= self._prev_bb_sz
            if a0_px <= self._prev_ba_px:
                ofi_evt -= a0_sz
            if a0_px >= self._prev_ba_px:
                ofi_evt += self._prev_ba_sz

        with self.lock:
            dt = max(1e-3, now - self._last_obi_ts) if self._last_obi_ts else OBI_EWMA_TAU_SEC
            alpha = 1.0 - math.exp(-dt / OBI_EWMA_TAU_SEC)
            self.obi_ewma += alpha * (raw - self.obi_ewma)
            self.tilt_ewma += alpha * (tilt_bps - self.tilt_ewma)
            self.obi_prev_raw = self.obi_raw
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
            # Accumuler l'OFI; état top-of-book précédent; micro-price.
            self._ofi_accum += ofi_evt
            self._prev_bb_px, self._prev_bb_sz = b0_px, b0_sz
            self._prev_ba_px, self._prev_ba_sz = a0_px, a0_sz
            self.micro_px = micro
            # Snapshot des niveaux pour le queue model (dicts px→sz, ≤20 niveaux).
            self.book_bid_szs = {float(l["px"]): float(l["sz"]) for l in bids}
            self.book_ask_szs = {float(l["px"]): float(l["sz"]) for l in asks}

            # Clôture du bucket OFI (1/s): z-score + échantillon de régression.
            if self._ofi_bucket_ts == 0.0:
                self._ofi_bucket_ts = now
                self._tfi_bucket_start = now
            elif now - self._ofi_bucket_ts >= OFI_BUCKET_SEC:
                v = self._ofi_accum
                self._ofi_accum = 0.0
                self._ofi_bucket_ts = now
                self.sig_hist.append((now, v))
                self._sig_s1 += v
                self._sig_s2 += v * v
                if not self._first_sig_ts:
                    self._first_sig_ts = now
                cutoff = now - ZSCORE_WINDOW_SEC
                while self.sig_hist and self.sig_hist[0][0] < cutoff:
                    _, old = self.sig_hist.popleft()
                    self._sig_s1 -= old
                    self._sig_s2 -= old * old
                # TFI bucket: flux agresseur signé (USD) depuis le dernier bucket.
                t_start = self._tfi_bucket_start
                self._tfi_bucket_start = now
                tfi_v = sum(sv for ts_t, sv in self.trades if ts_t >= t_start)
                self.tfi_hist.append((now, tfi_v))
                self._tfi_s1 += tfi_v
                self._tfi_s2 += tfi_v * tfi_v
                while self.tfi_hist and self.tfi_hist[0][0] < cutoff:
                    _, old = self.tfi_hist.popleft()
                    self._tfi_s1 -= old
                    self._tfi_s2 -= old * old
                # Échantillon (z1, z2, mid) en attente du rendement forward.
                z1_now = self._zscore_locked(now)
                z2_now = self._tfi_zscore_locked(now)
                self._beta_pending.append((now, z1_now, z2_now, mid))
                # Mûrir: ret_forward → deux OLS décayées (beta = Sxy/Sxx).
                matured = False
                while self._beta_pending and now - self._beta_pending[0][0] >= BETA_HORIZON_SEC:
                    _, z1_s, z2_s, mid_s = self._beta_pending.popleft()
                    if mid_s <= 0:
                        continue
                    ret_bps = (mid - mid_s) / mid_s * 10000.0
                    if self._beta_last_decay:
                        k = math.exp(-(now - self._beta_last_decay) / BETA_TAU_SEC)
                        self._beta_sxy *= k
                        self._beta_sxx *= k
                        self._beta2_sxy *= k
                        self._beta2_sxx *= k
                    self._beta_last_decay = now
                    if abs(z1_s) >= 1e-9:
                        self._beta_sxy += z1_s * ret_bps
                        self._beta_sxx += z1_s * z1_s
                    if abs(z2_s) >= 1e-9:
                        self._beta2_sxy += z2_s * ret_bps
                        self._beta2_sxx += z2_s * z2_s
                    self._beta_n += 1
                    matured = True
                if matured and self._beta_n >= BETA_MIN_SAMPLES:
                    # beta<0 = non prédictif sur ce token → 0, pas d'inversion
                    if self._beta_sxx > 1e-9:
                        self.beta_bps_val = max(0.0, min(BETA_MAX_BPS, self._beta_sxy / self._beta_sxx))
                    if self._beta2_sxx > 1e-9:
                        self.beta2_bps_val = max(0.0, min(BETA_MAX_BPS, self._beta2_sxy / self._beta2_sxx))

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

    def _zscore_locked(self, now: float) -> float:
        n = len(self.sig_hist)
        if n < 20 or not self._first_sig_ts or now - self._first_sig_ts < ZSCORE_WARMUP_SEC:
            return 0.0
        mean = self._sig_s1 / n
        var = max(0.0, self._sig_s2 / n - mean * mean)
        std = math.sqrt(var)
        if std < 1e-6:
            return 0.0
        sig = self.sig_hist[-1][1]
        return max(-3.0, min(3.0, (sig - mean) / std))

    def _tfi_zscore_locked(self, now: float) -> float:
        n = len(self.tfi_hist)
        if n < 20 or not self._first_sig_ts or now - self._first_sig_ts < ZSCORE_WARMUP_SEC:
            return 0.0
        mean = self._tfi_s1 / n
        var = max(0.0, self._tfi_s2 / n - mean * mean)
        std = math.sqrt(var)
        if std < 1e-6:
            return 0.0
        return max(-3.0, min(3.0, (self.tfi_hist[-1][1] - mean) / std))

    def zscore(self) -> float:
        """Alpha 1: OFI bucketé STANDARDISÉ (z-score glissant), clampé ±3."""
        with self.lock:
            return self._zscore_locked(time.time())

    def tfi_zscore(self) -> float:
        """Alpha 2: flux agresseur bucketé standardisé, clampé ±3."""
        with self.lock:
            return self._tfi_zscore_locked(time.time())

    def micro(self) -> float:
        """Micro-price de Stoikov (fair de base). 0 si pas encore de book."""
        with self.lock:
            return self.micro_px

    def level_size(self, side: str, px: float) -> float:
        """Taille affichée au niveau px (0 si absent) — queue model."""
        with self.lock:
            book = self.book_bid_szs if side == "B" else self.book_ask_szs
            return book.get(px, 0.0)

    def beta_bps(self) -> float:
        """Sensibilité apprise fair↔z_OFI (bps/z). Warm-up → C1_BPS."""
        with self.lock:
            if self._beta_n < BETA_MIN_SAMPLES:
                return C1_BPS
            return self.beta_bps_val

    def alpha_bps(self) -> float:
        """Ajustement de fair total: β1×z_OFI + β2×z_TFI."""
        with self.lock:
            now = time.time()
            if self._beta_n < BETA_MIN_SAMPLES:
                a = C1_BPS * self._zscore_locked(now)
            else:
                a = (self.beta_bps_val * self._zscore_locked(now)
                     + self.beta2_bps_val * self._tfi_zscore_locked(now))
            return max(-ALPHA_CAP_BPS, min(ALPHA_CAP_BPS, a))

    def flow_fast(self, window_sec: float = 3.0) -> float:
        """Flux agresseur sur fenêtre courte (sous-ensemble de la deque 10s)."""
        cutoff = time.time() - window_sec
        with self.lock:
            num = tot = 0.0
            for ts, v in reversed(self.trades):
                if ts < cutoff:
                    break
                num += v
                tot += abs(v)
            return (num / tot) if tot > 0 else 0.0

    def vol_spike(self) -> float:
        """Vol instantanée (|Δmid 1s|) vs vol EWMA — ≥3 = mouvement anormal."""
        m1 = abs(self.momentum_bps(1.0))
        with self.lock:
            base = max(self.vol_bps_ewma, 0.05)
        return m1 / base

    def regime(self) -> Tuple[Optional[str], int]:
        """Détecteur pump/dump avec hystérésis.

        Déclenchement: |Δmid 1s| ≥ PUMP_MOM_BPS ET (vol spike ≥ N× OU flux
        agresseur 3s quasi one-sided). L'état persiste tant que le move fait
        de nouveaux extrêmes; il tombe si le momentum s'inverse ou après
        REGIME_QUIET_SEC sans nouvel extrême. Retourne (état, intensité 0-3).
        """
        m1 = self.momentum_bps(1.0)
        spike = self.vol_spike()
        ff = self.flow_fast()
        now = time.time()
        trigger = abs(m1) >= PUMP_MOM_BPS and (spike >= PUMP_VOL_MULT or abs(ff) >= PUMP_FLOW_TH)
        with self.lock:
            latest = self.mids[-1][1] if self.mids else 0.0
            if self._regime is None:
                if trigger and latest > 0:
                    self._regime = "PUMP" if m1 > 0 else "DUMP"
                    self._regime_ts = now
                    self._regime_ext = latest
            else:
                if self._regime == "PUMP" and latest > self._regime_ext:
                    self._regime_ext = latest
                    self._regime_ts = now
                elif self._regime == "DUMP" and latest < self._regime_ext:
                    self._regime_ext = latest
                    self._regime_ts = now
                inverted = ((self._regime == "PUMP" and m1 < -0.5)
                            or (self._regime == "DUMP" and m1 > 0.5))
                if inverted or now - self._regime_ts > REGIME_QUIET_SEC:
                    self._regime = None
            reg = self._regime
        if not reg:
            return None, 0
        a = abs(m1)
        return reg, 1 + (1 if a >= 2 * PUMP_MOM_BPS else 0) + (1 if a >= 10.0 else 0)

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

        # Télémétrie (étape 1 du plan): fills + markouts persistés en SQLite.
        self.telemetry = FillLogger(SYMBOL) if (FillLogger and live) else None
        self.pending_telem: List[list] = []   # [ts, tid, side, px, next_horizon_idx]

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
        self.last_orderupdate_ts = time.time()  # dernier event orderUpdates reçu
        self.pause_until = 0.0
        self.hourly_pause_until = 0.0
        self.force_quote = False      # fill/régime → bypass du coalesce
        self.last_quote_bb = 0.0      # BBO au moment du dernier quote (urgence)
        self.last_quote_ba = 0.0

        # PnL positif: take-profit actif + protection de session (sans arrêt)
        self.upnl_peak_bps = 0.0      # plus haut uPnL (bps) de la position courante
        self.session_net_high = 0.0   # high-water du net de session
        self.protect = 0              # 0 = normal, 1/2 = resserrement progressif
        self.size_mult = 1.0          # multiplicateur de taille (PROTECT2: 0.7)
        self._last_regime: Optional[str] = None
        self.session_mode = session_state_utc(
            SESSION_WINDOWS_UTC, blackout_min=OPEN_BLACKOUT_MIN)

        # Stats
        self.session_pnl = 0.0
        self.total_fees = 0.0
        self.total_volume = 0.0
        self.fills_count = 0
        self.maker_fills = 0
        self.hourly_net_events: deque = deque()  # (ts, closedPnl - fee)
        self.hourly_net_pnl = 0.0
        self.hourly_loss_latched = False
        self.processed_fills: set = set()
        self.last_fill_ts = 0.0
        self.last_action = "init"
        self.last_error = ""
        self.start_time = time.time()

        self.running = True
        self.wake = threading.Event()
        self.console = Console()
        # Le thread WS peut maintenant envoyer des actions d'urgence (cancel
        # de sweep, scratch toxique) en concurrence avec la boucle principale:
        # sérialiser les appels exchange (requests.Session non thread-safe).
        self.api_lock = threading.Lock()
        self._emerg_ts: Dict[str, float] = {"B": 0.0, "A": 0.0}
        self._scratch_ts = 0.0
        # Queue position model: taille devant nous à la pose / restante.
        self.queue_init: Dict[int, float] = {}
        self.queue_ahead: Dict[int, float] = {}

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
            got = False
            buy_usd = 0.0
            sell_usd = 0.0
            trade_px_sz = []
            for t in data.get("data", []) or []:
                ts = float(t.get("time", 0)) / 1000.0 or time.time()
                px_t, sz_t = float(t["px"]), float(t["sz"])
                sd_t = str(t.get("side", ""))
                self.signal.on_trade(sd_t, px_t, sz_t, ts)
                trade_px_sz.append((px_t, sz_t, sd_t))
                if sd_t == "B":
                    buy_usd += px_t * sz_t
                else:
                    sell_usd += px_t * sz_t
                got = True
            if got:
                # Queue model: un trade à NOTRE niveau consomme la file devant
                # nous (agresseur vendeur → niveaux bid; acheteur → ask).
                if QUEUE_MODEL and self.queue_ahead:
                    with self.lock:
                        for oid_q, d_q in self.orders.items():
                            if oid_q not in self.queue_ahead:
                                continue
                            for px_t2, sz_t2, sd_t2 in trade_px_sz:
                                hit_side = "B" if sd_t2 != "B" else "A"
                                if d_q["side"] == hit_side and abs(d_q["px"] - px_t2) < 1e-9:
                                    self.queue_ahead[oid_q] = max(
                                        0.0, self.queue_ahead[oid_q] - sz_t2)
                # CHEMIN D'URGENCE: un burst agresseur massif dans CE batch
                # fonce vers nos quotes ouvrantes → cancel immédiat depuis ce
                # thread WS, sans attendre le cycle de la boucle principale.
                # (vendeur agressif → nos bids en danger; acheteur → nos asks)
                threshold = EMERG_SWEEP_MULT * ORDER_NOTIONAL_USD
                if sell_usd >= threshold:
                    self._emergency_pull("B", sell_usd)
                if buy_usd >= threshold:
                    self._emergency_pull("A", buy_usd)
                # Le flux agresseur est un signal (flow/pump-dump): réveiller
                # la boucle au lieu d'attendre le prochain tick L2.
                self.wake.set()
        except Exception:
            pass

    def _emergency_pull(self, side: str, burst_usd: float):
        """Retire les quotes OUVRANTES d'un côté, immédiatement, depuis le
        thread WS. Ne touche jamais aux sorties reduce-only (un dump qui
        remplit notre bid de rachat de short est un BON fill)."""
        now = time.time()
        if not self.live or now - self._emerg_ts[side] < EMERG_COOLDOWN_SEC:
            return
        with self.lock:
            oids = [o for o, d in self.orders.items()
                    if d["side"] == side and d.get("ro") is False]
        if not oids:
            return
        self._emerg_ts[side] = now
        self.cancel_oids(oids)
        with self.lock:
            self.last_intent = None
            self.last_action = f"⚡ sweep ${burst_usd:,.0f} → pull {side}"
        self.force_quote = True
        self.wake.set()

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
                    # Télémétrie: contexte complet du fill (attribution).
                    if self.telemetry is not None:
                        try:
                            bb_f, ba_f, mid_f = self.best_bid, self.best_ask, self.mid
                            tick_f = dyn_tick(mid_f if mid_f > 0 else px)
                            dist = (abs(px - (bb_f if sd == "B" else ba_f)) / tick_f
                                    if bb_f > 0 and ba_f > 0 else 0.0)
                            reg_f, _ = self.signal.regime()
                            self.telemetry.log_fill({
                                "tid": tid, "ts": now_f, "side": sd,
                                "px": px, "sz": sz,
                                "fee": float(f.get("fee", 0)),
                                "closed_pnl": float(f.get("closedPnl", 0)),
                                "crossed": 1 if bool(f.get("crossed", False)) else 0,
                                "z_ofi": self.signal.zscore(),
                                "z_tfi": self.signal.tfi_zscore(),
                                "alpha_bps": self.signal.alpha_bps(),
                                "spread_bps": ((ba_f - bb_f) / mid_f * 1e4
                                               if mid_f > 0 and ba_f > bb_f > 0 else 0.0),
                                "vol_bps": self.signal.vol_bps(),
                                "regime": reg_f or "",
                                "mid": mid_f,
                                "position_before": self.position,
                                "dist_bbo_ticks": dist,
                                "hour_utc": time.gmtime(now_f).tm_hour,
                            })
                            self.pending_telem.append([now_f, tid, sd, px, 0])
                        except Exception:
                            pass
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
                    closed_pnl = float(f.get("closedPnl", 0))
                    self.total_fees += fee
                    self.session_pnl += closed_pnl
                    self.hourly_net_events.append((now_f, closed_pnl - fee))
                    self.fills_count += 1
                    if not bool(f.get("crossed", False)):
                        self.maker_fills += 1
                    self.last_fill_ts = time.time()
                    # Flip éclair: reposter la sortie immédiatement, sans
                    # attendre la fenêtre de coalesce.
                    self.force_quote = True
            self.wake.set()
            # SCRATCH TOXIQUE (hors self.lock): si on vient de se faire
            # remplir en OUVERTURE alors que l'alpha est franchement contre
            # la position, sortir en IOC tout de suite — 0.83 bps de taker
            # valent mieux que les 3-10 bps du move qui suit un fill informé.
            self._maybe_toxic_scratch()
        except Exception:
            pass

    def _maybe_toxic_scratch(self):
        now = time.time()
        if not self.live or now - self._scratch_ts < SCRATCH_COOLDOWN_SEC:
            return
        with self.lock:
            pos = self.position
            bb, ba = self.best_bid, self.best_ask
        if abs(pos) < MIN_SIZE / 2 or bb <= 0 or ba <= bb:
            return
        z = self.signal.zscore()
        z2 = self.signal.tfi_zscore()
        reg, _ = self.signal.regime()
        toxic_long = pos > 0 and (min(z, z2) <= -TOXIC_SCRATCH_Z or reg == "DUMP")
        toxic_short = pos < 0 and (max(z, z2) >= TOXIC_SCRATCH_Z or reg == "PUMP")
        if not (toxic_long or toxic_short):
            return
        self._scratch_ts = now
        tick = dyn_tick((bb + ba) / 2.0)
        is_buy = pos < 0
        sz = round_sz(abs(pos))
        px = (ba + 10 * tick) if is_buy else (bb - 10 * tick)
        px = px_up(max(px, tick), tick) if is_buy else px_down(max(px, tick), tick)
        try:
            with self.api_lock:
                self.exchange.order(name=SYMBOL, is_buy=is_buy, sz=sz, limit_px=px,
                                    order_type={"limit": {"tif": "Ioc"}}, reduce_only=True)
            with self.lock:
                self.last_action = f"⚡ scratch toxique (z {z:+.1f}/{z2:+.1f} {reg or ''})"
                # Pause du côté qui rouvrirait dans le sens toxique.
                self.side_pause_until["B" if toxic_long else "A"] = now + BURST_PAUSE_SEC
            self.force_quote = True
            self.wake.set()
        except Exception as exc:
            with self.lock:
                self.last_error = f"scratch: {str(exc)[:80]}"

    def _on_order_updates(self, data):
        try:
            payload = data.get("data", [])
            updates = payload if isinstance(payload, list) else payload.get("orders", [])
            terminal = ("fill", "cancel", "reject", "margin", "reduceonly", "badalo", "expired")
            changed = False
            self.last_orderupdate_ts = time.time()
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
                            self.queue_init.pop(oid, None)
                            self.queue_ahead.pop(oid, None)
                            changed = True
                    elif status in {"open", "resting"} and oid not in self.orders:
                        self.orders[oid] = {
                            "side": "B" if order.get("side") == "B" else "A",
                            "px": float(order.get("limitPx", 0) or 0),
                            "sz": float(order.get("sz", 0) or 0),
                            "ro": None,   # flag inconnu → inéligible au modify
                            "ts": time.time(),
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
                oid = int(o["oid"])
                known = self.orders.get(oid, {})
                real[oid] = {
                    "side": "B" if o.get("side") == "B" else "A",
                    "px": float(o.get("limitPx", 0) or 0),
                    "sz": float(o.get("sz", 0) or 0),
                    # garder le flag RO et l'âge si on les connaissait déjà
                    "ro": known.get("ro"),
                    "ts": known.get("ts", time.time()),
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
            with self.api_lock:
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
                    self.queue_init.pop(oid, None)
                    self.queue_ahead.pop(oid, None)
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
                with self.api_lock:
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
                self.last_error = ""   # action OK: purger le message collant
                for req, status in zip(pending, statuses):
                    if isinstance(status, dict) and "resting" in status:
                        oid = int(status["resting"]["oid"])
                        if oid not in self.orders:
                            self.orders[oid] = {"side": "B" if req["is_buy"] else "A",
                                                "px": req["limit_px"], "sz": req["sz"],
                                                "ro": req["reduce_only"],
                                                "ts": time.time()}
                            # Queue model: taille affichée au niveau AVANT nous
                            # = file devant notre ordre.
                            qa = self.signal.level_size(
                                "B" if req["is_buy"] else "A", req["limit_px"])
                            self.queue_init[oid] = max(qa, 1e-9)
                            self.queue_ahead[oid] = qa
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

    def modify_quotes(self, pairs: List[Tuple[int, Dict[str, Any]]]) -> bool:
        """Requote en UN round-trip via batchModify (au lieu de cancel+place).
        pairs = [(old_oid, req)] avec req au format place_quotes. Un modify
        réussi renvoie un NOUVEL oid → remplacé dans le tracking. Retourne
        False au moindre échec: l'appelant invalide last_intent et le cycle
        suivant repasse par le chemin cancel+place classique (anti-fantômes)."""
        if not pairs:
            return True
        with self.lock:
            bb, ba = self.best_bid, self.best_ask
        tick = dyn_tick(self.mid)
        modifies = []
        for oid, r in pairs:
            # Clamp de dernière milliseconde au BBO le plus frais (jamais croiser).
            if r["is_buy"] and ba > 0:
                r["limit_px"] = min(r["limit_px"], px_down(ba - tick, tick))
            elif not r["is_buy"] and bb > 0:
                r["limit_px"] = max(r["limit_px"], px_up(bb + tick, tick))
            modifies.append({"oid": oid, "order": {
                "coin": SYMBOL, "is_buy": r["is_buy"], "sz": r["sz"],
                "limit_px": r["limit_px"],
                "order_type": {"limit": {"tif": "Alo"}},
                "reduce_only": r["reduce_only"], "cloid": None,
            }})
        try:
            with self.api_lock:
                res = self.exchange.bulk_modify_orders_new(modifies)
        except Exception as exc:
            with self.lock:
                self.last_error = f"modify: {str(exc)[:100]}"
            return False
        if not isinstance(res, dict) or res.get("status") != "ok":
            with self.lock:
                self.last_error = f"modify: {str(res)[:100]}"
            return False
        statuses = res.get("response", {}).get("data", {}).get("statuses", [])
        ok_all = len(statuses) == len(pairs)
        with self.lock:
            self.last_quote_ts = time.time()
            self.last_error = ""   # action OK: purger le message collant
            for (oid, r), status in zip(pairs, statuses):
                if isinstance(status, dict) and "resting" in status:
                    new_oid = int(status["resting"]["oid"])
                    self.orders.pop(oid, None)
                    self.queue_init.pop(oid, None)
                    self.queue_ahead.pop(oid, None)
                    self.orders[new_oid] = {"side": "B" if r["is_buy"] else "A",
                                            "px": r["limit_px"], "sz": r["sz"],
                                            "ro": r["reduce_only"],
                                            "ts": time.time()}
                    # Modify = fin de queue au nouveau prix.
                    qa = self.signal.level_size(
                        "B" if r["is_buy"] else "A", r["limit_px"])
                    self.queue_init[new_oid] = max(qa, 1e-9)
                    self.queue_ahead[new_oid] = qa
                elif isinstance(status, dict) and "filled" in status:
                    self.orders.pop(oid, None)   # modifié puis rempli — userFills gère
                else:
                    ok_all = False
                    self.last_error = str(status.get("error", status)
                                          if isinstance(status, dict) else status)[:100]
        return ok_all

    def ioc_close(self, reason: str):
        """Ferme la POSITION en IOC reduce-only sans annuler les quotes —
        c'est le 'bank': encaisser le profit (ou couper) en payant le taker
        (0.83 bps) quand le move le justifie. flatten() reste le hard-stop."""
        with self.lock:
            pos = self.position
            bb, ba = self.best_bid, self.best_ask
        if abs(pos) < MIN_SIZE / 2 or bb <= 0 or ba <= bb:
            return
        if not self.live:
            self.last_action = f"[dry] bank ({reason})"
            return
        tick = dyn_tick(self.mid)
        is_buy = pos < 0
        sz = round_sz(abs(pos))
        px = (ba + 20 * tick) if is_buy else (bb - 20 * tick)
        px = px_up(max(px, tick), tick) if is_buy else px_down(max(px, tick), tick)
        try:
            self.exchange.order(name=SYMBOL, is_buy=is_buy, sz=sz, limit_px=px,
                                order_type={"limit": {"tif": "Ioc"}}, reduce_only=True)
            self.last_action = f"bank ({reason})"
            self.force_quote = True   # re-quoter immédiatement après
        except Exception as exc:
            with self.lock:
                self.last_error = f"bank: {str(exc)[:100]}"

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
            with self.api_lock:
                self.exchange.order(name=SYMBOL, is_buy=is_buy, sz=sz, limit_px=px,
                                    order_type={"limit": {"tif": "Ioc"}}, reduce_only=True)
            self.last_action = f"flatten ({reason})"
        except Exception as exc:
            with self.lock:
                self.last_error = f"flatten: {str(exc)[:100]}"

    def _queue_fraction(self, side: str) -> Optional[float]:
        """Fraction de la file initiale encore devant l'ordre ouvrant."""
        if not QUEUE_MODEL:
            return None
        with self.lock:
            candidates = [
                (oid, dict(order)) for oid, order in self.orders.items()
                if order["side"] == side and order.get("ro") is False
                and oid in self.queue_ahead
            ]
        fractions = []
        for oid, order in candidates:
            displayed = self.signal.level_size(side, order["px"])
            with self.lock:
                if oid not in self.queue_ahead:
                    continue
                ahead = min(self.queue_ahead[oid], max(0.0, displayed - order["sz"]))
                self.queue_ahead[oid] = ahead
                fractions.append(ahead / max(self.queue_init.get(oid, 1e-9), 1e-9))
        return min(fractions) if fractions else None

    # ---------------------------------------------------------------- quoting
    def compute_intent(self) -> Tuple[Optional[Tuple[float, float, bool]],
                                      Optional[Tuple[float, float, bool]], str]:
        """Retourne (bid, ask, tag) où bid/ask = (px, sz, reduce_only) ou None.

        Empilement de couches, dans l'ordre:
          1. RESERVATION PRICE (hftbacktest): z-score → fair → resa ± half,
             skew d'inventaire continu; retrait d'un côté si |z| ≥ ZSCORE_PULL
          1b. régime PUMP/DUMP: côtés ouvrants retirés; sortie en ride/flee
          2. momentum court (openings)                       → pas de quote côté balayé
          3. sweep breaker + cooldown post-fill (openings)   → retrait temporaire
          4. probation markout (openings)                    → recul / retrait
          5. EV gate (openings)                              → recule jusqu'à EV≥0 ou retire
          6. vol widen (openings)                            → recul
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

        session_mode = session_state_utc(
            SESSION_WINDOWS_UTC, now, OPEN_BLACKOUT_MIN)
        self.session_mode = session_mode
        mom_f = self.signal.momentum_bps(MOM_FAST_SEC)
        mom_s = self.signal.momentum_bps(MOM_SLOW_SEC)
        vol = self.signal.vol_bps()
        z = self.signal.zscore()
        reg, reg_i = self.signal.regime()

        order_sz = round_sz(max(MIN_SIZE, ORDER_NOTIONAL_USD * self.size_mult / mid))
        if order_sz * mid < 10.0:
            order_sz = round_sz(math.ceil((10.5 / mid) / (10 ** -SZ_DECIMALS)) * (10 ** -SZ_DECIMALS))
        max_pos = MAX_INV_ORDERS * order_sz
        eps = MIN_SIZE / 2

        # ---- 1. Reservation price continu (hftbacktest):
        #   fair = mid × (1 + C1×z/1e4)  — le z-score déplace la juste valeur
        #   resa = fair − mid × SKEW/1e4 × (pos/order_sz)  — skew d'inventaire
        #   cibles = resa ± half, clampées pour ne jamais améliorer le BBO.
        inv_units = pos / max(order_sz, MIN_SIZE)
        # Fair = micro-price de Stoikov ajustée par l'alpha OFI auto-calibré
        # (beta appris en ligne × z de l'OFI, clampé) — plus de c1 en dur.
        alpha_bps = self.signal.alpha_bps()
        if session_mode == "off":
            # Hors cash session: les signaux lead/lag sont moins fiables. On
            # revient à une faible mean-reversion du momentum lent.
            alpha_bps = max(-ALPHA_CAP_BPS, min(ALPHA_CAP_BPS, -0.25 * mom_s))
        base_px = self.signal.micro() or mid
        fair = base_px * (1.0 + alpha_bps / 10000.0)
        resa = fair - mid * (SKEW_BPS / 10000.0) * inv_units
        # Demi-spread de BASE (mode profit hftbacktest): on quote À DISTANCE
        # du mid; seul un alpha/skew favorable ramène un côté vers le BBO
        # (clamp min/max) — coller au BBO en continu = cost/M qui explose.
        half_px = max(HALF_SPREAD_BPS, MAKER_FEE_BPS + self._eff_min_ev()) * mid / 10000.0
        bid_t = min(resa - half_px, bb)
        ask_t = max(resa + half_px, ba)
        extra_bid = max(0.0, round((bb - bid_t) / tick))   # recul en ticks vs BBO
        extra_ask = max(0.0, round((ask_t - ba) / tick))
        quote_bid = True
        quote_ask = True
        tag = f"z{z:+.1f}"
        if session_mode != "open":
            tag += f" | session:{session_mode}"
        if session_mode != "off" and z >= ZSCORE_PULL:
            quote_ask = False
            tag += " | ask✗z"
        elif session_mode != "off" and z <= -ZSCORE_PULL:
            quote_bid = False
            tag += " | bid✗z"

        # Un côté est "ouvrant" s'il augmente l'exposition; le côté qui
        # déboucle une position échappe aux couches 1b-6 (il DOIT rester).
        bid_is_opening = pos > -eps
        ask_is_opening = pos < eps

        # Dix minutes autour de l'open configuré: sorties uniquement.
        if session_mode == "blackout":
            if bid_is_opening:
                quote_bid = False
            if ask_is_opening:
                quote_ask = False
            tag += " | open blackout"

        # Le coupe-circuit horaire interdit les nouvelles expositions, mais ne
        # supprime jamais la quote reduce-only qui permet de sortir proprement.
        if now < self.hourly_pause_until:
            if bid_is_opening:
                quote_bid = False
            if ask_is_opening:
                quote_ask = False
            tag += " | hourly pause"

        # ---- 1b. Régime PUMP/DUMP: on ne pose AUCUN ordre ouvrant pendant
        # l'événement (exception assumée au volume) — la sortie, elle, est
        # gérée plus bas (ride/flee).
        if reg:
            tag += f" | {reg}{reg_i}"
            if bid_is_opening:
                quote_bid = False
            if ask_is_opening:
                quote_ask = False

        # ---- 2. Momentum: pas de quote ouvrante du côté qui se fait courir dessus
        mom_gate = MOM_GATE_BPS * (2.0 if session_mode == "off" else 1.0)
        if bid_is_opening and quote_bid and (
                mom_f <= -mom_gate
                or (mom_f <= -0.6 * mom_gate and mom_s <= -mom_gate)):
            quote_bid = False
            tag += " | mom↓"
        if ask_is_opening and quote_ask and (
                mom_f >= mom_gate
                or (mom_f >= 0.6 * mom_gate and mom_s >= mom_gate)):
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
        qf_bid = self._queue_fraction("B")
        qf_ask = self._queue_fraction("A")

        def queue_probs(qfrac: Optional[float]) -> Tuple[float, float]:
            if qfrac is None:
                return 1.0, 1.0  # comportement historique avant la première pose
            q = max(0.0, min(1.0, qfrac))
            return max(0.20, 1.0 - q), max(0.20, q)

        if bid_is_opening and quote_bid:
            adverse_pred = max(0.0, -mo_b)
            p_good, p_toxic = queue_probs(qf_bid)
            capture = spread_bps / 2 + extra_bid * tick_bps
            need_bps = (self._eff_min_ev() + MAKER_FEE_BPS
                        + adverse_pred * p_toxic) - capture * p_good
            if need_bps > 0:
                add = math.ceil(need_bps / max(tick_bps * p_good, 1e-9))
                if extra_bid + add > EV_MAX_EXTRA_TICKS:
                    quote_bid = False
                    tag += " | EV✗bid"
                else:
                    extra_bid += add
        if ask_is_opening and quote_ask:
            adverse_pred = max(0.0, -mo_a)
            p_good, p_toxic = queue_probs(qf_ask)
            capture = spread_bps / 2 + extra_ask * tick_bps
            need_bps = (self._eff_min_ev() + MAKER_FEE_BPS
                        + adverse_pred * p_toxic) - capture * p_good
            if need_bps > 0:
                add = math.ceil(need_bps / max(tick_bps * p_good, 1e-9))
                if extra_ask + add > EV_MAX_EXTRA_TICKS:
                    quote_ask = False
                    tag += " | EV✗ask"
                else:
                    extra_ask += add

        # ---- 6. Vol widen (le skew d'inventaire est déjà continu via resa)
        vol_extra = int(min(3.0, max(0.0, (vol * VOL_WIDEN_MULT - spread_bps / 2) / max(tick_bps, 1e-9))))
        if vol_extra > 0:
            if bid_is_opening:
                extra_bid += vol_extra
            if ask_is_opening:
                extra_ask += vol_extra
            tag += f" | vol+{vol_extra}"

        # Prix ouvrants
        bid_px = px_down(bb - extra_bid * tick, tick)
        ask_px = px_up(ba + extra_ask * tick, tick)

        # ---- 7. Inventaire: cap, reduce-only, sortie ancrée
        bid_ro = False
        ask_ro = False
        bid_sz = order_sz
        ask_sz = order_sz
        # Sizing par conviction (étape 4, Kelly-lite): le côté favorisé par
        # l'alpha grossit (jusqu'à ×2), le côté défavorisé maigrit (jusqu'à
        # ×0.5) — l'exposition suit la probabilité que le fill soit bon.
        if SIZING_CONVICTION and ALPHA_CAP_BPS > 0:
            conv = min(1.0, abs(alpha_bps) / ALPHA_CAP_BPS)
            fav = round_sz(min(max_pos, order_sz * (1.0 + conv)))
            unfav = round_sz(order_sz * (1.0 - 0.5 * conv))
            if unfav * mid < 10.5:          # jamais sous le min notionnel HL
                unfav = order_sz
            if alpha_bps > 0:
                bid_sz, ask_sz = fav, unfav
            elif alpha_bps < 0:
                bid_sz, ask_sz = unfav, fav
        if pos >= eps:                      # long → l'ask déboucle
            ask_sz = round_sz(min(order_sz, abs(pos)))
            ask_ro = ask_sz >= MIN_SIZE
            quote_ask = ask_ro
            age = now - pos_since if pos_since else 1e9
            if reg == "PUMP":
                # Long dans le sens du move: RIDE — l'exit colle au BBO et
                # trail le prix à chaque tick (tête de queue, bypass coalesce).
                ask_px = px_up(ba, tick)
                tag += " | ride"
            elif reg == "DUMP":
                # Long contre le move: FLEE — sortie au BBO immédiate (le cut
                # IOC de risk_checks prend le relais si intensité ≥ 2).
                ask_px = px_up(ba, tick)
                tag += " | flee"
            elif age <= UNWIND_ANCHOR_MAX_SEC and entry > 0:
                # Sortie ancrée: viser au moins entry + profit, pas un scratch.
                ask_px = max(px_up(ba, tick), px_up(entry + PROFIT_TICKS * tick, tick))
                tag += " | exit@entry+"
            else:
                ask_px = px_up(ba, tick)   # trop vieux: rejoint le BBO
                if age > self._eff_max_hold():
                    tag += " | forced unwind"
            if pos + bid_sz > max_pos + eps:
                quote_bid = False
                tag += " | invcap"
        elif pos <= -eps:                   # short → le bid déboucle
            bid_sz = round_sz(min(order_sz, abs(pos)))
            bid_ro = bid_sz >= MIN_SIZE
            quote_bid = bid_ro
            age = now - pos_since if pos_since else 1e9
            if reg == "DUMP":
                bid_px = px_down(bb, tick)
                tag += " | ride"
            elif reg == "PUMP":
                bid_px = px_down(bb, tick)
                tag += " | flee"
            elif age <= UNWIND_ANCHOR_MAX_SEC and entry > 0:
                bid_px = min(px_down(bb, tick), px_down(entry - PROFIT_TICKS * tick, tick))
                tag += " | exit@entry-"
            else:
                bid_px = px_down(bb, tick)
                if age > self._eff_max_hold():
                    tag += " | forced unwind"
            if -pos + ask_sz > max_pos + eps:
                quote_ask = False
                tag += " | invcap"

        bid = (bid_px, bid_sz, bid_ro) if (quote_bid and bid_sz >= MIN_SIZE) else None
        ask = (ask_px, ask_sz, ask_ro) if (quote_ask and ask_sz >= MIN_SIZE) else None
        return bid, ask, tag

    def apply_intent(self):
        now = time.time()
        tick = dyn_tick(self.mid if self.mid > 0 else 1.0)

        # --- Urgence: fill reçu / BBO qui a bougé / une de nos quotes
        # traversée par le marché → on saute la fenêtre de coalesce.
        with self.lock:
            bb, ba = self.best_bid, self.best_ask
            force = self.force_quote
            self.force_quote = False
            lqbb, lqba = self.last_quote_bb, self.last_quote_ba
            crossed = any(
                (d["side"] == "B" and ba > 0 and d["px"] >= ba)
                or (d["side"] == "A" and bb > 0 and d["px"] <= bb)
                for d in self.orders.values()
            )
        mid_now = (bb + ba) / 2.0 if bb > 0 and ba > bb else self.mid
        # Tolérance sticky en bps (hystérésis): la reservation price flotte en
        # continu, on ne bouge un ordre que si la cible a VRAIMENT dérivé.
        tol = max(REPLACE_TOL_TICKS * tick,
                  mid_now * REQUOTE_TOL_BPS / 10000.0) + 1e-12
        urgent = (force or crossed
                  or (lqbb > 0 and abs(bb - lqbb) > tol)
                  or (lqba > 0 and abs(ba - lqba) > tol))
        if not urgent and now - self.last_action_ts < ACTION_COALESCE_SEC:
            return

        bid, ask, tag = self.compute_intent()
        # Intent quantifié pour la comparaison sticky
        intent = (
            None if bid is None else (round(bid[0], PRICE_DECIMALS), bid[1], bid[2]),
            None if ask is None else (round(ask[0], PRICE_DECIMALS), ask[1], ask[2]),
        )
        with self.lock:
            resting_bids = {o: d for o, d in self.orders.items() if d["side"] == "B"}
            resting_asks = {o: d for o, d in self.orders.items() if d["side"] == "A"}
            last_intent = self.last_intent

        # Queue position model (étape 3): fraction de file restante devant nos
        # ordres ouvrants, resynchronisée avec la taille affichée au niveau.
        alpha_now = self.signal.alpha_bps()
        qf_b = self._queue_fraction("B")
        qf_a = self._queue_fraction("A")
        adv_b = alpha_now < -QUEUE_ADV_ALPHA_BPS   # alpha baissier → bid en danger
        adv_a = alpha_now > QUEUE_ADV_ALPHA_BPS

        # Fond de file + alpha adverse: viser explicitement le niveau suivant.
        # La cible est calculée depuis le fair courant, donc le déplacement ne
        # dérive pas d'un tick supplémentaire à chaque cycle.
        if (intent[0] is not None and not intent[0][2]
                and qf_b is not None and qf_b >= QUEUE_DEEP_FRAC and adv_b):
            intent = ((px_down(intent[0][0] - tick, tick), intent[0][1], False), intent[1])
            tag += " | own-next B"
        if (intent[1] is not None and not intent[1][2]
                and qf_a is not None and qf_a >= QUEUE_DEEP_FRAC and adv_a):
            intent = (intent[0], (px_up(intent[1][0] + tick, tick), intent[1][1], False))
            tag += " | own-next A"

        def side_ok(want, resting: Dict[int, Dict], qfrac=None, adverse=False) -> bool:
            """Sticky queue-aware: front de file → tolérance doublée (le fill
            imminent est bénin, ne surtout pas perdre la place); fond de file
            + alpha contre nous → replace 'gratuit' (on ne perd rien)."""
            if want is None:
                return not resting
            if len(resting) != 1:
                return False
            d = next(iter(resting.values()))
            diff = abs(d["px"] - want[0])
            if qfrac is not None and not want[2]:
                if qfrac >= QUEUE_DEEP_FRAC and adverse and diff >= tick - 1e-12:
                    return False
                if qfrac <= QUEUE_FRONT_FRAC:
                    return diff <= tol * 2.0
            return diff <= tol

        def must_act(want, resting: Dict[int, Dict], side: str) -> bool:
            """Repos minimal: un ordre OUVRANT qui vient d'être posé ne bouge
            pas avant MIN_REST_SEC — c'est en REPOSANT qu'on se fait remplir
            et qu'on gagne la priorité de queue. Urgences qui imposent d'agir
            quand même: retrait demandé, rien au repos, sortie RO à poser,
            fill qui vient d'arriver, ou ordre traversé par le BBO."""
            if want is None or not resting:
                return True
            if want[2] or force:
                return True
            for d in resting.values():
                if side == "B" and ba > 0 and d["px"] >= ba:
                    return True
                if side == "A" and bb > 0 and d["px"] <= bb:
                    return True
            newest = max(d.get("ts", 0.0) for d in resting.values())
            return now - newest >= MIN_REST_SEC

        bid_ok = side_ok(intent[0], resting_bids, qf_b, adv_b) \
            or not must_act(intent[0], resting_bids, "B")
        ask_ok = side_ok(intent[1], resting_asks, qf_a, adv_a) \
            or not must_act(intent[1], resting_asks, "A")
        if bid_ok and ask_ok and last_intent == intent:
            with self.lock:
                self.last_quote_bb, self.last_quote_ba = bb, ba
            return

        def req_for(want, is_buy: bool) -> Dict[str, Any]:
            return {"coin": SYMBOL, "is_buy": is_buy, "sz": want[1],
                    "limit_px": want[0],
                    "order_type": {"limit": {"tif": "Alo"}},
                    "reduce_only": want[2]}

        # Décision par côté: 1 ordre au repos → 1 ordre voulu = batchModify
        # (UN round-trip, moitié du budget rate-limit); sinon cancel+place.
        # Le modify n'est PAS éligible si le flag reduce-only change (HL
        # rejette "invalid new order" sur cette transition — ex: l'ordre
        # ouvrant qui doit devenir la sortie RO après un fill) ou si le flag
        # de l'ordre au repos est inconnu (découvert via WS/réconciliation).
        allow_modify = now >= getattr(self, "_modify_block_until", 0.0)

        def modifiable(want, resting: Dict[int, Dict]) -> bool:
            # Uniquement les quotes OUVRANTES (non reduce-only, flag connu et
            # inchangé): les sorties RO changent de taille avec la position et
            # HL rejette trop de transitions ("invalid new order").
            if want is None or want[2] or len(resting) != 1 or not allow_modify:
                return False
            d = next(iter(resting.values()))
            return d.get("ro") is False

        to_cancel: List[int] = []
        place_reqs: List[Dict[str, Any]] = []
        modify_pairs: List[Tuple[int, Dict[str, Any]]] = []
        if not bid_ok:
            if modifiable(intent[0], resting_bids):
                modify_pairs.append((next(iter(resting_bids)), req_for(intent[0], True)))
            else:
                to_cancel += list(resting_bids.keys())
                if intent[0] is not None:
                    place_reqs.append(req_for(intent[0], True))
        if not ask_ok:
            if modifiable(intent[1], resting_asks):
                modify_pairs.append((next(iter(resting_asks)), req_for(intent[1], False)))
            else:
                to_cancel += list(resting_asks.keys())
                if intent[1] is not None:
                    place_reqs.append(req_for(intent[1], False))

        self.last_action_ts = now
        self.last_action = tag
        with self.lock:
            self.last_quote_bb, self.last_quote_ba = bb, ba
        if not self.live:
            with self.lock:
                self.last_intent = intent
            return

        if modify_pairs and not self.modify_quotes(modify_pairs):
            # Modify raté (ordre disparu entre-temps, etc.): invalider et
            # forcer le chemin cancel+place pendant 1.5s — la réconciliation
            # et orderUpdates remettent le tracking d'équerre.
            self._modify_block_until = now + 1.5
            with self.lock:
                self.last_intent = None
            return
        if to_cancel and not self.cancel_oids(to_cancel):
            return  # cancel raté: ne PAS reposer par-dessus (anti-doublons)
        # Vérifier qu'aucun fantôme du même côté ne reste avant de reposer
        if place_reqs:
            with self.lock:
                still_b = any(d["side"] == "B" for d in self.orders.values())
                still_a = any(d["side"] == "A" for d in self.orders.values())
            # Un côté passé par modify garde légitimement son ordre: ne
            # filtrer que les côtés qu'on s'apprête à POSER.
            modified_sides = {("B" if r["is_buy"] else "A") for _, r in modify_pairs}
            place_reqs = [r for r in place_reqs
                          if not (r["is_buy"] and still_b and "B" not in modified_sides)
                          and not (not r["is_buy"] and still_a and "A" not in modified_sides)]
            if place_reqs:
                self.place_quotes(place_reqs)
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
            # Markouts télémétrie multi-horizons (1/5/30s) — liste scannée,
            # chaque entrée avance d'horizon en horizon puis disparaît.
            if self.telemetry is not None and self.pending_telem:
                keep = []
                for ent in self.pending_telem:
                    ts_e, tid_e, sd_e, px_e, hidx = ent
                    while hidx < len(MARKOUT_HORIZONS) and now - ts_e >= MARKOUT_HORIZONS[hidx]:
                        mo_e = ((mid - px_e) / px_e * 10000.0 if sd_e == "B"
                                else (px_e - mid) / px_e * 10000.0)
                        self.telemetry.log_markout(tid_e, MARKOUT_HORIZONS[hidx], mo_e)
                        hidx += 1
                    if hidx < len(MARKOUT_HORIZONS):
                        ent[4] = hidx
                        keep.append(ent)
                self.pending_telem = keep

    # ---------------------------------------------- valeurs effectives PROTECT
    def _eff_min_ev(self) -> float:
        """EV minimal exigé, durci quand la session est en gains à protéger."""
        return MIN_EV_BPS + (0.05 if self.protect >= 1 else 0.0) \
                          + (0.05 if self.protect >= 2 else 0.0)

    def _eff_max_hold(self) -> float:
        return MAX_HOLD_SEC / (2.0 if self.protect >= 1 else 1.0)

    def _eff_stop_bps(self, vol: float) -> float:
        """Stop adverse DYNAMIQUE: 3× la vol courante (borné 6..STOP_ADVERSE),
        resserré de 25% en mode PROTECT — plus la journée est bonne, moins on
        laisse une position respirer contre nous."""
        stop = min(STOP_ADVERSE_BPS, max(6.0, 3.0 * vol))
        if self.protect >= 1:
            stop *= 0.75
        return stop

    # ------------------------------------------------------------------- risk
    def risk_checks(self):
        now = time.time()
        with self.lock:
            pos = self.position
            upnl = self.unrealized_pnl
            mid = self.mid
            pos_since = self.pos_since
        vol = self.signal.vol_bps()
        z = self.signal.zscore()
        reg, reg_i = self.signal.regime()
        notional = abs(pos) * mid
        upnl_bps = (upnl / notional) * 10000.0 if notional > 0 else 0.0

        # --- D. Protection du PnL de session: resserrement, JAMAIS d'arrêt.
        net = self.session_pnl + upnl - self.total_fees
        with self.lock:
            cutoff = now - 3600.0
            while self.hourly_net_events and self.hourly_net_events[0][0] < cutoff:
                self.hourly_net_events.popleft()
            self.hourly_net_pnl = sum(delta for _, delta in self.hourly_net_events)
            hourly_net = self.hourly_net_pnl
        hourly_limit = -abs(MAX_SESSION_LOSS_USD) * max(0.0, HOURLY_LOSS_MULT)
        if hourly_limit < 0 and hourly_net < hourly_limit and not self.hourly_loss_latched:
            self.hourly_loss_latched = True
            self.hourly_pause_until = max(self.hourly_pause_until, now + HOURLY_PAUSE_SEC)
            self.last_action = (
                f"hourly loss ${hourly_net:+.2f} — pause {HOURLY_PAUSE_SEC / 60:.0f}m"
            )
            self.force_quote = True
        elif hourly_net >= hourly_limit:
            self.hourly_loss_latched = False
        self.session_net_high = max(self.session_net_high, net)
        if self.session_net_high >= AHEAD_USD:
            if net <= self.session_net_high * 0.5:
                self.protect = 2          # gros retracement du high → très strict
            elif net >= self.session_net_high * 0.8 and self.protect == 2:
                self.protect = 1          # récupéré → on relâche d'un cran
            elif self.protect == 0:
                self.protect = 1          # en gains → premier resserrement
        self.size_mult = 0.7 if self.protect >= 2 else 1.0

        # --- C. uPnL peak + take-profit actif (encaisser au lieu de rendre).
        if notional <= 0:
            self.upnl_peak_bps = 0.0
        else:
            self.upnl_peak_bps = max(self.upnl_peak_bps, upnl_bps)
            z_opposed = (pos > 0 and z <= -1.0) or (pos < 0 and z >= 1.0)
            if self.upnl_peak_bps >= TP_MIN_BPS and (
                    upnl_bps <= self.upnl_peak_bps * (1.0 - TP_RETRACE) or z_opposed):
                self.ioc_close(f"tp {upnl_bps:+.1f}/{self.upnl_peak_bps:.1f}bps")
                self.upnl_peak_bps = 0.0
                return

        # --- C. Pump/dump: cut immédiat si la position est CONTRE le move.
        if reg and notional > 0 and reg_i >= 2:
            against = (pos > 0 and reg == "DUMP") or (pos < 0 and reg == "PUMP")
            if against:
                self.ioc_close(f"cut {reg}{reg_i}")
                self.pause_until = now + 5.0
                self._last_regime = reg
                return
        # Fin de régime aligné: le move s'essouffle → banker le ride.
        if self._last_regime and not reg and notional > 0:
            aligned = (pos > 0 and self._last_regime == "PUMP") or \
                      (pos < 0 and self._last_regime == "DUMP")
            if aligned and upnl_bps >= 1.0:
                self.ioc_close(f"bank {self._last_regime}-end {upnl_bps:+.1f}bps")
                self._last_regime = reg
                return
        self._last_regime = reg

        # --- Stop adverse dynamique (vol-scalé, protect-aware).
        if notional > 0 and upnl < 0 and abs(upnl_bps) >= self._eff_stop_bps(vol):
            self.flatten(f"stop {abs(upnl_bps):.1f}bps")
            self.pause_until = now + STOP_PAUSE_SEC
            return
        if pos_since and now - pos_since > self._eff_max_hold() + FORCE_FLAT_EXTRA and abs(pos) >= MIN_SIZE:
            self.flatten("max hold")
            self.pause_until = now + STOP_PAUSE_SEC / 2
            return
        if net <= -MAX_SESSION_LOSS_USD:
            self.last_action = f"SESSION LOSS LIMIT ${net:+.2f} — arrêt"
            self.running = False

        # Deadman feed
        if now - self.last_tick_ts > DEADMAN_FEED_SEC and self.ws_ready:
            if not self.refresh_bbo_rest():
                if abs(pos) >= MIN_SIZE:
                    self.flatten("dead feed")
                self.cancel_all()
            if now - self.last_ws_restart > WS_RESTART_COOLDOWN:
                self.last_ws_restart = now
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
                    "protect": self.protect,
                    "hourly_net": self.hourly_net_pnl,
                    "hourly_pause_until": self.hourly_pause_until,
                    "session_mode": self.session_mode,
                }
            reg, _ = self.signal.regime()
            data["regime"] = reg or ""
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
        z = self.signal.zscore()
        reg, reg_i = self.signal.regime()
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
        sig_color = "green" if z > 0.5 else "red" if z < -0.5 else "white"
        mo_b_c = "red" if mo_b <= MARKOUT_SOFT_BPS else "green" if mo_b > 0 else "white"
        mo_a_c = "red" if mo_a <= MARKOUT_SOFT_BPS else "green" if mo_a > 0 else "white"
        reg_txt = f"[bold red]{reg}{reg_i}[/]" if reg else "—"
        protect_txt = f" | protect{self.protect}" if self.protect else ""
        t.add_row(
            f"{SYMBOL}\nbid {bb:.{PRICE_DECIMALS}f}\nask {ba:.{PRICE_DECIMALS}f}\nmid {mid:.{PRICE_DECIMALS}f}\nvol {volat:.2f}bps",
            f"OBI {obi:+.2f} flow {flow:+.2f}\ntilt {tilt:+.2f}bps\n[{sig_color}]z {z:+.2f}[/] mom {mom:+.1f}\n"
            f"β {self.signal.beta_bps():.2f} α {self.signal.alpha_bps():+.2f}bps\n"
            f"session {self.session_mode}\n"
            f"mo [{mo_b_c}]B {mo_b:+.2f}[/]/[{mo_a_c}]A {mo_a:+.2f}[/]\nordres {n_orders} | {reg_txt}",
            f"pos {pos:+.{SZ_DECIMALS}f}\nentry {entry:.{PRICE_DECIMALS}f}\nuPnL ${upnl:+.2f} (pk {self.upnl_peak_bps:.1f})\nequity ${eq:.2f}",
            f"vol ${vol:,.0f}\nfills {fills} ({maker_pct:.0f}% maker)\n"
            f"fees ${fees:.2f} | net ${net:+.2f} | h1 ${self.hourly_net_pnl:+.2f}\n"
            f"cost/M ${cost_m:+.0f}{protect_txt}",
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
            self.wake.wait(timeout=0.25)
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
            # Ordres au repos mais AUCUN event orderUpdates/quote depuis
            # STALE_ORDERUPDATE_SEC → probables fantômes ("l'ordre ne bouge
            # plus"): réconciliation REST immédiate, sans attendre la cadence.
            stale = (self.orders
                     and now - max(self.last_orderupdate_ts, self.last_quote_ts) > STALE_ORDERUPDATE_SEC)
            if stale or now - self.last_reconcile_ts > RECONCILE_EVERY_SEC:
                self.last_reconcile_ts = now
                self.reconcile_orders()
                if stale:
                    self.last_orderupdate_ts = now   # une vérif à la fois
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
        if self.telemetry is not None:
            self.telemetry.close()
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
    # Demi-spread par token (préset), sauf si forcé par variable d'env.
    _tok_cfg = TOKENS.get(token.strip().upper(), {})
    SESSION_WINDOWS_UTC = tuple(
        (int(start), int(end)) for start, end in _tok_cfg.get("session_utc", ())
    )
    OPEN_BLACKOUT_MIN = int(_tok_cfg.get("open_blackout_min", 10))
    if "half_spread_bps" in _tok_cfg and "OBI_HALF_SPREAD_BPS" not in os.environ:
        HALF_SPREAD_BPS = float(_tok_cfg["half_spread_bps"])
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
