"""Reproducible offline read benchmark, not a live execution/fill benchmark."""
from concurrent.futures import ThreadPoolExecutor
import json
import statistics
import time
from zeus.outcome_reads import OutcomeAccountReads
from zeus.outcome_flow import OutcomeFlowScanner


def main():
    def read(base, payload):
        time.sleep(.08)
        return [] if payload['type'] == 'openOrders' else {'balances': []}
    serial, parallel = [], []
    reader = OutcomeAccountReads('offline', 'dummy', read=read)
    try:
        for _ in range(6):
            start = time.perf_counter()
            read('', {'type': 'spotClearinghouseState'})
            read('', {'type': 'openOrders'})
            serial.append((time.perf_counter()-start)*1000)
            start = time.perf_counter()
            reader.snapshot()
            parallel.append((time.perf_counter()-start)*1000)
    finally:
        reader.close()
    scanner = OutcomeFlowScanner()
    scanner.connected, scanner.catalog_at = True, time.time()
    scanner.rows, scanner.subscribed = {10: {'id': 10}}, {'#100', '#101'}
    computations = []
    def scan(*args):
        computations.append(1)
        time.sleep(.04)
        return {'ready': False, 'candidates': []}
    scanner._snapshot = scan
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda _: scanner.snapshot('no', 25, 10), range(8)))
    finally:
        scanner.close()
    print(json.dumps(dict(kind='offline, two injected 80ms reads; six samples',
        serial_median_ms=statistics.median(serial), parallel_median_ms=statistics.median(parallel),
        concurrent_scan_requests=8, scan_computations=len(computations)), indent=2))


if __name__ == '__main__':
    main()
