"""Read-only account Outcome audit over WebSocket. Never signs or sends actions."""
import argparse
from collections import defaultdict
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from zeus.ws_execution import WebSocketExecution


def summarize(fills, now, start_time=0):
    hours = defaultdict(list)
    unique = {}
    for fill in fills:
        if str(fill.get('coin', '')).startswith('#'):
            unique[(fill['coin'], fill['time'], fill.get('tid'), fill['side'], fill['px'], fill['sz'])] = fill
    for fill in unique.values():
        hours[int(fill['time'])//3600000].append(fill)
    rows = []
    for hour, trades in sorted(hours.items()):
        volume = sum(float(f['px'])*abs(float(f['sz'])) for f in trades)
        gross = sum(float(f.get('closedPnl', 0)) for f in trades)
        fees = sum(float(f.get('fee', 0)) for f in trades)
        rows.append({'hour_bangkok': datetime.fromtimestamp(hour*3600, timezone(timedelta(hours=7))).isoformat(),
            'complete_hour': start_time <= hour*3600 and now >= (hour+1)*3600, 'volume_usd': round(volume, 5),
            'fills': len(trades), 'maker_fills': sum(f.get('crossed') is False for f in trades),
            'taker_fills': sum(bool(f.get('crossed')) for f in trades),
            'gross_realized_pnl': round(gross, 6), 'fees': round(fees, 6),
            'net_realized_pnl': round(gross-fees, 6),
            'cost_per_million': round((fees-gross)*1e6/volume, 2) if volume else None})
    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--account', default='')
    parser.add_argument('--days', type=int, choices=range(1, 8), default=1)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    account = args.account
    if not account:
        records = []
        for path in (root/'.zeus-workspace'/'tenants').glob('*/*.json'):
            try:
                row = json.loads(path.read_text(encoding='utf-8'))
                if row.get('account_kind') == 'outcome' and row.get('account'):
                    records.append(row)
            except (OSError, ValueError):
                pass
        if not records:
            parser.error('Provide the master account address with --account')
        account = max(records, key=lambda r: r.get('started', 0))['account']
    if not (len(account) == 42 and account.startswith('0x') and all(c in '0123456789abcdefABCDEF' for c in account[2:])):
        parser.error('Invalid master account address')
    ws = WebSocketExecution('https://api.hyperliquid.xyz', request_timeout=10)
    fills, requests, truncated = [], 0, False
    now = time.time()
    intervals = [(int((now-args.days*86400)*1000), int(now*1000))]
    try:
        ws.start()
        if not ws.wait_ready(5):
            raise RuntimeError('Read-only WebSocket unavailable')
        while intervals and requests < 32:
            start, end = intervals.pop()
            data = ws.post_info({'type': 'userFillsByTime', 'user': account,
                                 'startTime': start, 'endTime': end})
            requests += 1
            if len(data) >= 2000 and end > start:
                middle = (start+end)//2
                intervals.extend([(middle+1, end), (start, middle)])
            else:
                truncated |= len(data) >= 2000
                fills.extend(data)
    finally:
        ws.close()
    result = {'scope': 'account Outcome fills; not proof of bot ownership',
              'read_only': True, 'transport': 'websocket', 'generated_at_utc': datetime.now(timezone.utc).isoformat(),
              'history_complete': not truncated and not intervals, 'requests': requests,
              'hours': summarize(fills, now, now-args.days*86400)}
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered+'\n', encoding='utf-8')
        print(str(args.output))
    else:
        print(rendered)


if __name__ == '__main__':
    main()
