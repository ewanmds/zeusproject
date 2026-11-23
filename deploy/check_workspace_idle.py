"""Read-only deployment interlock. Never imports a supervisor or signs orders."""
import argparse
import json
from pathlib import Path


def unresolved(roots):
    pending = []
    for root in map(Path, roots):
        records = {}
        for path in root.glob('*.json'):
            if path.name == 'api-budget.json':
                continue
            try:
                row = json.loads(path.read_text(encoding='utf-8'))
                if not isinstance(row, dict) or not row.get('session_id'):
                    raise ValueError('Invalid journal')
                records[row['session_id']] = row
            except (OSError, ValueError):
                pending.append(str(path))
        def verified(sid, seen):
            if sid in seen or sid not in records:
                return False
            row = records[sid]
            if row.get('cleanup_confirmed') is True and not row.get('pending_cloids'):
                return True
            child = records.get(row.get('resumed_session_id'))
            return bool(child and child.get('account') == row.get('account')
                        and child.get('symbols') == row.get('symbols')
                        and verified(child['session_id'], seen | {sid}))
        pending.extend(sid for sid in records if not verified(sid, set()))
    return pending


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--workspace', type=Path, required=True)
    parser.add_argument('--legacy', type=Path, required=True)
    args = parser.parse_args()
    pending = unresolved([args.legacy, *list((args.workspace/'tenants').glob('*'))])
    if pending:
        print('Update blocked: stop and verify sessions first. Immediate cancellation does not close inventory.')
        print('\n'.join(pending[:20]))
        return 1
    print('All persisted trading sessions are independently confirmed closed.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
