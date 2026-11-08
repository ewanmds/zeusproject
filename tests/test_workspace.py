"""Real signed login, replay/tenant isolation, encrypted credentials; no exchange I/O."""
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch, Mock

from eth_account import Account
from eth_account.messages import encode_defunct, encode_typed_data
from fastapi.testclient import TestClient

from zeus.workspace.app import Settings, create_app
from zeus.workspace.accounts import HyperliquidAccounts, typed_approval, AccountContext


class FakeAccounts(HyperliquidAccounts):
    def __init__(self, store):
        self.approved = {}; self.relays = 0
        super().__init__(store, self.exchange)

    def exchange(self, path, data):
        if path == '/exchange':
            self.relays += 1
            self.approved[data['action']['agentAddress'].lower()] = True
            return {'status': 'ok'}
        if data['type'] == 'extraAgents':
            agent = self.store.agent(data['user'])
            return ([{'address': agent['address'], 'validUntil': (time.time()+86400)*1000}]
                    if agent and self.approved.get(agent['address'].lower()) else [])
        return {'balances': [{'coin': 'USDC', 'total': '100', 'hold': '0'}]}


class FakeManager:
    def __init__(self, state_dir, account_context):
        self.state_dir, self.account_context = state_dir, account_context
        self.rows = []; self.obi_procs = {}
        self.outcome = SimpleNamespace(records=lambda: [], procs={}, rotation=SimpleNamespace(close=lambda: None))

    def get_worldcup_stats(self): return {'display_bots': self.rows}
    def get_grid_stats(self): return {'bots': []}
    def _session_files(self, **kwargs): return []
    def start_worldcup(self, req):
        row = dict(session_id=uuid.uuid4().hex, market='#101', market_name='Test', config=req.model_dump(),
                   running=True, phase='running', cleanup_confirmed=False, accounting_complete=True,
                   started=time.time(), volume_usd=25, fees=.01, fills=1, net_pnl=-.01, position_usd=0)
        self.rows.append(row); return {'session_id': row['session_id']}
    def stop_worldcup(self, sid=None):
        for row in self.rows:
            if sid is None or row['session_id'] == sid: row.update(running=False, cleanup_confirmed=True, phase='stopped')
    def stop_grid(self, *args): pass
    def reconcile_sessions(self): return {'can_start': True, 'unresolved': []}


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.origin = 'http://127.0.0.1:8081'
        self.app = create_app(Settings(Path(self.temp.name), origin=self.origin, warm_catalogs=False),
                              accounts_factory=FakeAccounts, manager_factory=FakeManager)
        self.a, self.b = Account.create(), Account.create()
        self.client = TestClient(self.app, base_url=self.origin, headers={'Origin': self.origin})

    def tearDown(self):
        self.client.close(); self.app.state.store.engine.dispose(); self.temp.cleanup()

    def login(self, client, account):
        r = client.post('/api/v2/auth/challenge', json={'address': account.address, 'chain_id': 1})
        self.assertEqual(r.status_code, 200, r.text)
        signature = '0x'+Account.sign_message(encode_defunct(text=r.json()['message']), account.key).signature.hex()
        r = client.post('/api/v2/auth/verify', json={'signature': signature})
        self.assertEqual(r.status_code, 200, r.text)
        me = client.get('/api/v2/me').json(); client.headers['X-Zeus-CSRF'] = me['csrf']
        return me, signature

    def authorize(self, client, account):
        r = client.post('/api/v2/agent/prepare', json={}); self.assertEqual(r.status_code, 200, r.text)
        signature = '0x'+Account.sign_message(encode_typed_data(full_message=r.json()['typed_data']), account.key).signature.hex()
        r = client.post('/api/v2/agent/approve', json={'signature': signature})
        self.assertEqual(r.status_code, 200, r.text); self.assertEqual(r.json()['status'], 'ready')

    def test_no_identity_cannot_read_or_control_any_tenant(self):
        self.assertEqual(self.client.get('/api/v2/workspace').status_code, 401)
        self.assertEqual(self.client.post('/api/v2/sessions/x/stop', json={}).status_code, 401)
        self.assertEqual(self.client.get('/api/outcomes/status').status_code, 404)

    def test_login_signed_message_cookie_and_logout(self):
        me, _ = self.login(self.client, self.a)
        self.assertEqual(me['wallet'], self.a.address.lower())
        self.assertNotIn('private', json.dumps(me))
        self.assertEqual(self.client.post('/api/v2/auth/logout', json={}).status_code, 200)
        self.assertEqual(self.client.get('/api/v2/me').status_code, 401)

    def test_challenge_is_single_use_even_if_cookie_is_replayed(self):
        response = self.client.post('/api/v2/auth/challenge', json={'address': self.a.address})
        token = self.client.cookies.get('zeus_challenge')
        signature = '0x'+Account.sign_message(encode_defunct(text=response.json()['message']), self.a.key).signature.hex()
        self.assertEqual(self.client.post('/api/v2/auth/verify', json={'signature': signature}).status_code, 200)
        self.client.cookies.set('zeus_challenge', token, path='/api/v2/auth')
        self.assertEqual(self.client.post('/api/v2/auth/verify', json={'signature': signature}).status_code, 400)

    def test_wrong_signer_and_expired_challenge_fail(self):
        r = self.client.post('/api/v2/auth/challenge', json={'address': self.a.address})
        signature = '0x'+Account.sign_message(encode_defunct(text=r.json()['message']), self.b.key).signature.hex()
        self.assertEqual(self.client.post('/api/v2/auth/verify', json={'signature': signature}).status_code, 401)
        self.client.post('/api/v2/auth/challenge', json={'address': self.a.address})
        with self.app.state.store.engine.begin() as db:
            db.execute(self.app.state.store.challenges.update().values(expires=0))
        self.assertEqual(self.client.post('/api/v2/auth/verify', json={'signature': signature}).status_code, 400)

    def test_origin_csrf_and_extra_owner_cannot_bypass_auth(self):
        self.login(self.client, self.a)
        self.assertEqual(self.client.post('/api/v2/agent/prepare', json={}, headers={'Origin':'https://other.invalid'}).status_code, 403)
        self.assertEqual(self.client.post('/api/v2/agent/prepare', json={}, headers={'X-Zeus-CSRF':'wrong'}).status_code, 403)
        body = {'strategy':'outcome','order_usd':25,'position_cap':200,'owner': self.b.address}
        self.assertEqual(self.client.post('/api/v2/sessions', json=body).status_code, 422)

    def test_agents_encrypted_and_signatures_bound_to_owner(self):
        self.login(self.client, self.a)
        prep = self.client.post('/api/v2/agent/prepare', json={}).json()
        secret = self.app.state.store.agent(self.a.address.lower(), secret=True)['private_key']
        self.assertNotIn(secret, json.dumps(prep))
        self.assertNotIn(secret.encode(), (Path(self.temp.name)/'workspace.db').read_bytes())
        wrong = '0x'+Account.sign_message(encode_typed_data(full_message=prep['typed_data']), self.b.key).signature.hex()
        self.assertEqual(self.client.post('/api/v2/agent/approve', json={'signature':wrong}).status_code, 400)
        self.authorize(self.client, self.a)

    def test_metamask_v4_signature_matches_server_domain(self):
        self.login(self.client, self.a)
        prepared = self.client.post('/api/v2/agent/prepare', json={}).json()
        typed = prepared['typed_data']
        helper = Path(__file__).with_name('sign_agent_v4.cjs')
        result = subprocess.run(['node', str(helper)], input=json.dumps({'typed': typed, 'key': self.a.key.hex()}),
                                capture_output=True, text=True, check=True)
        signed = json.loads(result.stdout)
        message = encode_typed_data(full_message=typed)
        from hyperliquid.utils.signing import sign_inner, user_signed_payload
        # sign_agent forces the SDK's default chain; browser wallets use Arbitrum.
        sdk = sign_inner(self.a, user_signed_payload(typed['primaryType'],
                         typed['types'][typed['primaryType']], dict(prepared['approval'])))
        raw = bytes.fromhex(signed['valid'][2:])
        self.assertEqual(sdk, {'r': hex(int.from_bytes(raw[:32], 'big')),
                               's': hex(int.from_bytes(raw[32:64], 'big')), 'v': raw[64]})
        # The former missing-domain payload reproduces the reported mismatch.
        self.assertNotEqual(Account.recover_message(message, signature=signed['legacy']).lower(), self.a.address.lower())
        self.assertEqual(self.client.post('/api/v2/agent/approve', json={'signature': signed['legacy']}).status_code, 400)
        response = self.client.post('/api/v2/agent/approve', json={'signature': signed['valid']})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['status'], 'ready')

    def test_two_wallets_same_market_have_isolated_managers_and_streams(self):
        self.login(self.client, self.a); self.authorize(self.client, self.a)
        other = TestClient(self.app, base_url=self.origin, headers={'Origin':self.origin})
        try:
            self.login(other, self.b); self.authorize(other, self.b)
            payload={'strategy':'outcome','order_usd':25,'position_cap':200}
            one=self.client.post('/api/v2/sessions', json=payload, headers={'Idempotency-Key':'a'*32}).json()
            two=other.post('/api/v2/sessions', json=payload, headers={'Idempotency-Key':'b'*32}).json()
            self.assertEqual(one['phase'], 'confirmed', one);self.assertEqual(two['phase'], 'confirmed', two)
            own=self.client.get('/api/v2/workspace').json();foreign=other.get('/api/v2/workspace').json()
            self.assertEqual(len(own['sessions']), 1);self.assertEqual(len(foreign['sessions']), 1)
            self.assertNotEqual(own['sessions'][0]['id'],foreign['sessions'][0]['id'])
            self.assertEqual(other.post('/api/v2/sessions/'+own['sessions'][0]['id']+'/stop',json={}).status_code,404)
            self.assertEqual(other.get('/api/v2/commands/'+'a'*32).status_code,404)
            with self.client.websocket_connect('ws://127.0.0.1:8081/api/v2/ws',headers={'Origin':self.origin}) as ws:
                frame=ws.receive_json();self.assertEqual(frame['owner'],self.a.address.lower())
                self.assertEqual(frame['sessions'][0]['id'],own['sessions'][0]['id'])
        finally: other.close()

    def test_idempotent_launch_and_changed_payload_conflict(self):
        self.login(self.client,self.a);self.authorize(self.client,self.a)
        payload={'strategy':'outcome','order_usd':25,'position_cap':200};headers={'Idempotency-Key':'same-command-key-123'}
        first=self.client.post('/api/v2/sessions',json=payload,headers=headers)
        second=self.client.post('/api/v2/sessions',json=payload,headers=headers)
        self.assertEqual(first.json(),second.json());self.assertEqual(len(self.client.get('/api/v2/workspace').json()['sessions']),1)
        self.assertEqual(self.client.post('/api/v2/sessions',json={**payload,'order_usd':50},headers=headers).status_code,400)

    def test_uncertain_launch_is_consumed_and_never_automatically_replayed(self):
        self.login(self.client,self.a)
        with patch.object(self.app.state.runtimes,'launch',side_effect=TimeoutError('offline timeout')) as launch:
            for _ in range(2):
                r=self.client.post('/api/v2/sessions',json={'strategy':'outcome','order_usd':25,'position_cap':200},headers={'Idempotency-Key':'unknown-command-1234'})
                self.assertEqual(r.json()['phase'],'unconfirmed')
            self.assertEqual(launch.call_count,1)

    def test_preview_uses_account_balance_not_agent(self):
        self.login(self.client,self.a)
        p=self.client.post('/api/v2/sessions/preview',json={'strategy':'outcome','order_usd':25,'position_cap':200})
        self.assertEqual(p.json()['account'],self.a.address.lower());self.assertEqual(p.json()['usdc_available'],100)
        self.assertEqual(self.client.post('/api/v2/sessions/preview',json={'strategy':'outcome','order_usd':250,'position_cap':300}).status_code,400)

    def test_production_cannot_start_with_insecure_defaults(self):
        with self.assertRaises(RuntimeError): create_app(Settings(Path(self.temp.name)/'prod',production=True))

    def test_dashboard_does_not_wait_for_the_execution_lock(self):
        self.login(self.client,self.a)
        runtime=self.app.state.runtimes;owner=self.a.address.lower();runtime.manager(owner)
        entered,released=threading.Event(),threading.Event()
        def hold():
            with runtime.tenant_lock(owner): entered.set();released.wait(3)
        thread=threading.Thread(target=hold);thread.start();self.assertTrue(entered.wait(1))
        try:
            start=time.monotonic();snapshot=runtime.snapshot(owner)
            self.assertEqual(snapshot['owner'],owner);self.assertLess(time.monotonic()-start,1)
        finally:released.set();thread.join(3)

    def test_repeated_agent_approval_does_not_relay_twice(self):
        self.login(self.client,self.a)
        prepared=self.client.post('/api/v2/agent/prepare',json={}).json()
        signature='0x'+Account.sign_message(encode_typed_data(full_message=prepared['typed_data']),self.a.key).signature.hex()
        for _ in range(2):self.assertEqual(self.client.post('/api/v2/agent/approve',json={'signature':signature}).status_code,200)
        self.assertEqual(self.app.state.accounts.relays,1)

    def test_uncertain_approval_remains_pending_without_second_relay(self):
        self.login(self.client,self.a)
        prepared=self.client.post('/api/v2/agent/prepare',json={}).json()
        signature='0x'+Account.sign_message(encode_typed_data(full_message=prepared['typed_data']),self.a.key).signature.hex()
        calls=[]
        def uncertain(path,data):
            if path=='/exchange':calls.append(data['nonce']);raise TimeoutError('offline timeout')
            return []
        with patch.object(self.app.state.accounts,'post',side_effect=uncertain):
            self.assertEqual(self.client.post('/api/v2/agent/approve',json={'signature':signature}).status_code,503)
            retry=self.client.post('/api/v2/agent/approve',json={'signature':signature})
            self.assertEqual(retry.json()['status'],'approval_pending')
            self.assertEqual(len(calls),1)

    def test_only_proven_prelaunch_rejection_can_be_dismissed(self):
        self.login(self.client,self.a)
        body={'strategy':'outcome','order_usd':25,'position_cap':200}
        with patch.object(self.app.state.runtimes,'launch',side_effect=ValueError('No eligible flow')):
            response=self.client.post('/api/v2/sessions',json=body,headers={'Idempotency-Key':'rejected-before-spawn'})
            self.assertEqual(response.json()['phase'],'rejected')
        with patch.object(self.app.state.runtimes,'execution_revision',side_effect=['before','after']), \
             patch.object(self.app.state.runtimes,'launch',side_effect=ValueError('Worker registration uncertain')):
            response=self.client.post('/api/v2/sessions',json=body,headers={'Idempotency-Key':'unknown-after-change'})
            self.assertEqual(response.json()['phase'],'unconfirmed')


if __name__=='__main__': unittest.main()
