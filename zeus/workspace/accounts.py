"""Immutable execution identity and Hyperliquid agent onboarding."""
from dataclasses import dataclass, field
import secrets
import math
import time

from eth_account import Account
from eth_account.messages import encode_typed_data
import requests


@dataclass(frozen=True)
class AccountContext:
    owner: str
    account: str
    agent: str
    private_key: str = field(repr=False)

    def environment(self):
        return {'WALLET_ADDRESS': self.account, 'WALLET_ADDRESS_OUTCOME': self.account,
                'PRIVATE_KEY': self.private_key, 'PRIVATE_KEY_OUTCOME': self.private_key}


DOMAIN = {'name': 'HyperliquidSignTransaction', 'version': '1', 'chainId': 42161,
          'verifyingContract': '0x0000000000000000000000000000000000000000'}
DOMAIN_FIELDS = [{'name': 'name', 'type': 'string'}, {'name': 'version', 'type': 'string'},
                 {'name': 'chainId', 'type': 'uint256'}, {'name': 'verifyingContract', 'type': 'address'}]
FIELDS = [{'name': 'hyperliquidChain', 'type': 'string'}, {'name': 'agentAddress', 'type': 'address'},
          {'name': 'agentName', 'type': 'string'}, {'name': 'nonce', 'type': 'uint64'}]


def typed_approval(action):
    return {'domain': DOMAIN, 'primaryType': 'HyperliquidTransaction:ApproveAgent',
            'types': {'EIP712Domain': DOMAIN_FIELDS, 'HyperliquidTransaction:ApproveAgent': FIELDS},
            'message': {f['name']: action[f['name']] for f in FIELDS}}


class HyperliquidAccounts:
    def __init__(self, store, post=None):
        self.store, self.post = store, post or self._post

    @staticmethod
    def _post(path, data):
        r = requests.post('https://api.hyperliquid.xyz'+path, json=data, timeout=8)
        r.raise_for_status(); return r.json()

    def prepare(self, owner):
        old = self.store.agent(owner)
        if old and old['expires'] > time.time()+300:
            if old['status'] not in {'ready', 'approval_pending'} and time.time()*1000-old['approval']['nonce'] > 240000:
                private = self.store.agent(owner, secret=True)
                action = {**old['approval'], 'nonce': int(time.time()*1000)}
                self.store.save_agent(owner, old['account'], old['address'], private['private_key'],
                                      old['name'], action, old['expires'])
                old = self.store.agent(owner)
            return {**old, 'typed_data': typed_approval(old['approval'])}
        agent = Account.create()
        expiry = int(time.time()+30*86400)
        name = 'Zeus-'+secrets.token_hex(3)+' valid_until '+str(expiry*1000)
        action = {'type': 'approveAgent', 'hyperliquidChain': 'Mainnet', 'signatureChainId': '0xa4b1',
                  'agentAddress': agent.address, 'agentName': name, 'nonce': int(time.time()*1000)}
        self.store.save_agent(owner, owner, agent.address, agent.key.hex(), name, action, expiry)
        row = self.store.agent(owner)
        return {**row, 'typed_data': typed_approval(action)}

    def verify(self, owner):
        row = self.store.agent(owner)
        if not row: return None
        agents = self.post('/info', {'type': 'extraAgents', 'user': row['account']})
        if not isinstance(agents, list): raise ValueError('Invalid agent verification response')
        valid = any(str(a.get('address', '')).lower() == row['address'].lower()
                    and float(a.get('validUntil', 0)) > time.time()*1000 for a in agents)
        status = ('ready' if valid and row['expires'] > time.time() else
                  'approval_pending' if row['status'] == 'approval_pending' and row['expires'] > time.time()
                  else 'authorization_required')
        self.store.agent_status(owner, status)
        return {**row, 'status': status}

    def approve(self, owner, signature):
        row = self.store.agent(owner)
        if not row or row['expires'] <= time.time(): raise ValueError('Prepare an agent first')
        typed = typed_approval(row['approval'])
        signer = Account.recover_message(encode_typed_data(full_message=typed), signature=signature)
        if signer.lower() != owner: raise ValueError('Approval signer does not match the connected wallet')
        parsed = bytes.fromhex(signature.removeprefix('0x'))
        if len(parsed) != 65: raise ValueError('Invalid signature')
        sig = {'r': '0x'+parsed[:32].hex(), 's': '0x'+parsed[32:64].hex(), 'v': parsed[64]}
        # Record uncertain delivery before relay. Never automatically replay an approval.
        if not self.store.reserve_approval(owner): return self.verify(owner)
        result = self.post('/exchange', {'action': row['approval'], 'nonce': row['approval']['nonce'], 'signature': sig})
        if result.get('status') != 'ok':
            self.store.agent_status(owner, 'authorization_required')
            raise ValueError('Hyperliquid rejected the agent authorization')
        return self.verify(owner)

    def context(self, owner):
        row = self.verify(owner)
        if not row or row['status'] != 'ready': raise ValueError('Authorize your trading agent first')
        private = self.store.agent(owner, secret=True)
        if Account.from_key(private['private_key']).address.lower() != row['address'].lower():
            raise ValueError('Agent identity mismatch')
        return AccountContext(owner, row['account'], row['address'], private['private_key'])

    def balances(self, owner):
        data = self.post('/info', {'type': 'spotClearinghouseState', 'user': owner})
        balances = data.get('balances')
        if not isinstance(balances, list): raise ValueError('Account balance unavailable')
        usd = next((b for b in balances if b.get('coin') == 'USDC'), {})
        total, hold = float(usd.get('total', 0)), float(usd.get('hold', 0))
        if not all(math.isfinite(v) for v in (total, hold)) or total < 0 or not 0 <= hold <= total+1e-9:
            raise ValueError('Account balance is not verified')
        return {'usdc_total': total, 'usdc_available': max(0, total-hold)}
