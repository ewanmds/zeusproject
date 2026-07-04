"""
Hyperliquid Market Making Bot - ULTRA OPTIMIZED v3.0 MULTI-TOKENS
=====================================================
High-performance market making for HIP-3 km markets with:

🚀 VOLUME MAXIMIZATION:
- Ultra-tight spreads with dynamic volatility adjustment
- Multi-level grid with aggressive center weighting
- Instant requoting on fills via WebSocket
- Smart inventory-neutral positioning

💰 PNL PROTECTION:
- Avellaneda-Stoikov fair value skewing
- Volatility-adaptive spread widening
- Fast exit on adverse selection
- Anti-averaging protection with dynamic thresholds
- Momentum detection for flash crashes/pumps

🎯 MULTI-TOKENS SUPPORT:
- US500, USTECH, USOIL, GOLD, BTC, SP500
- Configuration personnalisée par token
- Menu de sélection interactif
"""

import copy
import time
import threading
from datetime import datetime
from collections import deque
from dataclasses import dataclass, field
import json
import math
import eth_account
from hyperliquid.info import Info
from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants
from typing import Optional, Tuple, List, Dict, Any
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
# CONFIGURATION MULTI-TOKENS - OPTIMIZED FOR MAX VOLUME & MIN LOSS
# =============================================================================

# --- 1. COMPTE & API ---
AGENT_PRIVATE_KEY = "0x60647606ac89626b8353d1c8a9cd234cb7a003e4ae21fd7c038216e0c9cd2dfd"
ACCOUNT_ADDRESS = "0xF8EA073D244e276247Fc1C5810dCA81A2354017d"
BASE_URL = constants.MAINNET_API_URL
SINGLE_LEVEL_TOKENS = {"XYZ100", "SP500"}


def build_fixed_spot_meta(base_url: str = BASE_URL) -> Optional[Dict[str, Any]]:
    """
    Workaround for hyperliquid-python-sdk spot-meta parsing.

    The SDK indexes `spot_meta["tokens"][i]` positionally, but the API now
    returns a `tokens` list whose positions no longer match the token `index`
    ids (delisted tokens create gaps), so `Info()`/`Exchange()` crash with
    IndexError. We rebuild `tokens` as a dense list where position == index,
    padding gaps with harmless placeholders. Returns None on failure so the
    caller can fall back to the SDK default.
    """
    try:
        import requests
        raw = requests.post(
            base_url + "/info", json={"type": "spotMeta"}, timeout=10
        ).json()
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

# --- FEES / EDGE MODEL ---
# Petit wallet sans rebate: on paye les fills maker. 28.8$/M = 0.288 bps par fill.
MAKER_FEE_PER_MILLION_USD = 28.8
MAKER_FEE_BPS = MAKER_FEE_PER_MILLION_USD / 100.0
DEFAULT_ADVERSE_BUFFER_BPS = 0.35
DEFAULT_MIN_HALF_SPREAD_BPS = 0.75


def _indicator_defaults_for_token(token_name: str) -> Dict[str, Any]:
    """Per-token indicator defaults tuned for fee-aware small-wallet MM."""
    cfg: Dict[str, Any] = {
        "enabled": True,
        "ema_fast_period": 12,
        "ema_slow_period": 26,
        "rsi_period": 14,
        "rsi_overbought": 70.0,
        "rsi_oversold": 30.0,
        "rsi_extreme_overbought": 80.0,
        "rsi_extreme_oversold": 20.0,
        "bollinger_period": 20,
        "bollinger_std": 2.0,
        "bollinger_squeeze_threshold": 0.0008,
        "bollinger_expansion_threshold": 0.004,
        "vwap_window_sec": 300,
        "indicator_skew_weight": 0.50,
        "indicator_spread_weight": 0.40,
        "vwap_blend_weight": 0.15,
        "rsi_quote_suppression": True,
        "take_profit_enabled": True,
        "tp_rsi_overbought": 75.0,
        "tp_rsi_oversold": 25.0,
        "tp_base_offset_ticks": 2,
        "tp_size_pct": 0.35,
        "tp_extra_edge_bps": 0.40,
        "ema_cross_exit_enabled": True,
        "ema_cross_market_exit": False,
        "maker_fee_per_million": MAKER_FEE_PER_MILLION_USD,
        "fee_floor_enabled": True,
        "min_half_spread_bps": DEFAULT_MIN_HALF_SPREAD_BPS,
        "adverse_buffer_bps": DEFAULT_ADVERSE_BUFFER_BPS,
        "squeeze_tighten_ticks": 0.0,  # no rebate: never tighten only to farm volume
        "squeeze_size_factor": 1.0,
        "expansion_size_factor": 0.60,
    }

    high_vol = {"BTC", "COPPER", "USOIL", "CL", "TSLA"}
    low_vol = {"US500", "USTECH", "GOLD", "GOLD_XYZ", "SILVER", "SP500"}
    illiquid = {"XYZ100", "PLTR"}

    if token_name in high_vol:
        cfg.update({
            "rsi_period": 10,
            "bollinger_period": 14,
            "indicator_spread_weight": 0.55,
            "min_half_spread_bps": 0.90,
            "adverse_buffer_bps": 0.50,
            "tp_extra_edge_bps": 0.55,
        })
        if token_name == "CL":
            cfg.update({
                "rsi_period": 10,
                "bollinger_period": 14,
                "indicator_skew_weight": 0.55,
                "indicator_spread_weight": 0.60,
                "vwap_blend_weight": 0.18,
                "min_half_spread_bps": 0.80,
                "adverse_buffer_bps": 0.45,
                "tp_size_pct": 0.45,
                "tp_base_offset_ticks": 4,
                "tp_extra_edge_bps": 0.45,
            })
    elif token_name in low_vol:
        cfg.update({
            "min_half_spread_bps": 0.65,
            "adverse_buffer_bps": 0.30,
        })
    elif token_name in illiquid:
        cfg.update({
            "vwap_blend_weight": 0.25,
            "take_profit_enabled": False,
            "indicator_skew_weight": 0.35,
            "min_half_spread_bps": 1.20,
            "adverse_buffer_bps": 0.70,
            "ema_cross_market_exit": False,
        })

    if token_name in SINGLE_LEVEL_TOKENS:
        cfg.update({
            "ema_fast_period": 12,
            "ema_slow_period": 50,
            "rsi_period": 7,
            "rsi_overbought": 70.0,
            "rsi_oversold": 30.0,
            "rsi_extreme_overbought": 82.0,
            "rsi_extreme_oversold": 18.0,
            "bollinger_period": 20,
            "bollinger_std": 2.0,
            "bollinger_squeeze_threshold": 0.0006,
            "bollinger_expansion_threshold": 0.0035,
            "vwap_blend_weight": 0.10,
            "indicator_skew_weight": 0.0,
            "indicator_spread_weight": 0.0,
            "rsi_quote_suppression": False,
            "take_profit_enabled": True,
            "tp_rsi_overbought": 72.0,
            "tp_rsi_oversold": 28.0,
            "tp_base_offset_ticks": 1,
            "tp_size_pct": 0.50,
            "tp_extra_edge_bps": 0.30,
            "ema_cross_exit_enabled": False,
            "min_half_spread_bps": 0.55,
            "adverse_buffer_bps": 0.30,
            "squeeze_tighten_ticks": 0.0,
            "squeeze_size_factor": 1.20,
            "expansion_size_factor": 0.75,
        })

    return cfg


def _microstructure_defaults_for_token(token_name: str) -> Dict[str, Any]:
    """HFT-style microstructure controls used to avoid adverse selection."""
    cfg: Dict[str, Any] = {
        "enabled": True,
        "levels": 5,
        "ofi_window_sec": 12.0,
        "markout_horizons_sec": [1.0, 5.0, 15.0, 30.0],
        "min_edge_bps": 0.10,
        "min_ev_bps": 0.15,                # NEW: EV floor (conservative default, tighter on XYZ100)
        "microprice_weight": 0.80,
        "ofi_weight": 0.45,
        "imbalance_weight": 0.35,
        "adverse_markout_weight": 0.90,
        "toxic_markout_bps": 0.40,         # was 0.75 — actual adverse never reached this
        "toxic_widen_ticks": 4.0,
        "low_edge_size_factor": 0.60,
        "block_negative_edge": True,
        "allow_probe_quote": True,
        "probe_size_factor": 0.35,
        "probe_extra_ticks": 4,
        "max_pressure_bps": 2.50,
        "min_markout_samples": 15,         # was 20 — leave probation faster, EV gate now covers it
        "probation_min_pressure_bps": 0.35,
        "probation_neutral_pressure_bps": 0.20,
        "probation_allow_neutral": True,
        "conflict_pause_sec": 8.0,
    }

    if token_name in SINGLE_LEVEL_TOKENS:
        cfg.update({
            "levels": 5,
            "ofi_window_sec": 10.0,
            "min_edge_bps": 0.55,            # was 0.45 — belt-and-suspenders alongside EV gate
            "min_ev_bps": 0.20,              # NEW: conservative EV floor for XYZ100
            "microprice_weight": 1.00,
            "ofi_weight": 0.60,
            "imbalance_weight": 0.45,
            "adverse_markout_weight": 1.20,  # was 1.40 — slight relax, EV gate uses blended adverse
            "toxic_markout_bps": 0.25,       # was 0.55 — fire toxic_pause on actually-observed adverse
            "toxic_widen_ticks": 5.0,
            "low_edge_size_factor": 0.50,
            "probe_size_factor": 0.30,
            "probe_extra_ticks": 5,
            "max_pressure_bps": 3.00,
            "min_markout_samples": 15,       # was 30 — exit probation sooner; EV gate covers it
            "probation_min_pressure_bps": 0.55,
            "probation_neutral_pressure_bps": 0.28,
            "probation_allow_neutral": True,
            "conflict_pause_sec": 12.0,
        })
    elif token_name in {"BTC", "COPPER", "TSLA", "USOIL", "CL"}:
        cfg.update({
            "min_edge_bps": 0.16,
            "toxic_markout_bps": 0.65,
            "toxic_widen_ticks": 4.5,
        })
        if token_name == "CL":
            cfg.update({
                "levels": 5,
                "ofi_window_sec": 8.0,
                "min_edge_bps": 0.22,
                "microprice_weight": 0.95,
                "ofi_weight": 0.55,
                "imbalance_weight": 0.40,
                "adverse_markout_weight": 1.20,
                "toxic_markout_bps": 0.60,
                "toxic_widen_ticks": 5.0,
                "low_edge_size_factor": 0.55,
                "probe_size_factor": 0.35,
                "probe_extra_ticks": 5,
                "max_pressure_bps": 2.80,
                "min_markout_samples": 24,
                "probation_min_pressure_bps": 0.42,
                "probation_neutral_pressure_bps": 0.22,
                "probation_allow_neutral": True,
                "conflict_pause_sec": 10.0,
            })

    return cfg

def _breakout_guard_defaults_for_token(token_name: str) -> Dict[str, Any]:
    """Leading-indicator big-candle detector. Mutes the exposed side before quotes get run over."""
    cfg: Dict[str, Any] = {
        "enabled": True,
        "atr_window_bars": 20,
        "atr_mult": 2.0,
        "min_atr_bps": 0.8,
        "velocity_window_sec": 2.0,
        "velocity_sigma_mult": 2.0,
        "velocity_min_bps": 1.5,
        "velocity_sigma_alpha": 0.05,
        "atr_alpha": 0.15,
        "pause_sec": 6.0,
        "opposite_widen_ticks": 3.0,
        "anti_spam_sec": 1.5,
        "min_baseline_samples": 10,
    }
    if token_name in SINGLE_LEVEL_TOKENS:
        cfg.update({
            "atr_mult": 2.0,
            "min_atr_bps": 1.0,
            "velocity_min_bps": 1.8,
            "pause_sec": 7.0,
        })
    elif token_name == "CL":
        cfg.update({
            "atr_mult": 2.2,
            "min_atr_bps": 1.5,
            "velocity_min_bps": 2.5,
            "pause_sec": 8.0,
            "opposite_widen_ticks": 4.0,
        })
    return cfg

# --- CONFIGURATION MULTI-TOKENS ---
# Tokens disponibles sur Kinetiq Markets
AVAILABLE_TOKENS = {
    "US500": {
        "symbol": "km:US500",
        "tick_size": 0.01,
        "min_size": 0.01,
        "order_size_usd": 150.0,
        "max_position_usd": 300.0,
        "target_leverage": 25.0,
        "num_levels": 4,
        "level_multipliers": [2.0, 1.5, 1.0, 0.5, 0.5]
    },
    "USTECH": {
        "symbol": "km:USTECH",
        "tick_size": 0.01,
        "min_size": 0.01,
        "order_size_usd": 100.0,
        "max_position_usd": 250.0,
        "target_leverage": 20.0,
        "num_levels": 4,
        "level_multipliers": [2.0, 1.5, 1.0, 0.5]
    },
    "USOIL": {
        "symbol": "km:USOIL",
        "tick_size": 0.01,
        "min_size": 0.01,
        "order_size_usd": 150.0,
        "max_position_usd": 400.0,
        "target_leverage": 20.0,
        "num_levels": 4,
        "level_multipliers": [2.0, 1.5, 1.0, 0.5]
    },
    "GOLD": {
        "symbol": "km:GOLD",
        "tick_size": 0.01,
        "min_size": 0.01,
        "order_size_usd": 60.0,
        "max_position_usd": 350.0,
        "target_leverage": 22.0,
        "num_levels": 4,
        "level_multipliers": [2.0, 1.5, 1.0, 0.5]
    },
    "BTC": {
        "symbol": "km:BTC",
        "tick_size": 0.01,
        "min_size": 0.001,
        "order_size_usd": 150.0,
        "max_position_usd": 400.0,
        "target_leverage": 25.0,
        "num_levels": 4,
        "level_multipliers": [2.0, 1.5, 1.0, 0.5]
    },
    # === XYZ TOKENS (HIP-3) ===
    "SILVER": {
        "symbol": "xyz:SILVER",
        "tick_size": 0.001,
        "min_size": 0.01,
        "order_size_usd": 150.0,
        "max_position_usd": 500.0,
        "target_leverage": 20.0,
        "num_levels": 4,
        "level_multipliers": [2.0, 1.5, 1.0, 0.5]
    },
    "GOLD_XYZ": {
        "symbol": "xyz:GOLD",
        "tick_size": 0.01,
        "min_size": 0.01,
        "order_size_usd": 60.0,
        "max_position_usd": 500.0,
        "target_leverage": 20.0,
        "num_levels": 4,
        "level_multipliers": [2.0, 1.5, 1.0, 0.5]
    },
    "SP500": {
        "symbol": "xyz:SP500",
        "tick_size": 0.1,
        "min_size": 0.001,
        "order_size_usd": 0.0,
        "max_position_usd": 220.0,
        "target_leverage": 50.0,
        "num_levels": 1,
        "level_multipliers": [1]
    },
    "XYZ100": {
        "symbol": "xyz:XYZ100",
        "tick_size": 1.0,
        "min_size": 0.001,
        "order_size_usd": 0.0,
        "max_position_usd": 220.0,
        "target_leverage": 25.0,
        "num_levels": 1,
        "level_multipliers": [1],
        "base_spread_ticks": 3,
        "min_spread_ticks": 2,
        "max_spread_ticks": 8,
        "price_drift_threshold_ticks": 5,
        "urgent_drift_threshold_ticks": 12,
        "min_requote_interval_sec": 2.5,
        "max_quote_ttl_sec": 18.0,
        "periodic_sync_seconds": 12.0,
        "max_cancels_per_min": 20,
        "same_quote_tolerance_ticks": 3.0,
        "post_only_safety_ticks": 0.0,
        "reject_cooldown_sec": 5.0,
        "min_order_notional_usd": 18.0,
        "feed_stale_seconds": 30.0,
        "ws_reconnect_cooldown_sec": 15.0,
        "loss_cooldown_sec": 30.0,
        "entry_streak_window_sec": 60.0,
        "entry_streak_cooldown_sec": 5.0,
        "max_same_side_entry_streak": 99,
        "loss_reduce_roe_pct": 0.7,
        "urgent_exit_roe_pct": 1.1,
        "stop_loss_roe_pct": 2.8,
        "momentum_threshold": 0.0009,
        "professional_mm": {
            "enabled": True,
            "name": "Avellaneda Stoikov MM",
            "risk_aversion": 0.14,
            "intensity_k": 1.25,
            "horizon_sec": 8.0,
            "min_sigma_bps": 0.35,
            "max_sigma_bps": 12.0,
            "inventory_linear_bps": 1.10,
            "inventory_spread_bps": 0.45,
            "max_inventory_skew_ticks": 7.0,
            "adverse_selection_bps": 0.35,
            "microprice_weight": 0.35,
            "target_fill_rate_per_min": 1.0,
            "fill_shortfall_tighten_ticks": 1.0,
            "cost_widen_threshold_per_million": 45.0,
            "cost_widen_ticks": 1.5
        },
        "mean_reversion": {"enabled": False},
        "market_data": {
            "enabled": True,
            "depth_levels": 5,
            "strong_depth_usd": 750000.0,
            "thin_depth_usd": 180000.0,
            "tight_spread_ticks": 3.0,
            "quiet_momentum": 0.00035,
            "trend_guard_momentum": 0.00075,
            "trend_guard_rsi": 68.0,
            "calm_tighten_ticks": 1.5,
            "trend_widen_ticks": 5.0,
            "thin_widen_ticks": 3.0,
            "block_fade_side": False
        },
        "microstructure": {
            "enabled": True,
            "min_edge_bps": -0.10,
            "block_negative_edge": False,
            "allow_probe_quote": True,
            "probe_size_factor": 0.25,
            "probe_extra_ticks": 3,
            "min_markout_samples": 12,
            "probation_allow_neutral": True
        },
        "adaptive": {
            "enabled": True,
            "bar_seconds": 5,
            "er_period": 20,
            "vp_bars": 90,
            "vp_bins": 24,
            "value_area_pct": 0.70,
            "atr_period": 14,
            "w_er": 0.6,
            "w_vp": 0.4,
            "enter_trend_score": 0.62,
            "exit_trend_score": 0.48,
            "poc_anchor_weight": 0.25,
            "atr_spread_mult": 0.5,
            "min_bars": 40,
            "vp_compress_lo": 0.35,
            "vp_compress_hi": 0.80
        }
    },
    "CL": {
        "symbol": "xyz:CL",
        "tick_size": 0.01,
        "min_size": 0.01,
        "order_size_usd": 120.0,
        "max_position_usd": 260.0,
        "target_leverage": 20.0,
        "num_levels": 2,
        "level_multipliers": [1.0, 0.6],
        "base_spread_ticks": 9,
        "min_spread_ticks": 6,
        "max_spread_ticks": 18,
        "level_tick_spacing": 5,
        "price_drift_threshold_ticks": 12,
        "urgent_drift_threshold_ticks": 24,
        "min_requote_interval_sec": 3.0,
        "max_quote_ttl_sec": 14.0,
        "periodic_sync_seconds": 24.0,
        "max_cancels_per_min": 18,
        "same_quote_tolerance_ticks": 4.0,
        "post_only_safety_ticks": 1.0,
        "reject_cooldown_sec": 8.0,
        "min_order_notional_usd": 18.0,
        "loss_cooldown_sec": 100.0,
        "entry_streak_window_sec": 70.0,
        "entry_streak_cooldown_sec": 55.0,
        "max_same_side_entry_streak": 2,
        "loss_reduce_roe_pct": 0.9,
        "urgent_exit_roe_pct": 1.2,
        "stop_loss_roe_pct": 1.8,
        "momentum_threshold": 0.00045,
        "market_data": {
            "enabled": True,
            "depth_levels": 5,
            "strong_depth_usd": 450000.0,
            "thin_depth_usd": 90000.0,
            "tight_spread_ticks": 3.0,
            "quiet_momentum": 0.00022,
            "trend_guard_momentum": 0.00036,
            "trend_guard_rsi": 63.0,
            "calm_tighten_ticks": 1.5,
            "trend_widen_ticks": 5.0,
            "thin_widen_ticks": 4.0,
            "block_fade_side": True
        }
    },
    "COPPER": {
        "symbol": "xyz:COPPER",
        "tick_size": 0.0001,
        "min_size": 0.1,
        "order_size_usd": 150.0,
        "max_position_usd": 400.0,
        "target_leverage": 20.0,
        "num_levels": 4,
        "level_multipliers": [2.0, 1.5, 1.0, 0.5]
    },
    "PLTR": {
        "symbol": "xyz:PLTR",
        "tick_size": 0.01,
        "min_size": 0.1,
        "order_size_usd": 100.0,
        "max_position_usd": 300.0,
        "target_leverage": 10.0,
        "num_levels": 4,
        "level_multipliers": [2.0, 1.5, 1.0, 0.5]
    },
    "TSLA": {
        "symbol": "xyz:TSLA",
        "tick_size": 0.01,
        "min_size": 0.01,
        "order_size_usd": 150.0,
        "max_position_usd": 400.0,
        "target_leverage": 10.0,
        "num_levels": 4,
        "level_multipliers": [2.0, 1.5, 1.0, 0.5]
    }
}

_XYZ100_SINGLE_LEVEL_PROFILE_KEYS = (
    "order_size_usd",
    "max_position_usd",
    "num_levels",
    "level_multipliers",
    "base_spread_ticks",
    "min_spread_ticks",
    "max_spread_ticks",
    "price_drift_threshold_ticks",
    "urgent_drift_threshold_ticks",
    "min_requote_interval_sec",
    "max_quote_ttl_sec",
    "periodic_sync_seconds",
    "max_cancels_per_min",
    "same_quote_tolerance_ticks",
    "post_only_safety_ticks",
    "reject_cooldown_sec",
    "min_order_notional_usd",
    "feed_stale_seconds",
    "ws_reconnect_cooldown_sec",
    "loss_cooldown_sec",
    "entry_streak_window_sec",
    "entry_streak_cooldown_sec",
    "max_same_side_entry_streak",
    "loss_reduce_roe_pct",
    "urgent_exit_roe_pct",
    "stop_loss_roe_pct",
    "momentum_threshold",
    "professional_mm",
    "mean_reversion",
    "market_data",
    "microstructure",
    "adaptive",
)
for _profile_token in ("SP500",):
    for _profile_key in _XYZ100_SINGLE_LEVEL_PROFILE_KEYS:
        AVAILABLE_TOKENS[_profile_token][_profile_key] = copy.deepcopy(AVAILABLE_TOKENS["XYZ100"][_profile_key])

for _token_name, _token_cfg in AVAILABLE_TOKENS.items():
    _token_cfg.setdefault("indicators", _indicator_defaults_for_token(_token_name))
    _token_cfg.setdefault("microstructure", _microstructure_defaults_for_token(_token_name))
    _token_cfg.setdefault("breakout_guard", _breakout_guard_defaults_for_token(_token_name))

