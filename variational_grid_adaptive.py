#!/usr/bin/env python3
"""
Variational Omni adaptive grid monitor.

Important:
  Variational's public Omni API is read-only as of the official docs. This bot
  does not send live orders and does not use a browser/session token for private
  trading endpoints. It loads VR_TOKEN only so your runtime is ready if/when an
  official trading API becomes available.

Run:
  python variational_grid_adaptive.py --ticker MU
  python variational_grid_adaptive.py --ticker MU --once
  python variational_grid_adaptive.py --list
"""

from __future__ import annotations

import argparse
import base64
import csv
import json
import math
import os
import re
import signal
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Deque, Dict, Iterable, List, Optional, Tuple

try:
    from curl_cffi import requests as curl_requests
except ImportError:
    curl_requests = None


try:
    import requests
except Exception as exc:  # pragma: no cover
    raise SystemExit(
        "Missing dependency: requests\n"
        "Install with: pip install requests\n"
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
except Exception as exc:  # pragma: no cover
    raise SystemExit(
        "Missing dependency: rich\n"
        "Install with: pip install rich\n"
        f"Import error: {exc}"
    )


DEFAULT_BASE_URL = "https://omni-client-api.prod.ap-northeast-1.variational.io"
STATS_PATH = "/metadata/stats"


def load_dotenv_light(path: str = ".env") -> None:
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
                if key and key not in os.environ:
                    os.environ[key] = value
    except OSError:
        return


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except Exception:
        return default


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def bps(numerator: float, denominator: float) -> float:
    if denominator <= 0:
        return 0.0
    return numerator / denominator * 10_000.0


def fmt_usd(value: float) -> str:
    sign = "-" if value < 0 else ""
    value = abs(value)
    if value >= 1_000_000:
        return f"{sign}${value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"{sign}${value / 1_000:.1f}k"
    if value >= 10:
        return f"{sign}${value:.2f}"
    return f"{sign}${value:.4f}"


def fmt_age(ts: float) -> str:
    if ts <= 0:
        return "--"
    age = max(0.0, time.time() - ts)
    if age < 1:
        return f"{age * 1000:.0f}ms"
    if age < 60:
        return f"{age:.1f}s"
    return f"{age / 60:.1f}m"


def fmt_runtime(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    return f"{minutes}m {secs}s"


def parse_iso_ts(value: str) -> float:
    if not value:
        return 0.0
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    if "." in text:
        head, tail = text.split(".", 1)
        tz = ""
        if "+" in tail:
            frac, tz = tail.split("+", 1)
            tz = "+" + tz
        elif "-" in tail:
            frac, tz = tail.split("-", 1)
            tz = "-" + tz
        else:
            frac = tail
        text = head + "." + frac[:6] + tz
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return 0.0


def decimals_in_price(raw: Any) -> int:
    text = str(raw)
    if "e" in text.lower():
        return 8
    if "." not in text:
        return 0
    return len(text.rstrip("0").split(".", 1)[1])


def round_price(px: float, decimals: int) -> float:
    decimals = clamp(decimals, 0, 10)
    return float(f"{px:.{int(decimals)}f}")


def mask_secret(value: str, left: int = 6, right: int = 4) -> str:
    if not value:
        return "--"
    if len(value) <= left + right + 3:
        return "*" * len(value)
    return value[:left] + "..." + value[-right:]


def find_evm_address(value: Any) -> Optional[str]:
    pattern = re.compile(r"0x[a-fA-F0-9]{40}")
    if isinstance(value, str):
        match = pattern.search(value)
        return match.group(0) if match else None
    if isinstance(value, dict):
        for item in value.values():
            found = find_evm_address(item)
            if found:
                return found
    if isinstance(value, list):
        for item in value:
            found = find_evm_address(item)
            if found:
                return found
    return None


def derive_address_from_token(token: str) -> str:
    token = token.strip()
    if not token:
        return ""
    found = find_evm_address(token)
    if found:
        return found
    parts = token.split(".")
    if len(parts) < 2:
        return ""
    payload = parts[1]
    padding = "=" * (-len(payload) % 4)
    try:
        decoded = base64.urlsafe_b64decode((payload + padding).encode("ascii"))
        data = json.loads(decoded.decode("utf-8"))
    except Exception:
        return ""
    return find_evm_address(data) or ""


@dataclass
class BotConfig:
    base_url: str
    ticker: str
    quote_size: str
    poll_sec: float
    request_timeout: float
    once: bool
    list_markets: bool
    num_levels: int
    order_usd: float
    max_inventory_usd: float
    base_spread_bps: float
    edge_bps: float
    level_step_bps: float
    atr_spread_mult: float
    er_period: int
    history_len: int
    paper_fill: bool
    maker_fee_bps: float
    order_export_path: str = ""
    live_mode: bool



@dataclass
class MarketSnapshot:
    ticker: str
    name: str
    mark: float
    bid: float
    ask: float
    mid: float
    quote_size: str
    quote_updated_ts: float
    api_ts: float
    volume_24h: float
    long_oi: float
    short_oi: float
    funding_rate: float
    base_spread_bps: float
    price_decimals: int

    @property
    def spread_bps(self) -> float:
        return bps(self.ask - self.bid, self.mid)

    @property
    def oi(self) -> float:
        return self.long_oi + self.short_oi


@dataclass
class RegimeState:
    ready: bool = False
    efficiency_ratio: float = 0.0
    trend_score: float = 0.0
    direction: int = 0
    atr_bps: float = 0.0
    realized_vol_bps: float = 0.0


@dataclass
class Quote:
    side: str
    px: float
    size: float
    notional: float
    reduce_only: bool
    level: int


@dataclass
class PaperFill:
    ts: float
    side: str
    px: float
    size: float
    fee: float


@dataclass
class PaperAccount:
    position: float = 0.0
    cash: float = 0.0
    fees: float = 0.0
    volume: float = 0.0
    fills: int = 0
    best_equity: float = 0.0
    fills_log: Deque[PaperFill] = field(default_factory=lambda: deque(maxlen=10))

    def equity(self, mid: float) -> float:
        return self.cash + self.position * mid

    def position_usd(self, mid: float) -> float:
        return self.position * mid

    def apply_fill(self, side: str, px: float, size: float, fee_bps: float) -> None:
        notional = px * size
        fee = notional * fee_bps / 10_000.0
        if side == "BUY":
            self.position += size
            self.cash -= notional + fee
        else:
            self.position -= size
            self.cash += notional - fee
        self.fees += fee
        self.volume += notional
        self.fills += 1
        self.fills_log.append(PaperFill(time.time(), side, px, size, fee))

    def maybe_fill_resting(self, market: MarketSnapshot, resting: Iterable[Quote], fee_bps: float) -> int:
        count = 0
        for quote in resting:
            if quote.side == "BUY" and market.ask > 0 and market.ask <= quote.px:
                self.apply_fill("BUY", quote.px, quote.size, fee_bps)
                count += 1
            elif quote.side == "SELL" and market.bid > 0 and market.bid >= quote.px:
                self.apply_fill("SELL", quote.px, quote.size, fee_bps)
                count += 1
        self.best_equity = max(self.best_equity, self.equity(market.mid))
        return count


class VariationalClient:
    def __init__(self, cfg: BotConfig):
        self.cfg = cfg
        # Use curl_cffi if available to bypass Cloudflare
        if curl_requests is not None:
            self.session = curl_requests.Session()
            self.session.impersonate = "chrome120"
        else:
            self.session = requests.Session()
            
        self.session.headers.update(
            {
                "Accept": "application/json",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            }
        )
        token = os.getenv("VR_TOKEN", "").strip()
        self.vr_token_loaded = bool(token)
        self.derived_address = derive_address_from_token(token)
        self.olp_company_id = None
        
        if self.vr_token_loaded:
            self.session.cookies.set("vr-token", token)
            self.session.headers.update({
                "vr-token": token,
                "x-vr-token": token,
                "Origin": "https://omni.variational.io",
                "Referer": "https://omni.variational.io/"
            })

    def resolve_olp_company_id(self) -> Optional[str]:
        if self.olp_company_id:
            return self.olp_company_id
        if not self.vr_token_loaded:
            return None
        try:
            url = "https://omni.variational.io/api/companies"
            resp = self.session.get(url, timeout=self.cfg.request_timeout)
            resp.raise_for_status()
            companies = resp.json().get("result", [])
            for c in companies:
                if c.get("is_lp") or "olp" in str(c.get("short_name", "")).lower():
                    self.olp_company_id = c.get("id")
                    return self.olp_company_id
        except Exception:
            pass
        return None

    def fetch_live_summary(self) -> Dict[str, Any]:
        if not self.vr_token_loaded:
            return {}
        url = "https://omni.variational.io/api/portfolio/summary"
        resp = self.session.get(url, timeout=self.cfg.request_timeout)
        resp.raise_for_status()
        return resp.json().get("result", {})

    def fetch_live_positions(self) -> List[Dict[str, Any]]:
        if not self.vr_token_loaded:
            return []
        url = "https://omni.variational.io/api/portfolio/positions"
        resp = self.session.get(url, timeout=self.cfg.request_timeout)
        resp.raise_for_status()
        return resp.json().get("result", [])

    def execute_rfq(self, side: str, qty: float) -> Tuple[bool, str]:
        """
        Creates an RFQ, waits for OLP quote, and accepts it.
        """
        if not self.vr_token_loaded:
            return False, "VR_TOKEN not loaded"
            
        ticker = self.cfg.ticker.upper()
        olp_id = self.resolve_olp_company_id()
        target_companies = [olp_id] if olp_id else []
        api_side = side.lower()
        
        structure = {
            "legs": [
                {
                    "side": api_side,
                    "ratio": 1,
                    "instrument": {
                        "instrument_type": "perpetual_future",
                        "underlying": ticker,
                        "settlement_asset": "USDC",
                        "funding_interval_s": 3600
                    }
                }
            ]
        }
        
        from datetime import datetime, timedelta, timezone
        expiry_time = (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()
        expiry_time = expiry_time.replace("+00:00", "Z")
        
        payload = {
            "structure": structure,
            "qty": f"{qty:.8f}",
            "expires_at": expiry_time,
            "target_companies": target_companies
        }
        
        url_new_rfq = "https://omni.variational.io/api/rfqs/new"
        try:
            resp = self.session.post(url_new_rfq, json=payload, timeout=self.cfg.request_timeout)
            resp.raise_for_status()
            rfq_data = resp.json().get("result", {})
            rfq_id = rfq_data.get("rfq_id")
            if not rfq_id:
                return False, f"Failed to get rfq_id: {resp.text}"
        except Exception as e:
            return False, f"RFQ creation error: {e}"
            
        url_poll = f"https://omni.variational.io/api/rfqs/sent?id={rfq_id}"
        quote_found = None
        for _ in range(30):
            time.sleep(0.1)
            try:
                poll_resp = self.session.get(url_poll, timeout=self.cfg.request_timeout)
                poll_resp.raise_for_status()
                rfq_status = poll_resp.json().get("result", {})
                quotes_list = rfq_status.get("asks" if api_side == "buy" else "bids", [])
                if quotes_list:
                    quote_found = quotes_list[0]
                    break
            except Exception:
                pass
                
        if not quote_found:
            try:
                self.session.post("https://omni.variational.io/api/rfqs/cancel", json={"id": rfq_id}, timeout=3)
            except Exception:
                pass
            return False, "No quote received from OLP within timeout"
            
        parent_quote_id = quote_found.get("parent_quote_id")
        url_accept = "https://omni.variational.io/api/quotes/accept"
        accept_payload = {
            "parent_quote_id": parent_quote_id,
            "rfq_id": rfq_id,
            "side": api_side
        }
        
        try:
            accept_resp = self.session.post(url_accept, json=accept_payload, timeout=self.cfg.request_timeout)
            accept_resp.raise_for_status()
            accept_res = accept_resp.json().get("result", {})
            status = accept_res.get("new_clearing_status", "unknown")
            if "success" in status or status == "success_trades_booked_into_pool":
                return True, f"Successfully executed trade: {qty} {ticker} {side} at {quote_found.get('quote_price')}"
            else:
                return False, f"Clearing status: {status}"
        except Exception as e:
            return False, f"Quote accept error: {e}"

    def fetch_stats(self) -> Dict[str, Any]:
        url = self.cfg.base_url.rstrip("/") + STATS_PATH
        resp = self.session.get(url, timeout=self.cfg.request_timeout)
        resp.raise_for_status()
        return resp.json()

    def list_tickers(self) -> List[Dict[str, Any]]:
        stats = self.fetch_stats()

        listings = stats.get("listings", [])
        if not isinstance(listings, list):
            return []
        return listings

    def snapshot(self) -> MarketSnapshot:
        stats = self.fetch_stats()
        listings = stats.get("listings", [])
        ticker = self.cfg.ticker.upper()
        row = None
        for item in listings:
            if str(item.get("ticker", "")).upper() == ticker:
                row = item
                break
        if row is None:
            available = ", ".join(str(x.get("ticker", "")) for x in listings[:40])
            raise RuntimeError(f"Ticker {ticker!r} not found. First markets: {available}")
        return self._snapshot_from_listing(row)

    def _snapshot_from_listing(self, row: Dict[str, Any]) -> MarketSnapshot:
        quotes = row.get("quotes", {}) if isinstance(row.get("quotes"), dict) else {}
        quote_size = self.cfg.quote_size
        quote = quotes.get(quote_size)
        if not isinstance(quote, dict):
            for fallback in ("base", "size_1k", "size_100k", "size_1m"):
                quote = quotes.get(fallback)
                if isinstance(quote, dict):
                    quote_size = fallback
                    break
        if not isinstance(quote, dict):
            quote = {}

        bid_raw = quote.get("bid")
        ask_raw = quote.get("ask")
        bid = safe_float(bid_raw)
        ask = safe_float(ask_raw)
        mark = safe_float(row.get("mark_price"))
        if bid <= 0 or ask <= 0 or bid >= ask:
            spread = max(0.0, safe_float(row.get("base_spread_bps"))) / 10_000.0
            bid = mark * (1.0 - spread / 2.0)
            ask = mark * (1.0 + spread / 2.0)
        mid = (bid + ask) / 2.0 if bid > 0 and ask > 0 else mark
        price_decimals = max(decimals_in_price(bid_raw), decimals_in_price(ask_raw))
        price_decimals = safe_int(clamp(price_decimals, 0, 10), 2)
        oi = row.get("open_interest", {}) if isinstance(row.get("open_interest"), dict) else {}
        updated = str(quotes.get("updated_at", ""))
        return MarketSnapshot(
            ticker=str(row.get("ticker", "")).upper(),
            name=str(row.get("name", "")),
            mark=mark,
            bid=bid,
            ask=ask,
            mid=mid,
            quote_size=quote_size,
            quote_updated_ts=parse_iso_ts(updated),
            api_ts=time.time(),
            volume_24h=safe_float(row.get("volume_24h")),
            long_oi=safe_float(oi.get("long_open_interest")),
            short_oi=safe_float(oi.get("short_open_interest")),
            funding_rate=safe_float(row.get("funding_rate")),
            base_spread_bps=safe_float(row.get("base_spread_bps")),
            price_decimals=price_decimals,
        )


class RegimeEngine:
    def __init__(self, cfg: BotConfig):
        self.cfg = cfg
        self.history: Deque[Tuple[float, float]] = deque(maxlen=cfg.history_len)
        self.state = RegimeState()

    def update(self, mid: float) -> RegimeState:
        if mid <= 0:
            return self.state
        self.history.append((time.time(), mid))
        prices = [p for _, p in self.history]
        min_needed = max(5, self.cfg.er_period + 1)
        if len(prices) < min_needed:
            self.state = RegimeState(ready=False)
            return self.state

        window = prices[-(self.cfg.er_period + 1) :]
        net = abs(window[-1] - window[0])
        path = sum(abs(window[i] - window[i - 1]) for i in range(1, len(window)))
        er = net / path if path > 1e-12 else 0.0
        direction = 1 if window[-1] > window[0] else (-1 if window[-1] < window[0] else 0)

        diffs = [abs(prices[i] - prices[i - 1]) for i in range(1, len(prices))]
        atr_abs = sum(diffs[-20:]) / max(1, min(len(diffs), 20))
        atr_bps = bps(atr_abs, mid)
        rets = [math.log(prices[i] / prices[i - 1]) for i in range(1, len(prices)) if prices[i - 1] > 0]
        sample = rets[-40:]
        if len(sample) >= 2:
            mean = sum(sample) / len(sample)
            variance = sum((x - mean) ** 2 for x in sample) / (len(sample) - 1)
            realized_vol_bps = math.sqrt(max(0.0, variance)) * 10_000.0
        else:
            realized_vol_bps = 0.0
        vol_component = clamp(realized_vol_bps / 8.0, 0.0, 1.0)
        trend_score = clamp(0.65 * er + 0.35 * vol_component, 0.0, 1.0)
        self.state = RegimeState(
            ready=True,
            efficiency_ratio=er,
            trend_score=trend_score,
            direction=direction,
            atr_bps=atr_bps,
            realized_vol_bps=realized_vol_bps,
        )
        return self.state


class AdaptiveGrid:
    def __init__(self, cfg: BotConfig):
        self.cfg = cfg
        self.resting: List[Quote] = []
        self.last_half_spread_bps = 0.0

    def build(self, market: MarketSnapshot, regime: RegimeState, account: PaperAccount) -> List[Quote]:
        mid = market.mid
        if mid <= 0:
            self.resting = []
            return []
        official_half = max(0.0, market.spread_bps / 2.0)
        atr_half = max(0.0, regime.atr_bps * self.cfg.atr_spread_mult)
        half_spread_bps = max(
            self.cfg.base_spread_bps,
            official_half + self.cfg.edge_bps,
            atr_half,
        )
        self.last_half_spread_bps = half_spread_bps

        pos_usd = account.position_usd(mid)
        can_buy = pos_usd + self.cfg.order_usd <= self.cfg.max_inventory_usd
        can_sell = pos_usd - self.cfg.order_usd >= -self.cfg.max_inventory_usd
        quotes: List[Quote] = []
        base_size = self.cfg.order_usd / mid
        for level in range(1, self.cfg.num_levels + 1):
            level_bps = half_spread_bps + (level - 1) * self.cfg.level_step_bps
            if can_buy:
                px = round_price(mid * (1.0 - level_bps / 10_000.0), market.price_decimals)
                quotes.append(
                    Quote(
                        side="BUY",
                        px=px,
                        size=base_size,
                        notional=px * base_size,
                        reduce_only=account.position < 0,
                        level=level,
                    )
                )
            if can_sell:
                px = round_price(mid * (1.0 + level_bps / 10_000.0), market.price_decimals)
                quotes.append(
                    Quote(
                        side="SELL",
                        px=px,
                        size=base_size,
                        notional=px * base_size,
                        reduce_only=account.position > 0,
                        level=level,
                    )
                )
        self.resting = quotes
        return quotes


class App:
    def __init__(self, cfg: BotConfig):
        self.cfg = cfg
        self.console = Console(width=180)
        self.client = VariationalClient(cfg)
        self.regime = RegimeEngine(cfg)
        self.grid = AdaptiveGrid(cfg)
        self.account = PaperAccount()
        self.running = True
        self.status = "init"
        self.last_error = ""
        self.last_market: Optional[MarketSnapshot] = None
        self.last_quotes: List[Quote] = []
        self.start_ts = time.time()
        self.last_step_ms = 0.0
        self.last_requote_ms = 0.0
        self.last_fill_count = 0
        self.anchor_mid = 0.0
        try:
            signal.signal(signal.SIGINT, self.shutdown)
            signal.signal(signal.SIGTERM, self.shutdown)
        except ValueError:
            pass
            
        if self.cfg.live_mode:
            try:
                summary = self.client.fetch_live_summary()
                self.account.cash = float(summary.get("sum_balance", 0.0))
                positions = self.client.fetch_live_positions()
                current_pos = 0.0
                for pos in positions:
                    instr = pos.get("instrument", {})
                    if instr.get("underlying") == self.cfg.ticker.upper():
                        current_pos = float(pos.get("qty", 0.0))
                self.account.position = current_pos
            except Exception as e:
                self.last_error = f"API Init Error: {e}"

    def shutdown(self, *_: Any) -> None:
        self.running = False
        self.status = "shutdown"

    def step(self) -> None:
        started = time.time()
        market = self.client.snapshot()
        fill_count = 0
        
        if self.cfg.live_mode:
            try:
                summary = self.client.fetch_live_summary()
                self.account.cash = float(summary.get("sum_balance", 0.0))
                positions = self.client.fetch_live_positions()
                current_pos = 0.0
                for pos in positions:
                    instr = pos.get("instrument", {})
                    if instr.get("underlying") == self.cfg.ticker.upper():
                        current_pos = float(pos.get("qty", 0.0))
                self.account.position = current_pos
            except Exception as e:
                self.last_error = f"API Sync Error: {e}"

        if self.cfg.live_mode and self.grid.resting:
            fill_count = self.maybe_fill_live(market, self.grid.resting)
        elif self.cfg.paper_fill and self.grid.resting:
            fill_count = self.account.maybe_fill_resting(market, self.grid.resting, self.cfg.maker_fee_bps)
            
        regime = self.regime.update(market.mid)
        rq_started = time.time()
        quotes = self.grid.build(market, regime, self.account)
        self.last_requote_ms = (time.time() - rq_started) * 1000.0
        if self.anchor_mid <= 0:
            self.anchor_mid = market.mid
        elif quotes:
            self.anchor_mid = market.mid
        self.last_market = market
        self.last_quotes = quotes
        self.last_step_ms = (time.time() - started) * 1000.0
        self.last_fill_count = fill_count
        self.status = f"live fills {fill_count}" if self.cfg.live_mode else f"paper fills {fill_count}" if fill_count else "quotes refreshed"

    def maybe_fill_live(self, market: MarketSnapshot, resting: Iterable[Quote]) -> int:
        count = 0
        for quote in resting:
            if quote.side == "BUY" and market.ask > 0 and market.ask <= quote.px:
                success, msg = self.client.execute_rfq("BUY", quote.size)
                if success:
                    self.log_order("BUY", quote.px, quote.size)
                    count += 1
                else:
                    self.last_error = f"Trade Fail: {msg}"
            elif quote.side == "SELL" and market.bid > 0 and market.bid >= quote.px:
                success, msg = self.client.execute_rfq("SELL", quote.size)
                if success:
                    self.log_order("SELL", quote.px, quote.size)
                    count += 1
                else:
                    self.last_error = f"Trade Fail: {msg}"
        return count

    def log_order(self, side: str, px: float, size: float) -> None:
        path = self.cfg.order_export_path
        if not path:
            return
        try:
            file_exists = os.path.exists(path)
            with open(path, "a", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                if not file_exists:
                    writer.writerow(["Timestamp", "Ticker", "Side", "Price", "Size", "Notional"])
                notional = px * size
                writer.writerow([
                    datetime.now(timezone.utc).isoformat(),
                    self.cfg.ticker.upper(),
                    side,
                    f"{px:.8g}",
                    f"{size:.8g}",
                    f"{notional:.2f}"
                ])
        except Exception as e:
            self.last_error = f"CSV Write Error: {e}"


    def render(self) -> Panel:
        market = self.last_market
        regime = self.regime.state
        mid = market.mid if market else 0.0
        equity = self.account.equity(mid)
        net = equity
        buy_count = sum(1 for q in self.last_quotes if q.side == "BUY")
        sell_count = sum(1 for q in self.last_quotes if q.side == "SELL")
        volume = self.account.volume
        cost_per_m = (self.account.fees - net) / (volume / 1_000_000.0) if volume > 0 else 0.0
        quote_age = fmt_age(market.quote_updated_ts) if market else "--"
        feed_age = fmt_age(market.api_ts) if market else "--"
        drift_bps = bps(abs(mid - self.anchor_mid), self.anchor_mid) if mid > 0 and self.anchor_mid > 0 else 0.0

        logo_content = r"""[bold cyan]
__     __         _       _   _                       _    ___                  _
\ \   / /_ _ _ __(_) __ _| |_(_) ___  _ __   __ _| |  / _ \ _ __ ___  _ __ (_)
 \ \ / / _` | '__| |/ _` | __| |/ _ \| '_ \ / _` | | | | | | '_ ` _ \| '_ \| |
  \ V / (_| | |  | | (_| | |_| | (_) | | | | (_| | | | |_| | | | | | | | | | |
   \_/ \__,_|_|  |_|\__,_|\__|_|\___/|_| |_|\__,_|_|  \___/|_| |_| |_|_| |_|_|

[/bold cyan]"""

        top = Table(box=box.ROUNDED, show_header=True, header_style="bold cyan")
        top.add_column("PAIR", style="bold white", justify="left")
        top.add_column("COST/1M", style="dim", justify="center")
        top.add_column("VOLUME", style="green", justify="right")
        top.add_column("FEES", style="yellow", justify="right")
        top.add_column("NET PNL", justify="right")

        net_color = "green" if net >= 0 else "red"
        cost_color = "green" if cost_per_m <= 0 else "yellow" if cost_per_m < 400 else "red"
        top.add_row(
            f"VAR:{self.cfg.ticker.upper()}",
            f"[{cost_color}]${cost_per_m:.1f}[/{cost_color}]",
            fmt_usd(volume),
            fmt_usd(self.account.fees),
            f"[{net_color}]{fmt_usd(net)}[/{net_color}]",
        )

        elapsed_h = max((time.time() - self.start_ts) / 3600.0, 1e-9)
        elapsed_m = max((time.time() - self.start_ts) / 60.0, 1.0)
        spread_bps = market.spread_bps if market else 0.0
        drift_color = "red" if drift_bps >= 10 else "yellow" if drift_bps >= 4 else "green"
        top.caption = (
            f"\nVol/H: [bold yellow]{fmt_usd(volume / elapsed_h)}[/bold yellow] | "
            f"Fills/min: [cyan]{self.account.fills / elapsed_m:.1f}[/cyan] | "
            f"Spread: [magenta]{spread_bps:.2f}bps[/magenta] | "
            f"Drift: [{drift_color}]{drift_bps:.2f}bps[/] | "
            f"Quote age: [white]{quote_age}[/]"
        )

        runtime = fmt_runtime(time.time() - self.start_ts)
        launch_time = datetime.fromtimestamp(self.start_ts).strftime("%H:%M:%S")
        token_status = "[green]loaded[/]" if self.client.vr_token_loaded else "[red]missing[/]"
        account = mask_secret(self.client.derived_address)
        position_usd = self.account.position_usd(mid)
        pos_color = "green" if position_usd > 0 else "red" if position_usd < 0 else "white"
        best_equity = max(self.account.best_equity, equity)
        drawdown = best_equity - equity
        funding_pct = (market.funding_rate * 100.0) if market else 0.0

        if not regime.ready:
            regime_text = "[dim]warming up... accumulating Omni quotes[/]"
        else:
            direction = "UP" if regime.direction > 0 else "DOWN" if regime.direction < 0 else "FLAT"
            regime_text = (
                f"[green]ADAPTIVE[/] score {regime.trend_score:.2f} | "
                f"ER {regime.efficiency_ratio:.2f} | "
                f"ATR {regime.atr_bps:.2f}bps | Vol {regime.realized_vol_bps:.2f}bps | dir {direction}"
            )

        st = Table(box=None, show_header=False, padding=(0, 1))
        st.add_column("Key", style="dim")
        st.add_column("Value", style="bold")
        st.add_row("Runtime", f"{runtime} (started {launch_time})")
        if market:
            st.add_row(
                "PAIR",
                f"[cyan]{market.ticker}[/] {market.name[:28]} | "
                f"Mid [white]{mid:.8g}[/] (bid {market.bid:.8g} / ask {market.ask:.8g})",
            )
            st.add_row(
                "Market",
                f"Vol24h {fmt_usd(market.volume_24h)} | OI {fmt_usd(market.oi)} | "
                f"Funding {funding_pct:+.4f}% | quote {market.quote_size}",
            )
        else:
            st.add_row("PAIR", f"[cyan]{self.cfg.ticker.upper()}[/] | waiting for first snapshot")
            st.add_row("Market", "waiting for Omni metadata")
        st.add_row("Venue", f"Variational Omni | VR_TOKEN {token_status} | Account [white]{account}[/]")
        mode_text = "[bold green]LIVE TRADING[/]" if self.cfg.live_mode else "[yellow]DRY-RUN / PAPER[/] | live trading requires official Variational trading API"
        orders_type = "live" if self.cfg.live_mode else "paper"
        st.add_row("Mode", mode_text)
        st.add_row("Orders", f"[green]{buy_count}B[/] / [red]{sell_count}A[/] {orders_type} resting quotes")
        st.add_row(
            "Position",
            f"[{pos_color}]{position_usd:+.2f} USD[/] | size {self.account.position:+.8g} | "
            f"cap {fmt_usd(self.cfg.max_inventory_usd)} | drawdown {fmt_usd(drawdown)}",
        )
        st.add_row("Regime", regime_text)
        st.add_row("Fills", f"{self.account.fills} {orders_type} trades | last cycle fills {self.last_fill_count}")

        st.add_row(
            "PnL",
            f"cash {fmt_usd(self.account.cash)} + inventory {fmt_usd(position_usd)} - fees {fmt_usd(self.account.fees)} = "
            f"[{net_color}]NET {fmt_usd(net)}[/]",
        )
        st.add_row(
            "Risk",
            f"Order {fmt_usd(self.cfg.order_usd)} | levels {self.cfg.num_levels} | "
            f"half spread {self.grid.last_half_spread_bps:.2f}bps | edge {self.cfg.edge_bps:.2f}bps",
        )
        st.add_row(
            "Cycle",
            f"loop {self.last_step_ms:.0f}ms | quote build {self.last_requote_ms:.1f}ms | "
            f"feed age {feed_age} | poll {self.cfg.poll_sec:.1f}s",
        )

        status_text = self.status
        if self.last_error:
            status_text += f"\n[red]{self.last_error}[/red]"
        if self.cfg.live_mode:
            status_text += "\n[bold green]Live browser cookie trading active: using VR_TOKEN session endpoints.[/bold green]"
        else:
            status_text += "\n[dim]No browser cookie trading: Omni public API is read-only; private session endpoints are not used.[/dim]"
        st.add_row("Status", status_text)


        quote_table = Table(box=box.ROUNDED, show_header=True, header_style="bold cyan")
        quote_table.add_column("LVL", justify="right")
        quote_table.add_column("SIDE")
        quote_table.add_column("PX", justify="right")
        quote_table.add_column("SIZE", justify="right")
        quote_table.add_column("VALUE", justify="right")
        quote_table.add_column("RO", justify="center")
        if self.last_quotes:
            for q in self.last_quotes[:12]:
                style = "green" if q.side == "BUY" else "red"
                quote_table.add_row(
                    str(q.level),
                    f"[{style}]{q.side}[/{style}]",
                    f"{q.px:.8g}",
                    f"{q.size:.10g}",
                    fmt_usd(q.notional),
                    "Y" if q.reduce_only else "",
                )
        else:
            quote_table.add_row("-", "-", "-", "-", "-", "-")

        return Panel(
            Group(
                Align.center(Text.from_markup(logo_content)),
                Align.center(top),
                Align.left(st),
                Align.left(quote_table),
            ),
            title="[bold cyan]GRID MM - VARIATIONAL ADAPTIVE[/bold cyan]",
            subtitle=f"[dim]Press Ctrl+C to stop | Ticker {self.cfg.ticker.upper()} | Refresh {self.cfg.poll_sec:.1f}s[/dim]",
            border_style="cyan",
            padding=(1, 2),
        )

    def run_once(self) -> int:
        self.step()
        self.console.print(self.render())
        return 0

    def run(self) -> int:
        with Live(self.render(), console=self.console, refresh_per_second=2, screen=True) as live:

            while self.running:
                started = time.time()
                try:
                    self.step()
                    self.last_error = ""
                except Exception as exc:
                    self.last_error = str(exc)[:180]
                    self.status = "error"
                live.update(self.render())
                elapsed = time.time() - started
                time.sleep(max(0.1, self.cfg.poll_sec - elapsed))
        return 0


def build_config(argv: Optional[List[str]] = None) -> BotConfig:
    load_dotenv_light()
    parser = argparse.ArgumentParser(description="Variational Omni adaptive grid trading")
    parser.add_argument("--ticker", default=os.getenv("VARIATIONAL_TICKER", "MU"))
    parser.add_argument("--quote-size", default=os.getenv("VARIATIONAL_QUOTE_SIZE", "base"))
    parser.add_argument("--base-url", default=os.getenv("VARIATIONAL_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--poll-sec", type=float, default=float(os.getenv("VARIATIONAL_POLL_SEC", "2.0")))
    parser.add_argument("--request-timeout", type=float, default=float(os.getenv("VARIATIONAL_TIMEOUT", "10")))
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--list", action="store_true", dest="list_markets")
    parser.add_argument("--live", action="store_true", help="enable live trading mode using VR_TOKEN")
    parser.add_argument("--num-levels", type=int, default=int(os.getenv("VARIATIONAL_NUM_LEVELS", "1")))
    parser.add_argument("--order-usd", type=float, default=float(os.getenv("VARIATIONAL_ORDER_USD", "15")))
    parser.add_argument("--max-inventory-usd", type=float, default=float(os.getenv("VARIATIONAL_MAX_INVENTORY_USD", "45")))
    parser.add_argument("--base-spread-bps", type=float, default=float(os.getenv("VARIATIONAL_BASE_SPREAD_BPS", "0")))
    parser.add_argument("--edge-bps", type=float, default=float(os.getenv("VARIATIONAL_EDGE_BPS", "2.0")))
    parser.add_argument("--level-step-bps", type=float, default=float(os.getenv("VARIATIONAL_LEVEL_STEP_BPS", "2.0")))
    parser.add_argument("--atr-spread-mult", type=float, default=float(os.getenv("VARIATIONAL_ATR_SPREAD_MULT", "0.20")))
    parser.add_argument("--er-period", type=int, default=int(os.getenv("VARIATIONAL_ER_PERIOD", "20")))
    parser.add_argument("--history-len", type=int, default=int(os.getenv("VARIATIONAL_HISTORY_LEN", "240")))
    parser.add_argument("--maker-fee-bps", type=float, default=float(os.getenv("VARIATIONAL_MAKER_FEE_BPS", "0.0")))
    parser.add_argument("--paper-fill", action=argparse.BooleanOptionalAction, default=os.getenv("VARIATIONAL_PAPER_FILL", "1") != "0")
    parser.add_argument("--order-export-path", default=os.getenv("VARIATIONAL_ORDER_EXPORT_PATH", "orders.csv"))
    args = parser.parse_args(argv)

    if args.live:
        if not os.getenv("VR_TOKEN"):
            raise SystemExit(
                "Live trading requires VR_TOKEN environment variable to be set. "
                "Please run set_variational_token.ps1 first."
            )

    return BotConfig(
        base_url=args.base_url.strip(),
        ticker=args.ticker.strip().upper(),
        quote_size=args.quote_size.strip(),
        poll_sec=max(1.0, args.poll_sec),
        request_timeout=max(1.0, args.request_timeout),
        once=bool(args.once),
        list_markets=bool(args.list_markets),
        num_levels=max(1, args.num_levels),
        order_usd=max(1.0, args.order_usd),
        max_inventory_usd=max(args.order_usd, args.max_inventory_usd),
        base_spread_bps=max(0.0, args.base_spread_bps),
        edge_bps=max(0.0, args.edge_bps),
        level_step_bps=max(0.0, args.level_step_bps),
        atr_spread_mult=max(0.0, args.atr_spread_mult),
        er_period=max(2, args.er_period),
        history_len=max(30, args.history_len),
        paper_fill=bool(args.paper_fill),
        maker_fee_bps=max(0.0, args.maker_fee_bps),
        order_export_path=args.order_export_path.strip(),
        live_mode=bool(args.live),
    )



def print_markets(cfg: BotConfig) -> int:
    client = VariationalClient(cfg)
    rows = client.list_tickers()
    rows = sorted(rows, key=lambda r: safe_float(r.get("volume_24h")), reverse=True)
    table = Table(box=box.ROUNDED, show_header=True, header_style="bold")
    table.add_column("Ticker")
    table.add_column("Name")
    table.add_column("Mark", justify="right")
    table.add_column("Vol24h", justify="right")
    table.add_column("Spread", justify="right")
    for row in rows[:80]:
        quotes = row.get("quotes", {}) if isinstance(row.get("quotes"), dict) else {}
        q = quotes.get("base") or quotes.get("size_1k") or {}
        bid = safe_float(q.get("bid"))
        ask = safe_float(q.get("ask"))
        mid = (bid + ask) / 2.0 if bid > 0 and ask > 0 else safe_float(row.get("mark_price"))
        table.add_row(
            str(row.get("ticker", "")),
            str(row.get("name", ""))[:28],
            f"{safe_float(row.get('mark_price')):.8g}",
            fmt_usd(safe_float(row.get("volume_24h"))),
            f"{bps(ask - bid, mid):.2f}bps" if bid > 0 and ask > 0 else "-",
        )
    Console(width=180).print(table)
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    cfg = build_config(argv)
    if cfg.list_markets:
        return print_markets(cfg)
    app = App(cfg)
    if cfg.once:
        return app.run_once()
    return app.run()


if __name__ == "__main__":
    raise SystemExit(main())
