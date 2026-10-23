"""Bounded retries for idempotent startup reads. Never wrap order submissions."""
import time

import requests


class StartupNetworkError(RuntimeError):
    pass


def retry_read(label, operation, *, attempts=4, sleep=time.sleep):
    """Retry transport errors, throttling and server errors; reject bad config immediately."""
    for attempt in range(1, attempts + 1):
        try:
            return operation()
        except Exception as exc:
            status = getattr(exc, "status_code", None)
            if isinstance(exc, requests.HTTPError) and exc.response is not None:
                status = exc.response.status_code
            transient = (isinstance(exc, (requests.ConnectionError, requests.Timeout))
                         or status == 429 or isinstance(status, int) and status >= 500)
            if not transient:
                raise
            if attempt == attempts:
                raise StartupNetworkError(
                    f"Hyperliquid inaccessible : {label}, {attempts} tentatives "
                    f"({type(exc).__name__}). Vérifiez la connexion réseau puis relancez."
                ) from exc
            delay = min(2 ** (attempt - 1), 8)
            print(f"Connexion Hyperliquid : {label}, nouvelle tentative "
                  f"{attempt + 1}/{attempts} dans {delay}s", flush=True)
            sleep(delay)


def post_info(base_url, payload):
    def read():
        response = requests.post(base_url + "/info", json=payload, timeout=10)
        response.raise_for_status()
        return response.json()
    return retry_read(str(payload.get("type", "info")), read)