# Choisir le token à trader (sera remplacé par le menu interactif)
ACTIVE_TOKEN = "XYZ100"
TOKEN_CONFIG = AVAILABLE_TOKENS[ACTIVE_TOKEN]
SYMBOL = TOKEN_CONFIG["symbol"]
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

# --- 3. GESTION DU RISQUE ULTRA-SAFE ---
STOP_LOSS_USD = 5       # Stop loss avec ordres limit agressifs ($0.20)
STOP_LOSS_MARKET_USD = 8  # Stop loss MARKET - ferme immédiatement si perte > $0.35
STOP_LOSS_ROE_PCT = 50    # Stop loss MARKET si ROE% <= -1.0%
ENABLE_STOP_LOSS_USD = False  # Si True, garde aussi le déclenchement en $ (STOP_LOSS_MARKET_USD)
QUICK_EXIT_THRESHOLD = 0.05  # Exit rapide à $0.05 de perte
LOSS_REDUCE_ROE_PCT = 1.0
URGENT_EXIT_ROE_PCT = 1.5
MAX_COST_PER_MILLION = 500.0 # Kill switch plus strict
MIN_FILLS_FOR_KILL_SWITCH = 10
MIN_VOLUME_FOR_KILL_SWITCH_USD = 500.0

# --- 4. GRID ULTRA-DENSE POUR MAX VOLUME ---
LEVEL_TICK_SPACING = 1  # 0.01$ entre les niveaux

# --- 5. SPREAD & SKEW INTELLIGENT ---
MIN_SPREAD_TICKS = 1    # Spread minimal 1 tick pour éviter les rejets
BASE_SPREAD_TICKS = 1   # Spread de base (0.02$)
MAX_SPREAD_TICKS = 2    # Spread max en haute volatilité (0.05$)
POSITION_SKEW_FACTOR = 0.3  # Skew agressif pour réduire l'inventaire vite
INVENTORY_TARGET = 0.0    # Position cible = flat
GAMMA = 0.3  # Risk aversion plus bas = plus de volume

# --- 6. DÉTECTION DE MOMENTUM ---
MOMENTUM_THRESHOLD = 0.0001  # 0.01% pour détecter les mouvements rapides - TRÈS SENSIBLE
VOLATILITY_WINDOW = 30  # Fenêtre de calcul de volatilité (ticks)

# --- 7. MODE HAUTE VITESSE ---
US_MARKET_MODE = True
BATCH_UPDATE_ONLY = True

# --- 8. VITESSES DE REFRESH ---
REFRESH_SPEED_FAST = 0.1    # 100ms pour les urgences (stop loss)
REFRESH_SPEED_NORMAL = 0.5  # 500ms normal - laisser les ordres vivre!
REFRESH_SPEED_SLOW = 2.0    # 2 secondes polling de base
POSITION_API_SYNC_INTERVAL_SEC = 1.0  # Sync API position/PnL pour ne pas rater un stop

# --- 8b. LOGIQUE INTELLIGENTE DE REFRESH ---
# Refresh UNIQUEMENT si le prix a bougé BEAUCOUP par rapport aux ordres actuels
PRICE_DRIFT_THRESHOLD_TICKS = 3  # Refresh si mid a bougé de 3+ ticks
URGENT_DRIFT_THRESHOLD_TICKS = 5  # Refresh urgent si 5+ ticks de drift
PERIODIC_SYNC_SECONDS = 10  # Sync périodique toutes les 10 secondes
MIN_REQUOTE_INTERVAL_SEC = 2.5  # Laisse les quotes respirer avant cancel/replace non urgent

# --- 9. PARAMÈTRES AVANCÉS ---
REQUOTE_ON_FILL = True      # Requote instantané après chaque fill
SMART_SPREAD_ENABLED = True # Ajuste le spread selon la volatilité
ANTI_ADVERSE_SELECTION = False  # DÉSACTIVÉ - bloquait les ordres!
FILL_RATE_TARGET = 0.4  # Cible 40% des ordres remplis (ajuste spread si trop/pas assez)
POST_ONLY_MM_QUOTES = True  # Mode normal: maker-only (Alo)
STRICT_POST_ONLY_FLOW = True  # N'autorise pas de quotes agressives hors close explicite

# --- 10. SCORE OPTIMIZATION / AUTO-TUNING ---
SCORE_LAMBDA_COST = 1.0
SCORE_GAMMA_INVENTORY = 0.35
SCORE_TAU_TOXIC = 0.70
AUTO_TUNE_INTERVAL_SEC = 60
AUTO_TUNE_STEP = 0.05
MIN_SCORE_WEIGHT = 0.10
MAX_SCORE_WEIGHT = 3.00

# --- 11. MICROSTRUCTURE / QUOTE QUALITY ---
L2_IMBALANCE_LEVELS = 3
MAX_QUOTE_TTL_SEC = 6.0
LATENCY_WIDENING_MS_REF = 250.0
MAX_CANCELS_PER_MIN = 24
LOSS_COOLDOWN_SEC = 90.0
ENTRY_STREAK_WINDOW_SEC = 60.0
ENTRY_STREAK_COOLDOWN_SEC = 45.0
MAX_SAME_SIDE_ENTRY_STREAK = 3

# --- 12. TOXIC FLOW / RISK GUARDS ---
TOXIC_FLOW_WINDOW_SEC = 20.0
TOXIC_FLOW_PAUSE_SEC = 5.0
TOXIC_ADVERSE_BPS_TRIGGER = 2.5
MAX_TOXIC_SCORE_BEFORE_PAUSE = 0.7
FEED_STALE_SECONDS = 4.0
MAX_P95_SLIPPAGE_BPS = 4.0
MAX_INVENTORY_HARD_PCT = 1.25

# --- 13. HARD-STOP TOGGLES (avoid unwanted full bot stops) ---
ENABLE_COST_HARD_STOP = True
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

# --- 14b. ORDER CHURN / REJECT GUARDS ---
MIN_ORDER_NOTIONAL_USD = 12.0
SAME_QUOTE_TOLERANCE_TICKS = 1.0
POST_ONLY_SAFETY_TICKS = 0.0
REJECT_COOLDOWN_SEC = 7.0
EMPTY_QUOTE_COOLDOWN_SEC = 3.0

# --- 15. RUNTIME LOG / ROE STOP RECOVERY ---
VERBOSE_RUNTIME_UPDATES = False
ROE_STOP_RETRY_COOLDOWN_SEC = 5.0

# =============================================================================
# MARKET MAKER CLASS - ULTRA OPTIMIZED
# =============================================================================

@dataclass(frozen=True)
class IndicatorConfig:
    enabled: bool = True
    ema_fast_period: int = 12
    ema_slow_period: int = 26
    rsi_period: int = 14
    rsi_overbought: float = 70.0
    rsi_oversold: float = 30.0
    rsi_extreme_overbought: float = 80.0
    rsi_extreme_oversold: float = 20.0
    bollinger_period: int = 20
    bollinger_std: float = 2.0
    bollinger_squeeze_threshold: float = 0.0008
    bollinger_expansion_threshold: float = 0.004
    vwap_window_sec: int = 300
    indicator_skew_weight: float = 0.50
    indicator_spread_weight: float = 0.40
    vwap_blend_weight: float = 0.15
    rsi_quote_suppression: bool = True
    take_profit_enabled: bool = True
    tp_rsi_overbought: float = 75.0
    tp_rsi_oversold: float = 25.0
    tp_base_offset_ticks: int = 2
    tp_size_pct: float = 0.35
    tp_extra_edge_bps: float = 0.40
    ema_cross_exit_enabled: bool = True
    ema_cross_market_exit: bool = False
    maker_fee_per_million: float = MAKER_FEE_PER_MILLION_USD
    fee_floor_enabled: bool = True
    min_half_spread_bps: float = DEFAULT_MIN_HALF_SPREAD_BPS
    adverse_buffer_bps: float = DEFAULT_ADVERSE_BUFFER_BPS
    squeeze_tighten_ticks: float = 0.0
    squeeze_size_factor: float = 1.0
    expansion_size_factor: float = 0.60
    metadata: Dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_token_config(cls, token_cfg: Dict[str, Any]) -> "IndicatorConfig":
        raw = dict(token_cfg.get("indicators", {}))
        allowed = set(cls.__dataclass_fields__.keys())
        clean = {k: v for k, v in raw.items() if k in allowed}
        return cls(**clean)


@dataclass(frozen=True)
class IndicatorSnapshot:
    ts: float = 0.0
    last_bar_ts: int = 0
    ready: bool = False
    bar_count: int = 0
    last_close: Optional[float] = None
    ema_fast: Optional[float] = None
    ema_slow: Optional[float] = None
    ema_diff_bps: float = 0.0
    ema_cross_up: bool = False
    ema_cross_down: bool = False
    rsi: Optional[float] = None
    bollinger_mid: Optional[float] = None
    bollinger_upper: Optional[float] = None
    bollinger_lower: Optional[float] = None
    bollinger_bandwidth: Optional[float] = None
    vwap: Optional[float] = None
    vwap_samples: int = 0
    on_tick_latency_ms: float = 0.0


class IndicatorEngine:
    """Incremental technical indicators fed by L2 ticks and user fills."""

    def __init__(self, cfg: IndicatorConfig):
        self.cfg = cfg
        self.lock = threading.Lock()
        self.tick_buffer: deque = deque(maxlen=600)
        self.bar_1s: deque = deque(maxlen=30000)
        self.bar_5s: deque = deque(maxlen=6000)
        self.returns_buffer: deque = deque(maxlen=50)
        self.vwap_window: deque = deque(maxlen=max(300, cfg.vwap_window_sec * 2))

        self._current_sec: Optional[int] = None
        self._current_bar: Optional[Dict[str, float]] = None
        self._last_vwap_tick_sec: Optional[int] = None
        self._last_close: Optional[float] = None
        self._ema_fast: Optional[float] = None
        self._ema_slow: Optional[float] = None
        self._last_ema_diff: Optional[float] = None
        self._last_cross_up = False
        self._last_cross_down = False
        self._rsi_seed_gains: deque = deque(maxlen=max(1, cfg.rsi_period))
        self._rsi_seed_losses: deque = deque(maxlen=max(1, cfg.rsi_period))
        self._rsi_avg_gain: Optional[float] = None
        self._rsi_avg_loss: Optional[float] = None
        self._rsi: Optional[float] = None
        self._bb_mid: Optional[float] = None
        self._bb_upper: Optional[float] = None
        self._bb_lower: Optional[float] = None
        self._bb_bandwidth: Optional[float] = None
        self._last_tick_latency_ms = 0.0
        self._snapshot = IndicatorSnapshot()

    def snapshot(self) -> IndicatorSnapshot:
        if not self.cfg.enabled:
            return IndicatorSnapshot()
        with self.lock:
            return self._snapshot

    def get_bars(self, timeframe_sec: int = 1, max_bars: int = 300, include_partial: bool = False) -> List[Dict[str, float]]:
        """Return closed bars aggregated to the requested timeframe."""
        timeframe_sec = max(1, int(timeframe_sec))
        max_bars = max(1, int(max_bars))
        with self.lock:
            if timeframe_sec <= 1:
                return [dict(b) for b in list(self.bar_1s)[-max_bars:]]
            source = list(self.bar_1s)

        if not source:
            return []

        aggregated: List[Dict[str, float]] = []
        current: Optional[Dict[str, float]] = None
        for raw in source:
            ts = int(float(raw["ts"]))
            bucket_ts = float(ts - (ts % timeframe_sec))
            if current is None or current["ts"] != bucket_ts:
                if current is not None:
                    aggregated.append(current)
                current = {
                    "ts": bucket_ts,
                    "o": float(raw["o"]),
                    "h": float(raw["h"]),
                    "l": float(raw["l"]),
                    "c": float(raw["c"]),
                    "v": float(raw.get("v", 1.0)),
                }
            else:
                current["h"] = max(current["h"], float(raw["h"]))
                current["l"] = min(current["l"], float(raw["l"]))
                current["c"] = float(raw["c"])
                current["v"] += float(raw.get("v", 1.0))

        if current is not None:
            aggregated.append(current)
        if timeframe_sec > 1 and aggregated and not include_partial:
            latest_source_ts = int(float(source[-1]["ts"]))
            last_bucket_ts = int(float(aggregated[-1]["ts"]))
            if latest_source_ts < last_bucket_ts + timeframe_sec - 1:
                aggregated.pop()
        return aggregated[-max_bars:]

    def get_developing_bar(self) -> Optional[Dict[str, float]]:
        """Snapshot of the in-progress 1s bar (open/high/low/close/volume)."""
        with self.lock:
            if self._current_bar is None:
                return None
            return dict(self._current_bar)

    def get_recent_ticks(self, max_seconds: float, now_ts: Optional[float] = None) -> List[Tuple[float, float, float, float]]:
        """Ticks within the last `max_seconds`, oldest first. Each tick = (ts, bid, ask, mid)."""
        if not self.cfg.enabled:
            return []
        cutoff = (now_ts if now_ts is not None else time.time()) - max(0.5, max_seconds)
        with self.lock:
            return [tick for tick in self.tick_buffer if tick[0] >= cutoff]

    def on_tick(self, best_bid: float, best_ask: float, bid_size: float = 1.0, ask_size: float = 1.0, ts: Optional[float] = None):
        if not self.cfg.enabled or best_bid <= 0 or best_ask <= 0:
            return

        start = time.perf_counter()
        now_ts = ts if ts is not None else time.time()
        mid = (best_bid + best_ask) / 2.0
        sec = int(now_ts)

        with self.lock:
            self.tick_buffer.append((now_ts, best_bid, best_ask, mid))
            if self._last_vwap_tick_sec != sec:
                synthetic_weight = max(0.000001, min(1.0, (abs(bid_size) + abs(ask_size)) / 2.0))
                self._add_vwap_locked(now_ts, mid, synthetic_weight)
                self._last_vwap_tick_sec = sec
            self._update_bar_locked(sec, mid)
            self._prune_vwap_locked(now_ts)
            self._last_tick_latency_ms = (time.perf_counter() - start) * 1000.0
            self._snapshot = self._build_snapshot_locked(now_ts)

    def on_fill(self, price: float, size: float, ts: Optional[float] = None):
        if not self.cfg.enabled or price <= 0 or size <= 0:
            return
        now_ts = ts if ts is not None else time.time()
        with self.lock:
            self._add_vwap_locked(now_ts, price, abs(size))
            self._prune_vwap_locked(now_ts)
            self._snapshot = self._build_snapshot_locked(now_ts)

    def _update_bar_locked(self, sec: int, price: float):
        if self._current_sec is None:
            self._current_sec = sec
            self._current_bar = {"ts": float(sec), "o": price, "h": price, "l": price, "c": price, "v": 1.0}
            return

        if sec == self._current_sec and self._current_bar is not None:
            self._current_bar["h"] = max(self._current_bar["h"], price)
            self._current_bar["l"] = min(self._current_bar["l"], price)
            self._current_bar["c"] = price
            self._current_bar["v"] += 1.0
            return

        if self._current_bar is not None:
            self._finalize_bar_locked(self._current_bar)

        self._current_sec = sec
        self._current_bar = {"ts": float(sec), "o": price, "h": price, "l": price, "c": price, "v": 1.0}

    def _finalize_bar_locked(self, bar: Dict[str, float]):
        close = float(bar["c"])
        self.bar_1s.append(dict(bar))

        if self._last_close and self._last_close > 0:
            self.returns_buffer.append((close - self._last_close) / self._last_close)

        self._update_ema_locked(close)
        self._update_rsi_locked(close)
        self._update_bollinger_locked()
        self._update_bar_5s_locked()
        self._last_close = close

    def _update_ema_locked(self, close: float):
        fast_alpha = 2.0 / (max(1, self.cfg.ema_fast_period) + 1.0)
        slow_alpha = 2.0 / (max(1, self.cfg.ema_slow_period) + 1.0)
        self._ema_fast = close if self._ema_fast is None else (fast_alpha * close + (1.0 - fast_alpha) * self._ema_fast)
        self._ema_slow = close if self._ema_slow is None else (slow_alpha * close + (1.0 - slow_alpha) * self._ema_slow)

        diff = self._ema_fast - self._ema_slow
        self._last_cross_up = False
        self._last_cross_down = False
        if self._last_ema_diff is not None:
            self._last_cross_up = self._last_ema_diff <= 0 < diff
            self._last_cross_down = self._last_ema_diff >= 0 > diff
        self._last_ema_diff = diff

    def _update_rsi_locked(self, close: float):
        if self._last_close is None or self.cfg.rsi_period <= 0:
            return

        change = close - self._last_close
        gain = max(change, 0.0)
        loss = max(-change, 0.0)

        if self._rsi_avg_gain is None or self._rsi_avg_loss is None:
            self._rsi_seed_gains.append(gain)
            self._rsi_seed_losses.append(loss)
            if len(self._rsi_seed_gains) < self.cfg.rsi_period:
                return
            self._rsi_avg_gain = sum(self._rsi_seed_gains) / self.cfg.rsi_period
            self._rsi_avg_loss = sum(self._rsi_seed_losses) / self.cfg.rsi_period
        else:
            period = float(self.cfg.rsi_period)
            self._rsi_avg_gain = ((period - 1.0) * self._rsi_avg_gain + gain) / period
            self._rsi_avg_loss = ((period - 1.0) * self._rsi_avg_loss + loss) / period

        if self._rsi_avg_loss == 0 and self._rsi_avg_gain == 0:
            self._rsi = 50.0
        elif self._rsi_avg_loss == 0:
            self._rsi = 100.0
        elif self._rsi_avg_gain == 0:
            self._rsi = 0.0
        else:
            rs = self._rsi_avg_gain / self._rsi_avg_loss
            self._rsi = 100.0 - (100.0 / (1.0 + rs))

    def _update_bollinger_locked(self):
        period = max(2, self.cfg.bollinger_period)
        if len(self.bar_1s) < period:
            return
        closes = [float(b["c"]) for b in list(self.bar_1s)[-period:]]
        mean = sum(closes) / len(closes)
        variance = sum((x - mean) ** 2 for x in closes) / len(closes)
        std = math.sqrt(max(variance, 0.0))
        self._bb_mid = mean
        self._bb_upper = mean + self.cfg.bollinger_std * std
        self._bb_lower = mean - self.cfg.bollinger_std * std
        self._bb_bandwidth = ((self._bb_upper - self._bb_lower) / mean) if mean > 0 else 0.0

    def _update_bar_5s_locked(self):
        if len(self.bar_1s) < 5:
            return
        last_bar_ts = int(self.bar_1s[-1]["ts"])
        if last_bar_ts % 5 != 0:
            return
        bars = list(self.bar_1s)[-5:]
        self.bar_5s.append({
            "ts": bars[0]["ts"],
            "o": bars[0]["o"],
            "h": max(b["h"] for b in bars),
            "l": min(b["l"] for b in bars),
            "c": bars[-1]["c"],
            "v": sum(b["v"] for b in bars),
        })

    def _add_vwap_locked(self, ts: float, price: float, size: float):
        self.vwap_window.append((ts, price, max(size, 0.000001)))

    def _prune_vwap_locked(self, now_ts: float):
        cutoff = now_ts - max(1, self.cfg.vwap_window_sec)
        while self.vwap_window and self.vwap_window[0][0] < cutoff:
            self.vwap_window.popleft()

    def _vwap_locked(self) -> Optional[float]:
        if not self.vwap_window:
            return None
        denom = sum(x[2] for x in self.vwap_window)
        if denom <= 0:
            return None
        return sum(x[1] * x[2] for x in self.vwap_window) / denom

    def _build_snapshot_locked(self, now_ts: float) -> IndicatorSnapshot:
        last_close = self._current_bar["c"] if self._current_bar else self._last_close
        ema_diff_bps = 0.0
        if self._ema_fast is not None and self._ema_slow is not None and last_close and last_close > 0:
            ema_diff_bps = (self._ema_fast - self._ema_slow) / last_close * 10000.0

        ready = (
            self._ema_fast is not None and
            self._ema_slow is not None and
            self._rsi is not None and
            self._bb_bandwidth is not None
        )
        last_bar_ts = int(self.bar_1s[-1]["ts"]) if self.bar_1s else 0
        return IndicatorSnapshot(
            ts=now_ts,
            last_bar_ts=last_bar_ts,
            ready=ready,
            bar_count=len(self.bar_1s),
            last_close=last_close,
            ema_fast=self._ema_fast,
            ema_slow=self._ema_slow,
            ema_diff_bps=ema_diff_bps,
            ema_cross_up=self._last_cross_up,
            ema_cross_down=self._last_cross_down,
            rsi=self._rsi,
            bollinger_mid=self._bb_mid,
            bollinger_upper=self._bb_upper,
            bollinger_lower=self._bb_lower,
            bollinger_bandwidth=self._bb_bandwidth,
            vwap=self._vwap_locked(),
            vwap_samples=len(self.vwap_window),
            on_tick_latency_ms=self._last_tick_latency_ms,
        )


@dataclass(frozen=True)
class MicrostructureSnapshot:
    ts: float = 0.0
    ready: bool = False
    microprice: Optional[float] = None
    microprice_bps: float = 0.0
    weighted_mid: Optional[float] = None
    spread_ticks: float = 0.0
    imbalance_l1: float = 0.0
    imbalance_l3: float = 0.0
    imbalance_l5: float = 0.0
    ofi_norm: float = 0.0
    ofi_bps: float = 0.0
    pressure_bps: float = 0.0
    depth_usd: float = 0.0
    adverse_markout_1s_bps: float = 0.0
    adverse_markout_5s_bps: float = 0.0
    adverse_markout_15s_bps: float = 0.0
    edge_buy_bps: float = 0.0
    edge_sell_bps: float = 0.0
    markout_samples: int = 0
    signal_conflict: bool = False
    toxic: bool = False


