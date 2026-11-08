"""Hyperliquid BBO / MID Order Book market maker.
BBO quotes at best bid/ask. MID quotes at mid +/- a configurable bps distance,
rounded outward to valid ticks. Shared post-only execution and hard-risk limits.
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
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple, NamedTuple
from zeus import PROJECT_ROOT
from zeus.safety import SafetyLatch, account_snapshot, supervisor_alive, finite, post_only_rejection, missing_modify_rejection, atomic_json
from zeus.safety import SubmissionSkipped, safe_error
from zeus.safety import invalid_modify_rejection
from zeus.latency import LatencyMetrics
from zeus.mid_maintenance import MidMaintenance, MaintenanceRequest
from zeus.inventory import apply_fill, mark_inventory, InventoryDesyncError
from zeus.ws_execution import WebSocketExecution
from zeus.action_worker import LatestActionWorker
from contextlib import contextmanager, nullcontext
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
    sys.exit("❌ PRIVATE_KEY / WALLET_ADDRESS missing — add them to .env")
BASE_URL = constants.MAINNET_API_URL

LEVERAGE  = max(1, min(50, int(float(os.getenv("OBI_LEVERAGE", "20")))))
IS_CROSS  = True

# --- Tokens presets (tick/min viennent du meta live si absent d'ici) ---
DEFAULT_TOKEN = os.getenv("OBI_TOKEN", "SP500")
STRATEGY = "bbo"
MID_OFFSET_BPS = max(0.0, float(os.getenv("OBI_MID_OFFSET_BPS", "1.0")))
MID_REPRICE_FRACTION = 0.25  # opening price drift; one tick is the minimum step
MID_EXIT_PROTECTION = False  # enabled explicitly by CLI/server for MID sessions
MID_EXIT_PROTECT_SEC = 30.0

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
#    → fenêtre de sortie bank ALO immédiate. On reste maker; un Stop manuel
#    explicite est la seule voie vers une fermeture marketable reduce-only.
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
# ba−1 tick), re-peggée à chaque mouvement du BBO. Seul un Stop manuel explicite
# peut demander une fermeture marketable reduce-only.
BANK_ALO_SEC       = 8.0    # durée d'une fenêtre de sortie agressive post-only
# Routine age/loss exits remain maker-only; a timer renews the ALO window.
# Only an explicit manual Stop may request a reduce-only marketable close.
HARD_EXIT_MAKER_SEC = float(os.getenv("OBI_HARD_EXIT_MAKER_SEC", "4.0"))
RUNAWAY_STOP_MULT   = 2.0
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
TP_RETRACE         = 0.40   # retracement du peak qui déclenche le bank ALO

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

# --- MID: couche adaptative (reservation price, spread capture, anti-adverse) ---
# Le mode MID fixe (mid +/- offset en permanence) a ete laisse en place comme
# repli. Par defaut on reactive le moteur de reservation: fair = micro-price
# ajuste par l'alpha OFI/TFI + le contexte macro; demi-spread = max(offset
# utilisateur, frais + marge EV, fraction du spread live). Les sorties, elles,
# ne suivent JAMAIS le mid vers le bas: elles visent la touche (ou le bank
# ALO encore plus agressif) pour reduire l'inventaire sans payer le taker.
MID_ADAPTIVE              = os.getenv("OBI_MID_ADAPTIVE", "1") not in ("0", "false", "False")
MID_MAX_SKEW_BPS          = float(os.getenv("OBI_MID_MAX_SKEW_BPS", "3.0"))
MID_EXIT_REPRICE_TOL_BPS  = float(os.getenv("OBI_MID_EXIT_REPRICE_TOL_BPS", "0.4"))
MID_EXIT_MIN_REST_SEC     = float(os.getenv("OBI_MID_EXIT_MIN_REST_SEC", "1.0"))
MID_OFF_SESSION_MR_K      = float(os.getenv("OBI_MID_OFF_SESSION_MR_K", "0.25"))
MID_REENTRY_RECHECK       = os.getenv("OBI_MID_REENTRY_RECHECK", "1") not in ("0", "false", "False")
MID_POST_ONLY_BACKOFF     = float(os.getenv("OBI_MID_POST_ONLY_BACKOFF", "0.15"))
MID_TRADES_MAXLEN         = int(os.getenv("OBI_MID_TRADES_MAXLEN", "20000"))
# A round trip pays TWO maker fees. The EV gate must price both plus a real
# edge target and a prior adverse cost (the bot must not have to lose first to
# learn that fills are toxic). Widths are in bps, not ticks, so the same
# economic meaning holds across instruments.
MID_EDGE_TARGET_BPS       = float(os.getenv("OBI_MID_EDGE_TARGET_BPS", "1.0"))
MID_ADVERSE_FLOOR_BPS     = float(os.getenv("OBI_MID_ADVERSE_FLOOR_BPS", "0.75"))
MID_MAX_QUOTE_WIDTH_BPS   = float(os.getenv("OBI_MID_MAX_QUOTE_WIDTH_BPS", "5.0"))
# One transient background read failure must not stop a healthy session with
# inventory. Retry at the maintenance cadence; fail only after N consecutive
# critical read errors (~15 s at the 5 s position cadence).
MID_MAINTENANCE_FAIL_LIMIT = max(1, int(os.getenv("OBI_MID_MAINTENANCE_FAIL_LIMIT", "3")))
# "Normal" MID exits: after a fill the bot rests a reduce-only order at the
# fee-aware profit anchor (entry +/- two maker fees and one tick) and waits.
# It no longer cuts the position at a loss on an alpha/regime/age signal: those
# bank windows were realising the spread plus the adverse move on every brief
# spike. The only loss-taking exit left is the catastrophic stop below; set
# OBI_MID_LOSS_CUTS=1 to restore the previous churn-prone loss-cut behaviour.
MID_LOSS_CUTS_ENABLED = os.getenv("OBI_MID_LOSS_CUTS", "0") not in ("0", "false", "False")
MID_CATASTROPHIC_STOP_BPS = max(0.0, float(os.getenv("OBI_MID_CATASTROPHIC_STOP_BPS", "50.0")))
# A lost signed-action ACK must not block the single action worker for the old
# 10 s default: that is a 10 s head-of-line stall on an HFT quote loop.
WS_ACK_TIMEOUT = max(0.25, float(os.getenv("OBI_WS_ACK_TIMEOUT", "2.0")))
POST_ONLY_MAX_RETRIES = 2   # reprice 1 tick et retente si l'ALO croiserait
MAX_SPREAD_TICKS    = 40    # spread book au-delà: marché cassé, on ne quote pas

# --- Risque ---
STOP_ADVERSE_BPS   = 20.0   # uPnL < −20 bps du notionnel → fenêtre de sortie maker
STOP_PAUSE_SEC     = 60.0
MAX_HOLD_SEC       = 120.0  # position plus vieille → débouclage passif forcé
FORCE_FLAT_EXTRA   = 45.0   # ... et fenêtre maker renouvelée si toujours pas flat
MAX_SESSION_LOSS_USD = float(os.getenv("OBI_MAX_SESSION_LOSS_USD", "5"))
HOURLY_LOSS_MULT     = float(os.getenv("OBI_HOURLY_LOSS_MULT", "0.5"))
HOURLY_PAUSE_SEC     = float(os.getenv("OBI_HOURLY_PAUSE_SEC", "900"))
DEADMAN_FEED_SEC   = 12.0   # feed muet + position → REST refresh sinon cancel + stop
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
    distance = mid * MID_OFFSET_BPS / 10000.0
    bid, ask = px_down(mid - distance, tick), px_up(mid + distance, tick)
    if bid >= ask:
        # Zero/sub-resolution offsets at an aligned mid must not create a
        # self-crossing pair. Outward rounding supplies the valid minimum;
        # forcing a full tick BEFORE rounding unnecessarily widened tight MID.
        bid, ask = px_down(mid - tick / 2, tick), px_up(mid + tick / 2, tick)
    if bid <= 0 or ask <= bid:
        raise ValueError("MID distance produces invalid prices")
    return bid, ask


def mid_reprice_ticks(mid: float, tick: float) -> int:
    """Useful opening-price drift: 25% of the offset, rounded up to ticks."""
    if (not all(math.isfinite(v) and v > 0 for v in (mid, tick))
            or not math.isfinite(MID_OFFSET_BPS) or not 0 <= MID_OFFSET_BPS < 10000):
        raise ValueError("Invalid MID repricing inputs")
    return max(1, math.ceil(round(mid * MID_OFFSET_BPS / 10000.0
                                  * MID_REPRICE_FRACTION / tick, 9)))


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
            "phase": phase, "action": "Connecting to Hyperliquid" if phase == "connecting"
            else "Startup failed", "error": error}
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as stream:
        json.dump(data, stream)
    os.replace(temporary, path)


def stop_file_path(symbol: str) -> str:
    """Fichier sentinelle: sa présence demande un arrêt PROPRE (cancel all,
    puis fermeture reduce-only seulement si le Stop est explicitement manuel)
    au bot de ce symbole — utilisé par le dashboard pour stopper un process
    enfant sans le tuer brutalement."""
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
                    "maxLeverage": int(u.get("maxLeverage", 50) or 50),
                    "onlyIsolated": bool(u.get("onlyIsolated", False))}
    raise ValueError(f"{symbol} not found in {dex} meta")


def leverage_settings(meta, requested):
    """Respect the venue's leverage ceiling and mandatory margin mode."""
    ceiling = int(meta["maxLeverage"])
    if ceiling < 1:
        raise ValueError("Invalid market maximum leverage")
    return min(int(requested), ceiling), not bool(meta.get("onlyIsolated", False))


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
            elif row[0] < self._cur[0]:
                return  # delayed message for an older interval: never regress
            elif row[0] == self._cur[0]:
                self._cur = row               # même bougie: maj live o/h/l/c/v
            else:
                # Close the previous bar only once: a replayed candle must not
                # duplicate an already-closed bar in the series.
                if not self.bars or self._cur[0] > self.bars[-1][0]:
                    self.bars.append(self._cur)
                self._cur = row
                self._recompute_locked()

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
        self.micro_tilt_bps = 0.0      # (micro - mid)/mid * 1e4, same snapshot
        self.adj_best_bid = 0.0        # best bid after removing our own size
        self.adj_best_ask = 0.0
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
                ref = ref_ts = None
                for ts, m in reversed(self.mids):
                    if now - ts >= 1.0:
                        ref, ref_ts = m, ts
                        break
                if ref and ref > 0:
                    dt = max(1.0, now - ref_ts)
                    # Normalize a multi-second gap move to a 1 s-equivalent
                    # move; otherwise a 30 s quiet gap looks like a 1 s spike.
                    r = abs(mid - ref) / ref * 10000.0 * math.sqrt(1.0 / dt)
                    a = 1.0 - math.exp(-max(1.0, now - self._last_vol_ts) / VOL_TAU_SEC)
                    self.vol_bps_ewma += a * (r - self.vol_bps_ewma)
                self._last_vol_ts = now
            # Accumuler l'OFI; état top-of-book précédent; micro-price.
            self._ofi_accum += ofi_evt
            self._prev_bb_px, self._prev_bb_sz = b0_px, b0_sz
            self._prev_ba_px, self._prev_ba_sz = a0_px, a0_sz
            self.micro_px = micro
            self.micro_tilt_bps = tilt_bps
            try:
                self.adj_best_bid = float(bids[0]["px"])
                self.adj_best_ask = float(asks[0]["px"])
            except (IndexError, KeyError, TypeError, ValueError):
                self.adj_best_bid = self.adj_best_ask = 0.0
            self.b0_sz, self.a0_sz = b0_sz, a0_sz   # tailles AU touch (toxicité fill)
            # Snapshot des niveaux pour le queue model (dicts px→sz, ≤20 niveaux).
            self.book_bid_szs = {float(l["px"]): float(l["sz"]) for l in bids}
            self.book_ask_szs = {float(l["px"]): float(l["sz"]) for l in asks}

            # Clôture du bucket OFI (1/s): z-score + échantillon de régression.
            if self._ofi_bucket_ts == 0.0:
                self._ofi_bucket_ts = now
                self._tfi_bucket_start = now
            elif now - self._ofi_bucket_ts >= OFI_BUCKET_SEC:
                # Buckets close on an l2Book event, so their duration varies.
                # Store a rate (size/s, USD/s); a raw sum mixes 1 s and 5 s
                # buckets and distorts the z-score variance.
                span = max(1e-3, now - self._ofi_bucket_ts)
                v = self._ofi_accum / span
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
                tfi_v = sum(sv for ts_t, sv in self.trades if ts_t >= t_start) / span
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
            # Borne temporelle: les fenetres de flux (flow/TFI) ne remontent
            # jamais au-dela de FLOW_WINDOW_SEC. Sans cette purge, un bot
            # headless (dashboard jamais rendu) accumulait tous les trades et
            # le bucket TFI devenait O(historique) chaque seconde.
            cutoff = ts - max(FLOW_WINDOW_SEC, OFI_BUCKET_SEC * 2.0)
            while self.trades and self.trades[0][0] < cutoff:
                self.trades.popleft()
            while len(self.trades) > MID_TRADES_MAXLEN:
                self.trades.popleft()

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
            cutoff = now - window_sec
            ref = ref_ts = None
            # Newest sample at or before the window start (reverse scan; the
            # old forward scan walked from the oldest and returned the first
            # sample *inside* the window, which is not the window reference).
            for ts, m in reversed(self.mids):
                if ts <= cutoff:
                    ref, ref_ts = m, ts
                    break
            if ref is None:
                ref, ref_ts = self.mids[0][1], self.mids[0][0]
            if ref is None or ref <= 0:
                return 0.0
            if now - ref_ts > max(3.0 * window_sec, window_sec + 1.0):
                return 0.0   # too sparse to call this a momentum signal
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

    def micro_tilt(self):
        """(tilt bps vs snapshot mid, local receive time of that snapshot).

        A relative tilt survives a BBO that is newer than the l2Book snapshot;
        the absolute micro price would point at a stale level.
        """
        with self.lock:
            return self.micro_tilt_bps, self._last_obi_ts

    def adjusted_touch(self):
        """Best bid/ask with this session's own resting size removed."""
        with self.lock:
            return self.adj_best_bid, self.adj_best_ask, self._last_obi_ts

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

    def reset_feed_state(self):
        """Drop cross-gap state after a reconnect.

        A reconnect or a REST recovery snapshot must not produce a synthetic
        OFI event, a phantom beta sample or stale alpha from before the gap.
        The learned beta itself is kept; only pending/forward samples are
        discarded.
        """
        with self.lock:
            self._prev_bb_px = self._prev_bb_sz = 0.0
            self._prev_ba_px = self._prev_ba_sz = 0.0
            self._ofi_accum = 0.0
            self._ofi_bucket_ts = 0.0
            self._tfi_bucket_start = 0.0
            self.trades.clear()
            self.mids.clear()
            self.sig_hist.clear()
            self._sig_s1 = self._sig_s2 = 0.0
            self._first_sig_ts = 0.0
            self.tfi_hist.clear()
            self._tfi_s1 = self._tfi_s2 = 0.0
            self._beta_pending.clear()
            self.micro_px = 0.0
            self.micro_tilt_bps = 0.0
            self.adj_best_bid = self.adj_best_ask = 0.0
            self.obi_ewma = self.obi_raw = self.obi_prev_raw = 0.0
            self.tilt_ewma = 0.0
            self.flow = 0.0
            self.vol_bps_ewma = 0.0
            self._last_vol_ts = 0.0
            self._last_obi_ts = 0.0
            self._regime = None
            self._regime_ts = 0.0
            self._regime_ext = 0.0
            self.b0_sz = self.a0_sz = 0.0

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
        self.telemetry = LatencyMetrics() if fast_mm() else None
        self.book_revision = 0
        self.top_revision = 0
        self.book_received_ns = 0
        self.last_book_exchange_ms = 0
        self.last_depth_exchange_ms = 0
        self._last_l2_receive = 0.0
        self._submit_position_revision = None
        self._execution_revision = 0
        self._mid_requests_in_flight = 0
        self._position_verified_at = 0.0
        self._orders_verified_at = 0.0
        self._maintenance = None
        self._maintenance_error_count = 0
        self._maintenance_error_since = 0.0
        self._position_read_generation = 0
        self._feed_generation = 0
        self._quote_due_at = 0.0
        self._urgent_quote = False
        self._execution_transport = None
        self._quote_worker = None
        self._action_in_progress = False
        self._inventory_mark_known = True
        self._inventory_basis_known = True
        self._risk_basis_revision = 0
        self._risk_revision = 0
        self.lock = threading.Lock()
        self.wallet = eth_account.Account.from_key(AGENT_PRIVATE_KEY)
        self.dex = SYMBOL.split(":", 1)[0]
        self.fixed_spot_meta = build_fixed_spot_meta()
        self.info = retry_read("market initialization", lambda: Info(
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
        self.position_revision = 0
        self.position_dirty = False
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
        self._sig_micro = 0.0
        self._sig_micro_tilt = 0.0
        self._sig_micro_ts = 0.0
        self._sig_adj_bid = 0.0
        self._sig_adj_ask = 0.0
        self._sig_adj_ts = 0.0
        self._sig_mom_fast = 0.0
        self._sig_mom_slow = 0.0
        self._qf_b: Optional[float] = None
        self._qf_a: Optional[float] = None
        # Un fill rend la lecture REST de position prioritaire (re-entree rapide
        # sans supprimer la verification: le worker est reveille tout de suite).
        self._position_recheck_due = False
        self._orders_recheck_due = False
        self._last_signal_refresh = 0.0

        # PnL positif: take-profit actif + protection de session (sans arrêt)
        self.bank_until = 0.0         # fenêtre de sortie bank ALO (maker, à la touche)
        self._hard_exit_at = 0.0      # échéance de renouvellement de la fenêtre maker
        self._hard_exit_reason = ""
        self.upnl_peak_bps = 0.0      # plus haut uPnL (bps) de la position courante
        self._last_mark_upnl = 0.0    # dernier mark connu (book temporairement indisponible)
        self._last_mark_bps = 0.0
        self._last_mark_at = 0.0
        self._session_halt = False    # limite de perte atteinte: sorties seules
        self._session_halt_at = 0.0
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
        self.taker_fills = 0
        self.unknown_role_fills = 0
        self.accounting_complete = True
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
        self.quote_warning = ""
        self.quote_retry_after = {"B": 0.0, "A": 0.0}
        self.quote_reject_count = {"B": 0, "A": 0}
        self.start_time = time.time()

        self.running = True
        self.wake = threading.Event()
        self.console = Console()
        # Le thread WS peut maintenant envoyer des actions d'urgence (cancel
        # de sweep, scratch toxique) en concurrence avec la boucle principale:
        # sérialiser les appels exchange (requests.Session non thread-safe).
        self.api_lock = threading.Lock()
        self.safety = SafetyLatch(self._halt, os.getenv("ZEUS_SAFETY_FILE"),
                                  is_running=lambda: self.running and not os.path.exists(self._stop_file) and supervisor_alive(),
                                  passive_only=True)
        self.safety.metrics = self.telemetry
        if self.live:
            from zeus.signing import SigningCoordinator
            from zeus import PROJECT_ROOT
            signing = SigningCoordinator(PROJECT_ROOT / ".zeus-state" / "signing", self.wallet.address, self.telemetry)
            self._execution_transport = WebSocketExecution(
                BASE_URL, self.telemetry, final_gate=self._final_ws_gate,
                request_timeout=WS_ACK_TIMEOUT)
            signing.instrument_transport(self.exchange, self._execution_transport)
            self.exchange = signing.wrap(
                self.exchange, before_send=self._prepare_mid_submission,
                after_send=self._record_mid_response,
                before_action=self._begin_mid_action,
                after_action=self._end_mid_action)
            self.exchange = self.safety.wrap(self.exchange, reconcile_cancel=self._reconcile_missing_cancels,
                                            reconcile_reduce_only=self._reconcile_reduce_only,
                                            reconcile_post_only=self._reconcile_post_only,
                                            reconcile_modify=self._reconcile_missing_modifies,
                                            reconcile_invalid_modify=self._reconcile_invalid_modifies)
        self._emerg_ts: Dict[str, float] = {"B": 0.0, "A": 0.0}
        self._scratch_ts = 0.0
        # Queue position model: taille devant nous à la pose / restante.
        self.queue_init: Dict[int, float] = {}
        self.queue_ahead: Dict[int, float] = {}

        # Canal inter-process avec le dashboard: stats sortantes + stop entrant.
        self._stats_file = stats_file_path(SYMBOL)
        self._stop_file = stop_file_path(SYMBOL)
        self._last_stats_write = 0.0
        # A stop request may arrive during construction; never delete it here.
        try:
            signal.signal(signal.SIGINT, self._sig_shutdown)
            signal.signal(signal.SIGTERM, self._sig_shutdown)
        except ValueError:
            pass  # pas dans le main thread (lancé par un dashboard)

    # ------------------------------------------------------------------ setup
    def _sig_shutdown(self, signum=None, *_):
        if signum == signal.SIGINT and os.getenv("OBI_HEADLESS","")!="1" and sys.stdin.isatty():
            self.safety.request_manual_stop()
        else:
            self.safety.stop()  # SIGTERM/supervisor shutdown never authorizes taker

    def _manual_stop_file(self):
        try:
            path=getattr(self,"_stop_file",None)
            if not path:
                return False
            data=json.loads(Path(path).read_text(encoding="utf-8"))
            return (isinstance(data,dict) and data.get("manual") is True
                    and data.get("session_id")==self.safety.session_id)
        except (OSError,ValueError,TypeError):
            return False

    def _halt(self):
        self.running = False
        self.wake.set()

    def set_leverage(self):
        if not self.live:
            return
        try:
            self.exchange.update_leverage(LEVERAGE, SYMBOL, is_cross=IS_CROSS)
        except Exception as exc:
            self.safety.fail(f"Leverage setup failed: {exc}")

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
                print(f"🧹 {len(reqs)} orphan order(s) cancelled at startup")
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
        with self.lock:
            self._feed_generation = getattr(self, "_feed_generation", 0) + 1
            generation = self._feed_generation
            if generation > 1:
                self.position_dirty = True
                self.position_revision += 1
                self._inventory_basis_known = False
                self.safety.account_known = False
                self._bump_execution_locked()
        if generation > 1:
            # Never carry pre-outage flow/micro/beta state across a gap.
            self.signal.reset_feed_state()
            with self.lock:
                self._sig_z = self._sig_alpha = 0.0
                self._sig_vol = self._sig_touch = 0.0
                self._sig_micro_tilt = self._sig_micro_ts = 0.0
                self._sig_adj_bid = self._sig_adj_ask = self._sig_adj_ts = 0.0
                self._sig_mom_fast = self._sig_mom_slow = 0.0
                self._reg_now = (None, 0)
                self._early_now = (None, 0)
                self._macro = MACRO_NEUTRAL
                self._qf_b = self._qf_a = None
        ws_info = retry_read("initialisation du flux", lambda: Info(
            BASE_URL, skip_ws=True, spot_meta=self.fixed_spot_meta,
            perp_dexs=PERP_DEXS, timeout=10))
        self.ws_info = ws_info
        if not self.running or self.safety.stopped.is_set():
            ws_info.session.close()
            return
        ws_info.ws_manager = WebsocketManager(BASE_URL)
        ws_info.ws_manager.start()
        def current(callback, pass_generation=False, allow_stopped=False):
            def dispatch(data):
                if generation == self._feed_generation and (self.running or allow_stopped):
                    if pass_generation:
                        callback(data, generation=generation)
                    else:
                        callback(data)
            return dispatch
        ws_info.subscribe({"type": "bbo", "coin": SYMBOL}, current(self._on_bbo, True))
        ws_info.subscribe({"type": "l2Book", "coin": SYMBOL, "fast": True}, current(self._on_l2, True))
        ws_info.subscribe({"type": "trades", "coin": SYMBOL}, current(self._on_trades))
        ws_info.subscribe({"type": "userFills", "user": ACCOUNT_ADDRESS}, current(self._on_user_fills, True, True))
        ws_info.subscribe({"type": "orderUpdates", "user": ACCOUNT_ADDRESS}, current(self._on_order_updates, True))
        if MACRO_ENABLE:
            self.ws_info.subscribe(
                {"type": "candle", "coin": SYMBOL, "interval": MACRO_INTERVAL},
                current(self._on_candle))

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

    def _accept_top_locked(self, bb, ba, exchange_ms, received_ns, source, generation=None):
        """Order BBO/L2 by exchange time; BBO wins a tie. Caller holds lock."""
        if generation is not None and generation != getattr(self, "_feed_generation", 0):
            return False
        last_ms = getattr(self, "last_book_exchange_ms", 0)
        if exchange_ms < last_ms or (exchange_ms == last_ms
                and source != "bbo" and getattr(self, "book_source", "") == "bbo"):
            return False
        if (bb, ba) != (getattr(self, "best_bid", 0.0), getattr(self, "best_ask", 0.0)):
            # Only a touch change can invalidate a signed limit price. A
            # depth-only L2 update must not abort the in-flight signed action.
            self.top_revision = getattr(self, "top_revision", 0) + 1
        self.best_bid, self.best_ask = bb, ba
        self.mid = (bb + ba) / 2.0
        self.last_tick_ts = time.time()
        self.ws_ready = True
        self.book_revision = getattr(self, "book_revision", 0) + 1
        self.book_received_ns = received_ns
        self.last_book_exchange_ms = exchange_ms
        self.book_source = source
        return True

    def _on_bbo(self, data, generation=None):
        received_ns = time.perf_counter_ns()
        try:
            payload = data["data"]
            if payload.get("coin") != SYMBOL:
                return
            bid, ask = payload["bbo"]
            bb, ba = finite(bid["px"]), finite(ask["px"])
            exchange_ms = int(payload["time"])
            if bb <= 0 or ba <= bb or exchange_ms <= 0:
                return
            with self.lock:
                accepted = self._accept_top_locked(bb, ba, exchange_ms, received_ns, "bbo", generation)
            if accepted:
                if getattr(self, "telemetry", None):
                    self.telemetry.count("book_events")
                    self.telemetry.count("bbo_events")
                    self.telemetry.observe("bbo_callback", (time.perf_counter_ns()-received_ns)/1e6)
                self.wake.set()
        except (KeyError, TypeError, ValueError, OverflowError):
            if getattr(self, "telemetry", None):
                self.telemetry.count("invalid_bbo_events")

    def _on_l2(self, data, generation=None):
        received_ns = time.perf_counter_ns()
        try:
            levels = data.get("data", {}).get("levels", [])
            if len(levels) < 2 or not levels[0] or not levels[1]:
                return
            bb = float(levels[0][0]["px"])
            ba = float(levels[1][0]["px"])
            if not math.isfinite(bb + ba) or bb <= 0 or ba <= bb:
                return
            exchange_ms = int(data.get("data", {}).get("time", 0) or 0)
            coin = data.get("data", {}).get("coin", SYMBOL)
            if exchange_ms <= 0:
                return
            with self.lock:
                last_depth = getattr(self, "last_depth_exchange_ms",
                                     getattr(self, "last_book_exchange_ms", 0))
                if (coin != SYMBOL or generation is not None and generation != getattr(self, "_feed_generation", 0)
                        or exchange_ms < last_depth):
                    return
                self.last_depth_exchange_ms = exchange_ms
                self._last_l2_receive = time.time()
                accepted = self._accept_top_locked(bb, ba, exchange_ms, received_ns, "l2_fast", generation)
                # A quiet BBO is not a dead feed: a current equal-time depth
                # snapshot confirms transport health without replacing its prices.
                if (generation is None or generation == getattr(self, "_feed_generation", 0)) and exchange_ms == getattr(self, "last_book_exchange_ms", 0):
                    self.last_tick_ts = time.time()
            # Feed the signal only after the generation/ordering fence, and
            # with our own resting size removed. Otherwise our own quotes are
            # read back as external order flow and bias OFI/micro/queue.
            sig_bids, sig_asks = self._levels_without_own(levels[0], levels[1])
            self.signal.on_l2(sig_bids, sig_asks)
            if getattr(self,"telemetry",None):
                self.telemetry.count("l2_events")
                if accepted:
                    self.telemetry.count("book_events")
                self.telemetry.observe("l2_callback",(time.perf_counter_ns()-received_ns)/1e6)
            self.wake.set()
        except Exception:
            pass

    def _levels_without_own(self, bids, asks):
        """Remove this session's resting size from the depth fed to signals.

        OFI/micro/queue must describe the *external* book; otherwise every
        place/modify/cancel we send is read back as market flow.
        """
        with self.lock:
            own = []
            for row in self.orders.values():
                try:
                    own.append((row.get("side"), float(row["px"]), float(row["sz"])))
                except (KeyError, TypeError, ValueError):
                    continue
        def strip(rows, side):
            out = []
            for row in rows:
                try:
                    px, sz = float(row["px"]), float(row["sz"])
                except (KeyError, TypeError, ValueError):
                    continue
                left = sz
                for own_side, own_px, own_sz in own:
                    if own_side == side and abs(px - own_px) < 1e-9:
                        left -= own_sz
                if left > 1e-12:
                    out.append({"px": row["px"], "sz": str(left)})
            return out
        sig_bids, sig_asks = strip(bids, "B"), strip(asks, "A")
        if not sig_bids or not sig_asks:
            return bids, asks
        return sig_bids, sig_asks

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
            received = time.time()
            for t in data.get("data", []) or []:
                # Use the local receive clock for bucketing: the signal window
                # and the OFI/TFI buckets must share one clock (exchange clock
                # skew otherwise drops or double-counts trades).
                ts = received
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
        """Met en pause immédiate le côté ouvrant menacé par un sweep.

        Appelé depuis le thread WebSocket: il ne fait AUCUN appel réseau ici
        (un cancel synchrone bloquerait le feed de marché pendant un ACK).
        Il arme la pause et réveille la boucle; le worker d'action annule ou
        replace la quote au cycle suivant. Les sorties reduce-only ne sont
        jamais visées (un dump qui remplit notre rachat de short est un bon
        fill)."""
        now = time.time()
        if not self.live or now - self._emerg_ts[side] < EMERG_COOLDOWN_SEC:
            return
        self._emerg_ts[side] = now
        with self.lock:
            pause = getattr(self, "side_pause_until", {"B": 0.0, "A": 0.0})
            pause[side] = max(float(pause.get(side, 0.0) or 0.0),
                              now + EMERG_COOLDOWN_SEC)
            self.side_pause_until = pause
            self.last_intent = None
            self.last_action = f"⚡ sweep ${burst_usd:,.0f} → pause {side}"
            self.force_quote = True
            self._urgent_quote = True
        self.wake.set()

    def _reset_position_tracking_locked(self, *, flat, starts_new):
        """Reset only the state derived from the previous position."""
        if flat or starts_new:
            self.upnl_peak_bps = 0.0
            self.bank_until = 0.0
            self._hard_exit_at = 0.0
            self._hard_exit_reason = ""
        if flat:
            self.pos_since = 0.0
        elif starts_new or not getattr(self, "pos_since", 0.0):
            self.pos_since = time.time()

    def _refresh_inventory_mark_locked(self):
        """Recompute uPnL from the current book; unknown is not zero."""
        if (not getattr(self,"_inventory_basis_known",True)
                or getattr(self,"_risk_basis_revision",self.position_revision)!=self.position_revision):
            self._inventory_mark_known=False
            return None
        position = getattr(self, "position", 0.0)
        entry = getattr(self, "entry_price", 0.0)
        received = getattr(self, "book_received_ns", 0)
        age = (time.perf_counter_ns() - received) / 1e9 if received else None
        try:
            mark = mark_inventory(
                position, entry, getattr(self, "best_bid", 0.0), getattr(self, "best_ask", 0.0),
                book_age=age, max_book_age=DEADMAN_FEED_SEC if age is not None else None)
        except (ValueError, InventoryDesyncError):
            self._inventory_mark_known = False
            return None
        self.unrealized_pnl = mark.unrealized_pnl
        self._inventory_mark_known = True
        self._inventory_mark_source = mark.source
        return mark

    def _on_user_fills(self, data, generation=None):
        try:
            payload = data.get("data", {})
            fills = payload.get("fills", []) or []
            # Un snapshot userFills contient aussi les fills recus pendant une
            # coupure WS. Les ignorer sous-comptait le PnL live (limite de
            # perte, TP) jusqu'au bilan final. On applique donc uniquement les
            # fills de CETTE session; les anciens restent dedupliques.
            is_snapshot = bool(payload.get("isSnapshot"))
            with self.lock:
                if generation is not None and generation != getattr(self, "_feed_generation", 0):
                    return
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
                        if is_snapshot:
                            try:
                                fill_ms = finite(f.get("time", 0) or 0)
                            except (TypeError, ValueError):
                                fill_ms = 0.0
                            started_ms = float(getattr(self, "start_time", 0.0) or 0.0) * 1000.0
                            if fill_ms <= 0 or (started_ms > 0 and fill_ms < started_ms - 5000.0):
                                # Ancien fill (session precedente): dedup only.
                                self.processed_fills.add(tid)
                                continue
                        px, sz = finite(f["px"]), finite(f["sz"])
                        fee, closed_pnl = finite(f.get("fee",0)), finite(f.get("closedPnl",0))
                        side = str(f.get("side", "")).upper()
                        if px <= 0 or sz <= 0 or side not in {"B","A"} or f.get("tid") is None:
                            raise ValueError("Invalid fill identity/price/size/side")
                        fill_oid=int(f.get("oid",0))
                        # A fill consumes (part of) a tracked order. Removing it
                        # immediately stops the urgent exit from modifying a
                        # dead OID and triggering a multi-REST reconcile storm.
                        if fill_oid:
                            tracked = self.orders.get(fill_oid)
                            if tracked is not None:
                                left = float(tracked.get("sz", 0.0) or 0.0) - sz
                                if left <= MIN_SIZE / 2:
                                    self.orders.pop(fill_oid, None)
                                    self.queue_init.pop(fill_oid, None)
                                    self.queue_ahead.pop(fill_oid, None)
                                else:
                                    tracked = dict(tracked)
                                    tracked["sz"] = round_sz(left)
                                    self.orders[fill_oid] = tracked
                        previous_position = getattr(self, "position", 0.0)
                        previous_entry = getattr(self, "entry_price", 0.0)
                        transition = None
                        desync = None
                        try:
                            transition = apply_fill(
                                previous_position, previous_entry, side, px, sz,
                                quantity_decimals=SZ_DECIMALS,
                                reported_start_position=f.get("startPosition"))
                        except InventoryDesyncError as exc:
                            # The fill is real, but it cannot be added blindly to
                            # an inventory snapshot another read may already own.
                            desync = exc
                        except ValueError as exc:
                            if (previous_position and previous_entry <= 0
                                    and "known positive entry" in str(exc)):
                                # Accounting still belongs to this fill; the
                                # quantity basis needs an authoritative sync.
                                desync = InventoryDesyncError(
                                    "Local entry basis is unknown; reconcile before applying")
                            else:
                                raise
                        self.processed_fills.add(tid)
                        received_fills=getattr(self,"_received_fills",{})
                        received_fills[tid]=(fill_oid,sz)
                        self._received_fills=received_fills
                        if "fee" not in f or "closedPnl" not in f:self.accounting_complete=False
                        sd = side
                        new_pos = transition.new_position if transition is not None else previous_position
                        reported_start = f.get("startPosition")
                        try:
                            reported_opening = (reported_start is not None
                                and (finite(reported_start) == 0
                                     or (side == "B") == (finite(reported_start) > 0)))
                        except (TypeError, ValueError):
                            reported_opening = False
                        if "fee" in f:
                            rate = max(0.0, fee) / (px * sz)
                            if f.get("crossed") is False:
                                self._observed_maker_rate = max(
                                    getattr(self, "_observed_maker_rate", 0.0), rate)
                            if reported_opening or (transition is not None and (
                                    abs(new_pos) > abs(previous_position) + MIN_SIZE / 4
                                    or new_pos * previous_position < 0)):
                                self._observed_entry_rate = max(
                                    getattr(self, "_observed_entry_rate", 0.0), rate)
                        if transition is not None:
                            is_opening_fill = (previous_position == 0
                                               or abs(new_pos) > abs(previous_position) + MIN_SIZE / 4
                                               or new_pos * previous_position < 0)
                        else:
                            is_opening_fill = reported_opening
                        now_f = time.time()
                        # Only entry/add fills feed the side markout EWMA. An
                        # exit fill is a sell/cover, not evidence that the
                        # opening side of that sign is toxic.
                        if is_opening_fill:
                            self.pending_markouts.append((now_f, sd, px))
                        self.last_side_fill_ts[sd] = now_f
                        q = self.side_fills[sd]
                        q.append(now_f)
                        while q and q[0] < now_f - BURST_WINDOW_SEC:
                            q.popleft()
                        if len(q) >= 2:
                            self.side_pause_until[sd] = now_f + BURST_PAUSE_SEC
                        if transition is not None:
                            was_below_min = abs(previous_position) < MIN_SIZE / 2
                            self.position = transition.new_position
                            self.entry_price = transition.entry_price
                            flat = transition.became_flat or abs(self.position) < MIN_SIZE / 2
                            starts_new = transition.starts_new_position or was_below_min
                            self._reset_position_tracking_locked(flat=flat, starts_new=starts_new and not flat)
                        else:
                            self.position_dirty = True
                            self.safety.account_known = False
                            self._inventory_mark_known = False
                            self._inventory_basis_known = False
                            self.last_error = f"fill basis: {str(desync)[:80]}"
                        self.position_revision += 1
                        if transition is not None:
                            self._risk_basis_revision=self.position_revision
                            self._refresh_inventory_mark_locked()
                        self._bump_execution_locked()
                        # A startPosition-validated fill is authoritative for
                        # the local basis; openings may resume immediately.
                        # An unvalidated fill keeps dirty until the REST read.
                        self.position_dirty = not (transition is not None
                                                   and f.get("startPosition") is not None)
                        if MID_REENTRY_RECHECK:
                            self._position_recheck_due = True
                        self.total_volume += px * sz
                        self.total_fees += fee
                        self.session_pnl += closed_pnl
                        self.hourly_net_events.append((now_f, closed_pnl - fee))
                        self.fills_count += 1
                        if f.get("crossed") is False:
                            self.maker_fills += 1
                        elif f.get("crossed") is True:
                            self.taker_fills = getattr(self,"taker_fills",0)+1
                        else:
                            self.unknown_role_fills = getattr(self,"unknown_role_fills",0)+1
                        self.last_fill_ts = time.time()
                        # Flip éclair: reposter la sortie immédiatement.
                        self.force_quote = True
                        self._urgent_quote = True
                        self._last_fill_received_ns = time.perf_counter_ns()
                    except Exception as exc:
                        self.position_dirty = True
                        self.position_revision += 1
                        self._bump_execution_locked()
                        self.safety.account_known = False
                        self._inventory_basis_known = False
                        self.accounting_complete = False
                        if MID_REENTRY_RECHECK:
                            self._position_recheck_due = True
                        self.last_error = f"fill parse: {str(exc)[:80]}"
            self.wake.set()
            # SCRATCH TOXIQUE (hors self.lock): si on vient de se faire
            # remplir en OUVERTURE alors que l'alpha est franchement contre
            # la position, ouvrir tout de suite une fenêtre bank ALO maker
            # plutôt que de subir les 3-10 bps du move qui suit un fill informé.
            self._maybe_toxic_scratch()
        except Exception:
            pass

    def _maybe_toxic_scratch(self):
        """Fill ouvrant reçu avec l'alpha franchement contre → sortie BANK ALO
        immédiate (maker, tête de queue) au lieu de l'ancien IOC taker. Le
        flip éclair + le re-peg d'urgence font le reste; si la perte creuse
        malgré tout, la fenêtre maker du hard-stop vol-scalé est renouvelée."""
        now = time.time()
        if not MID_LOSS_CUTS_ENABLED:
            # Normal mode: keep the passive profit-anchored exit resting.
            return
        if STRATEGY != "mid" or not self.live or now - self._scratch_ts < SCRATCH_COOLDOWN_SEC:
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
            self._urgent_quote = True
        self.wake.set()

    def _on_order_updates(self, data, generation=None):
        try:
            payload = data.get("data", [])
            updates = payload if isinstance(payload, list) else payload.get("orders", [])
            terminal = ("fill", "cancel", "reject", "margin", "reduceonly", "badalo", "expired")
            changed = False
            with self.lock:
                if generation is not None and generation != getattr(self, "_feed_generation", 0):
                    return
                self.last_orderupdate_ts = time.time()
                for u in updates or []:
                    if not isinstance(u, dict):
                        continue
                    order = u.get("order", u)
                    if not self._coin_matches(order.get("coin")):
                        continue
                    oid = int(order.get("oid", u.get("oid", 0)) or 0)
                    if not oid:
                        continue
                    self._bump_execution_locked()
                    status = str(u.get("status", "")).lower().replace("_", "")
                    if any(m in status for m in terminal):
                        if "fill" in status or "reduceonly" in status:
                            self.position_revision += 1
                            self._bump_execution_locked()
                            self.position_dirty = True
                            self.force_quote = True
                        if oid in self.orders:
                            self.orders.pop(oid, None)
                            self.queue_init.pop(oid, None)
                            self.queue_ahead.pop(oid, None)
                            changed = True
                    elif status in {"open", "resting"} and oid not in self.orders:
                        if fast_mm() and not self.safety.owns(order):
                            continue
                        raw_ro = order.get("reduceOnly")
                        self.orders[oid] = {
                            "side": "B" if order.get("side") == "B" else "A",
                            "px": float(order.get("limitPx", 0) or 0),
                            "sz": float(order.get("sz", 0) or 0),
                            # None = genuinely unknown; a known flag avoids a
                            # needless cancel/replace (and the unprotected gap).
                            "ro": raw_ro if isinstance(raw_ro, bool) else None,
                            "ts": time.time(),
                        }
                        changed = True
                if changed:
                    self.last_intent = None
            if changed:
                self.wake.set()
        except Exception:
            pass

    def refresh_bbo_rest(self, reader=None) -> bool:
        try:
            with self.lock:
                revision = getattr(self, "book_revision", 0)
                generation = getattr(self, "_feed_generation", 0)
            book = reader() if reader is not None else self.info.l2_snapshot(SYMBOL)
            levels = book.get("levels", []) if isinstance(book, dict) else []
            if len(levels) < 2 or not levels[0] or not levels[1]:
                return False
            bb, ba = float(levels[0][0]["px"]), float(levels[1][0]["px"])
            if not math.isfinite(bb+ba) or bb <= 0 or ba <= bb:
                return False
            with self.lock:
                if (revision != getattr(self, "book_revision", 0)
                        or generation != getattr(self, "_feed_generation", 0)):
                    return self.ws_ready  # newer WS event already won the race
                exchange_ms = int(book.get("time", 0) or getattr(self, "last_book_exchange_ms", 0))
                if exchange_ms < getattr(self, "last_book_exchange_ms", 0):
                    return False
                self._accept_top_locked(bb, ba, exchange_ms, time.perf_counter_ns(), "rest", generation)
                self.last_tick_ts = time.time()
            # Do not inject a REST recovery snapshot into the warm signal
            # state: the next accepted WS depth/BBO event restores it.
            return True
        except Exception:
            return False

    # ----------------------------------------------- MID background maintenance
    def _bump_execution_locked(self):
        """Caller holds self.lock; revisions fence all asynchronous REST results."""
        if fast_mm():
            self._execution_revision = getattr(self, "_execution_revision", 0) + 1

    def _begin_mid_action(self, name):
        with self.lock:
            self._mid_requests_in_flight = getattr(self, "_mid_requests_in_flight", 0) + 1
            self._bump_execution_locked()

    def _end_mid_action(self, name):
        with self.lock:
            self._mid_requests_in_flight = max(0, getattr(self, "_mid_requests_in_flight", 0) - 1)
            self._bump_execution_locked()

    def _mid_opening_allowed_locked(self):
        if getattr(self, "_maintenance", None) is None:
            return True
        now = time.monotonic()
        position_at = getattr(self, "_position_verified_at", 0)
        orders_at = getattr(self, "_orders_verified_at", 0)
        return (position_at > 0 and orders_at > 0
                and now - position_at <= SYNC_EVERY_SEC
                and now - orders_at <= RECONCILE_EVERY_SEC)

    def _orders_fingerprint_locked(self):
        """Caller holds self.lock. Changes only when tracked orders change."""
        return tuple(sorted(
            (int(oid), str(row.get("side", "")),
             round(float(row.get("px", 0) or 0), 10),
             round(float(row.get("sz", 0) or 0), 10),
             row.get("ro"))
            for oid, row in self.orders.items()))

    def _apply_maintenance_result(self, result):
        """Only called by the trading loop. Stale replies never mutate account state.

        Position and order components are fenced independently: a quote
        place/cancel does not change inventory, so it must not discard a fresh
        position read (which keeps opening verification alive during active
        quoting). Orders still require the execution revision fence.
        """
        recheck_position = False
        recheck_orders = False
        fail_maintenance = False
        with self.lock:
            request = result.request
            if (not self.running or self.safety.stopped.is_set()
                    or request.session_id != self.safety.session_id
                    or request.in_flight or getattr(self, "_mid_requests_in_flight", 0)
                    or getattr(self,"_action_in_progress",False)):
                if request.position and self.running and not self.safety.stopped.is_set():
                    self._position_recheck_due = True
                if request.orders and self.running and not self.safety.stopped.is_set():
                    self._orders_recheck_due = True
                if self.telemetry:
                    self.telemetry.count("maintenance_results_discarded")
                return False
            self._maintenance_last_error = result.error
            if result.error:
                # A transient background read failure is not a reason to stop
                # a healthy session holding inventory: block new exposure,
                # keep reduce-only exits possible, and retry at the cadence.
                self._maintenance_error_count = getattr(self, "_maintenance_error_count", 0) + 1
                if self._maintenance_error_count == 1:
                    self._maintenance_error_since = time.time()
                fail_maintenance = (self._maintenance_error_count
                                    >= MID_MAINTENANCE_FAIL_LIMIT)
                self.position_dirty = True
                self.safety.account_known = False
                # Retry at the normal 5 s / 10 s cadence with backoff, not on
                # every loop wake: a persistent outage must take ~3 cadence
                # intervals, not three fast round trips, to latch the stop.
                recheck_position = recheck_orders = False
            else:
                self._maintenance_error_count = 0
                has_position = result.position is not None and request.position
                has_orders = result.orders is not None and request.orders
                position_valid = (request.position_revision == self.position_revision
                                  and request.position_generation
                                  == getattr(self, "_position_read_generation", 0))
                orders_valid = (request.orders_fingerprint
                                == self._orders_fingerprint_locked())
                applied = False
                if has_position and position_valid:
                    position = result.position
                    old = self.position
                    old_entry = self.entry_price
                    self.position = position.quantity
                    self.entry_price = position.entry_price
                    if old!=self.position or old_entry!=self.entry_price:
                        self.position_revision+=1
                    self._inventory_basis_known=True
                    self._risk_basis_revision=self.position_revision
                    flat = abs(self.position) < MIN_SIZE / 2
                    starts_new = (not flat and abs(old) < MIN_SIZE / 2) or old * self.position < 0
                    self._reset_position_tracking_locked(flat=flat, starts_new=starts_new)
                    self._refresh_inventory_mark_locked()
                    self.position_dirty = False
                    self.safety.account_known = True
                    self._position_verified_at = result.position_started
                    self._position_read_generation = getattr(self, "_position_read_generation", 0) + 1
                    if result.spot_usdc is not None:
                        self.spot_usdc = result.spot_usdc
                    self.equity = position.perp_equity + self.spot_usdc
                    self._maintenance_equity_stale = bool(result.spot_error)
                    applied = True
                elif has_position:
                    recheck_position = True
                if has_orders and orders_valid:
                    owned = {}
                    for order in result.orders:
                        if not self.safety.owns({"oid": order.oid, "cloid": order.cloid}):
                            continue
                        old = self.orders.get(order.oid, {})
                        ro = old.get("ro")
                        if ro is None:
                            ro = order.reduce_only
                        owned[order.oid] = {"side": order.side, "px": order.price, "sz": order.size,
                                            "ro": ro, "ts": old.get("ts", time.time())}
                    self.orders = owned
                    self.queue_init = {oid: value for oid, value in self.queue_init.items() if oid in owned}
                    self.queue_ahead = {oid: value for oid, value in self.queue_ahead.items() if oid in owned}
                    self._orders_verified_at = result.orders_started
                    self._bump_execution_locked()
                    applied = True
                elif has_orders:
                    recheck_orders = True
                if applied:
                    self.last_intent = None
                    self.force_quote = True
                    if self.telemetry:
                        self.telemetry.count("maintenance_results_applied")
                    return True
            if recheck_position:
                self._position_recheck_due = True
            if recheck_orders:
                self._orders_recheck_due = True
            if self.telemetry:
                self.telemetry.count("maintenance_results_discarded")
        # Keep the existing interlock for a failed critical read, but only
        # after the bounded retry window. Never call it while holding the
        # trading-state lock or turn failed data into zeros.
        if fail_maintenance:
            self.safety.fail("Market maker maintenance verification failed repeatedly: "
                             + str(result.error))
        return False

    def _maintenance_tick(self):
        worker = getattr(self, "_maintenance", None)
        if worker is None:
            return
        with self.lock:
            if getattr(self,"_action_in_progress",False):
                return  # retain the bounded result slot until execution is idle
        if not worker.state()["alive"] and not self.safety.stopped.is_set():
            self.safety.fail("Market maker maintenance worker stopped unexpectedly")
            return
        result = worker.poll()
        if result is not None:
            self._apply_maintenance_result(result)
        now, wall_now = time.monotonic(), time.time()
        with self.lock:
            if not self.running or self.safety.stopped.is_set():
                return
            if getattr(self, "_mid_requests_in_flight", 0) or getattr(self,"_action_in_progress",False):
                return
            position_due = (now - getattr(self, "_maintenance_position_request_at", 0) >= SYNC_EVERY_SEC
                            or bool(getattr(self, "_position_recheck_due", False)))
            suspicious = bool(self.orders and wall_now - max(self.last_orderupdate_ts, self.last_quote_ts)
                              > STALE_ORDERUPDATE_SEC)
            # The generation fence replaces the old post-quote grace for
            # periodic reads. Otherwise continuous quoting postpones verification
            # forever, then blocks openings once its 10 s freshness expires.
            orders_due = (now - getattr(self, "_maintenance_orders_request_at", 0) >= RECONCILE_EVERY_SEC
                          or suspicious and wall_now-self.last_quote_ts >= ORDER_RECONCILE_GRACE_SEC
                          or bool(getattr(self, "_orders_recheck_due", False)))
            if not position_due and not orders_due:
                return
            request = MaintenanceRequest(self.safety.session_id,
                                         getattr(self, "_execution_revision", 0), False,
                                         position=position_due, orders=orders_due,
                                         position_revision=self.position_revision,
                                         position_generation=getattr(self, "_position_read_generation", 0),
                                         orders_fingerprint=self._orders_fingerprint_locked())
            if worker.request(request):
                if position_due:
                    self._maintenance_position_request_at = now
                    # A fill was just observed: this read clears position_dirty
                    # in ~one round-trip instead of waiting the 5 s cadence.
                    self._position_recheck_due = False
                if orders_due:
                    self._maintenance_orders_request_at = now
                    self._orders_recheck_due = False
                    if suspicious:
                        self.last_orderupdate_ts = wall_now

    def _maintenance_status(self):
        worker = getattr(self, "_maintenance", None)
        if worker is None:
            return {"enabled": False}
        with self.lock:
            now = time.monotonic()
            position_at = getattr(self, "_position_verified_at", 0)
            orders_at = getattr(self, "_orders_verified_at", 0)
            status = {"enabled": True,
                      "position_age_ms": (now-position_at)*1000 if position_at else None,
                      "orders_age_ms": (now-orders_at)*1000 if orders_at else None,
                      "opening_blocked": (not self._mid_opening_allowed_locked() or self.position_dirty
                                          or self.safety.stopped.is_set()),
                      "last_error": getattr(self, "_maintenance_last_error", ""),
                      "equity_stale": getattr(self, "_maintenance_equity_stale", False)}
        return {**status, **worker.state()}

    # ------------------------------------------------------------ account sync
    def sync_position(self, include_spot=True):
        try:
            started = time.time()
            read_started = time.monotonic()
            with self.lock:
                revision = self.position_revision
                execution_revision = getattr(self, "_execution_revision", 0)
            st = self.info.user_state(ACCOUNT_ADDRESS, dex=self.dex)
            if not isinstance(st, dict) or not isinstance(st.get("assetPositions"), list):
                raise ValueError("Invalid account response")
            spot_usdc = self.spot_usdc
            if include_spot:
                try:
                    spot = self.info.spot_user_state(ACCOUNT_ADDRESS)
                    spot_usdc = sum(max(0.0, float(b.get("total", 0) or 0) - float(b.get("hold", 0) or 0))
                                    for b in spot.get("balances", []) if b.get("coin") == "USDC")
                except Exception:
                    pass  # display-only collateral retains its last known value
            target = SYMBOL.split(":")[-1]
            with self.lock:
                if (self.last_fill_ts > started or revision != self.position_revision
                        or fast_mm() and execution_revision != getattr(self, "_execution_revision", 0)):
                    return False
                self._position_verified_at = read_started
                self._position_read_generation = getattr(self, "_position_read_generation", 0) + 1
                self._bump_execution_locked()
                perp_eq = float(st.get("marginSummary", {}).get("accountValue", 0) or 0)
                self.spot_usdc = spot_usdc
                self.equity = perp_eq + spot_usdc
                for entry in st.get("assetPositions", []):
                    pos = entry.get("position", {})
                    if pos.get("coin") in (SYMBOL, target):
                        old = self.position
                        old_entry = self.entry_price
                        self.position = finite(pos.get("szi", 0))
                        self.entry_price = finite(pos.get("entryPx", 0) or 0)
                        if old!=self.position or old_entry!=self.entry_price:
                            self.position_revision+=1
                        self._inventory_basis_known=True
                        self._risk_basis_revision=self.position_revision
                        flat = abs(self.position) < MIN_SIZE / 2
                        starts_new = (not flat and abs(old) < MIN_SIZE / 2) or old * self.position < 0
                        self._reset_position_tracking_locked(flat=flat, starts_new=starts_new)
                        self._refresh_inventory_mark_locked()
                        self.position_dirty = False
                        self.safety.account_known = True
                        return True
                if self.position!=0 or self.entry_price!=0:
                    self.position_revision+=1
                self.position = 0.0
                self.unrealized_pnl = 0.0
                self.entry_price = 0.0
                self._reset_position_tracking_locked(flat=True, starts_new=False)
                self._inventory_mark_known = True
                self._inventory_basis_known=True
                self._risk_basis_revision=self.position_revision
                self.position_dirty = False
                self.safety.account_known = True
                return True
        except Exception as exc:
            self.safety.fail(f"Position synchronization failed: {exc}")
            return False

    def _reconcile_reduce_only(self):
        """A definitively rejected close is not a fill or permission to reopen."""
        self._reconcile_execution_state()

    def _reconcile_missing_cancels(self, requests):
        """A cancel/fill race resolves only after the exact OIDs are absent."""
        self._reconcile_execution_state({int(req["oid"]) for req in requests})

    def _reconcile_execution_state(self, absent_oids=()):
        # One position read (spot equity is display-only and skipped on the
        # execution path) + the independent account snapshot. The second
        # user_state read is the cross-check that catches a fill landing
        # between the two reads.
        synced = self.sync_position(include_spot=False) if fast_mm() else self.sync_position()
        if not synced and self.safety.stopped.is_set():
            raise RuntimeError("Execution reconciliation: position is unconfirmed")
        with self.lock:
            revision = self.position_revision
        rows = self._cleanup_snapshot()
        symbol, position, orders = rows[0]
        if set(absent_oids) & {int(order["oid"]) for order in orders}:
            raise RuntimeError("Cancellation rejected: order still open")
        with self.lock:
            changed = (not synced or revision != self.position_revision or self.position_dirty
                       or abs(position - self.position) > MIN_SIZE / 2)
            if revision == self.position_revision:
                old = self.position
                self.position = position
                if old!=position:
                    self.position_revision+=1
                flat = abs(position) < MIN_SIZE / 2
                starts_new = (not flat and abs(old) < MIN_SIZE / 2) or old * position < 0
                self._reset_position_tracking_locked(flat=flat, starts_new=starts_new)
                if flat:
                    self.entry_price = 0.0
                elif starts_new:
                    # A sign flip has a new basis this snapshot cannot supply.
                    self.entry_price = 0.0
                self._inventory_basis_known=not changed
                self._risk_basis_revision=self.position_revision if not changed else -1
                self._refresh_inventory_mark_locked()
            if changed:
                # A normal fill can overtake either read. Keep the session
                # alive, but force the existing preflight to obtain a new
                # authoritative position before submitting another request.
                self.position_dirty = True
                self.safety.account_known = False
            owned = {}
            for order in orders:
                if self.safety.owns(order):
                    oid = int(order["oid"])
                    old = self.orders.get(oid, {})
                    owned[oid] = {"side": order["side"], "px": finite(order["limitPx"]),
                                  "sz": finite(order["sz"]), "ro": old.get("ro"),
                                  "ts": old.get("ts", time.time())}
            self.orders = owned
            self.last_intent = None
            self.force_quote = True
            self.last_action = "orders and position resynced after an already-resolved request"
        self.wake.set()

    def _position_checked_requests(self, requests, replacing=()):
        started=time.perf_counter_ns()
        try:return self._position_checked_requests_impl(requests,replacing)
        finally:
            if getattr(self,"telemetry",None):self.telemetry.observe("position_preflight",(time.perf_counter_ns()-started)/1e6)

    def _position_checked_requests_impl(self, requests, replacing=()):
        """Preflight exits against inventory and already resting closes.

        The MID hot path uses the background maintenance snapshot when it is
        fresh and the local fill basis is known: an exit is reduce-only, so a
        stale size can never open exposure. Stale/unknown state still performs
        the synchronous REST preflight before any send.
        """
        retry_after = getattr(self, "quote_retry_after", {})
        requests = [req for req in requests if req.get("reduce_only")
                    or time.time() >= retry_after.get("B" if req["is_buy"] else "A", 0)]
        if not requests:
            return [], None
        closing = any(req.get("reduce_only") for req in requests)
        now_mono = time.monotonic()
        with self.lock:
            local_known = (getattr(self, "_inventory_basis_known", True)
                           and getattr(self, "_inventory_mark_known", True))
            position_at = getattr(self, "_position_verified_at", 0.0)
            orders_at = getattr(self, "_orders_verified_at", 0.0)
            verified_fresh = (position_at > 0 and orders_at > 0
                              and now_mono - position_at <= SYNC_EVERY_SEC
                              and now_mono - orders_at <= RECONCILE_EVERY_SEC)
            skip_reads = bool(closing and fast_mm() and local_known and verified_fresh)
        if (closing or self.position_dirty) and not skip_reads:
            # MID exit sizing needs perp inventory, not a spot equity widget.
            # The periodic full synchronization still refreshes spot collateral.
            synced = self.sync_position(include_spot=False) if fast_mm() else self.sync_position()
            if not synced:
                return [], None
        with self.lock:
            if fast_mm() and not self._mid_opening_allowed_locked():
                requests = [req for req in requests if req.get("reduce_only")]
            revision, position = self.position_revision, self.position
            tracked = {oid: dict(row) for oid, row in self.orders.items()}
        remaining = abs(position)
        orders_started = None
        if closing:
            if skip_reads:
                # Local tracking + fresh verified snapshot: no network here.
                closing_side = "B" if position < 0 else "A"
                for oid, order in tracked.items():
                    if oid in replacing:
                        continue
                    if order.get("side") == closing_side:
                        remaining -= max(0.0, finite(order.get("sz", 0) or 0))
            else:
                try:
                    orders_started = time.monotonic()
                    orders = self.info.open_orders(ACCOUNT_ADDRESS, dex=self.dex)
                    if not isinstance(orders, list):
                        raise ValueError("Invalid open orders during reduce-only preflight")
                    for order in orders:
                        if self._coin_matches(order.get("coin")) and int(order["oid"]) not in replacing:
                            if order.get("side") not in {"B", "A"}:
                                raise ValueError("Invalid order side during reduce-only preflight")
                            if (order["side"] == "B") == (position < 0):
                                remaining -= max(0.0, finite(order["sz"]))
                except Exception as exc:
                    self.safety.fail(f"Reduce-only preflight failed: {exc}")
                    return [], None
        checked = []
        for req in requests:
            if req.get("reduce_only"):
                if abs(position) < MIN_SIZE / 2 or bool(req["is_buy"]) != (position < 0):
                    continue
                size = math.floor(max(0.0, min(finite(req["sz"]), remaining)) * 10 ** SZ_DECIMALS) / 10 ** SZ_DECIMALS
                if size < MIN_SIZE:
                    continue
                checked.append({**req, "sz": size})
                remaining -= size
            elif abs(position) < MIN_SIZE / 2:
                checked.append(req)
        with self.lock:
            if revision != self.position_revision:
                return [], None
            if self.position_dirty and not (closing and skip_reads and local_known):
                return [], None
            if orders_started is not None:
                self._orders_verified_at = orders_started
                self._bump_execution_locked()
            if not checked:
                self.last_intent = None
                self.force_quote = True
        return checked, revision

    def reconcile_orders(self, force=False):
        """Le snapshot REST des ordres ouverts est la seule vérité — les oids
        locaux peuvent contenir des fantômes (event WS raté)."""
        with self.lock:
            if not force and time.time() - self.last_quote_ts < ORDER_RECONCILE_GRACE_SEC:
                return
            revision = self.position_revision
            execution_revision = getattr(self, "_execution_revision", 0)
            tracked = {oid:dict(row) for oid,row in self.orders.items()}
        read_started = time.monotonic()
        try:
            oo = self.info.open_orders(ACCOUNT_ADDRESS, dex=self.dex)
            if not isinstance(oo, list):
                raise ValueError("Invalid order snapshot")
        except Exception as exc:
            self.safety.fail(f"Order synchronization failed: {exc}")
            return
        real: Dict[int, Dict[str, Any]] = {}
        for o in oo:
            if self._coin_matches(o.get("coin")):
                if fast_mm() and not self.safety.owns(o):
                    continue
                oid = int(o["oid"])
                known = tracked.get(oid, {})
                ro = known.get("ro")
                if ro is None:
                    raw_ro = o.get("reduceOnly")
                    ro = raw_ro if isinstance(raw_ro, bool) else None
                real[oid] = {
                    "side": "B" if o.get("side") == "B" else "A",
                    "px": float(o.get("limitPx", 0) or 0),
                    "sz": float(o.get("sz", 0) or 0),
                    # garder le flag RO et l'âge si on les connaissait déjà
                    "ro": ro,
                    "ts": known.get("ts", time.time()),
                }
        with self.lock:
            if fast_mm() and (revision != self.position_revision or tracked != self.orders
                    or execution_revision != getattr(self, "_execution_revision", 0)) :
                self.force_quote = True
                return
            self._orders_verified_at = read_started
            self._bump_execution_locked()
            if real != self.orders:
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
            with self._execution_lock():
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
                if ok:
                    self.orders.pop(oid, None)
                    self.queue_init.pop(oid, None)
                    self.queue_ahead.pop(oid, None)
            self.last_quote_ts = time.time()
            confirmed = len(statuses) == len(oids) and all(status == "success" for status in statuses)
            if confirmed:
                self.last_error = ""
        return confirmed

    def cancel_all(self) -> bool:
        with self.lock:
            oids = list(self.orders.keys())
        return self.cancel_oids(oids)

    @staticmethod
    def _is_post_only_error(err: str) -> bool:
        return post_only_rejection(err)

    def _reconcile_post_only(self, requests, action):
        if getattr(self,"telemetry",None):self.telemetry.count("post_only_rejections",len(requests))
        # The exchange definitively refused just these ALO requests: no fill is
        # possible, so inventory is unchanged. Verify the old order still
        # exists (a concurrent fill/cancel is possible), but do NOT poison
        # position_dirty or block both sides until a 5 s read.
        if action == "bulk_modify_orders_new":
            self.reconcile_orders(force=True)
            if self.safety.stopped.is_set():
                raise RuntimeError("Post-only replacement: order synchronization unconfirmed")
        else:
            with self.lock:
                self._orders_recheck_due = True
        self._defer_quote_requests(requests,
                                   "Post-only: book moved, recalculating the rejected side.",
                                   inventory_unchanged=True)

    def _reconcile_missing_modifies(self, modifications):
        if getattr(self,"telemetry",None):self.telemetry.count("missing_modify_races",len(modifications))
        missing = {int(item["oid"]) for item in modifications}
        self._reconcile_execution_state(missing)
        with self.lock:
            for oid in missing:
                self.queue_init.pop(oid, None)
                self.queue_ahead.pop(oid, None)
        self._defer_quote_requests([item["order"] for item in modifications],
                                   "Order already filled or cancelled: quote recalculated after sync.")

    def _reconcile_invalid_modifies(self, modifications):
        """Explicit reject: verify real state before a cancel/fresh-order fallback.

        Do not infer whether the old order survived. No execution is sent from
        this callback while the API lock is held; the next decision owns it.
        """
        self._reconcile_execution_state()
        with self.lock:
            invalid = getattr(self, "_invalid_modify_oids", set()) & self.orders.keys()
            invalid.update(int(item["oid"]) for item in modifications if int(item["oid"]) in self.orders)
            self._invalid_modify_oids = invalid
            self.quote_warning = "Modify refused: state verified; recalculating a fresh maker order."
            self.force_quote = True
        if getattr(self, "telemetry", None):
            self.telemetry.count("invalid_modify_reconciled", len(modifications))
        self.wake.set()

    def _defer_quote_requests(self, requests, warning, *, inventory_unchanged=False):
        with self.lock:
            self.quote_retry_after = getattr(self, "quote_retry_after", {"B": 0.0, "A": 0.0})
            self.quote_reject_count = getattr(self, "quote_reject_count", {"B": 0, "A": 0})
            for request in requests:
                side = "B" if request["is_buy"] else "A"
                count = min(4, self.quote_reject_count[side] + 1)
                self.quote_reject_count[side] = count
                # A normal ALO race must not park a MID side for seconds.
                if STRATEGY == "mid" and MID_ADAPTIVE:
                    delay = min(1.5, max(0.05, MID_POST_ONLY_BACKOFF) * 2 ** (count - 1))
                else:
                    delay = min(8.0, 2 ** (count - 1))
                self.quote_retry_after[side] = time.time() + delay
            self.quote_warning = warning
            if not inventory_unchanged:
                self.position_revision += 1
                self._bump_execution_locked()
                self.position_dirty = True
            self.force_quote = True
            self.last_intent = None
        self.wake.set()

    def _quote_side_accepted(self, request):
        if getattr(self,"telemetry",None):self.telemetry.count("accepted_quote_results")
        """Called under the state lock; do not clear the other side's warning."""
        side = "B" if request["is_buy"] else "A"
        if hasattr(self, "quote_reject_count"):
            self.quote_reject_count[side] = 0
            self.quote_retry_after[side] = 0.0
            if not any(self.quote_reject_count.values()):
                self.quote_warning = ""

    def place_quotes(self, reqs: List[Dict[str, Any]]) -> None:
        if not self.running or self.safety.stopped.is_set():
            return
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
                with self._execution_lock():
                    pending, revision = self._position_checked_requests(pending)
                    with self.lock:
                        # A fill can arrive after compute_intent(). Do not add
                        # exposure from a stale opening request in that case;
                        # a reduce-only exit with a known basis may proceed.
                        closing_only = all(bool(r.get("reduce_only")) for r in pending)
                        dirty_blocks = (self.position_dirty and not (
                            closing_only
                            and getattr(self, "_inventory_basis_known", False)
                            and getattr(self, "_inventory_mark_known", False)))
                        if revision != self.position_revision or dirty_blocks:
                            pending = []
                        if not pending:
                            self.force_quote = True
                            return
                        self.order_placements += len(pending)
                        self.order_requests += 1
                    self._submit_position_revision = revision
                    res = self.exchange.bulk_orders(pending)
            except SubmissionSkipped:
                if getattr(self,"telemetry",None):self.telemetry.count("local_submission_skips")
                self.force_quote = True
                return
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
                        self._quote_side_accepted(req)
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
                        self._quote_side_accepted(req)
                        self.position_revision += 1
                        self._bump_execution_locked()
                        self.position_dirty = True
                        self.force_quote = True
                        self.wake.set()
                    else:
                        err = str(status.get("error", status) if isinstance(status, dict) else status)
                        if post_only_rejection(err):
                            continue  # expected ALO race: warning/backoff was recorded by the guard
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
        False si un échec reste non réconcilié: l'appelant invalide last_intent et le cycle
        suivant repasse par le chemin cancel+place classique (anti-fantômes)."""
        if not self.running or self.safety.stopped.is_set():
            return False
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
            with self._execution_lock():
                checked, revision = self._position_checked_requests([r for _, r in pairs],
                                                                      replacing={oid for oid, _ in pairs})
                if len(checked) != len(pairs):
                    self.force_quote = True
                    self.wake.set()
                    return True  # known local deferral; preserve the existing order
                pairs = [(oid, req) for (oid, _), req in zip(pairs, checked)]
                for modification, req in zip(modifies, checked):
                    modification["order"]["sz"] = req["sz"]
                with self.lock:
                    closing_only = all(bool(r.get("reduce_only")) for r in checked)
                    dirty_blocks = (self.position_dirty and not (
                        closing_only
                        and getattr(self, "_inventory_basis_known", False)
                        and getattr(self, "_inventory_mark_known", False)))
                    if revision != self.position_revision or dirty_blocks:
                        self.force_quote = True
                        return False
                    self.order_modifications += len(pairs)
                    self.order_requests += 1
                self._submit_position_revision = revision
                res = self.exchange.bulk_modify_orders_new(modifies)
        except SubmissionSkipped:
            if getattr(self,"telemetry",None):self.telemetry.count("local_submission_skips")
            self.force_quote = True
            return True  # local deferral must not trigger a cancel/place fallback
        except Exception as exc:
            with self.lock:
                self.last_error = f"modify: {str(exc)[:100]}"
            return False
        if not isinstance(res, dict) or res.get("status") != "ok":
            with self.lock:
                self.last_error = f"modify: {str(res)[:100]}"
            return False
        for (_, request), modification in zip(pairs, modifies):
            request["limit_px"] = modification["order"]["limit_px"]
        statuses = res.get("response", {}).get("data", {}).get("statuses", [])
        ok_all = len(statuses) == len(pairs)
        with self.lock:
            self.last_quote_ts = time.time()
            self.last_error = ""   # action OK: purger le message collant
            for (oid, r), status in zip(pairs, statuses):
                if isinstance(status, dict) and "resting" in status:
                    self._quote_side_accepted(r)
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
                    self._quote_side_accepted(r)
                    self.orders.pop(oid, None)   # modifié puis rempli — userFills gère
                    self.position_revision += 1
                    self._bump_execution_locked()
                    self.position_dirty = True
                    self.force_quote = True
                    self.wake.set()
                else:
                    if isinstance(status, dict) and (post_only_rejection(status.get("error"))
                                                    or missing_modify_rejection(status.get("error"))
                                                    or invalid_modify_rejection(status.get("error"))):
                        continue  # reconciled and deferred; preserve the other side's queue
                    ok_all = False
                    self.last_error = str(status.get("error", status)
                                          if isinstance(status, dict) else status)[:100]
        return ok_all

    def flatten(self, reason: str):
        """Explicit manual Stop only: cancel all + reduce-only IOC close.

        Automatic risk/feed/session paths never call this. The passive-only
        execution gate rejects the close unless a manual-close scope is active.
        """
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
            with self.api_lock, self.safety.manual_close_scope():
                checked, revision = self._position_checked_requests([{"coin": SYMBOL, "is_buy": is_buy,
                    "sz": sz, "limit_px": px, "order_type": {"limit": {"tif": "Ioc"}}, "reduce_only": True}])
                with self.lock:
                    if not checked or revision != self.position_revision or self.position_dirty:
                        return
                request = checked[0]
                self.exchange.order(name=SYMBOL, is_buy=request["is_buy"], sz=request["sz"], limit_px=request["limit_px"],
                                    order_type=request["order_type"], reduce_only=True)
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
    @contextmanager
    def _execution_lock(self):
        started=time.perf_counter_ns()
        with self.api_lock:
            if getattr(self,"telemetry",None):self.telemetry.observe("api_lock_wait",(time.perf_counter_ns()-started)/1e6)
            held=time.perf_counter_ns()
            try:yield
            finally:
                if getattr(self,"telemetry",None):self.telemetry.observe("api_lock_hold",(time.perf_counter_ns()-held)/1e6)

    def _record_mid_response(self, name, received):
        metrics=getattr(self,"telemetry",None)
        if metrics and name in {"bulk_orders","bulk_modify_orders_new"}:
            if received and getattr(self,"_submit_event_ns",0):metrics.observe("event_to_ack",(time.perf_counter_ns()-self._submit_event_ns)/1e6)
            metrics.count("exchange_responses" if received else "transport_errors")

    def _final_ws_gate(self, payload):
        """Validate the exact signed action immediately before transport send.

        This runs on the signing thread, after the SDK has built and signed the
        payload. It must never mutate it. The checks repeat the passive-only and
        inventory fences because a fill, Stop or stale book can arrive between
        signing and the final send.
        """
        if not isinstance(payload, dict) or not isinstance(payload.get("action"), dict):
            raise SubmissionSkipped("Invalid signed action payload")
        action = payload["action"]
        kind = action.get("type")
        if not isinstance(kind, str):
            raise SubmissionSkipped("Invalid signed action type")
        cleanup = getattr(self.safety.local, "cleanup", False)
        manual_close = (self.safety.manual_stop_requested
                        and getattr(self.safety.local, "manual_close", False))
        if not cleanup and (self.safety.stopped.is_set() or not self.running
                            or os.path.exists(getattr(self, "_stop_file", ""))
                            or not supervisor_alive()):
            raise SubmissionSkipped("Stop/supervisor gate closed before send")
        if kind in {"cancel", "cancelByCloid", "updateLeverage"}:
            return
        if kind == "order":
            rows = action.get("orders")
        elif kind == "batchModify":
            rows = [item.get("order") if isinstance(item, dict) else None
                    for item in action.get("modifies", [])]
        else:
            raise SubmissionSkipped("Unsupported signed action type")
        if not isinstance(rows, list) or not rows:
            raise SubmissionSkipped("Signed action requires an explicit order batch")
        opening = False
        for row in rows:
            if not isinstance(row, dict):
                raise SubmissionSkipped("Invalid signed order")
            order_type = row.get("t")
            if (not isinstance(order_type,dict) or set(order_type)!={"limit"}
                    or not isinstance(row.get("r"),bool)):
                raise SubmissionSkipped("Signed order must have an explicit limit type and reduce-only flag")
            limit = order_type.get("limit") if isinstance(order_type, dict) else None
            tif = limit.get("tif") if isinstance(limit, dict) else None
            if (cleanup or self.safety.stopped.is_set()) and row["r"] is not True:
                raise SubmissionSkipped("Stopped session cannot create opening exposure")
            if tif == "Alo":
                opening = opening or row.get("r") is not True
                continue
            if manual_close and row.get("r") is True and tif in {"Ioc", "Gtc"}:
                continue
            raise SubmissionSkipped("Passive-only session blocks signed non-post-only order")
        if cleanup and manual_close:
            return  # the manual closer has its own independent position/book preflight
        with self.lock:
            inventory_unknown = (not getattr(self,"_inventory_basis_known",True)
                                 or not getattr(self,"_inventory_mark_known",True)
                                 or (self.position_dirty and opening))
            if inventory_unknown:
                raise SubmissionSkipped("Inventory state unknown before send")
            if getattr(self,"_submit_position_revision",None)!=self.position_revision:
                raise SubmissionSkipped("Inventory changed before send")
            if (getattr(self,"_submit_book_revision",None)!=getattr(self,"top_revision",0)
                    or getattr(self,"_submit_feed_generation",None)!=getattr(self,"_feed_generation",0)):
                raise SubmissionSkipped("Newer book superseded signed action")
            received=getattr(self,"book_received_ns",0)
            age=(time.perf_counter_ns()-received)/1e9 if received else math.inf
            if not math.isfinite(age) or not 0<=age<=DEADMAN_FEED_SEC:
                raise SubmissionSkipped("Book stale before WebSocket send")
            if opening and not self._mid_opening_allowed_locked():
                raise SubmissionSkipped("Account/order verification expired before WebSocket send")
            if opening:
                now=time.time()
                if (getattr(self,"_submit_risk_revision",0)!=getattr(self,"_risk_revision",0)
                        or abs(self.position)>=MIN_SIZE/2
                        or now<self.pause_until or now<self.hourly_pause_until or now<self.bank_until
                        or session_state_utc(SESSION_WINDOWS_UTC,now,OPEN_BLACKOUT_MIN)=="blackout"):
                    raise SubmissionSkipped("Risk/inventory gate changed before WebSocket send")
                if not math.isfinite(self.mid) or self.mid <= 0:
                    raise SubmissionSkipped("Invalid mid before WebSocket send")
                cap=round_sz(max(MIN_SIZE,ORDER_NOTIONAL_USD*self.size_mult/self.mid))
                if cap*self.mid<10:
                    cap=round_sz(math.ceil((10.5/self.mid)/MIN_SIZE)*MIN_SIZE)
                sizes={True:0.0,False:0.0}
                counts={True:0,False:0}
                for row in rows:
                    if row["r"] is False:
                        if not isinstance(row.get("b"),bool):
                            raise SubmissionSkipped("Invalid opening side")
                        size=finite(row.get("s"))
                        sizes[row["b"]]+=size
                        counts[row["b"]]+=1
                        if size<=0 or sizes[row["b"]]>cap+MIN_SIZE/2 or counts[row["b"]]>MAX_INV_ORDERS:
                            raise SubmissionSkipped("Opening exceeds current session size cap")

    def _prepare_mid_submission(self, name, args, kwargs):
        """Inside the signer lock, after REST preflight, before SDK signing."""
        self.safety.assert_allowed_action(name,args,kwargs)
        if name not in {"bulk_orders","bulk_modify_orders_new"}:return
        requests = args[0] if name == "bulk_orders" else [item["order"] for item in args[0]]
        if os.path.exists(getattr(self,"_stop_file","")) or not supervisor_alive():
            self.safety.stop()
            raise SubmissionSkipped("Stop/supervisor gate closed before signing")
        closing_only = all(bool(req.get("reduce_only")) for req in requests)
        with self.lock:
            # A validated fill basis is authoritative for a reduce-only exit
            # even while its background REST confirmation is pending. Openings
            # still require the full clean inventory state.
            inventory_unknown = (not getattr(self, "_inventory_basis_known", True)
                                 or not getattr(self, "_inventory_mark_known", True)
                                 or (self.position_dirty and not closing_only))
            if (not self.running or self.safety.stopped.is_set() or inventory_unknown
                    or self._submit_position_revision != self.position_revision):
                raise SubmissionSkipped("Inventory/stop changed before signing")
            if any(not req.get("reduce_only") for req in requests) and not self._mid_opening_allowed_locked():
                raise SubmissionSkipped("Market maker periodic verification is stale")
            bb,ba=self.best_bid,self.best_ask
            # Touch-only revision: depth-only L2 updates do not invalidate a
            # signed limit price between preparation and transport send.
            book_revision=getattr(self,"top_revision",0)
            feed_generation=getattr(self,"_feed_generation",0)
            risk_revision=getattr(self,"_risk_revision",0)
            received=getattr(self,"book_received_ns",0)
            age=(time.perf_counter_ns()-received)/1e9 if received else time.time()-getattr(self,"last_tick_ts",0)
        if not all(math.isfinite(v) for v in (bb,ba,age)) or bb<=0 or ba<=bb or age>DEADMAN_FEED_SEC:
            raise SubmissionSkipped("Book is stale or invalid before signing")
        tick=dyn_tick((bb+ba)/2)
        for req in requests:
            req["limit_px"]=self._execution_price(req["is_buy"],req["reduce_only"],bb,ba,tick)
        with self.lock:
            dirty_blocks = (self.position_dirty and not (
                closing_only
                and getattr(self, "_inventory_basis_known", False)
                and getattr(self, "_inventory_mark_known", False)))
            if (book_revision != getattr(self,"top_revision",0)
                    or self._submit_position_revision != self.position_revision
                    or dirty_blocks or self.safety.stopped.is_set()):
                raise SubmissionSkipped("Newer event superseded submission")
            if name == "bulk_modify_orders_new" and STRATEGY == "mid":
                for item in args[0]:
                    old = self.orders.get(int(item["oid"]))
                    req = item["order"]
                    if old and self._keep_mid_opening_locked(
                            "B" if req["is_buy"] else "A", old,
                            (req["limit_px"], req["sz"], req["reduce_only"]), tick,
                            urgent=getattr(self,"_urgent_quote",False)):
                        # Do not prune a guarded batch after its expected ACK
                        # count/CLOIDs were recorded. Recompute a fresh subset
                        # on the next actor wake, preserving the resting OIDs.
                        if getattr(self,"telemetry",None):
                            self.telemetry.count("mid_reprice_deferred_before_sign")
                        raise SubmissionSkipped("MID opening remains within repricing tolerance")
            if name == "bulk_modify_orders_new" and all(
                    (old := self.orders.get(int(item["oid"]))) and old.get("ro") == item["order"]["reduce_only"]
                    and abs(old["px"]-item["order"]["limit_px"])<tick/2
                    and abs(old["sz"]-item["order"]["sz"])<MIN_SIZE/2 for item in args[0]):
                raise SubmissionSkipped("Resting quote already matches latest target")
        self._submit_book_ns = received
        self._submit_book_revision = book_revision
        self._submit_feed_generation = feed_generation
        self._submit_risk_revision = risk_revision
        self._submit_event_ns = getattr(self, "_decision_event_ns", 0) or received
        if getattr(self,"telemetry",None):
            self.telemetry.observe("book_age_at_submit",age*1000)
            if self._submit_event_ns:
                self.telemetry.observe("event_to_submit", (time.perf_counter_ns()-self._submit_event_ns)/1e6)
            self.telemetry.count("submitted_batches")

    def _execution_price(self, is_buy, reduce_only, bb, ba, tick):
        """Final price at the wire: adaptive MID entries, touch-joined exits."""
        if STRATEGY != "mid":
            bid_px, ask_px = order_book_prices(bb, ba, tick)
            return bid_px if is_buy else ask_px
        now = time.time()
        if reduce_only:
            # The exit policy (hold-to-profit, bank window, protection) is the
            # same in adaptive and rollback modes; never send a second version.
            return self._mid_exit_price(is_buy, bb, ba, tick, now)
        if MID_ADAPTIVE:
            with self.lock:
                pos = self.position
            st = {"bb": bb, "ba": ba, "mid": (bb + ba) / 2.0, "tick": tick,
                  "pos": pos, "bid_ro": False, "ask_ro": False,
                  "bid_ok": True, "ask_ok": True,
                  "bid_size": 0.0, "ask_size": 0.0}
            targets = self._mid_targets(st, now)
            return targets["bid_px"] if is_buy else targets["ask_px"]
        # Rollback path: configured distance for entries.
        bid_px, ask_px = order_book_prices(bb, ba, tick)
        return bid_px if is_buy else ask_px

    def _fast_quote_gates(self):
        """Book + inventory/risk gates shared by BBO and MID (no OBI filters)."""
        now = time.time()
        with self.lock:
            if not getattr(self,"ws_ready",True):
                return None,"feed unavailable"
            bb, ba = self.best_bid, self.best_ask
            pos = self.position
            risk_pause = now < self.pause_until
            exits_only = now < self.hourly_pause_until or now < self.bank_until
            received=getattr(self,"book_received_ns",0)
            if received and (time.perf_counter_ns()-received)/1e9>DEADMAN_FEED_SEC:
                return None,"stale book"
            # A dirty fill basis or an unavailable mark cannot support new
            # exposure. Existing inventory may still rest reduce-only exits.
            state_unknown = (self.position_dirty
                             or not getattr(self, "_inventory_mark_known", True)
                             or not getattr(self, "_inventory_basis_known", True))
            side_pause = dict(getattr(self, "side_pause_until", {}) or {})
            last_side_fill = dict(getattr(self, "last_side_fill_ts", {}) or {})
            pause_b = now < float(side_pause.get("B", 0.0) or 0.0)
            pause_a = now < float(side_pause.get("A", 0.0) or 0.0)
            cool_b = now - float(last_side_fill.get("B", 0.0) or 0.0) < FILL_COOLDOWN_SEC
            cool_a = now - float(last_side_fill.get("A", 0.0) or 0.0) < FILL_COOLDOWN_SEC
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
        bid_ok = bid_ro or (flat and not exits_only and not state_unknown
                            and not pause_b and not cool_b
                            and pos + bid_size <= cap + eps)
        ask_ok = ask_ro or (flat and not exits_only and not state_unknown
                            and not pause_a and not cool_a
                            and -pos + ask_size <= cap + eps)
        return {
            "bb": bb, "ba": ba, "mid": mid, "tick": tick, "pos": pos,
            "bid_ro": bid_ro, "ask_ro": ask_ro,
            "bid_size": bid_size, "ask_size": ask_size,
            "bid_ok": bid_ok, "ask_ok": ask_ok,
        }, None

    # ------------------------------------------------ MID adaptive quoting
    def _mid_quote_context(self, now):
        """Snapshot the cached MID signal layer for one decision (no network)."""
        with self.lock:
            markout = getattr(self, "markout_bps", {}) or {}
            pause = getattr(self, "side_pause_until", {}) or {}
            fills = getattr(self, "last_side_fill_ts", {}) or {}
            markout = markout if isinstance(markout, dict) else {}
            pause = pause if isinstance(pause, dict) else {}
            fills = fills if isinstance(fills, dict) else {}
            return {
                "z": float(getattr(self, "_sig_z", 0.0) or 0.0),
                "alpha": float(getattr(self, "_sig_alpha", 0.0) or 0.0),
                "vol": float(getattr(self, "_sig_vol", 0.0) or 0.0),
                "touch": float(getattr(self, "_sig_touch", 0.0) or 0.0),
                "micro": float(getattr(self, "_sig_micro", 0.0) or 0.0),
                "micro_tilt": float(getattr(self, "_sig_micro_tilt", 0.0) or 0.0),
                "micro_ts": float(getattr(self, "_sig_micro_ts", 0.0) or 0.0),
                "adj_bid": float(getattr(self, "_sig_adj_bid", 0.0) or 0.0),
                "adj_ask": float(getattr(self, "_sig_adj_ask", 0.0) or 0.0),
                "adj_ts": float(getattr(self, "_sig_adj_ts", 0.0) or 0.0),
                "mom_f": float(getattr(self, "_sig_mom_fast", 0.0) or 0.0),
                "mom_s": float(getattr(self, "_sig_mom_slow", 0.0) or 0.0),
                "reg": getattr(self, "_reg_now", (None, 0)),
                "early": getattr(self, "_early_now", (None, 0)),
                "macro": getattr(self, "_macro", MACRO_NEUTRAL),
                "qf_b": getattr(self, "_qf_b", None),
                "qf_a": getattr(self, "_qf_a", None),
                "pos": self.position,
                "entry": self.entry_price,
                "pos_since": self.pos_since,
                "bank": now < self.bank_until,
                "risk_exit": bool(getattr(self, "_hard_exit_reason", "")),
                "mo_b": float(markout.get("B", 0.0) or 0.0),
                "mo_a": float(markout.get("A", 0.0) or 0.0),
                "pause_b": float(pause.get("B", 0.0) or 0.0),
                "pause_a": float(pause.get("A", 0.0) or 0.0),
                "fill_b": float(fills.get("B", 0.0) or 0.0),
                "fill_a": float(fills.get("A", 0.0) or 0.0),
                "entry_rate": getattr(self, "_observed_entry_rate", None),
                "maker_rate": getattr(self, "_observed_maker_rate", None),
            }

    @staticmethod
    def _queue_toxicity(qfrac, *, deep):
        """Estimated toxic fraction of the fills this quote would receive.

        A quote behind the touch can only be reached by a sweep, so it assumes
        the full measured adverse cost. At the touch, the share of the queue
        ahead of us is the estimate (floored so a fresh/unknown queue is not
        treated as risk-free).
        """
        if deep:
            return 1.0
        if qfrac is None:
            return 1.0
        try:
            q = max(0.0, min(1.0, float(qfrac)))
        except (TypeError, ValueError):
            return 1.0
        return max(0.20, q)

    def _mid_opening_prices(self, bb, ba, tick, ctx, order_sz, now):
        """Reservation-price targets before side gating.

        Quotes are clamped to the touch: a maker never improves the BBO here.
        The favoured side can join the touch; the adverse side is pushed back
        (or removed by the callers) depending on the signal.
        """
        mid = (bb + ba) / 2.0
        if not math.isfinite(mid) or mid <= 0:
            raise ValueError("Invalid MID reference price")
        spread_bps = (ba - bb) / mid * 1e4
        tick_bps = tick / mid * 1e4
        alpha_bps = ctx["alpha"]
        if getattr(self, "session_mode", "open") == "off":
            alpha_bps = max(-ALPHA_CAP_BPS, min(ALPHA_CAP_BPS,
                                               -MID_OFF_SESSION_MR_K * ctx["mom_s"]))
        tilt_bps = ctx["micro_tilt"]
        micro_ts = ctx["micro_ts"]
        if (not math.isfinite(tilt_bps) or not micro_ts
                or now - micro_ts > 0.5):
            tilt_bps = 0.0   # stale L2 snapshot: never misprice off an old level
        # Use the touch with our own size removed as the reference mid when it
        # is fresh; the raw BBO may simply be quoting ourselves.
        fair_mid = mid
        adj_bid, adj_ask, adj_ts = ctx["adj_bid"], ctx["adj_ask"], ctx["adj_ts"]
        if (adj_bid > 0 and adj_ask > adj_bid and adj_ts
                and now - adj_ts <= 0.5):
            fair_mid = (adj_bid + adj_ask) / 2.0
        macro = ctx["macro"]
        macro_bps = 0.0
        if macro is not None and getattr(macro, "ready", False):
            macro_bps = (MACRO_TREND_W * getattr(macro, "trend_dir", 0)
                         * getattr(macro, "trend_strength", 0.0)
                         + getattr(macro, "revert_bps", 0.0))
            macro_bps = max(-MACRO_MAX_BPS, min(MACRO_MAX_BPS, macro_bps))
        fair = fair_mid * (1.0 + (tilt_bps + alpha_bps + macro_bps) / 1e4)
        inv_units = ctx["pos"] / max(order_sz, MIN_SIZE)
        skew_bps = max(-MID_MAX_SKEW_BPS,
                       min(MID_MAX_SKEW_BPS, SKEW_BPS * inv_units))
        resa = fair - mid * (skew_bps / 1e4)
        half_bps = max(MID_OFFSET_BPS,
                       MAKER_FEE_BPS + self._eff_min_ev(),
                       SPREAD_CAPTURE_FRAC * spread_bps)
        if macro is not None and getattr(macro, "ready", False):
            half_bps = max(half_bps, spread_bps / 2.0 + getattr(macro, "widen_bps", 0.0))
        half_px = half_bps * mid / 1e4
        bid_t = min(resa - half_px, bb)
        ask_t = max(resa + half_px, ba)
        extra_bid = max(0, int(round((bb - bid_t) / tick)))
        extra_ask = max(0, int(round((ask_t - ba) / tick)))
        return extra_bid, extra_ask, spread_bps, tick_bps, alpha_bps, macro

    def _mid_exit_price(self, is_buy, bb, ba, tick, now, *, entry=None,
                        pos_since=None, bank=None, risk_exit=None,
                        entry_rate=None, maker_rate=None):
        """Maker-only exit target: bank/touch first, profit floor second.

        Closing a long means selling (is_buy=False); closing a short means
        buying (is_buy=True). During a bank/risk window the price is the most
        aggressive valid post-only price (one tick inside the spread), so the
        exit is at the front of the queue without paying the taker fee. In
        normal mode a routine exit rests at the fee-aware profit anchor and
        never follows the touch into a realised loss. Legacy loss-cuts: a
        normal exit joins the touch; the first 30 s can raise it to the
        fee-aware break-even floor, and the next 60 s to entry +/- profit
        ticks.
        """
        if entry is None:
            with self.lock:
                entry = self.entry_price
                pos_since = self.pos_since
                bank = now < self.bank_until
                risk_exit = bool(getattr(self, "_hard_exit_reason", ""))
                entry_rate = getattr(self, "_observed_entry_rate", None)
                maker_rate = getattr(self, "_observed_maker_rate", None)
        if ba <= bb or bb <= 0:
            raise ValueError("Invalid book for MID exit")
        if is_buy:
            aggressive = px_down(max(tick, ba - tick), tick)
            touch = px_down(bb, tick)
        else:
            aggressive = px_up(bb + tick, tick)
            touch = px_up(ba, tick)
        if risk_exit or bank:
            return aggressive
        if entry is None or entry <= 0 or not pos_since:
            return touch
        if not MID_LOSS_CUTS_ENABLED:
            # Normal mode: never follow the touch into a realised loss. Rest
            # the reduce-only exit at break-even (two maker fees + one tick)
            # and wait for the market to come back.
            offset = max(PROFIT_TICKS * tick,
                         entry * (2.0 * MAKER_FEE_BPS) / 1e4 + tick)
            anchor = (px_down(entry - offset, tick) if is_buy
                      else px_up(entry + offset, tick))
            return min(touch, anchor) if is_buy else max(touch, anchor)
        age = now - pos_since
        if 0 <= age < MID_EXIT_PROTECT_SEC and MID_EXIT_PROTECTION:
            protected = exit_price(touch, is_buy, entry, tick,
                                             entry_rate, maker_rate)
            if protected is not None:
                return protected
        elif 0 <= age <= UNWIND_ANCHOR_MAX_SEC:
            # Fee-aware profit anchor: two maker fees plus one tick, never
            # just two ticks (which is below the round-trip cost on SP500).
            offset = max(PROFIT_TICKS * tick,
                         entry * (2.0 * MAKER_FEE_BPS) / 1e4 + tick)
            anchor = (px_down(entry - offset, tick) if is_buy
                      else px_up(entry + offset, tick))
            return min(touch, anchor) if is_buy else max(touch, anchor)
        return touch

    def _mid_targets(self, st, now):
        """Adaptive MID bid/ask targets, sizes and gate flags."""
        bb, ba, tick = st["bb"], st["ba"], st["tick"]
        if not MID_ADAPTIVE:
            bid_px, ask_px = order_book_prices(bb, ba, tick)
            return {"bid_px": bid_px, "ask_px": ask_px,
                    "bid_ok": st["bid_ok"], "ask_ok": st["ask_ok"],
                    "tag": f"MID | mid +/- {MID_OFFSET_BPS:g}bps"}
        ctx = self._mid_quote_context(now)
        size_mult = float(getattr(self, "size_mult", 1.0) or 1.0)
        order_sz = round_sz(max(MIN_SIZE, ORDER_NOTIONAL_USD * size_mult / st["mid"]))
        if order_sz * st["mid"] < 10.0:
            order_sz = round_sz(math.ceil((10.5 / st["mid"]) / MIN_SIZE) * MIN_SIZE)
        extra_bid, extra_ask, spread_bps, tick_bps, alpha_bps, macro = \
            self._mid_opening_prices(bb, ba, tick, ctx, order_sz, now)
        # Bound the reservation offset in economic units: a strong alpha or
        # macro shift must not park a side arbitrarily far behind the touch.
        max_extra_ticks = max(0, int((MID_MAX_QUOTE_WIDTH_BPS - spread_bps / 2.0)
                                     / max(tick_bps, 1e-9)))
        extra_bid = min(extra_bid, max_extra_ticks)
        extra_ask = min(extra_ask, max_extra_ticks)
        quote_bid, quote_ask = st["bid_ok"], st["ask_ok"]
        bid_is_opening = st["bid_ro"] is False
        ask_is_opening = st["ask_ro"] is False
        tag = f"MID | z{ctx['z']:+.1f} a{alpha_bps:+.1f}"
        z = ctx["z"]
        if bid_is_opening and quote_bid and z <= -ZSCORE_PULL:
            quote_bid = False
            tag += " | bid z-"
        if ask_is_opening and quote_ask and z >= ZSCORE_PULL:
            quote_ask = False
            tag += " | ask z+"
        reg, reg_i = ctx["reg"]
        if reg:
            # Directional, like the early-warning layer: do not sell into a
            # pump / buy into a dump. The aligned side may keep quoting; only
            # an extreme event (intensity 3) pulls both sides.
            if reg == "PUMP" and ask_is_opening and quote_ask:
                quote_ask = False
                tag += f" | {reg}{reg_i} ask"
            elif reg == "DUMP" and bid_is_opening and quote_bid:
                quote_bid = False
                tag += f" | {reg}{reg_i} bid"
            if reg_i >= 3:
                if bid_is_opening and quote_bid:
                    quote_bid = False
                if ask_is_opening and quote_ask:
                    quote_ask = False
                tag += " | panic"
        else:
            early, early_score = ctx["early"]
            if early == "DUMP" and bid_is_opening and quote_bid:
                quote_bid = False
                tag += f" | earlyD{early_score}"
            elif early == "PUMP" and ask_is_opening and quote_ask:
                quote_ask = False
                tag += f" | earlyU{early_score}"
        mom_gate = MOM_GATE_BPS * (2.0 if getattr(self, "session_mode", "open") == "off" else 1.0)
        mom_f, mom_s = ctx["mom_f"], ctx["mom_s"]
        if (bid_is_opening and quote_bid and
                (mom_f <= -mom_gate
                 or (mom_f <= -0.6 * mom_gate and mom_s <= -mom_gate))):
            quote_bid = False
            tag += " | momD"
        if (ask_is_opening and quote_ask and
                (mom_f >= mom_gate
                 or (mom_f >= 0.6 * mom_gate and mom_s >= mom_gate))):
            quote_ask = False
            tag += " | momU"
        if bid_is_opening and quote_bid and (
                now < ctx["pause_b"] or now - ctx["fill_b"] < FILL_COOLDOWN_SEC):
            quote_bid = False
            tag += " | B pause"
        if ask_is_opening and quote_ask and (
                now < ctx["pause_a"] or now - ctx["fill_a"] < FILL_COOLDOWN_SEC):
            quote_ask = False
            tag += " | A pause"
        mo_b, mo_a = ctx["mo_b"], ctx["mo_a"]
        # The EV gate already charges the adverse floor from the first fill.
        # The side probation only acts on *evidence* beyond that floor; a
        # zero/no-sample EWMA must not widen an otherwise healthy quote.
        mo_soft = -MID_ADVERSE_FLOOR_BPS
        mo_hard = -2.0 * MID_ADVERSE_FLOOR_BPS
        if bid_is_opening and quote_bid:
            if mo_b <= mo_hard:
                quote_bid = False
                tag += " | moB-"
            elif mo_b <= mo_soft:
                extra_bid += MARKOUT_EXTRA_TICKS
                tag += " | moB~"
        if ask_is_opening and quote_ask:
            if mo_a <= mo_hard:
                quote_ask = False
                tag += " | moA-"
            elif mo_a <= mo_soft:
                extra_ask += MARKOUT_EXTRA_TICKS
                tag += " | moA~"
        # Deep quotes fill mainly via sweeps: charge the full measured adverse
        # cost, not the front-of-queue discount. This used to make the most
        # toxic fills look cheap.
        p_toxic_b = self._queue_toxicity(ctx["qf_b"], deep=extra_bid > 0)
        p_toxic_a = self._queue_toxicity(ctx["qf_a"], deep=extra_ask > 0)
        touch = ctx["touch"]
        adv_touch_bid = TOUCH_ADVERSE_BPS * max(0.0, -touch)
        adv_touch_ask = TOUCH_ADVERSE_BPS * max(0.0, touch)
        adv_trend_bid = adv_trend_ask = 0.0
        if (macro is not None and getattr(macro, "ready", False)
                and getattr(macro, "trend_dir", 0)):
            trend_cost = MACRO_TREND_ADVERSE_BPS * getattr(macro, "trend_strength", 0.0)
            if macro.trend_dir < 0:
                adv_trend_bid = trend_cost
            else:
                adv_trend_ask = trend_cost
        rt_target = MID_EDGE_TARGET_BPS + (self._eff_min_ev() - MIN_EV_BPS)
        exit_capture = spread_bps / 2.0
        if bid_is_opening and quote_bid:
            adverse = (max(MID_ADVERSE_FLOOR_BPS, max(0.0, -mo_b))
                       + adv_touch_bid + adv_trend_bid)
            gross = spread_bps / 2.0 + extra_bid * tick_bps + exit_capture
            expected = gross - (2.0 * MAKER_FEE_BPS + rt_target
                                + adverse * p_toxic_b)
            if expected < 0:
                add = max(1, math.ceil(-expected / max(tick_bps, 1e-9)))
                width = spread_bps / 2.0 + (extra_bid + add) * tick_bps
                if width > MID_MAX_QUOTE_WIDTH_BPS:
                    quote_bid = False
                    tag += " | EV-bid"
                else:
                    extra_bid += add
                    tag += f" | EV+{add}"
        if ask_is_opening and quote_ask:
            adverse = (max(MID_ADVERSE_FLOOR_BPS, max(0.0, -mo_a))
                       + adv_touch_ask + adv_trend_ask)
            gross = spread_bps / 2.0 + extra_ask * tick_bps + exit_capture
            expected = gross - (2.0 * MAKER_FEE_BPS + rt_target
                                + adverse * p_toxic_a)
            if expected < 0:
                add = max(1, math.ceil(-expected / max(tick_bps, 1e-9)))
                width = spread_bps / 2.0 + (extra_ask + add) * tick_bps
                if width > MID_MAX_QUOTE_WIDTH_BPS:
                    quote_ask = False
                    tag += " | EV-ask"
                else:
                    extra_ask += add
                    tag += f" | EV+{add}"
        # Macro widening is already folded into half_bps; only short-term vol
        # may add ticks here (do not count the same widening twice).
        widen_bps = ctx["vol"] * VOL_WIDEN_MULT
        vol_extra = int(min(4.0, max(0.0, (widen_bps - spread_bps / 2.0)
                                   / max(tick_bps, 1e-9))))
        if vol_extra > 0:
            if bid_is_opening:
                extra_bid += vol_extra
            if ask_is_opening:
                extra_ask += vol_extra
            tag += f" | vol+{vol_extra}"
        extra_bid = min(extra_bid, max_extra_ticks)
        extra_ask = min(extra_ask, max_extra_ticks)
        bid_px = px_down(bb - extra_bid * tick, tick)
        ask_px = px_up(ba + extra_ask * tick, tick)
        pos = st["pos"]
        eps = MIN_SIZE / 2.0
        if pos >= eps:
            quote_ask = True
            ask_px = self._mid_exit_price(
                False, bb, ba, tick, now, entry=ctx["entry"],
                pos_since=ctx["pos_since"], bank=ctx["bank"],
                risk_exit=ctx["risk_exit"], entry_rate=ctx["entry_rate"],
                maker_rate=ctx["maker_rate"])
            tag += " | exit"
        elif pos <= -eps:
            quote_bid = True
            bid_px = self._mid_exit_price(
                True, bb, ba, tick, now, entry=ctx["entry"],
                pos_since=ctx["pos_since"], bank=ctx["bank"],
                risk_exit=ctx["risk_exit"], entry_rate=ctx["entry_rate"],
                maker_rate=ctx["maker_rate"])
            tag += " | exit"
        # Defensive clamps: a post-only quote must never cross the book.
        if quote_bid and bid_px >= ba:
            bid_px = px_down(max(tick, ba - tick), tick)
        if quote_ask and ask_px <= bb:
            ask_px = px_up(bb + tick, tick)
        return {"bid_px": bid_px, "ask_px": ask_px,
                "bid_ok": bool(quote_bid and st["bid_ok"]),
                "ask_ok": bool(quote_ask and st["ask_ok"]), "tag": tag}

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
        """Adaptive MID: reservation price for entries, touch-joined exits."""
        st, err = self._fast_quote_gates()
        if st is None:
            return None, None, f"MID | {err}"
        try:
            targets = self._mid_targets(st, time.time())
        except (ValueError, OverflowError) as exc:
            return None, None, f"MID | {exc}"
        bid = ((targets["bid_px"], st["bid_size"], st["bid_ro"])
               if targets["bid_ok"] and st["bid_size"] >= MIN_SIZE else None)
        ask = ((targets["ask_px"], st["ask_size"], st["ask_ro"])
               if targets["ask_ok"] and st["ask_size"] >= MIN_SIZE else None)
        return bid, ask, targets["tag"]

    def compute_intent(self):
        if STRATEGY == "mid":
            return self.compute_mid_intent()
        if STRATEGY == "bbo":
            return self.compute_bbo_intent()
        raise ValueError(f"Unsupported market making mode: {STRATEGY}")

    def _keep_mid_opening_locked(self, side, old, want, tick, urgent=False):
        """Retain queue priority for small MID opening-price drift only.

        Caller holds self.lock. Inventory, sizes, RO conversions, exits and
        protective pulls never wait for this price-only hysteresis.
        """
        if (STRATEGY != "mid" or urgent or getattr(self,"_urgent_quote",False)
                or getattr(self,"_hard_exit_reason","") or self.position_dirty
                or abs(self.position) >= MIN_SIZE / 2 or want[2] is not False
                or old.get("ro") is not False or old.get("side") != side
                or abs(old["sz"] - want[1]) >= MIN_SIZE / 2):
            return False
        px, mid = old["px"], self.mid
        if (side == "B" and (px >= mid or px >= self.best_ask)
                or side == "A" and (px <= mid or px <= self.best_bid)):
            return False
        threshold = mid_reprice_ticks(mid, tick) * tick
        return abs(px - want[0]) + tick * 1e-7 < threshold

    def _keep_mid_exit_locked(self, side, old, want, tick, urgent, now):
        """Small MID exit drift keeps its queue slot; bank/stop is immediate.

        Exits are risk-reducing, so their hysteresis is shorter than an
        opening quote's. An urgent window (fill/TP/stop) always reprices.
        """
        if (STRATEGY != "mid" or not MID_ADAPTIVE or urgent
                or want[2] is not True or old.get("ro") is not True
                or old.get("side") != side
                or abs(old["sz"] - want[1]) >= MIN_SIZE / 2):
            return False
        mid = self.mid
        if not math.isfinite(mid) or mid <= 0 or tick <= 0:
            return False
        age = now - float(old.get("ts", 0.0) or 0.0)
        if side == "A":
            if self.best_bid > 0 and old["px"] <= self.best_bid:
                return False   # stale/crossing ask: reprice to a valid maker level
            if want[0] + tick * 1e-9 >= old["px"]:
                return True   # target moved up: the resting ask is the best price
            adverse_delta = (old["px"] - want[0]) / mid * 1e4
        else:
            if self.best_ask > 0 and old["px"] >= self.best_ask:
                return False   # stale/crossing bid: reprice to a valid maker level
            if want[0] - tick * 1e-9 <= old["px"]:
                return True   # target moved down: the resting bid is the best price
            adverse_delta = (want[0] - old["px"]) / mid * 1e4
        return adverse_delta < MID_EXIT_REPRICE_TOL_BPS and age < MID_EXIT_MIN_REST_SEC

    def _mid_quote_status(self):
        """Adaptive opening targets versus resting quotes; no mutation/network."""
        if STRATEGY != "mid":
            return None
        st, _error = self._fast_quote_gates()
        if st is None:
            return None
        try:
            targets = self._mid_targets(st, time.time())
            mid, tick = st["mid"], st["tick"]
            ticks = mid_reprice_ticks(mid, tick)
            with self.lock:
                buys = [row["px"] for row in self.orders.values() if row.get("side") == "B"]
                sells = [row["px"] for row in self.orders.values() if row.get("side") == "A"]
            bid, ask = targets["bid_px"], targets["ask_px"]
            return {"configured_offset_bps": MID_OFFSET_BPS,
                    "adaptive": bool(MID_ADAPTIVE),
                    "target_bid": bid, "target_ask": ask,
                    "target_bid_distance_bps": (mid - bid) / mid * 10000,
                    "target_ask_distance_bps": (ask - mid) / mid * 10000,
                    "resting_bid": max(buys) if buys else None,
                    "resting_ask": min(sells) if sells else None,
                    "tick_bps": tick / mid * 10000, "tick_size": tick,
                    "reprice_threshold_ticks": ticks,
                    "reprice_threshold_bps": ticks * tick / mid * 10000,
                    "reprice_fraction": MID_REPRICE_FRACTION,
                    "reprice_applies_to": "opening_price_only",
                    "gate": targets["tag"]}
        except (ValueError, OverflowError):
            return None

    def apply_intent(self):
        """Shared BBO/MID execution: change only a different price/size/flag."""
        now = time.time()
        decision_ns = time.perf_counter_ns()
        with self.lock:
            revision = getattr(self, "book_revision", 0)
            received = getattr(self, "book_received_ns", 0)
            fresh_event = revision != getattr(self, "_last_decision_book_revision", 0)
            fill_ns = getattr(self, "_last_fill_received_ns", 0)
            fresh_fill = fill_ns > getattr(self, "_last_decision_fill_ns", 0)
            self._decision_event_ns = (max(received if fresh_event else 0, fill_ns if fresh_fill else 0)
                                       or getattr(self, "_pending_quote_event_ns", 0) or decision_ns)
        bid, ask, tag = self.compute_intent()
        if getattr(self,"telemetry",None):
            self.telemetry.observe("decision",(time.perf_counter_ns()-decision_ns)/1e6)
            self.telemetry.count("decisions")
            if fresh_event and received:
                self.telemetry.observe("event_queue_delay", (decision_ns-received)/1e6)
                self.telemetry.observe("event_to_decision", (time.perf_counter_ns()-received)/1e6)
                self.telemetry.count("coalesced_book_events", max(0,revision-getattr(self,"_last_decision_book_revision",0)-1))
            if fresh_fill:
                self.telemetry.observe("fill_to_decision", (decision_ns-fill_ns)/1e6)
            if received:
                self.telemetry.observe("book_age_at_decision", (decision_ns-received)/1e6)
        self._last_decision_book_revision = revision
        self._last_decision_fill_ns = fill_ns
        intent = (bid, ask)
        tick = dyn_tick(self.mid if self.mid > 0 else 1.0)
        with self.lock:
            resting_by_side = {
                side: {oid: dict(d) for oid, d in self.orders.items() if d["side"] == side}
                for side in ("B", "A")
            }
            urgent = getattr(self, "_urgent_quote", False)
        to_cancel, place_reqs, modify_pairs = [], [], []
        allow_modify = now >= getattr(self, "_modify_block_until", 0.0)
        for side, want in zip(("B", "A"), intent):
            resting = resting_by_side[side]
            if fast_mm() and want is not None and not want[2]:
                with self.lock:
                    if not self._mid_opening_allowed_locked():
                        continue  # preserve resting priority; never increase stale exposure
            if want is not None and not want[2] and now < getattr(self, "quote_retry_after", {}).get(side, 0):
                continue  # keep the accepted resting quote while only this side backs off
            if want is None:
                # A stale/dead book must never cancel the only reduce-only
                # exit: that would leave the position naked. Opening quotes
                # are still pulled. The maintenance read reconciles later.
                keep_ro = {oid for oid, row in resting.items() if row.get("ro") is True}
                to_cancel.extend(oid for oid in resting if oid not in keep_ro)
                if keep_ro:
                    continue
                urgent = urgent or bool(resting)  # protective pull is never coalesced
                continue
            if len(resting) == 1:
                oid, old = next(iter(resting.items()))
                if (abs(old["px"] - want[0]) < tick / 2.0
                        and abs(old["sz"] - want[1]) < MIN_SIZE / 2.0
                        and old.get("ro") == want[2]):
                    continue
                with self.lock:
                    if want[2] is True:
                        keep = self._keep_mid_exit_locked(side, old, want, tick, urgent, now)
                    else:
                        keep = self._keep_mid_opening_locked(side, old, want, tick, urgent)
                if keep:
                    if getattr(self,"telemetry",None):
                        self.telemetry.count("mid_exit_price_holds" if want[2] else
                                             "mid_opening_price_holds")
                    continue
            req = {"coin": SYMBOL, "is_buy": side == "B", "sz": want[1],
                   "limit_px": want[0], "order_type": {"limit": {"tif": "Alo"}},
                   "reduce_only": want[2]}
            # A known session opening can become its exit via one batchModify.
            # The existing RO preflight still verifies inventory, other closes,
            # and excludes just this old OID. Never guess an unknown RO flag.
            compatible = len(resting) == 1 and (
                next(iter(resting.values())).get("ro") == want[2]
                or want[2] and next(iter(resting.values())).get("ro") is False
                and self.safety.owns({"oid": next(iter(resting))}))
            invalid_old = bool(set(resting) & getattr(self, "_invalid_modify_oids", set()))
            if compatible and allow_modify and not invalid_old:
                modify_pairs.append((next(iter(resting)), req))
            else:
                to_cancel.extend(resting)
                place_reqs.append(req)
                urgent = urgent or invalid_old  # pull a verified rejected replacement promptly
        self.last_action = tag
        self._quote_due_at = 0.0
        if not (to_cancel or place_reqs or modify_pairs):
            self._pending_quote_event_ns = 0
            return
        coalesce_remaining = ACTION_COALESCE_SEC - (now-self.last_action_ts)
        if coalesce_remaining > 0 and not urgent:
            self._quote_due_at = time.monotonic() + coalesce_remaining
            self._pending_quote_event_ns = self._decision_event_ns
            if getattr(self, "telemetry", None):
                self.telemetry.observe("action_coalesce_wait", coalesce_remaining*1000)
            return
        with self.lock:
            self._pending_quote_event_ns = 0
            self._urgent_quote = False
            self.force_quote = False
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
        protect = int(getattr(self, "protect", 0) or 0)
        return MIN_EV_BPS + (0.05 if protect >= 1 else 0.0) \
                          + (0.05 if protect >= 2 else 0.0)

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
        # Position-derived decisions and fill resets share the same lock. No
        # network request or exchange action is performed while holding it.
        with self.lock:
            before=(self.pause_until,self.hourly_pause_until,self.bank_until,self.size_mult,self.session_mode if hasattr(self,"session_mode") else "")
            self._risk_state_checks_locked()
            after=(self.pause_until,self.hourly_pause_until,self.bank_until,self.size_mult,self.session_mode if hasattr(self,"session_mode") else "")
            if before!=after:
                self._risk_revision=getattr(self,"_risk_revision",0)+1
        self._check_feed_health()

    def _risk_state_checks_locked(self):
        now = time.time()
        pos = self.position
        mid = self.mid
        pos_since = self.pos_since
        vol = self._sig_vol       # signaux cachés (1×/cycle), cohérents
        z = self._sig_z           # avec compute_intent du même cycle
        reg, reg_i = self._reg_now
        early_side_c, early_score_c = self._early_now
        # The marker is a current-book liquidation value, not the last REST
        # uPnL. Unknown is not zero: it blocks new exposure, but a recent
        # known mark is still the best available basis for stop / TP /
        # session-loss decisions while the book is briefly unavailable.
        mark = self._refresh_inventory_mark_locked()
        mark_known = mark is not None
        notional = abs(pos) * mid
        if mark_known:
            upnl = mark.unrealized_pnl
            self._last_mark_upnl = upnl
            self._last_mark_bps = (upnl / notional * 10000.0) if notional > 0 else 0.0
            self._last_mark_at = now
            mark_value_known = True
        else:
            stale_ok = (notional > 0
                        and getattr(self, "_last_mark_at", 0.0) > 0
                        and now - self._last_mark_at <= DEADMAN_FEED_SEC * 2)
            upnl = getattr(self, "_last_mark_upnl", 0.0) if stale_ok else 0.0
            self._last_mark_bps = getattr(self, "_last_mark_bps", 0.0) if stale_ok else 0.0
            mark_value_known = stale_ok
        upnl_bps = (self._last_mark_bps if mark_value_known else 0.0)

        # --- D. Protection du PnL de session: resserrement, JAMAIS d'arrêt.
        net = self.session_pnl + upnl - self.total_fees
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
        if mark_value_known:
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
        elif mark_value_known:
            self.upnl_peak_bps = max(self.upnl_peak_bps, upnl_bps)
            z_opposed = (pos > 0 and z <= -1.0) or (pos < 0 and z >= 1.0)
            # In normal (hold-to-profit) mode a retrace that has gone negative
            # must not force a loss-cutting exit; keep the profit anchor.
            tp_may_exit = MID_LOSS_CUTS_ENABLED or upnl_bps > 0
            if self.upnl_peak_bps >= TP_MIN_BPS and tp_may_exit and (
                    upnl_bps <= self.upnl_peak_bps * (1.0 - TP_RETRACE) or z_opposed):
                if now >= self.bank_until:
                    self.bank_until = now + BANK_ALO_SEC
                    self.last_action = f"bank-alo tp {upnl_bps:+.1f}/{self.upnl_peak_bps:.1f}bps"
                    self.force_quote = True
                    self._urgent_quote = True

        # --- C. Pump/dump CONTRE la position: sortie bank ALO immédiate
        # (le hard-stop vol-scalé ci-dessous reste le filet si ça creuse).
        # Normal mode keeps the profit anchor and does not realise the spike.
        if (MID_LOSS_CUTS_ENABLED and mark_value_known and reg and notional > 0
                and reg_i >= 2):
            against = (pos > 0 and reg == "DUMP") or (pos < 0 and reg == "PUMP")
            if against and now >= self.bank_until:
                self.bank_until = now + BANK_ALO_SEC
                self.last_action = f"bank-alo cut {reg}{reg_i}"
                self.force_quote = True
                self._urgent_quote = True
        # EARLY WARNING contre la position: sortir AVANT le move, en maker.
        if (MID_LOSS_CUTS_ENABLED and mark_value_known and not reg
                and notional > 0):
            early_side, early_score = early_side_c, early_score_c
            if early_side:
                against_early = (pos > 0 and early_side == "DUMP") or \
                                (pos < 0 and early_side == "PUMP")
                if against_early and now >= self.bank_until:
                    self.bank_until = now + BANK_ALO_SEC
                    self.last_action = f"bank-alo early {early_side}{early_score}"
                    self.force_quote = True
                    self._urgent_quote = True
        # Fin de régime aligné: le move s'essouffle → banker le ride (maker).
        if mark_value_known and self._last_regime and not reg and notional > 0:
            aligned = (pos > 0 and self._last_regime == "PUMP") or \
                      (pos < 0 and self._last_regime == "DUMP")
            if aligned and upnl_bps >= 1.0 and now >= self.bank_until:
                self.bank_until = now + BANK_ALO_SEC
                self.last_action = f"bank-alo {self._last_regime}-end {upnl_bps:+.1f}bps"
                self.force_quote = True
                self._urgent_quote = True
        self._last_regime = reg

        # Routine and runaway exits stay post-only: the bank window is renewed,
        # never converted to an autonomous taker order.
        if mark_value_known:
            self._manage_session_exit(now, pos, upnl, upnl_bps, pos_since, vol)

        if mark_value_known and net <= -MAX_SESSION_LOSS_USD:
            # Exits-only wind-down: do NOT stop with an open position. Keep
            # renewing the aggressive passive exit until flat, then stop. The
            # bounded deadline ends the process; cleanup leaves the exit
            # resting on the exchange rather than cancelling it.
            if not getattr(self, "_session_halt", False):
                self._session_halt = True
                self._session_halt_at = now
                self.hourly_pause_until = max(self.hourly_pause_until, now + 3600.0)
                self.bank_until = max(self.bank_until, now + BANK_ALO_SEC)
                self.last_action = f"SESSION LOSS LIMIT ${net:+.2f} — exits only"
                self.force_quote = True
                self._urgent_quote = True
            if abs(pos) < MIN_SIZE / 2:
                self.running = False
            elif now - getattr(self, "_session_halt_at", now) > 120.0:
                self.running = False

    def _check_feed_health(self):
        now=time.time()
        with self.lock:
            stale=(not self.ws_ready or now-self.last_tick_ts>DEADMAN_FEED_SEC)
            due=now-self.last_ws_restart>WS_RESTART_COOLDOWN
            if not stale or not due or not self.running or self.safety.stopped.is_set():
                return
            self.last_ws_restart=now
            self.ws_ready=False
            self.last_action="Feed stale: opening quotes suspended; recovering read-only"
        worker=getattr(self,"_feed_recovery",None)
        if worker is None or worker.state().get("failed"):
            worker=LatestActionWorker(self._recover_feed,metrics=getattr(self,"telemetry",None),
                                      thread_name="mm-feed-recovery")
            self._feed_recovery=worker
        worker.request()

    def _recover_feed(self):
        client=None
        try:
            from hyperliquid.api import API
            client=API(BASE_URL,timeout=3)
            if not self.running or self.safety.stopped.is_set():
                return
            self.refresh_bbo_rest(lambda:client.post("/info",{"type":"l2Book","coin":SYMBOL}))
            if self.running and not self.safety.stopped.is_set():
                self.restart_ws()
        except Exception as exc:
            if self.running:
                self.last_action="Feed recovery unavailable: "+type(exc).__name__
        finally:
            if client is not None:
                try:client.session.close()
                except Exception:pass
            self.wake.set()

    def _manage_session_exit(self, now, pos, upnl, upnl_bps, pos_since, vol):
        """Keep routine loss/age exits maker-only; no timer-to-taker fallback.

        Normal mode disables routine stops and max-hold loss exits entirely:
        the reduce-only order waits at the profit anchor and only a
        catastrophic move renews the passive bank window.
        """
        if abs(pos) < MIN_SIZE:
            self._hard_exit_at = 0.0
            self._hard_exit_reason = ""
            return
        stop_bps = self._eff_stop_bps(vol)
        if not MID_LOSS_CUTS_ENABLED:
            # Normal mode: no routine stop, no max-hold loss exit. The resting
            # reduce-only order waits at the fee-aware profit anchor; only a
            # catastrophic move may still force the passive bank window.
            stop_bps = max(RUNAWAY_STOP_MULT * stop_bps, MID_CATASTROPHIC_STOP_BPS)
            if upnl < 0 and abs(upnl_bps) >= stop_bps:
                if now >= self._hard_exit_at:
                    reason = f"runaway {abs(upnl_bps):.1f}bps"
                    self.bank_until = max(self.bank_until, now + HARD_EXIT_MAKER_SEC)
                    self._hard_exit_at = now + HARD_EXIT_MAKER_SEC
                    self._hard_exit_reason = reason
                    self.pause_until = max(self.pause_until, now + STOP_PAUSE_SEC)
                    self.last_action = f"maker exit ({reason})"
                    self.force_quote = True
                    self._urgent_quote = True
            return
        if upnl < 0 and abs(upnl_bps) >= RUNAWAY_STOP_MULT * stop_bps:
            # Emergency protection remains immediate, but maker-only: renew the
            # reduce-only ALO window instead of authorizing an autonomous taker.
            if now >= self._hard_exit_at:
                reason = f"runaway {abs(upnl_bps):.1f}bps"
                self.bank_until = max(self.bank_until, now + HARD_EXIT_MAKER_SEC)
                self._hard_exit_at = now + HARD_EXIT_MAKER_SEC
                self._hard_exit_reason = reason
                self.pause_until = max(self.pause_until, now + STOP_PAUSE_SEC)
                self.last_action = f"maker exit ({reason}); manual close if unfilled"
                self.force_quote = True
                self._urgent_quote = True
            return
        reason = self._hard_exit_reason if self._hard_exit_at else ""
        if not reason and upnl < 0 and abs(upnl_bps) >= stop_bps:
            reason = f"stop {abs(upnl_bps):.1f}bps"
        if not reason and pos_since and now - pos_since > self._eff_max_hold() + FORCE_FLAT_EXTRA:
            reason = "max hold"
        if reason and (not self._hard_exit_at or now >= self._hard_exit_at):
            # Renew the maker exit window; expiry never authorizes a taker send.
            self.bank_until = max(self.bank_until, now + HARD_EXIT_MAKER_SEC)
            self._hard_exit_at = now + HARD_EXIT_MAKER_SEC
            self._hard_exit_reason = reason
            self.pause_until = max(self.pause_until, now + STOP_PAUSE_SEC)
            self.last_action = f"maker exit ({reason})"
            self.force_quote = True
            self._urgent_quote = True

    # -------------------------------------------------- IPC dashboard (files)
    def _check_stop_file(self):
        if not supervisor_alive():
            self.safety.fail("Supervisor unavailable; manual restart required")
            return
        try:
            if os.path.exists(self._stop_file):
                if self._manual_stop_file():
                    self.safety.request_manual_stop()
                    self.last_action = "manual stop requested by the dashboard"
                else:
                    self._sig_shutdown()
                    self.last_action = "stop requested by the dashboard"
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
                    **self.safety.snapshot(),
                    "symbol": SYMBOL,
                    "running": running,
                    "phase": phase or self.safety.phase,
                    "live": self.live,
                    "ts": time.time(),
                    "start_time": self.start_time,
                    "mid": mid,
                    "spread_bps": spread_bps,
                    "position": self.position,
                    "position_usd": self.position * mid if mid > 0 else 0.0,
                    "entry": self.entry_price,
                    "upnl": self.unrealized_pnl,
                    "inventory_mark_known": getattr(self, "_inventory_mark_known", True),
                    "inventory_mark_source": getattr(self, "_inventory_mark_source", ""),
                    "execution_policy": "maker_only_auto",
                    "mid_exit_policy": ("loss_cuts" if MID_LOSS_CUTS_ENABLED
                                        else "hold_to_profit"),
                    "execution_transport": "websocket" if getattr(self,"_execution_transport",None) is not None else "simulation",
                    "risk_exit_reason": getattr(self,"_hard_exit_reason",""),
                    "equity": self.equity,
                    "volume": self.total_volume,
                    "fees": self.total_fees,
                    "rpnl": self.session_pnl,
                    "cost_per_1m": ((self.total_fees - self.session_pnl) * 1_000_000 / self.total_volume
                                    if self.total_volume > 0 and getattr(self,"accounting_complete",True) else None),
                    "accounting_complete": getattr(self,"accounting_complete",True),
                    "fills": self.fills_count,
                    "maker_fills": self.maker_fills,
                    "order_placements": self.order_placements,
                    "order_modifications": self.order_modifications,
                    "order_cancellations": self.order_cancellations,
                    "order_requests": self.order_requests,
                    "strategy": STRATEGY,
                    "notional": ORDER_NOTIONAL_USD,
                    "action": self.last_action,
                    "error": self.safety.error or self.last_error,
                    "warning": getattr(self, "quote_warning", ""),
                    "protect": self.protect,
                    "hourly_net": self.hourly_net_pnl,
                    "hourly_pause_until": self.hourly_pause_until,
                    "session_mode": getattr(self, "session_mode", "open"),
                    "markout_bid_bps": self.markout_bps["B"],
                    "markout_ask_bps": self.markout_bps["A"],
                    "mid_offset_bps": MID_OFFSET_BPS if STRATEGY == "mid" else 0.0,
                    "mid_quote": None,
                    "mid_protect_exits": STRATEGY=="mid" and MID_EXIT_PROTECTION,
                    "mid_exit_protect_sec": MID_EXIT_PROTECT_SEC,
                    "end_time": None if running else time.time(),
                    "accounting_error": getattr(self,"_accounting_error",""),
                }
            with self.lock:
                reg = self._reg_now[0]
                macro = self._macro
            data["mid_quote"] = self._mid_quote_status()
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
            if getattr(self,"telemetry",None):
                data["latency"] = self.telemetry.snapshot()
                data["latency"]["maintenance"] = self._maintenance_status()
                transport=getattr(self,"_execution_transport",None)
                if transport is not None:
                    data["latency"]["execution"]=transport.state()
                actions=getattr(self,"_quote_worker",None)
                if actions is not None:
                    data["latency"]["actions"]=actions.state()
                with self.lock:
                    received = getattr(self, "book_received_ns", 0)
                    data["latency"]["feed"] = {
                        "source": getattr(self, "book_source", ""),
                        "book_age_ms": (time.perf_counter_ns()-received)/1e6 if received else None,
                        "generation": getattr(self, "_feed_generation", 0)}
            data["taker_fills"] = getattr(self,"taker_fills",0)
            data["unknown_role_fills"] = getattr(self,"unknown_role_fills",0)
            writer=getattr(self,"_stats_writer",None)
            if writer is not None:
                if running:writer.submit(data)
                else:
                    if not writer.close(data):self.telemetry.count("final_stats_unconfirmed")
                return
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
        """Calcule les signaux coûteux au plus une fois toutes les 50 ms.

        Un fill (urgent) force la mise à jour. C'est le SEUL endroit où
        regime() avance sa machine à états et où _queue_fraction() décrémente
        queue_ahead. Un flux rapide ne doit pas payer O(mids) + O(trades) à
        chaque événement sur le thread de décision.
        """
        now_mono = time.monotonic()
        if (not getattr(self, "_urgent_quote", False)
                and self._last_signal_refresh
                and now_mono - self._last_signal_refresh < 0.05):
            return
        self._last_signal_refresh = now_mono
        adaptive_mid = STRATEGY == "mid" and MID_ADAPTIVE
        # MID reactivated its reservation/anti-adverse layer. BBO deliberately
        # keeps strict touch quoting and does not consume these filters.
        reg = self.signal.regime() if adaptive_mid else (None, 0)   # unique state-machine advance
        early = self.signal.early_warning() if adaptive_mid else (None, 0)
        alpha = self.signal.alpha_bps() if adaptive_mid else 0.0
        z = self.signal.zscore() if adaptive_mid else 0.0
        micro_tilt, micro_ts = (self.signal.micro_tilt() if adaptive_mid
                                else (0.0, 0.0))
        adj_bid, adj_ask, adj_ts = (self.signal.adjusted_touch() if adaptive_mid
                                    else (0.0, 0.0, 0.0))
        mom_f = self.signal.momentum_bps(MOM_FAST_SEC) if adaptive_mid else 0.0
        mom_s = self.signal.momentum_bps(MOM_SLOW_SEC) if adaptive_mid else 0.0
        vol = self.signal.vol_bps()
        touch = self.signal.touch_imbalance()   # déséquilibre au touch (toxicité)
        macro = self.macro.context()            # indicateurs bougies (contexte)
        qf_b = self._queue_fraction("B")
        qf_a = self._queue_fraction("A")
        last_l2 = getattr(self, "_last_l2_receive", 0.0)
        if adaptive_mid and (not last_l2 or time.time() - last_l2 > 2.0):
            # The BBO may still be alive while the depth/flow feed is stale.
            # Never quote off an old micro/OFI/queue snapshot: fall back to the
            # raw mid and conservative full adverse cost.
            alpha = z = micro_tilt = mom_f = mom_s = touch = 0.0
            micro_ts = 0.0
            adj_bid = adj_ask = adj_ts = 0.0
            reg = early = (None, 0)
            qf_b = qf_a = None
        with self.lock:
            self._reg_now = reg
            self._early_now = early
            self._sig_alpha = alpha
            self._sig_z = z
            self._sig_vol = vol
            self._sig_touch = touch
            self._sig_micro_tilt = micro_tilt
            self._sig_micro_ts = micro_ts
            self._sig_adj_bid = adj_bid
            self._sig_adj_ask = adj_ask
            self._sig_adj_ts = adj_ts
            self._sig_mom_fast = mom_f
            self._sig_mom_slow = mom_s
            self._macro = macro
            self._qf_b, self._qf_a = qf_b, qf_a

    def _next_wait_timeout(self):
        due = getattr(self, "_quote_due_at", 0.0)
        return min(0.25, max(0.0, due-time.monotonic())) if due else 0.25

    def _start_quote_worker(self):
        """Serialize exchange actions off the decision loop, latest state wins."""
        worker = getattr(self, "_quote_worker", None)
        if worker is not None:
            state = worker.state()
            if state["alive"] and not state["stopping"]:
                return worker
            worker.close(0)
        worker = LatestActionWorker(
            self._run_quote_action, on_error=self._quote_action_failed,
            metrics=getattr(self, "telemetry", None), thread_name="mm-actions")
        self._quote_worker = worker
        return worker

    def _run_quote_action(self):
        with self.lock:
            if not self.running or self.safety.stopped.is_set():
                return
            self._action_in_progress = True
        try:
            self.apply_intent()
        finally:
            with self.lock:
                self._action_in_progress = False
                due=getattr(self,"_quote_due_at",0.0)
            if due:
                self.wake.set()  # schedule a coalesced deadline, never an ACK-driven busy loop

    def _quote_action_failed(self, error):
        self.last_error = "action worker: " + safe_error(error)
        self.safety.fail("Action worker failed: " + safe_error(error))

    def _request_quote_action(self):
        worker = getattr(self, "_quote_worker", None)
        if worker is None or worker.state().get("failed"):
            return False
        accepted=worker.request()
        if accepted:
            with self.lock:
                due=getattr(self,"_quote_due_at",0.0)
                if due and due<=time.monotonic():
                    self._quote_due_at=0.0
        return accepted

    def _trading_loop(self, live_display=None):
        last_render = 0.0
        while self.running:
            self.wake.wait(timeout=self._next_wait_timeout())
            self.wake.clear()
            if not self.running:
                break
            now = time.time()
            self._check_stop_file()
            if not self.running:
                break
            self._refresh_signals()   # cache signaux 1×/cycle AVANT tout usage
            if fast_mm():
                self._maintenance_tick()
            else:
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
            if not self.running or self.safety.stopped.is_set():
                break
            self.update_markouts()
            self.risk_checks()
            if now - self._last_stats_write > 1.0:
                self._last_stats_write = now
                self.write_stats(running=True)
            if not self.running:
                break
            if self.ws_ready or bool(self.orders):
                self._request_quote_action()
            if live_display is not None and now - last_render > 0.5:
                last_render = now
                live_display.update(self.dashboard())

    def _cleanup_snapshot(self):
        return account_snapshot(self.info, ACCOUNT_ADDRESS, [SYMBOL])

    def _cleanup_snapshot_keep_exits(self):
        """Automatic stop: cancel openings but leave the passive close resting.

        Cancelling every owned order on an automatic stop would orphan the
        residual position with no exit at all. The position is still reported
        unconfirmed, but its Alo reduce-only order stays live on the exchange.
        An explicit manual Stop keeps the old full-cancel + IOC close path.
        """
        rows = self._cleanup_snapshot()
        if self.safety.manual_stop_requested:
            return rows
        filtered = []
        for symbol, position, orders in rows:
            retained = []
            for order in orders:
                side = order.get("side")
                closing = ((position > 0 and side == "A")
                           or (position < 0 and side == "B"))
                if closing:
                    continue
                retained.append(order)
            filtered.append((symbol, position, retained))
        return filtered

    def _cleanup_close(self, symbol, position):
        book = self.info.l2_snapshot(symbol)
        levels = book.get("levels", [])
        bb, ba = float(levels[0][0]["px"]), float(levels[1][0]["px"])
        if bb <= 0 or ba <= bb:
            raise RuntimeError("Cannot close: no fresh book")
        tick = dyn_tick((bb + ba) / 2)
        buy = position < 0
        px = px_up(ba + 20 * tick, tick) if buy else px_down(max(tick, bb - 20 * tick), tick)
        response=self.exchange.order(name=symbol, is_buy=buy, sz=round_sz(abs(position)), limit_px=px,
                                     order_type={"limit": {"tif": "Ioc"}}, reduce_only=True)
        for status in response.get("response",{}).get("data",{}).get("statuses",[]):
            if isinstance(status,dict) and "filled" in status:
                fill=status["filled"]
                expected=getattr(self,"_expected_cleanup_fills",{})
                expected[int(fill["oid"])]=finite(fill["totalSz"])
                self._expected_cleanup_fills=expected

    def _final_accounting(self, timeout):
        """Bounded, read-only accounting reconciliation after cleanup, including IOC fills."""
        if self.safety.cleanup_confirmed:
            with self.lock:
                self.position=self.entry_price=self.unrealized_pnl=self.pos_since=0.0
                self.position_dirty=False
        if not self.safety.exposure_started:
            return
        if timeout<=0:
            self.accounting_complete=False
            self._accounting_error="Final accounting deadline exceeded"
            return
        client=None
        try:
            from hyperliquid.api import API
            from zeus.safety import confirmed_client_order
            client=API(BASE_URL,timeout=min(3.0,timeout))
            # A placement whose ACK was lost is not in safety.owned; resolve
            # its CLOID before attributing fills so its real fills are counted.
            unresolved=set()
            for cloid in list(self.safety.pending_cloids):
                try:
                    oid,_status=confirmed_client_order(
                        client,ACCOUNT_ADDRESS,cloid,
                        {SYMBOL,SYMBOL.split(":")[-1]})
                    self.safety.owned.add(int(oid))
                    self.safety.pending_cloids.discard(cloid)
                except Exception:
                    unresolved.add(cloid)
            # One userFillsByTime page is capped at 2000 rows. A fast MID
            # session can exceed that, so page by the last observed time.
            start=max(0,int(self.start_time*1000)-30000)
            rows=[];seen=set();truncated=False
            deadline=time.monotonic()+max(1.0,min(8.0,timeout*0.6))
            for _ in range(20):
                page=client.post("/info",{"type":"userFillsByTime","user":ACCOUNT_ADDRESS,
                                          "startTime":start})
                if not isinstance(page,list):
                    raise ValueError("Invalid fill history")
                fresh=[r for r in page if isinstance(r,dict)
                       and str(r.get("tid")) not in seen]
                for row in fresh:
                    seen.add(str(row.get("tid")))
                rows.extend(fresh)
                if len(page)<2000:
                    break
                try:
                    last=max(int(r["time"]) for r in page if "time" in r)
                except (ValueError, TypeError):
                    truncated=True
                    break
                start=last+1
                if time.monotonic()>=deadline:
                    truncated=True
                    break
            with self.lock:
                expected=dict(getattr(self,"_expected_cleanup_fills",{}))
                received={}
                for oid,size in getattr(self,"_received_fills",{}).values():
                    if oid in self.safety.owned:
                        received[oid]=received.get(oid,0.0)+size
                for oid,size in received.items():
                    expected[oid]=max(expected.get(oid,0.0),size)
                totals=summarize_fills(rows,SYMBOL,self.safety.owned,expected,
                                       allow_large=True)
                self.total_volume=totals["volume"];self.total_fees=totals["fees"];self.session_pnl=totals["rpnl"]
                self.fills_count=totals["fills"];self.maker_fills=totals["maker"]
                self.taker_fills=totals["taker"];self.unknown_role_fills=totals["unknown"]
                self.accounting_complete = not unresolved and not truncated
                if self.accounting_complete:
                    self._accounting_error=""
                elif unresolved:
                    self._accounting_error="Unresolved placement client IDs"
                else:
                    self._accounting_error="Final fill history truncated"
                self.processed_fills.update(totals["tids"])
        except Exception as exc:
            self.accounting_complete=False
            self._accounting_error=f"Final fills unconfirmed: {type(exc).__name__}"
        finally:
            if client is not None:
                try:client.session.close()
                except Exception:pass

    def run(self):
        try:
            self._check_stop_file()
            if self.live and self.running:
                read_started = time.monotonic()
                initial = self._cleanup_snapshot()
                self._orders_verified_at = read_started
                if any(position or orders for _, position, orders in initial):
                    raise RuntimeError("Existing position/orders; manual reconciliation required")
            if self.running:
                self._run_session()
        except BaseException as exc:
            self.safety.fail(exc)
        finally:
            shutdown_started=time.monotonic()
            if self._manual_stop_file():
                self.safety.request_manual_stop()
            self.safety.stop()
            quote_worker = getattr(self, "_quote_worker", None)
            if quote_worker is not None:
                if not quote_worker.close(timeout=1) and getattr(self,"telemetry",None):
                    self.telemetry.count("action_worker_shutdown_pending")
            recovery=getattr(self,"_feed_recovery",None)
            if recovery is not None:
                recovery.close(timeout=1)
            maintenance = getattr(self, "_maintenance", None)
            if maintenance is not None:
                if not maintenance.close(timeout=1) and self.telemetry:
                    self.telemetry.count("maintenance_shutdown_pending")
            if self.live:
                # The actor may still be reading through self.info before it
                # enters the signed-action lock. Drain the whole callback, not
                # just signed requests, before sharing that HTTP client with
                # cleanup. A blocked actor cannot extend the shutdown budget.
                actions_drained = (quote_worker is None or quote_worker.close(
                    timeout=max(0,30-(time.monotonic()-shutdown_started))))
                if actions_drained:
                    manual_scope = (self.safety.manual_close_scope()
                                    if self.safety.manual_stop_requested else nullcontext())
                    snapshot = (self._cleanup_snapshot if self.safety.manual_stop_requested
                                else self._cleanup_snapshot_keep_exits)
                    with manual_scope:
                        self.safety.cleanup(snapshot,
                            lambda rows: self.exchange.bulk_cancel([{"coin": symbol, "oid": oid} for symbol, oid in rows]),
                            self._cleanup_close,timeout=max(0,30-(time.monotonic()-shutdown_started)),
                            allow_close=self.safety.manual_stop_requested)
                else:
                    self.safety.cleanup_confirmed=False
                    self.safety.cleanup_error="Execution worker still resolving; stop not confirmed"
                    self.safety.phase="failed"
                    self.safety.publish()
                self._final_accounting(30-(time.monotonic()-shutdown_started))
            else:
                self.safety.cleanup_confirmed = True
                self.safety.phase = "failed" if self.safety.error else "stopped"
            if quote_worker is not None and not quote_worker.close(timeout=0):
                self.safety.cleanup_confirmed=False
                self.safety.cleanup_error="Execution worker still resolving; stop not confirmed"
                self.safety.phase="failed"
                self.safety.publish()
            transport=getattr(self,"_execution_transport",None)
            if transport is not None:
                transport.close(timeout=1)
            try:
                if self.ws_info is not None:
                    self.ws_info.disconnect_websocket()
            except Exception:
                pass
            self.write_stats(running=False)

    def _run_session(self):
        # Headless: lancé dans un thread (dashboard in-process) OU en process
        # enfant du dashboard (OBI_HEADLESS=1) — pas de Rich Live ni de prompt.
        headless = (threading.current_thread() is not threading.main_thread()
                    or os.getenv("OBI_HEADLESS", "") == "1")
        if not headless:
            print(f"⏳ {SYMBOL} — attente du premier book L2...")
        transport=getattr(self,"_execution_transport",None)
        if self.live and transport is not None:
            transport.start()
            deadline=time.monotonic()+10
            while self.running and not transport.wait_ready(.1) and time.monotonic()<deadline:
                self._check_stop_file()
            if not self.running:
                return
            if not transport.state()["ready"]:
                self.safety.fail("Execution WebSocket unavailable; no trading action sent")
                return
        self.set_leverage()
        if not self.running:
            return
        self.sync_position()
        if not self.running:
            return
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
            self.last_action = "Cannot connect to Hyperliquid"
            self.safety.fail(exc)
            print(self.safety.error, flush=True)
            self.write_stats(running=False, phase="failed")
            return
        deadline = time.time() + 15
        while self.running and not self.ws_ready and time.time() < deadline:
            time.sleep(0.2)
        if not self.ws_ready:
            self.refresh_bbo_rest()

        if not self.running:
            return
        self.safety.phase = "running"
        self.safety.publish()
        if fast_mm():
            self._stats_writer = LatestSnapshotWriter(self._stats_file, self.telemetry)
            self._maintenance = MidMaintenance(BASE_URL, ACCOUNT_ADDRESS, SYMBOL, self.wake, self.telemetry,
                                               halt_event=self.safety.stopped)
            self._maintenance_position_request_at = self._position_verified_at
            self._maintenance_orders_request_at = self._orders_verified_at
        self._start_quote_worker()
        if headless:
            self._trading_loop(None)
        else:
            with Live(self.dashboard(), console=self.console, refresh_per_second=2, screen=False) as live:
                self._trading_loop(live)



# =============================================================================
# MAIN
# =============================================================================
def resolve_token(name: str) -> str:
    """Resolve a preset, a bare ticker or a dex-qualified HIP-3 market."""
    try:
        return resolve_symbol(name)
    except ValueError as exc:
        raise SystemExit(f"❌ Token inconnu: {name} ({exc})") from exc

# --- merged from session_accounting.py ---

"""Strict final fill aggregation scoped to exchange order IDs of one session."""
from zeus.safety import finite


def summarize_fills(rows, symbol, owned, expected=None, *, allow_large=False):
    # A single userFillsByTime page is capped at 2000 rows. The caller that
    # pages the history may pass allow_large=True; a raw single-page response
    # of >=2000 rows is still treated as truncated.
    if not isinstance(rows,list) or (not allow_large and len(rows)>=2000):
        raise ValueError("Final fills unavailable or truncated")
    fills={}
    for row in rows:
        if not isinstance(row,dict):
            raise ValueError("Invalid final fill row")
        if row.get("coin") not in (symbol,symbol.split(":")[-1]) or int(row.get("oid",0)) not in owned:
            continue
        if row.get("tid") is None or row.get("feeToken","USDC")!="USDC":
            raise ValueError("Final fill identity or fee currency unconfirmed")
        price,size,fee,pnl=(finite(row[k]) for k in ("px","sz","fee","closedPnl"))
        if price<=0 or size<=0:
            raise ValueError("Invalid final fill size/price")
        key=str(row["tid"])
        item=(int(row["oid"]),price,size,fee,pnl,row.get("crossed"))
        if key in fills and fills[key]!=item:
            raise ValueError("Conflicting duplicate final fill")
        fills[key]=item
    for oid,size in (expected or {}).items():
        observed=sum(item[2] for item in fills.values() if item[0]==oid)
        if observed+1e-9<size:
            raise ValueError("Closing fills not yet confirmed in history")
    return {"volume":sum(px*sz for _,px,sz,_,_,_ in fills.values()),
            "fees":sum(fee for _,_,_,fee,_,_ in fills.values()),
            "rpnl":sum(pnl for _,_,_,_,pnl,_ in fills.values()),
            "fills":len(fills),
            "maker":sum(role is False for *_,role in fills.values()),
            "taker":sum(role is True for *_,role in fills.values()),
            "unknown":sum(role is not True and role is not False for *_,role in fills.values()),
            "tids":tuple(fills)}

# --- merged from snapshot_writer.py ---

"""One background publisher with a single replaceable pending snapshot."""
import threading
import time
from zeus.safety import atomic_json

class LatestSnapshotWriter:
    def __init__(self,path,metrics=None,publish=atomic_json):
        self.path,self.metrics,self.publish=path,metrics,publish
        self.condition=threading.Condition();self.pending=None;self.stopping=False
        self.error=""
        self.thread=threading.Thread(target=self._run,name="mid-statistics",daemon=True)
        self.thread.start()
    def submit(self,data):
        with self.condition:
            if self.stopping:return False
            if self.pending is not None and self.metrics:self.metrics.count("stats_coalesced")
            self.pending=data;self.condition.notify()
        return True
    def _run(self):
        while True:
            with self.condition:
                self.condition.wait_for(lambda:self.pending is not None or self.stopping)
                if self.pending is None and self.stopping:return
                data=self.pending;self.pending=None
            started=time.perf_counter_ns()
            try:self.publish(self.path,data);self.error=""
            except Exception as exc:
                self.error=type(exc).__name__
                if self.metrics:self.metrics.count("stats_write_failures")
            finally:
                if self.metrics:self.metrics.observe("stats_write",(time.perf_counter_ns()-started)/1e6)
    def close(self,final,timeout=2):
        with self.condition:
            self.pending=final;self.stopping=True;self.condition.notify()
        self.thread.join(timeout)
        return not self.thread.is_alive() and not self.error

# --- merged from maker_exit.py ---

"""Bounded maker exit protection using observed fees; never authorizes taker."""
import math


def exit_price(base, buy, entry, tick, opening_rate, maker_rate):
    """Break-even after estimated fees plus one tick, rounded away from loss.

    Rates are conservative maxima observed in fills on this exact market.
    Returns None when costs cannot be estimated, rather than assuming zero.
    """
    values=(base,entry,tick,opening_rate,maker_rate)
    if any(v is None or not math.isfinite(v) for v in values):
        return None
    if min(base,entry,tick)<=0 or not 0<=opening_rate<1 or not 0<=maker_rate<1:
        return None
    if buy:
        target=(entry*(1-opening_rate)-tick)/(1+maker_rate)
        return min(base, math.floor((target+tick*1e-9)/tick)*tick) if target>0 else None
    target=(entry*(1+opening_rate)+tick)/(1-maker_rate)
    return max(base, math.ceil((target-tick*1e-9)/tick)*tick)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="BBO / MID Order Book market maker (HIP-3)")
    parser.add_argument("--token", default=None, help="e.g. SP500, XYZ100, SPCX")
    parser.add_argument("--live", action="store_true", help="real orders (default: dry-run)")
    parser.add_argument("--notional", type=float, default=None, help="USD per order")
    parser.add_argument("--strategy", choices=("bbo", "mid"), default="bbo",
                        help="BBO Order Book or MID Order Book")
    parser.add_argument("--offset-bps", type=float, default=None,
                        help="symmetric distance from mid for strategy=mid")
    exits=parser.add_mutually_exclusive_group()
    exits.add_argument("--protect-mid-exits",dest="mid_protect_exits",action="store_true")
    exits.add_argument("--strict-mid-exits",dest="mid_protect_exits",action="store_false")
    parser.set_defaults(mid_protect_exits=True)
    parser.add_argument("--mid-exit-protect-sec",type=float,default=30.0)
    args = parser.parse_args()
    STRATEGY = args.strategy
    if not math.isfinite(args.mid_exit_protect_sec) or not 1<=args.mid_exit_protect_sec<=120:
        parser.error("--mid-exit-protect-sec must be from 1 to 120 seconds")
    MID_EXIT_PROTECTION = STRATEGY=="mid" and args.mid_protect_exits
    MID_EXIT_PROTECT_SEC = args.mid_exit_protect_sec
    if args.offset_bps is not None:
        if not math.isfinite(args.offset_bps) or not 0 <= args.offset_bps < 10000:
            parser.error("--offset-bps must be finite, from 0 inclusive up to 10000 bps exclusive")
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
        sys.exit(f"❌ A bot is already running on {SYMBOL} (instance lock)")

    startup_started = time.time()
    write_startup_status(SYMBOL, STRATEGY, startup_started, args.live, "connecting")
    try:
        meta = fetch_token_meta(SYMBOL)
        SZ_DECIMALS = meta["szDecimals"]
        MIN_SIZE = 10.0 ** (-SZ_DECIMALS) if SZ_DECIMALS > 0 else 1.0
        PRICE_DECIMALS = max(0, 6 - SZ_DECIMALS)
        LEVERAGE, IS_CROSS = leverage_settings(meta, LEVERAGE)
        bot = OrderBookBot(live=args.live)
    except Exception as exc:
        message = str(exc) if isinstance(exc, StartupNetworkError) else f"Startup failed: {type(exc).__name__}: {exc}"
        write_startup_status(SYMBOL, STRATEGY, startup_started, args.live, "failed", message)
        print(message, flush=True)
        sys.exit(1)
    mode = "LIVE" if args.live else "DRY-RUN (no orders sent — add --live)"
    print(f"🚀 {STRATEGY.upper()} BOT | {SYMBOL} | {LEVERAGE}x | ~${ORDER_NOTIONAL_USD:.0f}/order | {mode}")
    bot.run()
