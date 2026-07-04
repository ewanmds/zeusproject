"""
Hyperliquid Market Making Bot - PROFIT-FIRST v4.0 MULTI-TOKENS
=====================================================
Profit-oriented market making for HIP-3 km/xyz markets with:

💰 PROFIT MAXIMIZATION:
- Wide spreads to capture real edge per round-trip
- Pyramidal sizing (biggest at best price)
- Stable orders that live long enough for round-trip completion
- Passive inventory reduction via skew (no panic market closes)

🛡️ ROUND-TRIP FOCUS:
- Track every buy→sell / sell→buy round-trip
- Adaptive spread based on round-trip profitability
- Post-fill cooldown to let opposite side get filled
- PnL/hour as primary metric (not volume)

🎯 MULTI-TOKENS SUPPORT:
- US500, USTECH, USOIL, GOLD, BTC, xyz tokens
- Configuration personnalisée par token
- Menu de sélection interactif
"""

import time
import threading
from datetime import datetime
from collections import deque
import json
import math
import eth_account
from hyperliquid.info import Info
from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants
from typing import Optional, Tuple, List
import signal
import sys
import argparse
import os
import psutil
from rich.console import Console, Group
from rich.table import Table
from rich.live import Live
from rich.panel import Panel
from rich.layout import Layout
from rich.text import Text
from rich import box
from rich.align import Align

# =============================================================================
# CONFIGURATION MULTI-TOKENS - OPTIMIZED FOR PROFIT (NOT VOLUME)
# =============================================================================

# --- 1. COMPTE & API ---
AGENT_PRIVATE_KEY = "0xd4dc9cb4f40d41dcb06f9dc01afdca2235c75b51c74002a0b88e8fcdc3bd8e69"
ACCOUNT_ADDRESS = "0x32486BaA4610534c1edA43194ADD5108b3bb4c26"
BASE_URL = constants.MAINNET_API_URL

# =============================================================================
# MODE DASHBOARD UNIQUEMENT — pas d'envoi d'ordres
# Mettre à False pour réactiver le trading
# =============================================================================
DASHBOARD_ONLY_MODE = True

# --- CONFIGURATION MULTI-TOKENS ---
# Tokens disponibles sur Kinetiq Markets
AVAILABLE_TOKENS = {
    "US500": {
        "symbol": "km:US500",
        "tick_size": 0.01,
        "min_size": 0.01,
        "order_size_usd": 11.0,
        "max_position_usd": 35.0,
        "target_leverage": 25.0,
        "num_levels": 3,
        "level_multipliers": [1.5, 1.0, 0.5]
    },
    "USTECH": {
        "symbol": "km:USTECH",
        "tick_size": 0.01,
        "min_size": 0.01,
        "order_size_usd": 11.0,
        "max_position_usd": 35.0,
        "target_leverage": 20.0,
        "num_levels": 3,
        "level_multipliers": [1.5, 1.0, 0.5]
    },
    "USOIL": {
        "symbol": "km:USOIL",
        "tick_size": 0.01,
        "min_size": 0.01,
        "order_size_usd": 11.0,
        "max_position_usd": 35.0,
        "target_leverage": 20.0,
        "num_levels": 3,
        "level_multipliers": [1.5, 1.0, 0.5]
    },
    "GOLD": {
        "symbol": "km:GOLD",
        "tick_size": 0.01,
        "min_size": 0.01,
        "order_size_usd": 11.0,
        "max_position_usd": 35.0,
        "target_leverage": 22.0,
        "num_levels": 3,
        "level_multipliers": [1.5, 1.0, 0.5]
    },
    "BTC": {
        "symbol": "km:BTC",
        "tick_size": 0.01,
        "min_size": 0.001,
        "order_size_usd": 11.0,
        "max_position_usd": 35.0,
        "target_leverage": 25.0,
        "num_levels": 3,
        "level_multipliers": [1.5, 1.0, 0.5]
    },
    # === XYZ TOKENS (HIP-3) ===
    "SILVER": {
        "symbol": "xyz:SILVER",
        "tick_size": 0.001,
        "min_size": 0.01,
        "order_size_usd": 11.0,
        "max_position_usd": 35.0,
        "target_leverage": 20.0,
        "num_levels": 3,
        "level_multipliers": [1.5, 1.0, 0.5]
    },
    "GOLD_XYZ": {
        "symbol": "xyz:GOLD",
        "tick_size": 0.01,
        "min_size": 0.01,
        "order_size_usd": 11.0,
        "max_position_usd": 35.0,
        "target_leverage": 20.0,
        "num_levels": 3,
        "level_multipliers": [1.5, 1.0, 0.5]
    },
    "XYZ100": {
        "symbol": "xyz:XYZ100",
        "tick_size": 1.0,
        "min_size": 0.0001,
        "order_size_usd": 120.0,
        "max_position_usd": 35.0,
        "target_leverage": 25.0,
        "num_levels": 3,
        "level_multipliers": [1.5, 1.0, 0.5]
    },
    "S&P500": {
        # Hyperliquid utilise "SP500" comme identifiant API pour S&P500.
        "symbol": "xyz:SP500",
        "tick_size": 1.0,
        "min_size": 0.0001,
        "order_size_usd": 120.0,
        "max_position_usd": 35.0,
        "target_leverage": 25.0,
        "num_levels": 3,
        "level_multipliers": [1.5, 1.0, 0.5]
    },
    "S&P500PS": {
        # Alias explicite demandé : même marché canonique côté API.
        "symbol": "xyz:SP500",
        "tick_size": 1.0,
        "min_size": 0.0001,
        "order_size_usd": 120.0,
        "max_position_usd": 35.0,
        "target_leverage": 25.0,
        "num_levels": 3,
        "level_multipliers": [1.5, 1.0, 0.5]
    },
    "CL": {
        "symbol": "xyz:CL",
        "tick_size": 0.01,
        "min_size": 0.01,
        "order_size_usd": 120.0,
        "max_position_usd": 200.0,
        "target_leverage": 20.0,
        "num_levels": 2,
        "level_multipliers": [1.0, 0.5]
    },
    "COPPER": {
        "symbol": "xyz:COPPER",
        "tick_size": 0.0001,
        "min_size": 0.1,
        "order_size_usd": 11.0,
        "max_position_usd": 35.0,
        "target_leverage": 20.0,
        "num_levels": 3,
        "level_multipliers": [1.5, 1.0, 0.5]
    },
    "PLTR": {
        "symbol": "xyz:PLTR",
        "tick_size": 0.01,
        "min_size": 0.1,
        "order_size_usd": 11.0,
        "max_position_usd": 35.0,
        "target_leverage": 10.0,
        "num_levels": 3,
        "level_multipliers": [1.5, 1.0, 0.5]
    },
    "TSLA": {
        "symbol": "xyz:TSLA",
        "tick_size": 0.01,
        "min_size": 0.01,
        "order_size_usd": 11.0,
        "max_position_usd": 35.0,
        "target_leverage": 10.0,
        "num_levels": 3,
        "level_multipliers": [1.5, 1.0, 0.5]
    },
    "AMD": {
        "symbol": "xyz:AMD",
        "tick_size": 0.01,
        "min_size": 0.01,
        "order_size_usd": 11.0,
        "max_position_usd": 35.0,
        "target_leverage": 10.0,
        "num_levels": 3,
        "level_multipliers": [1.5, 1.0, 0.5]
    }
}

# Choisir le token à trader (sera remplacé par le menu interactif)
SYMBOL_ALIASES = {
    # Aliases observés côté UI / anciennes configs -> symbole canonique API
    "xyz:S&P500": "xyz:SP500",
    "xyz:S&P500PS": "xyz:SP500",
}


def canonical_symbol(raw_symbol: str) -> str:
    return SYMBOL_ALIASES.get(raw_symbol, raw_symbol)


ACTIVE_TOKEN = "XYZ100"
TOKEN_CONFIG = AVAILABLE_TOKENS[ACTIVE_TOKEN]
SYMBOL = canonical_symbol(TOKEN_CONFIG["symbol"])
ORDER_SIZE_USD = TOKEN_CONFIG["order_size_usd"]
TARGET_LEVERAGE = TOKEN_CONFIG["target_leverage"]
TICK_SIZE = TOKEN_CONFIG["tick_size"]
MIN_SIZE = TOKEN_CONFIG["min_size"]

# Déterminer le DEX à utiliser (km ou xyz) et extraire le nom du coin
PERP_DEX = "xyz" if SYMBOL.startswith("xyz:") else "km"
COIN_NAME = SYMBOL.split(":")[-1]  # Nom du coin sans préfixe pour WebSocket
MAX_POSITION_USD = TOKEN_CONFIG["max_position_usd"]
NUM_LEVELS = TOKEN_CONFIG["num_levels"]
LEVEL_MULTIPLIERS = TOKEN_CONFIG["level_multipliers"]

# Calculer le nombre de décimales pour l'arrondi des prix
PRICE_DECIMALS = max(0, -int(math.floor(math.log10(TICK_SIZE)))) if TICK_SIZE < 1 else 0
SIZE_DECIMALS = max(0, -int(math.floor(math.log10(MIN_SIZE)))) if MIN_SIZE < 1 else 0

# --- 3. GESTION DU RISQUE - PATIENT (profit-first) ---
STOP_LOSS_USD = 0.50       # Stop loss avec ordres limit agressifs ($0.50) - wider
STOP_LOSS_MARKET_USD = 0.80  # Stop loss MARKET - only for extreme cases
STOP_LOSS_ROE_PCT = 4.0    # Stop loss MARKET si ROE% <= -4.0% (was 1%)
ENABLE_STOP_LOSS_USD = False  # Disabled - use ROE% instead
QUICK_EXIT_THRESHOLD = 0.15  # Exit rapide disabled effectively (high threshold)
SESSION_PNL_KILL_SWITCH = -1.00  # Kill switch: stop if session PnL < -$1.00
MIN_FILLS_FOR_KILL_SWITCH = 15

# --- 4. GRID SPACING - WIDER FOR PROFIT ---
LEVEL_TICK_SPACING = 2  # 2 ticks entre les niveaux (was 1)

# --- 5. SPREAD & SKEW - PROFIT-FIRST ---
MIN_SPREAD_TICKS = 3     # Spread minimal 3 ticks (was 1) - capture real edge
BASE_SPREAD_TICKS = 5    # Spread de base 5 ticks (was 2) - enough for round-trip profit
MAX_SPREAD_TICKS = 12     # Spread max en haute volatilité (was 5)
POSITION_SKEW_FACTOR = 0.5  # Skew pour sortir passivement (was 0.3)
INVENTORY_TARGET = 0.0    # Position cible = flat
GAMMA = 0.5  # Risk aversion higher = protect profits (was 0.3)

# --- 6. DÉTECTION DE MOMENTUM ---
MOMENTUM_THRESHOLD = 0.0001  # 0.01% pour détecter les mouvements rapides - TRÈS SENSIBLE
VOLATILITY_WINDOW = 30  # Fenêtre de calcul de volatilité (ticks)

# --- 7. MODE HAUTE VITESSE ---
US_MARKET_MODE = True
BATCH_UPDATE_ONLY = True

# --- 8. VITESSES DE REFRESH - SLOW = STABLE ORDERS ---
REFRESH_SPEED_FAST = 0.2    # 200ms pour les urgences (stop loss only)
REFRESH_SPEED_NORMAL = 1.0  # 1s normal - let orders LIVE (was 0.5)
REFRESH_SPEED_SLOW = 3.0    # 3 secondes polling de base (was 2)
POSITION_API_SYNC_INTERVAL_SEC = 2.0  # Sync API less often (was 1.0)

# --- 8b. LOGIQUE INTELLIGENTE DE REFRESH - PROFIT FOCUSED ---
# Refresh UNIQUEMENT si le prix a bougé BEAUCOUP par rapport aux ordres actuels
PRICE_DRIFT_THRESHOLD_TICKS = 5  # Refresh si mid a bougé de 5+ ticks (was 3)
URGENT_DRIFT_THRESHOLD_TICKS = 8  # Refresh urgent si 8+ ticks de drift (was 5)
PERIODIC_SYNC_SECONDS = 25  # Sync périodique toutes les 25 secondes (was 10)
ROUND_TRIP_WAIT_SEC = 8.0   # After a fill, wait this long before refreshing (let RT complete)

# --- 9. PARAMÈTRES AVANCÉS - PROFIT MODE ---
REQUOTE_ON_FILL = False     # DO NOT requote after fill - let round-trip complete (was True)
SMART_SPREAD_ENABLED = True # Ajuste le spread selon la volatilité
ANTI_ADVERSE_SELECTION = False  # DÉSACTIVÉ - le wide spread protège déjà
FILL_RATE_TARGET = 0.25  # Lower fill rate target = wider spread OK (was 0.4)
POST_ONLY_MM_QUOTES = True  # Mode normal: maker-only (Alo) - collect rebates
STRICT_POST_ONLY_FLOW = True  # N'autorise pas de quotes agressives hors close explicite

# --- 10. SCORE OPTIMIZATION / AUTO-TUNING ---
SCORE_LAMBDA_COST = 1.0
SCORE_GAMMA_INVENTORY = 0.35
SCORE_TAU_TOXIC = 0.70
AUTO_TUNE_INTERVAL_SEC = 300
AUTO_TUNE_STEP = 0.05
MIN_SCORE_WEIGHT = 0.10
MAX_SCORE_WEIGHT = 3.00

# --- 11. MICROSTRUCTURE / QUOTE QUALITY ---
L2_IMBALANCE_LEVELS = 3
MAX_QUOTE_TTL_SEC = 20.0  # Quotes live 20s (was 6s) - stable orders!
LATENCY_WIDENING_MS_REF = 250.0

# --- 12. TOXIC FLOW / RISK GUARDS ---
TOXIC_FLOW_WINDOW_SEC = 20.0
TOXIC_FLOW_PAUSE_SEC = 5.0
TOXIC_ADVERSE_BPS_TRIGGER = 2.5
MAX_TOXIC_SCORE_BEFORE_PAUSE = 0.7
FEED_STALE_SECONDS = 4.0
MAX_P95_SLIPPAGE_BPS = 4.0
MAX_INVENTORY_HARD_PCT = 1.25

# --- 12b. ALPHA SIGNALS CONFIGURATION ---
ALPHA_ENABLED = True                    # Master switch pour les signaux alpha
ALPHA_SKEW_INTENSITY = 1.5             # Intensité du skew alpha en ticks
ALPHA_MIN_TICKS_FOR_SIGNALS = 30       # Nombre min de ticks avant d'activer les signaux
ALPHA_COMPOSITE_DEADZONE = 0.1          # En-dessous de ce seuil, alpha = 0 (pas de biais)
# EMA
EMA_FAST_PERIOD = 8
EMA_SLOW_PERIOD = 21
EMA_TREND_PERIOD = 55
# Bollinger Bands
BB_PERIOD = 20
BB_STD_MULT = 2.0
# MACD
MACD_FAST_PERIOD = 12
MACD_SLOW_PERIOD = 26
MACD_SIGNAL_PERIOD = 9
# RSI
RSI_PERIOD = 14
RSI_OVERBOUGHT = 70
RSI_OVERSOLD = 30
# VWAP
VWAP_DEVIATION_THRESHOLD = 0.001       # 0.1% de déviation du VWAP pour signal
# Regime Detection
REGIME_HYSTERESIS_TICKS = 5            # Nombre de ticks pour confirmer un changement de régime
VOLATILE_BB_BANDWIDTH_THRESHOLD = 0.03 # Seuil bandwidth pour détecter VOLATILE
TRENDING_EMA_DIVERGENCE = 0.001        # Seuil divergence EMA pour détecter TRENDING
# Pondérations par régime
ALPHA_WEIGHTS = {
    "RANGING": {
        "ema": 0.10, "bollinger": 0.35, "macd": 0.10, "rsi": 0.30, "vwap": 0.15
    },
    "TRENDING": {
        "ema": 0.35, "bollinger": 0.05, "macd": 0.30, "rsi": 0.10, "vwap": 0.20
    },
    "VOLATILE": {
        "ema": 0.10, "bollinger": 0.25, "macd": 0.15, "rsi": 0.25, "vwap": 0.25
    }
}