class BreakoutGuard:
    """
    Pre-detector for big candles. Combines two leading signals:
      1) Range vs ATR baseline on the developing 1s bar (intra-bar)
      2) Tick velocity vs EWMA stddev baseline (sub-second)
    On trigger, marks the exposed side (the side that would get run over)
    and arms a short pause window so the bot mutes that side and cancels
    its working orders. Opposite side stays alive but is widened by the
    caller. Symmetric: handles pumps and dumps the same way.
    """

    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg or {}
        self.pause_until: float = 0.0
        self.exposed_side: Optional[str] = None
        self.last_trigger_ts: float = 0.0
        self.last_trigger_reason: str = ""
        self.atr_bps_ema: Optional[float] = None
        self.velocity_sigma_bps: Optional[float] = None
        self._velocity_samples: int = 0
        self._atr_samples: int = 0
        self._last_bar_ts_seen: Optional[float] = None
        self.trigger_count: int = 0

    def _c(self, key: str, default: Any) -> Any:
        return self.cfg.get(key, default)

    def enabled(self) -> bool:
        return bool(self._c("enabled", True))

    def update_atr(self, closed_bars: List[Dict[str, float]], mid: float) -> None:
        """EWMA of (high-low)/mid in bps, using only newly closed bars."""
        if not closed_bars or mid <= 0:
            return
        alpha = float(self._c("atr_alpha", 0.15))
        for bar in closed_bars:
            ts = float(bar.get("ts", 0.0))
            if self._last_bar_ts_seen is not None and ts <= self._last_bar_ts_seen:
                continue
            high = float(bar.get("h", 0.0))
            low = float(bar.get("l", 0.0))
            if high <= 0 or low <= 0 or high < low:
                continue
            range_bps = (high - low) / mid * 10000.0
            if self.atr_bps_ema is None:
                self.atr_bps_ema = range_bps
            else:
                self.atr_bps_ema = (1.0 - alpha) * self.atr_bps_ema + alpha * range_bps
            self._atr_samples += 1
            self._last_bar_ts_seen = ts

    def update_velocity_sigma(self, return_bps_abs: float) -> None:
        """EWMA of |return_2s| in bps. Called once per evaluation."""
        alpha = float(self._c("velocity_sigma_alpha", 0.05))
        if self.velocity_sigma_bps is None:
            self.velocity_sigma_bps = max(return_bps_abs, 0.1)
        else:
            self.velocity_sigma_bps = (1.0 - alpha) * self.velocity_sigma_bps + alpha * return_bps_abs
        self._velocity_samples += 1

    def _compute_velocity(self, recent_ticks, now_ts: float) -> Tuple[float, float]:
        """Return (signed_return_bps, abs_return_bps) over velocity_window_sec."""
        if not recent_ticks:
            return (0.0, 0.0)
        window = float(self._c("velocity_window_sec", 2.0))
        cutoff = now_ts - window
        old_mid: Optional[float] = None
        new_mid: Optional[float] = None
        for ts, _bid, _ask, mid in recent_ticks:
            if mid <= 0:
                continue
            if old_mid is None and ts >= cutoff:
                old_mid = mid
            new_mid = mid
        if old_mid is None or new_mid is None or old_mid <= 0:
            return (0.0, 0.0)
        ret = (new_mid - old_mid) / old_mid * 10000.0
        return (ret, abs(ret))

    def evaluate(
        self,
        now_ts: float,
        mid: float,
        current_bar: Optional[Dict[str, float]],
        recent_ticks,
        closed_bars: List[Dict[str, float]],
    ) -> Optional[Tuple[str, str]]:
        """Return (exposed_side, reason) on trigger, else None."""
        if not self.enabled() or mid <= 0:
            return None

        self.update_atr(closed_bars, mid)
        signed_ret_bps, abs_ret_bps = self._compute_velocity(recent_ticks, now_ts)
        self.update_velocity_sigma(abs_ret_bps)

        min_samples = int(self._c("min_baseline_samples", 10))
        if self._atr_samples < min_samples or self._velocity_samples < min_samples:
            return None

        if now_ts - self.last_trigger_ts < float(self._c("anti_spam_sec", 1.5)):
            return None

        reasons: List[str] = []
        side: Optional[str] = None

        # Signal 1: range vs ATR on developing bar
        atr = self.atr_bps_ema or 0.0
        if atr >= float(self._c("min_atr_bps", 0.8)) and current_bar is not None:
            high = float(current_bar.get("h", 0.0))
            low = float(current_bar.get("l", 0.0))
            open_ = float(current_bar.get("o", 0.0))
            close = float(current_bar.get("c", 0.0))
            if high > 0 and low > 0 and high >= low:
                range_bps = (high - low) / mid * 10000.0
                if range_bps > float(self._c("atr_mult", 2.0)) * atr:
                    reasons.append(f"range={range_bps:.1f}bps>{self._c('atr_mult', 2.0)}xATR({atr:.1f})")
                    if close >= open_:
                        side = "ask"
                    else:
                        side = "bid"

        # Signal 2: tick velocity vs sigma baseline
        sigma = self.velocity_sigma_bps or 0.0
        vel_min = float(self._c("velocity_min_bps", 1.5))
        vel_mult = float(self._c("velocity_sigma_mult", 2.0))
        if (
            sigma > 0.0
            and abs_ret_bps > vel_min
            and abs_ret_bps > vel_mult * sigma
        ):
            reasons.append(f"vel={abs_ret_bps:.1f}bps>{vel_mult}xsigma({sigma:.1f})")
            if signed_ret_bps > 0:
                side = "ask"
            elif signed_ret_bps < 0:
                side = "bid"

        if not reasons or side is None:
            return None

        # Arm or extend the pause; flip side if direction reversed mid-pause
        pause_sec = float(self._c("pause_sec", 6.0))
        new_until = now_ts + pause_sec
        self.pause_until = max(self.pause_until, new_until)
        self.exposed_side = side
        self.last_trigger_ts = now_ts
        self.last_trigger_reason = " + ".join(reasons)
        self.trigger_count += 1
        return (side, self.last_trigger_reason)

    def is_paused(self, side: str, now_ts: float) -> bool:
        if not self.enabled():
            return False
        return now_ts < self.pause_until and self.exposed_side == side


class AdaptiveRegimeEngine:
    """
    "Adaptive" market-regime classifier (Tread.fi-style).

    Each update it scores the market regime from live candles:
      - Kaufman Efficiency Ratio (ER): net move / total path.
        Near 0 = chop, near 1 = clean trend.
      - Volume-profile compression: width of the ~70% value area relative
        to the full traded range. A tight value area = compression (chop),
        a stretched one = expansion (trend).

    Both blend into a trend_score in [0, 1] with hysteresis (enter trend
    above `enter_trend_score`, exit below `exit_trend_score`) so the regime
    never flip-flops on noise.

    CHOP  -> quote the grid, anchored on the value area (POC), spread scaled
             on ATR with a hard floor that always covers fees.
    TREND -> defense first: the MarketMaker goes flatten-only, quoting only
             the side that reduces its position, then pauses until the score
             cools back down.
    """

    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = dict(cfg or {})
        self.enabled = bool(self.cfg.get("enabled", False))
        self.bar_seconds = int(self.cfg.get("bar_seconds", 5))
        self.er_period = int(self.cfg.get("er_period", 20))
        self.vp_bars = int(self.cfg.get("vp_bars", 90))
        self.vp_bins = int(self.cfg.get("vp_bins", 24))
        self.value_area_pct = float(self.cfg.get("value_area_pct", 0.70))
        self.atr_period = int(self.cfg.get("atr_period", 14))
        self.w_er = float(self.cfg.get("w_er", 0.6))
        self.w_vp = float(self.cfg.get("w_vp", 0.4))
        self.enter_trend_score = float(self.cfg.get("enter_trend_score", 0.62))
        self.exit_trend_score = float(self.cfg.get("exit_trend_score", 0.48))
        self.poc_anchor_weight = float(self.cfg.get("poc_anchor_weight", 0.25))
        self.atr_spread_mult = float(self.cfg.get("atr_spread_mult", 0.5))
        self.min_bars = int(self.cfg.get("min_bars", 40))
        self.vp_compress_lo = float(self.cfg.get("vp_compress_lo", 0.35))
        self.vp_compress_hi = float(self.cfg.get("vp_compress_hi", 0.80))

        # Live state
        self.ready = False
        self.is_trend = False
        self.trend_score = 0.0
        self.efficiency_ratio = 0.0
        self.vp_expansion = 0.0
        self.trend_dir = 0          # +1 up, -1 down, 0 flat
        self.poc: Optional[float] = None
        self.vah: Optional[float] = None
        self.val: Optional[float] = None
        self.atr = 0.0

    @staticmethod
    def _clamp01(x: float) -> float:
        return 0.0 if x < 0.0 else (1.0 if x > 1.0 else x)

    def _kaufman_er(self, closes: List[float]) -> Tuple[float, int]:
        n = self.er_period
        if len(closes) < n + 1:
            return 0.0, 0
        window = closes[-(n + 1):]
        net = abs(window[-1] - window[0])
        path = sum(abs(window[i] - window[i - 1]) for i in range(1, len(window)))
        if path <= 1e-12:
            return 0.0, 0
        direction = 1 if window[-1] > window[0] else (-1 if window[-1] < window[0] else 0)
        return self._clamp01(net / path), direction

    def _atr_from_bars(self, bars: List[Dict[str, float]]) -> float:
        n = self.atr_period
        if len(bars) < n + 1:
            return 0.0
        trs = []
        for i in range(len(bars) - n, len(bars)):
            if i <= 0:
                continue
            hi = float(bars[i]["h"])
            lo = float(bars[i]["l"])
            prev_close = float(bars[i - 1]["c"])
            trs.append(max(hi - lo, abs(hi - prev_close), abs(lo - prev_close)))
        return sum(trs) / len(trs) if trs else 0.0

    def _volume_profile(self, bars: List[Dict[str, float]]) -> None:
        """Build a histogram of traded activity per price level over vp_bars."""
        sample = bars[-self.vp_bars:]
        prices = []
        weights = []
        lo = float("inf")
        hi = float("-inf")
        for b in sample:
            typ = (float(b["h"]) + float(b["l"]) + float(b["c"])) / 3.0
            w = max(0.0, float(b.get("v", 1.0)))
            prices.append(typ)
            weights.append(w)
            lo = min(lo, float(b["l"]))
            hi = max(hi, float(b["h"]))
        total_range = hi - lo
        total_w = sum(weights)
        if total_range <= 0 or total_w <= 0:
            self.poc = self.vah = self.val = None
            self.vp_expansion = 0.0
            return

        bins = max(4, self.vp_bins)
        hist = [0.0] * bins
        bin_w = total_range / bins
        for p, w in zip(prices, weights):
            idx = int((p - lo) / bin_w)
            idx = 0 if idx < 0 else (bins - 1 if idx >= bins else idx)
            hist[idx] += w

        poc_idx = max(range(bins), key=lambda i: hist[i])
        # Expand the value area around the POC until it holds value_area_pct.
        target = total_w * self.value_area_pct
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
        va_width = self.vah - self.val
        # Compression: narrow value area vs full range = chop; wide = expansion.
        ratio = va_width / total_range
        span = max(1e-9, self.vp_compress_hi - self.vp_compress_lo)
        self.vp_expansion = self._clamp01((ratio - self.vp_compress_lo) / span)

    def update(self, bars: List[Dict[str, float]], mid_price: float) -> None:
        if not self.enabled or not bars or len(bars) < self.min_bars:
            self.ready = False
            return
        closes = [float(b["c"]) for b in bars]
        er, direction = self._kaufman_er(closes)
        self.efficiency_ratio = er
        self.trend_dir = direction
        self.atr = self._atr_from_bars(bars)
        self._volume_profile(bars)

        wsum = max(1e-9, self.w_er + self.w_vp)
        score = (self.w_er * er + self.w_vp * self.vp_expansion) / wsum
        self.trend_score = score

        # Hysteresis state machine.
        if self.is_trend:
            if score < self.exit_trend_score:
                self.is_trend = False
        else:
            if score > self.enter_trend_score:
                self.is_trend = True
        self.ready = True

    def atr_half_spread(self, mid_price: float) -> float:
        """ATR-scaled half-spread (price units). 0 if not ready."""
        if not self.ready or self.atr <= 0:
            return 0.0
        return self.atr * self.atr_spread_mult

    def status_line(self) -> str:
        if not self.ready:
            return "ADAPTIVE: warming up"
        regime = "TREND" if self.is_trend else "CHOP"
        return (
            f"ADAPTIVE: {regime} score={self.trend_score:.2f} "
            f"ER={self.efficiency_ratio:.2f} VPexp={self.vp_expansion:.2f} "
            f"dir={self.trend_dir:+d}"
        )


