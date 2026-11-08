"""Bounded read-only observer for an explicitly launched Auto session.

No signing, launch, stop, reservation changes or credential output.
"""
import argparse
import datetime
import json
from pathlib import Path
import time

import requests
from dotenv import dotenv_values


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--session', required=True)
    parser.add_argument('--started', required=True, type=float)
    parser.add_argument('--duration', type=float, default=600)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    env = dotenv_values(root/'.env')
    base = 'http://127.0.0.1:'+str(env.get('BOT_PORT', '8080'))
    headers = {'X-API-Token': env.get('BOT_API_TOKEN', '')}
    folder = root/'reports'/'observations'/datetime.datetime.now(datetime.timezone.utc).strftime('recovered-%Y%m%d-%H%M%SZ')
    folder.mkdir(parents=True, exist_ok=False)
    http = requests.Session()
    workers, fills, markets, errors = {}, {}, set(), []
    start = time.monotonic()
    next_exchange = next_scan = next_progress = 0
    sample_count = 0
    def log(kind, data):
        with (folder/'samples.jsonl').open('a', encoding='utf-8') as file:
            file.write(json.dumps({'observed_at': time.time(), 'kind': kind, 'data': data})+'\n')
    def get(path, params=None):
        r = http.get(base+path, headers=headers, params=params, timeout=5)
        r.raise_for_status()
        return r.json()
    def info(kind):
        r = http.post('https://api.hyperliquid.xyz/info',
                      json={'type': kind, 'user': env['WALLET_ADDRESS_OUTCOME']}, timeout=5)
        r.raise_for_status()
        return r.json()
    print('OBSERVATION', folder.relative_to(root), flush=True)
    try:
        while time.monotonic()-start < args.duration:
            cycle = time.monotonic()
            try:
                state = get('/api/outcomes/status')
                rows = [b for b in state.get('bots', []) if b.get('session_id') == args.session
                        or b.get('started', 0) >= args.started-1]
                for bot in rows:
                    markets.add(bot['market'])
                    workers[bot['session_id']] = bot
                log('status', rows)
                sample_count += 1
                if cycle >= next_exchange:
                    next_exchange = cycle+15
                    log('open_orders', info('openOrders'))
                    for fill in info('userFills'):
                        if fill.get('coin') in markets and fill.get('time', 0)/1000 >= args.started:
                            key = (fill.get('tid'), fill.get('oid'), fill.get('time'), fill.get('sz'))
                            if key not in fills:
                                fills[key] = fill
                                log('fill', fill)
                    log('balances', info('spotClearinghouseState').get('balances'))
                if cycle >= next_scan:
                    next_scan = cycle+30
                    log('scanner', get('/api/outcomes/auto', {'side': 'no', 'order_usd': 25, 'max_spread_bps': 10}))
                if cycle >= next_progress:
                    next_progress = cycle+30
                    print(json.dumps({'elapsed_sec': round(cycle-start), 'workers': [
                        {k: b.get(k) for k in ('session_id', 'market', 'running', 'phase', 'volume_usd', 'fills',
                            'net_pnl', 'orders_sent', 'cancels_sent', 'execution_state', 'auto_rotation_state',
                            'auto_rotation_reason', 'telemetry_write_retries', 'error')}
                        for b in workers.values()],
                        'exchange_fills': len(fills)}), flush=True)
                # End observation early only when execution itself failed/stopped.
                logical_running = state.get('active_metrics', {}).get('running', state.get('running'))
                if rows and not logical_running:
                    break
            except Exception as exc:
                error = {'type': type(exc).__name__}
                errors.append(error)
                log('read_error', error)
            time.sleep(max(0, min(3-(time.monotonic()-cycle), args.duration-(time.monotonic()-start))))
    finally:
        summary = {'root_session_id': args.session, 'session_started_at': args.started,
                   'observed_sec': time.monotonic()-start, 'sample_count': sample_count,
                   'workers': list(workers.values()), 'fills': list(fills.values()), 'read_errors': errors,
                   'exchange_volume': sum(float(f['px'])*float(f['sz']) for f in fills.values()),
                   'exchange_fees': sum(float(f.get('fee') or 0) for f in fills.values())}
        (folder/'summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
        print('COMPLETE', folder.relative_to(root), flush=True)


if __name__ == '__main__':
    main()
