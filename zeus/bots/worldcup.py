#!/usr/bin/env python3
"""
Hyperliquid HIP spread-volume bot.

Default mode is DRY-RUN. Add --live only when you really want to send orders.

What it does:
  - discovers HIP-4 World Cup outcome markets from country names or slugs
  - subscribes to real-time BBO over the Hyperliquid SDK websocket
  - buys the NO spot token with market-ish IOC when flat and spread < 0.020%
  - sells the same NO spot token with market-ish IOC when inventory is positive
  - shows a terminal dashboard with PnL, volume, PnL/min and PnL per $1M

Install deps if needed:
  pip install hyperliquid-python-sdk eth-account rich python-dotenv
"""

from __future__ import annotations

from zeus import PROJECT_ROOT

import argparse
import math
import os
import signal
import sys
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests


class RateLimited(Exception):
    pass

try:
    from hyperliquid.exchange import Exchange
    from hyperliquid.info import Info
    from hyperliquid.utils import constants
except Exception as exc:  # pragma: no cover - startup guard
    raise SystemExit(
        "Missing Hyperliquid SDK. Run: pip install hyperliquid-python-sdk eth-account\n"
        f"Import error: {exc}"
    )

try:
    from rich import box
    from rich.align import Align
    from rich.console import Console, Group
    from rich.live import Live
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
except Exception as exc:  # pragma: no cover - startup guard
    raise SystemExit(f"Missing Rich dashboard dependency. Run: pip install rich\nImport error: {exc}")


DEFAULT_KEYWORDS = "WORLD,CUP,2026,CHAMPION,WINNER"
DEFAULT_MAX_SPREAD_PCT = 0.020  # percent. 0.020% = 2 bps.
DEFAULT_WORLDCUP_COUNTRIES = "ALL"
WORLDCUP_DESCRIPTION_NEEDLE = "officially declared the 2026 FIFA World Cup champion"
OUTCOME_ASSET_OFFSET = 100_000_000


def load_dotenv_light(path: str = str(PROJECT_ROOT / ".env")) -> None:
    """Tiny .env loader so the bot does not require python-dotenv."""
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                os.environ.setdefault(key, value)
    except Exception:
        return


def parse_csv(value: str) -> List[str]:
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def slugify(value: str) -> str:
    text = unicodedata.normalize("NFKD", value)
    text = text.encode("ascii", "ignore").decode("ascii")
    chars = []
    prev_dash = False
    for ch in text.lower():
        if ch.isalnum():
            chars.append(ch)
            prev_dash = False
        elif not prev_dash:
            chars.append("-")
            prev_dash = True
    return "".join(chars).strip("-")


def worldcup_slug(country: str, side: str) -> str:
    return f"2026-world-cup-champion-{slugify(country)}-{side.lower()}"


def parse_worldcup_slug(symbol: str) -> Tuple[Optional[str], Optional[str]]:
    raw = symbol.strip().lower()
    prefix = "2026-world-cup-champion-"
    if not raw.startswith(prefix):
        return None, None
    tail = raw[len(prefix):]
    for side in ("yes", "no"):
        suffix = f"-{side}"
        if tail.endswith(suffix):
            return tail[: -len(suffix)], side
    return tail, None


def outcome_coin(outcome_id: int, side: int) -> str:
    return f"#{outcome_id * 10 + side}"


def outcome_balance_coin(outcome_id: int, side: int) -> str:
    return f"+{outcome_id * 10 + side}"


def outcome_asset_id(outcome_id: int, side: int) -> int:
    return OUTCOME_ASSET_OFFSET + outcome_id * 10 + side


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def now_ms() -> int:
    return int(time.time() * 1000)


def fmt_usd(value: float) -> str:
    if abs(value) >= 1_000_000:
        return f"${value / 1_000_000:.2f}M"
    if abs(value) >= 1_000:
        return f"${value / 1_000:.1f}k"
    if abs(value) >= 10:
        return f"${value:.2f}"
    return f"${value:.4f}"


def fmt_age(ts: float) -> str:
    if ts <= 0:
        return "--"
    age = max(0.0, time.time() - ts)
    if age < 1:
        return f"{age * 1000:.0f}ms"
    return f"{age:.1f}s"


def coin_matches(symbol: str, coin: Optional[str]) -> bool:
    if not symbol or not coin:
        return False
    return coin == symbol or symbol.endswith(":" + coin) or coin.endswith(":" + symbol)


def round_size(size: float, sz_decimals: int) -> float:
    if size <= 0:
        return 0.0
    factor = 10 ** max(0, sz_decimals)
    return math.floor(size * factor) / factor


def round_px_wire(px: float) -> float:
    """Keep floats acceptable to hyperliquid.utils.signing.float_to_wire."""
    return float(f"{px:.8f}")


def round_outcome_buy_px(px: float) -> float:
    return min(0.999, max(0.001, math.ceil(px * 10_000.0 - 1e-9) / 10_000.0))


def round_outcome_sell_px(px: float) -> float:
    return min(0.999, max(0.001, math.floor(px * 10_000.0 + 1e-9) / 10_000.0))


@dataclass
class BotConfig:
    base_url: str
    perp_dex: str
    live: bool
    symbols: List[str]
    keywords: List[str]
    max_symbols: int
    order_usd: float
    min_order_usd: float
    close_min_order_usd: float
    max_spread_pct: float
    max_position_usd: float
    min_action_interval_sec: float
    stale_sec: float
    refresh_hz: float
    stop_loss_usd: float
    take_profit_usd: float
    rest_bbo_hz: float
    rest_workers: int
    rest_max_per_sweep: int
    rest_429_backoff_sec: float
    market_slippage_bps: float
    market_cycle_cooldown_sec: float
    account_sync_sec: float
    account_address: str
    private_key: str
    scan_only: bool
    worldcup: bool
    countries: List[str]
    outcome_side: str
    exec_mode: str = "taker"   # "taker" = IOC croise le spread | "maker" = limit post-only
    maker_offset_pct: float = 0.0  # % derrière le touch, même convention que max_spread_pct (0.02 = 0.02%)

    @property
    def max_spread_bps(self) -> float:
        return self.max_spread_pct * 100.0

    @property
    def is_maker(self) -> bool:
        return self.exec_mode == "maker"


@dataclass
class MarketSpec:
    symbol: str
    sz_decimals: int
    max_leverage: float = 0.0
    display_name: str = ""
    outcome_id: Optional[int] = None
    outcome_side: Optional[str] = None
    balance_coin: Optional[str] = None


@dataclass
class MarketState:
    spec: MarketSpec
    bid: float = 0.0
    ask: float = 0.0
    mid: float = 0.0
    spread_bps: float = math.inf
    last_book_ts: float = 0.0
    bid_oid: Optional[int] = None
    ask_oid: Optional[int] = None
    quoted_bid: float = 0.0
    quoted_ask: float = 0.0
    position: float = 0.0
    unrealized_pnl: float = 0.0
    margin_used: float = 0.0
    last_action: str = "waiting"
    last_error: str = ""
    last_order_ts: float = 0.0
    last_rest_book_ts: float = 0.0
    book_source: str = "-"
    pending_until: float = 0.0
    pending_side: str = ""

    @property
    def has_bbo(self) -> bool:
        return self.bid > 0 and self.ask > 0 and self.ask >= self.bid

    @property
    def position_usd(self) -> float:
        return self.position * self.mid if self.mid > 0 else 0.0

    def update_bbo(self, bid: float, ask: float, ts_ms: Optional[int] = None) -> None:
        self.bid = bid
        self.ask = ask
        self.mid = (bid + ask) / 2.0 if bid > 0 and ask > 0 else 0.0
        self.spread_bps = ((ask - bid) / self.mid * 10_000.0) if self.mid > 0 else math.inf
        self.last_book_ts = (ts_ms / 1000.0) if ts_ms else time.time()