class MarketMaker:
    def __init__(self):
        self.wallet = eth_account.Account.from_key(AGENT_PRIVATE_KEY)
        # Pre-fetch a corrected spot_meta to dodge the SDK's positional-index crash.
        fixed_spot_meta = build_fixed_spot_meta(BASE_URL)
        self.fixed_spot_meta = fixed_spot_meta
        self.info = Info(BASE_URL, skip_ws=True, spot_meta=fixed_spot_meta, perp_dexs=[PERP_DEX])
        self.exchange = Exchange(
            self.wallet,
            base_url=BASE_URL,
            account_address=ACCOUNT_ADDRESS,
            spot_meta=fixed_spot_meta,
            perp_dexs=[PERP_DEX]
        )
        
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
        self.last_target_bid_count = 0
        self.last_target_ask_count = 0
        
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
        self.entry_block_until = {"buy": 0.0, "sell": 0.0}
        self.same_side_entry_counts = {"buy": 0, "sell": 0}
        self.last_entry_side = None
        self.last_entry_ts = 0.0
        self.last_guard_reason = ""
        self.session_start_timestamp = int(time.time() * 1000)

        # Execution quality tracking
        self.maker_fills = 0
        self.taker_fills = 0
        self.slippage_bps_history: deque = deque(maxlen=400)
        self.inventory_notional_history: deque = deque(maxlen=600)
        self.toxic_loss_usd = 0.0
        self.useful_volume_usd = 0.0
        self.maker_fees_paid_usd = 0.0
        self.tp_capture_pnl_usd = 0.0
        self.tp_orders_placed = 0
        self.quotes_suppressed_count = 0
        self.directional_skew_bps_history: deque = deque(maxlen=600)
        self.last_ema_exit_bar_ts = 0
        self.indicator_config = IndicatorConfig()
        self.indicators = IndicatorEngine(self.indicator_config)
        self.last_indicator_snapshot = IndicatorSnapshot()
        self.professional_mm_config: Dict[str, Any] = {}
        self.mm_model_state: Dict[str, Any] = {}
        self.mean_reversion_config: Dict[str, Any] = {}
        self.last_mr_state: Dict[str, Any] = {}
        self.mr_last_entry_bar = -(10 ** 9)
        self.mr_last_long_signal_bar = -(10 ** 9)
        self.mr_last_short_signal_bar = -(10 ** 9)
        self.mr_entry_atr: Optional[float] = None
        self.mr_entry_side: Optional[str] = None
        self.microstructure_config: Dict[str, Any] = {}
        self.micro_snapshot = MicrostructureSnapshot()
        self.prev_top_book: Optional[Tuple[float, float, float, float]] = None
        self.ofi_events: deque = deque(maxlen=800)
        self.pending_markouts: deque = deque(maxlen=600)
        # Per-side adverse markout: separate buy and sell deques per horizon.
        # `_adverse_markout_bps(h, side="any")` aggregates both for legacy use.
        self.markout_good_bps = {
            "buy": {
                1.0: deque(maxlen=300),
                5.0: deque(maxlen=300),
                15.0: deque(maxlen=300),
                30.0: deque(maxlen=300),
            },
            "sell": {
                1.0: deque(maxlen=300),
                5.0: deque(maxlen=300),
                15.0: deque(maxlen=300),
                30.0: deque(maxlen=300),
            },
        }
        self.last_micro_pressure_bps = 0.0
        self.micro_conflict_pause_until = 0.0
        self.last_micro_guard_reason = ""

        # Breakout guard (leading anti-spike). Initialized empty here, configured in load_config.
        self.breakout_guard_config: Dict[str, Any] = {}
        self.breakout_guard: Optional[BreakoutGuard] = None
        self.last_breakout_cancel_ts: float = 0.0

        # EV gate diagnostics
        self.ev_reject_count_buy: int = 0
        self.ev_reject_count_sell: int = 0
        self.last_ev_log_ts: float = 0.0

        # Quote/cancel cadence control
        self.last_quote_update_ts = 0.0
        self.quote_lifetimes: deque = deque(maxlen=400)
        self.cancel_events: deque = deque(maxlen=300)
        self.replace_events: deque = deque(maxlen=300)
        self.last_replace_mid = None

        # Feed quality / latency
        self.last_ws_msg_ts = 0.0
        self.last_ws_reconnect_ts = 0.0
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
        self.reject_pause_until = 0.0
        self.empty_quote_pause_until = 0.0
        self.last_reject_reason = ""
        self.roe_stop_active = False
        self.last_roe_stop_attempt_ts = 0.0
        
        # Load Configuration
        self.load_config()
        
    def load_config(self):
        """Load configuration from global constants"""
        config = TOKEN_CONFIG
        self.strategy_name = "MM PRO v4.0 Fee-Aware"
        self.strategy_desc = f"Levier: {config['target_leverage']}x | Grid: {config['num_levels']} lvls | RSI/EMA/BB/VWAP"
        self.professional_mm_config = dict(config.get("professional_mm", {}))
        if self.professional_mm_config.get("enabled", False):
            self.strategy_name = self.professional_mm_config.get("name", "Professional MM")
            self.strategy_desc = (
                f"Reservation-price MM | gamma={float(self.professional_mm_config.get('risk_aversion', 0.14)):.2f} | "
                f"{config['target_leverage']}x post-only"
            )
        self.indicator_config = IndicatorConfig.from_token_config(config)
        self.indicators = IndicatorEngine(self.indicator_config)
        self.last_indicator_snapshot = self.indicators.snapshot()
        self.mean_reversion_config = dict(config.get("mean_reversion", {}))
        if self.mean_reversion_config.get("enabled", False):
            self.strategy_name = self.mean_reversion_config.get("name", "BB Mean Revert")
            tf = int(self.mean_reversion_config.get("signal_bar_seconds", 15))
            target_fills = int(self.mean_reversion_config.get("target_fills_per_day", 0))
            self.strategy_desc = (
                f"XYZ100 BB/RSI/ADX mean-revert | {tf}s bars | "
                f"{config['target_leverage']}x | target {target_fills}/day fills"
            )
        self.microstructure_config = dict(config.get("microstructure", {}))
        self.micro_snapshot = MicrostructureSnapshot()
        self.breakout_guard_config = dict(config.get("breakout_guard", {}))
        self.breakout_guard = BreakoutGuard(self.breakout_guard_config)
        # "Adaptive" regime engine: primary defense (chop = quote, trend = flatten-only).
        self.adaptive_config = dict(config.get("adaptive", {}))
        self.adaptive_engine = AdaptiveRegimeEngine(self.adaptive_config)
        self.last_adaptive_reason = ""
        if self.adaptive_engine.enabled:
            self.strategy_desc += " | Adaptive regime filter"
        
        # Refresh settings - LOGIQUE BASÉE SUR DRIFT DE PRIX
        self.BASE_REFRESH_INTERVAL = REFRESH_SPEED_SLOW  # Polling de base
        self.URGENT_REFRESH_INTERVAL = REFRESH_SPEED_FAST
        self.current_refresh_interval = self.BASE_REFRESH_INTERVAL
        
        # Seuils de drift en ticks
        self.PRICE_DRIFT_TICKS = config.get("price_drift_threshold_ticks", PRICE_DRIFT_THRESHOLD_TICKS)
        self.URGENT_DRIFT_TICKS = config.get("urgent_drift_threshold_ticks", URGENT_DRIFT_THRESHOLD_TICKS)
        self.MIN_REQUOTE_INTERVAL = config.get("min_requote_interval_sec", MIN_REQUOTE_INTERVAL_SEC)
        self.MAX_QUOTE_TTL = config.get("max_quote_ttl_sec", MAX_QUOTE_TTL_SEC)
        self.PERIODIC_SYNC_SECONDS = config.get("periodic_sync_seconds", PERIODIC_SYNC_SECONDS)
        self.MAX_CANCELS_PER_MIN = config.get("max_cancels_per_min", MAX_CANCELS_PER_MIN)
        self.SAME_QUOTE_TOLERANCE_TICKS = config.get("same_quote_tolerance_ticks", SAME_QUOTE_TOLERANCE_TICKS)
        self.POST_ONLY_SAFETY_TICKS = config.get("post_only_safety_ticks", POST_ONLY_SAFETY_TICKS)
        self.REJECT_COOLDOWN_SEC = config.get("reject_cooldown_sec", REJECT_COOLDOWN_SEC)
        self.MIN_ORDER_NOTIONAL_USD = config.get("min_order_notional_usd", MIN_ORDER_NOTIONAL_USD)
        self.FEED_STALE_SECONDS = config.get("feed_stale_seconds", FEED_STALE_SECONDS)
        self.WS_RECONNECT_COOLDOWN_SEC = config.get("ws_reconnect_cooldown_sec", 15.0)
        self.LOSS_COOLDOWN_SEC = config.get("loss_cooldown_sec", LOSS_COOLDOWN_SEC)
        self.ENTRY_STREAK_WINDOW_SEC = config.get("entry_streak_window_sec", ENTRY_STREAK_WINDOW_SEC)
        self.ENTRY_STREAK_COOLDOWN_SEC = config.get("entry_streak_cooldown_sec", ENTRY_STREAK_COOLDOWN_SEC)
        self.MAX_SAME_SIDE_ENTRY_STREAK = config.get("max_same_side_entry_streak", MAX_SAME_SIDE_ENTRY_STREAK)
        self.market_data_config = config.get("market_data", {})
        
        # MM parameters
        self.NUM_LEVELS = config.get("num_levels", NUM_LEVELS)
        self.LEVEL_TICK_SPACING = config.get("level_tick_spacing", LEVEL_TICK_SPACING)
        self.LEVEL_MULTIPLIERS = config.get("level_multipliers", LEVEL_MULTIPLIERS)
        self.STOP_LOSS_THRESHOLD = STOP_LOSS_USD
        self.STOP_LOSS_ROE_PCT = config.get("stop_loss_roe_pct", STOP_LOSS_ROE_PCT)
        self.LOSS_REDUCE_ROE_PCT = config.get("loss_reduce_roe_pct", LOSS_REDUCE_ROE_PCT)
        self.URGENT_EXIT_ROE_PCT = config.get("urgent_exit_roe_pct", URGENT_EXIT_ROE_PCT)
        self.POSITION_SKEW_FACTOR = config.get("position_skew_factor", POSITION_SKEW_FACTOR)
        self.MIN_SPREAD_TICKS = config.get("min_spread_ticks", MIN_SPREAD_TICKS)
        self.BASE_SPREAD_TICKS = config.get("base_spread_ticks", BASE_SPREAD_TICKS)
        self.MAX_SPREAD_TICKS = config.get("max_spread_ticks", MAX_SPREAD_TICKS)
        self.MOMENTUM_THRESHOLD = config.get("momentum_threshold", MOMENTUM_THRESHOLD)
        
        # Dynamic spread state
        self.current_spread_ticks = self.BASE_SPREAD_TICKS
        self.current_fee_floor_ticks = 0.0
        
        # Stats tracking
        self.hourly_volume_history = []
        self.target_leverage = config.get("target_leverage", TARGET_LEVERAGE)
        
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
        self.ws_info = Info(BASE_URL, skip_ws=False, spot_meta=getattr(self, "fixed_spot_meta", None), perp_dexs=[PERP_DEX])
        
        # Subscribe to L2 Book for BBO (utiliser SYMBOL complet avec préfixe)
        self.ws_info.subscribe({"type": "l2Book", "coin": SYMBOL}, self._handle_l2_update)
        # Subscribe to User events for fills/position
        self.ws_info.subscribe({"type": "userEvents", "user": ACCOUNT_ADDRESS}, self._handle_user_update)
        
        self.ws_ready = True

    def _handle_l2_update(self, data):
        """Callback for L2 Book updates with volatility tracking"""
        try:
            # Handle both 'data' wrapper and direct payload
            payload = data.get("data", data)
            levels = payload.get("levels", [])
            if len(levels) >= 2:
                # Levels structure: [[{'px': 'px', 'sz': 'sz'}, ...], [...]]
                best_bid = float(levels[0][0]["px"])
                best_ask = float(levels[1][0]["px"])
                best_bid_sz = float(levels[0][0].get("sz", 1))
                best_ask_sz = float(levels[1][0].get("sz", 1))
                mid = (best_bid + best_ask) / 2
                self.latest_bbo = (best_bid, best_ask, mid)
                if not self.ws_ready: self.ws_ready = True
                self.latest_levels = levels

                now_ts = time.time()
                if self.last_ws_msg_ts > 0:
                    self.feed_gap_ms_history.append((now_ts - self.last_ws_msg_ts) * 1000.0)
                self.last_ws_msg_ts = now_ts
                self.indicators.on_tick(best_bid, best_ask, best_bid_sz, best_ask_sz, now_ts)
                self.last_indicator_snapshot = self.indicators.snapshot()
                self._update_microstructure(levels, mid, now_ts)
                
                # === VOLATILITY TRACKING ===
                self.price_history.append((time.time(), mid))
                if len(self.price_history) >= 5:
                    self._update_volatility()
                
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

    def _compute_recent_momentum(self, window: int = 3) -> float:
        """Estimate short-term price momentum from the last few mid-price samples."""
        if len(self.price_history) < window:
            return 0.0
        recent_prices = [p[1] for p in list(self.price_history)[-window:]]
        if recent_prices[0] <= 0:
            return 0.0
        return (recent_prices[-1] - recent_prices[0]) / recent_prices[0]

    def _evaluate_breakout_guard(self, now_ts: float, mid_price: float) -> None:
        """Pull inputs, run BreakoutGuard.evaluate, react on trigger (cancel exposed side)."""
        guard = self.breakout_guard
        if guard is None or not guard.enabled() or mid_price <= 0:
            return
        try:
            window_bars = int(guard.cfg.get("atr_window_bars", 20))
            closed_bars = self.indicators.get_bars(
                timeframe_sec=1,
                max_bars=max(window_bars + 5, 30),
                include_partial=False,
            )
            current_bar = self.indicators.get_developing_bar()
            recent_ticks = self.indicators.get_recent_ticks(
                max_seconds=float(guard.cfg.get("velocity_window_sec", 2.0)) + 0.5,
                now_ts=now_ts,
            )
            trigger = guard.evaluate(now_ts, mid_price, current_bar, recent_ticks, closed_bars)
            if trigger is None:
                return
            exposed_side, reason = trigger
            # Cancel exposed side once per trigger to avoid spamming the API.
            if now_ts - self.last_breakout_cancel_ts >= float(guard.cfg.get("anti_spam_sec", 1.5)):
                self._cancel_side(exposed_side)
                self.last_breakout_cancel_ts = now_ts
            self.last_micro_guard_reason = f"breakout {exposed_side} ({reason})"
        except Exception:
            return
    
    def _p95(self, values: List[float]) -> float:
        if not values:
            return 0.0
        data = sorted(values)
        idx = min(len(data) - 1, int(0.95 * (len(data) - 1)))
        return data[idx]

    def _avg(self, values) -> float:
        if not values:
            return 0.0
        return sum(values) / len(values)

    def _micro_cfg(self, key: str, default: Any) -> Any:
        return self.microstructure_config.get(key, default)

    def _depth_imbalance_from_levels(self, levels, depth_levels: int) -> Tuple[float, float]:
        if not levels or len(levels) < 2:
            return (0.0, 0.0)
        bids = levels[0][:depth_levels]
        asks = levels[1][:depth_levels]
        bid_usd = sum(float(x.get("px", 0)) * float(x.get("sz", 0)) for x in bids)
        ask_usd = sum(float(x.get("px", 0)) * float(x.get("sz", 0)) for x in asks)
        den = bid_usd + ask_usd
        if den <= 0:
            return (0.0, 0.0)
        return ((bid_usd - ask_usd) / den, min(bid_usd, ask_usd))

    def _update_markouts(self, mid_price: float, now_ts: float):
        if mid_price <= 0 or not self.pending_markouts:
            return
        still_pending = deque(maxlen=self.pending_markouts.maxlen)
        for item in list(self.pending_markouts):
            elapsed = now_ts - item["ts"]
            remaining = []
            side_key = "buy" if item["side_sign"] > 0 else "sell"
            for horizon in item["remaining"]:
                if elapsed >= horizon:
                    good_bps = item["side_sign"] * (mid_price - item["price"]) / item["price"] * 10000.0
                    side_buckets = self.markout_good_bps.setdefault(side_key, {})
                    bucket = side_buckets.setdefault(float(horizon), deque(maxlen=300))
                    bucket.append(good_bps)
                else:
                    remaining.append(horizon)
            if remaining:
                item["remaining"] = remaining
                still_pending.append(item)
        self.pending_markouts = still_pending

    def _register_markout_fill(self, fill_price: float, side_sign: int, fill_size: float):
        if side_sign == 0 or fill_price <= 0 or fill_size <= 0:
            return
        horizons = [float(x) for x in self._micro_cfg("markout_horizons_sec", [1.0, 5.0, 15.0, 30.0])]
        self.pending_markouts.append({
            "ts": time.time(),
            "price": fill_price,
            "size": fill_size,
            "side_sign": side_sign,
            "remaining": horizons,
        })

    def _adverse_markout_bps(self, horizon: float, side: str = "any") -> float:
        """Average adverse markout in bps. Positive = bad for the MM.

        `side`:
          - "buy"  → only buy fills' markouts
          - "sell" → only sell fills' markouts
          - "any"  → both combined (legacy behavior)
        """
        h = float(horizon)
        if side in ("buy", "sell"):
            data = self.markout_good_bps.get(side, {}).get(h)
            if not data:
                return 0.0
            avg_good = self._avg(data)
            return max(0.0, -avg_good)
        # Aggregated: weighted average across both sides by sample count.
        buy_data = self.markout_good_bps.get("buy", {}).get(h)
        sell_data = self.markout_good_bps.get("sell", {}).get(h)
        n_buy = len(buy_data) if buy_data else 0
        n_sell = len(sell_data) if sell_data else 0
        if n_buy + n_sell == 0:
            return 0.0
        sum_good = (self._avg(buy_data) * n_buy if buy_data else 0.0) + (self._avg(sell_data) * n_sell if sell_data else 0.0)
        avg_good = sum_good / (n_buy + n_sell)
        return max(0.0, -avg_good)

    def _adverse_blended_bps(self, side: str = "any") -> float:
        """Weighted blend of adverse markouts across horizons, favoring longer ones."""
        weights = {1.0: 0.10, 5.0: 0.25, 15.0: 0.35, 30.0: 0.30}
        total = 0.0
        sum_w = 0.0
        for h, w in weights.items():
            v = self._adverse_markout_bps(h, side=side)
            if v > 0.0:
                total += v * w
                sum_w += w
        return total / sum_w if sum_w > 0 else 0.0

    def _markout_samples(self, side: str = "any", horizon: float = 5.0) -> int:
        h = float(horizon)
        if side in ("buy", "sell"):
            return len(self.markout_good_bps.get(side, {}).get(h, []))
        return (
            len(self.markout_good_bps.get("buy", {}).get(h, []))
            + len(self.markout_good_bps.get("sell", {}).get(h, []))
        )

    def _update_microstructure(self, levels, mid_price: float, now_ts: float):
        if not self.microstructure_config.get("enabled", False) or not levels or len(levels) < 2:
            self.micro_snapshot = MicrostructureSnapshot()
            return
        try:
            bid = float(levels[0][0]["px"])
            ask = float(levels[1][0]["px"])
            bid_sz = float(levels[0][0].get("sz", 0))
            ask_sz = float(levels[1][0].get("sz", 0))
            den_sz = bid_sz + ask_sz
            if bid <= 0 or ask <= 0 or den_sz <= 0 or mid_price <= 0:
                return

            microprice = (ask * bid_sz + bid * ask_sz) / den_sz
            weighted_mid = (bid * bid_sz + ask * ask_sz) / den_sz
            microprice_bps = (microprice - mid_price) / mid_price * 10000.0
            spread_ticks = (ask - bid) / max(TICK_SIZE, 1e-9)
            imb1, depth1 = self._depth_imbalance_from_levels(levels, 1)
            imb3, depth3 = self._depth_imbalance_from_levels(levels, 3)
            imb5, depth5 = self._depth_imbalance_from_levels(levels, int(self._micro_cfg("levels", 5)))

            ofi_event = 0.0
            prev = self.prev_top_book
            if prev:
                prev_bid, prev_bid_sz, prev_ask, prev_ask_sz = prev
                ofi_units = 0.0
                ofi_units += bid_sz if bid >= prev_bid else 0.0
                ofi_units -= prev_bid_sz if bid <= prev_bid else 0.0
                ofi_units -= ask_sz if ask <= prev_ask else 0.0
                ofi_units += prev_ask_sz if ask >= prev_ask else 0.0
                ofi_event = ofi_units * mid_price
                self.ofi_events.append((now_ts, ofi_event))
            self.prev_top_book = (bid, bid_sz, ask, ask_sz)

            window = float(self._micro_cfg("ofi_window_sec", 12.0))
            while self.ofi_events and (now_ts - self.ofi_events[0][0]) > window:
                self.ofi_events.popleft()
            depth_ref = max(depth5, depth3, depth1, 1.0)
            ofi_norm = max(-1.0, min(1.0, sum(x[1] for x in self.ofi_events) / depth_ref))
            ofi_bps = ofi_norm * float(self._micro_cfg("max_pressure_bps", 2.5))

            pressure_bps = (
                microprice_bps * float(self._micro_cfg("microprice_weight", 0.8)) +
                ofi_bps * float(self._micro_cfg("ofi_weight", 0.45)) +
                imb5 * float(self._micro_cfg("imbalance_weight", 0.35))
            )
            max_pressure = float(self._micro_cfg("max_pressure_bps", 2.5))
            pressure_bps = max(-max_pressure, min(max_pressure, pressure_bps))
            self.last_micro_pressure_bps = pressure_bps
            self._update_markouts(mid_price, now_ts)

            adverse_1s = self._adverse_markout_bps(1.0)
            adverse_5s = self._adverse_markout_bps(5.0)
            adverse_15s = self._adverse_markout_bps(15.0)
            markout_samples = self._markout_samples(side="any", horizon=5.0)
            toxic = adverse_5s >= float(self._micro_cfg("toxic_markout_bps", 0.75))
            momentum = self._compute_recent_momentum(window=6)
            signal_conflict = (
                (pressure_bps > 0 and momentum < -float(self._micro_cfg("probation_min_pressure_bps", 0.35)) / 10000.0) or
                (pressure_bps < 0 and momentum > float(self._micro_cfg("probation_min_pressure_bps", 0.35)) / 10000.0)
            )
            if signal_conflict:
                self.micro_conflict_pause_until = max(
                    self.micro_conflict_pause_until,
                    now_ts + float(self._micro_cfg("conflict_pause_sec", 8.0)),
                )
                self.last_micro_guard_reason = "micro/momentum conflict"

            self._evaluate_breakout_guard(now_ts, mid_price)
            self._log_ev_diagnostics(now_ts, mid_price)

            self.micro_snapshot = MicrostructureSnapshot(
                ts=now_ts,
                ready=True,
                microprice=microprice,
                microprice_bps=microprice_bps,
                weighted_mid=weighted_mid,
                spread_ticks=spread_ticks,
                imbalance_l1=imb1,
                imbalance_l3=imb3,
                imbalance_l5=imb5,
                ofi_norm=ofi_norm,
                ofi_bps=ofi_bps,
                pressure_bps=pressure_bps,
                depth_usd=depth5,
                adverse_markout_1s_bps=adverse_1s,
                adverse_markout_5s_bps=adverse_5s,
                adverse_markout_15s_bps=adverse_15s,
                markout_samples=markout_samples,
                signal_conflict=signal_conflict,
                toxic=toxic,
            )
        except Exception:
            return

    def _microstructure_edge(self, mid_price: float, half_spread: Optional[float] = None) -> Tuple[float, float]:
        snap = self.micro_snapshot
        if not self.microstructure_config.get("enabled", False) or not snap.ready or mid_price <= 0:
            return (999.0, 999.0)
        half = half_spread if half_spread is not None else self._min_fee_aware_half_spread(mid_price)
        half_bps = half / mid_price * 10000.0
        weight = float(self._micro_cfg("adverse_markout_weight", 0.9))
        # Per-side blended adverse: captures the toxic-flow signal across all horizons (1/5/15/30s),
        # so we don't underestimate adverse cost like the legacy 5s-only path did.
        adverse_buy = self._adverse_blended_bps(side="buy") * weight
        adverse_sell = self._adverse_blended_bps(side="sell") * weight
        fee = self._maker_fee_bps()
        edge_buy = half_bps - fee - adverse_buy + snap.pressure_bps
        edge_sell = half_bps - fee - adverse_sell - snap.pressure_bps
        self.micro_snapshot = MicrostructureSnapshot(
            **{**snap.__dict__, "edge_buy_bps": edge_buy, "edge_sell_bps": edge_sell}
        )
        return (edge_buy, edge_sell)

    def _micro_probation_active(self) -> bool:
        if not self.microstructure_config.get("enabled", False):
            return False
        min_samples = int(self._micro_cfg("min_markout_samples", 20))
        return self.micro_snapshot.markout_samples < min_samples

    def _expected_value_bps(self, side: str, mid_price: float, half_bps: Optional[float] = None) -> float:
        """Net EV of a quote in bps. Positive = profitable in expectation.

        EV = half_bps_captured - maker_fee - blended_adverse + pressure_signed
        """
        if mid_price <= 0:
            return 0.0
        if half_bps is None:
            half_px = self._min_fee_aware_half_spread(mid_price)
            half_bps = half_px / mid_price * 10000.0
        fee = self._maker_fee_bps()
        adverse = self._adverse_blended_bps(side=side)
        pressure = self.micro_snapshot.pressure_bps if self.micro_snapshot.ready else 0.0
        pressure_signed = pressure if side == "buy" else -pressure
        return half_bps - fee - adverse + pressure_signed

    def _log_ev_diagnostics(self, now_ts: float, mid_price: float) -> None:
        """Log a snapshot of EV per side + reject counters every 30s. For tuning min_ev_bps."""
        if now_ts - self.last_ev_log_ts < 30.0 or mid_price <= 0:
            return
        try:
            half_px = self._min_fee_aware_half_spread(mid_price)
            half_bps = half_px / mid_price * 10000.0
            fee = self._maker_fee_bps()
            adv_buy = self._adverse_blended_bps(side="buy")
            adv_sell = self._adverse_blended_bps(side="sell")
            pres = self.micro_snapshot.pressure_bps if self.micro_snapshot.ready else 0.0
            ev_buy = half_bps - fee - adv_buy + pres
            ev_sell = half_bps - fee - adv_sell - pres
            min_ev = float(self._micro_cfg("min_ev_bps", 0.20))
            print(
                f"EV-DIAG | buy: ev={ev_buy:+.2f} (half={half_bps:.2f} fee={fee:.2f} adv_b={adv_buy:.2f} pres={pres:+.2f}) "
                f"{'PASS' if ev_buy >= min_ev else 'BLOCK'} | "
                f"sell: ev={ev_sell:+.2f} (adv_s={adv_sell:.2f}) "
                f"{'PASS' if ev_sell >= min_ev else 'BLOCK'} | "
                f"rejects 30s: {self.ev_reject_count_buy}b/{self.ev_reject_count_sell}s | min_ev={min_ev:.2f}",
                flush=True,
            )
        except Exception:
            pass
        self.last_ev_log_ts = now_ts
        self.ev_reject_count_buy = 0
        self.ev_reject_count_sell = 0

    def _micro_allows_side(self, side: str) -> bool:
        if not self.microstructure_config.get("enabled", False) or not self.micro_snapshot.ready:
            return True
        now_ts = time.time()

        pressure = self.micro_snapshot.pressure_bps
        if now_ts < self.micro_conflict_pause_until:
            neutral_pressure = float(self._micro_cfg("probation_neutral_pressure_bps", 0.20))
            if side == "buy" and pressure <= -neutral_pressure:
                self.last_micro_guard_reason = "conflict allows buy pressure"
            elif side == "sell" and pressure >= neutral_pressure:
                self.last_micro_guard_reason = "conflict allows sell pressure"
            else:
                self.last_micro_guard_reason = "micro conflict pause"
                return False

        # EV gate: enforce real expected-value floor INCLUDING during probation.
        # This is the main fix: previously the probation branch skipped edge filtering,
        # letting the first ~30 fills through unchecked - exactly the most toxic ones.
        mid_for_ev = 0.0
        if self.latest_bbo and len(self.latest_bbo) >= 3:
            mid_for_ev = float(self.latest_bbo[2])
        if mid_for_ev > 0:
            ev = self._expected_value_bps(side, mid_for_ev)
            min_ev = float(self._micro_cfg("min_ev_bps", 0.20))
            if ev < min_ev:
                self.last_micro_guard_reason = f"{side} EV {ev:.2f}<{min_ev:.2f}"
                if side == "buy":
                    self.ev_reject_count_buy += 1
                else:
                    self.ev_reject_count_sell += 1
                return False

        edge = self.micro_snapshot.edge_buy_bps if side == "buy" else self.micro_snapshot.edge_sell_bps
        if self._micro_probation_active():
            need_pressure = float(self._micro_cfg("probation_min_pressure_bps", 0.35))
            neutral_pressure = float(self._micro_cfg("probation_neutral_pressure_bps", 0.20))
            if (
                self._micro_cfg("probation_allow_neutral", True) and
                abs(pressure) <= neutral_pressure and
                not self.micro_snapshot.toxic and
                not self.micro_snapshot.signal_conflict
            ):
                self.last_micro_guard_reason = "probation neutral allowed"
                return True
            if side == "buy" and pressure < need_pressure:
                self.last_micro_guard_reason = "probation blocks buy"
                return False
            if side == "sell" and pressure > -need_pressure:
                self.last_micro_guard_reason = "probation blocks sell"
                return False
            self.last_micro_guard_reason = f"probation allows {side}"
            return True

        min_edge = float(self._micro_cfg("min_edge_bps", 0.10))
        if edge < min_edge:
            self.last_micro_guard_reason = f"{side} edge {edge:.2f}<min"
            return False

        return True

    def _choose_probe_side(self) -> str:
        pressure = self.micro_snapshot.pressure_bps if self.micro_snapshot.ready else 0.0
        regime = self._market_data_regime(self.latest_bbo[2] if self.latest_bbo else 0.0)
        if regime.get("trend_down") or pressure < -float(self._micro_cfg("probation_neutral_pressure_bps", 0.20)):
            return "buy"
        if regime.get("trend_up") or pressure > float(self._micro_cfg("probation_neutral_pressure_bps", 0.20)):
            return "sell"
        return "buy" if self._fill_rate_per_min() == 0 else "sell"

    def _build_probe_quote(
        self,
        best_bid: float,
        best_ask: float,
        mid_price: float,
        base_size: float,
    ) -> Tuple[List[Tuple[float, float]], List[Tuple[float, float]], List[bool], List[bool], List[str], List[str]]:
        if not self._micro_cfg("allow_probe_quote", True) or mid_price <= 0 or base_size <= 0:
            return [], [], [], [], [], []
        side = self._choose_probe_side()
        extra_ticks = int(self._micro_cfg("probe_extra_ticks", 4))
        size_factor = float(self._micro_cfg("probe_size_factor", 0.35))
        probe_size = round(max(MIN_SIZE, base_size * size_factor), SIZE_DECIMALS)
        if probe_size < MIN_SIZE:
            return [], [], [], [], [], []

        if side == "buy":
            px = self._round_price_down(max(TICK_SIZE, best_bid - extra_ticks * TICK_SIZE))
            return [(px, probe_size)], [], [False], [], ["probe"], []

        px = self._round_price_up(best_ask + extra_ticks * TICK_SIZE)
        return [], [(px, probe_size)], [], [False], [], ["probe"]

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

    def _cancel_rate_per_min(self) -> float:
        now_ts = time.time()
        while self.cancel_events and (now_ts - self.cancel_events[0]) > 60.0:
            self.cancel_events.popleft()
        return float(len(self.cancel_events))

    def _fill_probability_estimate(self, spread_ticks: float) -> float:
        fills_pm = self._fill_rate_per_min()
        spread_penalty = max(0.0, spread_ticks - 1.0) * 0.08
        raw = 0.45 + 0.06 * fills_pm - spread_penalty
        return min(0.95, max(0.05, raw))

    def _book_depth_usd(self, levels_count: int = 5) -> Tuple[float, float]:
        if not self.latest_levels or len(self.latest_levels) < 2:
            return (0.0, 0.0)
        try:
            bid_depth = sum(float(x.get("px", 0)) * float(x.get("sz", 0)) for x in self.latest_levels[0][:levels_count])
            ask_depth = sum(float(x.get("px", 0)) * float(x.get("sz", 0)) for x in self.latest_levels[1][:levels_count])
            return (bid_depth, ask_depth)
        except Exception:
            return (0.0, 0.0)

    def _market_data_regime(self, mid_price: float, snap: Optional[IndicatorSnapshot] = None) -> Dict[str, Any]:
        cfg = getattr(self, "market_data_config", {})
        if not cfg.get("enabled", False) or mid_price <= 0:
            return {
                "enabled": False,
                "calm_liquid": False,
                "thin_book": False,
                "trend_up": False,
                "trend_down": False,
                "trend_strength": 0.0,
                "depth_usd": 0.0,
                "spread_ticks": 0.0,
            }

        snap = snap or self._indicator_snapshot()
        levels_count = int(cfg.get("depth_levels", 5))
        bid_depth, ask_depth = self._book_depth_usd(levels_count)
        min_depth = min(bid_depth, ask_depth) if bid_depth > 0 and ask_depth > 0 else 0.0
        spread_ticks = 0.0
        if self.latest_bbo:
            spread_ticks = (self.latest_bbo[1] - self.latest_bbo[0]) / max(TICK_SIZE, 1e-9)

        momentum = self._compute_recent_momentum(window=6)
        ema_bias = snap.ema_diff_bps / 10000.0 if snap and snap.ready else 0.0
        rsi = snap.rsi if snap and snap.rsi is not None else 50.0
        trend_threshold = float(cfg.get("trend_guard_momentum", 0.00045))
        trend_rsi = float(cfg.get("trend_guard_rsi", 64.0))
        trend_up = momentum >= trend_threshold or (ema_bias > trend_threshold * 0.35 and rsi >= trend_rsi)
        trend_down = momentum <= -trend_threshold or (ema_bias < -trend_threshold * 0.35 and rsi <= (100.0 - trend_rsi))
        trend_strength = min(1.0, max(abs(momentum), abs(ema_bias)) / max(trend_threshold, 1e-9))

        strong_depth = min_depth >= float(cfg.get("strong_depth_usd", 750000.0))
        thin_book = 0 < min_depth <= float(cfg.get("thin_depth_usd", 180000.0))
        tight_spread = spread_ticks <= float(cfg.get("tight_spread_ticks", 2.0))
        calm = abs(momentum) <= float(cfg.get("quiet_momentum", 0.00025)) and not trend_up and not trend_down

        return {
            "enabled": True,
            "calm_liquid": strong_depth and tight_spread and calm,
            "thin_book": thin_book,
            "trend_up": trend_up,
            "trend_down": trend_down,
            "trend_strength": trend_strength,
            "depth_usd": min_depth,
            "spread_ticks": spread_ticks,
            "momentum": momentum,
        }

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

    def _mm_cfg(self, key: str, default: Any) -> Any:
        return self.professional_mm_config.get(key, default)

    def _sigma_bps(self) -> float:
        sigma_bps = max(0.0, self.volatility * 100.0)
        return max(
            float(self._mm_cfg("min_sigma_bps", 0.35)),
            min(float(self._mm_cfg("max_sigma_bps", 12.0)), sigma_bps),
        )

    def _session_cost_per_million(self) -> float:
        if self.total_volume <= 0:
            return 0.0
        return (self.total_fees - self.session_pnl) / (self.total_volume / 1_000_000)

    def _compute_inventory_skew(self, mid_price: float, position: float) -> float:
        if mid_price <= 0:
            return 0.0
        if self.professional_mm_config.get("enabled", False):
            max_inv = max(MAX_POSITION_USD, 1.0)
            q_norm = max(-1.50, min(1.50, (position * mid_price) / max_inv))
            gamma = max(0.001, float(self._mm_cfg("risk_aversion", 0.14)))
            horizon = max(1.0, float(self._mm_cfg("horizon_sec", 8.0)))
            sigma = self._sigma_bps() / 10000.0
            as_component = gamma * q_norm * (sigma ** 2) * horizon * mid_price
            linear_component = q_norm * float(self._mm_cfg("inventory_linear_bps", 1.10)) * mid_price / 10000.0
            skew = as_component + linear_component
            max_skew = max(1.0, float(self._mm_cfg("max_inventory_skew_ticks", 7.0))) * TICK_SIZE
            skew = max(-max_skew, min(max_skew, skew))
            self.mm_model_state.update({
                "q_norm": q_norm,
                "inventory_skew_ticks": skew / max(TICK_SIZE, 1e-9),
                "sigma_bps": self._sigma_bps(),
            })
            return skew

        max_pos_units = max(MAX_POSITION_USD / mid_price, MIN_SIZE)
        q = max(-1.5, min(1.5, position / max(max_pos_units, 1e-9)))
        sigma = max(0.00005, self.volatility / 100.0)
        horizon = 2.0
        skew_abs = (self.score_gamma * (sigma ** 2) * horizon + self.POSITION_SKEW_FACTOR * 0.0008) * abs(q) * mid_price
        skew_abs = min(skew_abs, 4.0 * TICK_SIZE)
        return skew_abs if position > 0 else -skew_abs

    def _compute_dynamic_half_spread(self, mid_price: float, best_bid: float, best_ask: float) -> float:
        tick = TICK_SIZE
        base_ticks = float(self.BASE_SPREAD_TICKS)
        sigma = max(0.00005, self.volatility / 100.0)
        imbalance = self._orderbook_imbalance()
        latency_ms = self._avg_feed_gap_ms()
        toxic = self._toxic_score()
        snap = self._indicator_snapshot()

        if self.professional_mm_config.get("enabled", False):
            gamma = max(0.001, float(self._mm_cfg("risk_aversion", 0.14)))
            k = max(0.05, float(self._mm_cfg("intensity_k", 1.25)))
            horizon = max(1.0, float(self._mm_cfg("horizon_sec", 8.0)))
            sigma_bps = self._sigma_bps()
            sigma_px = mid_price * sigma_bps / 10000.0
            risk_ticks = min(4.0, gamma * (sigma_px / max(tick, 1e-9)) * math.sqrt(horizon))
            intensity_ticks = min(3.0, math.log1p(gamma / k) / gamma)
            inventory_ratio = abs(self.latest_position * mid_price) / max(MAX_POSITION_USD, 1e-9)
            inventory_ticks = min(3.0, inventory_ratio * float(self._mm_cfg("inventory_spread_bps", 0.45)) * mid_price / 10000.0 / max(tick, 1e-9))
            adverse_ticks = float(self._mm_cfg("adverse_selection_bps", 0.35)) * mid_price / 10000.0 / max(tick, 1e-9)
            if self.micro_snapshot.ready:
                adverse_ticks += min(2.5, max(0.0, self.micro_snapshot.adverse_markout_5s_bps) * 0.80)
                adverse_ticks += min(1.5, abs(self.micro_snapshot.pressure_bps) * 0.25)
            latency_term = min(1.5, latency_ms / LATENCY_WIDENING_MS_REF)
            imbalance_term = abs(imbalance) * 1.0
            target_fill_rate = float(self._mm_cfg("target_fill_rate_per_min", 1.0))
            fill_shortfall = max(0.0, target_fill_rate - self._fill_rate_per_min())
            fill_term = -min(float(self._mm_cfg("fill_shortfall_tighten_ticks", 1.0)), fill_shortfall * 0.50)
            cost_pm = self._session_cost_per_million()
            cost_term = (
                float(self._mm_cfg("cost_widen_ticks", 1.5))
                if self.total_volume >= MIN_VOLUME_FOR_KILL_SWITCH_USD and
                cost_pm > float(self._mm_cfg("cost_widen_threshold_per_million", 45.0))
                else 0.0
            )
            rough_ticks = (
                base_ticks +
                risk_ticks +
                intensity_ticks +
                inventory_ticks +
                adverse_ticks +
                latency_term +
                imbalance_term +
                toxic +
                cost_term +
                fill_term
            )
            self.mm_model_state.update({
                "gamma": gamma,
                "k": k,
                "risk_ticks": risk_ticks,
                "intensity_ticks": intensity_ticks,
                "inventory_ticks": inventory_ticks,
                "adverse_ticks": adverse_ticks,
                "cost_pm": cost_pm,
            })
        else:
            vol_term = min(2.5, sigma * 6500.0)
            latency_term = min(1.5, latency_ms / LATENCY_WIDENING_MS_REF)
            imbalance_term = abs(imbalance) * 1.2

            rough_ticks = base_ticks + vol_term + latency_term + imbalance_term + toxic
            fill_prob = self._fill_probability_estimate(rough_ticks)
            fill_term = max(-0.8, (FILL_RATE_TARGET - fill_prob) * 2.0)

        indicator_ticks = 0.0
        if self.indicator_config.enabled and snap.ready:
            weight = max(0.0, self.indicator_config.indicator_spread_weight)
            if snap.bollinger_bandwidth is not None:
                bw = snap.bollinger_bandwidth
                if bw < self.indicator_config.bollinger_squeeze_threshold:
                    indicator_ticks -= abs(self.indicator_config.squeeze_tighten_ticks) * weight
                elif bw > self.indicator_config.bollinger_expansion_threshold:
                    expansion_ratio = bw / max(self.indicator_config.bollinger_expansion_threshold, 1e-9)
                    indicator_ticks += min(3.0, 1.0 + (expansion_ratio - 1.0) * 1.5) * weight
            if snap.ema_fast is not None and snap.ema_slow is not None:
                ema_div_ticks = abs(snap.ema_fast - snap.ema_slow) / max(tick, 1e-9)
                indicator_ticks += min(3.0, ema_div_ticks * 0.35) * weight

        regime = self._market_data_regime(mid_price, snap)
        if regime.get("enabled"):
            md_cfg = self.market_data_config
            if regime.get("calm_liquid"):
                indicator_ticks -= float(md_cfg.get("calm_tighten_ticks", 0.0))
            if regime.get("thin_book"):
                indicator_ticks += float(md_cfg.get("thin_widen_ticks", 0.0))
            if regime.get("trend_up") or regime.get("trend_down"):
                trend_widen = float(md_cfg.get("trend_widen_ticks", 0.0))
                indicator_ticks += trend_widen * max(0.25, float(regime.get("trend_strength", 0.0)))

        micro = self.micro_snapshot
        if self.microstructure_config.get("enabled", False) and micro.ready:
            if micro.toxic:
                indicator_ticks += float(self._micro_cfg("toxic_widen_ticks", 4.0))
            indicator_ticks += min(2.0, abs(micro.pressure_bps) * 0.35)

        # Breakout guard: widen the surviving side while a breakout pause is active.
        # The exposed side gets dropped in the quote-assembly stage, so this widening
        # effectively only protects the opposite side from getting picked off too cheap
        # during the move.
        if self.breakout_guard is not None and self.breakout_guard.enabled():
            now_ts_bg = time.time()
            if now_ts_bg < self.breakout_guard.pause_until:
                indicator_ticks += float(self.breakout_guard.cfg.get("opposite_widen_ticks", 3.0))

        spread_ticks = max(self.MIN_SPREAD_TICKS, min(self.MAX_SPREAD_TICKS + 3, rough_ticks + fill_term + indicator_ticks))
        market_ticks = max(1.0, (best_ask - best_bid) / max(tick, 1e-9))
        spread_ticks = max(spread_ticks, min(market_ticks + 1.0, self.MAX_SPREAD_TICKS + 2))
        dynamic_half = max(tick / 2.0, (spread_ticks * tick) / 2.0)
        fee_floor = self._min_fee_aware_half_spread(mid_price)
        half_spread = max(dynamic_half, fee_floor)
        self.current_fee_floor_ticks = (fee_floor * 2.0) / max(tick, 1e-9)
        self.current_spread_ticks = int(round((half_spread * 2.0) / max(tick, 1e-9)))
        return half_spread

    def _compute_directional_skew(self, mid_price: float, snap: Optional[IndicatorSnapshot] = None) -> float:
        if mid_price <= 0 or not self.indicator_config.enabled:
            return 0.0
        snap = snap or self._indicator_snapshot()
        if not snap.ready or snap.ema_fast is None or snap.ema_slow is None:
            return 0.0

        diff_bps = snap.ema_diff_bps
        if abs(diff_bps) < 0.01:
            return 0.0

        damp = 1.0
        if snap.rsi is not None:
            if diff_bps > 0 and snap.rsi >= self.indicator_config.rsi_overbought:
                room = max(1.0, self.indicator_config.rsi_extreme_overbought - self.indicator_config.rsi_overbought)
                damp = max(0.0, (self.indicator_config.rsi_extreme_overbought - snap.rsi) / room)
            elif diff_bps < 0 and snap.rsi <= self.indicator_config.rsi_oversold:
                room = max(1.0, self.indicator_config.rsi_oversold - self.indicator_config.rsi_extreme_oversold)
                damp = max(0.0, (snap.rsi - self.indicator_config.rsi_extreme_oversold) / room)

        skew_bps = diff_bps * self.indicator_config.indicator_skew_weight * damp
        skew_bps = max(-2.0, min(2.0, skew_bps))
        self.directional_skew_bps_history.append(skew_bps)
        return mid_price * skew_bps / 10000.0

    def _compute_take_profit_quote(
        self,
        best_bid: float,
        best_ask: float,
        mid_price: float,
        position: float,
        base_size: float,
        snap: Optional[IndicatorSnapshot] = None,
    ) -> Optional[Tuple[bool, float, float]]:
        if (
            not self.indicator_config.enabled or
            not self.indicator_config.take_profit_enabled or
            self._is_flat_position(position) or
            mid_price <= 0
        ):
            return None

        snap = snap or self._indicator_snapshot()
        if not snap.ready or snap.rsi is None or snap.bollinger_upper is None or snap.bollinger_lower is None:
            return None

        raw_size = abs(position) * max(0.05, min(1.0, self.indicator_config.tp_size_pct))
        if raw_size < MIN_SIZE and abs(position) >= MIN_SIZE:
            raw_size = abs(position)
        tp_size = round(min(abs(position), max(raw_size, MIN_SIZE)), SIZE_DECIMALS)
        if tp_size < MIN_SIZE:
            return None

        needed_bps = (
            self._round_trip_maker_fee_bps() +
            self.indicator_config.adverse_buffer_bps +
            self.indicator_config.tp_extra_edge_bps
        )
        offset_ticks = max(
            int(self.indicator_config.tp_base_offset_ticks),
            int(math.ceil((mid_price * needed_bps / 10000.0) / max(TICK_SIZE, 1e-9))),
        )

        if position > 0:
            signal = snap.rsi >= self.indicator_config.tp_rsi_overbought and mid_price > snap.bollinger_upper
            if not signal:
                return None
            px = max(best_ask, mid_price + offset_ticks * TICK_SIZE)
            if self.entry_price and self.entry_price > 0:
                px = max(px, self.entry_price * (1.0 + needed_bps / 10000.0))
            return (False, self._round_price_up(px), tp_size)

        signal = snap.rsi <= self.indicator_config.tp_rsi_oversold and mid_price < snap.bollinger_lower
        if not signal:
            return None
        px = min(best_bid, mid_price - offset_ticks * TICK_SIZE)
        if self.entry_price and self.entry_price > 0:
            px = min(px, self.entry_price * (1.0 - needed_bps / 10000.0))
        px = max(TICK_SIZE, px)
        return (True, self._round_price_down(px), tp_size)

    def _compute_passive_reduce_quote(
        self,
        best_bid: float,
        best_ask: float,
        mid_price: float,
        position: float,
        urgent: bool = False,
    ) -> Optional[Tuple[bool, float, float]]:
        """Always keep a passive reduce-only escape quote when inventory exists."""
        if self._is_flat_position(position) or mid_price <= 0:
            return None

        needed_bps = self._round_trip_maker_fee_bps() + self.indicator_config.adverse_buffer_bps
        offset_ticks = 0 if urgent else max(
            1,
            int(math.ceil((mid_price * needed_bps / 10000.0) / max(TICK_SIZE, 1e-9))),
        )
        reduce_pct = 1.0 if urgent else 0.50
        reduce_size = round(max(MIN_SIZE, min(abs(position), abs(position) * reduce_pct)), SIZE_DECIMALS)
        if reduce_size < MIN_SIZE:
            return None

        if position > 0:
            px = best_ask if urgent else max(best_ask, mid_price + offset_ticks * TICK_SIZE)
            return (False, self._round_price_up(px), reduce_size)

        px = best_bid if urgent else min(best_bid, mid_price - offset_ticks * TICK_SIZE)
        return (True, self._round_price_down(max(TICK_SIZE, px)), reduce_size)

    def _should_ema_cross_exit(self, position: float, snap: IndicatorSnapshot) -> Tuple[bool, str]:
        if (
            not self.indicator_config.enabled or
            not self.indicator_config.ema_cross_exit_enabled or
            self._is_flat_position(position) or
            not snap.ready or
            snap.last_bar_ts <= 0 or
            snap.last_bar_ts == self.last_ema_exit_bar_ts or
            snap.rsi is None
        ):
            return (False, "")

        if position > 0 and snap.ema_cross_down and snap.rsi < 50.0:
            return (True, "SELL")
        if position < 0 and snap.ema_cross_up and snap.rsi > 50.0:
            return (True, "BUY")
        return (False, "")

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
            return self.BASE_SPREAD_TICKS
        if not self.latest_bbo:
            return self.BASE_SPREAD_TICKS
        best_bid, best_ask, mid = self.latest_bbo
        half_spread = self._compute_dynamic_half_spread(mid, best_bid, best_ask)
        return max(self.MIN_SPREAD_TICKS, int(round((half_spread * 2.0) / max(TICK_SIZE, 1e-9))))

    def _indicator_snapshot(self) -> IndicatorSnapshot:
        self.last_indicator_snapshot = self.indicators.snapshot()
        return self.last_indicator_snapshot

    def _mr_cfg(self, key: str, default: Any) -> Any:
        return self.mean_reversion_config.get(key, default)

    def _ema_series(self, values: List[float], period: int) -> List[float]:
        if not values:
            return []
        alpha = 2.0 / (max(1, int(period)) + 1.0)
        out = [float(values[0])]
        for value in values[1:]:
            out.append(alpha * float(value) + (1.0 - alpha) * out[-1])
        return out

    def _rsi_from_closes(self, closes: List[float], period: int) -> Optional[float]:
        period = max(1, int(period))
        if len(closes) <= period:
            return None
        gains = []
        losses = []
        for i in range(1, period + 1):
            change = closes[i] - closes[i - 1]
            gains.append(max(change, 0.0))
            losses.append(max(-change, 0.0))
        avg_gain = sum(gains) / period
        avg_loss = sum(losses) / period
        for i in range(period + 1, len(closes)):
            change = closes[i] - closes[i - 1]
            gain = max(change, 0.0)
            loss = max(-change, 0.0)
            avg_gain = ((period - 1.0) * avg_gain + gain) / period
            avg_loss = ((period - 1.0) * avg_loss + loss) / period
        if avg_gain == 0 and avg_loss == 0:
            return 50.0
        if avg_loss == 0:
            return 100.0
        if avg_gain == 0:
            return 0.0
        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1.0 + rs))

    def _bb_at(self, closes: List[float], end_idx: int, length: int, mult: float) -> Optional[Tuple[float, float, float]]:
        length = max(2, int(length))
        if end_idx < 0:
            end_idx = len(closes) + end_idx
        start = end_idx - length + 1
        if start < 0 or end_idx >= len(closes):
            return None
        window = closes[start:end_idx + 1]
        mean = sum(window) / len(window)
        variance = sum((x - mean) ** 2 for x in window) / len(window)
        std = math.sqrt(max(variance, 0.0))
        return (mean, mean + float(mult) * std, mean - float(mult) * std)

    def _atr_from_bars(self, bars: List[Dict[str, float]], period: int) -> Optional[float]:
        period = max(1, int(period))
        if len(bars) <= period:
            return None
        trs = []
        for i in range(1, len(bars)):
            high = float(bars[i]["h"])
            low = float(bars[i]["l"])
            prev_close = float(bars[i - 1]["c"])
            trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
        if len(trs) < period:
            return None
        return sum(trs[-period:]) / period

    def _adx_from_bars(self, bars: List[Dict[str, float]], period: int) -> Optional[float]:
        period = max(1, int(period))
        if len(bars) < period * 2 + 1:
            return None
        trs: List[float] = []
        plus_dm: List[float] = []
        minus_dm: List[float] = []
        for i in range(1, len(bars)):
            high = float(bars[i]["h"])
            low = float(bars[i]["l"])
            prev_high = float(bars[i - 1]["h"])
            prev_low = float(bars[i - 1]["l"])
            prev_close = float(bars[i - 1]["c"])
            up_move = high - prev_high
            down_move = prev_low - low
            trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
            plus_dm.append(up_move if up_move > down_move and up_move > 0 else 0.0)
            minus_dm.append(down_move if down_move > up_move and down_move > 0 else 0.0)

        dxs: List[float] = []
        for end in range(period, len(trs) + 1):
            tr_sum = sum(trs[end - period:end])
            if tr_sum <= 0:
                continue
            plus_di = 100.0 * sum(plus_dm[end - period:end]) / tr_sum
            minus_di = 100.0 * sum(minus_dm[end - period:end]) / tr_sum
            den = plus_di + minus_di
            dxs.append(0.0 if den <= 0 else 100.0 * abs(plus_di - minus_di) / den)
        if len(dxs) < period:
            return None
        return sum(dxs[-period:]) / period

    def _mean_reversion_state(self, mid_price: float) -> Dict[str, Any]:
        cfg = self.mean_reversion_config
        if not cfg.get("enabled", False):
            return {"enabled": False, "ready": False}

        tf = max(1, int(cfg.get("signal_bar_seconds", 15)))
        bb_len = max(2, int(cfg.get("bb_len", 20)))
        rsi_len = max(1, int(cfg.get("rsi_len", 7)))
        atr_len = max(1, int(cfg.get("atr_len", 14)))
        ema_len = max(2, int(cfg.get("ema_len", 50)))
        slope_lookback = max(1, int(cfg.get("slope_lookback", 5)))
        adx_len = max(1, int(cfg.get("adx_len", 14)))
        needed = max(bb_len + 2, rsi_len + 2, atr_len + 2, ema_len + slope_lookback + 1, adx_len * 2 + 2)
        fast_start = bool(cfg.get("fast_start", False))
        bars = self.indicators.get_bars(
            tf,
            max_bars=max(needed + 25, 120),
            include_partial=fast_start and bool(cfg.get("use_partial_bar", False)),
        )
        bar_index = len(bars) - 1
        state: Dict[str, Any] = {
            "enabled": True,
            "ready": False,
            "timeframe_sec": tf,
            "bar_index": bar_index,
            "reason": "warming up",
        }
        is_fast_start = False
        if mid_price <= 0 or len(bars) < needed:
            startup_min_bars = max(1, int(cfg.get("startup_min_bars", 3)))
            if fast_start and cfg.get("startup_quote_both_sides", True) and len(bars) < startup_min_bars:
                close = float(bars[-1]["c"]) if bars else mid_price
                fallback_atr = max(TICK_SIZE, abs(mid_price - close), TICK_SIZE)
                state.update({
                    "ready": True,
                    "reason": "startup liquidity",
                    "need_bars": startup_min_bars,
                    "have_bars": len(bars),
                    "close": close,
                    "bb_mid": close,
                    "bb_upper": close + fallback_atr,
                    "bb_lower": close - fallback_atr,
                    "rsi": 50.0,
                    "atr": fallback_atr,
                    "adx": 0.0,
                    "ema_slope_pct": 0.0,
                    "raw_long": False,
                    "raw_short": False,
                    "active_long": True,
                    "active_short": True,
                    "long_zone": True,
                    "short_zone": True,
                    "range_ok": True,
                    "long_trend_ok": True,
                    "short_trend_ok": True,
                    "cooldown_ok": True,
                    "allow_buy": True,
                    "allow_sell": True,
                })
                self.last_mr_state = state
                return state
            if fast_start and len(bars) >= startup_min_bars:
                is_fast_start = True
                bb_len = min(bb_len, max(2, min(int(cfg.get("startup_bb_len", 3)), len(bars) - 1)))
                rsi_len = min(rsi_len, max(1, min(int(cfg.get("startup_rsi_len", 2)), len(bars) - 1)))
                atr_len = min(atr_len, max(1, min(int(cfg.get("startup_atr_len", 2)), len(bars) - 1)))
                slope_lookback = min(slope_lookback, max(1, len(bars) - 1))
                state["reason"] = "fast start"
            else:
                state["need_bars"] = needed
                state["have_bars"] = len(bars)
                self.last_mr_state = state
                return state
        else:
            state["reason"] = "ok"

        if len(bars) < 3:
            state["need_bars"] = needed
            state["have_bars"] = len(bars)
            self.last_mr_state = state
            return state

        closes = [float(b["c"]) for b in bars]
        prev_bb = self._bb_at(closes, -2, bb_len, float(cfg.get("bb_mult", 1.5)))
        cur_bb = self._bb_at(closes, -1, bb_len, float(cfg.get("bb_mult", 1.5)))
        rsi = self._rsi_from_closes(closes, rsi_len)
        atr = self._atr_from_bars(bars, atr_len)
        adx = self._adx_from_bars(bars, adx_len)
        ema = self._ema_series(closes, ema_len)
        if atr is not None and fast_start:
            min_atr = max(TICK_SIZE, float(cfg.get("fast_start_min_atr_ticks", 1.0)) * TICK_SIZE)
            atr = max(float(atr), min_atr)

        if adx is None and fast_start and not bool(cfg.get("startup_adx_required", False)):
            adx = 0.0
        if not prev_bb or not cur_bb or rsi is None or atr is None or adx is None:
            state["reason"] = "indicator warmup"
            self.last_mr_state = state
            return state

        _, prev_upper, prev_lower = prev_bb
        cur_mid, cur_upper, cur_lower = cur_bb
        prev_close = closes[-2]
        cur_close = closes[-1]
        if len(ema) <= slope_lookback and fast_start and not bool(cfg.get("startup_ema_required", False)):
            slope_pct = 0.0
        elif len(ema) <= slope_lookback:
            state["reason"] = "ema warmup"
            self.last_mr_state = state
            return state
        else:
            ema_prev = ema[-1 - slope_lookback]
            slope_pct = ((ema[-1] - ema_prev) / ema_prev * 100.0) if ema_prev else 0.0

        rsi_os = float(cfg.get("rsi_oversold", 35.0))
        rsi_ob = float(cfg.get("rsi_overbought", 65.0))
        flat_threshold = float(cfg.get("flat_threshold_pct", 0.12))
        adx_max = float(cfg.get("adx_max", 32.0))
        if is_fast_start and bool(cfg.get("ignore_adx_during_fast_start", False)):
            adx_max = float(cfg.get("fast_start_adx_max", 100.0))
        min_bars = max(1, int(cfg.get("min_bars_between", 1)))
        ttl = max(0, int(cfg.get("signal_ttl_bars", 3)))

        raw_long = prev_close < prev_lower and cur_close > cur_lower and rsi <= rsi_os
        raw_short = prev_close > prev_upper and cur_close < cur_upper and rsi >= rsi_ob
        if raw_long:
            self.mr_last_long_signal_bar = bar_index
        if raw_short:
            self.mr_last_short_signal_bar = bar_index

        active_long = (bar_index - self.mr_last_long_signal_bar) <= ttl
        active_short = (bar_index - self.mr_last_short_signal_bar) <= ttl

        allow_zone = bool(cfg.get("allow_zone_reentry", True))
        zone_atr = max(0.0, float(cfg.get("zone_band_buffer_atr", 0.20))) * atr
        zone_rsi = max(0.0, float(cfg.get("zone_rsi_buffer", 6.0)))
        long_zone = allow_zone and cur_close >= cur_lower and cur_close <= cur_lower + zone_atr and rsi <= rsi_os + zone_rsi
        short_zone = allow_zone and cur_close <= cur_upper and cur_close >= cur_upper - zone_atr and rsi >= rsi_ob - zone_rsi

        range_ok = adx <= adx_max
        long_trend_ok = slope_pct >= -flat_threshold
        short_trend_ok = slope_pct <= flat_threshold
        cooldown_ok = (bar_index - self.mr_last_entry_bar) >= min_bars

        allow_buy = cooldown_ok and range_ok and long_trend_ok and (active_long or long_zone)
        allow_sell = cooldown_ok and range_ok and short_trend_ok and (active_short or short_zone)
        if (
            is_fast_start and
            bool(cfg.get("fast_start_neutral_quote", False)) and
            not allow_buy and
            not allow_sell and
            cooldown_ok and
            range_ok
        ):
            allow_buy = True
            allow_sell = True
            state["reason"] = "fast start neutral"
        state.update({
            "ready": True,
            "fast_start": is_fast_start,
            "last_bar_ts": int(float(bars[-1]["ts"])),
            "close": cur_close,
            "bb_mid": cur_mid,
            "bb_upper": cur_upper,
            "bb_lower": cur_lower,
            "rsi": rsi,
            "atr": atr,
            "adx": adx,
            "ema_slope_pct": slope_pct,
            "raw_long": raw_long,
            "raw_short": raw_short,
            "active_long": active_long,
            "active_short": active_short,
            "long_zone": long_zone,
            "short_zone": short_zone,
            "range_ok": range_ok,
            "long_trend_ok": long_trend_ok,
            "short_trend_ok": short_trend_ok,
            "cooldown_ok": cooldown_ok,
            "allow_buy": allow_buy,
            "allow_sell": allow_sell,
        })
        self.last_mr_state = state
        return state

    def _mean_reversion_exit_state(self, position: float, mid_price: float) -> Dict[str, Any]:
        if not self.mean_reversion_config.get("enabled", False) or self._is_flat_position(position):
            return {"enabled": False}
        entry = self.entry_price if self.entry_price and self.entry_price > 0 else None
        state = self.last_mr_state if self.last_mr_state.get("ready") else self._mean_reversion_state(mid_price)
        atr = self.mr_entry_atr if self.mr_entry_atr and self.mr_entry_atr > 0 else state.get("atr")
        if entry is None or atr is None or atr <= 0:
            return {"enabled": True, "ready": False}

        sl_mult = float(self._mr_cfg("sl_atr_mult", 1.5))
        tp_mult = float(self._mr_cfg("tp_atr_mult", 1.0))
        if position > 0:
            stop_px = entry - sl_mult * atr
            take_px = entry + tp_mult * atr
            return {
                "enabled": True,
                "ready": True,
                "side": "long",
                "stop_px": stop_px,
                "take_px": take_px,
                "stop_hit": mid_price <= stop_px,
                "take_hit": mid_price >= take_px,
            }

        stop_px = entry + sl_mult * atr
        take_px = entry - tp_mult * atr
        return {
            "enabled": True,
            "ready": True,
            "side": "short",
            "stop_px": stop_px,
            "take_px": take_px,
            "stop_hit": mid_price >= stop_px,
            "take_hit": mid_price <= take_px,
        }

    def _compute_mean_reversion_take_profit_quote(
        self,
        best_bid: float,
        best_ask: float,
        mid_price: float,
        position: float,
    ) -> Optional[Tuple[bool, float, float]]:
        exit_state = self._mean_reversion_exit_state(position, mid_price)
        if not exit_state.get("ready"):
            return None
        size = round(max(MIN_SIZE, abs(position)), SIZE_DECIMALS)
        if size < MIN_SIZE:
            return None
        take_px = float(exit_state["take_px"])
        if position > 0:
            px = best_ask if exit_state.get("take_hit") else max(best_ask, take_px)
            return (False, self._round_price_up(px), size)
        px = best_bid if exit_state.get("take_hit") else min(best_bid, take_px)
        return (True, self._round_price_down(max(TICK_SIZE, px)), size)

    def _maker_fee_bps(self) -> float:
        return max(0.0, self.indicator_config.maker_fee_per_million) / 100.0

    def _round_trip_maker_fee_bps(self) -> float:
        return self._maker_fee_bps() * 2.0

    def _min_fee_aware_half_spread(self, mid_price: float) -> float:
        if mid_price <= 0 or not self.indicator_config.fee_floor_enabled:
            return TICK_SIZE / 2.0
        floor_bps = max(
            self.indicator_config.min_half_spread_bps,
            self._maker_fee_bps() + self.indicator_config.adverse_buffer_bps,
        )
        return max(TICK_SIZE / 2.0, mid_price * floor_bps / 10000.0)

    def _round_price_up(self, price: float) -> float:
        if TICK_SIZE <= 0:
            return round(price, PRICE_DECIMALS)
        return round(math.ceil(price / TICK_SIZE) * TICK_SIZE, PRICE_DECIMALS)

    def _round_price_down(self, price: float) -> float:
        if TICK_SIZE <= 0:
            return round(price, PRICE_DECIMALS)
        return round(math.floor(price / TICK_SIZE) * TICK_SIZE, PRICE_DECIMALS)

    def _set_reject_cooldown(self, reason: str):
        self.reject_pause_until = time.time() + max(0.0, self.REJECT_COOLDOWN_SEC)
        self.last_reject_reason = str(reason)[:90]

    def _current_side_details(self, is_buy: bool) -> List[Dict[str, Any]]:
        oids = self.buy_oids if is_buy else self.sell_oids
        details = []
        for oid in oids:
            raw = self.order_details.get(oid, {})
            try:
                px = float(raw.get("price", 0.0))
                sz = float(raw.get("size", 0.0))
            except (TypeError, ValueError):
                continue
            if px <= 0 or sz <= 0:
                continue
            details.append({
                "price": px,
                "size": sz,
                "reduce_only": bool(raw.get("reduce_only", False)),
                "tag": raw.get("tag", "mm"),
            })
        return sorted(details, key=lambda x: x["price"], reverse=is_buy)

    def _desired_quote_matches_open_orders(
        self,
        bids: List[Tuple[float, float]],
        asks: List[Tuple[float, float]],
        bid_reduce_flags: List[bool],
        ask_reduce_flags: List[bool],
        bid_tags: List[str],
        ask_tags: List[str],
    ) -> bool:
        if not self.buy_oids and not self.sell_oids:
            return False

        tol_px = max(TICK_SIZE, self.SAME_QUOTE_TOLERANCE_TICKS * TICK_SIZE)

        def side_matches(
            desired: List[Tuple[float, float]],
            current: List[Dict[str, Any]],
            flags: List[bool],
            tags: List[str],
        ) -> bool:
            if len(desired) != len(current):
                return False
            for idx, (wanted_px, wanted_sz) in enumerate(desired):
                cur = current[idx]
                if abs(float(wanted_px) - cur["price"]) > tol_px:
                    return False
                size_tol = max(MIN_SIZE, abs(float(wanted_sz)) * 0.25)
                if abs(float(wanted_sz) - cur["size"]) > size_tol:
                    return False
                wanted_reduce = bool(flags[idx]) if idx < len(flags) else False
                if wanted_reduce != cur["reduce_only"]:
                    return False
            return True

        return (
            side_matches(bids, self._current_side_details(True), bid_reduce_flags, bid_tags) and
            side_matches(asks, self._current_side_details(False), ask_reduce_flags, ask_tags)
        )

    def _preflight_quote_levels(
        self,
        bids: List[Tuple[float, float]],
        asks: List[Tuple[float, float]],
        bid_reduce_flags: List[bool],
        ask_reduce_flags: List[bool],
        bid_tags: List[str],
        ask_tags: List[str],
        best_bid: float,
        best_ask: float,
        tif: str,
    ) -> Tuple[List[Tuple[float, float]], List[Tuple[float, float]], List[bool], List[bool], List[str], List[str], int]:
        min_notional = max(MIN_ORDER_NOTIONAL_USD, float(getattr(self, "MIN_ORDER_NOTIONAL_USD", MIN_ORDER_NOTIONAL_USD)))
        strict_post_only = str(tif).lower() == "alo"
        safety_ticks = max(0.0, float(getattr(self, "POST_ONLY_SAFETY_TICKS", POST_ONLY_SAFETY_TICKS)))
        dropped = 0

        def clean_side(
            levels: List[Tuple[float, float]],
            flags: List[bool],
            tags: List[str],
            is_buy: bool,
        ) -> Tuple[List[Tuple[float, float]], List[bool], List[str], int]:
            cleaned_levels: List[Tuple[float, float]] = []
            cleaned_flags: List[bool] = []
            cleaned_tags: List[str] = []
            local_dropped = 0

            for idx, (raw_px, raw_sz) in enumerate(levels):
                reduce_only = bool(flags[idx]) if idx < len(flags) else False
                tag = tags[idx] if idx < len(tags) else "mm"
                try:
                    px = float(raw_px)
                    sz = float(raw_sz)
                except (TypeError, ValueError):
                    local_dropped += 1
                    continue

                if strict_post_only and best_bid > 0 and best_ask > 0:
                    side_safety_ticks = 0.0 if reduce_only and tag == "risk_exit" else safety_ticks
                    if is_buy:
                        px = min(px, best_bid - side_safety_ticks * TICK_SIZE)
                    else:
                        px = max(px, best_ask + side_safety_ticks * TICK_SIZE)

                px = self._round_price_down(px) if is_buy else self._round_price_up(px)
                sz = round(max(0.0, sz), SIZE_DECIMALS)
                if px <= 0 or sz < MIN_SIZE:
                    local_dropped += 1
                    continue
                if strict_post_only and best_bid > 0 and best_ask > 0:
                    if is_buy and px >= best_ask:
                        local_dropped += 1
                        continue
                    if not is_buy and px <= best_bid:
                        local_dropped += 1
                        continue
                if not reduce_only and px * sz < min_notional:
                    local_dropped += 1
                    continue

                cleaned_levels.append((px, sz))
                cleaned_flags.append(reduce_only)
                cleaned_tags.append(tag)

            return cleaned_levels, cleaned_flags, cleaned_tags, local_dropped

        bids, bid_reduce_flags, bid_tags, bid_dropped = clean_side(bids, bid_reduce_flags, bid_tags, True)
        asks, ask_reduce_flags, ask_tags, ask_dropped = clean_side(asks, ask_reduce_flags, ask_tags, False)
        dropped += bid_dropped + ask_dropped
        return bids, asks, bid_reduce_flags, ask_reduce_flags, bid_tags, ask_tags, dropped

    def _is_probably_maker_fill(self, fill: Dict[str, Any], oid: int) -> bool:
        if oid in self.order_details:
            return True
        crossed = fill.get("crossed")
        if crossed is not None:
            return not bool(crossed)
        return False

    def _fill_side_sign(self, direction: str) -> int:
        d = str(direction).lower()
        if "open long" in d or "close short" in d or "buy" in d:
            return 1
        if "open short" in d or "close long" in d or "sell" in d:
            return -1
        return 0

    def _opening_entry_side(self, direction: str) -> Optional[str]:
        d = str(direction).lower()
        if "open long" in d:
            return "buy"
        if "open short" in d:
            return "sell"
        return None

    def _loss_block_side(self, direction: str, closed_pnl: float, fee: float) -> Optional[str]:
        d = str(direction).lower()
        net_pnl = closed_pnl - max(0.0, fee)
        if net_pnl > -0.005:
            return None
        if "close long" in d:
            return "buy"
        if "close short" in d:
            return "sell"
        return None

    def _entry_side_blocked(self, side: str) -> bool:
        return time.time() < self.entry_block_until.get(side, 0.0)

    def _register_entry_guard(self, direction: str, closed_pnl: float, fee: float):
        now_ts = time.time()
        loss_side = self._loss_block_side(direction, closed_pnl, fee)
        if loss_side:
            self.entry_block_until[loss_side] = max(
                self.entry_block_until.get(loss_side, 0.0),
                now_ts + self.LOSS_COOLDOWN_SEC,
            )
            self.same_side_entry_counts[loss_side] = 0
            self.last_guard_reason = f"loss cooldown {loss_side}"
            self.orders_need_sync = True
            self.feed_event.set()
            return

        entry_side = self._opening_entry_side(direction)
        if not entry_side:
            return

        if self.mean_reversion_config.get("enabled", False):
            state = self.last_mr_state if self.last_mr_state else {}
            if isinstance(state.get("bar_index"), int):
                self.mr_last_entry_bar = int(state["bar_index"])
            atr = state.get("atr")
            self.mr_entry_atr = float(atr) if atr and float(atr) > 0 else self.mr_entry_atr
            self.mr_entry_side = "long" if entry_side == "buy" else "short"

        if self.last_entry_side == entry_side and (now_ts - self.last_entry_ts) <= self.ENTRY_STREAK_WINDOW_SEC:
            self.same_side_entry_counts[entry_side] += 1
        else:
            self.same_side_entry_counts[entry_side] = 1
        other_side = "sell" if entry_side == "buy" else "buy"
        self.same_side_entry_counts[other_side] = 0
        self.last_entry_side = entry_side
        self.last_entry_ts = now_ts

        if self.same_side_entry_counts[entry_side] >= self.MAX_SAME_SIDE_ENTRY_STREAK:
            self.entry_block_until[entry_side] = max(
                self.entry_block_until.get(entry_side, 0.0),
                now_ts + self.ENTRY_STREAK_COOLDOWN_SEC,
            )
            self.last_guard_reason = f"entry streak cooldown {entry_side}"
            self.orders_need_sync = True
            self.feed_event.set()

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
                            self.mr_entry_atr = None
                            self.mr_entry_side = None
                
                margin = ch.get("marginSummary", {})
                self.ws_equity = float(margin.get("accountValue", 0))

            # Update Fills & Volume with Advanced Tracking
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
                            order_meta = self.order_details.get(oid, {})
                            
                            self.total_volume += px * sz
                            self.fills_count += 1
                            self.total_fees += fee
                            self.session_pnl += closed_pnl
                            self.indicators.on_fill(px, sz)
                            self.last_indicator_snapshot = self.indicators.snapshot()
                            
                            # === ADVANCED: Track fill for analysis ===
                            self.fill_history.append({
                                'time': time.time(),
                                'price': px,
                                'size': sz,
                                'pnl': closed_pnl,
                                'oid': oid,
                                'fee': fee
                            })
                            self.last_fill_time = time.time()
                            direction = str(fill.get("dir", ""))
                            side_sign = self._fill_side_sign(direction)
                            self._register_fill_slippage(oid, px, side_sign)
                            self._update_toxic_flow(px, side_sign, sz)
                            self._register_markout_fill(px, side_sign, sz)
                            self._register_entry_guard(direction, closed_pnl, fee)
                            is_maker_fill = self._is_probably_maker_fill(fill, oid)
                            if is_maker_fill:
                                self.maker_fills += 1
                                self.maker_fees_paid_usd += max(0.0, fee)
                            else:
                                self.taker_fills += 1
                            if order_meta.get("tag") == "tp":
                                self.tp_capture_pnl_usd += closed_pnl - fee
                            self.useful_volume_usd += max(0.0, px * sz - max(0.0, fee))
                            
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
                            
                            # === INSTANT REQUOTE on fill ===
                            if REQUOTE_ON_FILL:
                                self.orders_need_sync = True
                                self.feed_event.set()
            
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
        vol_per_hour = self.total_volume / elapsed_hr
        inv_notional = abs(self.latest_position * mid_price) if mid_price > 0 else 0.0
        score = self._score_snapshot(mid_price) if mid_price > 0 else 0.0
        avg_dir_skew = (
            sum(self.directional_skew_bps_history) / len(self.directional_skew_bps_history)
            if self.directional_skew_bps_history else 0.0
        )

        print("\n" + "=" * 78)
        print(f"SESSION SUMMARY | {SYMBOL}")
        print("=" * 78)
        print(f"Runtime            : {elapsed_hr:.2f}h")
        print(f"Stop reason        : {self.last_stop_reason or 'N/A'}")
        print(f"Volume total       : ${self.total_volume:,.2f}")
        print(f"Volume utile       : ${self.useful_volume_usd:,.2f}")
        print(f"Fees totales       : ${self.total_fees:,.4f}")
        print(f"PnL session        : ${self.session_pnl:,.4f}")
        print(f"Net cost           : ${net_cost_usd:,.4f} | {net_cost_bps:.2f} bps")
        print(f"Cost per million   : ${cost_per_m:,.2f}")
        print(f"Fills              : {self.fills_count} | {fills_per_min:.2f}/min")
        print(f"Maker ratio        : {maker_ratio:.2f}%")
        print(f"P95 slippage       : {p95_slip:.2f} bps")
        print(f"Inventory notional : ${inv_notional:,.2f}")
        print(f"Toxic loss (est.)  : ${self.toxic_loss_usd:,.4f}")
        print(f"Maker fees paid    : ${self.maker_fees_paid_usd:,.4f} ({self.indicator_config.maker_fee_per_million:.1f}$/M model)")
        print(f"TP capture PnL     : ${self.tp_capture_pnl_usd:,.4f} | TP orders: {self.tp_orders_placed}")
        print(f"Directional skew   : {avg_dir_skew:.3f} bps avg")
        print(f"RSI suppressions   : {self.quotes_suppressed_count}")
        if self.micro_snapshot.ready:
            print(
                f"Micro edge B/S     : {self.micro_snapshot.edge_buy_bps:+.2f}/"
                f"{self.micro_snapshot.edge_sell_bps:+.2f} bps | "
                f"Markout 5s adverse: {self.micro_snapshot.adverse_markout_5s_bps:.2f} bps"
            )
        print(f"Volume/hour        : ${vol_per_hour:,.2f}")
        print(f"Score              : {score:,.2f}")
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
                            else:
                                self.entry_price = None
                                self.position_margin = 0.0
                                self.mr_entry_atr = None
                                self.mr_entry_side = None
                            return (True, size, unrealized_pnl)
            except Exception:
                continue

        if api_ok:
            # API responded and symbol not found -> flat for this symbol.
            self.entry_price = None
            self.position_margin = 0.0
            self.mr_entry_atr = None
            self.mr_entry_side = None
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
                self.total_fees += fee
                self.session_pnl += closed_pnl
                self.fills_count += 1
                oid = int(fill.get("oid", 0))
                direction = fill.get("dir", "")
                self.indicators.on_fill(price, size)
                self.last_indicator_snapshot = self.indicators.snapshot()
                if self._is_probably_maker_fill(fill, oid):
                    self.maker_fills += 1
                    self.maker_fees_paid_usd += max(0.0, fee)
                else:
                    self.taker_fills += 1
                if self.order_details.get(oid, {}).get("tag") == "tp":
                    self.tp_capture_pnl_usd += closed_pnl - fee
                self.useful_volume_usd += max(0.0, trade_value - max(0.0, fee))
                side_sign = self._fill_side_sign(direction)
                self._register_markout_fill(price, side_sign, size)
                self._register_entry_guard(fill.get("dir", ""), closed_pnl, fee)
                
                # Update status with last fill
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

        # 1. FIXED SIZE (Si configuré) - base nominale, puis facteurs de risque.
        if ORDER_SIZE_USD > 0:
            base_size = max(round(ORDER_SIZE_USD / mid_price, SIZE_DECIMALS), MIN_SIZE)
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

        sum_multipliers = max(sum(self.LEVEL_MULTIPLIERS), 1e-9)
        inventory_room_notional = max(0.0, MAX_POSITION_USD - abs(self.latest_position * mid_price))
        max_base_size_by_inventory = inventory_room_notional / sum_multipliers / max(mid_price, 1e-9)
        base_size = min(base_size, max_base_size_by_inventory)
        if max_base_size_by_inventory < min_size_target:
            return 0.0

        vol = max(0.0, self.volatility / 100.0)
        vol_factor = max(0.35, 1.0 - min(0.55, vol * 35.0))
        imbalance = abs(self._orderbook_imbalance())
        depth_factor = 1.0 - min(0.20, imbalance * 0.25)
        inv_notional = abs(self.latest_position * mid_price)
        inv_ratio = inv_notional / max(MAX_POSITION_USD, 1e-9)
        inventory_factor = max(0.30, 1.0 - min(0.60, inv_ratio * 0.75))
        toxic_factor = 0.8 if time.time() < self.toxic_pause_until else 1.0
        bb_size_factor = 1.0
        snap = self._indicator_snapshot()
        if self.indicator_config.enabled and snap.bollinger_bandwidth is not None:
            bw = snap.bollinger_bandwidth
            if bw > self.indicator_config.bollinger_expansion_threshold * 1.5:
                bb_size_factor = self.indicator_config.expansion_size_factor
            elif bw < self.indicator_config.bollinger_squeeze_threshold:
                bb_size_factor = self.indicator_config.squeeze_size_factor
        edge_factor = 1.0
        if self.microstructure_config.get("enabled", False) and self.micro_snapshot.ready:
            min_edge = float(self._micro_cfg("min_edge_bps", 0.10))
            if max(self.micro_snapshot.edge_buy_bps, self.micro_snapshot.edge_sell_bps) < min_edge:
                edge_factor = float(self._micro_cfg("low_edge_size_factor", 0.60))
            if self.micro_snapshot.toxic:
                edge_factor = min(edge_factor, 0.50)

        margin_reject_factor = 0.5 if time.time() < self.margin_reject_pause_until else 1.0
        adjusted = base_size * vol_factor * depth_factor * inventory_factor * toxic_factor * bb_size_factor * edge_factor * self.dynamic_size_scale * margin_reject_factor
        if adjusted <= 0:
            return 0.0
        return max(round(adjusted, SIZE_DECIMALS), round(min_size_target, SIZE_DECIMALS))

    def calculate_levels(
        self,
        best_bid: float,
        best_ask: float,
        position: float,
    ) -> tuple[list[tuple[float, float]], list[tuple[float, float]], list[bool], list[bool], list[str], list[str]]:
        """
        ULTRA-OPTIMIZED Level Calculation with Avellaneda-Stoikov Model
        
        Objectifs:
        1. MAX VOLUME: Spreads serrés, beaucoup de niveaux
        2. MIN LOSS: Skew intelligent basé sur l'inventaire
        3. ADAPTATIF: Spread dynamique selon volatilité
        """
        tick = TICK_SIZE  # Utiliser le tick_size du token configuré
        mid_price = (best_bid + best_ask) / 2
        snap = self._indicator_snapshot()

        # === 0. ADAPTIVE REGIME: score the market from live candles ===
        if self.adaptive_engine is not None and self.adaptive_engine.enabled:
            try:
                regime_bars = self.indicators.get_bars(
                    timeframe_sec=self.adaptive_engine.bar_seconds,
                    max_bars=self.adaptive_engine.vp_bars + self.adaptive_engine.atr_period + 5,
                )
                self.adaptive_engine.update(regime_bars, mid_price)
            except Exception:
                pass

        # === 1. DYNAMIC SPREAD CALCULATION ===
        adaptive_spread_ticks = self._calculate_adaptive_spread()
        self.current_spread_ticks = adaptive_spread_ticks
        
        # === 2. AVELLANEDA-STOIKOV FAIR PRICE ===
        # Formule: Fair = Mid - q * gamma * sigma^2 * T
        # Simplifié pour HFT: Fair = Mid - position * skew_factor
        
        # Skew borné et stable (Avellaneda-Stoikov simplifié)
        position_normalized = position * mid_price / 100  # Normaliser en $ centaines
        inventory_skew = self._compute_inventory_skew(mid_price, position)
        blended_mid = mid_price
        if self.indicator_config.enabled and snap.vwap and snap.vwap > 0:
            vwap_weight = max(0.0, min(0.50, self.indicator_config.vwap_blend_weight))
            blended_mid = (1.0 - vwap_weight) * mid_price + vwap_weight * snap.vwap
        if self.microstructure_config.get("enabled", False) and self.micro_snapshot.ready and self.micro_snapshot.microprice:
            micro_weight = max(0.0, min(0.75, float(self._mm_cfg("microprice_weight", 0.35))))
            blended_mid = (1.0 - micro_weight) * blended_mid + micro_weight * self.micro_snapshot.microprice
        directional_skew = self._compute_directional_skew(mid_price, snap)
        fair_price = blended_mid - inventory_skew + directional_skew
        
        # === 3. SPREAD OPTIMAL ===
        # Base spread + volatility + fill/latency/toxic adjustments
        half_spread = self._compute_dynamic_half_spread(mid_price, best_bid, best_ask)

        # === 2b. ADAPTIVE CHOP TUNING ===
        # In chop: anchor the grid on the value area (POC) and scale the spread
        # to ATR. Always keep a hard floor that covers fees (article's rule).
        adaptive = self.adaptive_engine
        if adaptive is not None and adaptive.enabled and adaptive.ready and not adaptive.is_trend:
            if adaptive.poc and adaptive.poc > 0:
                w = max(0.0, min(0.75, adaptive.poc_anchor_weight))
                fair_price = (1.0 - w) * fair_price + w * adaptive.poc
            atr_half = adaptive.atr_half_spread(mid_price)
            if atr_half > 0:
                half_spread = max(half_spread, atr_half)
        # Hard fee floor: the spread must always cover round-trip fees.
        half_spread = max(half_spread, self._min_fee_aware_half_spread(mid_price))

        edge_buy_bps, edge_sell_bps = self._microstructure_edge(mid_price, half_spread)

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
        
        # === 5. GÉNÉRATION DE LA GRILLE PYRAMIDALE ===
        for i in range(self.NUM_LEVELS if base_size > 0 else 0):
            # Écartement progressif
            level_offset = i * self.LEVEL_TICK_SPACING * tick
            
            # Multiplicateur de taille configuré par niveau
            if i < len(self.LEVEL_MULTIPLIERS):
                multiplier = self.LEVEL_MULTIPLIERS[i]
            else:
                multiplier = self.LEVEL_MULTIPLIERS[-1] * 0.5
            
            # === SIZE BOOST basé sur la direction de l'inventaire ===
            # Si on est long, boost les asks pour vendre plus vite
            # Si on est short, boost les bids pour racheter plus vite
            exit_boost = 1.0
            if position > max(MIN_SIZE * 0.5, 1e-9):  # Long position
                exit_boost = 1.0 + min(abs(position_normalized) * 0.2, 0.5) if i < 2 else 1.0
                # Reduce bid size when long
                bid_size_factor = max(0.3, 1.0 - abs(position_normalized) * 0.15)
                ask_size_factor = exit_boost
            elif position < -max(MIN_SIZE * 0.5, 1e-9):  # Short position
                exit_boost = 1.0 + min(abs(position_normalized) * 0.2, 0.5) if i < 2 else 1.0
                ask_size_factor = max(0.3, 1.0 - abs(position_normalized) * 0.15)
                bid_size_factor = exit_boost
            else:
                bid_size_factor = 1.0
                ask_size_factor = 1.0
            
            # Calcul Taille
            bid_size = round(base_size * multiplier * bid_size_factor, SIZE_DECIMALS)
            ask_size = round(base_size * multiplier * ask_size_factor, SIZE_DECIMALS)
            
            p_bid = round(start_bid - level_offset, PRICE_DECIMALS)
            p_ask = round(start_ask + level_offset, PRICE_DECIMALS)
            
            # Validation: prix positifs et tailles minimales
            if p_bid > 0 and bid_size >= MIN_SIZE:
                bids.append((p_bid, round(bid_size, SIZE_DECIMALS)))
            if p_ask > 0 and ask_size >= MIN_SIZE:
                asks.append((p_ask, round(ask_size, SIZE_DECIMALS)))

        # RSI extremes: suppress only the inventory-increasing side.
        if self.indicator_config.enabled and self.indicator_config.rsi_quote_suppression and snap.ready and snap.rsi is not None:
            if snap.rsi >= self.indicator_config.rsi_extreme_overbought and bids:
                self.quotes_suppressed_count += len(bids)
                bids = []
            elif snap.rsi <= self.indicator_config.rsi_extreme_oversold and asks:
                self.quotes_suppressed_count += len(asks)
                asks = []

        regime = self._market_data_regime(mid_price, snap)
        if regime.get("enabled") and self.market_data_config.get("block_fade_side", False):
            if regime.get("trend_up") and position >= 0 and bids:
                self.quotes_suppressed_count += len(bids)
                bids = []
            elif regime.get("trend_down") and position <= 0 and asks:
                self.quotes_suppressed_count += len(asks)
                asks = []

        eps_pos = max(MIN_SIZE * 0.5, 1e-9)

        # === ADAPTIVE REGIME FILTER (PRIMARY DEFENSE) ===
        # In a trend, market making bleeds: one side keeps filling while the
        # other runs away, so the bot accumulates a one-sided position. The
        # single best defense is to stop quoting into the trend. We go
        # flatten-only: keep ONLY the side that reduces the current position,
        # and when flat we pause entirely until the score cools down.
        if self.adaptive_engine is not None and self.adaptive_engine.enabled and self.adaptive_engine.is_trend:
            if position > eps_pos:          # long -> only sells reduce
                if bids:
                    self.quotes_suppressed_count += len(bids)
                    bids = []
            elif position < -eps_pos:       # short -> only buys reduce
                if asks:
                    self.quotes_suppressed_count += len(asks)
                    asks = []
            else:                            # flat -> pause both sides
                self.quotes_suppressed_count += len(bids) + len(asks)
                bids = []
                asks = []
            self.last_adaptive_reason = (
                f"TREND score={self.adaptive_engine.trend_score:.2f} "
                f"dir={self.adaptive_engine.trend_dir:+d} flatten-only"
            )
        elif self.adaptive_engine is not None and self.adaptive_engine.ready:
            self.last_adaptive_reason = f"CHOP score={self.adaptive_engine.trend_score:.2f} quoting"

        if self._entry_side_blocked("buy") and position >= -eps_pos and bids:
            self.quotes_suppressed_count += len(bids)
            bids = []
        if self._entry_side_blocked("sell") and position <= eps_pos and asks:
            self.quotes_suppressed_count += len(asks)
            asks = []

        if not self._is_flat_position(position) and self.latest_unrealized_pnl < -0.015:
            if position > 0 and bids:
                self.quotes_suppressed_count += len(bids)
                bids = []
            elif position < 0 and asks:
                self.quotes_suppressed_count += len(asks)
                asks = []

        if self.microstructure_config.get("enabled", False) and self._micro_cfg("block_negative_edge", True):
            if not self._micro_allows_side("buy") and position >= -eps_pos and bids:
                self.quotes_suppressed_count += len(bids)
                bids = []
            if not self._micro_allows_side("sell") and position <= eps_pos and asks:
                self.quotes_suppressed_count += len(asks)
                asks = []

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

        # Breakout guard: mute the side that would get run over by the developing big candle.
        # Keep the opposite side alive (already widened in _compute_dynamic_half_spread) to
        # capture mean-reversion. Reduce-only side stays alive even if it's the exposed side.
        if self.breakout_guard is not None and self.breakout_guard.enabled():
            now_ts_bg = time.time()
            if self.breakout_guard.is_paused("bid", now_ts_bg) and position >= -eps_pos and bids:
                self.quotes_suppressed_count += len(bids)
                bids = []
            if self.breakout_guard.is_paused("ask", now_ts_bg) and position <= eps_pos and asks:
                self.quotes_suppressed_count += len(asks)
                asks = []

        bid_reduce_only_flags = [False] * len(bids)
        ask_reduce_only_flags = [False] * len(asks)
        bid_tags = ["mm"] * len(bids)
        ask_tags = ["mm"] * len(asks)

        if position > 0 and not asks:
            reduce_quote = self._compute_passive_reduce_quote(best_bid, best_ask, mid_price, position)
            if reduce_quote:
                _, reduce_px, reduce_size = reduce_quote
                asks.append((reduce_px, reduce_size))
                ask_reduce_only_flags.append(True)
                ask_tags.append("reduce_fallback")
        elif position < 0 and not bids:
            reduce_quote = self._compute_passive_reduce_quote(best_bid, best_ask, mid_price, position)
            if reduce_quote:
                _, reduce_px, reduce_size = reduce_quote
                bids.append((reduce_px, reduce_size))
                bid_reduce_only_flags.append(True)
                bid_tags.append("reduce_fallback")

        tp_quote = self._compute_take_profit_quote(best_bid, best_ask, mid_price, position, max(base_size, MIN_SIZE), snap)
        if tp_quote:
            is_buy, tp_px, tp_size = tp_quote
            if is_buy:
                bids.append((tp_px, tp_size))
                bid_reduce_only_flags.append(True)
                bid_tags.append("tp")
            else:
                asks.append((tp_px, tp_size))
                ask_reduce_only_flags.append(True)
                ask_tags.append("tp")

        if not bids and not asks and self._is_flat_position(position) and base_size > 0:
            (
                bids,
                asks,
                bid_reduce_only_flags,
                ask_reduce_only_flags,
                bid_tags,
                ask_tags,
            ) = self._build_probe_quote(best_bid, best_ask, mid_price, base_size)

        if self.NUM_LEVELS <= 1 or ACTIVE_TOKEN in SINGLE_LEVEL_TOKENS:
            bids = bids[:1]
            asks = asks[:1]
            bid_reduce_only_flags = bid_reduce_only_flags[:1]
            ask_reduce_only_flags = ask_reduce_only_flags[:1]
            bid_tags = bid_tags[:1]
            ask_tags = ask_tags[:1]

        return bids, asks, bid_reduce_only_flags, ask_reduce_only_flags, bid_tags, ask_tags

    def cancel_all_orders(self):
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
            # Keep order metadata for late fill events; order ids are unique and needed
            # to classify maker fills even when maker fees are positive.
            
            return all_oids
        except Exception as e:
            self.buy_oids = []
            self.sell_oids = []
            return []

    def _cancel_side(self, side: str) -> List[int]:
        """Bulk-cancel only one side (`bid` or `ask`). Used by the breakout guard."""
        if side == "bid":
            target_oids = list(self.buy_oids)
        elif side == "ask":
            target_oids = list(self.sell_oids)
        else:
            return []
        if not target_oids:
            return []
        try:
            cancellations = [{"coin": SYMBOL, "oid": oid} for oid in target_oids]
            self.exchange.bulk_cancel(cancellations)
            cancel_ts = time.time()
            for _ in target_oids:
                self.cancel_events.append(cancel_ts)
            if side == "bid":
                self.buy_oids = []
            else:
                self.sell_oids = []
            return target_oids
        except Exception:
            if side == "bid":
                self.buy_oids = []
            else:
                self.sell_oids = []
            return []

    def close_position(self, known_position: Optional[float] = None, force: bool = False, trigger: str = "") -> bool:
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

    def place_order_batch(
        self,
        bids,
        asks,
        tif: str = "Gtc",
        bid_reduce_only_flags: Optional[List[bool]] = None,
        ask_reduce_only_flags: Optional[List[bool]] = None,
        bid_tags: Optional[List[str]] = None,
        ask_tags: Optional[List[str]] = None,
    ):
        """Place all orders in a single batch request for TRUE speed"""
        if not bids and not asks:
            return [], []
            
        order_requests = []
        request_meta = []
        bid_reduce_only_flags = bid_reduce_only_flags or [False] * len(bids)
        ask_reduce_only_flags = ask_reduce_only_flags or [False] * len(asks)
        bid_tags = bid_tags or ["mm"] * len(bids)
        ask_tags = ask_tags or ["mm"] * len(asks)

        for idx, (p, s) in enumerate(bids):
            reduce_only = bool(bid_reduce_only_flags[idx]) if idx < len(bid_reduce_only_flags) else False
            order_requests.append({
                "coin": SYMBOL,
                "is_buy": True,
                "sz": s,
                "limit_px": p,
                "order_type": {"limit": {"tif": tif}},
                "reduce_only": reduce_only
            })
            request_meta.append({"tag": bid_tags[idx] if idx < len(bid_tags) else "mm"})
            
        for idx, (p, s) in enumerate(asks):
            reduce_only = bool(ask_reduce_only_flags[idx]) if idx < len(ask_reduce_only_flags) else False
            order_requests.append({
                "coin": SYMBOL,
                "is_buy": False,
                "sz": s,
                "limit_px": p,
                "order_type": {"limit": {"tif": tif}},
                "reduce_only": reduce_only
            })
            request_meta.append({"tag": ask_tags[idx] if idx < len(ask_tags) else "mm"})

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
                            "reduce_only": req.get("reduce_only", False),
                            "tag": request_meta[i].get("tag", "mm") if i < len(request_meta) else "mm",
                            "ts": time.time(),
                        }
                        if req.get("reduce_only") and self.order_details[oid].get("tag") == "tp":
                            self.tp_orders_placed += 1
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
                    self._set_reject_cooldown("insufficient margin")
                    self.last_status = (
                        f"⚠️ Insufficient margin x{insufficient_margin_errors} | "
                        f"size_scale={self.dynamic_size_scale:.2f}"
                    )
                    self.get_account_info_cached(force=True)
                elif other_reject_errors:
                    sample = " | ".join(other_reject_errors[:2])
                    self._set_reject_cooldown(sample)
                    if not new_buy_oids and not new_sell_oids:
                        self.last_status = f"⏸️ Bulk rejects: {sample[:90]}"
                    else:
                        self.last_status = f"⚠️ Partial reject: {sample[:70]}"
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
                    self._set_reject_cooldown("insufficient margin")
                    self.last_status = f"⚠️ Bulk margin reject | size_scale={self.dynamic_size_scale:.2f}"
                    self.get_account_info_cached(force=True)
                else:
                    self._set_reject_cooldown(err_msg)
                    self.last_status = f"⏸️ Bulk Error: {str(result.get('response'))[:60]}"
            
            return new_buy_oids, new_sell_oids
        except Exception as e:
            self._set_reject_cooldown(str(e))
            self.last_status = f"❌ Batch Exception"
            return [], []

    def place_order(self, is_buy: bool, price: float, size: float, tif: str = "Gtc") -> Optional[int]:
        """Place a limit order and return order ID"""
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
                self._set_reject_cooldown(err_msg)
                self.last_status = f"❌ Order Rejected: {err_msg[:30]}"
                return None

            statuses = result.get("response", {}).get("data", {}).get("statuses", [])
            if not statuses:
                self.last_status = "❌ Empty Response"
                return None

            st = statuses[0]
            if "resting" in st:
                oid = st["resting"]["oid"]
                self.order_details[oid] = {"price": price, "size": size, "is_buy": is_buy, "reduce_only": False, "tag": "single"}
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
                self._set_reject_cooldown(str(st.get("error", "")))
                self.last_status = f"❌ {st['error'][:30]}"
                return None
                
            return None
        except Exception as e:
            self._set_reject_cooldown(str(e))
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
        
        cost_color = "green" if cost_per_m < MAX_COST_PER_MILLION * 0.7 else "yellow" if cost_per_m < MAX_COST_PER_MILLION else "red"
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
        
        # Volume Projection
        elapsed_hours = (time.time() - self.start_time) / 3600
        if elapsed_hours > 0:
            current_hourly_rate = self.total_volume / elapsed_hours
        else:
            current_hourly_rate = 0
        
        # Fills per minute
        elapsed_mins = max((time.time() - self.start_time) / 60, 1)
        fills_per_min = self.fills_count / elapsed_mins
        
        # Calculer le drift actuel pour l'affichage
        current_drift = 0
        if self.orders_anchor_price and self.latest_bbo:
            current_drift = int(abs(self.latest_bbo[2] - self.orders_anchor_price) / TICK_SIZE)
        
        # Add enhanced caption
        vol_info = f"Vol/H: [bold yellow]${current_hourly_rate:.0f}[/bold yellow]"
        fill_info = f"Fills/min: [cyan]{fills_per_min:.1f}[/cyan]"
        spread_info = f"Spread: [magenta]{self.current_spread_ticks if hasattr(self, 'current_spread_ticks') else '-'}t[/magenta]"
        drift_info = f"Drift: [{'red' if current_drift >= 2 else 'yellow' if current_drift >= 1 else 'green'}]{current_drift}t[/]"
        
        table.caption = f"\n⏱️  {vol_info} | {fill_info} | {spread_info} | {drift_info}"

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
        snap = self._indicator_snapshot()
        vwap_txt = f"${snap.vwap:.2f}" if snap.vwap else "-"
        fee_floor_txt = f"{self.current_fee_floor_ticks:.1f}t" if self.current_fee_floor_ticks else "-"
        regime = self._market_data_regime(mid_price, snap)
        if regime.get("trend_up"):
            regime_txt = "Trend up"
        elif regime.get("trend_down"):
            regime_txt = "Trend down"
        elif regime.get("calm_liquid"):
            regime_txt = "Calm liquid"
        elif regime.get("thin_book"):
            regime_txt = "Thin"
        else:
            regime_txt = "Neutral"
        cancel_rate = self._cancel_rate_per_min()
        micro = self.micro_snapshot
        micro_txt = (
            f"MP {micro.microprice_bps:+.2f}bps | OFI {micro.ofi_norm:+.2f} | "
            f"Pr {micro.pressure_bps:+.2f}bps"
            if micro.ready else "-"
        )
        markout_txt = (
            f"1/5/15s {micro.adverse_markout_1s_bps:.2f}/"
            f"{micro.adverse_markout_5s_bps:.2f}/{micro.adverse_markout_15s_bps:.2f}bps"
            if micro.ready else "-"
        )
        mm_txt = (
            f"q {self.mm_model_state.get('q_norm', 0.0):+.2f} | "
            f"sigma {self.mm_model_state.get('sigma_bps', 0.0):.2f}bps | "
            f"skew {self.mm_model_state.get('inventory_skew_ticks', 0.0):+.1f}t | "
            f"risk {self.mm_model_state.get('risk_ticks', 0.0):.1f}t | "
            f"k {self.mm_model_state.get('k', 0.0):.2f}"
        )
        edge_txt = (
            f"B/S {micro.edge_buy_bps:+.2f}/{micro.edge_sell_bps:+.2f}bps"
            if micro.ready else "-"
        )
        status_table.add_row("Runtime", f"{runtime_str} (started {launch_time})")
        status_table.add_row("PAIR", f"[cyan]{SYMBOL}[/]")
        status_table.add_row("Leverage", f"[yellow]{self.target_leverage}x[/]")
        status_table.add_row("Orders", f"[green]{n_bids}B[/] / [red]{n_asks}A[/] | Drift: {current_drift}t")
        status_table.add_row("Fills", f"{self.fills_count} trades | Best PnL: ${self.best_pnl_session:.3f}")
        status_table.add_row("MM Model", mm_txt)
        status_table.add_row("Book", f"{regime_txt} | Depth ${regime.get('depth_usd', 0.0):.0f} | Spr {regime.get('spread_ticks', 0.0):.1f}t | VWAP {vwap_txt}")
        ae = getattr(self, "adaptive_engine", None)
        if ae is not None and ae.enabled:
            if ae.ready:
                _reg = "[red]TREND[/]" if ae.is_trend else "[green]CHOP[/]"
                _va = f"VA[{ae.val:.2f}-{ae.vah:.2f}]" if ae.val and ae.vah else "VA[--]"
                adaptive_txt = (
                    f"{_reg} score {ae.trend_score:.2f} | ER {ae.efficiency_ratio:.2f} | "
                    f"VPexp {ae.vp_expansion:.2f} | {_va} | dir {ae.trend_dir:+d}"
                )
            else:
                adaptive_txt = "[dim]warming up...[/]"
            status_table.add_row("Adaptive", adaptive_txt)
        status_table.add_row("Micro", f"{micro_txt} | Markout {markout_txt}")
        status_table.add_row("Fees/Edge", f"Maker {self.indicator_config.maker_fee_per_million:.1f}$/M | Floor {fee_floor_txt} | Paid ${self.maker_fees_paid_usd:.4f} | Edge {edge_txt}")
        status_table.add_row("Risk", f"TP ${self.tp_capture_pnl_usd:.4f} ({self.tp_orders_placed}) | Cxl/min {cancel_rate:.0f}")
        
        status_table.add_row("Status", self.last_status)
        
        # Layout
        layout = Layout()
        layout.split_column(
            Layout(Align.center(table), name="main", size=7),
            Layout(Align.center(status_table), name="status", size=15)
        )
        
        return Panel(
            Group(Align.center(Text.from_markup(logo_content)), layout),
            title=f"[bold cyan]🚀 {self.strategy_name.upper()}[/bold cyan]",
            subtitle=f"[dim]Press Ctrl+C to stop | Refresh: {self.current_refresh_interval*1000:.0f}ms | Mode: {'TURBO' if SMART_SPREAD_ENABLED else 'STD'}[/dim]",
            border_style="cyan",
            padding=(1, 2)
        )
    
    def should_quote_buy(self, position: float, mid_price: float) -> bool:
        """Check if we should place a buy order based on position limits (USD Value)"""
        current_notional = position * mid_price
        return current_notional < MAX_POSITION_USD
    
    def should_quote_sell(self, position: float, mid_price: float) -> bool:
        """Check if we should place a sell order based on position limits (USD Value)"""
        current_notional = position * mid_price
        return current_notional > -MAX_POSITION_USD
    
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
        momentum = self._compute_recent_momentum()
        momentum_triggered = abs(momentum) > self.MOMENTUM_THRESHOLD

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

        # === KILL SWITCH: COST PER MILLION ===
        if (
            ENABLE_COST_HARD_STOP and
            self.total_volume >= MIN_VOLUME_FOR_KILL_SWITCH_USD and
            self.fills_count >= MIN_FILLS_FOR_KILL_SWITCH
        ):
            net_cost = self.total_fees - self.session_pnl
            cost_per_m = net_cost / (self.total_volume / 1_000_000)
            if cost_per_m > MAX_COST_PER_MILLION:
                self.last_status = f"🛑 KILL SWITCH: Cost/1M (${cost_per_m:.0f}) > {MAX_COST_PER_MILLION}"
                self.emergency_full_stop(reason="cost_limit_stop")
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

            stop_by_roe = roe_pct <= -self.STOP_LOSS_ROE_PCT
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
        if self.last_ws_msg_ts > 0 and (time.time() - self.last_ws_msg_ts) > self.FEED_STALE_SECONDS:
            stale_age = time.time() - self.last_ws_msg_ts
            self.cancel_all_orders()
            if (time.time() - self.last_ws_reconnect_ts) >= self.WS_RECONNECT_COOLDOWN_SEC:
                self.last_ws_reconnect_ts = time.time()
                try:
                    self._start_websocket()
                    self.last_status = f"🛡️ Feed stale {stale_age:.1f}s - WS reconnect"
                except Exception as e:
                    self.last_status = f"🛡️ Feed stale {stale_age:.1f}s - reconnect fail {str(e)[:30]}"
            else:
                self.last_status = f"🛡️ Feed stale > {self.FEED_STALE_SECONDS:.1f}s - quotes pulled"
            hard_stale_threshold = self.FEED_STALE_SECONDS * FEED_STALE_HARD_STOP_MULTIPLIER
            if ENABLE_FEED_STALE_HARD_STOP and stale_age > hard_stale_threshold:
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
        # === NOUVELLE LOGIQUE: DRIFT DE PRIX EN TICKS ===
        # =====================================================================
        tick = TICK_SIZE
        
        # Calculer le drift = combien de ticks le mid a bougé vs notre ancrage
        price_drift_ticks = 0
        if self.orders_anchor_price is not None:
            price_drift = abs(mid_price - self.orders_anchor_price)
            price_drift_ticks = int(price_drift / tick)
        
        # Position drift = position a changé significativement
        position_changed = False
        if self.orders_anchor_position is not None:
            pos_diff = abs(position - self.orders_anchor_position)
            # Position a changé de plus de 10% ou fill significatif
            if self.orders_anchor_position != 0:
                position_changed = pos_diff / abs(self.orders_anchor_position) > 0.1
            else:
                position_changed = pos_diff > 0.001
        
        # Ordres présents?
        has_orders = len(self.buy_oids) > 0 or len(self.sell_oids) > 0
        target_bid_count = getattr(self, "last_target_bid_count", self.NUM_LEVELS)
        target_ask_count = getattr(self, "last_target_ask_count", self.NUM_LEVELS)
        orders_complete = (
            len(self.buy_oids) >= target_bid_count and
            len(self.sell_oids) >= target_ask_count
        )
        
        # === EXIT LOGIC - MULTI-TIER ===
        need_emergency_exit = False
        need_urgent_exit = False
        need_quick_exit = False
        loss_reduce_mode = False
        exit_side = None
        
        if abs(position) > 0.001:
            exit_side = "SELL" if position > 0 else "BUY"
            position_notional = abs(position * mid_price)
            
            if unrealized_pnl < -self.STOP_LOSS_THRESHOLD:
                need_emergency_exit = True
            elif (
                unrealized_pnl < -QUICK_EXIT_THRESHOLD or
                position_notional > MAX_POSITION_USD * 0.8 or
                (abs(position) > 0.0001 and "roe_pct" in locals() and roe_pct <= -self.URGENT_EXIT_ROE_PCT)
            ):
                need_urgent_exit = True
            elif (
                unrealized_pnl < -0.02 or
                (abs(position) > 0.0001 and "roe_pct" in locals() and roe_pct <= -self.LOSS_REDUCE_ROE_PCT)
            ):
                need_quick_exit = True
                loss_reduce_mode = True

        indicator_snapshot = self._indicator_snapshot()
        ema_cross_reduce_only, ema_exit_side = self._should_ema_cross_exit(position, indicator_snapshot)
        if ema_cross_reduce_only and self.indicator_config.ema_cross_market_exit:
            self.last_ema_exit_bar_ts = indicator_snapshot.last_bar_ts
            self.last_status = f"⚡ EMA cross market exit {ema_exit_side}"
            self.cancel_all_orders()
            self.close_position(known_position=position, trigger="ema_cross")
            return (mid_price, position)
        
        # =====================================================================
        # === DÉCISION DE REFRESH BASÉE SUR LE DRIFT ===
        # =====================================================================
        need_refresh = False
        refresh_reason = ""
        quote_age = time.time() - self.last_quote_update_ts if self.last_quote_update_ts > 0 else 999.0
        cancel_rate = self._cancel_rate_per_min()
        churn_limited = cancel_rate >= self.MAX_CANCELS_PER_MIN and quote_age < self.MAX_QUOTE_TTL
        can_requote = quote_age >= self.MIN_REQUOTE_INTERVAL and not churn_limited
        
        # Priorité 1: Urgences (toujours refresh immédiat)
        if need_emergency_exit:
            need_refresh = True
            refresh_reason = f"🚨 STOP LOSS {exit_side}"
            self.current_refresh_interval = REFRESH_SPEED_FAST
        
        elif need_urgent_exit:
            need_refresh = True
            refresh_reason = f"⚠️ URGENT {exit_side}"
            self.current_refresh_interval = REFRESH_SPEED_FAST

        elif ema_cross_reduce_only:
            need_refresh = True
            refresh_reason = f"📉 EMA exit {ema_exit_side}"
            self.current_refresh_interval = REFRESH_SPEED_FAST

        # Priorité 2: Pas d'ordres = placement initial
        elif not has_orders:
            need_refresh = True
            refresh_reason = "📊 Initial placement"
            self.current_refresh_interval = REFRESH_SPEED_FAST

        # Priorité 2b: sync demandé après fill ou event user
        elif self.orders_need_sync:
            need_refresh = True
            refresh_reason = "⚡ Fill sync"
            self.current_refresh_interval = REFRESH_SPEED_FAST

        elif momentum_triggered and can_requote:
            need_refresh = True
            refresh_reason = f"⚡ Momentum {momentum*100:.3f}%"
            self.current_refresh_interval = REFRESH_SPEED_FAST
        
        # Priorité 3: Quick exit si en perte
        elif need_quick_exit and price_drift_ticks >= 1:
            need_refresh = True
            refresh_reason = f"🔄 Reducing (drift: {price_drift_ticks}t)"
            self.current_refresh_interval = REFRESH_SPEED_NORMAL
        
        # Priorité 4: DRIFT URGENT (2+ ticks) = refresh immédiat
        elif price_drift_ticks >= self.URGENT_DRIFT_TICKS and can_requote:
            need_refresh = True
            refresh_reason = f"⚡ DRIFT {price_drift_ticks}t"
            self.current_refresh_interval = REFRESH_SPEED_FAST
        
        # Priorité 5: DRIFT NORMAL (1 tick) = refresh rapide
        elif price_drift_ticks >= self.PRICE_DRIFT_TICKS and can_requote:
            need_refresh = True
            refresh_reason = f"📈 Drift {price_drift_ticks}t"
            self.current_refresh_interval = REFRESH_SPEED_NORMAL
        
        # Priorité 6: Position a changé (fill détecté)
        elif position_changed:
            need_refresh = True
            refresh_reason = "🔄 Position changed"
            self.current_refresh_interval = REFRESH_SPEED_NORMAL
        
        # Priorité 7: Ordres incomplets (certains ont été fill)
        elif not orders_complete and has_orders:
            need_refresh = True
            refresh_reason = f"↻ Incomplete ({len(self.buy_oids)}B/{len(self.sell_oids)}A)"
            self.current_refresh_interval = REFRESH_SPEED_FAST  # Plus rapide pour éviter les gaps

        # Priorité 8: TTL expiré pour éviter des quotes obsolètes
        elif quote_age >= self.MAX_QUOTE_TTL:
            need_refresh = True
            refresh_reason = f"⏲️ TTL {quote_age:.1f}s"
            self.current_refresh_interval = REFRESH_SPEED_NORMAL
        
        # Priorité 8: Sync périodique (toutes les 3 secondes pour éviter les gaps)
        elif time.time() - self.last_sync_time > self.PERIODIC_SYNC_SECONDS and can_requote:
            need_refresh = True
            refresh_reason = "🔄 Periodic sync"
            self.current_refresh_interval = REFRESH_SPEED_NORMAL
            self.last_sync_time = time.time()
        
        else:
            # === MARCHÉ STABLE: On ne touche à rien! ===
            self.current_refresh_interval = REFRESH_SPEED_SLOW
            anchor_info = f"Anchor: ${self.orders_anchor_price:.2f}" if self.orders_anchor_price else "---"
            churn_txt = f" | cancels/min {cancel_rate:.0f}" if churn_limited else ""
            self.last_status = f"{ws_icon} 💎 STABLE @ ${mid_price:.2f} | {anchor_info} | Drift: {price_drift_ticks}t{churn_txt}"
            return (mid_price, position)
        
        # === UPDATE STATE ===
        self.last_bbo = self.latest_bbo
        self.last_position = position
        self.last_mid_price = mid_price
        
        # === CALCULATE LEVELS ===
        bid_levels, ask_levels, bid_reduce_flags, ask_reduce_flags, bid_tags, ask_tags = self.calculate_levels(best_bid, best_ask, position)
        
        # === RISK REDUCTION MODE (post-only, no aggressive crossing) ===
        if need_emergency_exit:
            reduce_quote = self._compute_passive_reduce_quote(best_bid, best_ask, mid_price, position, urgent=True)
            if exit_side == "SELL":
                bid_levels = []
                bid_reduce_flags = []
                bid_tags = []
                ask_levels = [(reduce_quote[1], reduce_quote[2])] if reduce_quote else ask_levels[:1]
                ask_reduce_flags = [True] * len(ask_levels)
                ask_tags = ["risk_exit"] * len(ask_levels)
            else:
                ask_levels = []
                ask_reduce_flags = []
                ask_tags = []
                bid_levels = [(reduce_quote[1], reduce_quote[2])] if reduce_quote else bid_levels[:1]
                bid_reduce_flags = [True] * len(bid_levels)
                bid_tags = ["risk_exit"] * len(bid_levels)

        elif momentum_triggered:
            if momentum > 0:
                bid_levels = []
                bid_reduce_flags = []
                bid_tags = []
                ask_levels = ask_levels[:max(1, min(2, len(ask_levels)))]
                ask_reduce_flags = ask_reduce_flags[:len(ask_levels)]
                ask_tags = ask_tags[:len(ask_levels)]
            else:
                ask_levels = []
                ask_reduce_flags = []
                ask_tags = []
                bid_levels = bid_levels[:max(1, min(2, len(bid_levels)))]
                bid_reduce_flags = bid_reduce_flags[:len(bid_levels)]
                bid_tags = bid_tags[:len(bid_levels)]
        
        # === MOMENTUM/FLASH PROTECTION ===
        elif price_drift_ticks >= self.URGENT_DRIFT_TICKS and self.orders_anchor_price is not None:
            # Grand drift = on réduit les ordres du côté défavorable
            if mid_price > self.orders_anchor_price:
                # Prix monte: on garde les asks, réduit les bids (évite d'acheter haut)
                bid_levels = bid_levels[:1] if bid_levels else []
                bid_reduce_flags = bid_reduce_flags[:len(bid_levels)]
                bid_tags = bid_tags[:len(bid_levels)]
            else:
                # Prix baisse: on garde les bids, réduit les asks (évite de vendre bas)
                ask_levels = ask_levels[:1] if ask_levels else []
                ask_reduce_flags = ask_reduce_flags[:len(ask_levels)]
                ask_tags = ask_tags[:len(ask_levels)]
        
        elif need_urgent_exit or need_quick_exit:
            # Reduce inventory with passive quotes only.
            reduce_quote = self._compute_passive_reduce_quote(
                best_bid,
                best_ask,
                mid_price,
                position,
                urgent=bool(need_urgent_exit or loss_reduce_mode),
            )
            if exit_side == "SELL":
                bid_levels = []
                bid_reduce_flags = []
                bid_tags = []
                ask_levels = [(reduce_quote[1], reduce_quote[2])] if reduce_quote else ask_levels[:1]
                ask_reduce_flags = [True] * len(ask_levels)
                ask_tags = ["risk_exit"] * len(ask_levels)
            else:
                ask_levels = []
                ask_reduce_flags = []
                ask_tags = []
                bid_levels = [(reduce_quote[1], reduce_quote[2])] if reduce_quote else bid_levels[:1]
                bid_reduce_flags = [True] * len(bid_levels)
                bid_tags = ["risk_exit"] * len(bid_levels)
        elif ema_cross_reduce_only:
            self.last_ema_exit_bar_ts = indicator_snapshot.last_bar_ts
            if ema_exit_side == "SELL":
                bid_levels = []
                bid_reduce_flags = []
                bid_tags = []
                ask_levels = ask_levels[:max(1, min(3, len(ask_levels)))]
                ask_reduce_flags = [True] * len(ask_levels)
                ask_tags = ["ema_exit"] * len(ask_levels)
            else:
                ask_levels = []
                ask_reduce_flags = []
                ask_tags = []
                bid_levels = bid_levels[:max(1, min(3, len(bid_levels)))]
                bid_reduce_flags = [True] * len(bid_levels)
                bid_tags = ["ema_exit"] * len(bid_levels)
        else:
            # === NORMAL MODE: Check position limits ===
            can_buy = self.should_quote_buy(position, mid_price)
            can_sell = self.should_quote_sell(position, mid_price)
            
            # === ANTI-ADVERSE SELECTION ===
            if ANTI_ADVERSE_SELECTION and unrealized_pnl < -0.02:
                if position > 0.001:
                    can_buy = False
                    self.last_status = f"🛡️ Anti-Loss: No more bids"
                elif position < -0.001:
                    can_sell = False
                    self.last_status = f"🛡️ Anti-Loss: No more asks"
            
            if not can_buy:
                bid_levels = []
                bid_reduce_flags = []
                bid_tags = []
            if not can_sell:
                ask_levels = []
                ask_reduce_flags = []
                ask_tags = []

        # === EXECUTE: Cancel & Replace ===
        quote_tif = "Alo" if POST_ONLY_MM_QUOTES else "Gtc"
        if STRICT_POST_ONLY_FLOW:
            quote_tif = "Alo"

        (
            bid_levels,
            ask_levels,
            bid_reduce_flags,
            ask_reduce_flags,
            bid_tags,
            ask_tags,
            preflight_dropped,
        ) = self._preflight_quote_levels(
            bid_levels,
            ask_levels,
            bid_reduce_flags,
            ask_reduce_flags,
            bid_tags,
            ask_tags,
            best_bid,
            best_ask,
            quote_tif,
        )

        risk_refresh = bool(need_emergency_exit or need_urgent_exit or need_quick_exit or ema_cross_reduce_only)
        now_ts = time.time()
        if now_ts < self.reject_pause_until and not risk_refresh:
            wait_s = max(0.0, self.reject_pause_until - now_ts)
            reason = f" | {self.last_reject_reason}" if self.last_reject_reason else ""
            self.orders_need_sync = False
            self.last_status = f"⏸️ Reject cooldown {wait_s:.1f}s{reason}"
            return (mid_price, position)

        if not bid_levels and not ask_levels:
            self.orders_need_sync = False
            self.last_target_bid_count = 0
            self.last_target_ask_count = 0
            self.empty_quote_pause_until = now_ts + EMPTY_QUOTE_COOLDOWN_SEC
            drop_txt = f" | preflight drop {preflight_dropped}" if preflight_dropped else ""
            if has_orders and quote_age >= self.MIN_REQUOTE_INTERVAL:
                self.cancel_all_orders()
                self.last_quote_update_ts = now_ts
                self.orders_anchor_price = mid_price
                self.orders_anchor_position = position
                self.orders_best_bid = None
                self.orders_best_ask = None
                self.last_status = f"{ws_icon} 🧯 No safe quote - pulled orders{drop_txt}"
            else:
                self.last_status = f"{ws_icon} ⏸️ No safe quote{drop_txt}"
            return (mid_price, position)

        if (
            has_orders and
            not risk_refresh and
            self._desired_quote_matches_open_orders(
                bid_levels,
                ask_levels,
                bid_reduce_flags,
                ask_reduce_flags,
                bid_tags,
                ask_tags,
            )
        ):
            self.orders_need_sync = False
            self.current_refresh_interval = REFRESH_SPEED_SLOW
            drop_txt = f" | preflight drop {preflight_dropped}" if preflight_dropped else ""
            self.last_status = (
                f"{ws_icon} 💎 KEEP quotes @ ${mid_price:.2f} | "
                f"same within {self.SAME_QUOTE_TOLERANCE_TICKS:g}t | Drift: {price_drift_ticks}t{drop_txt}"
            )
            return (mid_price, position)

        self.cancel_all_orders()
        self.buy_oids, self.sell_oids = self.place_order_batch(
            bid_levels,
            ask_levels,
            tif=quote_tif,
            bid_reduce_only_flags=bid_reduce_flags,
            ask_reduce_only_flags=ask_reduce_flags,
            bid_tags=bid_tags,
            ask_tags=ask_tags,
        )
        now_ts = time.time()
        self.last_quote_update_ts = now_ts
        self.last_replace_mid = mid_price
        self.replace_events.append(now_ts)
        self.orders_need_sync = False
        self.last_target_bid_count = len(bid_levels)
        self.last_target_ask_count = len(ask_levels)
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
        spread_info = f"S:{self.current_spread_ticks}t" if hasattr(self, 'current_spread_ticks') else ""
        drop_txt = f" drop:{preflight_dropped}" if preflight_dropped else ""
        if n_bids == 0 and n_asks == 0 and time.time() < self.reject_pause_until:
            wait_s = max(0.0, self.reject_pause_until - time.time())
            self.last_status = f"⏸️ Rejected → pause {wait_s:.1f}s | {self.last_reject_reason[:70]}"
        else:
            self.last_status = f"{ws_icon} {refresh_reason} → {n_bids}B/{n_asks}A @ ${mid_price:.2f} {spread_info} {quote_tif}{drop_txt}"
        
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
    SYMBOL = TOKEN_CONFIG["symbol"]
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
