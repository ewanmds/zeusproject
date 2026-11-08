"""Parallel, read-only account snapshots with persistent per-thread HTTP sessions.

Exchange submissions never enter this module. Consumers still validate both
responses and reject snapshots overtaken by WebSocket account revisions.
"""
from concurrent.futures import ThreadPoolExecutor, wait
import threading

import requests


_local = threading.local()


def read_info(base_url, payload, *, timeout=10):
    if not hasattr(_local, 'session'):
        _local.session = requests.Session()
    response = _local.session.post(base_url+'/info', json=payload, timeout=timeout)
    response.raise_for_status()
    return response.json()


class OutcomeAccountReads:
    def __init__(self, base_url, account, *, read=read_info):
        self.base_url, self.account, self.read = base_url, account, read
        self.lock = threading.Lock()
        self.closed = False
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='outcome-account')

    def snapshot(self):
        with self.lock:
            if self.closed:
                raise RuntimeError('Outcome account reader closed')
            futures = [self.pool.submit(self.read, self.base_url, {'type': kind, 'user': self.account})
                       for kind in ('spotClearinghouseState', 'openOrders')]
            # Drain both bounded reads even if one fails: no old request may
            # leak into a later reconciliation or mutate execution state.
            wait(futures)
            return tuple(future.result() for future in futures)

    def close(self):
        with self.lock:
            if not self.closed:
                self.closed = True
                self.pool.shutdown(wait=False, cancel_futures=True)
