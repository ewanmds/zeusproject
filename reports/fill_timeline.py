"""Read-only: fill timeline, idle gaps and per-market episodes."""
import collections
import time
from pathlib import Path

import requests
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
acct = dotenv_values(ROOT / '.env')['WALLET_ADDRESS_OUTCOME']
now = time.time()
fills = requests.post('https://api.hyperliquid.xyz/info', json={'type': 'userFills', 'user': acct}, timeout=10).json()
# userFillsMax has a bigger window; fall back to userFills if unavailable.
big = requests.post('https://api.hyperliquid.xyz/info',
                    json={'type': 'userFillsByTime', 'user': acct, 'startTime': int((now - 86400 * 7) * 1000)},
                    timeout=15)
if big.status_code == 200:
    fills = big.json()
fills = sorted(fills, key=lambda x: float(x['time']))
print('fills', len(fills), 'span_h', round((float(fills[-1]['time']) - float(fills[0]['time'])) / 3.6e6, 2) if fills else 0)

for hours in (1, 3, 6, 24, 72, 168):
    sel = [x for x in fills if now - float(x['time']) / 1000 <= hours * 3600]
    if len(sel) < 2:
        continue
    ts = [float(x['time']) / 1000 for x in sel]
    gaps = [b - a for a, b in zip(ts, ts[1:])]
    vol = sum(float(x['px']) * float(x['sz']) for x in sel)
    active = sum(g for g in gaps if g <= 120)
    idle = sum(g for g in gaps if g > 120)
    print(f'last {hours:>3}h fills {len(sel):>4} vol ${vol:8.2f} $/min {vol / (hours * 60):6.2f} | '
          f'gaps<=120s {active / 60:6.1f}min gaps>120s {idle / 60:6.1f}min '
          f'({100 * idle / max(1e-9, active + idle):4.1f}% idle) max_gap {max(gaps):6.0f}s')

# last 24h: volume per 30-min bucket
sel = [x for x in fills if now - float(x['time']) / 1000 <= 86400]
buckets = collections.Counter()
for x in sel:
    b = int((now - float(x['time']) / 1000) // 1800)
    buckets[b] += float(x['px']) * float(x['sz'])
print('last 24h volume per 30-min bucket (most recent first):')
print(' '.join(f'{buckets.get(i, 0):.0f}' for i in range(48)))

# episodes: market sequence in last 24h with duration
seq = []
for x in sel:
    m = x['coin']
    if not seq or seq[-1]['coin'] != m:
        seq.append({'coin': m, 'first': float(x['time']) / 1000, 'last': float(x['time']) / 1000,
                    'n': 0, 'vol': 0.0})
    e = seq[-1]
    e['last'] = float(x['time']) / 1000
    e['n'] += 1
    e['vol'] += float(x['px']) * float(x['sz'])
print('last 24h market episodes (fills spread):')
for e in seq:
    print(f"  {e['coin']:>9} n={e['n']:>3} vol=${e['vol']:7.2f} span={(e['last'] - e['first']) / 60:6.1f}min "
          f"age={round((now - e['last']) / 60)}min")
