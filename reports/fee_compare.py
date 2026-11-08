"""Read-only: fee comparison maker vs taker in recent account fills."""
import collections
import time
from pathlib import Path

import requests
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
acct = dotenv_values(ROOT / '.env')['WALLET_ADDRESS_OUTCOME']
now = time.time()
fills = requests.post('https://api.hyperliquid.xyz/info', json={'type': 'userFills', 'user': acct}, timeout=10).json()

stats = collections.defaultdict(lambda: {'n': 0, 'vol': 0.0, 'fee': 0.0})
for x in fills:
    key = ('taker' if x.get('crossed') else 'maker', x.get('side'))
    s = stats[key]
    s['n'] += 1
    s['vol'] += float(x['px']) * float(x['sz'])
    s['fee'] += float(x.get('fee') or 0)

print('fills in feed:', len(fills), 'oldest age h:', round((now - min(float(x['time']) / 1000 for x in fills)) / 3600, 2))
for key, s in sorted(stats.items()):
    print(f'{key[0]:>5} {key[1]}: n={s["n"]:>4} vol=${s["vol"]:9.2f} fee=${s["fee"]:.4f} '
          f'bps={s["fee"] / s["vol"] * 1e4 if s["vol"] else 0:.2f}')
