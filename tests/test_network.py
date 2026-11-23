"""Typed Hyperliquid retry policy: 429, Retry-After, timeouts, cancellation."""
import unittest

import requests

from zeus.network import (
    StartupNetworkError,
    StartupReadCancelled,
    classify_error,
    retry_after_seconds,
    retry_read,
)
from zeus.safety import safe_error


def http_error(status, headers=None):
    response = requests.Response()
    response.status_code = status
    response.headers.update(headers or {})
    return requests.HTTPError(f"HTTP {status}", response=response)


class NetworkRetryTests(unittest.TestCase):
    def test_rate_limit_respects_retry_after_and_reports_next_attempt(self):
        sleeps = []
        attempts = []

        def operation():
            attempts.append(1)
            if len(attempts) == 1:
                raise http_error(429, {"Retry-After": "0.2"})
            return "ok"

        result = retry_read("catalog", operation, sleep=sleeps.append)
        self.assertEqual(result, "ok")
        self.assertEqual(len(attempts), 2)
        self.assertAlmostEqual(sum(sleeps), 0.2, delta=0.06)

    def test_rate_limit_exhaustion_is_typed_not_a_network_guess(self):
        sleeps = []

        def operation():
            raise http_error(429, {"Retry-After": "0.3"})

        with self.assertRaises(StartupNetworkError) as caught:
            retry_read("scanner", operation, attempts=2, sleep=sleeps.append)
        error = caught.exception
        self.assertEqual(error.kind, "rate_limited")
        self.assertEqual(error.operation, "scanner")
        self.assertEqual(error.status, 429)
        self.assertAlmostEqual(error.retry_after, 0.3)
        self.assertIn("Limitation Hyperliquid", str(error))
        self.assertNotIn("Internet", str(error))

    def test_timeout_retries_then_succeeds(self):
        calls = []

        def operation():
            calls.append(1)
            if len(calls) == 1:
                raise requests.Timeout("slow")
            return {"ok": True}

        self.assertEqual(retry_read("meta", operation, sleep=lambda _: None),
                         {"ok": True})
        self.assertEqual(len(calls), 2)

    def test_wait_is_interruptible_by_stop(self):
        cancelled = True

        def operation():
            raise requests.ConnectionError("down")

        with self.assertRaises(StartupReadCancelled):
            retry_read("meta", operation, sleep=lambda _: None,
                       cancelled=lambda: cancelled)

    def test_invalid_configuration_never_retries(self):
        calls = []

        def operation():
            calls.append(1)
            raise http_error(400)

        with self.assertRaises(StartupNetworkError) as caught:
            retry_read("meta", operation, sleep=lambda _: None)
        self.assertEqual(caught.exception.kind, "invalid_configuration")
        self.assertEqual(len(calls), 1)

    def test_safe_error_never_dumps_sdk_response_headers(self):
        class ClientError(Exception):
            pass

        raw = ClientError("(429, None, 'null', None, {'Content-Type': 'application/json', "
                          "'Server': 'nginx', 'secret': 'leak'})")
        text = safe_error(raw)
        self.assertEqual(text, "Limitation Hyperliquid (429). Reprise automatique.")
        self.assertNotIn("nginx", text)
        self.assertNotIn("secret", text)
        self.assertEqual(safe_error(http_error(503)),
                         "Hyperliquid indisponible (503). Reprise automatique.")

    def test_classification_and_header_parsing(self):
        self.assertEqual(classify_error(http_error(429)), "rate_limited")
        self.assertEqual(classify_error(http_error(502)), "exchange_unavailable")
        self.assertEqual(classify_error(requests.Timeout()), "timeout")
        self.assertEqual(classify_error(http_error(422)), "invalid_configuration")
        self.assertEqual(retry_after_seconds(http_error(429, {"Retry-After": "7"})), 7)
        self.assertIsNone(retry_after_seconds(http_error(429)))
        self.assertEqual(retry_after_seconds(http_error(429, {"Retry-After": "999"})), 60)


if __name__ == "__main__":
    unittest.main()
