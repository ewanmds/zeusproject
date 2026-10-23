"""Hyperliquid BBO / MID Order Book market maker.
BBO quotes at best bid/ask. MID quotes at mid +/- a configurable bps distance
(minimum one tick). Shared post-only execution, inventory and hard-risk limits.
CLI: python -m zeus.bots.order_book (dry-run unless --live is supplied).
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
from typing import Optional, List, Dict, Any, Tuple, NamedTuple
from zeus import PROJECT_ROOT
from zeus.network import StartupNetworkError, post_info, retry_read

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
from zeus.markets import PERP_DEXS, TOKENS, resolve_symbol, token_config

from rich.console import Console
from rich.table import Table
from rich.live import Live
from rich.panel import Panel
from rich import box

# =============================================================================
# CONFIGURATION
# =============================================================================
from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")
AGENT_PRIVATE_KEY = os.getenv("PRIVATE_KEY", "")
ACCOUNT_ADDRESS   = os.getenv("WALLET_ADDRESS", "")
if not AGENT_PRIVATE_KEY or not ACCOUNT_ADDRESS:
    sys.exit("❌ PRIVATE_KEY / WALLET_ADDRESS manquants — mets-les dans le .env")
BASE_URL = constants.MAINNET_API_URL

LEVERAGE  = max(1, min(50, int(float(os.getenv("OBI_LEVERAGE", "20")))))
IS_CROSS  = True

# --- Tokens presets (tick/min viennent du meta live si absent d'ici) ---
DEFAULT_TOKEN = os.getenv("OBI_TOKEN", "SP500")
STRATEGY = "bbo"
MID_OFFSET_BPS = max(0.0, float(os.getenv("OBI_MID_OFFSET_BPS", "1.0")))

# --- Sizing ---
ORDER_NOTIONAL_USD = max(10.5, float(os.getenv("OBI_ORDER_NOTIONAL_USD", "100")))
MAX_INV_ORDERS     = 1  # One opening size; a filled side must unwind before reopening.

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
# --- EARLY WARNING pump/dump: détection AVANT le move ---
# Le détecteur regime() actuel constate un déplacement du mid déjà réalisé
# (≥3 bps en 1s) — trop tard. L'early warning n'utilise QUE des indices
# avancés qui précèdent le prix: z_OFI (queues du book qui s'effondrent),
# z_TFI (flux agresseur qui monte), flux 2s one-sided, OBI brut extrême.
# ≥ EARLY_SCORE_MIN indices alignés = alerte → côté ouvrant menacé retiré
# et, si position contre, sortie bank ALO immédiate — le tout AVANT le tick.
EARLY_Z            = 1.5    # |z_OFI| ou |z_TFI| au-delà = un indice
EARLY_FLOW         = 0.75   # flux agresseur 2s quasi one-sided
EARLY_OBI          = 0.55   # déséquilibre pondéré du book extrême
EARLY_SCORE_MIN    = 2      # nb d'indices alignés pour déclencher
# --- Sorties 100% MAKER: "bank ALO" à la touche ---
# Plus d'IOC taker (0.83 bps) sur les sorties routinières: le bank pose la
# sortie reduce-only EN TÊTE DE QUEUE (long → ask à bb+1 tick; short → bid à
# ba−1 tick), re-peggée à chaque mouvement du BBO. Le taker ne subsiste que
# sur les hard-stops de sécurité (stop adverse, session, feed mort, Ctrl+C).
BANK_ALO_SEC       = 8.0    # durée d'une fenêtre de sortie agressive post-only
# Hard-stop MAKER-FIRST: sur un stop adverse "normal", on tente d'abord de
# sortir en MAKER (post reduce-only à la touche, zéro frais taker, on ne paie
# PAS le spread) pendant HARD_EXIT_MAKER_SEC ; le taker IOC n'intervient qu'en
# DERNIER recours si la position n'est toujours pas fermée après ce délai, OU
# immédiatement si la perte est un vrai runaway (≥ RUNAWAY_MULT × le stop).
# → les closes routiniers ne traversent plus le spread (cf. PnL négatif observé).
HARD_EXIT_MAKER_SEC = float(os.getenv("OBI_HARD_EXIT_MAKER_SEC", "4.0"))
RUNAWAY_STOP_MULT   = 2.0   # perte ≥ N× le stop = runaway → taker immédiat (sécurité)
# --- Queue position model (étape 3 du plan) ---
# La rentabilité d'un ordre passif dépend de sa place dans la file: devant,
# on capture le bruit (fills bénins); au fond, on ne reçoit QUE les sweeps.
QUEUE_MODEL         = os.getenv("OBI_QUEUE_MODEL", "1") == "1"
QUEUE_FRONT_FRAC    = 0.30   # ≤30% de la file initiale devant nous = front → sticky renforcé
QUEUE_DEEP_FRAC     = 0.80   # ≥80% devant nous = fond de file
QUEUE_ADV_ALPHA_BPS = 0.30   # fond de file + alpha contre nous → replace gratuit
QUEUE_NEAR_TICKS    = 2      # "own-next" ne s'applique QU'aux ordres à ≤N ticks
                            # du BBO: un ordre parké loin (half-spread) ne
                            # filera pas, le glisser d'un tick = churn stérile
# --- Sizing par conviction (étape 4 du plan, Kelly-lite) ---
# Taille × [1 .. 2] du côté favorisé par l'alpha, × [1 .. 0.5] du côté
# défavorisé — grosse taille seulement quand le signal paie.
SIZING_CONVICTION   = os.getenv("OBI_SIZING", "1") == "1"
SIZING_MIN_ALPHA_BPS = 0.5   # hystérésis: en-dessous, taille SYMÉTRIQUE (pas de
                             # flip fav/unfav sur un alpha bruité proche de 0)
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
# Demi-spread ADAPTATIF: capture une fraction du spread live du book. Sur un
# token à spread large (SKHX ~1.6 bps) → quote proche du BBO et encaisse la
# large fourchette; sur un token serré (SP500 ~0.13 bps) → composante
# négligeable, on reste au plancher. Se widen tout seul quand le spread
# s'élargit (volatilité) — exactement ce qu'on veut sur un chart qui trend.
SPREAD_CAPTURE_FRAC = float(os.getenv("OBI_SPREAD_CAPTURE_FRAC", "0.5"))

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
# Coût adverse instantané imputé quand la queue au touch est stackée à 100%
# contre le côté qu'on ouvre (déséquilibre = ±1). Prédicteur direct de fill
# toxique (traversant) ⇒ l'EV gate recule/retire ce côté. 0 = désactivé.
TOUCH_ADVERSE_BPS   = float(os.getenv("OBI_TOUCH_ADVERSE_BPS", "0.6"))
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

# --- COUCHE DE CONTEXTE MACRO (indicateurs sur vraies bougies Hyperliquid) ---
# EMA/MACD/RSI/Bollinger/VWAP calculés sur des bougies RÉELLES (candleSnapshot
# REST au démarrage + subscription WS "candle" en live). Signaux LENTS (échelle
# minute) : ils ne remplacent PAS le moteur OFI micro, ils le MODULENT —
#   1) biais de fair dans le sens de la tendance (EMA/MACD) + pull mean-reversion
#      vers le VWAP (borné MACRO_MAX_BPS),
#   2) coût adverse par côté quand on quote CONTRE une tendance confirmée
#      (vendre en rallye = toxique) → l'EV gate recule/retire ce côté,
#   3) widen du spread quand les bandes de Bollinger s'écartent (vol qui monte).
# Tout est plafonné et tunable ; MACRO_ENABLE=0 restaure le comportement actuel.
MACRO_ENABLE        = os.getenv("OBI_MACRO_ENABLE", "1") not in ("0", "false", "False")
MACRO_INTERVAL      = os.getenv("OBI_MACRO_INTERVAL", "1m")
MACRO_BACKFILL_BARS = int(os.getenv("OBI_MACRO_BACKFILL_BARS", "200"))
EMA_FAST            = int(os.getenv("OBI_EMA_FAST", "12"))
EMA_SLOW            = int(os.getenv("OBI_EMA_SLOW", "26"))
MACD_SIGNAL         = int(os.getenv("OBI_MACD_SIGNAL", "9"))
RSI_PERIOD          = int(os.getenv("OBI_RSI_PERIOD", "14"))
BB_PERIOD           = int(os.getenv("OBI_BB_PERIOD", "20"))
BB_K                = float(os.getenv("OBI_BB_K", "2.0"))
VWAP_BARS           = int(os.getenv("OBI_VWAP_BARS", "60"))   # fenêtre glissante 24/7
# Poids de modulation (bps) — conservateurs par défaut, plafonds durs.
MACRO_TREND_W          = float(os.getenv("OBI_MACRO_TREND_W", "0.5"))    # skew fair/tendance
MACRO_REVERT_K         = float(os.getenv("OBI_MACRO_REVERT_K", "0.05"))  # pull vers VWAP
MACRO_REVERT_CAP_BPS   = float(os.getenv("OBI_MACRO_REVERT_CAP_BPS", "0.5"))
MACRO_MAX_BPS          = float(os.getenv("OBI_MACRO_MAX_BPS", "0.6"))    # |skew fair| total
MACRO_TREND_ADVERSE_BPS= float(os.getenv("OBI_MACRO_TREND_ADVERSE_BPS", "0.5"))
MACRO_VOL_K            = float(os.getenv("OBI_MACRO_VOL_K", "0.15"))     # Bollinger→widen
RSI_OVERBOUGHT         = float(os.getenv("OBI_RSI_OB", "70"))
RSI_OVERSOLD           = float(os.getenv("OBI_RSI_OS", "30"))

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


def fast_mm() -> bool:
    """BBO + MID: requote rapide, pas de couches OBI."""
    return STRATEGY in ("bbo", "mid")


def touch_mm() -> bool:
    """BBO only: snap des prix au meilleur bid/ask."""
    return STRATEGY == "bbo"


def round_sz(sz: float) -> float:
    return round(sz, SZ_DECIMALS)


def order_book_prices(bb: float, ba: float, tick: float) -> Tuple[float, float]:
    """Prices for both modes; bps are a half-spread from the current mid."""
    if STRATEGY == "bbo":
        return px_down(bb, tick), px_up(ba, tick)
    if STRATEGY != "mid":
        raise ValueError(f"Unsupported market making mode: {STRATEGY}")
    if not math.isfinite(MID_OFFSET_BPS) or not 0 <= MID_OFFSET_BPS < 10000:
        raise ValueError("MID distance must be finite and between 0 and 10000 bps (exclusive)")
    mid = (bb + ba) / 2.0
    distance = max(tick, mid * MID_OFFSET_BPS / 10000.0)
    bid, ask = px_down(mid - distance, tick), px_up(mid + distance, tick)
    if bid <= 0 or ask <= bid:
        raise ValueError("MID distance produces invalid prices")
    return bid, ask


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


def write_startup_status(symbol, strategy, started, live, phase, error=""):
    """Publish a current-run state before network initialization can fail."""
    path = stats_file_path(symbol)
    data = {"symbol": symbol, "strategy": strategy, "start_time": started,
            "ts": time.time(), "live": live, "running": phase != "failed",
            "phase": phase, "action": "Connexion à Hyperliquid" if phase == "connecting"
            else "Démarrage impossible", "error": error}
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as stream:
        json.dump(data, stream)
    os.replace(temporary, path)


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
    data = post_info(BASE_URL, {"type": "metaAndAssetCtxs", "dex": dex})
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
# MACRO CONTEXT — indicateurs techniques sur VRAIES bougies Hyperliquid
# =============================================================================
# EMA / MACD / RSI / Bollinger / VWAP calculés sur des bougies réelles
# (candleSnapshot REST au démarrage + WS "candle" en live). Couche de CONTEXTE
# lente qui module le maker OFI (voir bloc config MACRO_*). Pas de données
# synthétiques : tout provient de l'API Hyperliquid.

# Normalisation de la force de tendance : séparation EMA (bps) à laquelle
# trend_strength atteint 1.0.
TREND_FULL_BPS   = float(os.getenv("OBI_TREND_FULL_BPS", "15"))
TREND_DEADBAND_BPS = float(os.getenv("OBI_TREND_DEADBAND_BPS", "0.5"))
MACRO_WIDEN_CAP  = float(os.getenv("OBI_MACRO_WIDEN_CAP_BPS", "3.0"))
_INTERVAL_MS = {"1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000,
                "30m": 1_800_000, "1h": 3_600_000, "4h": 14_400_000}


class MacroCtx(NamedTuple):
    """Sortie immuable de MacroContext.context(), prête à consommer."""
    ready: bool
    trend_dir: int            # -1 / 0 / +1  (confluence EMA + MACD hist)
    trend_strength: float     # [0, 1]
    revert_bps: float         # pull mean-reversion vers le VWAP (signé)
    widen_bps: float          # élargissement de spread (Bollinger), ≥ 0
    diag: Dict[str, float]    # valeurs brutes pour le dashboard


MACRO_NEUTRAL = MacroCtx(False, 0, 0.0, 0.0, 0.0, {})


class MacroContext:
    """Buffer OHLCV de bougies RÉELLES + indicateurs techniques standard.

    Thread-safe (lock propre). Alimenté par backfill() (REST) puis on_candle()
    (WS). Les indicateurs sont recalculés depuis le buffer à chaque bougie CLOSE
    (coût trivial, ~1×/minute). context() est un pur read borné."""

    def __init__(self):
        self.lock = threading.Lock()
        maxlen = max(MACRO_BACKFILL_BARS, EMA_SLOW + MACD_SIGNAL + 5,
                     BB_PERIOD + 5, RSI_PERIOD + 5, VWAP_BARS + 5) + 10
        self.bars: deque = deque(maxlen=maxlen)   # bougies CLOSES: (t,o,h,l,c,v)
        self._cur: Optional[list] = None          # bougie en cours [t,o,h,l,c,v]
        self._ctx = MACRO_NEUTRAL

    # ---- ingestion -----------------------------------------------------------
    @staticmethod
    def _parse(c: Dict) -> list:
        return [int(c["t"]), float(c["o"]), float(c["h"]),
                float(c["l"]), float(c["c"]), float(c.get("v", 0.0) or 0.0)]

    def backfill(self, info, name: str, interval: str) -> int:
        """Charge ~MACRO_BACKFILL_BARS dernières bougies via candleSnapshot."""
        step = _INTERVAL_MS.get(interval, 60_000)
        end = int(time.time() * 1000)
        start = end - (MACRO_BACKFILL_BARS + 2) * step
        raw = info.candles_snapshot(name, interval, start, end)
        rows = [self._parse(c) for c in raw] if raw else []
        rows.sort(key=lambda r: r[0])
        with self.lock:
            self.bars.clear()
            # Toutes sauf la dernière = closes ; la dernière = bougie en cours.
            for r in rows[:-1]:
                self.bars.append(r)
            self._cur = rows[-1] if rows else None
            self._recompute_locked()
        return len(rows)

    def on_candle(self, c: Dict) -> None:
        """Callback WS. La bougie en cours est mise à jour jusqu'à sa clôture ;
        quand un nouveau t arrive, la précédente est figée dans le buffer."""
        row = self._parse(c)
        with self.lock:
            if self._cur is None:
                self._cur = row
            elif row[0] > self._cur[0]:
                self.bars.append(self._cur)   # la précédente vient de clôturer
                self._cur = row
                self._recompute_locked()
            else:
                self._cur = row               # même bougie: maj live o/h/l/c/v

    # ---- indicateurs (formules standard) ------------------------------------
    @staticmethod
    def _ema_series(vals: List[float], n: int) -> List[float]:
        """EMA seedée par la SMA des n premières valeurs (convention classique).
        Renvoie la série alignée à partir de l'indice n-1."""
        if len(vals) < n:
            return []
        k = 2.0 / (n + 1.0)
        ema = sum(vals[:n]) / n
        out = [ema]
        for v in vals[n:]:
            ema = v * k + ema * (1.0 - k)
            out.append(ema)
        return out

    @staticmethod
    def _rsi(closes: List[float], n: int) -> float:
        """RSI de Wilder. Nécessite ≥ n+1 closes."""
        if len(closes) < n + 1:
            return 50.0
        gains = losses = 0.0
        for i in range(1, n + 1):
            d = closes[i] - closes[i - 1]
            gains += max(d, 0.0)
            losses += max(-d, 0.0)
        avg_g, avg_l = gains / n, losses / n
        for i in range(n + 1, len(closes)):
            d = closes[i] - closes[i - 1]
            avg_g = (avg_g * (n - 1) + max(d, 0.0)) / n
            avg_l = (avg_l * (n - 1) + max(-d, 0.0)) / n
        if avg_l == 0.0:
            return 100.0 if avg_g > 0.0 else 50.0
        rs = avg_g / avg_l
        return 100.0 - 100.0 / (1.0 + rs)

    def _recompute_locked(self) -> None:
        """Recalcule tous les indicateurs depuis le buffer de closes + la bougie
        en cours. Appelé sous self.lock à chaque clôture / backfill."""
        bars = list(self.bars)
        cur = self._cur
        # Séquence de closes : bougies closes + close live (réactivité).
        closes = [b[4] for b in bars]
        last_px = cur[4] if cur else (closes[-1] if closes else 0.0)
        seq = closes + ([last_px] if cur else [])
        need = max(EMA_SLOW + MACD_SIGNAL, BB_PERIOD, RSI_PERIOD + 1)
        if len(seq) < need or last_px <= 0:
            self._ctx = MACRO_NEUTRAL
            return

        ema_f = self._ema_series(seq, EMA_FAST)
        ema_s = self._ema_series(seq, EMA_SLOW)
        # Aligner les deux séries EMA sur leur queue commune pour le MACD.
        m = min(len(ema_f), len(ema_s))
        macd_series = [ema_f[-m + i] - ema_s[-m + i] for i in range(m)]
        sig_series = self._ema_series(macd_series, MACD_SIGNAL)
        macd = macd_series[-1]
        signal = sig_series[-1] if sig_series else macd
        hist = macd - signal
        ema_fast_v, ema_slow_v = ema_f[-1], ema_s[-1]

        rsi = self._rsi(seq, RSI_PERIOD)

        window = seq[-BB_PERIOD:]
        basis = sum(window) / BB_PERIOD
        var = sum((x - basis) ** 2 for x in window) / BB_PERIOD
        sd = math.sqrt(var)
        upper, lower = basis + BB_K * sd, basis - BB_K * sd
        width_bps = (upper - lower) / basis * 1e4 if basis > 0 else 0.0
        pctb = (last_px - lower) / (upper - lower) if upper > lower else 0.5

        # VWAP glissant sur VWAP_BARS bougies (typical·volume). Fenêtre roulante
        # adaptée au 24/7 (SKHX) ; pour une session bornée (SP500) c'est le VWAP
        # de la dernière heure — proxy de mean-reversion suffisant.
        vbars = bars[-VWAP_BARS:] if len(bars) >= 1 else []
        num = sum(((b[2] + b[3] + b[4]) / 3.0) * b[5] for b in vbars)
        den = sum(b[5] for b in vbars)
        vwap = (num / den) if den > 0 else last_px

        # ---- dérivés bornés (couche de contexte) ----
        # Direction + force = séparation des EMA (robuste sur tendance soutenue,
        # contrairement au MACD hist qui → 0 sur une pente constante). Deadband
        # anti-flicker autour de 0.
        sep_bps = (ema_fast_v - ema_slow_v) / ema_slow_v * 1e4 if ema_slow_v > 0 else 0.0
        trend_dir = 1 if sep_bps > TREND_DEADBAND_BPS else (-1 if sep_bps < -TREND_DEADBAND_BPS else 0)
        strength = max(0.0, min(1.0, abs(sep_bps) / max(TREND_FULL_BPS, 1e-9)))
        # MACD histogram (accélération) = modulateur de CONVICTION : s'il
        # confirme la direction (tendance qui accélère) on garde toute la force ;
        # s'il l'oppose (tendance qui décélère / risque de retournement) on
        # divise la conviction par deux. C'est là que le MACD sert vraiment.
        if trend_dir > 0 and hist < 0:
            strength *= 0.5
        elif trend_dir < 0 and hist > 0:
            strength *= 0.5

        dist_bps = (last_px - vwap) / vwap * 1e4 if vwap > 0 else 0.0
        revert = -MACRO_REVERT_K * dist_bps
        # Renforcer le fade UNIQUEMENT hors tendance (band-walk en tendance =
        # pas de contre-sens) quand RSI extrême et prix hors des bandes.
        if trend_dir == 0:
            if (rsi >= RSI_OVERBOUGHT and pctb > 1.0) or (rsi <= RSI_OVERSOLD and pctb < 0.0):
                revert *= 1.8
        else:
            revert *= 0.3   # en tendance, on n'essaie pas de mean-reverter fort
        revert = max(-MACRO_REVERT_CAP_BPS, min(MACRO_REVERT_CAP_BPS, revert))

        widen = max(0.0, min(MACRO_WIDEN_CAP, MACRO_VOL_K * width_bps))

        self._ctx = MacroCtx(
            ready=True, trend_dir=trend_dir, trend_strength=strength,
            revert_bps=revert, widen_bps=widen,
            diag={"ema_f": ema_fast_v, "ema_s": ema_slow_v, "macd": macd,
                  "macd_sig": signal, "macd_hist": hist, "rsi": rsi,
                  "bb_basis": basis, "bb_up": upper, "bb_lo": lower,
                  "bb_pctb": pctb, "bb_width_bps": width_bps,
                  "vwap": vwap, "vwap_dist_bps": dist_bps, "last": last_px},
        )

    def context(self) -> MacroCtx:
        if not MACRO_ENABLE:
            return MACRO_NEUTRAL
        with self.lock:
            return self._ctx


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
        self.b0_sz = 0.0             # taille au meilleur bid / ask (touch)
        self.a0_sz = 0.0

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
            self.b0_sz, self.a0_sz = b0_sz, a0_sz   # tailles AU touch (toxicité fill)
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
        # Composite OBI+flow+tilt — DIAGNOSTIC ONLY (dashboard). Le quoting
        # utilise l'alpha OFI (alpha_bps/zscore/micro), pas ce composite.
        tilt_c = max(-1.0, min(1.0, self.tilt_ewma / TILT_NORM_BPS))
        return max(-1.0, min(1.0, OBI_WEIGHT * self.obi_ewma
                             + FLOW_WEIGHT * self.flow + TILT_WEIGHT * tilt_c))

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

    def touch_imbalance(self) -> float:
        """Déséquilibre de taille AU touch ∈ [-1,1] : (b0-a0)/(b0+a0).
        >0 = bids dominent (pression acheteuse) ; <0 = asks dominent.
        Prédicteur direct de toxicité de fill : un fill sur notre bid quand
        les asks dominent (<0) = vendeurs qui traversent = toxique."""
        with self.lock:
            tot = self.b0_sz + self.a0_sz
            if tot <= 0.0:
                return 0.0
            return (self.b0_sz - self.a0_sz) / tot

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

    def early_warning(self) -> Tuple[Optional[str], int]:
        """Détection AVANT le move (leading): faisceau d'indices qui précèdent
        le prix — aucun ne dépend d'un déplacement du mid déjà réalisé.

        Indices DUMP: z_OFI très négatif (queues bid qui fondent), z_TFI très
        négatif (vendeurs agressifs en rafale), flux 2s one-sided vendeur,
        OBI pondéré écrasé côté ask. PUMP = miroir. ≥ EARLY_SCORE_MIN indices
        du même côté = alerte (côté, score)."""
        now = time.time()
        with self.lock:
            z1 = self._zscore_locked(now)
            z2 = self._tfi_zscore_locked(now)
            obi = self.obi_ewma
        ff = self.flow_fast(2.0)
        dump_score = int(z1 <= -EARLY_Z) + int(z2 <= -EARLY_Z) \
            + int(ff <= -EARLY_FLOW) + int(obi <= -EARLY_OBI)
        pump_score = int(z1 >= EARLY_Z) + int(z2 >= EARLY_Z) \
            + int(ff >= EARLY_FLOW) + int(obi >= EARLY_OBI)
        if dump_score >= EARLY_SCORE_MIN and dump_score > pump_score:
            return "DUMP", dump_score
        if pump_score >= EARLY_SCORE_MIN and pump_score > dump_score:
            return "PUMP", pump_score
        return None, 0

    def snapshot(self) -> Tuple[float, float, float, float]:
        with self.lock:
            self._refresh_flow()
            return self.obi_ewma, self.flow, self.tilt_ewma, self._combined_locked()


