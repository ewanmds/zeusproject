"""Exercise real supervisor admission with fake Popen; never run trading children."""
import os
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from eth_account import Account
from zeus.server import BotManager, WorldcupStartRequest, GridStartRequest
from zeus.workspace.accounts import AccountContext
from zeus import PROJECT_ROOT


class TenantProcessTests(unittest.TestCase):
    def test_same_outcome_has_separate_credentials_roots_and_admission(self):
        with tempfile.TemporaryDirectory() as folder:
            accounts=[Account.create(),Account.create()]
            managers=[BotManager(Path(folder)/str(i),AccountContext(a.address.lower(),a.address,a.address,a.key.hex())) for i,a in enumerate(accounts)]
            old_env=dict(os.environ)
            processes=[Mock(pid=10001+i,poll=Mock(return_value=None)) for i in range(2)]
            with patch('zeus.outcomes.resolve_market',side_effect=lambda key:{'key':key,'name':'Offline'}), \
                 patch('zeus.outcome_supervisor.subprocess.Popen',side_effect=processes) as spawn:
                first=managers[0].start_worldcup(WorldcupStartRequest(market='#101',exec_mode='maker'))
                second=managers[1].start_worldcup(WorldcupStartRequest(market='#101',exec_mode='maker'))
                self.assertNotEqual(first['session_id'],second['session_id'])
                with self.assertRaises(RuntimeError):managers[0].start_worldcup(WorldcupStartRequest(market='#101',exec_mode='maker'))
                for i,call in enumerate(spawn.call_args_list):
                    env=call.kwargs['env'];self.assertTrue(env['PRIVATE_KEY_OUTCOME']==accounts[i].key.hex())
                    self.assertEqual(env['WALLET_ADDRESS_OUTCOME'],accounts[i].address)
                    self.assertEqual(env['ZEUS_SIGNING_ROOT'],str(PROJECT_ROOT / '.zeus-state' / 'signing'))
                    self.assertNotIn(accounts[i].key.hex(),' '.join(call.args[0]))
                self.assertNotEqual(spawn.call_args_list[0].args[0][-1],spawn.call_args_list[1].args[0][-1])
            self.assertEqual(hashlib.sha256(json.dumps(dict(os.environ),sort_keys=True).encode()).hexdigest(),
                             hashlib.sha256(json.dumps(old_env,sort_keys=True).encode()).hexdigest())
            for m in managers:m.outcome.rotation.close()

    def test_grid_files_and_locks_are_account_scoped_without_importing_signer(self):
        with tempfile.TemporaryDirectory() as folder:
            accounts=[Account.create(),Account.create()]
            managers=[BotManager(Path(folder)/str(i),AccountContext(a.address.lower(),a.address,a.address,a.key.hex())) for i,a in enumerate(accounts)]
            with patch('zeus.markets.resolve_symbol',return_value='xyz:SP500'), \
                 patch('zeus.server.subprocess.Popen',side_effect=[Mock(pid=101,poll=Mock(return_value=None)),Mock(pid=102,poll=Mock(return_value=None))]) as spawn, \
                 patch('zeus.server.time.sleep'):
                for manager in managers:manager.start_grid(GridStartRequest(token='xyz:SP500',strategy='bbo',mode='live',order_size_usd=25))
                paths=[next(iter(m.obi_procs.values())) for m in managers]
                self.assertNotEqual(paths[0]['stop_file'],paths[1]['stop_file'])
                self.assertNotEqual(paths[0]['stats_file'],paths[1]['stats_file'])
                for i,call in enumerate(spawn.call_args_list):
                    self.assertTrue(call.kwargs['env']['PRIVATE_KEY']==accounts[i].key.hex())
                    self.assertTrue(paths[i]['stats_file'].startswith(call.kwargs['env']['ZEUS_RUNTIME_DIR']))
            for m in managers:m.outcome.rotation.close()


if __name__=='__main__':unittest.main()