@dataclass
class SessionStats:
    start_ts: float = field(default_factory=time.time)
    gross_closed_pnl: float = 0.0
    fees_paid: float = 0.0
    volume_usd: float = 0.0
    fills: int = 0
    processed_fills: set = field(default_factory=set)
    best_net_pnl: float = 0.0
    rejected: int = 0
    orders_sent: int = 0
    cancels_sent: int = 0

    @property
    def net_pnl(self) -> float:
        # Hyperliquid fee can be negative for rebates.
        return self.gross_closed_pnl - self.fees_paid

    @property
    def elapsed_min(self) -> float:
        return max((time.time() - self.start_ts) / 60.0, 1e-9)

    @property
    def pnl_per_min(self) -> float:
        return self.net_pnl / self.elapsed_min

    @property
    def volume_per_min(self) -> float:
        return self.volume_usd / self.elapsed_min

    @property
    def fills_per_min(self) -> float:
        return self.fills / self.elapsed_min

    @property
    def pnl_per_million(self) -> float:
        return self.net_pnl / (self.volume_usd / 1_000_000.0) if self.volume_usd > 0 else 0.0


class HyperliquidSpreadBot:
    def __init__(self, cfg: BotConfig):
        self.cfg = cfg
        self.console = Console()
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.running = True
        self.last_global_action_ts = 0.0
        self.last_ws_restart_ts = 0.0
        self.last_rest_sweep_ts = 0.0
        self.rest_backoff_until = 0.0
        self.status = "init"
        self.stats = SessionStats()

        perp_dexs = [cfg.perp_dex] if cfg.perp_dex else None
        self.info = Info(cfg.base_url, skip_ws=True, perp_dexs=perp_dexs, timeout=10)
        self.outcome_meta: Optional[Dict[str, Any]] = None
        if cfg.worldcup:
            self.outcome_meta = self.info.post("/info", {"type": "outcomeMeta"})
            self.patch_outcome_assets(self.info, self.outcome_meta)
        self.exchange: Optional[Exchange] = None
        self.wallet = None
        if cfg.live:
            self._init_exchange(perp_dexs)

        self.specs = self.discover_markets()
        self.states: Dict[str, MarketState] = {spec.symbol: MarketState(spec) for spec in self.specs}
        self.ws_info: Optional[Info] = None

        try:
            signal.signal(signal.SIGINT, self.shutdown)
            signal.signal(signal.SIGTERM, self.shutdown)
        except ValueError:
            pass

    def _init_exchange(self, perp_dexs: Optional[List[str]]) -> None:
        if not self.cfg.private_key or not self.cfg.account_address:
            raise SystemExit(
                "--live requires credentials. Set HL_OUTCOME_PRIVATE_KEY and "
                "HL_OUTCOME_ACCOUNT_ADDRESS, or PRIVATE_KEY_OUTCOME and "
                "WALLET_ADDRESS_OUTCOME in .env."
            )
        try:
            import eth_account

            self.wallet = eth_account.Account.from_key(self.cfg.private_key)
            self.exchange = Exchange(
                self.wallet,
                base_url=self.cfg.base_url,
                account_address=self.cfg.account_address,
                perp_dexs=perp_dexs,
                timeout=10,
            )
            if self.cfg.worldcup and self.outcome_meta:
                self.patch_outcome_assets(self.exchange.info, self.outcome_meta)
        except Exception as exc:
            raise SystemExit(f"Could not initialize live Exchange: {exc}") from exc

    @staticmethod
    def patch_outcome_assets(info: Info, outcome_meta: Dict[str, Any]) -> None:
        """Teach the current SDK about HIP-4 #N outcome coins."""
        for outcome in outcome_meta.get("outcomes", []) if isinstance(outcome_meta, dict) else []:
            outcome_id = int(outcome.get("outcome", 0) or 0)
            if outcome_id <= 0:
                continue
            sides = outcome.get("sideSpecs") or [{"name": "Yes"}, {"name": "No"}]
            for side in range(len(sides)):
                coin = outcome_coin(outcome_id, side)
                asset_id = outcome_asset_id(outcome_id, side)
                info.coin_to_asset[coin] = asset_id
                info.name_to_coin[coin] = coin
                info.asset_to_sz_decimals[asset_id] = 0

    def liquidate_all_positions(self) -> None:
        """Vend au market (IOC) tout l'inventaire de tokens NO restant.
        Appelé au stop: le bot ne doit rien laisser en position derrière lui."""
        if not self.cfg.live or self.exchange is None:
            return
        self.sync_account()
        slip = max(0.0, self.cfg.market_slippage_bps) / 10_000.0
        with self.lock:
            states = list(self.states.values())
        for st in states:
            sell_size = float(math.floor(max(0.0, st.position)))
            if sell_size < 1.0:
                continue
            ref = st.bid if st.bid > 0 else (st.mid if st.mid > 0 else 0.0)
            if ref <= 0:
                st.last_action = "liquidate: pas de bid, position conservée"
                continue
            px = round_outcome_sell_px(ref * (1.0 - max(slip, 0.002)))
            try:
                self.exchange.order(
                    name=st.spec.symbol,
                    is_buy=False,
                    sz=sell_size,
                    limit_px=round_px_wire(px),
                    order_type={"limit": {"tif": "Ioc"}},
                    reduce_only=False,
                )
                st.last_action = f"liquidate: SELL {sell_size:g} @ {px:g}"
            except Exception as exc:
                st.last_error = str(exc)[:120]
                st.last_action = "liquidate error"

    def shutdown(self, *_: Any) -> None:
        self.running = False
        self.wake.set()
        if self.cfg.live:
            self.cancel_all("shutdown")
            # Stop = flat: on ferme tout l'inventaire au market, pas seulement
            # les ordres. Best effort (book vide -> la position reste, loggé).
            try:
                self.liquidate_all_positions()
            except Exception:
                pass
        try:
            if self.ws_info is not None:
                self.ws_info.disconnect_websocket()
        except Exception:
            pass

    # ------------------------------------------------------------------ setup
    def discover_markets(self) -> List[MarketSpec]:
        if self.cfg.worldcup:
            return self.discover_worldcup_markets()

        meta = self.info.meta(self.cfg.perp_dex)
        universe = meta.get("universe", []) if isinstance(meta, dict) else []
        by_name: Dict[str, Dict[str, Any]] = {
            str(item.get("name", "")).upper(): item
            for item in universe
            if item.get("name") and not item.get("isDelisted", False)
        }

        selected: List[Dict[str, Any]] = []
        if self.cfg.symbols:
            for symbol in self.cfg.symbols:
                key = symbol.upper()
                item = by_name.get(key)
                if item is None and self.cfg.perp_dex and ":" not in key:
                    item = by_name.get(f"{self.cfg.perp_dex}:{key}".upper())
                if item is None:
                    selected.append({"name": symbol, "szDecimals": 4, "maxLeverage": 0})
                else:
                    selected.append(item)
        else:
            keywords = [kw.upper() for kw in self.cfg.keywords if kw]
            scored: List[Tuple[int, Dict[str, Any]]] = []
            for item in universe:
                name = str(item.get("name", ""))
                if item.get("isDelisted", False):
                    continue
                base_name = name.split(":", 1)[-1]
                haystack = base_name.upper().replace("-", "_")
                score = sum(1 for kw in keywords if kw in haystack)
                if score > 0 or not keywords:
                    scored.append((score, item))
            scored.sort(key=lambda pair: (-pair[0], str(pair[1].get("name", ""))))
            selected = [item for _, item in scored[: self.cfg.max_symbols]]

        specs = [
            MarketSpec(
                symbol=str(item.get("name")),
                sz_decimals=int(item.get("szDecimals", 4) or 4),
                max_leverage=safe_float(item.get("maxLeverage"), 0.0),
                display_name=str(item.get("name")),
            )
            for item in selected
            if item.get("name")
        ]

        if not specs:
            sample = ", ".join(str(item.get("name")) for item in universe[:30])
            raise SystemExit(
                "No matching markets found. Use --symbols with exact symbols, or loosen --keywords.\n"
                f"Dex={self.cfg.perp_dex or 'main'} keywords={','.join(self.cfg.keywords)}\n"
                f"First markets seen: {sample}"
            )
        return specs

    def discover_worldcup_markets(self) -> List[MarketSpec]:
        if not self.outcome_meta:
            self.outcome_meta = self.info.post("/info", {"type": "outcomeMeta"})
            self.patch_outcome_assets(self.info, self.outcome_meta)

        outcomes = self.outcome_meta.get("outcomes", []) if isinstance(self.outcome_meta, dict) else []

        # The dashboard catalog renders template names (sports tournaments,
        # awards, price markets...).  Fall back to the raw outcomeMeta rows if
        # the catalog module is unavailable, so explicit #N coins still work.
        catalog: List[Dict[str, Any]] = []
        try:
            from zeus import outcomes as outcome_mod
            catalog = outcome_mod.catalog_snapshot()
        except Exception:
            catalog = []
        if not catalog:
            catalog = [
                {
                    "id": int(outcome.get("outcome")),
                    "name": str(outcome.get("name") or outcome.get("outcome")),
                    "key": outcome_coin(int(outcome.get("outcome")), 1),
                }
                for outcome in outcomes
                if str(outcome.get("venue") or "").lower() == "out"
                and str(outcome.get("outcome") or "").isdigit()
            ]

        by_slug: Dict[str, Dict[str, Any]] = {}
        for row in catalog:
            for label in (row.get("name"), row.get("question_title")):
                if label:
                    by_slug.setdefault(slugify(str(label)), row)

        requested: List[Tuple[str, str]] = []
        if self.cfg.symbols:
            for raw_symbol in self.cfg.symbols:
                raw = raw_symbol.strip()
                if raw.startswith("#"):
                    encoding = int(raw[1:])
                    outcome_id, side_idx = divmod(encoding, 10)
                    side_name = "yes" if side_idx == 0 else "no"
                    requested.append((f"#{encoding}", side_name))
                    continue
                country_slug, slug_side = parse_worldcup_slug(raw)
                if country_slug:
                    requested.append((country_slug, slug_side or self.cfg.outcome_side))
                else:
                    requested.append((slugify(raw), self.cfg.outcome_side))
        else:
            countries = [str(country).strip() for country in self.cfg.countries if str(country).strip()]
            if not countries or any(country.lower() == "all" for country in countries):
                side_name = "yes" if str(self.cfg.outcome_side).lower() == "yes" else "no"
                side_idx = 0 if side_name == "yes" else 1
                requested = [
                    (outcome_coin(int(row["id"]), side_idx), side_name)
                    for row in catalog
                    if row.get("id") is not None
                ]
            else:
                requested = [(slugify(country), self.cfg.outcome_side) for country in countries]

        specs: List[MarketSpec] = []
        missing: List[str] = []
        for country_slug, side_name in requested:
            side_name = side_name.lower()
            side_idx = 1 if side_name == "no" else 0
            if country_slug.startswith("#"):
                symbol = country_slug
                display = symbol
                outcome_id = int(symbol[1:]) // 10
                try:
                    from zeus import outcomes as outcome_mod
                    resolved = outcome_mod.resolve_market(symbol)
                    if resolved:
                        display = str(resolved.get("name") or symbol)
                except Exception:
                    pass
                specs.append(
                    MarketSpec(
                        symbol=symbol,
                        sz_decimals=0,
                        display_name=display,
                        outcome_id=outcome_id,
                        outcome_side=side_name,
                        balance_coin=outcome_balance_coin(outcome_id, side_idx),
                    )
                )
                continue

            row = by_slug.get(country_slug)
            if not row:
                missing.append(country_slug)
                continue
            outcome_id = int(row["id"])
            symbol = outcome_coin(outcome_id, side_idx)
            display_name = str(row.get("name") or country_slug)
            specs.append(
                MarketSpec(
                    symbol=symbol,
                    sz_decimals=0,
                    max_leverage=1.0,
                    display_name=display_name,
                    outcome_id=outcome_id,
                    outcome_side=side_name,
                    balance_coin=outcome_balance_coin(outcome_id, side_idx),
                )
            )

        if missing:
            available = ", ".join(sorted(by_slug.keys())[:80])
            self.console.print(
                "Some Outcome markets were not found in outcomeMeta: "
                f"{', '.join(missing)}\nAvailable slugs: {available}",
                style="yellow",
            )
        if not specs:
            raise SystemExit("No HIP-4 Outcome markets selected.")
        return specs

    def scan_table(self) -> Table:
        table = Table(title="Discovered markets", box=box.SIMPLE_HEAVY)
        table.add_column("Market", style="bold cyan")
        table.add_column("API coin", style="magenta")
        table.add_column("Side", justify="center")
        table.add_column("Size dec", justify="right")
        for spec in self.specs:
            table.add_row(
                spec.display_name or spec.symbol,
                spec.symbol,
                spec.outcome_side or "-",
                str(spec.sz_decimals),
            )
        return table

    def start_ws(self) -> None:
        perp_dexs = [self.cfg.perp_dex] if self.cfg.perp_dex else None
        self.status = "connecting ws"
        self.ws_info = Info(self.cfg.base_url, skip_ws=False, perp_dexs=perp_dexs, timeout=10)
        if self.cfg.worldcup and self.outcome_meta:
            self.patch_outcome_assets(self.ws_info, self.outcome_meta)
        for symbol in self.states:
            self.ws_info.subscribe({"type": "bbo", "coin": symbol}, self.on_bbo)
            self.ws_info.subscribe({"type": "l2Book", "coin": symbol}, self.on_l2_book)
        if self.cfg.account_address:
            self.ws_info.subscribe({"type": "userEvents", "user": self.cfg.account_address}, self.on_user_events)
            self.ws_info.subscribe({"type": "userFills", "user": self.cfg.account_address}, self.on_user_fills)
            self.ws_info.subscribe({"type": "orderUpdates", "user": self.cfg.account_address}, self.on_order_updates)
        self.status = "ws live"

    def restart_ws_if_needed(self) -> None:
        now = time.time()
        newest = max((st.last_book_ts for st in self.states.values()), default=0.0)
        if newest <= 0 or now - newest <= self.cfg.stale_sec * 3:
            return
        if now - self.last_ws_restart_ts < 10:
            return
        self.last_ws_restart_ts = now
        self.status = "ws stale, reconnecting"
        try:
            if self.ws_info is not None:
                self.ws_info.disconnect_websocket()
        except Exception:
            pass
        try:
            self.start_ws()
        except Exception as exc:
            self.status = f"ws reconnect failed: {str(exc)[:80]}"

    # -------------------------------------------------------------- callbacks
    def on_bbo(self, data: Dict[str, Any]) -> None:
        try:
            payload = data.get("data", data)
            symbol = str(payload.get("coin", ""))
            bbo = payload.get("bbo", [])
            if len(bbo) != 2 or bbo[0] is None or bbo[1] is None:
                return
            bid = safe_float(bbo[0].get("px"))
            ask = safe_float(bbo[1].get("px"))
            if bid <= 0 or ask <= 0:
                return
            with self.lock:
                for local_symbol, st in self.states.items():
                    if coin_matches(local_symbol, symbol):
                        st.update_bbo(bid, ask, int(payload.get("time", 0) or 0))
                        st.book_source = "ws"
                        break
            self.wake.set()
        except Exception:
            return

    def on_l2_book(self, data: Dict[str, Any]) -> None:
        try:
            payload = data.get("data", data)
            symbol = str(payload.get("coin", ""))
            levels = payload.get("levels", [])
            if len(levels) < 2 or not levels[0] or not levels[1]:
                return
            bid = safe_float(levels[0][0].get("px"))
            ask = safe_float(levels[1][0].get("px"))
            if bid <= 0 or ask <= 0:
                return
            with self.lock:
                for local_symbol, st in self.states.items():
                    if coin_matches(local_symbol, symbol):
                        st.update_bbo(bid, ask, int(payload.get("time", 0) or 0))
                        st.book_source = "l2"
                        if st.last_error.startswith("REST bbo"):
                            st.last_error = ""
                        break
            self.wake.set()
        except Exception:
            return

    def refresh_rest_bbos(self, force: bool = False) -> None:
        if self.cfg.rest_bbo_hz <= 0:
            return
        now = time.time()
        if not force and now < self.rest_backoff_until:
            return
        min_gap = 1.0 / max(self.cfg.rest_bbo_hz, 0.1)
        if not force and now - self.last_rest_sweep_ts < min_gap:
            return
        self.last_rest_sweep_ts = now
        stale_after = min(self.cfg.stale_sec * 0.5, min_gap)
        with self.lock:
            candidates = []
            for symbol, st in self.states.items():
                age = now - st.last_book_ts if st.last_book_ts > 0 else 999.0
                rest_age = now - st.last_rest_book_ts if st.last_rest_book_ts > 0 else 999.0
                should_refresh = (
                    force
                    or not st.has_bbo
                    or age >= stale_after
                    or rest_age >= max(self.cfg.stale_sec, min_gap * 4)
                )
                if not should_refresh:
                    continue
                priority = 0.0
                if st.position >= 1.0:
                    priority += 100.0
                if not st.has_bbo:
                    priority += 80.0
                if age >= self.cfg.stale_sec:
                    priority += 60.0
                if math.isfinite(st.spread_bps) and st.spread_bps < self.cfg.max_spread_bps * 2.0:
                    priority += 30.0
                priority += min(20.0, age)
                candidates.append((priority, rest_age, symbol))
            candidates.sort(reverse=True)
            symbols = [symbol for _, _, symbol in candidates[: self.cfg.rest_max_per_sweep]]
        if not symbols:
            return

        workers = max(1, min(self.cfg.rest_workers, len(symbols)))
        updated = 0
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(self.fetch_rest_bbo, symbol): symbol for symbol in symbols}
            for future in as_completed(futures):
                symbol = futures[future]
                try:
                    bid, ask = future.result()
                except RateLimited as exc:
                    self.rest_backoff_until = time.time() + self.cfg.rest_429_backoff_sec
                    with self.lock:
                        if symbol in self.states:
                            self.states[symbol].last_error = f"REST bbo: {exc}"
                    continue
                except Exception as exc:
                    with self.lock:
                        if symbol in self.states:
                            self.states[symbol].last_error = f"REST bbo: {str(exc)[:80]}"
                    continue
                if bid <= 0 or ask <= 0:
                    continue
                with self.lock:
                    st = self.states.get(symbol)
                    if st is None:
                        continue
                    st.update_bbo(bid, ask)
                    st.last_rest_book_ts = time.time()
                    st.book_source = "rest"
                    if st.last_error.startswith("REST bbo"):
                        st.last_error = ""
                    updated += 1
        if updated:
            self.status = f"ws+rest live ({updated} books)"
            self.wake.set()

    def fetch_rest_bbo(self, symbol: str) -> Tuple[float, float]:
        response = requests.post(
            self.cfg.base_url + "/info",
            json={"type": "l2Book", "coin": symbol},
            timeout=1.5,
        )
        if response.status_code == 429:
            raise RateLimited("429 backoff")
        response.raise_for_status()
        book = response.json()
        levels = book.get("levels", [[], []]) if isinstance(book, dict) else [[], []]
        bids = levels[0] if len(levels) > 0 else []
        asks = levels[1] if len(levels) > 1 else []
        bid = safe_float(bids[0].get("px")) if bids else 0.0
        ask = safe_float(asks[0].get("px")) if asks else 0.0
        return bid, ask

    def on_user_fills(self, data: Dict[str, Any]) -> None:
        try:
            payload = data.get("data", data)
            fills = payload.get("fills", []) if isinstance(payload, dict) else []
            if payload.get("isSnapshot"):
                with self.lock:
                    for fill in fills:
                        tid = str(fill.get("tid", ""))
                        if tid:
                            self.stats.processed_fills.add(tid)
                return
            self._record_fills(fills)
        except Exception:
            return

    def on_user_events(self, data: Dict[str, Any]) -> None:
        try:
            payload = data.get("data", data)
            if not isinstance(payload, dict):
                return
            if "clearinghouseState" in payload:
                self._record_positions(payload["clearinghouseState"])
            self._record_fills(payload.get("fills", []))
        except Exception:
            return

    def on_order_updates(self, data: Dict[str, Any]) -> None:
        try:
            payload = data.get("data", data)
            updates = payload if isinstance(payload, list) else payload.get("orders", [])
            terminal = ("fill", "cancel", "reject", "margin", "badalo", "reduceonly", "expired")
            changed = False
            with self.lock:
                for update in updates or []:
                    order = update.get("order", update) if isinstance(update, dict) else {}
                    coin = order.get("coin")
                    oid = int(order.get("oid", update.get("oid", 0)) or 0)
                    if not oid:
                        continue
                    status = str(update.get("status", "")).lower().replace("_", "")
                    for st in self.states.values():
                        if not coin_matches(st.spec.symbol, coin):
                            continue
                        if any(marker in status for marker in terminal):
                            if st.bid_oid == oid:
                                st.bid_oid = None
                                changed = True
                            if st.ask_oid == oid:
                                st.ask_oid = None
                                changed = True
                            if "reject" in status:
                                self.stats.rejected += 1
                                st.last_error = status
                        elif status in {"open", "resting"}:
                            side = str(order.get("side", "")).upper()
                            if side == "B":
                                st.bid_oid = oid
                            else:
                                st.ask_oid = oid
                            changed = True
            if changed:
                self.wake.set()
        except Exception:
            return

    def _record_positions(self, clearinghouse_state: Dict[str, Any]) -> None:
        with self.lock:
            found = {symbol: False for symbol in self.states}
            for entry in clearinghouse_state.get("assetPositions", []):
                pos = entry.get("position", {})
                coin = pos.get("coin")
                for symbol, st in self.states.items():
                    if coin_matches(symbol, coin):
                        st.position = safe_float(pos.get("szi"))
                        st.unrealized_pnl = safe_float(pos.get("unrealizedPnl"))
                        st.margin_used = safe_float(pos.get("marginUsed"))
                        found[symbol] = True
            for symbol, was_found in found.items():
                if not was_found:
                    self.states[symbol].position = 0.0
                    self.states[symbol].unrealized_pnl = 0.0
                    self.states[symbol].margin_used = 0.0

    def _record_fills(self, fills: Iterable[Dict[str, Any]]) -> None:
        changed = False
        with self.lock:
            for fill in fills or []:
                coin = fill.get("coin")
                state = next((st for st in self.states.values() if coin_matches(st.spec.symbol, coin)), None)
                if state is None:
                    continue
                tid = str(fill.get("tid") or fill.get("hash") or "")
                if tid and tid in self.stats.processed_fills:
                    continue
                if tid:
                    self.stats.processed_fills.add(tid)

                px = safe_float(fill.get("px"))
                sz = safe_float(fill.get("sz"))
                fee = safe_float(fill.get("fee"))
                closed = safe_float(fill.get("closedPnl"))
                self.stats.volume_usd += px * sz
                self.stats.fees_paid += fee
                self.stats.gross_closed_pnl += closed
                self.stats.fills += 1
                self.stats.best_net_pnl = max(self.stats.best_net_pnl, self.stats.net_pnl)

                side = str(fill.get("side", "")).upper()
                direction = str(fill.get("dir", "")).lower()
                if side == "B" or direction in ("open long", "close short"):
                    state.position = round_size(state.position + sz, state.spec.sz_decimals)
                elif side in ("A", "S") or direction in ("open short", "close long"):
                    state.position = round_size(state.position - sz, state.spec.sz_decimals)
                state.last_action = f"fill {side or '?'} {sz:g} @ {px:g}"
                changed = True
        if changed:
            self.wake.set()

    # -------------------------------------------------------------- sync/risk
    def sync_account(self) -> None:
        if not self.cfg.account_address:
            return
        if self.cfg.worldcup:
            try:
                spot_state = self.info.spot_user_state(self.cfg.account_address)
                balances = {
                    str(balance.get("coin")): balance
                    for balance in spot_state.get("balances", [])
                }
                with self.lock:
                    for state in self.states.values():
                        if not state.spec.balance_coin:
                            continue
                        bal = balances.get(state.spec.balance_coin, {})
                        total = safe_float(bal.get("total"))
                        hold = safe_float(bal.get("hold"))
                        state.position = total - hold
                        state.margin_used = 0.0
            except Exception:
                pass
        else:
            try:
                st = self.info.user_state(self.cfg.account_address, dex=self.cfg.perp_dex)
                self._record_positions(st)
            except Exception:
                pass
        try:
            dex = "" if self.cfg.worldcup else self.cfg.perp_dex
            orders = self.info.open_orders(self.cfg.account_address, dex=dex)
        except Exception:
            orders = []
        with self.lock:
            for state in self.states.values():
                state.bid_oid = None
                state.ask_oid = None
            for order in orders or []:
                coin = order.get("coin")
                oid = int(order.get("oid", 0) or 0)
                side = str(order.get("side", "")).upper()
                for state in self.states.values():
                    if coin_matches(state.spec.symbol, coin):
                        if side == "B":
                            state.bid_oid = oid
                        else:
                            state.ask_oid = oid

    def risk_block_reason(self, st: MarketState, closing_inventory: bool = False) -> Optional[str]:
        if self.stats.net_pnl <= -abs(self.cfg.stop_loss_usd):
            return f"session stop loss {fmt_usd(self.stats.net_pnl)}"
        if self.cfg.take_profit_usd > 0 and self.stats.net_pnl >= self.cfg.take_profit_usd:
            return f"session take profit {fmt_usd(self.stats.net_pnl)}"
        if not st.has_bbo:
            return "waiting bbo"
        if time.time() - st.last_book_ts > self.cfg.stale_sec:
            return f"stale feed {fmt_age(st.last_book_ts)}"
        # Le gate de spread protège le TAKER (croiser un spread large coûte cher).
        # En maker on ne croise jamais: un spread large est au contraire le
        # moment où la quote passive capture le plus.
        if not self.cfg.is_maker and not closing_inventory and st.spread_bps >= self.cfg.max_spread_bps:
            return f"spread {st.spread_bps:.3f}bps >= {self.cfg.max_spread_bps:.3f}bps"
        return None

    def quote_size(self, st: MarketState) -> float:
        if st.mid <= 0:
            return 0.0
        raw = self.cfg.order_usd / st.mid
        size = round_size(raw, st.spec.sz_decimals)
        if st.spec.sz_decimals == 0:
            size = max(1.0, math.floor(size))
        return size

    def desired_spot_no_order(self, st: MarketState) -> Tuple[Optional[Dict[str, Any]], str]:
        pos_usd = st.position_usd
        cap = abs(self.cfg.max_position_usd)
        slip = max(0.0, self.cfg.market_slippage_bps) / 10_000.0
        has_no_inventory = st.position >= 1.0
        maker = self.cfg.is_maker
        # Maker: limit post-only placé maker_offset_pct (%) DERRIÈRE le touch
        # (0 = colle au BBO). Ne croise jamais, 0 slippage, frais maker.
        # Taker: IOC qui croise avec un peu de slip.
        tif = "Alo" if maker else "Ioc"
        mode_tag = "maker" if maker else "market"
        maker_off = max(0.0, self.cfg.maker_offset_pct) / 100.0

        if has_no_inventory:
            max_sell_size = math.floor(st.position)
            sell_size = float(max_sell_size)
            if sell_size <= 0:
                return None, "sell size=0"
            if maker:
                # ceil: N bps au-dessus du best ask, jamais à travers
                px = round_outcome_buy_px(st.ask * (1.0 + maker_off))
            else:
                px = round_outcome_sell_px(st.bid * (1.0 - slip))
            return (
                {
                    "coin": st.spec.symbol,
                    "is_buy": False,
                    "sz": sell_size,
                    "limit_px": round_px_wire(px),
                    "order_type": {"limit": {"tif": tif}},
                    "reduce_only": False,
                },
                f"SELL NO {mode_tag} {sell_size:g} value ${sell_size * px:.2f}"
                + (" wide-exit" if st.spread_bps >= self.cfg.max_spread_bps else ""),
            )

        if pos_usd >= cap:
            return None, "inventory cap"
        target_usd = max(self.cfg.order_usd, self.cfg.min_order_usd)
        max_buy_usd = max(0.0, min(target_usd, cap - max(0.0, pos_usd)))
        if max_buy_usd < self.cfg.min_order_usd:
            return None, f"cap leaves {max_buy_usd:.2f}<${self.cfg.min_order_usd:.2f}"
        if maker:
            # floor: N bps sous le best bid, jamais à travers
            px = round_outcome_sell_px(st.bid * (1.0 - maker_off))
        else:
            px = round_outcome_buy_px(st.ask * (1.0 + slip))
        buy_size = float(math.ceil(self.cfg.min_order_usd / max(px, 1e-9)))
        target_size = float(math.floor(max_buy_usd / max(px, 1e-9)))
        buy_size = max(buy_size, target_size)
        if buy_size * px < self.cfg.min_order_usd:
            buy_size = float(math.ceil(self.cfg.min_order_usd / max(px, 1e-9)))
        if buy_size <= 0:
            return None, "buy size=0"
        return (
            {
                "coin": st.spec.symbol,
                "is_buy": True,
                "sz": buy_size,
                "limit_px": round_px_wire(px),
                "order_type": {"limit": {"tif": tif}},
                "reduce_only": False,
            },
            f"BUY NO {mode_tag} {buy_size:g} value ${buy_size * px:.2f}",
        )

    # ------------------------------------------------------------ order logic
    def can_send_action(self) -> bool:
        now = time.time()
        if now - self.last_global_action_ts < self.cfg.min_action_interval_sec:
            return False
        self.last_global_action_ts = now
        return True

    def cancel_all(self, reason: str, symbols: Optional[List[str]] = None) -> None:
        if not self.cfg.live or self.exchange is None:
            with self.lock:
                for st in self.states.values():
                    if symbols is None or st.spec.symbol in symbols:
                        st.last_action = f"dry cancel: {reason}"
            return
        reqs = []
        with self.lock:
            for symbol, st in self.states.items():
                if symbols is not None and symbol not in symbols:
                    continue
                for oid in (st.bid_oid, st.ask_oid):
                    if oid:
                        reqs.append({"coin": symbol, "oid": int(oid)})
                st.last_action = f"cancel: {reason}"
        if not reqs:
            return
        if not self.can_send_action():
            with self.lock:
                for symbol in symbols or self.states.keys():
                    if symbol in self.states:
                        self.states[symbol].last_action = f"cancel throttled: {reason}"
            return
        try:
            self.exchange.bulk_cancel(reqs)
            self.stats.cancels_sent += len(reqs)
            cancelled = {req["coin"]: set() for req in reqs}
            for req in reqs:
                cancelled.setdefault(req["coin"], set()).add(int(req["oid"]))
            with self.lock:
                for symbol, oids in cancelled.items():
                    st = self.states.get(symbol)
                    if st is None:
                        continue
                    if st.bid_oid in oids:
                        st.bid_oid = None
                    if st.ask_oid in oids:
                        st.ask_oid = None
        except Exception as exc:
            with self.lock:
                for symbol in symbols or self.states.keys():
                    if symbol in self.states:
                        self.states[symbol].last_error = str(exc)[:120]

    def place_or_requote(self, st: MarketState) -> None:
        closing_inventory = st.position >= 1.0
        block = self.risk_block_reason(st, closing_inventory=closing_inventory)
        if block:
            if st.bid_oid or st.ask_oid:
                self.cancel_all(block, [st.spec.symbol])
            st.last_action = block
            return

        now = time.time()
        if now < st.pending_until:
            st.last_action = f"await {st.pending_side} sync"
            return
        if now - st.last_order_ts < self.cfg.market_cycle_cooldown_sec:
            st.last_action = "cycle cooldown"
            return

        req, reason = self.desired_spot_no_order(st)
        if not req:
            st.last_action = reason
            return

        # Mode maker: gérer l'ordre limite déjà au repos AVANT d'en envoyer un
        # autre (sinon on empile des limits). Si la quote en place est déjà au
        # prix cible -> on la garde (priorité de queue). Sinon cancel, et on
        # re-place au prochain cycle une fois le cancel acquitté.
        if self.cfg.is_maker:
            same_oid = st.bid_oid if req["is_buy"] else st.ask_oid
            same_quoted = st.quoted_bid if req["is_buy"] else st.quoted_ask
            other_oid = st.ask_oid if req["is_buy"] else st.bid_oid
            target_px = float(req["limit_px"])
            if same_oid and other_oid is None and abs(float(same_quoted) - target_px) < 1e-9:
                side_txt = "BID" if req["is_buy"] else "ASK"
                st.last_action = f"maker resting {side_txt} @ {target_px:g}"
                return
            if same_oid or other_oid:
                self.cancel_all("maker requote", [st.spec.symbol])
                st.last_action = "maker requote (cancel d'abord)"
                return

        if not self.cfg.live:
            px = safe_float(req.get("limit_px"))
            sz = safe_float(req.get("sz"))
            dry_tag = "DRY MAKER" if self.cfg.is_maker else "DRY"
            if req["is_buy"]:
                st.position = round_size(st.position + sz, st.spec.sz_decimals)
                st.last_action = f"{dry_tag} BUY NO {sz:g} @ {px:g}"
            else:
                st.position = round_size(max(0.0, st.position - sz), st.spec.sz_decimals)
                st.last_action = f"{dry_tag} SELL NO {sz:g} @ {px:g}"
            st.last_order_ts = now
            self.stats.volume_usd += px * sz
            return

        order_value = float(req["sz"]) * float(req["limit_px"])
        min_live_value = self.cfg.min_order_usd if req["is_buy"] else 0.0
        if order_value < min_live_value:
            st.last_action = f"skip low value ${order_value:.2f}<${min_live_value:.2f}"
            return

        if self.exchange is None or not self.can_send_action():
            st.last_action = "global throttle"
            return

        try:
            res = self.exchange.order(
                name=req["coin"],
                is_buy=bool(req["is_buy"]),
                sz=float(req["sz"]),
                limit_px=float(req["limit_px"]),
                order_type=req["order_type"],
                reduce_only=False,
            )
            self.stats.orders_sent += 1
            st.last_order_ts = now
            st.pending_side = "BUY NO" if req["is_buy"] else "SELL NO"
            st.pending_until = now + 0.35
            self._parse_order_response(st, [req], res)
        except Exception as exc:
            st.last_error = str(exc)[:160]
            st.last_action = "order error"

    def _parse_order_response(self, st: MarketState, reqs: List[Dict[str, Any]], res: Any) -> None:
        if not isinstance(res, dict):
            st.last_action = f"unknown response {str(res)[:80]}"
            return
        if res.get("status") != "ok":
            st.last_error = str(res)[:160]
            st.last_action = "rejected"
            self.stats.rejected += 1
            return
        statuses = res.get("response", {}).get("data", {}).get("statuses", [])
        ok_count = 0
        errors: List[str] = []
        fills: List[str] = []
        for req, status in zip(reqs, statuses):
            if isinstance(status, dict) and "resting" in status:
                oid = int(status["resting"]["oid"])
                if req["is_buy"]:
                    st.bid_oid = oid
                    st.quoted_bid = req["limit_px"]
                else:
                    st.ask_oid = oid
                    st.quoted_ask = req["limit_px"]
                ok_count += 1
            elif isinstance(status, dict) and "filled" in status:
                filled = status["filled"]
                filled_sz = safe_float(filled.get("totalSz"), safe_float(req.get("sz")))
                avg_px = safe_float(filled.get("avgPx"), safe_float(req.get("limit_px")))
                side = "BUY NO" if req["is_buy"] else "SELL NO"
                fills.append(f"{side} filled {filled_sz:g} @ {avg_px:g}")
                ok_count += 1
            else:
                self.stats.rejected += 1
                errors.append(str(status)[:160])
        if len(statuses) != len(reqs):
            errors.append(f"partial ack {len(statuses)}/{len(reqs)}")
        st.last_order_ts = time.time()
        if ok_count > 0:
            st.last_error = ""
            if fills:
                st.last_action = " | ".join(fills)
            else:
                suffix = " partial" if ok_count != len(reqs) else ""
                st.last_action = f"accepted {ok_count}/{len(reqs)} orders{suffix}"
        else:
            st.last_error = errors[-1] if errors else ""
            st.last_action = f"accepted 0/{len(reqs)} orders"

    # --------------------------------------------------------------- runtime
    @staticmethod
    def _ask_float(label: str, current: float, minimum: float = 0.0) -> float:
        while True:
            raw = input(f"{label} [Enter={current:g}] : ").strip().replace(",", ".")
            if not raw:
                return current
            try:
                value = float(raw)
                if value < minimum:
                    print(f"  Nombre >= {minimum:g} attendu.")
                    continue
                return value
            except ValueError:
                print("  Nombre attendu.")

    def _launch_prompt(self) -> None:
        """Au clavier: choisir le marché, le %% de spread et la taille.
        Taker: le %% = gate d'entrée (max-spread-pct). Maker: le %% = offset
        derrière le touch. Enter partout = garder les défauts CLI/.env."""
        # JAMAIS en mode embarqué (dashboard): le bot tourne alors dans un
        # thread de bot_server et input() bloquerait le thread indéfiniment
        # (statut RUNNING mais aucun ordre). Menu = console directe uniquement.
        if threading.current_thread() is not threading.main_thread():
            return
        try:
            if not sys.stdin.isatty():
                return
        except Exception:
            return
        mode_txt = "MAKER post-only" if self.cfg.is_maker else "TAKER IOC"
        print(f"\n--- Mode {mode_txt}: marché, spread, taille ---")
        for i, spec in enumerate(self.specs, 1):
            print(f"  [{i}] {spec.display_name or spec.symbol}")
        while True:
            choice = input(f"Marché ? [1-{len(self.specs)} / nom (ex: france) / Enter=tous] : ").strip().lower()
            if not choice:
                break
            picked = None
            try:
                idx = int(choice)
                if 1 <= idx <= len(self.specs):
                    picked = self.specs[idx - 1]
            except ValueError:
                matches = [s for s in self.specs
                           if choice in (s.display_name or "").lower() or choice in s.symbol.lower()]
                if len(matches) == 1:
                    picked = matches[0]
                elif len(matches) > 1:
                    print(f"  Ambigu ({len(matches)} marchés) — précise.")
                    continue
            if picked is None:
                print("  Introuvable — numéro, nom partiel, ou Enter pour tous.")
                continue
            self.specs = [picked]
            with self.lock:
                self.states = {picked.symbol: MarketState(picked)}
            print(f"  -> {picked.display_name or picked.symbol} uniquement")
            break

        if self.cfg.is_maker:
            self.cfg.maker_offset_pct = self._ask_float(
                "Spread maker en % derrière le touch (ex 0.02 = 0.02%)",
                self.cfg.maker_offset_pct, minimum=0.0)
        else:
            self.cfg.max_spread_pct = self._ask_float(
                "Spread max en % pour entrer (gate taker, ex 0.02 = 0.02%)",
                self.cfg.max_spread_pct, minimum=0.0)

        value = self._ask_float("Taille d'ordre en $ (minimum exchange 10)",
                                self.cfg.order_usd, minimum=10.0)
        self.cfg.order_usd = value
        # le sizing exige buy_value >= min_order_usd: l'aligner sur la taille
        # choisie sinon une taille < 45 (défaut) serait silencieusement bloquée
        self.cfg.min_order_usd = max(10.0, min(self.cfg.min_order_usd, value))

        pct_txt = (f"offset {self.cfg.maker_offset_pct:g}%" if self.cfg.is_maker
                   else f"spread< {self.cfg.max_spread_pct:g}%")
        print(f"{mode_txt} | {pct_txt} | ordre ${self.cfg.order_usd:g} | "
              f"{len(self.specs)} marché(s)\n")

    def _trading_loop(self, refresh: float, live=None) -> None:
        """Boucle de trading. `live` = dashboard Rich (console directe) ou
        None (headless: lancé par bot_server, l'affichage est le web)."""
        last_sync = 0.0
        while self.running:
            if time.time() - last_sync > self.cfg.account_sync_sec:
                self.sync_account()
                last_sync = time.time()

            self.restart_ws_if_needed()
            self.refresh_rest_bbos()
            with self.lock:
                states = list(self.states.values())
            for st in states:
                self.place_or_requote(st)

            if live is not None:
                live.update(self.render())
            self.wake.wait(refresh)
            self.wake.clear()

    def run(self) -> None:
        # Headless quand lancé par bot_server (thread non principal): pas de
        # dashboard Rich dans le terminal (deux Live simultanés = clignotement),
        # les stats passent par le dashboard web.
        headless = threading.current_thread() is not threading.main_thread()
        if not headless:
            self.console.print(self.scan_table())
        if self.cfg.scan_only:
            return
        self._launch_prompt()

        self.sync_account()
        self.start_ws()
        self.refresh_rest_bbos(force=True)
        refresh = max(0.05, 1.0 / max(self.cfg.refresh_hz, 0.1))

        if headless:
            self._trading_loop(refresh, live=None)
        else:
            with Live(self.render(), console=self.console, screen=True, refresh_per_second=self.cfg.refresh_hz) as live:
                self._trading_loop(refresh, live=live)

        self.shutdown()

    def export_dashboard_stats(self, lock_timeout: float = 0.3) -> Optional[Dict[str, Any]]:
        """Return a JSON-safe stats snapshot for the web dashboard."""
        acquired = self.lock.acquire(timeout=lock_timeout)
        if not acquired:
            return None
        try:
            stats = self.stats
            total_position_usd = 0.0
            mid_prices: List[float] = []
            spreads: List[float] = []
            for state in self.states.values():
                total_position_usd += abs(state.position_usd)
                if state.mid and state.mid > 0:
                    mid_prices.append(state.mid)
                spread = state.spread_bps
                if spread is not None and math.isfinite(spread) and spread > 0:
                    spreads.append(spread)

            avg_mid = sum(mid_prices) / len(mid_prices) if mid_prices else 0.0
            avg_spread = sum(spreads) / len(spreads) if spreads else 0.0

            return {
                "duration_sec": stats.elapsed_min * 60,
                "volume_usd": stats.volume_usd,
                "net_pnl": stats.net_pnl,
                "fees": stats.fees_paid,
                "cost_per_1m": stats.pnl_per_million,
                "fills": stats.fills,
                "mid_price": avg_mid,
                "spread_bps": avg_spread,
                "position_usd": total_position_usd,
                "active_markets": len(mid_prices),
            }
        finally:
            self.lock.release()

    # ------------------------------------------------------------- dashboard
    def render(self) -> Group:
        acquired = self.lock.acquire(timeout=1.0)
        if not acquired:
            return Group(Text("Dashboard lock busy — retrying…"))
        try:
            states = list(self.states.values())
            stats = SessionStats(
                start_ts=self.stats.start_ts,
                gross_closed_pnl=self.stats.gross_closed_pnl,
                fees_paid=self.stats.fees_paid,
                volume_usd=self.stats.volume_usd,
                fills=self.stats.fills,
                processed_fills=set(),
                best_net_pnl=self.stats.best_net_pnl,
                rejected=self.stats.rejected,
                orders_sent=self.stats.orders_sent,
                cancels_sent=self.stats.cancels_sent,
            )
            status = self.status
            cfg = self.cfg
        finally:
            self.lock.release()

        mode = "[bold red]LIVE[/]" if cfg.live else "[bold yellow]DRY-RUN[/]"
        exec_txt = (f"[bold green]MAKER post-only +{cfg.maker_offset_pct:g}%[/]"
                    if cfg.is_maker else "[bold magenta]TAKER IOC[/]")
        spread_txt = ("spread gate off (maker)" if cfg.is_maker
                      else f"spread< {cfg.max_spread_pct:.3f}% ({cfg.max_spread_bps:.3f}bps)")
        title = Text.from_markup(
            f"Hyperliquid HIP Spread Bot  {mode}  {exec_txt}  {spread_txt}"
        )

        runtime = int(time.time() - stats.start_ts)
        hh, rem = divmod(runtime, 3600)
        mm, ss = divmod(rem, 60)
        runtime_s = f"{hh}h {mm:02d}m {ss:02d}s" if hh else f"{mm}m {ss:02d}s"

        net_color = "green" if stats.net_pnl >= 0 else "red"
        top = Table.grid(expand=True)
        top.add_column(ratio=1)
        top.add_column(ratio=1)
        top.add_column(ratio=1)
        top.add_column(ratio=1)
        top.add_row(
            f"[dim]Runtime[/]\n[bold]{runtime_s}[/]",
            f"[dim]Net PnL[/]\n[{net_color}]{fmt_usd(stats.net_pnl)}[/]",
            f"[dim]Volume[/]\n[bold cyan]{fmt_usd(stats.volume_usd)}[/]",
            f"[dim]PnL/min[/]\n[bold]{fmt_usd(stats.pnl_per_min)}[/]",
        )
        top.add_row(
            f"[dim]PnL/$1M[/]\n[bold]{fmt_usd(stats.pnl_per_million)}[/]",
            f"[dim]Vol/min[/]\n[bold cyan]{fmt_usd(stats.volume_per_min)}[/]",
            f"[dim]Fills/min[/]\n[bold]{stats.fills_per_min:.2f}[/]",
            f"[dim]Orders/Cancels[/]\n[bold]{stats.orders_sent}/{stats.cancels_sent}[/]",
        )

        table = Table(box=box.SIMPLE_HEAVY, expand=True, show_lines=False)
        table.add_column("Market", style="bold cyan", no_wrap=True)
        table.add_column("Coin", style="magenta", no_wrap=True)
        table.add_column("Bid / Ask", justify="right")
        table.add_column("Spread", justify="right")
        table.add_column("Age", justify="right")
        table.add_column("Src", justify="center")
        table.add_column("Pos", justify="right")
        table.add_column("Oids", justify="center")
        table.add_column("Action", overflow="fold")

        for st in states:
            spread_color = "green" if st.spread_bps < cfg.max_spread_bps else "red"
            pos_color = "green" if st.position > 0 else "red" if st.position < 0 else "white"
            bidask = f"{st.bid:g} / {st.ask:g}" if st.has_bbo else "-- / --"
            spread = "--" if not math.isfinite(st.spread_bps) else f"[{spread_color}]{st.spread_bps:.3f}bps[/]"
            oids = f"{'B' if st.bid_oid else '-'}{'A' if st.ask_oid else '-'}"
            action = st.last_action
            if st.last_error:
                action += f" | [red]{st.last_error[:80]}[/]"
            table.add_row(
                st.spec.display_name or st.spec.symbol,
                st.spec.symbol,
                bidask,
                spread,
                fmt_age(st.last_book_ts),
                st.book_source,
                f"[{pos_color}]{st.position:+g}[/]\n[dim]{fmt_usd(st.position_usd)}[/]",
                oids,
                action,
            )

        footer = (
            f"Status: {status} | Mode: {'HIP-4 Outcome' if cfg.worldcup else 'perp/spot'} | "
            f"Dex: {cfg.perp_dex or 'main'} | "
            f"Order: {fmt_usd(cfg.order_usd)} per leg | "
            f"Cap: {fmt_usd(cfg.max_position_usd)} per market | "
            f"Started: {datetime.fromtimestamp(stats.start_ts).strftime('%H:%M:%S')}"
        )

        return Group(
            Align.center(title),
            Panel(top, box=box.ROUNDED),
            table,
            Panel(footer, box=box.ROUNDED, style="dim"),
        )