# =============================================================================
# LE BOT
# =============================================================================
class OrderBookBot:
    def __init__(self, live: bool):
        self.live = live
        self.lock = threading.Lock()
        self.wallet = eth_account.Account.from_key(AGENT_PRIVATE_KEY)
        self.dex = SYMBOL.split(":", 1)[0]
        self.fixed_spot_meta = build_fixed_spot_meta()
        self.info = retry_read("initialisation des marchés", lambda: Info(
            BASE_URL, skip_ws=True, spot_meta=self.fixed_spot_meta,
            perp_dexs=PERP_DEXS, timeout=10))
        self.exchange = retry_read("initialisation du client", lambda: Exchange(
            self.wallet, base_url=BASE_URL, account_address=ACCOUNT_ADDRESS,
            spot_meta=self.fixed_spot_meta, perp_dexs=PERP_DEXS, timeout=10))
        self.signal = SignalEngine()
        self.macro = MacroContext()      # indicateurs sur vraies bougies HL
        self._macro = MACRO_NEUTRAL      # contexte caché 1×/cycle

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
        self.last_orderupdate_ts = time.time()  # dernier event orderUpdates reçu
        self.pause_until = 0.0
        self.hourly_pause_until = 0.0
        self.force_quote = False      # fill/régime → bypass du coalesce
        self.last_quote_bb = 0.0      # BBO au moment du dernier quote (urgence)
        self.last_quote_ba = 0.0
        # Après un retrait d'urgence ou un filtre de risque, attendre avant de
        # rouvrir le même côté. Les sorties reduce-only ne sont pas retenues.
        self.side_rearm_until: Dict[str, float] = {"B": 0.0, "A": 0.0}
        self.side_ready_since: Dict[str, float] = {"B": 0.0, "A": 0.0}

        # Cache des signaux calculés UNE fois par cycle (_refresh_signals):
        # regime() avance sa machine à états UNIQUEMENT là — tous les autres
        # lecteurs (compute_intent, risk_checks, dashboard, scratch WS) lisent
        # ce cache pour éviter l'incohérence intra-cycle + le recalcul redondant.
        self._reg_now: Tuple[Optional[str], int] = (None, 0)
        self._early_now: Tuple[Optional[str], int] = (None, 0)
        self._sig_alpha = 0.0
        self._sig_z = 0.0
        self._sig_vol = 0.0
        self._sig_touch = 0.0
        self._qf_b: Optional[float] = None
        self._qf_a: Optional[float] = None

        # PnL positif: take-profit actif + protection de session (sans arrêt)
        self.bank_until = 0.0         # fenêtre de sortie bank ALO (maker, à la touche)
        self._hard_exit_at = 0.0      # échéance d'escalade taker si maker n'a pas fermé
        self._hard_exit_reason = ""
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
        self.order_placements = 0
        self.order_modifications = 0
        self.order_cancellations = 0
        self.order_requests = 0
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
        # Load metadata before starting a WS thread, so a failed read cannot
        # leave orphan websocket threads behind during a retry.
        from hyperliquid.websocket_manager import WebsocketManager
        self.ws_info = retry_read("initialisation du flux", lambda: Info(
            BASE_URL, skip_ws=True, spot_meta=self.fixed_spot_meta,
            perp_dexs=PERP_DEXS, timeout=10))
        self.ws_info.ws_manager = WebsocketManager(BASE_URL)
        self.ws_info.ws_manager.start()
        self.ws_info.subscribe({"type": "l2Book", "coin": SYMBOL}, self._on_l2)
        self.ws_info.subscribe({"type": "trades", "coin": SYMBOL}, self._on_trades)
        self.ws_info.subscribe({"type": "userFills", "user": ACCOUNT_ADDRESS}, self._on_user_fills)
        self.ws_info.subscribe({"type": "orderUpdates", "user": ACCOUNT_ADDRESS}, self._on_order_updates)
        if MACRO_ENABLE:
            self.ws_info.subscribe(
                {"type": "candle", "coin": SYMBOL, "interval": MACRO_INTERVAL},
                self._on_candle)

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
            # A5: mettre à jour le signal AVANT de marquer le feed "vivant".
            # Si on_l2 lève, last_tick_ts/ws_ready ne mentent pas au deadman.
            self.signal.on_l2(levels[0], levels[1])
            with self.lock:
                self.best_bid, self.best_ask = bb, ba
                self.mid = (bb + ba) / 2.0
                self.last_tick_ts = time.time()
                self.ws_ready = True
            self.wake.set()
        except Exception:
            pass

    def _on_candle(self, data):
        """WS candle: {"channel":"candle","data":{t,T,s,i,o,h,l,c,v,n}}.
        Alimente la couche de contexte macro (vraies bougies Hyperliquid)."""
        try:
            c = data.get("data") if isinstance(data, dict) else None
            if c and "c" in c:
                self.macro.on_candle(c)
        except Exception as exc:
            self.last_error = f"candle: {str(exc)[:60]}"

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
        if fast_mm() or not self.live or now - self._emerg_ts[side] < EMERG_COOLDOWN_SEC:
            return
        with self.lock:
            # A4: pull tout ordre du côté menacé qui n'est PAS une sortie RO
            # confirmée (inclut ro=None, appris via WS) — sinon un ordre ouvrant
            # découvert par le flux WS ne serait jamais retiré en urgence.
            oids = [o for o, d in self.orders.items()
                    if d["side"] == side and d.get("ro") is not True]
        if not oids:
            return
        self._emerg_ts[side] = now
        self.cancel_oids(oids)
        with self.lock:
            self.last_intent = None
            self.last_action = f"⚡ sweep ${burst_usd:,.0f} → pull {side}"
            self.force_quote = True   # A3: sous lock (sinon perdu vs read-clear)
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
                    # A6: try/except PAR fill — un fill corrompu ne fait plus
                    # sauter le BATCH (sinon perte silencieuse d'un fill →
                    # position locale qui dérive de la vérité jusqu'au resync).
                    try:
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
                        # Markout différé + détection de sweep (≥2 fills même
                        # côté dans la fenêtre = flux informé, on retire ce côté).
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
                        # Flip éclair: reposter la sortie immédiatement.
                        self.force_quote = True
                    except Exception as exc:
                        self.last_error = f"fill parse: {str(exc)[:80]}"
            self.wake.set()
            # SCRATCH TOXIQUE (hors self.lock): si on vient de se faire
            # remplir en OUVERTURE alors que l'alpha est franchement contre
            # la position, sortir en IOC tout de suite — 0.83 bps de taker
            # valent mieux que les 3-10 bps du move qui suit un fill informé.
            self._maybe_toxic_scratch()
        except Exception:
            pass

    def _maybe_toxic_scratch(self):
        """Fill ouvrant reçu avec l'alpha franchement contre → sortie BANK ALO
        immédiate (maker, tête de queue) au lieu de l'ancien IOC taker. Le
        flip éclair + le re-peg d'urgence font le reste; si la perte creuse
        malgré tout, le hard-stop vol-scalé (taker, assumé) tranche."""
        now = time.time()
        if fast_mm() or not self.live or now - self._scratch_ts < SCRATCH_COOLDOWN_SEC:
            return
        with self.lock:
            pos = self.position
            bb, ba = self.best_bid, self.best_ask
        if abs(pos) < MIN_SIZE / 2 or bb <= 0 or ba <= bb:
            return
        # z/z2 sont des lectures PURES (pas d'effet de bord) → fraîches OK sur
        # un fill; reg vient du CACHE (ne PAS ré-avancer la machine d'état
        # depuis le thread WS — c'est le bug A1).
        z = self.signal.zscore()
        z2 = self.signal.tfi_zscore()
        with self.lock:
            reg = self._reg_now[0]
        toxic_long = pos > 0 and (min(z, z2) <= -TOXIC_SCRATCH_Z or reg == "DUMP")
        toxic_short = pos < 0 and (max(z, z2) >= TOXIC_SCRATCH_Z or reg == "PUMP")
        if not (toxic_long or toxic_short):
            return
        self._scratch_ts = now
        with self.lock:   # A3: bank_until + force_quote sous lock (thread WS)
            self.bank_until = max(self.bank_until, now + BANK_ALO_SEC)
            self.last_action = f"⚡ bank toxique ALO (z {z:+.1f}/{z2:+.1f} {reg or ''})"
            # Pause du côté qui rouvrirait dans le sens toxique.
            self.side_pause_until["B" if toxic_long else "A"] = now + BURST_PAUSE_SEC
            self.force_quote = True
        self.wake.set()

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
        if not self.live:
            return True
        if not oids:
            return True
        try:
            with self.api_lock:
                with self.lock:
                    self.order_cancellations += len(oids)
                    self.order_requests += 1
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
        if not self.live:
            return
        if not reqs:
            return
        pending = reqs
        for retry in range(POST_ONLY_MAX_RETRIES + 1):
            # Clamp de dernière milliseconde au BBO le plus frais (jamais croiser).
            with self.lock:
                bb, ba = self.best_bid, self.best_ask
            tick = dyn_tick(self.mid)
            if bb <= 0 or ba <= bb:
                return
            try:
                for r in pending:
                    r["limit_px"] = self._execution_price(
                        r["is_buy"], r["reduce_only"], bb, ba, tick)
            except ValueError as exc:
                self.last_error = str(exc)
                return
            try:
                with self.api_lock:
                    with self.lock:
                        # A fill can arrive after compute_intent(). Do not add
                        # exposure from a stale opening request in that case.
                        if abs(self.position) >= MIN_SIZE / 2:
                            pending = [r for r in pending if r["reduce_only"]]
                        if not pending:
                            self.force_quote = True
                            return
                        self.order_placements += len(pending)
                        self.order_requests += 1
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
                        if not fast_mm() and retry < POST_ONLY_MAX_RETRIES and self._is_post_only_error(err):
                            # A7: RECULER d'1 tick avant de retenter — resoumettre
                            # le même prix croisant se ferait rejeter en boucle.
                            rt = dyn_tick(self.mid if self.mid > 0 else req["limit_px"])
                            if req["is_buy"]:
                                req["limit_px"] = px_down(req["limit_px"] - rt, rt)
                            else:
                                req["limit_px"] = px_up(req["limit_px"] + rt, rt)
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
        if not self.live:
            return True
        if not pairs:
            return True
        with self.lock:
            bb, ba = self.best_bid, self.best_ask
        tick = dyn_tick(self.mid)
        modifies = []
        if bb <= 0 or ba <= bb:
            return False
        try:
            for _, r in pairs:
                r["limit_px"] = self._execution_price(
                    r["is_buy"], r["reduce_only"], bb, ba, tick)
        except ValueError as exc:
            self.last_error = str(exc)
            return False
        for oid, r in pairs:
            modifies.append({"oid": oid, "order": {
                "coin": SYMBOL, "is_buy": r["is_buy"], "sz": r["sz"],
                "limit_px": r["limit_px"],
                "order_type": {"limit": {"tif": "Alo"}},
                "reduce_only": r["reduce_only"], "cloid": None,
            }})
        try:
            with self.api_lock:
                with self.lock:
                    if abs(self.position) >= MIN_SIZE / 2 and any(not r["reduce_only"] for _, r in pairs):
                        self.force_quote = True
                        return False
                    self.order_modifications += len(pairs)
                    self.order_requests += 1
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
    def _execution_price(self, is_buy, reduce_only, bb, ba, tick):
        """Urgent MID exits join touch, without moving an inside quote away."""
        bid_px, ask_px = order_book_prices(bb, ba, tick)
        with self.lock:
            urgent = time.time() < self.bank_until or getattr(self, "_hard_exit_at", 0.0) > 0
        if STRATEGY == "mid" and reduce_only and urgent:
            bid_px = max(bid_px, px_down(bb, tick))
            ask_px = min(ask_px, px_up(ba, tick))
        return bid_px if is_buy else ask_px

    def _fast_quote_gates(self):
        """Book + inventory/risk gates shared by BBO and MID (no OBI filters)."""
        now = time.time()
        with self.lock:
            bb, ba = self.best_bid, self.best_ask
            pos = self.position
            risk_pause = now < self.pause_until
            exits_only = now < self.hourly_pause_until or now < self.bank_until
        if not all(math.isfinite(v) for v in (bb, ba, pos)) or bb <= 0 or ba <= bb:
            return None, "no book"
        mid = (bb + ba) / 2.0
        tick = dyn_tick(mid)
        if (ba - bb) / tick > MAX_SPREAD_TICKS:
            return None, "spread too wide"
        self.session_mode = session_state_utc(SESSION_WINDOWS_UTC, now, OPEN_BLACKOUT_MIN)
        exits_only = exits_only or risk_pause or self.session_mode == "blackout"
        size = round_sz(max(MIN_SIZE, ORDER_NOTIONAL_USD * self.size_mult / mid))
        if size * mid < 10.0:
            size = round_sz(math.ceil((10.5 / mid) / MIN_SIZE) * MIN_SIZE)
        cap = MAX_INV_ORDERS * size
        eps = MIN_SIZE / 2.0
        bid_ro, ask_ro = pos <= -eps, pos >= eps
        bid_size = round_sz(min(size, abs(pos))) if bid_ro else size
        ask_size = round_sz(min(size, abs(pos))) if ask_ro else size
        flat = abs(pos) < eps
        bid_ok = bid_ro or (flat and not exits_only and pos + bid_size <= cap + eps)
        ask_ok = ask_ro or (flat and not exits_only and -pos + ask_size <= cap + eps)
        return {
            "bb": bb, "ba": ba, "mid": mid, "tick": tick, "pos": pos,
            "bid_ro": bid_ro, "ask_ro": ask_ro,
            "bid_size": bid_size, "ask_size": ask_size,
            "bid_ok": bid_ok, "ask_ok": ask_ok,
        }, None

    def compute_bbo_intent(self):
        """Touch quotes with inventory and hard risk limits, without OBI filters."""
        st, err = self._fast_quote_gates()
        if st is None:
            return None, None, f"BBO | {err}"
        tick, bb, ba = st["tick"], st["bb"], st["ba"]
        bid = (px_down(bb, tick), st["bid_size"], st["bid_ro"]) if st["bid_ok"] and st["bid_size"] >= MIN_SIZE else None
        ask = (px_up(ba, tick), st["ask_size"], st["ask_ro"]) if st["ask_ok"] and st["ask_size"] >= MIN_SIZE else None
        tag = "BBO | bid/ask" if bid and ask else "BBO | inventory/risk limit"
        return bid, ask, tag

    def compute_mid_intent(self):
        """Classic symmetric market making at a fixed distance from mid."""
        st, err = self._fast_quote_gates()
        if st is None:
            return None, None, f"MID | {err}"
        try:
            bid_px = self._execution_price(True, st["bid_ro"], st["bb"], st["ba"], st["tick"])
            ask_px = self._execution_price(False, st["ask_ro"], st["bb"], st["ba"], st["tick"])
        except ValueError as exc:
            return None, None, f"MID | {exc}"
        bid = (bid_px, st["bid_size"], st["bid_ro"]) if st["bid_ok"] and st["bid_size"] >= MIN_SIZE else None
        ask = (ask_px, st["ask_size"], st["ask_ro"]) if st["ask_ok"] and st["ask_size"] >= MIN_SIZE else None
        return bid, ask, f"MID | mid +/- {MID_OFFSET_BPS:g}bps"

    def compute_intent(self):
        if STRATEGY == "mid":
            return self.compute_mid_intent()
        if STRATEGY == "bbo":
            return self.compute_bbo_intent()
        raise ValueError(f"Unsupported market making mode: {STRATEGY}")

    def apply_intent(self):
        """Shared BBO/MID execution: change only a different price/size/flag."""
        now = time.time()
        if now - self.last_action_ts < ACTION_COALESCE_SEC:
            return
        bid, ask, tag = self.compute_intent()
        intent = (bid, ask)
        tick = dyn_tick(self.mid if self.mid > 0 else 1.0)
        with self.lock:
            resting_by_side = {
                side: {oid: dict(d) for oid, d in self.orders.items() if d["side"] == side}
                for side in ("B", "A")
            }
            self.force_quote = False
        to_cancel, place_reqs, modify_pairs = [], [], []
        allow_modify = now >= getattr(self, "_modify_block_until", 0.0)
        for side, want in zip(("B", "A"), intent):
            resting = resting_by_side[side]
            if want is None:
                to_cancel.extend(resting)
                continue
            if len(resting) == 1:
                oid, old = next(iter(resting.items()))
                if (abs(old["px"] - want[0]) < tick / 2.0
                        and abs(old["sz"] - want[1]) < MIN_SIZE / 2.0
                        and old.get("ro") == want[2]):
                    continue
            req = {"coin": SYMBOL, "is_buy": side == "B", "sz": want[1],
                   "limit_px": want[0], "order_type": {"limit": {"tif": "Alo"}},
                   "reduce_only": want[2]}
            if (len(resting) == 1 and allow_modify and not want[2]
                    and next(iter(resting.values())).get("ro") is False):
                modify_pairs.append((next(iter(resting)), req))
            else:
                to_cancel.extend(resting)
                place_reqs.append(req)
        self.last_action = tag
        if not (to_cancel or place_reqs or modify_pairs):
            return
        self.last_action_ts = now
        if not self.live:
            with self.lock:
                self.last_intent = intent
            return
        if modify_pairs and not self.modify_quotes(modify_pairs):
            self._modify_block_until = now + 1.5
            with self.lock:
                self.last_intent = None
            return
        if to_cancel and not self.cancel_oids(to_cancel):
            return
        if place_reqs:
            with self.lock:
                occupied = {d["side"] for d in self.orders.values()}
            place_reqs = [r for r in place_reqs
                          if ("B" if r["is_buy"] else "A") not in occupied]
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
            vol = self._sig_vol       # signaux cachés (1×/cycle), cohérents
            z = self._sig_z           # avec compute_intent du même cycle
            reg, reg_i = self._reg_now
            early_side_c, early_score_c = self._early_now
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

        # --- C. uPnL peak + take-profit actif — 100% MAKER: le bank ouvre une
        # fenêtre de sortie ALO en tête de queue (bank-alo dans compute_intent)
        # au lieu de payer le taker.
        if notional <= 0:
            self.upnl_peak_bps = 0.0
            self.bank_until = 0.0   # plat: fenêtre bank soldée
        else:
            self.upnl_peak_bps = max(self.upnl_peak_bps, upnl_bps)
            z_opposed = (pos > 0 and z <= -1.0) or (pos < 0 and z >= 1.0)
            if self.upnl_peak_bps >= TP_MIN_BPS and (
                    upnl_bps <= self.upnl_peak_bps * (1.0 - TP_RETRACE) or z_opposed):
                if now >= self.bank_until:
                    self.bank_until = now + BANK_ALO_SEC
                    self.last_action = f"bank-alo tp {upnl_bps:+.1f}/{self.upnl_peak_bps:.1f}bps"
                    self.force_quote = True

        # --- C. Pump/dump CONTRE la position: sortie bank ALO immédiate
        # (le hard-stop vol-scalé ci-dessous reste le filet si ça creuse).
        if reg and notional > 0 and reg_i >= 2:
            against = (pos > 0 and reg == "DUMP") or (pos < 0 and reg == "PUMP")
            if against and now >= self.bank_until:
                self.bank_until = now + BANK_ALO_SEC
                self.last_action = f"bank-alo cut {reg}{reg_i}"
                self.force_quote = True
        # EARLY WARNING contre la position: sortir AVANT le move, en maker.
        if not reg and notional > 0:
            early_side, early_score = early_side_c, early_score_c
            if early_side:
                against_early = (pos > 0 and early_side == "DUMP") or \
                                (pos < 0 and early_side == "PUMP")
                if against_early and now >= self.bank_until:
                    self.bank_until = now + BANK_ALO_SEC
                    self.last_action = f"bank-alo early {early_side}{early_score}"
                    self.force_quote = True
        # Fin de régime aligné: le move s'essouffle → banker le ride (maker).
        if self._last_regime and not reg and notional > 0:
            aligned = (pos > 0 and self._last_regime == "PUMP") or \
                      (pos < 0 and self._last_regime == "DUMP")
            if aligned and upnl_bps >= 1.0 and now >= self.bank_until:
                self.bank_until = now + BANK_ALO_SEC
                self.last_action = f"bank-alo {self._last_regime}-end {upnl_bps:+.1f}bps"
                self.force_quote = True
        self._last_regime = reg

        # --- Escalade TAKER en dernier recours: un hard-stop maker-first qui
        # n'a pas fermé la position après HARD_EXIT_MAKER_SEC → flatten IOC
        # (garantit la sortie sur un vrai runaway). Sinon on reste en maker.
        if abs(pos) < MIN_SIZE:
            self._hard_exit_at = 0.0
        elif self._hard_exit_at and now >= self._hard_exit_at:
            self.flatten(self._hard_exit_reason or "hard-exit")   # IOC dernier recours
            self._hard_exit_at = 0.0
            self.pause_until = now + STOP_PAUSE_SEC

        # --- Stop adverse dynamique (vol-scalé, protect-aware) — MAKER-FIRST.
        stop_bps = self._eff_stop_bps(vol)
        if notional > 0 and upnl < 0 and abs(upnl_bps) >= stop_bps and not self._hard_exit_at:
            if abs(upnl_bps) >= RUNAWAY_STOP_MULT * stop_bps:
                # Runaway: taker immédiat (la perte creuse trop vite pour maker).
                self.flatten(f"stop-runaway {abs(upnl_bps):.1f}bps")
                self.pause_until = now + STOP_PAUSE_SEC
            else:
                # Sortie MAKER d'abord (pas de spread payé); taker seulement si
                # pas fermé après HARD_EXIT_MAKER_SEC.
                self.bank_until = now + HARD_EXIT_MAKER_SEC
                self._hard_exit_at = now + HARD_EXIT_MAKER_SEC
                self._hard_exit_reason = f"stop {abs(upnl_bps):.1f}bps"
                self.pause_until = now + STOP_PAUSE_SEC
                self.last_action = f"bank-stop {abs(upnl_bps):.1f}bps (maker→taker {HARD_EXIT_MAKER_SEC:.0f}s)"
                self.force_quote = True
        # Position trop vieille: débouclage MAKER-FIRST (pas forcément en perte).
        elif (pos_since and now - pos_since > self._eff_max_hold() + FORCE_FLAT_EXTRA
              and abs(pos) >= MIN_SIZE and not self._hard_exit_at):
            self.bank_until = now + HARD_EXIT_MAKER_SEC
            self._hard_exit_at = now + HARD_EXIT_MAKER_SEC
            self._hard_exit_reason = "max hold"
            self.pause_until = now + STOP_PAUSE_SEC / 2
            self.last_action = f"bank-maxhold (maker→taker {HARD_EXIT_MAKER_SEC:.0f}s)"
            self.force_quote = True

        if net <= -MAX_SESSION_LOSS_USD:
            self.last_action = f"SESSION LOSS LIMIT ${net:+.2f} — arrêt"
            self.running = False

        # Deadman feed (tourne TOUJOURS — plus de return prématuré avant, A8)
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

    def write_stats(self, running: bool = True, phase: Optional[str] = None):
        """Snapshot JSON atomique pour le dashboard (multi-process)."""
        try:
            with self.lock:
                mid = self.mid
                spread_bps = ((self.best_ask - self.best_bid) / mid * 10000.0
                              if mid > 0 and self.best_ask > self.best_bid > 0 else 0.0)
                data = {
                    "symbol": SYMBOL,
                    "running": running,
                    "phase": phase or ("running" if running and self.ws_ready
                                       else "connecting" if running else "stopped"),
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
                    "cost_per_1m": ((self.total_fees - self.session_pnl) * 1_000_000 / self.total_volume
                                    if self.total_volume > 0 else None),
                    "fills": self.fills_count,
                    "maker_fills": self.maker_fills,
                    "order_placements": self.order_placements,
                    "order_modifications": self.order_modifications,
                    "order_cancellations": self.order_cancellations,
                    "order_requests": self.order_requests,
                    "strategy": STRATEGY,
                    "notional": ORDER_NOTIONAL_USD,
                    "action": self.last_action,
                    "error": self.last_error,
                    "protect": self.protect,
                    "hourly_net": self.hourly_net_pnl,
                    "hourly_pause_until": self.hourly_pause_until,
                    "session_mode": self.session_mode,
                    "markout_bid_bps": self.markout_bps["B"],
                    "markout_ask_bps": self.markout_bps["A"],
                    "mid_offset_bps": MID_OFFSET_BPS if STRATEGY == "mid" else 0.0,
                }
            with self.lock:
                reg = self._reg_now[0]
                macro = self._macro
            data["regime"] = reg or ""
            if macro.ready:
                d = macro.diag
                data["macro"] = {
                    "trend_dir": macro.trend_dir,
                    "trend_strength": round(macro.trend_strength, 3),
                    "ema_f": d.get("ema_f"), "ema_s": d.get("ema_s"),
                    "macd_hist": d.get("macd_hist"), "rsi": round(d.get("rsi", 0.0), 1),
                    "bb_pctb": round(d.get("bb_pctb", 0.0), 3),
                    "bb_width_bps": round(d.get("bb_width_bps", 0.0), 2),
                    "vwap": d.get("vwap"), "vwap_dist_bps": round(d.get("vwap_dist_bps", 0.0), 2),
                }
            tmp = self._stats_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp, self._stats_file)
        except Exception:
            pass

    # -------------------------------------------------------------- dashboard
    def dashboard(self) -> Panel:
        obi, flow, tilt, _ = self.signal.snapshot()   # composite = diagnostic only
        mom = self.signal.momentum_bps(MOM_FAST_SEC)
        with self.lock:
            volat = self._sig_vol         # signaux cachés (affichage cohérent)
            z = self._sig_z
            reg, reg_i = self._reg_now
            early_s, early_n = self._early_now
            alpha_disp = self._sig_alpha
            bb, ba, mid = self.best_bid, self.best_ask, self.mid
            pos, entry, upnl = self.position, self.entry_price, self.unrealized_pnl
            vol, fees, rpnl = self.total_volume, self.total_fees, self.session_pnl
            fills, makers = self.fills_count, self.maker_fills
            n_orders = len(self.orders)
            action, err = self.last_action, self.last_error
            eq = self.equity
            mo_b, mo_a = self.markout_bps["B"], self.markout_bps["A"]
            macro = self._macro
        net = rpnl + upnl - fees
        # Même métrique que le market maker: (fees − PnL réalisé) / M de volume,
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
        if reg:
            reg_txt = f"[bold red]{reg}{reg_i}[/]"
        elif early_s:
            reg_txt = f"[yellow]early {early_s}{early_n}[/]"
        else:
            reg_txt = "—"
        if time.time() < self.bank_until:
            reg_txt += " [cyan]bank[/]"
        protect_txt = f" | protect{self.protect}" if self.protect else ""
        # Ligne indicateurs macro (vraies bougies) — visible dans le dashboard.
        if macro.ready:
            d = macro.diag
            td = macro.trend_dir
            tc = "green" if td > 0 else "red" if td < 0 else "white"
            arrow = "↑" if td > 0 else "↓" if td < 0 else "→"
            rsi_v = d.get("rsi", 50.0)
            rsi_c = "red" if rsi_v >= RSI_OVERBOUGHT else "green" if rsi_v <= RSI_OVERSOLD else "white"
            macro_txt = (f"[{tc}]trend {arrow}{macro.trend_strength:.2f}[/] "
                         f"MACD {d.get('macd_hist', 0.0):+.4f}\n"
                         f"[{rsi_c}]RSI {rsi_v:.0f}[/] BB%b {d.get('bb_pctb', 0.0):+.2f} "
                         f"w {d.get('bb_width_bps', 0.0):.0f}bps\n"
                         f"VWAPΔ {d.get('vwap_dist_bps', 0.0):+.1f}bps")
        else:
            macro_txt = "[dim]macro: warmup…[/]"
        t.add_row(
            f"{SYMBOL}\nbid {bb:.{PRICE_DECIMALS}f}\nask {ba:.{PRICE_DECIMALS}f}\nmid {mid:.{PRICE_DECIMALS}f}\nvol {volat:.2f}bps",
            f"OBI {obi:+.2f} flow {flow:+.2f}\ntilt {tilt:+.2f}bps\n[{sig_color}]z {z:+.2f}[/] mom {mom:+.1f}\n"
            f"β {self.signal.beta_bps():.2f} α {alpha_disp:+.2f}bps\n"
            f"session {self.session_mode}\n"
            f"mo [{mo_b_c}]B {mo_b:+.2f}[/]/[{mo_a_c}]A {mo_a:+.2f}[/]\nordres {n_orders} | {reg_txt}\n{macro_txt}",
            f"pos {pos:+.{SZ_DECIMALS}f}\nentry {entry:.{PRICE_DECIMALS}f}\nuPnL ${upnl:+.2f} (pk {self.upnl_peak_bps:.1f})\nequity ${eq:.2f}",
            f"vol ${vol:,.0f}\nfills {fills} ({maker_pct:.0f}% maker)\n"
            f"fees ${fees:.2f} | net ${net:+.2f} | h1 ${self.hourly_net_pnl:+.2f}\n"
            f"cost/M ${cost_m:+.0f}{protect_txt}",
        )
        mode = "[green]LIVE[/]" if self.live else "[yellow]DRY-RUN[/]"
        sub = f"{mode} | {action}"
        if err:
            sub += f" | [red]{err}[/]"
        title = (
            "BBO ORDER BOOK" if STRATEGY == "bbo"
            else "MID ORDER BOOK" if STRATEGY == "mid"
            else "BBO ORDER BOOK"
        )
        return Panel(t, title=f"{title} — {SYMBOL}", subtitle=sub, box=box.ROUNDED)

    # -------------------------------------------------------------------- run
    def _refresh_signals(self):
        """Calcule TOUS les signaux à effet de bord / coûteux UNE fois par
        cycle et les cache sous lock. C'est le SEUL endroit où regime() avance
        sa machine à états (sinon 4-5 avancées/cycle depuis 2 threads = état
        instable) et où _queue_fraction() décrémente queue_ahead. Réactivité:
        supprime ~15-20 acquisitions de lock redondantes par cycle."""
        reg = (None, 0) if fast_mm() else self.signal.regime()          # unique point d'avancement machine
        early = (None, 0) if fast_mm() else self.signal.early_warning()
        alpha = 0.0 if fast_mm() else self.signal.alpha_bps()
        z = 0.0 if fast_mm() else self.signal.zscore()
        vol = self.signal.vol_bps()
        touch = self.signal.touch_imbalance()   # déséquilibre au touch (toxicité)
        macro = self.macro.context()            # indicateurs bougies (contexte)
        qf_b = self._queue_fraction("B")
        qf_a = self._queue_fraction("A")
        with self.lock:
            self._reg_now = reg
            self._early_now = early
            self._sig_alpha = alpha
            self._sig_z = z
            self._sig_vol = vol
            self._sig_touch = touch
            self._macro = macro
            self._qf_b, self._qf_a = qf_b, qf_a

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
            self._refresh_signals()   # cache signaux 1×/cycle AVANT tout usage
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
        if MACRO_ENABLE:
            try:
                n = self.macro.backfill(self.info, SYMBOL, MACRO_INTERVAL)
                if not headless:
                    print(f"📊 Macro: {n} bougies {MACRO_INTERVAL} chargées (EMA/MACD/RSI/BB/VWAP)")
            except Exception as exc:
                self.last_error = f"backfill macro: {str(exc)[:60]}"
        try:
            self.start_ws()
        except StartupNetworkError as exc:
            self.last_error = str(exc)
            self.last_action = "Connexion Hyperliquid impossible"
            self.running = False
            print(str(exc), flush=True)
            self.write_stats(running=False, phase="failed")
            return
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
    """Resolve a preset, a bare ticker or a dex-qualified HIP-3 market."""
    try:
        return resolve_symbol(name)
    except ValueError as exc:
        raise SystemExit(f"❌ Token inconnu: {name} ({exc})") from exc


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="BBO / MID Order Book market maker (HIP-3)")
    parser.add_argument("--token", default=None, help="ex: SP500, XYZ100, SPCX")
    parser.add_argument("--live", action="store_true", help="ordres réels (défaut: dry-run)")
    parser.add_argument("--notional", type=float, default=None, help="USD par ordre")
    parser.add_argument("--strategy", choices=("bbo", "mid"), default="bbo",
                        help="BBO Order Book ou MID Order Book")
    parser.add_argument("--offset-bps", type=float, default=None,
                        help="distance symétrique au mid pour strategy=mid")
    args = parser.parse_args()
    STRATEGY = args.strategy
    if args.offset_bps is not None:
        if not math.isfinite(args.offset_bps) or not 0 <= args.offset_bps < 10000:
            parser.error("--offset-bps doit être fini, entre 0 inclus et 10000 bps exclus")
        MID_OFFSET_BPS = args.offset_bps

    if args.notional:
        ORDER_NOTIONAL_USD = max(10.5, args.notional)

    token = args.token
    if not token:
        print("Tokens: " + ", ".join(TOKENS))
        token = input(f"Token ? [{DEFAULT_TOKEN}] : ").strip() or DEFAULT_TOKEN

    SYMBOL = resolve_token(token)
    # Demi-spread par token (préset), sauf si forcé par variable d'env.
    # token_config accepte les clés historiques (SP500) et les marchés
    # qualifiés (XYZ:COPPER, PARA:10Y) résolus dans ce process enfant.
    _tok_cfg = token_config(token)
    SESSION_WINDOWS_UTC = tuple(
        (int(start), int(end)) for start, end in _tok_cfg.get("session_utc", ())
    )
    OPEN_BLACKOUT_MIN = int(_tok_cfg.get("open_blackout_min", 10))
    if "half_spread_bps" in _tok_cfg and "OBI_HALF_SPREAD_BPS" not in os.environ:
        HALF_SPREAD_BPS = float(_tok_cfg["half_spread_bps"])
    if not acquire_instance_lock(SYMBOL):
        sys.exit(f"❌ Un bot tourne déjà sur {SYMBOL} (verrou d'instance)")

    startup_started = time.time()
    write_startup_status(SYMBOL, STRATEGY, startup_started, args.live, "connecting")
    try:
        meta = fetch_token_meta(SYMBOL)
        SZ_DECIMALS = meta["szDecimals"]
        MIN_SIZE = 10.0 ** (-SZ_DECIMALS) if SZ_DECIMALS > 0 else 1.0
        PRICE_DECIMALS = max(0, 6 - SZ_DECIMALS)
        LEVERAGE = min(LEVERAGE, meta.get("maxLeverage", LEVERAGE))
        bot = OrderBookBot(live=args.live)
    except Exception as exc:
        message = str(exc) if isinstance(exc, StartupNetworkError) else f"Démarrage impossible : {type(exc).__name__}: {exc}"
        write_startup_status(SYMBOL, STRATEGY, startup_started, args.live, "failed", message)
        print(message, flush=True)
        sys.exit(1)
    mode = "LIVE" if args.live else "DRY-RUN (aucun ordre envoyé — ajoute --live)"
    print(f"🚀 {STRATEGY.upper()} BOT | {SYMBOL} | {LEVERAGE}x | ~${ORDER_NOTIONAL_USD:.0f}/ordre | {mode}")
    bot.run()
