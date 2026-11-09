"""Tenant-owned managers and public read projections. No global env mutation."""
import math
import os
from pathlib import Path
import threading
import time
import uuid

from .accounts import AccountContext


def visible_session(row, kind):
    cfg = row.get('config', {})
    phase = row.get('phase', 'connecting')
    complete = row.get('accounting_complete', True) and not row.get('stale') and all(
        isinstance(row.get(k), (int, float)) and not isinstance(row[k], bool) and math.isfinite(row[k])
        for k in ('volume_usd', 'net_pnl', 'fees', 'fills'))
    config = {k: cfg.get(k) for k in ('selection_mode', 'exec_mode', 'order_size_usd',
              'max_position_usd', 'outcome_side', 'strategy', 'offset_bps', 'auto_max_spread_bps',
              'session_duration_sec', 'target_volume_usd', 'session_expires_at', 'session_stop_loss_usd') if k in cfg}
    return {'id': row.get('session_id') or row.get('key') or row.get('token'), 'kind': kind,
        'strategy': 'outcome' if kind == 'outcome' else cfg.get('strategy', 'bbo'),
        'name': row.get('market_name') or row.get('symbol') or row.get('market') or row.get('token', 'Session'),
        'market': row.get('market') or row.get('symbol'), 'phase': phase,
        'running': bool(row.get('running')), 'cleanup_confirmed': row.get('cleanup_confirmed') is True,
        'started': row.get('started', 0), 'duration_sec': row.get('duration_sec', 0),
        'volume_usd': row.get('volume_usd') if complete else None,
        'net_pnl': row.get('net_pnl') if complete else None, 'fees': row.get('fees') if complete else None,
        'fills': row.get('fills') if complete else None, 'cost_per_1m': row.get('cost_per_1m') if complete else None,
        'position_usd': row.get('position_usd'), 'stale': bool(row.get('stale')),
        'accounting_complete': bool(complete), 'config': config, 'quotes': row.get('quotes', []),
        'state': row.get('execution_state') or row.get('action') or phase,
        'reason': row.get('error') or row.get('cleanup_error') or row.get('blocked_reason') or row.get('auto_rotation_reason') or '',
        'rotations': row.get('auto_rotations', 0)}


