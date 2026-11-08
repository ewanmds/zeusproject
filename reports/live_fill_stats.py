"""Read-only: summarize recent live fills for the outcome account."""
import collections
import json
import time
from pathlib import Path

import requests
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
acct = dotenv_values(ROOT / '.env')['WALLET_ADDRESS_OUTCOME']
now = time.time()
fills = requests.post('https://api.hyperliquid.xyz/info', json={'type': 'userFills', 'user': acct}, timeout=10).json()


def window(sec):
    sel = [x for x in fills if now - float(x['time']) / 1000 <= sec]
    vol = sum(float(x['px']) * float(x['sz']) for x in sel)
    fee = sum(float(x.get('fee') or 0) for x in sel)
    return len(sel), vol, fee, sel


for sec in (300, 900, 1800, 3600, 7200, 86400):
    n, vol, fee, sel = window(sec)
    print(f'last {sec:>6}s fills {n:>4} vol ${vol:8.2f} fee ${fee:.4f} '
          f'$/min {vol / (sec / 60):7.2f} cost/1M {fee / vol * 1e6 if vol else 0:7.1f}')

n, vol, fee, sel = window(3600)
bycoin = collections.defaultdict(lambda: [0, 0.0, 0.0])
for x in sel:
    k = x['coin']
    bycoin[k][0] += 1
    bycoin[k][1] += float(x['px']) * float(x['sz'])
    bycoin[k][2] += float(x.get('fee') or 0)
print('--- last 3600s by coin ---')
for k, v in sorted(bycoin.items(), key=lambda kv: -kv[1][1]):
    print(f'{k:>9} fills {v[0]:>3} vol ${v[1]:8.2f} fee ${v[2]:.4f}')

b = [x for x in sel if x.get('side') == 'B']
a = [x for x in sel if x.get('side') == 'A']
print('last 1h buys', len(b), 'sells', len(a),
      'buy$', round(sum(float(x['px']) * float(x['sz']) for x in b), 2),
      'sell$', round(sum(float(x['px']) * float(x['sz']) for x in a), 2))
crossed = sum(1 for x in sel if x.get('crossed'))
print('crossed (taker) fills in last 1h:', crossed, '/', len(sel))
