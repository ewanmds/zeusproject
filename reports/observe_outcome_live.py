"""Read-only, bounded live observation. Never imports a trading engine or signs."""
import collections
import datetime
import json
from pathlib import Path
import threading
import time

import requests
import websocket
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
CFG = dotenv_values(ROOT / '.env')
BASE = 'http://127.0.0.1:' + str(CFG.get('BOT_PORT', '8080'))
HEADERS = {'X-API-Token': CFG.get('BOT_API_TOKEN', '')}
ACCOUNT = CFG.get('WALLET_ADDRESS_OUTCOME', '')
INITIAL_ID = '5e713f0b5c554efe8b88ca9c3446e5dd'
INITIAL_START = 1794064858.2004597
DURATION = 660
FOLDER = ROOT / 'reports' / 'observations' / datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d-%H%M%SZ')
FOLDER.mkdir(parents=True, exist_ok=False)
LOG = FOLDER / 'samples.jsonl'
lock = threading.Lock()
done = threading.Event()
status_counts = collections.Counter()
seen_updates = set()
seen_fills = set()
fills = []
markets = {'#257961'}
sessions = {}
samples = []
started = time.time()


def log(kind, data):
    with lock:
        with LOG.open('a', encoding='utf-8') as file:
            file.write(json.dumps({'observed_at': time.time(), 'kind': kind, 'data': data}) + '\n')


def order_update(item):
    order = item.get('order', {})
    if order.get('coin') not in markets:
        return
    row = {k: order.get(k) for k in ('coin', 'side', 'limitPx', 'sz', 'origSz', 'oid', 'timestamp')}
    row.update(status=item.get('status'), statusTimestamp=item.get('statusTimestamp'))
    key = (row['oid'], row['status'], row['statusTimestamp'], row['sz'])
    with lock:
        if key in seen_updates:
            return
        seen_updates.add(key)
        status_counts[row['status']] += 1
    log('order_update', row)


def record_fill(item):
    if item.get('coin') not in markets or float(item.get('time', 0))/1000 < INITIAL_START:
        return
    row = {k: item.get(k) for k in ('coin', 'side', 'px', 'sz', 'time', 'oid', 'tid', 'fee', 'feeToken', 'closedPnl', 'crossed')}
    key = (row['tid'], row['oid'], row['time'], row['sz'])
    with lock:
        if key in seen_fills:
            return
        seen_fills.add(key)
        fills.append(row)
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
                                     on_error=lambda ws, error: log('ws_error', {'type': type(error).__name__}))
        globals()['ws_app'] = app
        app.run_forever(ping_interval=20, ping_timeout=10)
        done.wait(3)


threading.Thread(target=websocket_loop, daemon=True).start()
http = requests.Session()
last_public = last_scan = last_progress = 0
print('OBSERVATION', FOLDER.relative_to(ROOT), 'duration_sec', DURATION, flush=True)
try:
    while time.time()-started < DURATION:
        cycle = time.time()
        try:
            r = http.get(BASE+'/api/outcomes/status', headers=HEADERS, timeout=8)
            r.raise_for_status()
            state = r.json()
            rows = []
            for bot in state.get('bots', []):
                if bot.get('session_id') != INITIAL_ID and bot.get('started', 0) < INITIAL_START:
                    continue
                sid = bot['session_id']
                markets.add(bot['market'])
                row = {k: bot.get(k) for k in ('session_id', 'market', 'market_name', 'started', 'running', 'phase', 'stale',
                    'cleanup_confirmed', 'position_known', 'position_usd', 'volume_usd', 'net_pnl', 'fees', 'fills', 'duration',
                    'bid_open', 'ask_open', 'last_action', 'error', 'auto_rotation_state', 'auto_rotation_reason', 'auto_fill_usd_per_min')}
                config = bot.get('config', {})
                row['config'] = {k: config.get(k) for k in ('order_size_usd', 'max_position_usd', 'outcome_side', 'exec_mode')}
                row['initial_selection'] = config.get('auto_selection')
                sessions[sid] = row
                rows.append(row)
            log('status', rows)
            samples.append({'time': cycle, 'rows': rows})
            # A small read of the existing fund journal, without modifying reservations.
            reserved = 0
            for path in (ROOT/'.zeus-state'/'funds').glob('*/funds.json'):
                data = json.loads(path.read_text())
                reserved += sum(float(x['usd']) for x in data.get('reserved', {}).values())
            log('funds_reserved', reserved)
        except Exception as error:
            log('status_error', {'type': type(error).__name__, 'message': str(error)[:160]})
        if cycle-last_public >= 12:
            last_public = cycle
            try:
                for kind in ('openOrders', 'userFills'):
                    r = http.post('https://api.hyperliquid.xyz/info', json={'type': kind, 'user': ACCOUNT}, timeout=8)
                    r.raise_for_status()
                    data = r.json()
                    if kind == 'userFills':
                        for fill in data:
                            record_fill(fill)
                    else:
                        log('open_orders', [{k: o.get(k) for k in ('coin', 'side', 'limitPx', 'sz', 'oid', 'timestamp')}
                                            for o in data if o.get('coin') in markets])
                for market in {row['market'] for row in sessions.values() if row.get('running')}:
                    r = http.post('https://api.hyperliquid.xyz/info', json={'type': 'l2Book', 'coin': market}, timeout=8)
                    r.raise_for_status()
                    book = r.json()
                    log('book', {'coin': market, 'time': book.get('time'), 'levels': [levels[:3] for levels in book.get('levels', [])]})
            except Exception as error:
                log('public_read_error', {'type': type(error).__name__, 'message': str(error)[:160]})
        if cycle-last_scan >= 45:
            last_scan = cycle
            try:
                r = http.get(BASE+'/api/outcomes/auto', params={'side': 'no', 'order_usd': 10, 'max_spread_bps': 10}, headers=HEADERS, timeout=10)
                r.raise_for_status()
                log('scanner', r.json())
            except Exception as error:
                log('scanner_error', {'type': type(error).__name__})
        if cycle-last_progress >= 55:
            last_progress = cycle
            print(json.dumps({'elapsed_sec': round(cycle-started), 'sessions': list(sessions.values()),
                'order_status_counts': dict(status_counts), 'exchange_fills': len(fills),
                'exchange_volume': sum(float(f['px'])*float(f['sz']) for f in fills)}), flush=True)
        done.wait(max(0, 5-(time.time()-cycle)))
finally:
    done.set()
    if globals().get('ws_app'):
        ws_app.close()
    summary = {'started_at': started, 'ended_at': time.time(), 'elapsed_sec': time.time()-started,
        'sessions': list(sessions.values()), 'order_status_counts': dict(status_counts), 'fills': fills,
        'exchange_volume': sum(float(f['px'])*float(f['sz']) for f in fills),
        'exchange_fees': sum(float(f.get('fee') or 0) for f in fills), 'sample_count': len(samples)}
    (FOLDER/'summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    print('COMPLETE', FOLDER.relative_to(ROOT), flush=True)