class Runtimes:
    def __init__(self, root, store, accounts, manager_factory=None):
        self.root, self.store, self.accounts = Path(root), store, accounts
        self.factory = manager_factory
        self.items, self.locks, self.read_locks, self.cache = {}, {}, {}, {}
        self.guard = threading.RLock()
        self.source = uuid.uuid4().hex
        self.revisions = {}

    def tenant_lock(self, owner):
        with self.guard: return self.locks.setdefault(owner, threading.RLock())

    def manager(self, owner):
        with self.guard:
            if owner not in self.items:
                factory = self.factory
                if factory is None:
                    from zeus.server import BotManager
                    factory = BotManager
                agent = self.store.agent(owner, secret=True)
                context = AccountContext(owner, owner, agent['address'] if agent else '',
                                         agent['private_key'] if agent else '')
                self.items[owner] = factory(state_dir=self.root/'tenants'/owner[2:], account_context=context)
            return self.items[owner]

    def invalidate(self, owner):
        with self.guard: self.cache.pop(owner, None)

    def execution_revision(self, owner):
        manager = self.manager(owner)
        journals = {str(path) for path, _ in manager._session_files(fresh=True)}
        processes = {(key, info['proc'].pid) for key, info in manager.obi_procs.items()}
        processes.update((key, info['proc'].pid) for key, info in manager.outcome.procs.items())
        return frozenset(journals), frozenset(processes)

    def snapshot(self, owner):
        with self.guard:
            read_lock = self.read_locks.setdefault(owner, threading.Lock())
        # Starting/stopping can perform slow exchange reads. They must not
        # hold the lock used by this tenant's dashboard or live stream.
        with read_lock:
            cached = self.cache.get(owner)
            if cached and time.monotonic()-cached[0] < .5: return cached[1]
            manager = self.manager(owner)
            outcome = manager.get_worldcup_stats()
            active = outcome.get('display_bots', [])
            active_groups = {r['session_id'] for r in active}
            # All journals belong to this tenant. Aggregate archived rotations
            # once by logical session, preserving each leg's final accounting.
            groups = {}
            for path, record in manager.outcome.records():
                row = manager.outcome._entry(path, record, manager.outcome.procs.get(record['session_id']))
                group = row.get('config', {}).get('auto_group_id') or row['session_id']
                groups.setdefault(group, []).append(row)
            archived = []
            for group, members in groups.items():
                if group in active_groups: continue
                row = dict(max(members, key=lambda b: b.get('started', 0)))
                row['session_id'] = group
                row['started'] = min(b.get('started', 0) for b in members)
                complete = all(b.get('accounting_complete') and not b.get('stale') for b in members)
                for field in ('volume_usd', 'net_pnl', 'fees', 'fills'):
                    row[field] = sum(b.get(field) or 0 for b in members) if complete else None
                row['accounting_complete'] = complete
                row['cost_per_1m'] = -row['net_pnl']*1e6/row['volume_usd'] if complete and row['volume_usd'] else None
                archived.append(row)
            sessions = [visible_session(b, 'outcome') for b in active+archived]
            grid = manager.get_grid_stats()
            attached_grid = {Path(i['safety_file']).stem for i in manager.obi_procs.values() if i.get('safety_file')}
            for b in grid.get('bots', []):
                row = dict(b)
                row.setdefault('session_id', b.get('token') or b.get('key') or b.get('symbol'))
                row.setdefault('config', {})
                row['config'] = {**row['config'], 'strategy': b.get('strategy', row['config'].get('strategy', 'bbo'))}
                if row['session_id'] not in attached_grid and row.get('phase') == 'failed':
                    row['running'] = False
                sessions.append(visible_session(row, 'grid'))
            known = {s['id'] for s in sessions}
            for _, record in manager._session_files():
                if record.get('account_kind') != 'perp' or record['session_id'] in known:
                    continue
                snap = {}
                try:
                    from zeus.safety import read_json
                    candidate = read_json(record['stats_file'])
                    if candidate.get('symbol') == record['symbols'][0] and candidate.get('start_time', 0) >= record.get('started', 0)-5:
                        snap = candidate
                except (OSError, KeyError, ValueError): pass
                complete = bool(snap) and snap.get('accounting_complete') is not False
                row = {**record, 'running': False, 'symbol': record['symbols'][0],
                       'accounting_complete': complete, 'volume_usd': snap.get('volume'),
                       'net_pnl': float(snap.get('rpnl', 0))-float(snap.get('fees', 0)) if complete else None,
                       'fees': snap.get('fees'), 'fills': snap.get('fills'),
                       'position_usd': 0 if record.get('cleanup_confirmed') else None,
                       'duration_sec': max(0, float(snap.get('end_time') or snap.get('ts') or 0)-float(snap.get('start_time') or 0))}
                row['cost_per_1m'] = -row['net_pnl']*1e6/row['volume_usd'] if complete and row['volume_usd'] else None
                sessions.append(visible_session(row, 'grid'))
            sessions.sort(key=lambda b: b['started'], reverse=True)
            active_rows = [s for s in sessions if s['running'] or not s['cleanup_confirmed']]
            metrics = self.metrics(active_rows)
            self.revisions[owner] = self.revisions.get(owner, 0)+1
            result = {'type': 'workspace', 'owner': owner, 'source_id': self.source,
                      'revision': self.revisions[owner], 'published_at': time.time(),
                      'sessions': sessions, 'metrics': metrics}
            self.cache[owner] = (time.monotonic(), result)
            return result

    @staticmethod
    def metrics(rows):
        result = {'active': sum(r['running'] for r in rows), 'accounting_complete':
                  all(r['accounting_complete'] for r in rows)}
        for k in ('volume_usd', 'net_pnl', 'fees', 'fills'):
            result[k] = sum(r[k] or 0 for r in rows) if result['accounting_complete'] else None
        result['cost_per_1m'] = -result['net_pnl']*1e6/result['volume_usd'] if result['volume_usd'] else None
        return result

    def launch(self, owner, payload):
        from zeus.server import WorldcupStartRequest, GridStartRequest, manager as legacy
        with self.tenant_lock(owner):
            context = self.accounts.context(owner)  # Independently verify current approval before launch.
            legacy_accounts = {os.getenv('WALLET_ADDRESS', '').lower(), os.getenv('WALLET_ADDRESS_OUTCOME', '').lower()}
            if owner in legacy_accounts:
                for _, row in legacy._session_files(fresh=True):
                    if not row.get('cleanup_confirmed'):
                        raise ValueError('Stop and verify the previous private workspace before starting this account here')
            manager = self.manager(owner)
            manager.account_context = context
            if payload['strategy'] == 'outcome':
                req = WorldcupStartRequest(selection_mode='auto', outcome_side=payload['side'], exec_mode='maker',
                      two_sided=True, full_size_quotes=True, order_size_usd=payload['order_usd'],
                      max_position_usd=payload['position_cap'], auto_max_spread_bps=payload['spread_bps'],
                      session_duration_sec=payload.get('duration_sec'), target_volume_usd=payload.get('target_volume_usd'),
                      session_stop_loss_usd=payload.get('stop_loss_usd', 50))
                result = manager.start_worldcup(req)
            else:
                req = GridStartRequest(token=payload['market'], strategy=payload['strategy'], mode='live',
                      order_size_usd=payload['order_usd'], offset_bps=payload['offset_bps'])
                manager.start_grid(req)
                info = manager.obi_procs[req.token.strip().upper()]
                result = {'session_id': Path(info['safety_file']).stem, 'phase': 'connecting'}
            self.invalidate(owner)
            return result

    def stop(self, owner, session_id):
        with self.tenant_lock(owner):
            self.invalidate(owner)
            row = next((s for s in self.snapshot(owner)['sessions'] if s['id'] == session_id), None)
            if not row: raise KeyError('Session not found')
            if not row['running'] and row['cleanup_confirmed']: return self.snapshot(owner)
            manager = self.manager(owner)
            if row['kind'] == 'outcome': manager.stop_worldcup(session_id)
            else:
                # Resolve only this tenant's known Grid identity to its actual token.
                token = next((key for key, info in manager.obi_procs.items()
                              if key == session_id or info.get('safety_file') and Path(info['safety_file']).stem == session_id), None)
                if token is None: raise ValueError('Interrupted session: verify its stop first')
                manager.stop_grid(token)
            self.invalidate(owner)
            return self.snapshot(owner)

    def reconcile(self, owner):
        with self.tenant_lock(owner):
            result = self.manager(owner).reconcile_sessions()
            self.invalidate(owner)
            return result

    def close(self):
        import logging
        from zeus.safety import safe_error
        for manager in list(self.items.values()):
            manager.outcome.rotation.close()
            # Run every tenant's existing cleanup even if one cannot confirm.
            for stop in (lambda: manager.stop_grid(None, False), manager.stop_worldcup):
                try: stop()
                except Exception as exc:
                    logging.getLogger(__name__).error('Workspace cleanup unconfirmed: %s', safe_error(exc))