# --- 13. HARD-STOP TOGGLES (avoid unwanted full bot stops) ---
ENABLE_FEED_STALE_HARD_STOP = False
FEED_STALE_HARD_STOP_MULTIPLIER = 8.0  # Hard stop only if stale for FEED_STALE_SECONDS * multiplier
ENABLE_DRAWDOWN_HARD_STOP = False
DRAWDOWN_REQUIRE_NEGATIVE_PNL = True
DRAWDOWN_MIN_PNL_USD = -0.10
ENABLE_P95_SLIPPAGE_KILL_SWITCH = False
ENABLE_HARD_INVENTORY_FORCE_CLOSE = False
CLOSE_ATTEMPT_COOLDOWN_SEC = 2.0

# --- 14. MARGIN-REJECT HANDLING ---
ACCOUNT_INFO_CACHE_SEC = 1.0
MARGIN_HEADROOM_PCT = 0.85
MARGIN_REJECT_COOLDOWN_SEC = 4.0
MARGIN_REJECT_SIZE_DECAY = 0.75
MARGIN_SIZE_RECOVERY_STEP = 0.03
MIN_DYNAMIC_SIZE_SCALE = 0.15

# --- 15. RUNTIME LOG / ROE STOP RECOVERY ---
VERBOSE_RUNTIME_UPDATES = False
ROE_STOP_RETRY_COOLDOWN_SEC = 5.0

# =============================================================================
# ALPHA SIGNAL ENGINE — Pure Python, zero external deps
# =============================================================================

class AlphaSignalEngine:
    """
    Pipeline de signaux alpha avancés pour piloter dynamiquement
    le spread, le skew et le sizing des ordres.
    Indicateurs: EMA, Bollinger Bands, MACD, RSI, pseudo-VWAP.
    """

    def __init__(self):
        self.prices: deque = deque(maxlen=500)     # (timestamp, mid_price)
        self.tick_count = 0

        # --- EMA state (incremental) ---
        self.ema_fast = 0.0    # EMA(8)
        self.ema_slow = 0.0    # EMA(21)
        self.ema_trend = 0.0   # EMA(55)
        self._ema_fast_alpha = 2.0 / (EMA_FAST_PERIOD + 1)
        self._ema_slow_alpha = 2.0 / (EMA_SLOW_PERIOD + 1)
        self._ema_trend_alpha = 2.0 / (EMA_TREND_PERIOD + 1)
        self._ema_initialized = False

        # --- Bollinger state ---
        self.bb_upper = 0.0
        self.bb_lower = 0.0
        self.bb_sma = 0.0
        self.bb_bandwidth = 0.0
        self.bb_pct_b = 0.5

        # --- MACD state ---
        self.macd_ema_fast = 0.0   # EMA(12) for MACD
        self.macd_ema_slow = 0.0   # EMA(26) for MACD
        self._macd_fast_alpha = 2.0 / (MACD_FAST_PERIOD + 1)
        self._macd_slow_alpha = 2.0 / (MACD_SLOW_PERIOD + 1)
        self._macd_signal_alpha = 2.0 / (MACD_SIGNAL_PERIOD + 1)
        self.macd_line = 0.0
        self.macd_signal_line = 0.0
        self.macd_histogram = 0.0
        self.macd_prev_histogram = 0.0
        self._macd_initialized = False

        # --- RSI state (Wilder smoothing) ---
        self.rsi_value = 50.0
        self.rsi_avg_gain = 0.0
        self.rsi_avg_loss = 0.0
        self._rsi_initialized = False
        self._rsi_prev_price = 0.0
        self._rsi_count = 0

        # --- VWAP state (time-weighted pseudo-VWAP) ---
        self.vwap_cum_pv = 0.0     # cumulative price * weight
        self.vwap_cum_v = 0.0      # cumulative weight
        self.vwap_value = 0.0

        # --- Signal outputs (normalized -1.0 to +1.0) ---
        self.ema_signal = 0.0
        self.bollinger_signal = 0.0
        self.macd_signal = 0.0
        self.rsi_signal = 0.0
        self.vwap_signal = 0.0

        # --- Composite ---
        self.composite_alpha = 0.0
        self.regime = "RANGING"
        self._regime_candidate = "RANGING"
        self._regime_counter = 0

    # ── public entry point ──────────────────────────────────────────────
    def update(self, mid_price: float, timestamp: float):
        """Called on every L2 tick. Updates all indicators incrementally."""
        if mid_price <= 0:
            return

        self.prices.append((timestamp, mid_price))
        self.tick_count += 1

        # --- EMA update (incremental) ---
        self._update_ema(mid_price)

        # --- Bollinger update (sliding window) ---
        self._update_bollinger()

        # --- MACD update (incremental) ---
        self._update_macd(mid_price)

        # --- RSI update (Wilder) ---
        self._update_rsi(mid_price)

        # --- VWAP update (time-weighted) ---
        self._update_vwap(mid_price, timestamp)

        # --- Regime detection ---
        self._detect_regime()

        # --- Compute signals (only after warm-up) ---
        if self.tick_count >= ALPHA_MIN_TICKS_FOR_SIGNALS:
            self._compute_ema_signal()
            self._compute_bollinger_signal()
            self._compute_macd_signal()
            self._compute_rsi_signal()
            self._compute_vwap_signal(mid_price)
            self._compute_composite()
        else:
            # Warm-up: all signals neutral
            self.ema_signal = 0.0
            self.bollinger_signal = 0.0
            self.macd_signal = 0.0
            self.rsi_signal = 0.0
            self.vwap_signal = 0.0
            self.composite_alpha = 0.0

    # ── EMA ─────────────────────────────────────────────────────────────
    def _update_ema(self, price: float):
        if not self._ema_initialized:
            self.ema_fast = price
            self.ema_slow = price
            self.ema_trend = price
            self.macd_ema_fast = price
            self.macd_ema_slow = price
            self._ema_initialized = True
            return
        self.ema_fast = self._ema_fast_alpha * price + (1.0 - self._ema_fast_alpha) * self.ema_fast
        self.ema_slow = self._ema_slow_alpha * price + (1.0 - self._ema_slow_alpha) * self.ema_slow
        self.ema_trend = self._ema_trend_alpha * price + (1.0 - self._ema_trend_alpha) * self.ema_trend

    def _compute_ema_signal(self):
        if self.ema_fast > self.ema_slow > self.ema_trend:
            self.ema_signal = 1.0
        elif self.ema_fast > self.ema_slow:
            self.ema_signal = 0.5
        elif self.ema_fast < self.ema_slow < self.ema_trend:
            self.ema_signal = -1.0
        elif self.ema_fast < self.ema_slow:
            self.ema_signal = -0.5
        else:
            self.ema_signal = 0.0

    # ── Bollinger Bands ─────────────────────────────────────────────────
    def _update_bollinger(self):
        n = min(len(self.prices), BB_PERIOD)
        if n < 5:
            return
        # Use last n prices
        recent = [p for _, p in list(self.prices)[-n:]]
        sma = sum(recent) / n
        variance = sum((p - sma) ** 2 for p in recent) / n
        std = math.sqrt(variance) if variance > 0 else 0.0

        self.bb_sma = sma
        self.bb_upper = sma + BB_STD_MULT * std
        self.bb_lower = sma - BB_STD_MULT * std

        band_range = self.bb_upper - self.bb_lower
        if band_range > 0:
            self.bb_pct_b = (recent[-1] - self.bb_lower) / band_range
        else:
            self.bb_pct_b = 0.5

        if sma > 0:
            self.bb_bandwidth = band_range / sma
        else:
            self.bb_bandwidth = 0.0

    def _compute_bollinger_signal(self):
        b = self.bb_pct_b
        if b > 1.0:
            self.bollinger_signal = -1.0
        elif b > 0.8:
            self.bollinger_signal = -0.5
        elif b < 0.0:
            self.bollinger_signal = 1.0
        elif b < 0.2:
            self.bollinger_signal = 0.5
        else:
            self.bollinger_signal = 0.0

    # ── MACD ────────────────────────────────────────────────────────────
    def _update_macd(self, price: float):
        if not self._macd_initialized:
            self.macd_ema_fast = price
            self.macd_ema_slow = price
            self.macd_line = 0.0
            self.macd_signal_line = 0.0
            self.macd_histogram = 0.0
            self._macd_initialized = True
            return

        self.macd_ema_fast = self._macd_fast_alpha * price + (1.0 - self._macd_fast_alpha) * self.macd_ema_fast
        self.macd_ema_slow = self._macd_slow_alpha * price + (1.0 - self._macd_slow_alpha) * self.macd_ema_slow
        self.macd_line = self.macd_ema_fast - self.macd_ema_slow
        self.macd_signal_line = self._macd_signal_alpha * self.macd_line + (1.0 - self._macd_signal_alpha) * self.macd_signal_line

        self.macd_prev_histogram = self.macd_histogram
        self.macd_histogram = self.macd_line - self.macd_signal_line

    def _compute_macd_signal(self):
        h = self.macd_histogram
        h_prev = self.macd_prev_histogram
        if abs(h) < 1e-9:
            self.macd_signal = 0.0
        elif h > 0 and h > h_prev:
            self.macd_signal = 1.0   # bullish momentum accelerating
        elif h > 0:
            self.macd_signal = 0.5   # bullish momentum
        elif h < 0 and h < h_prev:
            self.macd_signal = -1.0  # bearish momentum accelerating
        else:
            self.macd_signal = -0.5  # bearish momentum

    # ── RSI (Wilder smoothing) ──────────────────────────────────────────
    def _update_rsi(self, price: float):
        if self._rsi_count == 0:
            self._rsi_prev_price = price
            self._rsi_count = 1
            return

        delta = price - self._rsi_prev_price
        self._rsi_prev_price = price
        gain = max(0.0, delta)
        loss = max(0.0, -delta)
        self._rsi_count += 1

        if not self._rsi_initialized:
            # Accumulate for initial SMA-style seed
            self.rsi_avg_gain += gain
            self.rsi_avg_loss += loss
            if self._rsi_count >= RSI_PERIOD + 1:
                self.rsi_avg_gain /= RSI_PERIOD
                self.rsi_avg_loss /= RSI_PERIOD
                self._rsi_initialized = True
        else:
            # Wilder smoothing: avg = (prev_avg * (n-1) + current) / n
            self.rsi_avg_gain = (self.rsi_avg_gain * (RSI_PERIOD - 1) + gain) / RSI_PERIOD
            self.rsi_avg_loss = (self.rsi_avg_loss * (RSI_PERIOD - 1) + loss) / RSI_PERIOD

        if self._rsi_initialized:
            if self.rsi_avg_loss > 0:
                rs = self.rsi_avg_gain / self.rsi_avg_loss
                self.rsi_value = 100.0 - (100.0 / (1.0 + rs))
            elif self.rsi_avg_gain > 0:
                self.rsi_value = 100.0
            else:
                self.rsi_value = 50.0

    def _compute_rsi_signal(self):
        rsi = self.rsi_value
        if rsi > 80:
            self.rsi_signal = -1.0
        elif rsi > RSI_OVERBOUGHT:
            self.rsi_signal = -0.5
        elif rsi < 20:
            self.rsi_signal = 1.0
        elif rsi < RSI_OVERSOLD:
            self.rsi_signal = 0.5
        else:
            self.rsi_signal = 0.0

    # ── VWAP (time-weighted pseudo) ─────────────────────────────────────
    def _update_vwap(self, price: float, timestamp: float):
        # Use an exponential time-decay weight (recent prices matter more)
        # Weight = 1.0 for simplicity (uniform TWAP), with recency bias via EMA blending
        weight = 1.0
        self.vwap_cum_pv += price * weight
        self.vwap_cum_v += weight
        if self.vwap_cum_v > 0:
            self.vwap_value = self.vwap_cum_pv / self.vwap_cum_v

    def _compute_vwap_signal(self, price: float):
        if self.vwap_value <= 0:
            self.vwap_signal = 0.0
            return
        ratio = price / self.vwap_value
        if ratio < (1.0 - VWAP_DEVIATION_THRESHOLD * 2):
            self.vwap_signal = 1.0
        elif ratio < (1.0 - VWAP_DEVIATION_THRESHOLD):
            self.vwap_signal = 0.5
        elif ratio > (1.0 + VWAP_DEVIATION_THRESHOLD * 2):
            self.vwap_signal = -1.0
        elif ratio > (1.0 + VWAP_DEVIATION_THRESHOLD):
            self.vwap_signal = -0.5
        else:
            self.vwap_signal = 0.0

    # ── Regime detection ────────────────────────────────────────────────
    def _detect_regime(self):
        if self.bb_bandwidth > VOLATILE_BB_BANDWIDTH_THRESHOLD:
            candidate = "VOLATILE"
        elif self.ema_slow > 0 and abs(self.ema_fast - self.ema_slow) / self.ema_slow > TRENDING_EMA_DIVERGENCE:
            candidate = "TRENDING"
        else:
            candidate = "RANGING"

        # Hysteresis: only change regime after N consecutive confirmations
        if candidate != self.regime:
            self._regime_counter += 1
            if self._regime_counter >= REGIME_HYSTERESIS_TICKS:
                self.regime = candidate
                self._regime_counter = 0
        else:
            self._regime_counter = 0

    # ── Composite signal ────────────────────────────────────────────────
    def _compute_composite(self):
        w = ALPHA_WEIGHTS.get(self.regime, ALPHA_WEIGHTS["RANGING"])
        raw = (
            w["ema"] * self.ema_signal +
            w["bollinger"] * self.bollinger_signal +
            w["macd"] * self.macd_signal +
            w["rsi"] * self.rsi_signal +
            w["vwap"] * self.vwap_signal
        )
        # Clamp
        raw = max(-1.0, min(1.0, raw))
        # Dead zone
        if abs(raw) < ALPHA_COMPOSITE_DEADZONE:
            raw = 0.0
        self.composite_alpha = raw

    # ── Utility: signal agreement ───────────────────────────────────────
    def signal_agreement(self) -> float:
        """
        Returns 0.0-1.0 measuring how much the individual signals agree.
        1.0 = all signals point the same direction, 0.0 = total disagreement.
        """
        signals = [self.ema_signal, self.bollinger_signal, self.macd_signal,
                    self.rsi_signal, self.vwap_signal]
        if all(abs(s) < 0.01 for s in signals):
            return 0.5  # all neutral = moderate agreement

        positive = sum(1 for s in signals if s > 0.1)
        negative = sum(1 for s in signals if s < -0.1)
        total_active = positive + negative
        if total_active == 0:
            return 0.5
        majority = max(positive, negative)
        return majority / total_active


