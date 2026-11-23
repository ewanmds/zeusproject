"""Bounded retries for idempotent reads. Never wrap order submissions.

One policy for catalogs, scanners and account reads: classify the failure,
respect Retry-After, back off with jitter, and make every wait interruptible by
Stop. Order submissions keep their own durable-reconciliation path.
"""
import random
import math
import threading
import time
from email.utils import parsedate_to_datetime
from http.client import IncompleteRead

import requests

# Keep-alive pool: the public catalog endpoints are called several times per
# refresh, and a fresh TLS handshake costs roughly as much as the request.
_local = threading.local()


def _http():
    session = getattr(_local, "session", None)
    if session is None:
        session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(pool_connections=8, pool_maxsize=32)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        _local.session = session
    return session


class HyperliquidError(RuntimeError):
    """Typed exchange failure with the operation, status and next retry."""

    def __init__(self, message, *, kind="network_error", operation="read",
                 status=None, retry_after=None):
        super().__init__(message)
        self.kind = kind
        self.operation = operation
        self.status = status
        self.retry_after = retry_after


class StartupNetworkError(HyperliquidError):
    pass


class StartupReadCancelled(RuntimeError):
    pass


def _status(exc):
    status = getattr(exc, "status_code", getattr(exc, "status", None))
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        status = exc.response.status_code
    return status


def classify_error(exc):
    """Return one stable category, never a transport-class soup."""
    if isinstance(exc, HyperliquidError):
        return exc.kind
    status = _status(exc)
    if status == 429:
        return "rate_limited"
    if isinstance(exc, (requests.Timeout, TimeoutError)) or status in (408, 504):
        return "timeout"
    if isinstance(exc, (requests.ConnectionError,
                        requests.exceptions.ChunkedEncodingError, IncompleteRead)):
        return "network_error"
    if isinstance(exc, requests.exceptions.JSONDecodeError):
        return "invalid_metadata"
    if isinstance(status, int) and status >= 500:
        return "exchange_unavailable"
    if status in (400, 401, 403, 404, 422):
        return "invalid_configuration"
    return "invalid_metadata"


def retry_after_seconds(exc):
    if isinstance(exc, HyperliquidError):
        return exc.retry_after
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if not headers:
        return None
    raw = headers.get("Retry-After")
    if raw is None:
        return None
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        try:
            value = parsedate_to_datetime(str(raw)).timestamp() - time.time()
        except (TypeError, ValueError, OverflowError):
            return None
    return max(0.0, value) if math.isfinite(value) else None


def _interruptible_wait(delay, sleep, cancelled):
    """Sleep in small slices so Stop closes the wait immediately."""
    remaining = max(0.0, delay)
    while remaining > 0:
        if cancelled and cancelled():
            raise StartupReadCancelled("Startup cancelled before trading")
        slice_sec = min(0.05, remaining)
        sleep(slice_sec)
        remaining -= slice_sec


def retry_read(label, operation, *, attempts=4, sleep=time.sleep, cancelled=None,
               base_delay=1.0, max_delay=60.0, jitter=True):
    """Retry rate limits, timeouts and server errors with typed diagnostics."""
    for attempt in range(1, attempts + 1):
        if cancelled and cancelled():
            raise StartupReadCancelled("Startup cancelled before trading")
        try:
            return operation()
        except Exception as exc:
            if cancelled and cancelled():
                raise StartupReadCancelled("Startup cancelled before trading") from exc
            kind = classify_error(exc)
            status = _status(exc)
            transient = kind in {"rate_limited", "timeout", "network_error",
                                 "exchange_unavailable"}
            if not transient:
                raise StartupNetworkError(
                    f"Hyperliquid {kind}: {label}", kind=kind, operation=label,
                    status=status, retry_after=retry_after_seconds(exc)) from exc
            retry_after = retry_after_seconds(exc)
            if attempt == attempts:
                if kind == "rate_limited":
                    wait = retry_after if retry_after is not None else max_delay
                    message = f"Limitation Hyperliquid, reprise dans {wait:.0f} s"
                elif kind == "timeout":
                    message = f"Hyperliquid timeout: {label}"
                elif kind == "exchange_unavailable":
                    message = f"Hyperliquid indisponible: {label}"
                else:
                    message = f"Hyperliquid réseau indisponible: {label}"
                raise StartupNetworkError(
                    message, kind=kind, operation=label, status=status,
                    retry_after=retry_after) from exc
            if retry_after is not None:
                delay = retry_after
            else:
                delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
                if jitter:
                    delay *= random.uniform(0.8, 1.2)
            # A server cooldown is a lower bound, including values above 60 s.
            delay = max(0.05, delay if retry_after is not None else min(max_delay, delay))
            prefix = ("Limitation Hyperliquid" if kind == "rate_limited"
                      else f"Hyperliquid {kind}")
            print(f"{prefix}: {label}, retrying {attempt + 1}/{attempts} "
                  f"in {delay:.1f}s", flush=True)
            _interruptible_wait(delay, sleep, cancelled)


def post_info(base_url, payload, *, cancelled=None):
    def read():
        return request_info(base_url, payload, cancelled=cancelled)
    return retry_read(str(payload.get("type", "info")), read, cancelled=cancelled)


def request_info(base_url, payload, *, timeout=10, cancelled=None, critical=False):
    """One budgeted read. No hidden retry and no signed action in this path."""
    from zeus.read_cache import read_budget
    read_budget().acquire(payload.get("type", "info"), critical=critical,
                          cancelled=cancelled)
    response = _http().post(base_url.rstrip("/") + "/info", json=payload, timeout=timeout)
    if response.status_code == 429:
        error = requests.HTTPError(response=response)
        read_budget().cooldown(retry_after_seconds(error) or 30)
    response.raise_for_status()
    return response.json()


def instrument_info(info, base_url, *, cancelled=None, transport=None):
    """Cover SDK account reads too; retain its original non-info operations."""
    original = info.post
    def post(path, payload=None):
        if path != "/info":
            return original(path, payload)
        ws = transport() if transport else None
        if ws is not None and ws.state()["ready"]:
            return ws.post_info(payload or {})
        return request_info(base_url, payload or {}, cancelled=cancelled, critical=True)
    info.post = post
    return info


class AccountInfoReads:
    """Read-only verification without constructing/loading an execution SDK."""
    def __init__(self, base_url, timeout=5, read=None):
        self.base_url, self.timeout, self.read = base_url, timeout, read or request_info

    def post(self, path, payload):
        if path != '/info':
            raise ValueError('Account verification permits info reads only')
        return self.read(self.base_url, payload, timeout=self.timeout, critical=True)

    def spot_user_state(self, account):
        return self.post('/info', {'type': 'spotClearinghouseState', 'user': account})

    def frontend_open_orders(self, account, dex=''):
        return self.post('/info', {'type': 'frontendOpenOrders', 'user': account, 'dex': dex})

    def user_state(self, account, dex=''):
        return self.post('/info', {'type': 'clearinghouseState', 'user': account, 'dex': dex})
