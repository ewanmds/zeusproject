"""Read-only, bounded observation of the live outcome account.

Subscribes to orderUpdates/userFills (public user feed, no signing) and polls
openOrders + l2Book. Never imports a trading engine and never signs.
"""
import collections
import datetime
import json
from pathlib import Path
import sys
import threading
import time

import requests
import websocket
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
CFG = dotenv_values(ROOT / '.env')
ACCOUNT = CFG.get('WALLET_ADDRESS_OUTCOME', '')
DURATION = float(sys.argv[1]) if len(sys.argv) > 1 else 300.0
FOLDER = ROOT / 'reports' / 'observations' / datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d-%H%M%SZ')
FOLDER.mkdir(parents=True, exist_ok=False)
LOG = FOLDER / 'samples.jsonl'
lock = threading.Lock()
done = threading.Event()
status_counts = collections.Counter()
seen_updates = set()
seen_fills = set()
fills = []
orders = {}          # oid -> lifecycle record
coins = set()
started = time.time()


def log(kind, data):
    with lock:
        with LOG.open('a', encoding='utf-8') as f:
            f.write(json.dumps({'observed_at': time.time(), 'kind': kind, 'data': data}) + '\n')


def order_update(item):
    order = item.get('order', {})
    row = {k: order.get(k) for k in ('coin', 'side', 'limitPx', 'sz', 'origSz', 'oid')}
    row.update(status=item.get('status'), statusTimestamp=item.get('statusTimestamp'))
    key = (row['oid'], row['status'], row['statusTimestamp'], row['sz'])
    with lock:
        if key in seen_updates:
            return
        seen_updates.add(key)
        if row.get('coin'):
            coins.add(row['coin'])
        rec = orders.setdefault(row['oid'], {'coin': row.get('coin'), 'side': row.get('side'),
                                             'limitPx': row.get('limitPx'), 'origSz': row.get('origSz'),
                                             'events': []})
        rec['events'].append({'status': row['status'], 'ts': row['statusTimestamp'], 'sz': row['sz']})
        status_counts[row['status']] += 1
    log('order_update', row)


def record_fill(item):
    row = {k: item.get(k) for k in ('coin', 'side', 'px', 'sz', 'time', 'oid', 'tid', 'fee', 'closedPnl', 'crossed')}
    key = (row.get('tid'), row.get('oid'), row.get('time'), row.get('sz'))
    with lock:
        if key in seen_fills:
            return
        seen_fills.add(key)
        fills.append(row)
        if row.get('coin'):
            coins.add(row['coin'])
    log('fill', row)


def opened(ws):
    log('ws_open', {})
    for kind in ('orderUpdates', 'userFills'):
        ws.send(json.dumps({'method': 'subscribe', 'subscription': {'type': kind, 'user': ACCOUNT}}))


def message(ws, raw):
    data = json.loads(raw)
    if data.get('channel') == 'orderUpdates':
        for item in data.get('data', []):
            order_update(item)
    elif data.get('channel') == 'userFills':
        for item in data.get('data', {}).get('fills', []):
            record_fill(item)


def websocket_loop():
    while not done.is_set():
        app = websocket.WebSocketApp('wss://api.hyperliquid.xyz/ws', on_open=opened, on_message=message,
                                     on_error=lambda ws, e: log('ws_error', {'type': type(e).__name__}))
        globals()['ws_app'] = app
        app.run_forever(ping_interval=20, ping_timeout=10)
        done.wait(3)


threading.Thread(target=websocket_loop, daemon=True).start()
http = requests.Session()
last_poll = 0.0
print('OBSERVATION', FOLDER.relative_to(ROOT), 'duration_sec', DURATION, flush=True)
try:
    while time.time() - started < DURATION:
        cycle = time.time()
        if cycle - last_poll >= 8:
            last_poll = cycle
            try:
                oo = http.post('https://api.hyperliquid.xyz/info', json={'type': 'openOrders', 'user': ACCOUNT}, timeout=8).json()
                log('open_orders', [{k: o.get(k) for k in ('coin', 'side', 'limitPx', 'sz', 'oid', 'timestamp', 'origSz')} for o in oo])
                d = http.post('https://api.hyperliquid.xyz/info', json={'type': 'userFills', 'user': ACCOUNT}, timeout=8).json()
                for fill in d:
                    if float(fill.get('time', 0)) / 1000 >= started - 5:
                        record_fill(fill)
                for m in {o['coin'] for o in oo if o.get('coin')} | (coins & set(list(coins)[:4])):
                    r = http.post('https://api.hyperliquid.xyz/info', json={'type': 'l2Book', 'coin': m}, timeout=8)
                    if r.status_code == 200:
                        b = r.json()
                        log('book', {'coin': m, 'time': b.get('time'), 'levels': [lv[:2] for lv in b.get('levels', [])]})
            except Exception as e:
                log('poll_error', {'type': type(e).__name__, 'message': str(e)[:160]})
        time.sleep(max(0, 2 - (time.time() - cycle)))
finally:
    done.set()
    if globals().get('ws_app'):
        try:
            ws_app.close()
        except Exception:
            pass
    lat = []
    for oid, rec in orders.items():
        ev = sorted(rec['events'], key=lambda x: x['ts'] or 0)
        if ev:
            life = ((ev[-1]['ts'] or 0) - (ev[0]['ts'] or 0)) / 1000.0
            lat.append({'oid': oid, 'coin': rec['coin'], 'side': rec['side'], 'limitPx': rec['limitPx'],
                        'origSz': rec['origSz'], 'statuses': [e['status'] for e in ev], 'life_sec': round(life, 3)})
    summary = {'started_at': started, 'ended_at': time.time(), 'elapsed_sec': time.time() - started,
               'fills': fills, 'fill_count': len(fills),
               'exchange_volume': sum(float(f['px']) * float(f['sz']) for f in fills),
               'exchange_fees': sum(float(f.get('fee') or 0) for f in fills),
               'order_status_counts': dict(status_counts), 'orders': lat,
               'cancel_lifetimes': sorted(x['life_sec'] for x in lat if 'canceled' in x['statuses']),
               'coins': sorted(coins)}
    (FOLDER / 'summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    print('COMPLETE', FOLDER.relative_to(ROOT), flush=True)