# =============================================================================
# MARKET MAKER CLASS - ULTRA OPTIMIZED
# =============================================================================

class MarketMaker:
    def __init__(self):
        self.wallet = eth_account.Account.from_key(AGENT_PRIVATE_KEY)
        self.info = Info(BASE_URL, skip_ws=True, perp_dexs=[PERP_DEX])
        if not DASHBOARD_ONLY_MODE:
            self.exchange = Exchange(
                self.wallet,
                base_url=BASE_URL,
                account_address=ACCOUNT_ADDRESS,
                perp_dexs=[PERP_DEX]
            )
        else:
            self.exchange = None
        
        # WebSocket State
        self.ws_info = None
        self.latest_bbo = None
        self.latest_position = 0.0
        self.latest_unrealized_pnl = 0.0
        self.ws_ready = False
        
        self.running = True
        self.buy_oids: list[int] = []
        self.sell_oids: list[int] = []
        
        # State tracking for Smart Refresh
        self.last_bbo = None
        self.last_position = None
        self.last_mid_price = None
        self.last_equity = 0.0
        
        # === SMART ORDER TRACKING ===
        # Prix d'ancrage = prix mid quand les ordres ont été placés
        self.orders_anchor_price = None  # Mid price au moment du placement
        self.orders_anchor_position = None  # Position au moment du placement
        self.orders_best_bid = None  # Meilleur bid de nos ordres
        self.orders_best_ask = None  # Meilleur ask de nos ordres
        
        # === ENTRY PRICE TRACKING pour Stop Loss ===
        self.entry_price = None  # Prix d'entrée moyen de la position
        self.position_margin = 0.0  # Margin utilisée
        
        # === Advanced Tracking ===
        self.price_history: deque = deque(maxlen=VOLATILITY_WINDOW)
        self.fill_history: deque = deque(maxlen=100)  # Track recent fills for fill rate
        self.volatility = 0.0
        self.realized_spread = 0.0
        self.fill_rate = 0.0
        self.adverse_selection_score = 0.0
        self.last_fill_time = 0
        self.consecutive_losses = 0
        self.best_pnl_session = 0.0
        self.last_stop_reason = ""
        self.order_details = {}
        self.session_start_timestamp = int(time.time() * 1000)

        # Execution quality tracking
        self.maker_fills = 0
        self.taker_fills = 0
        self.slippage_bps_history: deque = deque(maxlen=400)
        self.inventory_notional_history: deque = deque(maxlen=600)
        self.toxic_loss_usd = 0.0
        self.useful_volume_usd = 0.0

        # Quote/cancel cadence control
        self.last_quote_update_ts = 0.0
        self.quote_lifetimes: deque = deque(maxlen=400)
        self.cancel_events: deque = deque(maxlen=300)
        self.replace_events: deque = deque(maxlen=300)
        self.last_replace_mid = None

        # Feed quality / latency
        self.last_ws_msg_ts = 0.0
        self.feed_gap_ms_history: deque = deque(maxlen=400)
        self.latest_levels = None
        self.last_api_pos_sync_ts = 0.0

        # Toxic flow control
        self.toxic_events: deque = deque(maxlen=300)
        self.toxic_pause_until = 0.0
        self.last_auto_tune_ts = time.time()
        self.score_lambda = SCORE_LAMBDA_COST
        self.score_gamma = SCORE_GAMMA_INVENTORY
        self.score_tau = SCORE_TAU_TOXIC
        self.last_close_attempt_ts = 0.0
        self.last_account_info_ts = 0.0
        self.cached_account_info = (0.0, 0.0, 0.0)
        self.dynamic_size_scale = 1.0
        self.margin_reject_pause_until = 0.0
        self.roe_stop_active = False
        self.last_roe_stop_attempt_ts = 0.0
        
        # === ALPHA SIGNAL ENGINE ===
        self.alpha_engine = AlphaSignalEngine()

        # === ROUND-TRIP TRACKING (profit-first) ===
        self.pending_round_trips: list = []  # [{side:'buy', price:X, size:S, time:T}]
        self.completed_round_trips: deque = deque(maxlen=200)  # [{buy_px, sell_px, size, profit, time}]
        self.round_trip_count = 0
        self.round_trip_profit_total = 0.0
        self.last_fill_side = None  # 'buy' or 'sell' - last fill direction
        self.last_fill_time_rt = 0.0  # Time of last fill (for round-trip wait)
        self.waiting_for_round_trip = False  # True = don't refresh, wait for opposite fill

        # === ADAPTIVE SPREAD from round-trip performance ===
        self.rt_spread_adjustment = 0  # Extra ticks added if RT ratio is bad

        # Load Configuration
        self.load_config()
        
    def load_config(self):
        """Load configuration from global constants"""
        self.strategy_name = "MM PROFIT-FIRST v4.0"
        self.strategy_desc = f"Levier: {TARGET_LEVERAGE}x | Grid: {NUM_LEVELS} lvls | Profit Mode"
        
        # Refresh settings - LOGIQUE BASÉE SUR DRIFT DE PRIX
        self.BASE_REFRESH_INTERVAL = REFRESH_SPEED_SLOW  # Polling de base
        self.URGENT_REFRESH_INTERVAL = REFRESH_SPEED_FAST
        self.current_refresh_interval = self.BASE_REFRESH_INTERVAL
        
        # Seuils de drift en ticks
        self.PRICE_DRIFT_TICKS = PRICE_DRIFT_THRESHOLD_TICKS
        self.URGENT_DRIFT_TICKS = URGENT_DRIFT_THRESHOLD_TICKS
        
        # MM parameters
        self.NUM_LEVELS = NUM_LEVELS
        self.LEVEL_TICK_SPACING = LEVEL_TICK_SPACING
        self.LEVEL_MULTIPLIERS = LEVEL_MULTIPLIERS
        self.STOP_LOSS_THRESHOLD = STOP_LOSS_USD
        self.POSITION_SKEW_FACTOR = POSITION_SKEW_FACTOR
        self.MIN_SPREAD_TICKS = MIN_SPREAD_TICKS
        
        # Dynamic spread state
        self.current_spread_ticks = BASE_SPREAD_TICKS
        
        # Stats tracking
        self.hourly_volume_history = []
        self.target_leverage = TARGET_LEVERAGE
        
        # Stats tracking
        self.total_volume = 0.0
        self.total_fees = 0.0
        self.session_pnl = 0.0
        self.fills_count = 0
        self.processed_fill_ids = set()
        self.session_start_timestamp = int(time.time() * 1000)
        
        # Real-time WebSocket State
        self.ws_ready = False
        self.latest_bbo = None
        self.latest_position = 0.0
        self.latest_unrealized_pnl = 0.0
        self.ws_equity = 0.0
        self.open_oids_state = set()
        self.console = Console()
        self.start_time = time.time()
        self.last_status = "Initializing..."
        self.current_spread = 0.0
        
        # Real-time Event Event
        self.feed_event = threading.Event()
        self.last_sync_time = 0
        self.orders_need_sync = False
        
        # Setup graceful shutdown
        signal.signal(signal.SIGINT, self._shutdown)
        signal.signal(signal.SIGTERM, self._shutdown)

        if US_MARKET_MODE:
            self._start_websocket()
    
    def _start_websocket(self):
        """Initialize WebSocket connection for real-time updates"""
        self.console.print("[bold magenta]⚡ Starting High-Speed WebSocket Feed...[/]")
        self.ws_info = Info(BASE_URL, skip_ws=False, perp_dexs=[PERP_DEX])
        
        # Subscribe to L2 Book for BBO (utiliser SYMBOL complet avec préfixe)
        self.ws_info.subscribe({"type": "l2Book", "coin": SYMBOL}, self._handle_l2_update)
        # Subscribe to User events for fills/position
        self.ws_info.subscribe({"type": "userEvents", "user": ACCOUNT_ADDRESS}, self._handle_user_update)
        
        self.ws_ready = True

    def _handle_l2_update(self, data):
        """Callback for L2 Book updates with volatility tracking + Alpha Signal Engine"""
        try:
            # Handle both 'data' wrapper and direct payload
            payload = data.get("data", data)
            levels = payload.get("levels", [])
            if len(levels) >= 2:
                # Levels structure: [[{'px': 'px', 'sz': 'sz'}, ...], [...]]
                best_bid = float(levels[0][0]["px"])
                best_ask = float(levels[1][0]["px"])
                mid = (best_bid + best_ask) / 2
                self.latest_bbo = (best_bid, best_ask, mid)
                if not self.ws_ready: self.ws_ready = True
                self.latest_levels = levels

                now_ts = time.time()
                if self.last_ws_msg_ts > 0:
                    self.feed_gap_ms_history.append((now_ts - self.last_ws_msg_ts) * 1000.0)
                self.last_ws_msg_ts = now_ts
                
                # === VOLATILITY TRACKING ===
                self.price_history.append((time.time(), mid))
                if len(self.price_history) >= 5:
                    self._update_volatility()
                
                # === ALPHA SIGNAL ENGINE UPDATE ===
                if ALPHA_ENABLED:
                    self.alpha_engine.update(mid, now_ts)
                
                # Signal the main loop immediately!
                self.feed_event.set()
        except Exception as e:
            pass
    
    def _update_volatility(self):
        """Calculate rolling volatility from price history"""
        if len(self.price_history) < 5:
            return
        
        prices = [p[1] for p in self.price_history]
        returns = [(prices[i] - prices[i-1]) / prices[i-1] for i in range(1, len(prices))]
        
        if returns:
            # Standard deviation of returns (annualized factor not needed for relative comparison)
            mean_return = sum(returns) / len(returns)
            variance = sum((r - mean_return) ** 2 for r in returns) / len(returns)
            self.volatility = math.sqrt(variance) * 100  # As percentage
    
    def _p95(self, values: List[float]) -> float:
        if not values:
            return 0.0
        data = sorted(values)
        idx = min(len(data) - 1, int(0.95 * (len(data) - 1)))
        return data[idx]

    def _orderbook_imbalance(self) -> float:
        if not self.latest_levels or len(self.latest_levels) < 2:
            return 0.0
        try:
            bid_levels = self.latest_levels[0][:L2_IMBALANCE_LEVELS]
            ask_levels = self.latest_levels[1][:L2_IMBALANCE_LEVELS]
            bid_sum = sum(float(x.get("sz", 0)) for x in bid_levels)
            ask_sum = sum(float(x.get("sz", 0)) for x in ask_levels)
            den = bid_sum + ask_sum
            if den <= 0:
                return 0.0
            return (bid_sum - ask_sum) / den
        except Exception:
            return 0.0

    def _avg_feed_gap_ms(self) -> float:
        if not self.feed_gap_ms_history:
            return 0.0
        return sum(self.feed_gap_ms_history) / len(self.feed_gap_ms_history)

    def _fill_rate_per_min(self) -> float:
        elapsed = max(time.time() - self.start_time, 60.0)
        return self.fills_count / (elapsed / 60.0)

    def _fill_probability_estimate(self, spread_ticks: float) -> float:
        fills_pm = self._fill_rate_per_min()
        spread_penalty = max(0.0, spread_ticks - 1.0) * 0.08
        raw = 0.45 + 0.06 * fills_pm - spread_penalty
        return min(0.95, max(0.05, raw))

    def _inventory_risk_usd(self, mid_price: float) -> float:
        if abs(self.latest_position) < 1e-9 or mid_price <= 0:
            return 0.0
        return abs(self.latest_position * mid_price)

    def _toxic_score(self) -> float:
        now_ts = time.time()
        while self.toxic_events and (now_ts - self.toxic_events[0][0]) > TOXIC_FLOW_WINDOW_SEC:
            self.toxic_events.popleft()
        if not self.toxic_events:
            return 0.0
        losses = [ev[1] for ev in self.toxic_events]
        return min(2.0, sum(losses) / max(len(losses), 1))

    def _compute_inventory_skew(self, mid_price: float, position: float) -> float:
        if mid_price <= 0:
            return 0.0
        max_pos_units = max(MAX_POSITION_USD / mid_price, MIN_SIZE)
        q = max(-1.5, min(1.5, position / max(max_pos_units, 1e-9)))
        sigma = max(0.00005, self.volatility / 100.0)
        horizon = 2.0
        skew_abs = (self.score_gamma * (sigma ** 2) * horizon + self.POSITION_SKEW_FACTOR * 0.0008) * abs(q) * mid_price
        skew_abs = min(skew_abs, 4.0 * TICK_SIZE)
        inv_skew = skew_abs if position > 0 else -skew_abs

        # === ALPHA SKEW (bullish → fair price up → buy more aggressively) ===
        if ALPHA_ENABLED and self.alpha_engine.tick_count >= ALPHA_MIN_TICKS_FOR_SIGNALS:
            alpha = self.alpha_engine.composite_alpha
            alpha_skew = alpha * TICK_SIZE * ALPHA_SKEW_INTENSITY
            # Dampen alpha when it would reinforce an already-large position
            if position > 0 and alpha > 0:
                alpha_skew *= 0.3
            elif position < 0 and alpha < 0:
                alpha_skew *= 0.3
            inv_skew += alpha_skew

        return inv_skew

    def _update_rt_spread_adjustment(self):
        """Adjust spread based on round-trip performance.
        If RT ratio < 50% or avg RT profit < 0, widen the spread."""
        if self.fills_count < 6:
            self.rt_spread_adjustment = 0
            return

        # Round-trip ratio = completed RTs / total fills * 2 (each RT = 2 fills)
        if self.fills_count > 0 and self.round_trip_count > 0:
            rt_ratio = (self.round_trip_count * 2) / self.fills_count
        else:
            rt_ratio = 0.0

        # Average RT profit
        if self.round_trip_count > 0:
            avg_rt_profit = self.round_trip_profit_total / self.round_trip_count
        else:
            avg_rt_profit = 0.0

        adjustment = 0
        # If RT ratio < 50%, widen spread
        if rt_ratio < 0.5 and self.round_trip_count >= 3:
            adjustment += 1
        if rt_ratio < 0.3 and self.round_trip_count >= 5:
            adjustment += 1
        # If avg RT profit negative, widen more
        if avg_rt_profit < 0 and self.round_trip_count >= 3:
            adjustment += 1
        if avg_rt_profit < -0.005 and self.round_trip_count >= 5:
            adjustment += 1

        self.rt_spread_adjustment = min(adjustment, 4)  # Cap at +4 ticks

    def get_round_trip_stats(self) -> dict:
        """Get round-trip performance stats for dashboard."""
        if self.round_trip_count == 0:
            return {"count": 0, "ratio": 0.0, "avg_profit": 0.0, "total_profit": 0.0}
        rt_ratio = (self.round_trip_count * 2) / max(self.fills_count, 1)
        avg_profit = self.round_trip_profit_total / self.round_trip_count
        return {
            "count": self.round_trip_count,
            "ratio": min(rt_ratio, 1.0),
            "avg_profit": avg_profit,
            "total_profit": self.round_trip_profit_total,
            "pending": len(self.pending_round_trips)
        }

    def _compute_dynamic_half_spread(self, mid_price: float, best_bid: float, best_ask: float) -> float:
        tick = TICK_SIZE
        base_ticks = float(BASE_SPREAD_TICKS)
        sigma = max(0.00005, self.volatility / 100.0)
        imbalance = self._orderbook_imbalance()
        latency_ms = self._avg_feed_gap_ms()
        toxic = self._toxic_score()

        vol_term = min(2.5, sigma * 6500.0)
        latency_term = min(1.5, latency_ms / LATENCY_WIDENING_MS_REF)
        imbalance_term = abs(imbalance) * 1.2

        rough_ticks = base_ticks + vol_term + latency_term + imbalance_term + toxic
        fill_prob = self._fill_probability_estimate(rough_ticks)
        fill_term = max(-0.8, (FILL_RATE_TARGET - fill_prob) * 2.0)

        spread_ticks = max(self.MIN_SPREAD_TICKS, min(MAX_SPREAD_TICKS + 2, rough_ticks + fill_term))
        market_ticks = max(1.0, (best_ask - best_bid) / max(tick, 1e-9))
        spread_ticks = max(spread_ticks, min(market_ticks + 1.0, MAX_SPREAD_TICKS + 2))

        # === ALPHA: Adjust spread based on regime & confidence ===
        if ALPHA_ENABLED and self.alpha_engine.tick_count >= ALPHA_MIN_TICKS_FOR_SIGNALS:
            alpha_confidence = abs(self.alpha_engine.composite_alpha)
            # High confidence → tighten spread (more volume)
            # Low confidence → widen (protection)
            confidence_adj = 1.0 - (alpha_confidence * 0.3)  # range 0.7 – 1.0
            if self.alpha_engine.regime == "VOLATILE":
                confidence_adj *= 1.3  # wider in volatile
            elif self.alpha_engine.regime == "TRENDING":
                confidence_adj *= 1.05  # slightly wider to avoid adverse selection
            spread_ticks = spread_ticks * confidence_adj
            # Re-clamp
            spread_ticks = max(self.MIN_SPREAD_TICKS, spread_ticks)

        # === ROUND-TRIP ADAPTIVE: widen if RT performance is bad ===
        spread_ticks += self.rt_spread_adjustment

        self.current_spread_ticks = int(round(spread_ticks))
        return max(tick / 2.0, (spread_ticks * tick) / 2.0)

    def _update_toxic_flow(self, fill_price: float, side_sign: int, fill_size: float):
        if not self.latest_bbo:
            return
        mid = self.latest_bbo[2]
        if mid <= 0:
            return
        adverse_bps = side_sign * (mid - fill_price) / mid * 10000.0
        toxic_loss = max(0.0, adverse_bps) * abs(fill_size) * mid / 10000.0
        self.toxic_loss_usd += toxic_loss
        normalized = adverse_bps / max(TOXIC_ADVERSE_BPS_TRIGGER, 1e-6)
        self.toxic_events.append((time.time(), max(0.0, normalized)))
        if self._toxic_score() >= MAX_TOXIC_SCORE_BEFORE_PAUSE:
            self.toxic_pause_until = max(self.toxic_pause_until, time.time() + TOXIC_FLOW_PAUSE_SEC)

    def _register_fill_slippage(self, oid: int, fill_price: float, side_sign: int):
        expected = None
        if oid in self.order_details:
            expected = float(self.order_details[oid].get("price", fill_price))
        if expected and expected > 0:
            slip_bps = side_sign * (fill_price - expected) / expected * 10000.0
            self.slippage_bps_history.append(slip_bps)

    def _auto_tune_score_weights(self):
        now_ts = time.time()
        if now_ts - self.last_auto_tune_ts < AUTO_TUNE_INTERVAL_SEC:
            return
        self.last_auto_tune_ts = now_ts
        p95_slippage = self._p95(list(self.slippage_bps_history)) if self.slippage_bps_history else 0.0
        inv_risk = self._inventory_risk_usd(self.latest_bbo[2]) if self.latest_bbo else 0.0
        toxic = self._toxic_score()

        if p95_slippage > MAX_P95_SLIPPAGE_BPS:
            self.score_lambda = min(MAX_SCORE_WEIGHT, self.score_lambda + AUTO_TUNE_STEP)
        else:
            self.score_lambda = max(MIN_SCORE_WEIGHT, self.score_lambda - AUTO_TUNE_STEP * 0.5)

        if inv_risk > MAX_POSITION_USD * 0.7:
            self.score_gamma = min(MAX_SCORE_WEIGHT, self.score_gamma + AUTO_TUNE_STEP)
        else:
            self.score_gamma = max(MIN_SCORE_WEIGHT, self.score_gamma - AUTO_TUNE_STEP * 0.5)

        if toxic > 0.5:
            self.score_tau = min(MAX_SCORE_WEIGHT, self.score_tau + AUTO_TUNE_STEP)
        else:
            self.score_tau = max(MIN_SCORE_WEIGHT, self.score_tau - AUTO_TUNE_STEP * 0.5)

    def _score_snapshot(self, mid_price: float) -> float:
        cost_usd = self.total_fees - self.session_pnl
        inv_usd = self._inventory_risk_usd(mid_price)
        return self.total_volume - self.score_lambda * cost_usd - self.score_gamma * inv_usd - self.score_tau * self.toxic_loss_usd

    def _vlog(self, msg: str):
        if VERBOSE_RUNTIME_UPDATES:
            print(msg)

    def _is_flat_position(self, pos: float) -> bool:
        eps = max(0.0001, MIN_SIZE * 0.5)
        return abs(pos) < eps

    def _effective_unrealized_pnl(self, mid_price: float, position: float, ws_unrealized: float, api_unrealized: Optional[float]) -> float:
        """
        Build a conservative unrealized PnL for risk decisions.
        We intentionally pick the most negative available estimate to avoid missing stop-loss.
        """
        candidates = []
        if ws_unrealized is not None:
            candidates.append(float(ws_unrealized))
        if api_unrealized is not None:
            candidates.append(float(api_unrealized))
        if self.entry_price and self.entry_price > 0 and abs(position) > 0.0001 and mid_price > 0:
            candidates.append((mid_price - self.entry_price) * position)
        if not candidates:
            return 0.0
        return min(candidates)

    def _calculate_adaptive_spread(self) -> int:
        """
        Calculate spread based on volatility, fill rate, and adverse selection
        Returns spread in ticks
        """
        if not SMART_SPREAD_ENABLED:
            return BASE_SPREAD_TICKS
        if not self.latest_bbo:
            return BASE_SPREAD_TICKS
        best_bid, best_ask, mid = self.latest_bbo
        half_spread = self._compute_dynamic_half_spread(mid, best_bid, best_ask)
        return max(MIN_SPREAD_TICKS, int(round((half_spread * 2.0) / max(TICK_SIZE, 1e-9))))

    def _handle_user_update(self, data):
        """Callback for User events - Full State Tracking with PnL Analysis"""
        try:
            payload = data.get("data", {})
            
            # Update Position from UserState if provided
            if "clearinghouseState" in payload:
                ch = payload["clearinghouseState"]
                for entry in ch.get("assetPositions", []):
                    pos = entry.get("position", {})
                    if pos.get("coin") == SYMBOL or SYMBOL.endswith(pos.get("coin", "")):
                        self.latest_position = float(pos.get("szi", 0))
                        self.latest_unrealized_pnl = float(pos.get("unrealizedPnl", 0))
                        # === TRACK ENTRY PRICE pour Stop Loss ===
                        if abs(self.latest_position) > 0.0001:
                            self.entry_price = float(pos.get("entryPx", 0))
                            self.position_margin = float(pos.get("marginUsed", 0))
                        else:
                            self.entry_price = None
                            self.position_margin = 0.0
                
                margin = ch.get("marginSummary", {})
                self.ws_equity = float(margin.get("accountValue", 0))

            # Update Fills & Volume with ROUND-TRIP Tracking
            if "fills" in payload:
                for fill in payload["fills"]:
                    if fill.get("coin") == SYMBOL or SYMBOL.endswith(fill.get("coin", "")):
                        tid = str(fill.get("tid"))
                        oid = int(fill.get("oid", 0))

                        if tid not in self.processed_fill_ids:
                            self.processed_fill_ids.add(tid)
                            px, sz = float(fill["px"]), float(fill["sz"])
                            fee = float(fill.get("fee", 0))
                            closed_pnl = float(fill.get("closedPnl", 0))

                            self.total_volume += px * sz
                            self.fills_count += 1
                            self.total_fees += abs(fee)
                            self.session_pnl += closed_pnl

                            # Determine fill side
                            direction = str(fill.get("dir", ""))
                            is_buy_fill = ("Long" in direction or "Buy" in direction)
                            side_sign = 1 if is_buy_fill else -1
                            fill_side = "buy" if is_buy_fill else "sell"
                            now_t = time.time()

                            # === ROUND-TRIP TRACKING ===
                            # When we get a fill, check if it completes a pending round-trip
                            rt_matched = False
                            if fill_side == "sell" and self.pending_round_trips:
                                # Look for a matching buy to complete round-trip
                                for i, prt in enumerate(self.pending_round_trips):
                                    if prt["side"] == "buy":
                                        rt_profit = (px - prt["price"]) * min(sz, prt["size"]) - abs(fee)
                                        self.completed_round_trips.append({
                                            "buy_px": prt["price"],
                                            "sell_px": px,
                                            "size": min(sz, prt["size"]),
                                            "profit": rt_profit,
                                            "time": now_t,
                                            "duration": now_t - prt["time"]
                                        })
                                        self.round_trip_count += 1
                                        self.round_trip_profit_total += rt_profit
                                        self.pending_round_trips.pop(i)
                                        rt_matched = True
                                        break
                            elif fill_side == "buy" and self.pending_round_trips:
                                # Look for a matching sell to complete round-trip
                                for i, prt in enumerate(self.pending_round_trips):
                                    if prt["side"] == "sell":
                                        rt_profit = (prt["price"] - px) * min(sz, prt["size"]) - abs(fee)
                                        self.completed_round_trips.append({
                                            "buy_px": px,
                                            "sell_px": prt["price"],
                                            "size": min(sz, prt["size"]),
                                            "profit": rt_profit,
                                            "time": now_t,
                                            "duration": now_t - prt["time"]
                                        })
                                        self.round_trip_count += 1
                                        self.round_trip_profit_total += rt_profit
                                        self.pending_round_trips.pop(i)
                                        rt_matched = True
                                        break

                            if not rt_matched:
                                # No match = new pending half of a round-trip
                                self.pending_round_trips.append({
                                    "side": fill_side,
                                    "price": px,
                                    "size": sz,
                                    "time": now_t
                                })
                                # Trim old pending RTs (>5 min old = unlikely to complete)
                                self.pending_round_trips = [
                                    p for p in self.pending_round_trips
                                    if (now_t - p["time"]) < 300
                                ]

                            # Track fill for analysis
                            self.fill_history.append({
                                'time': now_t,
                                'price': px,
                                'size': sz,
                                'pnl': closed_pnl,
                                'oid': oid,
                                'fee': fee,
                                'side': fill_side,
                                'rt_matched': rt_matched
                            })
                            self.last_fill_time = now_t
                            self.last_fill_side = fill_side
                            self.last_fill_time_rt = now_t

                            self._register_fill_slippage(oid, px, side_sign)
                            self._update_toxic_flow(px, side_sign, sz)
                            if fee < 0:
                                self.maker_fills += 1
                            else:
                                self.taker_fills += 1
                            self.useful_volume_usd += max(0.0, px * sz - max(0.0, abs(fee)))

                            # Track consecutive losses
                            if closed_pnl < -0.01:
                                self.consecutive_losses += 1
                            else:
                                self.consecutive_losses = 0

                            # Track best PnL
                            self.best_pnl_session = max(self.best_pnl_session, self.session_pnl)

                            # Remove from local tracked orders
                            if oid in self.buy_oids:
                                self.buy_oids.remove(oid)
                            if oid in self.sell_oids:
                                self.sell_oids.remove(oid)

                            # === ROUND-TRIP WAIT MODE ===
                            # After a fill, KEEP opposite orders alive, enter wait mode
                            if not rt_matched:
                                # We have a new pending RT - enter wait mode
                                self.waiting_for_round_trip = True
                            else:
                                # RT completed! Can refresh normally
                                self.waiting_for_round_trip = False

                            # === ADAPTIVE SPREAD based on RT performance ===
                            self._update_rt_spread_adjustment()

                            # === REQUOTE LOGIC (profit-first) ===
                            if REQUOTE_ON_FILL:
                                # Old aggressive behavior
                                self.orders_need_sync = True
                                self.feed_event.set()
                            else:
                                # PROFIT MODE: Do NOT cancel opposite side
                                # Only signal if ALL orders on one side are gone
                                if not self.buy_oids or not self.sell_oids:
                                    # One side is empty - need to replenish
                                    # But still wait for round-trip cooldown
                                    pass  # Main loop handles this via periodic sync
            
            self.ws_ready = True
        except Exception:
            pass
    
    def _shutdown(self, signum, frame):
        """Graceful shutdown - cancel all orders and close positions"""
        self.emergency_full_stop(reason="Signal/Ctrl+C")

    def print_session_summary(self):
        """Print end-of-session execution and cost summary."""
        elapsed_sec = max(time.time() - self.start_time, 1.0)
        elapsed_min = elapsed_sec / 60.0
        elapsed_hr = elapsed_sec / 3600.0
        mid_price = self.latest_bbo[2] if self.latest_bbo else 0.0

        net_cost_usd = self.total_fees - self.session_pnl
        cost_per_m = net_cost_usd / (self.total_volume / 1_000_000) if self.total_volume > 0 else 0.0
        net_cost_bps = (net_cost_usd / self.total_volume * 10000.0) if self.total_volume > 0 else 0.0
        maker_ratio = (self.maker_fills / max(self.maker_fills + self.taker_fills, 1)) * 100.0
        p95_slip = self._p95(list(self.slippage_bps_history)) if self.slippage_bps_history else 0.0
        fills_per_min = self.fills_count / elapsed_min
        inv_notional = abs(self.latest_position * mid_price) if mid_price > 0 else 0.0

        rt_stats = self.get_round_trip_stats()
        pnl_per_hour = self.session_pnl / max(elapsed_hr, 0.01)

        print("\n" + "=" * 78)
        print(f"SESSION SUMMARY (PROFIT-FIRST) | {SYMBOL}")
        print("=" * 78)
        print(f"Runtime            : {elapsed_hr:.2f}h")
        print(f"Stop reason        : {self.last_stop_reason or 'N/A'}")
        print(f"--- PROFIT METRICS ---")
        print(f"PnL session        : ${self.session_pnl:,.4f}")
        print(f"PnL/hour           : ${pnl_per_hour:,.4f}")
        print(f"Round-trips        : {rt_stats['count']} completed ({rt_stats['ratio']:.0%} ratio)")
        print(f"RT total profit    : ${rt_stats['total_profit']:,.4f}")
        print(f"RT avg profit      : ${rt_stats['avg_profit']:,.4f}")
        print(f"--- EXECUTION ---")
        print(f"Volume total       : ${self.total_volume:,.2f}")
        print(f"Fees totales       : ${self.total_fees:,.4f}")
        print(f"Net cost           : ${net_cost_usd:,.4f} | {net_cost_bps:.2f} bps")
        print(f"Cost per million   : ${cost_per_m:,.2f}")
        print(f"Fills              : {self.fills_count} | {fills_per_min:.2f}/min")
        print(f"Maker ratio        : {maker_ratio:.2f}%")
        print(f"P95 slippage       : {p95_slip:.2f} bps")
        print(f"Inventory notional : ${inv_notional:,.2f}")
        print(f"Toxic loss (est.)  : ${self.toxic_loss_usd:,.4f}")
        print("=" * 78 + "\n")

    def emergency_full_stop(self, reason: str = "Unknown"):
        """
        ROBUST SHUTDOWN PROCEDURE
        1. Cancel tracked orders
        2. Scan API for orphan orders
        3. Close position via API state with VERIFICATION LOOP
        """
        print(f"\n⚠️  INITIATING EMERGENCY STOP: {reason}")
        self.last_stop_reason = reason
        self.running = False
        if DASHBOARD_ONLY_MODE:
            return
        # 1. Cancel all pending orders (Tracked ones first)
        self.cancel_all_orders()
        
        # 1.5. FORCE Cancel all open orders via API (To catch ghosts/limit orders)
        print("🔍 Scanning for remaining open orders on API...")
        self.cancel_all_open_orders_via_api()

        # 2. Securely close position with RETRIES
        print("Checking final position via API...")
        
        max_retries = 10  # Try for 10 times to ensure close
        for i in range(max_retries):
            # Fetch position directly to handle errors explicitly
            current_pos = 0.0
            fetch_success = False
            
            try:
                user_state = self.info.user_state(ACCOUNT_ADDRESS, dex=PERP_DEX)
                if "assetPositions" in user_state:
                    for pos in user_state["assetPositions"]:
                        coin = pos.get("position", {}).get("coin", "")
                        if coin == SYMBOL or SYMBOL.endswith(coin):
                            current_pos = float(pos["position"].get("szi", 0))
                            break
                fetch_success = True
            except Exception as e:
                print(f"⚠️ Failed to fetch position (Attempt {i+1}): {e}")
                time.sleep(0.5)
                # If we can't fetch, we can't be sure, but we might try blindly closing if we had a last known position?
                # For now, just continue and retry fetch
                continue 
            
            # Check if closed
            if fetch_success and abs(current_pos) < 0.0001:
                print(f"✅ Position is safely closed: {current_pos} {SYMBOL}")
                break
            
            if fetch_success:
                print(f"🚨 Closing remaining position: {current_pos} {SYMBOL} (Attempt {i+1}/{max_retries})")
                # CALL CLOSE
                self.close_position(known_position=current_pos, force=True, trigger=reason)
            
            # Wait a bit for fill and propagation
            print("⏳ Waiting for fill...")
            time.sleep(1.5)
        
        self.print_session_summary()
        print("✅ Shutdown sequence complete. Goodbye!")
        sys.exit(0)

    def cancel_all_open_orders_via_api(self):
        """Fetch ALL open orders from API and cancel the ones for our symbol"""
        try:
            open_orders = self.info.open_orders(ACCOUNT_ADDRESS)
            target_base = SYMBOL.split(':')[-1] # "US500"
            
            count = 0
            for order in open_orders:
                # Check if order belongs to our symbol (handle "km:US500" vs "US500")
                o_coin = order.get("coin", "")
                if o_coin == SYMBOL or o_coin == target_base:
                    oid = order.get("oid")
                    print(f"Cancelling orphan order {oid} ({o_coin})...")
                    self.exchange.cancel(o_coin, oid)
                    count += 1
            
            if count > 0:
                print(f"✅ Cancelled {count} orphan orders via API.")
            else:
                print("✅ No orphan orders found on API.")
                
        except Exception as e:
            print(f"⚠️ Failed to fetch/cancel API orders: {e}")
    
    def get_bbo(self) -> Optional[tuple[float, float, float]]:
        """Fetch current best bid, best ask, and mid price from order book"""
        try:
            # Get L2 order book
            book = self.info.l2_snapshot(SYMBOL)
            if book and "levels" in book:
                bids = book["levels"][0]  # [[price, size], ...]
                asks = book["levels"][1]
                
                if bids and asks:
                    best_bid = float(bids[0]["px"])
                    best_ask = float(asks[0]["px"])
                    mid = (best_bid + best_ask) / 2
                    return (best_bid, best_ask, mid)
        except Exception as e:
            pass  # Silent error for dashboard
        return None
    
    def get_position_and_pnl_with_status(self) -> tuple[bool, float, float]:
        """Return (api_ok, position, unrealized_pnl). api_ok=False only if all API fetches fail."""
        target_base = SYMBOL.split(':')[-1]
        states = []
        api_ok = False
        try:
            states.append(self.info.user_state(ACCOUNT_ADDRESS, dex=PERP_DEX))
        except Exception:
            pass
        try:
            states.append(self.info.user_state(ACCOUNT_ADDRESS))
        except Exception:
            pass

        for user_state in states:
            try:
                api_ok = True
                if "assetPositions" in user_state:
                    for pos in user_state["assetPositions"]:
                        p_coin = pos.get("position", {}).get("coin", "")
                        if p_coin == SYMBOL or p_coin == target_base or SYMBOL.endswith(p_coin):
                            size = float(pos["position"].get("szi", 0))
                            unrealized_pnl = float(pos["position"].get("unrealizedPnl", 0))
                            entry_px = float(pos["position"].get("entryPx", 0))
                            margin_used = float(pos["position"].get("marginUsed", 0))
                            if abs(size) > 0.0001:
                                if entry_px > 0:
                                    self.entry_price = entry_px
                                if margin_used > 0:
                                    self.position_margin = margin_used
                            return (True, size, unrealized_pnl)
            except Exception:
                continue

        if api_ok:
            # API responded and symbol not found -> flat for this symbol.
            return (True, 0.0, 0.0)
        return (False, 0.0, 0.0)

    def get_position_and_pnl(self) -> tuple[float, float]:
        """Get current position size and unrealized PNL for the symbol."""
        ok, pos, pnl = self.get_position_and_pnl_with_status()
        if ok:
            return (pos, pnl)
        # API unavailable: fallback to latest WS state instead of forcing flat.
        return (self.latest_position, self.latest_unrealized_pnl)
    
    def get_account_info(self) -> tuple[float, float, float]:
        """Get account equity, margin used, and available margin"""
        try:
            states = []
            try:
                states.append(self.info.user_state(ACCOUNT_ADDRESS, dex=PERP_DEX))
            except Exception:
                pass
            try:
                states.append(self.info.user_state(ACCOUNT_ADDRESS))
            except Exception:
                pass

            for user_state in states:
                # 1. Check Perps/Broker balance
                margin = (
                    user_state.get("marginSummary") or
                    user_state.get("crossMarginSummary") or
                    user_state.get("clearinghouseState", {}).get("marginSummary")
                )
                if margin and float(margin.get("accountValue", 0)) > 0:
                    equity = float(margin.get("accountValue", 0))
                    margin_used = float(margin.get("totalMarginUsed", 0))
                    available = equity - margin_used
                    return (equity, margin_used, available)
            
            # 2. Check Spot balance (common mistake: money not transferred)
            spot_state = self.info.spot_user_state(ACCOUNT_ADDRESS)
            spot_balances = spot_state.get("balances", [])
            total_spot = sum(float(b.get("total", 0)) for b in spot_balances if b.get("coin") == "USDC")
            
            if total_spot > 0:
                self.last_status = f"⚠️ Money is in SPOT (${total_spot:.1f}). Transfer to Perps!"
                return (0.0, 0.0, 0.0)
                
            self.last_status = "⚠️ Wallet $0.00 (Check Master Address)"
        except Exception as e:
            self.last_status = f"❌ Account Error: {str(e)[:20]}"
        return (0.0, 0.0, 0.0)

    def get_account_info_cached(self, force: bool = False) -> tuple[float, float, float]:
        now_ts = time.time()
        if force or (now_ts - self.last_account_info_ts) >= ACCOUNT_INFO_CACHE_SEC:
            self.cached_account_info = self.get_account_info()
            self.last_account_info_ts = now_ts
        return self.cached_account_info
    
    def fetch_and_update_fills(self):
        """Fetch recent fills from API and update volume/fees/PNL stats"""
        try:
            fills = self.info.user_fills(ACCOUNT_ADDRESS)
            target_base = SYMBOL.split(':')[-1] # "US500"
            
            for fill in fills:
                # Normalize coin name
                f_coin = fill.get("coin", "")
                if f_coin != SYMBOL and f_coin != target_base:
                    continue
                    
                fill_time = fill.get("time", 0)
                if fill_time < self.session_start_timestamp:
                    continue
                
                # Create unique fill ID using tid (transaction ID)
                fill_id = str(fill.get("tid", ""))
                
                if fill_id in self.processed_fill_ids:
                    continue
                
                # Process this fill
                self.processed_fill_ids.add(fill_id)
                
                # Extract fill data
                price = float(fill.get("px", 0))
                size = float(fill.get("sz", 0))
                fee = float(fill.get("fee", 0))
                closed_pnl = float(fill.get("closedPnl", 0))
                
                # Update stats
                trade_value = price * size
                self.total_volume += trade_value
                self.total_fees += abs(fee)
                self.session_pnl += closed_pnl
                self.fills_count += 1
                
                # Update status with last fill
                direction = fill.get("dir", "")
                if "Long" in direction:
                    self.last_status = f"🟢 FILL: {size} @ ${price:.2f}"
                else:
                    self.last_status = f"🔴 FILL: {size} @ ${price:.2f}"
                
        except Exception as e:
            pass  # Silent error for dashboard
    
    def get_position(self) -> float:
        """Get current position size for the symbol"""
        states = []
        try:
            states.append(self.info.user_state(ACCOUNT_ADDRESS, dex=PERP_DEX))
        except Exception:
            pass
        try:
            states.append(self.info.user_state(ACCOUNT_ADDRESS))
        except Exception:
            pass
        for user_state in states:
            try:
                if "assetPositions" in user_state:
                    for pos in user_state["assetPositions"]:
                        coin = pos.get("position", {}).get("coin", "")
                        if coin == SYMBOL or SYMBOL.endswith(coin):
                            return float(pos["position"].get("szi", 0))
            except Exception:
                continue
        return 0.0
        
    def calculate_dynamic_size(self, mid_price: float) -> float:
        """Calculate base order size based on equity and target leverage"""
        if mid_price == 0:
            return 0.01

        # 1. FIXED SIZE (Si configuré)
        if ORDER_SIZE_USD > 0:
            base_size = ORDER_SIZE_USD / mid_price
        else:
            # 2. DYNAMIC SIZE (Automatique)
            equity, margin_used, available = self.get_account_info_cached()
            if equity <= 0:
                equity = 10.0
            target_total_notional = equity * self.target_leverage * 0.99
            self.last_equity = equity
            sum_multipliers = sum(self.LEVEL_MULTIPLIERS)
            base_notional = target_total_notional / sum_multipliers
            base_size = base_notional / mid_price

        # Minimum executable size in units (exchange lot-size + practical notional floor).
        min_size_target = max(MIN_SIZE, 12.0 / mid_price)

        # 3. Margin cap (prevents insufficient-margin batch rejects)
        equity, margin_used, available = self.get_account_info_cached()
        if available > 0:
            # One-side fill assumption: cap side notional to available margin * leverage * headroom.
            max_side_notional = available * self.target_leverage * MARGIN_HEADROOM_PCT
            side_room_after_inventory = max(0.0, max_side_notional - abs(self.latest_position * mid_price))
            sum_multipliers = max(sum(self.LEVEL_MULTIPLIERS), 1e-9)
            max_base_notional = side_room_after_inventory / sum_multipliers
            max_base_size_by_margin = max_base_notional / max(mid_price, 1e-9)
            base_size = min(base_size, max_base_size_by_margin)

            if max_base_size_by_margin < min_size_target:
                return 0.0

        vol = max(0.0, self.volatility / 100.0)
        vol_factor = max(0.35, 1.0 - min(0.55, vol * 35.0))
        imbalance = abs(self._orderbook_imbalance())
        depth_factor = 1.0 - min(0.20, imbalance * 0.25)
        inv_notional = abs(self.latest_position * mid_price)
        inv_ratio = inv_notional / max(MAX_POSITION_USD, 1e-9)
        inventory_factor = max(0.30, 1.0 - min(0.60, inv_ratio * 0.75))
        toxic_factor = 0.8 if time.time() < self.toxic_pause_until else 1.0

        margin_reject_factor = 0.5 if time.time() < self.margin_reject_pause_until else 1.0
        adjusted = base_size * vol_factor * depth_factor * inventory_factor * toxic_factor * self.dynamic_size_scale * margin_reject_factor
        if adjusted <= 0:
            return 0.0
        return max(round(adjusted, SIZE_DECIMALS), round(min_size_target, SIZE_DECIMALS))

    def calculate_levels(self, best_bid: float, best_ask: float, position: float) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
        """
        ULTRA-OPTIMIZED Level Calculation with Avellaneda-Stoikov Model
        
        Objectifs:
        1. MAX VOLUME: Spreads serrés, beaucoup de niveaux
        2. MIN LOSS: Skew intelligent basé sur l'inventaire
        3. ADAPTATIF: Spread dynamique selon volatilité
        """
        tick = TICK_SIZE  # Utiliser le tick_size du token configuré
        mid_price = (best_bid + best_ask) / 2
        
        # === 1. DYNAMIC SPREAD CALCULATION ===
        adaptive_spread_ticks = self._calculate_adaptive_spread()
        self.current_spread_ticks = adaptive_spread_ticks
        
        # === 2. AVELLANEDA-STOIKOV FAIR PRICE ===
        # Formule: Fair = Mid - q * gamma * sigma^2 * T
        # Simplifié pour HFT: Fair = Mid - position * skew_factor
        
        # Skew borné et stable (Avellaneda-Stoikov simplifié)
        position_normalized = position * mid_price / 100  # Normaliser en $ centaines
        inventory_skew = self._compute_inventory_skew(mid_price, position)
        fair_price = mid_price - inventory_skew
        
        # === 3. SPREAD OPTIMAL ===
        # Base spread + volatility + fill/latency/toxic adjustments
        half_spread = self._compute_dynamic_half_spread(mid_price, best_bid, best_ask)
        
        # Start prices
        start_bid = fair_price - half_spread
        start_ask = fair_price + half_spread
        
        # === 4. SÉCURITÉ: Anti-crossing ===
        # Ne jamais croiser le spread du marché
        start_bid = min(start_bid, best_bid)
        start_ask = max(start_ask, best_ask)
        
        # Si on risque de cross, on recule
        if start_bid >= start_ask:
            spread_needed = tick * 2
            start_bid = fair_price - spread_needed / 2
            start_ask = fair_price + spread_needed / 2
        
        bids = []
        asks = []
        base_size = self.calculate_dynamic_size(mid_price)
        if base_size <= 0:
            return bids, asks
        
        # === 5. GÉNÉRATION DE LA GRILLE PYRAMIDALE ===
        for i in range(self.NUM_LEVELS):
            # Écartement progressif
            level_offset = i * self.LEVEL_TICK_SPACING * tick
            
            # Multiplicateur de taille (pyramidal - plus gros au centre)
            if i < len(self.LEVEL_MULTIPLIERS):
                multiplier = self.LEVEL_MULTIPLIERS[i]
            else:
                multiplier = self.LEVEL_MULTIPLIERS[-1] * 0.5
            
            # === SIZE BOOST basé sur la direction de l'inventaire ===
            # Si on est long, boost les asks pour vendre plus vite
            # Si on est short, boost les bids pour racheter plus vite
            exit_boost = 1.0
            if position > 0.01:  # Long position
                exit_boost = 1.0 + min(abs(position_normalized) * 0.2, 0.5) if i < 2 else 1.0
                # Reduce bid size when long
                bid_size_factor = max(0.3, 1.0 - abs(position_normalized) * 0.15)
                ask_size_factor = exit_boost
            elif position < -0.01:  # Short position
                exit_boost = 1.0 + min(abs(position_normalized) * 0.2, 0.5) if i < 2 else 1.0
                ask_size_factor = max(0.3, 1.0 - abs(position_normalized) * 0.15)
                bid_size_factor = exit_boost
            else:
                bid_size_factor = 1.0
                ask_size_factor = 1.0
            
            # Calcul Taille
            bid_size = round(base_size * multiplier * bid_size_factor, SIZE_DECIMALS)
            ask_size = round(base_size * multiplier * ask_size_factor, SIZE_DECIMALS)

            # === ALPHA-DRIVEN SIZE ASYMMETRY ===
            if ALPHA_ENABLED and self.alpha_engine.tick_count >= ALPHA_MIN_TICKS_FOR_SIGNALS:
                alpha = self.alpha_engine.composite_alpha
                if alpha > 0.3:
                    bid_size = round(bid_size * (1.0 + alpha * 0.4), SIZE_DECIMALS)
                    ask_size = round(ask_size * (1.0 - alpha * 0.3), SIZE_DECIMALS)
                elif alpha < -0.3:
                    bid_size = round(bid_size * (1.0 + alpha * 0.3), SIZE_DECIMALS)  # alpha neg → shrinks
                    ask_size = round(ask_size * (1.0 - alpha * 0.4), SIZE_DECIMALS)  # alpha neg → grows

            p_bid = round(start_bid - level_offset, PRICE_DECIMALS)
            p_ask = round(start_ask + level_offset, PRICE_DECIMALS)
            
            # Validation: prix positifs et tailles minimales
            if p_bid > 0 and bid_size >= MIN_SIZE:
                bids.append((p_bid, round(bid_size, SIZE_DECIMALS)))
            if p_ask > 0 and ask_size >= MIN_SIZE:
                asks.append((p_ask, round(ask_size, SIZE_DECIMALS)))
            
        # Toxic retreat mode: réduire agressivement les entrées si flow toxique.
        if time.time() < self.toxic_pause_until:
            if position > 0:
                bids = []
                asks = asks[:max(1, min(2, len(asks)))]
            elif position < 0:
                asks = []
                bids = bids[:max(1, min(2, len(bids)))]
            else:
                bids = bids[:1]
                asks = asks[:1]

        return bids, asks

    def cancel_all_orders(self):
        if DASHBOARD_ONLY_MODE:
            return
        """Cancel tracked orders using BULK CANCEL for speed"""
        if not self.buy_oids and not self.sell_oids:
            return []
            
        try:
            all_oids = self.buy_oids + self.sell_oids
            if not all_oids:
                return []

            # Prepare bulk cancel payload
            cancellations = []
            for oid in all_oids:
                cancellations.append({
                    "coin": SYMBOL,
                    "oid": oid
                })
            
            # Execute Bulk Cancel
            # self.exchange.bulk_cancel is faster than loop
            self.exchange.bulk_cancel(cancellations)
            cancel_ts = time.time()
            for _ in all_oids:
                self.cancel_events.append(cancel_ts)
            if self.last_quote_update_ts > 0:
                self.quote_lifetimes.append(cancel_ts - self.last_quote_update_ts)
            
            # Assume success for local state to unblock immediately
            self.buy_oids = []
            self.sell_oids = []
            self.order_details.clear()
            
            return all_oids
        except Exception as e:
            self.buy_oids = []
            self.sell_oids = []
            return []

    def close_position(self, known_position: Optional[float] = None, force: bool = False, trigger: str = "") -> bool:
        if DASHBOARD_ONLY_MODE:
            return False
        """Close any open position for the SYMBOL using real-time cached state"""
        now_ts = time.time()
        if not force and (now_ts - self.last_close_attempt_ts) < CLOSE_ATTEMPT_COOLDOWN_SEC:
            return False
        self.last_close_attempt_ts = now_ts

        # Use real-time WS state for instant reaction
        position = known_position if known_position is not None else self.latest_position
        if self._is_flat_position(position):
            return True

        trigger_txt = f" | trigger={trigger}" if trigger else ""
        if VERBOSE_RUNTIME_UPDATES:
            self.console.print(f"[bold red]💥 MARKET CLOSE: {position} {SYMBOL}{trigger_txt}[/]")

        # Some SDK paths expect "xyz:XYZ100", others "XYZ100". Try both.
        target_base = SYMBOL.split(":")[-1]
        close_candidates = [SYMBOL, target_base]

        # Phase 1: use market_close with both coin formats.
        for attempt in range(1, 4):
            for coin_name in close_candidates:
                try:
                    self._vlog(f"Attempting Market Close for {coin_name}... ({attempt}/3)")
                    res = self.exchange.market_close(coin_name)
                    if isinstance(res, dict):
                        status = res.get("status", "unknown")
                        self._vlog(f"Close Result: {status} | {res}")
                    else:
                        self._vlog(f"Close Result: <no response> | {res}")
                except Exception as e:
                    self._vlog(f"❌ market_close exception ({coin_name}): {e}")
                    continue

                time.sleep(0.25)
                check_pos, _ = self.get_position_and_pnl()
                if self._is_flat_position(check_pos):
                    self.last_status = "✅ Position closed"
                    return True

        # Phase 2 fallback: aggressive reduce-only IOC.
        try:
            bbo = self.latest_bbo or self.get_bbo()
            if not bbo:
                self.last_status = "⚠️ close fallback failed: no BBO"
                return False
            best_bid, best_ask, _ = bbo
            check_pos, _ = self.get_position_and_pnl()
            if self._is_flat_position(check_pos):
                self.last_status = "✅ Position closed"
                return True

            is_buy = check_pos < 0  # short -> buy to close; long -> sell to close
            close_size = round(abs(check_pos), SIZE_DECIMALS)
            px = best_ask + (10 * TICK_SIZE) if is_buy else best_bid - (10 * TICK_SIZE)
            px = round(max(px, TICK_SIZE), PRICE_DECIMALS)

            for coin_name in close_candidates:
                try:
                    self._vlog(f"Fallback reduce-only IOC on {coin_name}: {'BUY' if is_buy else 'SELL'} {close_size} @ {px}")
                    res = self.exchange.order(
                        name=coin_name,
                        is_buy=is_buy,
                        sz=close_size,
                        limit_px=px,
                        order_type={"limit": {"tif": "Ioc"}},
                        reduce_only=True,
                    )
                    self._vlog(f"Fallback IOC Result: {res}")
                except Exception as e:
                    self._vlog(f"❌ fallback IOC exception ({coin_name}): {e}")
                    continue

                time.sleep(0.25)
                recheck_pos, _ = self.get_position_and_pnl()
                if self._is_flat_position(recheck_pos):
                    self.last_status = "✅ Position closed (fallback IOC)"
                    return True

            self.last_status = "⚠️ market_close retries exhausted"
            return False
        except Exception as e:
            self.last_status = f"❌ Close Fail: {str(e)[:30]}"
            return False

    def place_order_batch(self, bids, asks, tif: str = "Gtc"):
        """Place all orders in a single batch request for TRUE speed"""
        if DASHBOARD_ONLY_MODE:
            return [], []
        if not bids and not asks:
            return [], []
            
        order_requests = []
        for p, s in bids:
            order_requests.append({
                "coin": SYMBOL,
                "is_buy": True,
                "sz": s,
                "limit_px": p,
                "order_type": {"limit": {"tif": tif}},
                "reduce_only": False
            })
            
        for p, s in asks:
            order_requests.append({
                "coin": SYMBOL,
                "is_buy": False,
                "sz": s,
                "limit_px": p,
                "order_type": {"limit": {"tif": tif}},
                "reduce_only": False
            })

        try:
            # USE BULK API
            result = self.exchange.bulk_orders(order_requests)
            new_buy_oids = []
            new_sell_oids = []
            insufficient_margin_errors = 0
            other_reject_errors = []
            
            if result.get("status") == "ok":
                data = result.get("response", {}).get("data", {})
                statuses = data.get("statuses", [])
                for i, st in enumerate(statuses):
                    if "resting" in st:
                        oid = st["resting"]["oid"]
                        req = order_requests[i]
                        if req["is_buy"]: new_buy_oids.append(oid)
                        else: new_sell_oids.append(oid)
                        self.order_details[oid] = {
                            "price": req["limit_px"],
                            "size": req["sz"],
                            "is_buy": req["is_buy"],
                            "ts": time.time(),
                        }
                    elif "error" in st:
                        err_text = str(st.get("error", ""))
                        err_l = err_text.lower()
                        if "insufficient" in err_l and "margin" in err_l:
                            insufficient_margin_errors += 1
                        else:
                            other_reject_errors.append(err_text if err_text else "unknown reject")
                if insufficient_margin_errors > 0:
                    self.dynamic_size_scale = max(
                        MIN_DYNAMIC_SIZE_SCALE,
                        self.dynamic_size_scale * MARGIN_REJECT_SIZE_DECAY,
                    )
                    self.margin_reject_pause_until = time.time() + MARGIN_REJECT_COOLDOWN_SEC
                    self.last_status = (
                        f"⚠️ Insufficient margin x{insufficient_margin_errors} | "
                        f"size_scale={self.dynamic_size_scale:.2f}"
                    )
                    self.get_account_info_cached(force=True)
                elif other_reject_errors and not new_buy_oids and not new_sell_oids:
                    sample = " | ".join(other_reject_errors[:2])
                    self.last_status = f"❌ Bulk rejects: {sample[:90]}"
                else:
                    self.dynamic_size_scale = min(1.0, self.dynamic_size_scale + MARGIN_SIZE_RECOVERY_STEP)
            else:
                err_msg = str(result.get("response"))
                if "insufficient" in err_msg.lower() and "margin" in err_msg.lower():
                    self.dynamic_size_scale = max(
                        MIN_DYNAMIC_SIZE_SCALE,
                        self.dynamic_size_scale * MARGIN_REJECT_SIZE_DECAY,
                    )
                    self.margin_reject_pause_until = time.time() + MARGIN_REJECT_COOLDOWN_SEC
                    self.last_status = f"⚠️ Bulk margin reject | size_scale={self.dynamic_size_scale:.2f}"
                    self.get_account_info_cached(force=True)
                else:
                    self.last_status = f"❌ Bulk Error: {str(result.get('response'))[:30]}"
            
            return new_buy_oids, new_sell_oids
        except Exception as e:
            self.last_status = f"❌ Batch Exception"
            return [], []

    def place_order(self, is_buy: bool, price: float, size: float, tif: str = "Gtc") -> Optional[int]:
        """Place a limit order and return order ID"""
        if DASHBOARD_ONLY_MODE:
            return None
        try:
            result = self.exchange.order(
                name=SYMBOL,
                is_buy=is_buy,
                sz=size,
                limit_px=price,
                order_type={"limit": {"tif": tif}},
                reduce_only=False,
            )
            
            if result.get("status") != "ok":
                err_msg = str(result.get("response", ""))
                self.last_status = f"❌ Order Rejected: {err_msg[:30]}"
                return None

            statuses = result.get("response", {}).get("data", {}).get("statuses", [])
            if not statuses:
                self.last_status = "❌ Empty Response"
                return None

            st = statuses[0]
            if "resting" in st:
                oid = st["resting"]["oid"]
                self.order_details[oid] = {"price": price, "size": size, "is_buy": is_buy}
                return oid
            elif "filled" in st:
                # Order was filled immediately
                filled_info = st["filled"]
                filled_size = float(filled_info.get("totalSz", size))
                filled_price = float(filled_info.get("avgPx", price))
                self.total_volume += filled_price * filled_size
                self.fills_count += 1
                return None
            elif "error" in st:
                self.last_status = f"❌ {st['error'][:30]}"
                return None
                
            return None
        except Exception as e:
            self.last_status = f"❌ Order Error: {str(e)[:20]}"
            return None
    
    def generate_dashboard(self, mid_price: float = 0.0, position: float = 0.0) -> Panel:
        """Generate a live dashboard panel with trading stats - ENHANCED"""
        logo_content = r"""[bold cyan]
██╗  ██╗██╗   ██╗██████╗ ███████╗██████╗ ██╗     ██╗ ██████╗ ██╗   ██╗██╗██████╗ 
██║  ██║╚██╗ ██╔╝██╔══██╗██╔════╝██╔══██╗██║     ██║██╔═══██╗██║   ██║██║██╔══██╗
███████║ ╚████╔╝ ██████╔╝█████╗  ██████╔╝██║     ██║██║   ██║██║   ██║██║██║  ██║
██╔══██║  ╚██╔╝  ██╔═══╝ ██╔══╝  ██╔══██╗██║     ██║██║▄▄ ██║██║   ██║██║██║  ██║
██║  ██║   ██║   ██║     ███████╗██║  ██║███████╗██║╚██████╔╝╚██████╔╝██║██████╔╝
╚═╝  ╚═╝   ╚═╝   ╚═╝     ╚══════╝╚═╝  ╚═╝╚══════╝╚═╝ ╚══▀▀═╝  ╚═════╝ ╚═╝╚═════╝ 

[/bold cyan]"""
        # Main stats table
        table = Table(box=box.ROUNDED, show_header=True, header_style="bold cyan")
        table.add_column("PAIR", style="bold white", justify="left")
        table.add_column("COST/1M", style="dim", justify="center")
        table.add_column("VOLUME", style="green", justify="right")
        table.add_column("FEES", style="yellow", justify="right")
        table.add_column("PNL", justify="right")

        # Format PNL with color and appropriate precision
        pnl_color = "green" if self.session_pnl >= 0 else "red"
        if abs(self.session_pnl) < 1:
            pnl_text = f"[{pnl_color}]${self.session_pnl:+.4f}[/{pnl_color}]"
        else:
            pnl_text = f"[{pnl_color}]${self.session_pnl:+.2f}[/{pnl_color}]"

        # Calculate Cost Per Million
        net_cost = self.total_fees - self.session_pnl
        if self.total_volume > 0:
            cost_per_m = net_cost / (self.total_volume / 1_000_000)
        else:
            cost_per_m = 0.0

        cost_color = "green" if cost_per_m < 100 else "yellow" if cost_per_m < 200 else "red"
        cost_text = f"[{cost_color}]${cost_per_m:.1f}[/{cost_color}]"
        
        # Format fees with appropriate precision
        fees_text = f"${self.total_fees:.4f}" if self.total_fees < 1 else f"${self.total_fees:.2f}"
        
        table.add_row(
            f"🔶 {SYMBOL}",
            cost_text,
            f"${self.total_volume:.2f}",
            fees_text,
            pnl_text
        )
        
        # Round-trip stats (primary profit metric)
        rt_stats = self.get_round_trip_stats()
        elapsed_mins = max((time.time() - self.start_time) / 60, 1)
        fills_per_min = self.fills_count / elapsed_mins

        # Calculer le drift actuel pour l'affichage
        current_drift = 0
        if self.orders_anchor_price and self.latest_bbo:
            current_drift = int(abs(self.latest_bbo[2] - self.orders_anchor_price) / TICK_SIZE)

        # Profit-focused caption
        rt_ratio_color = "green" if rt_stats['ratio'] >= 0.5 else "yellow" if rt_stats['ratio'] >= 0.3 else "red"
        rt_info = f"RT: [{rt_ratio_color}]{rt_stats['count']} ({rt_stats['ratio']:.0%})[/{rt_ratio_color}]"
        rt_profit_color = "green" if rt_stats['avg_profit'] >= 0 else "red"
        rt_profit_info = f"Avg: [{rt_profit_color}]${rt_stats['avg_profit']:+.4f}[/{rt_profit_color}]" if rt_stats['count'] > 0 else ""
        spread_info = f"Spread: [magenta]{self.current_spread_ticks}t[/magenta]"
        drift_info = f"Drift: [{'red' if current_drift >= 5 else 'yellow' if current_drift >= 3 else 'green'}]{current_drift}t[/]"
        pending_info = f"Pend: {rt_stats.get('pending', 0)}" if rt_stats.get('pending', 0) > 0 else ""

        table.caption = f"\n{rt_info} {rt_profit_info} | {spread_info} | {drift_info} {pending_info}"

        # Status info table
        status_table = Table(box=None, show_header=False, padding=(0, 1))
        status_table.add_column("Key", style="dim")
        status_table.add_column("Value", style="bold")
        
        runtime = int(time.time() - self.start_time)
        hours, remainder = divmod(runtime, 3600)
        minutes, seconds = divmod(remainder, 60)
        runtime_str = f"{hours}h {minutes}m {seconds}s" if hours > 0 else f"{minutes}m {seconds}s"
        
        # Launch time
        launch_time = datetime.fromtimestamp(self.start_time).strftime("%H:%M:%S")
        
        n_bids, n_asks = len(self.buy_oids), len(self.sell_oids)
        
        maker_ratio = (self.maker_fills / max(self.maker_fills + self.taker_fills, 1)) * 100.0

        status_table.add_row("Runtime", f"{runtime_str} (started {launch_time})")
        status_table.add_row("PAIR", f"[cyan]{SYMBOL}[/] | Lev: [yellow]{self.target_leverage}x[/]")
        status_table.add_row("Orders", f"[green]{n_bids}B[/] / [red]{n_asks}A[/] | Drift: {current_drift}t")
        status_table.add_row("Fills", f"{self.fills_count} ({fills_per_min:.1f}/min) | Maker: {maker_ratio:.0f}%")

        # Round-trip profit row (primary metric)
        rt_profit_color = "green" if rt_stats['total_profit'] >= 0 else "red"
        rt_avg_color = "green" if rt_stats['avg_profit'] >= 0 else "red"
        if rt_stats['count'] > 0:
            status_table.add_row(
                "RoundTrips",
                f"{rt_stats['count']} completed ({rt_stats['ratio']:.0%}) | "
                f"Total: [{rt_profit_color}]${rt_stats['total_profit']:+.4f}[/{rt_profit_color}] | "
                f"Avg: [{rt_avg_color}]${rt_stats['avg_profit']:+.4f}[/{rt_avg_color}] | "
                f"Pend: {rt_stats.get('pending', 0)}"
            )
        else:
            status_table.add_row("RoundTrips", "[dim]Waiting for first round-trip...[/dim]")

        status_table.add_row("Status", self.last_status)

        # === ALPHA SIGNAL DISPLAY ===
        if ALPHA_ENABLED:
            alpha = self.alpha_engine.composite_alpha
            regime = self.alpha_engine.regime
            regime_color = {"RANGING": "cyan", "TRENDING": "yellow", "VOLATILE": "red"}.get(regime, "dim")
            alpha_color = "green" if alpha > 0.2 else "red" if alpha < -0.2 else "dim"
            bars_filled = int(abs(alpha) * 10)
            alpha_bar = "█" * bars_filled + "░" * (10 - bars_filled)
            alpha_dir = "▲" if alpha > 0 else "▼" if alpha < 0 else "◆"
            status_table.add_row(
                "Alpha",
                f"[{alpha_color}]{alpha_dir} {alpha:+.3f} [{alpha_bar}][/{alpha_color}] | "
                f"Regime: [{regime_color}]{regime}[/{regime_color}] | "
                f"RSI: {self.alpha_engine.rsi_value:.0f}"
            )
            signals_str = (
                f"EMA:{self.alpha_engine.ema_signal:+.1f} "
                f"BB:{self.alpha_engine.bollinger_signal:+.1f} "
                f"MACD:{self.alpha_engine.macd_signal:+.1f} "
                f"RSI:{self.alpha_engine.rsi_signal:+.1f} "
                f"VWAP:{self.alpha_engine.vwap_signal:+.1f}"
            )
            status_table.add_row("Signals", f"[dim]{signals_str}[/dim]")
        
        # Layout
        layout = Layout()
        layout.split_column(
            Layout(Align.center(table), name="main", size=7),
            Layout(Align.center(status_table), name="status", size=14)
        )
        
        return Panel(
            Group(Align.center(Text.from_markup(logo_content)), layout),
            title=f"[bold cyan]{self.strategy_name.upper()}[/bold cyan]",
            subtitle=f"[dim]Ctrl+C to stop | Spread: {MIN_SPREAD_TICKS}-{MAX_SPREAD_TICKS}t | RT Wait: {ROUND_TRIP_WAIT_SEC:.0f}s | Drift: {PRICE_DRIFT_THRESHOLD_TICKS}t/{URGENT_DRIFT_THRESHOLD_TICKS}t[/dim]",
            border_style="cyan",
            padding=(1, 2)
        )
    
    def should_quote_buy(self, position: float, mid_price: float) -> bool:
        """Check if we should place a buy order based on position limits + alpha gate"""
        current_notional = position * mid_price
        base = current_notional < MAX_POSITION_USD
        # Alpha gate: don't add to a long if alpha is strongly bearish
        if ALPHA_ENABLED and base and position > 0 and self.alpha_engine.composite_alpha < -0.5:
            return False
        return base
    
    def should_quote_sell(self, position: float, mid_price: float) -> bool:
        """Check if we should place a sell order based on position limits + alpha gate"""
        current_notional = position * mid_price
        base = current_notional > -MAX_POSITION_USD
        # Alpha gate: don't add to a short if alpha is strongly bullish
        if ALPHA_ENABLED and base and position < 0 and self.alpha_engine.composite_alpha > 0.5:
            return False
        return base

    def get_dynamic_stop_loss_roe(self) -> float:
        """Dynamic stop loss based on regime: tighter in trend/volatile, wider in range."""
        base = STOP_LOSS_ROE_PCT
        if not ALPHA_ENABLED:
            return base
        regime = self.alpha_engine.regime
        if regime == "TRENDING":
            return base * 0.7
        elif regime == "VOLATILE":
            return base * 0.5
        else:  # RANGING
            return base * 1.2
    
    def run_iteration(self) -> tuple[float, float]:
        """
        NOUVELLE LOGIQUE INTELLIGENTE:
        - Ordres STABLES quand le prix ne bouge pas
        - Refresh INSTANTANÉ quand le prix drift de X ticks vs ordres actuels
        - Pas de délai artificiel basé sur le temps
        """
        # 1. GET PRICE FROM WS
        if not self.latest_bbo:
            self.last_status = "⌛ Waiting for Price Feed..."
            return (0.0, 0.0)
            
        best_bid, best_ask, mid_price = self.latest_bbo
        ws_active = True
        
        # 2. GET STATE FROM WS (No HTTP user_state!)
        position = self.latest_position
        unrealized_pnl = self.latest_unrealized_pnl
        equity = self.ws_equity if self.ws_equity > 0 else 10.0
        
        # 2b. FALLBACK: Si on a une position mais pas de PnL du WS, fetch via API
        if abs(position) > 0.001 and unrealized_pnl == 0:
            try:
                api_ok, api_pos, api_pnl = self.get_position_and_pnl_with_status()
                if api_ok:
                    position = api_pos
                    self.latest_position = api_pos
                    unrealized_pnl = api_pnl
                    self.latest_unrealized_pnl = api_pnl
            except:
                pass

        # 2c. Sync API position/PnL (throttled): avoids missing pre-existing positions.
        now_ts = time.time()
        if (now_ts - self.last_api_pos_sync_ts) >= POSITION_API_SYNC_INTERVAL_SEC:
            self.last_api_pos_sync_ts = now_ts
            try:
                api_ok, api_pos, api_pnl = self.get_position_and_pnl_with_status()
                if api_ok:
                    position = api_pos
                    self.latest_position = api_pos
                    unrealized_pnl = api_pnl
                    self.latest_unrealized_pnl = api_pnl
            except Exception:
                pass
        
        # Dashboard status
        ws_icon = "⚡" if ws_active else "☁️"

        # === MODE DASHBOARD UNIQUEMENT: pas d'ordres, juste observer ===
        if DASHBOARD_ONLY_MODE:
            self.last_status = (
                f"{ws_icon} 📊 DASHBOARD | ${mid_price:.2f} "
                f"| Pos: {position:+.4f} | PnL: ${self.session_pnl:+.4f}"
            )
            return (mid_price, position)

        # === ROE STOP RECOVERY MODE ===
        # After a ROE stop trigger, keep the bot alive but retry close at low cadence
        # until API confirms flat position.
        if self.roe_stop_active:
            try:
                api_ok, api_pos, api_pnl = self.get_position_and_pnl_with_status()
                if api_ok:
                    position = api_pos
                    unrealized_pnl = api_pnl
            except Exception:
                pass

            if self._is_flat_position(position):
                self.roe_stop_active = False
                self.last_status = "✅ ROE stop recovered (position flat)"
                self.orders_need_sync = True
                self.feed_event.set()
            else:
                if (now_ts - self.last_roe_stop_attempt_ts) >= ROE_STOP_RETRY_COOLDOWN_SEC:
                    self.cancel_all_orders()
                    self.close_position(known_position=position, trigger="roe_stop_recovery")
                    self.last_roe_stop_attempt_ts = now_ts
                self.last_status = f"🧯 ROE stop recovery | pos={position:.4f}"
                return (mid_price, position)

        # === KILL SWITCH: SESSION PNL (profit-first) ===
        if self.fills_count >= MIN_FILLS_FOR_KILL_SWITCH:
            if self.session_pnl < SESSION_PNL_KILL_SWITCH:
                self.last_status = f"🛑 KILL SWITCH: Session PnL ${self.session_pnl:.4f} < ${SESSION_PNL_KILL_SWITCH}"
                self.emergency_full_stop(reason="pnl_kill_switch")
                return (mid_price, position)

        # === POSITION STOP: MARKET CLOSE @ -1% ROE (bot continues) ===
        if abs(position) > 0.0001:
            # Toujours fetch le PnL API pour la décision de stop.
            api_pnl = None
            try:
                api_ok, api_pos, fetched_api_pnl = self.get_position_and_pnl_with_status()
                if api_ok:
                    position = api_pos
                    api_pnl = fetched_api_pnl
                    pnl = fetched_api_pnl
                else:
                    pnl = unrealized_pnl
            except Exception:
                pnl = unrealized_pnl

            # Fallback robuste: si PnL API/WS nul mais position ouverte, estimer via entry price.
            if abs(position) > 0.0001 and abs(pnl) < 1e-9 and self.entry_price and self.entry_price > 0:
                pnl = (mid_price - self.entry_price) * position

            # PnL effectif (conservateur) pour les décisions de risque.
            pnl = self._effective_unrealized_pnl(mid_price, position, unrealized_pnl, api_pnl)
            unrealized_pnl = pnl

            # Margin: prefer API/WS position margin if available.
            position_value = abs(position * mid_price)
            margin_used = self.position_margin if self.position_margin > 0 else (position_value / max(TARGET_LEVERAGE, 1e-9))

            # ROE% = PnL / Margin * 100
            roe_pct = (pnl / margin_used * 100) if margin_used > 0.01 else 0
            self._vlog(
                f"[STOP CHECK] Pos: {position:.4f} | PnL_eff: ${pnl:.4f} | "
                f"Margin: ${margin_used:.2f} | ROE: {roe_pct:.2f}% | Entry: {self.entry_price}"
            )

            stop_by_roe = roe_pct <= -self.get_dynamic_stop_loss_roe()
            stop_by_usd = ENABLE_STOP_LOSS_USD and (pnl <= -STOP_LOSS_MARKET_USD)
            if stop_by_roe or stop_by_usd:
                reason = f"ROE: {roe_pct:.2f}%" if stop_by_roe else f"USD: ${pnl:.2f}"
                self.last_status = f"🚨 POSITION STOP MARKET: {reason} (bot continues)"
                self.roe_stop_active = True
                self._vlog(f"POSITION STOP MARKET: {reason} | pnl={pnl:.4f} | margin={margin_used:.4f}")
                if (now_ts - self.last_roe_stop_attempt_ts) >= ROE_STOP_RETRY_COOLDOWN_SEC:
                    self.cancel_all_orders()
                    self.close_position(known_position=position, trigger="roe_stop")
                    self.last_roe_stop_attempt_ts = now_ts
                self.orders_anchor_price = None
                self.orders_anchor_position = None
                self.orders_need_sync = True
                self.feed_event.set()
                return (mid_price, position)

        # === FEED STALE / CONNECTIVITY PROTECTION ===
        if self.last_ws_msg_ts > 0 and (time.time() - self.last_ws_msg_ts) > FEED_STALE_SECONDS:
            self.cancel_all_orders()
            self.last_status = f"🛡️ Feed stale > {FEED_STALE_SECONDS:.1f}s - quotes pulled"
            hard_stale_threshold = FEED_STALE_SECONDS * FEED_STALE_HARD_STOP_MULTIPLIER
            if ENABLE_FEED_STALE_HARD_STOP and (time.time() - self.last_ws_msg_ts) > hard_stale_threshold:
                self.emergency_full_stop(reason="Feed Stale Hard Stop")
            return (mid_price, position)
        
        # === DRAWDOWN PROTECTION ===
        if ENABLE_DRAWDOWN_HARD_STOP and self.best_pnl_session > 0.5 and self.session_pnl < self.best_pnl_session * 0.3:
            drawdown_block = True
            if DRAWDOWN_REQUIRE_NEGATIVE_PNL and self.session_pnl > DRAWDOWN_MIN_PNL_USD:
                drawdown_block = False
            if drawdown_block:
                self.last_status = f"🛑 DRAWDOWN: PnL dropped from ${self.best_pnl_session:.2f}"
                self.emergency_full_stop(reason="Drawdown Protection")
                return (mid_price, position)

        # === HARD INVENTORY LIMIT ===
        if abs(position * mid_price) > MAX_POSITION_USD * MAX_INVENTORY_HARD_PCT:
            if ENABLE_HARD_INVENTORY_FORCE_CLOSE:
                self.last_status = "🛑 Hard inventory cap exceeded - force close"
                self.cancel_all_orders()
                self.close_position(known_position=position, trigger="hard_inventory")
                return (mid_price, position)
            else:
                self.last_status = "⚠️ Hard inventory cap exceeded - reducing only"

        # === P95 SLIPPAGE GUARD ===
        if ENABLE_P95_SLIPPAGE_KILL_SWITCH and len(self.slippage_bps_history) >= 40:
            p95_slip = self._p95(list(self.slippage_bps_history))
            if p95_slip > MAX_P95_SLIPPAGE_BPS:
                self.last_status = f"🛑 p95 slippage {p95_slip:.2f}bps > {MAX_P95_SLIPPAGE_BPS:.2f}"
                self.emergency_full_stop(reason="Slippage Kill Switch")
                return (mid_price, position)
        
        # Position stop handled above (before feed-stale early return).

        # Update spread for dashboard
        spread_bps = (best_ask - best_bid) / mid_price * 10000
        self.current_spread = spread_bps
        
        # =====================================================================
        # === PROFIT-FIRST REFRESH LOGIC ===
        # Key principle: STABLE ORDERS → let round-trips complete
        # Only refresh when: (a) drift > 5 ticks, (b) ALL orders one side gone,
        #                     (c) TTL expired, (d) stop loss, (e) periodic sync
        # NEVER refresh during round-trip wait period (post-fill cooldown)
        # =====================================================================
        tick = TICK_SIZE
        now_ts = time.time()

        # Calculer le drift = combien de ticks le mid a bougé vs notre ancrage
        price_drift_ticks = 0
        if self.orders_anchor_price is not None:
            price_drift = abs(mid_price - self.orders_anchor_price)
            price_drift_ticks = int(price_drift / tick)

        # Ordres présents?
        has_orders = len(self.buy_oids) > 0 or len(self.sell_oids) > 0
        has_bids = len(self.buy_oids) > 0
        has_asks = len(self.sell_oids) > 0
        both_sides_present = has_bids and has_asks
        one_side_empty = has_orders and not both_sides_present  # Orders exist but only on one side

        # Time since last fill (for round-trip wait)
        time_since_fill = now_ts - self.last_fill_time_rt if self.last_fill_time_rt > 0 else 999.0
        in_rt_cooldown = time_since_fill < ROUND_TRIP_WAIT_SEC and self.waiting_for_round_trip

        # === EXIT LOGIC - ONLY for real emergencies (wider thresholds) ===
        need_emergency_exit = False
        exit_side = None

        if abs(position) > 0.001:
            exit_side = "SELL" if position > 0 else "BUY"
            # Only emergency exit at STOP_LOSS level (much wider now: $0.50)
            if unrealized_pnl < -self.STOP_LOSS_THRESHOLD:
                need_emergency_exit = True

        # =====================================================================
        # === DÉCISION DE REFRESH - PROFIT FIRST ===
        # =====================================================================
        need_refresh = False
        refresh_reason = ""
        quote_age = now_ts - self.last_quote_update_ts if self.last_quote_update_ts > 0 else 999.0

        # Priorité 1: Emergency stop loss (always immediate)
        if need_emergency_exit:
            need_refresh = True
            refresh_reason = f"STOP {exit_side}"
            self.current_refresh_interval = REFRESH_SPEED_FAST

        # Priorité 2: No orders at all = initial placement
        elif not has_orders:
            need_refresh = True
            refresh_reason = "Initial placement"
            self.current_refresh_interval = REFRESH_SPEED_FAST

        # Priorité 3: ROUND-TRIP COOLDOWN - block refresh to let RT complete
        elif in_rt_cooldown and both_sides_present:
            # Orders on both sides + waiting for RT = DO NOTHING
            self.current_refresh_interval = REFRESH_SPEED_SLOW
            rt_stats = self.get_round_trip_stats()
            self.last_status = (
                f"{ws_icon} RT WAIT {ROUND_TRIP_WAIT_SEC - time_since_fill:.0f}s "
                f"| {len(self.buy_oids)}B/{len(self.sell_oids)}A "
                f"| RT:{rt_stats['count']} ({rt_stats['ratio']:.0%})"
            )
            return (mid_price, position)

        # Priorité 4: One side completely empty (all fills consumed)
        # AND not in RT cooldown → need to replenish
        elif one_side_empty and not in_rt_cooldown:
            need_refresh = True
            side_gone = "BIDS" if not has_bids else "ASKS"
            refresh_reason = f"Replenish {side_gone}"
            self.current_refresh_interval = REFRESH_SPEED_NORMAL
            # Also clear RT wait since one side is fully consumed
            self.waiting_for_round_trip = False

        # Priorité 5: URGENT DRIFT (8+ ticks) - price moved far, must adjust
        elif price_drift_ticks >= self.URGENT_DRIFT_TICKS:
            need_refresh = True
            refresh_reason = f"DRIFT {price_drift_ticks}t"
            self.current_refresh_interval = REFRESH_SPEED_FAST
            self.waiting_for_round_trip = False  # Cancel RT wait on big moves

        # Priorité 6: NORMAL DRIFT (5+ ticks) - adjust if not in RT wait
        elif price_drift_ticks >= self.PRICE_DRIFT_TICKS and not in_rt_cooldown:
            need_refresh = True
            refresh_reason = f"Drift {price_drift_ticks}t"
            self.current_refresh_interval = REFRESH_SPEED_NORMAL

        # Priorité 7: TTL expired (quotes too old)
        elif quote_age >= MAX_QUOTE_TTL_SEC and not in_rt_cooldown:
            need_refresh = True
            refresh_reason = f"TTL {quote_age:.0f}s"
            self.current_refresh_interval = REFRESH_SPEED_NORMAL

        # Priorité 8: Periodic sync
        elif (now_ts - self.last_sync_time) > PERIODIC_SYNC_SECONDS and not in_rt_cooldown:
            need_refresh = True
            refresh_reason = "Periodic"
            self.current_refresh_interval = REFRESH_SPEED_NORMAL
            self.last_sync_time = now_ts

        else:
            # === MARCHÉ STABLE: On ne touche à rien! ===
            self.current_refresh_interval = REFRESH_SPEED_SLOW
            anchor_info = f"Anchor: ${self.orders_anchor_price:.2f}" if self.orders_anchor_price else "---"
            rt_stats = self.get_round_trip_stats()
            rt_info = f"RT:{rt_stats['count']}({rt_stats['ratio']:.0%})" if rt_stats['count'] > 0 else ""
            self.last_status = (
                f"{ws_icon} STABLE @ ${mid_price:.2f} | {anchor_info} "
                f"| {price_drift_ticks}t | {rt_info}"
            )
            return (mid_price, position)

        # === UPDATE STATE ===
        self.last_bbo = self.latest_bbo
        self.last_position = position
        self.last_mid_price = mid_price

        # === CALCULATE LEVELS ===
        bid_levels, ask_levels = self.calculate_levels(best_bid, best_ask, position)

        # === SIGNAL CONSENSUS GATE ===
        if ALPHA_ENABLED and self.alpha_engine.tick_count >= ALPHA_MIN_TICKS_FOR_SIGNALS:
            agreement = self.alpha_engine.signal_agreement()
            if agreement < 0.3:
                self.last_status += " LOW CONSENSUS"
                bid_levels = bid_levels[:1]
                ask_levels = ask_levels[:1]

        # === RISK REDUCTION MODE - PASSIVE ONLY ===
        if need_emergency_exit:
            # Emergency: only quote exit side passively
            if exit_side == "SELL":
                bid_levels = []
                ask_levels = ask_levels[:max(1, min(3, len(ask_levels)))]
            else:
                ask_levels = []
                bid_levels = bid_levels[:max(1, min(3, len(bid_levels)))]

        # === MOMENTUM/FLASH PROTECTION ===
        elif price_drift_ticks >= self.URGENT_DRIFT_TICKS and self.orders_anchor_price is not None:
            if mid_price > self.orders_anchor_price:
                bid_levels = bid_levels[:1] if bid_levels else []
            else:
                ask_levels = ask_levels[:1] if ask_levels else []

        else:
            # === NORMAL MODE: Check position limits ===
            can_buy = self.should_quote_buy(position, mid_price)
            can_sell = self.should_quote_sell(position, mid_price)

            if ANTI_ADVERSE_SELECTION and unrealized_pnl < -0.02:
                if position > 0.001:
                    can_buy = False
                elif position < -0.001:
                    can_sell = False

            if not can_buy: bid_levels = []
            if not can_sell: ask_levels = []

        # === EXECUTE: Cancel & Replace ===
        quote_tif = "Alo" if POST_ONLY_MM_QUOTES else "Gtc"
        if STRICT_POST_ONLY_FLOW:
            quote_tif = "Alo"

        self.cancel_all_orders()
        self.buy_oids, self.sell_oids = self.place_order_batch(bid_levels, ask_levels, tif=quote_tif)
        self.last_quote_update_ts = now_ts
        self.last_replace_mid = mid_price
        self.replace_events.append(now_ts)
        self.orders_need_sync = False
        self.inventory_notional_history.append(abs(position * mid_price))
        self._auto_tune_score_weights()

        # === SAUVEGARDER LE PRIX D'ANCRAGE DES NOUVEAUX ORDRES ===
        self.orders_anchor_price = mid_price
        self.orders_anchor_position = position
        if bid_levels:
            self.orders_best_bid = bid_levels[0][0]
        if ask_levels:
            self.orders_best_ask = ask_levels[0][0]

        n_bids = len(self.buy_oids)
        n_asks = len(self.sell_oids)
        rt_stats = self.get_round_trip_stats()
        spread_info = f"S:{self.current_spread_ticks}t"
        rt_info = f"RT:{rt_stats['count']}({rt_stats['ratio']:.0%})" if rt_stats['count'] > 0 else ""
        self.last_status = (
            f"{ws_icon} {refresh_reason} -> {n_bids}B/{n_asks}A "
            f"@ ${mid_price:.2f} {spread_info} {rt_info}"
        )

        return (mid_price, position)
        
    def run(self):
        """Run the market maker with live dashboard"""
        mid_price = 0.0
        position = 0.0
        
        with Live(self.generate_dashboard(mid_price, position), refresh_per_second=4, console=self.console) as live:
            while self.running:
                try:
                    # WAIT FOR EVENT instead of sleep
                    # giving it a timeout equal to the refresh interval
                    # This enables INSTANT reaction to WS price updates
                    
                    if self.feed_event.wait(timeout=self.current_refresh_interval):
                        self.feed_event.clear()
                    
                    mid_price, position = self.run_iteration()
                    live.update(self.generate_dashboard(mid_price, position))
                    
                except KeyboardInterrupt:
                    break
                except Exception as e:
                    self.last_status = f"❌ Error: {str(e)[:30]}"
                    live.update(self.generate_dashboard(mid_price, position))
                    time.sleep(1.0) # Backoff on error
        
        self.emergency_full_stop(reason="Loop Ended")

