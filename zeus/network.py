"""Bounded retries for idempotent startup reads. Never wrap order submissions."""
import threading
import time
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


class StartupNetworkError(RuntimeError):
    pass


class StartupReadCancelled(RuntimeError):
    pass


def retry_read(label, operation, *, attempts=4, sleep=time.sleep, cancelled=None):
    """Retry transport errors, throttling and server errors; reject bad config immediately."""
    for attempt in range(1, attempts + 1):
        if cancelled and cancelled():
            raise StartupReadCancelled("Startup cancelled before trading")
        try:
            return operation()
        except Exception as exc:
            if cancelled and cancelled():
                raise StartupReadCancelled("Startup cancelled before trading") from exc
            status = getattr(exc, "status_code", None)
            if isinstance(exc, requests.HTTPError) and exc.response is not None:
                status = exc.response.status_code
            transient = (isinstance(exc, (requests.ConnectionError, requests.Timeout,
                                       requests.exceptions.ChunkedEncodingError, IncompleteRead))
                         or status == 429 or isinstance(status, int) and status >= 500)
            if not transient:
                raise
            if attempt == attempts:
                raise StartupNetworkError(
                    f"Hyperliquid unreachable: {label}, {attempts} attempts "
                    f"({type(exc).__name__}). Check your network connection and retry."
                ) from exc
            delay = min(2 ** (attempt - 1), 8)
            print(f"Hyperliquid connection: {label}, retrying "
                  f"{attempt + 1}/{attempts} in {delay}s", flush=True)
            sleep(delay)


def post_info(base_url, payload):
    def read():
        response = _http().post(base_url + "/info", json=payload, timeout=10)
        response.raise_for_status()
        return response.json()
    return retry_read(str(payload.get("type", "info")), read)