def build_config(argv: Optional[List[str]] = None) -> BotConfig:
    load_dotenv_light()

    parser = argparse.ArgumentParser(description="Hyperliquid HIP spread-volume bot")
    parser.add_argument("--live", action="store_true", help="send real orders; default is dry-run")
    parser.add_argument("--testnet", action="store_true", help="use Hyperliquid testnet")
    parser.add_argument("--dex", default=os.getenv("HL_PERP_DEX", "xyz"), help="perp dex, e.g. xyz or empty")
    parser.add_argument("--symbols", default=os.getenv("HL_SYMBOLS", ""), help="comma-separated exact symbols, #N coins, or Outcome slugs")
    parser.add_argument("--keywords", default=os.getenv("HL_KEYWORDS", DEFAULT_KEYWORDS), help="comma keywords")
    parser.add_argument("--worldcup", action="store_true", help="force HIP-4 Outcome mode")
    parser.add_argument("--legacy-scan", action="store_true", help="use old perp/HIP-3 keyword discovery instead of HIP-4 Outcome")
    parser.add_argument("--countries", default=os.getenv("HL_WORLDCUP_COUNTRIES", DEFAULT_WORLDCUP_COUNTRIES))
    parser.add_argument("--outcome-side", choices=("yes", "no"), default=os.getenv("HL_OUTCOME_SIDE", "no"))
    parser.add_argument("--max-symbols", type=int, default=int(os.getenv("HL_MAX_SYMBOLS", "8")))
    parser.add_argument("--order-usd", type=float, default=float(os.getenv("HL_ORDER_USD", "45")))
    parser.add_argument("--min-order-usd", type=float, default=float(os.getenv("HL_MIN_ORDER_USD", "45")))
    parser.add_argument("--close-min-order-usd", type=float, default=float(os.getenv("HL_CLOSE_MIN_ORDER_USD", "0")))
    parser.add_argument("--max-spread-pct", type=float, default=float(os.getenv("HL_MAX_SPREAD_PCT", str(DEFAULT_MAX_SPREAD_PCT))))
    parser.add_argument("--max-position-usd", type=float, default=float(os.getenv("HL_MAX_POSITION_USD", "250")))
    parser.add_argument("--min-action-interval", type=float, default=float(os.getenv("HL_MIN_ACTION_INTERVAL", "0.03")))
    parser.add_argument("--stale-sec", type=float, default=float(os.getenv("HL_STALE_SEC", "1.2")))
    parser.add_argument("--refresh-hz", type=float, default=float(os.getenv("HL_REFRESH_HZ", "12")))
    parser.add_argument("--rest-bbo-hz", type=float, default=float(os.getenv("HL_REST_BBO_HZ", "4")))
    parser.add_argument("--rest-workers", type=int, default=int(os.getenv("HL_REST_WORKERS", "24")))
    parser.add_argument("--rest-max-per-sweep", type=int, default=int(os.getenv("HL_REST_MAX_PER_SWEEP", "2")))
    parser.add_argument("--rest-429-backoff-sec", type=float, default=float(os.getenv("HL_REST_429_BACKOFF_SEC", "2.5")))
    parser.add_argument("--exec", dest="exec_mode", choices=("taker", "maker"),
                        default=os.getenv("HL_EXEC_MODE", "taker"),
                        help="taker = IOC qui croise le spread (défaut) | maker = limit post-only (0 slippage, frais maker)")
    parser.add_argument("--maker-offset-pct", type=float,
                        default=float(os.getenv("HL_MAKER_OFFSET_PCT", "0")),
                        help="mode maker: %% derrière le touch, même convention que --max-spread-pct "
                             "(ex 0.02 = 0.02%% = 2 bps; 0 = colle au BBO). Aussi choisissable au lancement.")
    parser.add_argument("--market-slippage-bps", type=float, default=float(os.getenv("HL_MARKET_SLIPPAGE_BPS", "5")))
    parser.add_argument("--market-cycle-cooldown", type=float, default=float(os.getenv("HL_MARKET_CYCLE_COOLDOWN", "0.12")))
    parser.add_argument("--account-sync-sec", type=float, default=float(os.getenv("HL_ACCOUNT_SYNC_SEC", "2")))
    parser.add_argument("--stop-loss-usd", type=float, default=float(os.getenv("HL_STOP_LOSS_USD", "50")))
    parser.add_argument("--take-profit-usd", type=float, default=float(os.getenv("HL_TAKE_PROFIT_USD", "0")))
    parser.add_argument(
        "--account",
        default=(
            os.getenv("HL_OUTCOME_ACCOUNT_ADDRESS")
            or os.getenv("WALLET_ADDRESS_OUTCOME")
            or os.getenv("HL_ACCOUNT_ADDRESS")
            or os.getenv("WALLET_ADDRESS", "")
        ),
    )
    parser.add_argument("--scan-only", action="store_true", help="only list matching markets")
    args = parser.parse_args(argv)

    private_key = (
        os.getenv("HL_OUTCOME_PRIVATE_KEY")
        or os.getenv("PRIVATE_KEY_OUTCOME")
        or os.getenv("HL_AGENT_PRIVATE_KEY")
        or os.getenv("PRIVATE_KEY", "")
    )
    base_url = constants.TESTNET_API_URL if args.testnet else constants.MAINNET_API_URL
    dex = args.dex.strip()
    symbols = parse_csv(args.symbols)
    looks_hip4 = any(
        symbol.strip().startswith("#") or "world-cup-champion" in symbol.lower()
        for symbol in symbols
    )
    worldcup = bool(args.worldcup or looks_hip4 or (not args.legacy_scan and not symbols))
    if worldcup:
        # HIP-4 must never silently sign with the general trading wallet.
        account_address = (
            os.getenv("HL_OUTCOME_ACCOUNT_ADDRESS")
            or os.getenv("WALLET_ADDRESS_OUTCOME", "")
        ).strip()
        private_key = (
            os.getenv("HL_OUTCOME_PRIVATE_KEY")
            or os.getenv("PRIVATE_KEY_OUTCOME", "")
        ).strip()
    else:
        account_address = args.account.strip()

    return BotConfig(
        base_url=base_url,
        perp_dex=dex,
        live=bool(args.live),
        symbols=symbols,
        keywords=parse_csv(args.keywords),
        max_symbols=max(1, args.max_symbols),
        order_usd=max(1.0, args.order_usd),
        min_order_usd=max(10.0, args.min_order_usd),
        close_min_order_usd=max(0.0, args.close_min_order_usd),
        max_spread_pct=max(0.0, args.max_spread_pct),
        max_position_usd=max(1.0, args.max_position_usd),
        min_action_interval_sec=max(0.01, args.min_action_interval),
        stale_sec=max(0.5, args.stale_sec),
        refresh_hz=max(0.5, args.refresh_hz),
        stop_loss_usd=max(1.0, args.stop_loss_usd),
        take_profit_usd=max(0.0, args.take_profit_usd),
        rest_bbo_hz=max(0.0, args.rest_bbo_hz),
        rest_workers=max(1, args.rest_workers),
        rest_max_per_sweep=max(1, args.rest_max_per_sweep),
        rest_429_backoff_sec=max(0.5, args.rest_429_backoff_sec),
        market_slippage_bps=max(0.0, args.market_slippage_bps),
        market_cycle_cooldown_sec=max(0.02, args.market_cycle_cooldown),
        account_sync_sec=max(0.5, args.account_sync_sec),
        account_address=account_address,
        private_key=private_key.strip(),
        scan_only=bool(args.scan_only),
        worldcup=worldcup,
        countries=parse_csv(args.countries),
        outcome_side=args.outcome_side,
        exec_mode=str(args.exec_mode).strip().lower(),
        maker_offset_pct=max(0.0, args.maker_offset_pct),
    )


def main(argv: Optional[List[str]] = None) -> int:
    cfg = build_config(argv)
    bot = HyperliquidSpreadBot(cfg)
    bot.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