# =============================================================================
# MAIN
# =============================================================================

def kill_other_instances():
    """Finds and kills other instances of market_maker.py to prevent duplicates"""
    current_pid = os.getpid()
    script_name = os.path.basename(__file__)
    killed_count = 0
    
    for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
        try:
            # Check if it's a python process
            if proc.info['name'] and 'python' in proc.info['name'].lower():
                cmdline = proc.info['cmdline']
                # Check if it's running our script and is not our current process
                if cmdline and any(script_name in arg for arg in cmdline) and proc.info['pid'] != current_pid:
                    print(f"🗡️ Found existing instance (PID {proc.info['pid']}). Killing it...")
                    p = psutil.Process(proc.info['pid'])
                    p.terminate()
                    # Wait a bit for termination, then force kill if needed
                    try:
                        p.wait(timeout=3)
                    except psutil.TimeoutExpired:
                        p.kill()
                    killed_count += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
            
    if killed_count > 0:
        print(f"✅ Cleaned up {killed_count} existing instances.")
    else:
        print("✅ No other instances found. Starting fresh.")



def select_token_menu():
    """Menu interactif pour choisir le token à trader"""
    print("\n" + "="*60)
    print("🚀 HYPERLIQUID MARKET MAKER - Sélection du Token")
    print("="*60)
    print("\nTokens disponibles:\n")

    tokens_list = list(AVAILABLE_TOKENS.keys())
    for i, token in enumerate(tokens_list, 1):
        config = AVAILABLE_TOKENS[token]
        print(f"  [{i}] {config['symbol']:<15} - Leverage: {config['target_leverage']}x, Size: ${config['order_size_usd']}")

    print(f"\n  [Enter] Utiliser {ACTIVE_TOKEN} (par défaut)")

    while True:
        try:
            choice = input(f"\nChoisissez un token (1-{len(tokens_list)}) ou Enter pour défaut: ").strip()

            if choice == "":
                return ACTIVE_TOKEN

            if choice.lower() == 'q':
                sys.exit(0)

            idx = int(choice) - 1
            if 0 <= idx < len(tokens_list):
                return tokens_list[idx]
            else:
                print(f"❌ Choix invalide. Entrez un nombre entre 1 et {len(tokens_list)}")
        except ValueError:
            print("❌ Entrée invalide. Entrez un nombre.")
        except KeyboardInterrupt:
            print("\n👋 Au revoir!")
            sys.exit(0)

if __name__ == "__main__":
    # Menu interactif de sélection du token
    ACTIVE_TOKEN = select_token_menu()
    TOKEN_CONFIG = AVAILABLE_TOKENS[ACTIVE_TOKEN]
    SYMBOL = canonical_symbol(TOKEN_CONFIG["symbol"])
    ORDER_SIZE_USD = TOKEN_CONFIG["order_size_usd"]
    TARGET_LEVERAGE = TOKEN_CONFIG["target_leverage"]
    MAX_POSITION_USD = TOKEN_CONFIG["max_position_usd"]
    NUM_LEVELS = TOKEN_CONFIG["num_levels"]
    LEVEL_MULTIPLIERS = TOKEN_CONFIG["level_multipliers"]
    TICK_SIZE = TOKEN_CONFIG["tick_size"]
    MIN_SIZE = TOKEN_CONFIG["min_size"]
    
    # Mettre à jour le DEX et le nom du coin
    PERP_DEX = "xyz" if SYMBOL.startswith("xyz:") else "km"
    COIN_NAME = SYMBOL.split(":")[-1]
    
    # Calculer le nombre de décimales pour l'arrondi
    PRICE_DECIMALS = max(0, -int(math.floor(math.log10(TICK_SIZE)))) if TICK_SIZE < 1 else 0
    SIZE_DECIMALS = max(0, -int(math.floor(math.log10(MIN_SIZE)))) if MIN_SIZE < 1 else 0

    print(f"\n✅ Token sélectionné: {SYMBOL}")
    print(f"   Leverage: {TARGET_LEVERAGE}x | Size: ${ORDER_SIZE_USD} | Max Position: ${MAX_POSITION_USD}")
    print(f"   Tick: {TICK_SIZE} | Min Size: {MIN_SIZE}")
    print("\n")

    kill_other_instances()
    mm = MarketMaker()
    mm.run()
