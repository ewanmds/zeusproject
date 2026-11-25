"""Targeted volume fixes against the restored release; no exchange I/O."""
from collections import deque
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from test_outcome_maker_exit import make_bot
from zeus.bots.worldcup import MarketSpec, MarketState
from zeus.outcome_rotation import OutcomeRotation
from zeus.safety import SafetyLatch


class FlatRotationTests(unittest.TestCase):
    def fixture(self, folder, **overrides):
        now = time.time()
        row = dict(session_id='one', market='#101', running=True, phase='running',
                   cleanup_confirmed=False, stale=False, position_known=True,
                   position_usd=0.0, volume_usd=206, fills=5,
                   execution_state='Bid working', last_fill_at=now-100)
        row.update(overrides)
        owner = SimpleNamespace(procs={'one': {'stop_file': str(Path(folder)/'stop')}},
                                status=Mock(return_value={'bots': [row]}), start=Mock())
        rotation = OutcomeRotation(owner)
        with patch.object(rotation, '_loop'):
            rotation.register({'session_id': 'one'}, dict(selection_mode='auto', auto_rotate=True,
                order_size_usd=70, outcome_side='no', auto_rotation_after_sec=60), group_id='group')
        group = rotation.groups['group']
        group['started'] = now-300
        group['samples'] = deque([(now-65, 200)])
        return rotation, owner, row, now

    def scan(self, now):
        return dict(ready=True, updated_at=now,
                    candidates=[dict(market='#111', queue_proxy_sec=10)])

    def test_slow_volume_with_inventory_keeps_trading_without_forced_exit(self):
        with tempfile.TemporaryDirectory() as folder:
            rotation, owner, row, now = self.fixture(folder, position_usd=73.70)
            with patch('zeus.outcome_flow.scanner.snapshot') as scan:
                rotation.tick(now=now)
            scan.assert_not_called()
            self.assertFalse((Path(folder)/'drain.json').exists())
            self.assertEqual(rotation.groups['group']['state'], 'observing')
            self.assertFalse(rotation.groups['group']['cancelled'])
            owner.start.assert_not_called()
            rotation.close()

    def test_unknown_or_invalid_position_never_authorizes_rotation(self):
        for value, known in ((None, True), (0, False), (float('nan'), True), (False, True)):
            with self.subTest(position=value, known=known), tempfile.TemporaryDirectory() as folder:
                rotation, _, _, now = self.fixture(folder, position_usd=value, position_known=known)
                with patch('zeus.outcome_flow.scanner.snapshot') as scan:
                    rotation.tick(now=now)
                scan.assert_not_called()
                self.assertFalse((Path(folder)/'drain.json').exists())
                rotation.close()

    def test_flat_worker_can_still_rotate_and_only_requests_order_cleanup(self):
        with tempfile.TemporaryDirectory() as folder:
            rotation, owner, _, now = self.fixture(folder)
            with patch('zeus.outcome_flow.scanner.snapshot', return_value=self.scan(now)):
                rotation.tick(now=now)
            drain = json.loads((Path(folder)/'drain.json').read_text())
            self.assertEqual(drain['next_market'], '#111')
            self.assertEqual(rotation.groups['group']['state'], 'draining')
            self.assertFalse((Path(folder)/'stop').exists())
            owner.start.assert_not_called()  # old worker has not confirmed cleanup
            rotation.close()

    def test_zero_fill_leg_moves_to_the_best_free_market(self):
        with tempfile.TemporaryDirectory() as folder:
            rotation, _, _, now = self.fixture(folder, volume_usd=0, fills=0)
            scan = dict(ready=True, updated_at=now, candidates=[
                dict(market='#111', queue_proxy_sec=10, volume_usd_1m=900, rank=1),
                dict(market='#101', queue_proxy_sec=80, volume_usd_1m=200, rank=4),
            ])
            with patch('zeus.outcome_flow.scanner.snapshot', return_value=scan):
                rotation.tick(now=now)
            drain = json.loads((Path(folder)/'drain.json').read_text())
            self.assertEqual(drain['next_market'], '#111')
            group = rotation.groups['group']
            self.assertEqual(group['state'], 'draining')
            self.assertIn('#4', group['reason'])
            self.assertIn('#1', group['reason'])
            self.assertEqual(group['market_rank'], 4)
            self.assertEqual(group['best_rank'], 1)
            rotation.close()

    def test_zero_fill_grace_uses_the_queue_estimate_not_the_full_window(self):
        with tempfile.TemporaryDirectory() as folder:
            rotation, _, _, now = self.fixture(folder, volume_usd=0, fills=0)
            group = rotation.groups['group']
            group['current_selection'] = {'queue_proxy_sec': 40}  # grace = 50 s
            group['started'] = now-40
            scan = dict(ready=True, updated_at=now, candidates=[
                dict(market='#111', queue_proxy_sec=10, volume_usd_1m=900, rank=1),
                dict(market='#101', queue_proxy_sec=80, volume_usd_1m=200, rank=4),
            ])
            with patch('zeus.outcome_flow.scanner.snapshot', return_value=scan):
                rotation.tick(now=now)
            self.assertFalse((Path(folder)/'drain.json').exists())
            group['started'] = now-51
            scan['updated_at'] = now+1
            with patch('zeus.outcome_flow.scanner.snapshot', return_value=scan):
                rotation.tick(now=now+1)
            drain = json.loads((Path(folder)/'drain.json').read_text())
            self.assertEqual(drain['next_market'], '#111')
            rotation.close()

    def test_zero_fill_leg_already_on_the_best_market_does_not_churn(self):
        with tempfile.TemporaryDirectory() as folder:
            rotation, _, _, now = self.fixture(folder, volume_usd=0, fills=0)
            scan = dict(ready=True, updated_at=now, candidates=[
                dict(market='#101', queue_proxy_sec=10, volume_usd_1m=900, rank=1),
                dict(market='#111', queue_proxy_sec=80, volume_usd_1m=200, rank=2),
            ])
            with patch('zeus.outcome_flow.scanner.snapshot', return_value=scan):
                rotation.tick(now=now)
            self.assertFalse((Path(folder)/'drain.json').exists())
            self.assertEqual(rotation.groups['group']['state'], 'observing')
            self.assertIn('déjà', rotation.groups['group']['reason'])
            rotation.close()

    def test_zero_fill_leg_waits_when_every_better_market_is_busy(self):
        with tempfile.TemporaryDirectory() as folder:
            rotation, owner, row, now = self.fixture(folder, volume_usd=0, fills=0)
            other = dict(row, session_id='two', market='#111')
            owner.status = Mock(return_value={'bots': [row, other]})
            scan = dict(ready=True, updated_at=now, candidates=[
                dict(market='#111', queue_proxy_sec=10, volume_usd_1m=900, rank=1),
                dict(market='#101', queue_proxy_sec=80, volume_usd_1m=200, rank=2),
            ])
            with patch('zeus.outcome_flow.scanner.snapshot', return_value=scan):
                rotation.tick(now=now)
            self.assertFalse((Path(folder)/'drain.json').exists())
            self.assertEqual(rotation.groups['group']['state'], 'observing')
            self.assertIn('indisponible', rotation.groups['group']['reason'])
            rotation.close()

    def test_filling_market_is_drained_when_its_volume_rank_decays(self):
        with tempfile.TemporaryDirectory() as folder:
            rotation, owner, row, now = self.fixture(folder)
            group = rotation.groups['group']
            group['auto_min_fill_usd_per_min'] = 1
            group['config']['auto_min_fill_usd_per_min'] = 1
            scan = dict(ready=True, updated_at=now, candidates=[
                dict(market='#111', queue_proxy_sec=10, volume_usd_1m=900, rank=1),
                dict(market='#101', queue_proxy_sec=80, volume_usd_1m=200, rank=4),
            ])
            with patch('zeus.outcome_flow.scanner.snapshot', return_value=scan):
                rotation.tick(now=now)
            drain = json.loads((Path(folder)/'drain.json').read_text())
            self.assertEqual(drain['next_market'], '#111')
            self.assertIn('volume', rotation.groups['group']['reason'])
            self.assertEqual(rotation.groups['group']['state'], 'draining')
            rotation.close()

    def test_fill_during_slow_scan_cancels_the_rotation_decision(self):
        with tempfile.TemporaryDirectory() as folder:
            rotation, _, row, now = self.fixture(folder)
            def scan(*args, **kwargs):
                row['position_usd'] = 20
                return self.scan(now)
            with patch('zeus.outcome_flow.scanner.snapshot', side_effect=scan):
                rotation.tick(now=now)
            self.assertFalse((Path(folder)/'drain.json').exists())
            self.assertEqual(rotation.groups['group']['state'], 'observing')
            rotation.close()

    def test_manual_stop_during_scan_still_prevents_replacement(self):
        with tempfile.TemporaryDirectory() as folder:
            rotation, _, _, now = self.fixture(folder)
            def scan(*args, **kwargs):
                rotation.cancel('group')
                return self.scan(now)
            with patch('zeus.outcome_flow.scanner.snapshot', side_effect=scan):
                rotation.tick(now=now)
            self.assertFalse((Path(folder)/'drain.json').exists())
            self.assertTrue(rotation.groups['group']['cancelled'])
            rotation.close()

    def test_unexpected_clean_exit_relaunches_instead_of_ending_the_group(self):
        with tempfile.TemporaryDirectory() as folder:
            rotation, owner, row, now = self.fixture(folder)
            row.update(running=False, phase='failed', cleanup_confirmed=True, stale=False)
            with patch('zeus.outcome_flow.scanner.snapshot', return_value=self.scan(now)):
                rotation.tick(now=now)
            group = rotation.groups['group']
            self.assertFalse(group['cancelled'])
            owner.start.assert_called_once()
            request = owner.start.call_args.args[0]
            self.assertEqual(request.market, '#111')
            self.assertEqual(owner.start.call_args.kwargs['rotation_group'], 'group')
            self.assertEqual(group['state'], 'observing')
            self.assertEqual(group['rotations'], 1)
            rotation.close()

    def test_unconfirmed_crash_blocks_without_cancelling_then_resumes(self):
        with tempfile.TemporaryDirectory() as folder:
            rotation, owner, row, now = self.fixture(folder)
            row.update(running=False, phase='failed', cleanup_confirmed=False, stale=False)
            with patch('zeus.outcome_flow.scanner.snapshot') as scan:
                rotation.tick(now=now)
            scan.assert_not_called()
            owner.start.assert_not_called()
            group = rotation.groups['group']
            self.assertFalse(group['cancelled'])
            self.assertEqual(group['state'], 'blocked')
            self.assertIn('non confirmé', group['reason'])
            # The delayed exchange confirmation releases the same logical group.
            row.update(cleanup_confirmed=True)
            with patch('zeus.outcome_flow.scanner.snapshot', return_value=self.scan(now)):
                rotation.tick(now=now)
            owner.start.assert_called_once()
            self.assertFalse(group['cancelled'])
            rotation.close()

    def test_manual_exit_is_terminal_and_is_never_relaunched(self):
        with tempfile.TemporaryDirectory() as folder:
            rotation, owner, row, now = self.fixture(folder)
            (Path(folder)/'drain.json').write_text(json.dumps(
                {'manual': True, 'reason': 'Manual stop: maker exit'}))
            row.update(running=False, phase='failed', cleanup_confirmed=False, stale=False)
            rotation.tick(now=now)
            group = rotation.groups['group']
            self.assertEqual(group['state'], 'finishing')
            self.assertFalse(group['cancelled'])
            owner.start.assert_not_called()
            row.update(cleanup_confirmed=True)
            rotation.tick(now=now)
            self.assertTrue(group['cancelled'])
            owner.start.assert_not_called()
            rotation.close()

    def test_stale_launching_state_returns_to_observing(self):
        with tempfile.TemporaryDirectory() as folder:
            rotation, owner, row, now = self.fixture(folder)
            group = rotation.groups['group']
            group.update(state='launching', retry_at=now-1)
            rotation.tick(now=now)
            self.assertEqual(group['state'], 'observing')
            owner.start.assert_not_called()
            rotation.close()

    def test_recovered_exit_only_leg_stays_terminal(self):
        with tempfile.TemporaryDirectory() as folder:
            rotation, owner, row, now = self.fixture(folder)
            row.update(running=False, phase='failed', cleanup_confirmed=True, stale=False,
                       config={'resume_exit': True, 'resume_trading': False})
            rotation.tick(now=now)
            group = rotation.groups['group']
            self.assertFalse(group['cancelled'])
            self.assertEqual(group['state'], 'finishing')
            owner.start.assert_not_called()
            rotation.close()

    def test_blocked_group_follows_a_recovered_worker_instead_of_draining(self):
        with tempfile.TemporaryDirectory() as folder:
            rotation, owner, row, now = self.fixture(folder)
            group = rotation.groups['group']
            group['state'] = 'blocked'
            group['reason'] = 'Rotation blocked: interruption, arrêt non confirmé'
            with patch('zeus.outcome_flow.scanner.snapshot') as scan:
                rotation.tick(now=now)
            scan.assert_not_called()
            self.assertEqual(group['state'], 'observing')
            self.assertFalse((Path(folder)/'drain.json').exists())
            owner.start.assert_not_called()
            rotation.close()

    def test_repeated_immediate_crash_backs_off_instead_of_looping(self):
        with tempfile.TemporaryDirectory() as folder:
            rotation, owner, row, now = self.fixture(folder)
            row.update(running=False, phase='failed', cleanup_confirmed=True, stale=False)
            with patch('zeus.outcome_flow.scanner.snapshot', return_value=self.scan(now)):
                rotation.tick(now=now)
            self.assertEqual(owner.start.call_count, 1)
            group = rotation.groups['group']
            self.assertEqual(group['resume_failures'], 1)
            with patch('zeus.outcome_flow.scanner.snapshot', return_value=self.scan(now+1)):
                rotation.tick(now=now+1)
            self.assertEqual(owner.start.call_count, 1)
            self.assertEqual(group['state'], 'draining')
            self.assertGreater(group['retry_at'], now+1)
            rotation.close()


class PartialMakerVolumeTests(unittest.TestCase):
    def fixture(self, quantity):
        bot = make_bot(order_size_usd=70, max_position_usd=120)
        bot.available_usdc = 31.86
        st = MarketState(MarketSpec('#101', 0, balance_coin='+101'),
                         position=quantity, available_position=quantity)
        st.update_bbo(.5, .5001)
        return bot, st

    def test_partial_inventory_is_offered_without_waiting_for_a_full_order(self):
        bot, st = self.fixture(40)  # $20, below the previous $63 threshold
        plan = bot.bilateral_requests(st)
        self.assertEqual(plan[False]['sz'], 40)
        self.assertEqual(plan[False]['limit_px'], st.ask)
        self.assertEqual(plan[False]['order_type']['limit']['tif'], 'Alo')
        self.assertNotIn(True, plan)  # $31.86 is not permission to resize a $70 buy
        self.assertEqual(bot.cfg.order_usd, 70)
        self.assertEqual(bot.cfg.max_position_usd, 120)

    def test_sub_minimum_new_sale_is_not_submitted_but_existing_residual_keeps_queue(self):
        bot, st = self.fixture(10)  # about $5
        self.assertNotIn(False, bot.bilateral_requests(st))
        st.ask_oid, st.ask_quantity, st.quoted_ask = 123, 10, .5002
        st.available_position = 0
        order = bot.bilateral_requests(st)[False]
        self.assertEqual(order['sz'], 10)
        self.assertEqual(order['limit_px'], .5002)

    def test_executable_partial_ask_tracks_touch_instead_of_sticking_at_old_price(self):
        bot, st = self.fixture(40)
        st.ask_oid, st.ask_quantity, st.quoted_ask = 123, 40, .5002
        st.available_position = 0
        self.assertEqual(bot.bilateral_requests(st)[False]['limit_px'], st.ask)

    def test_available_tokens_and_order_size_bound_the_sale(self):
        for quantity, free in ((40, 20), (400, 400)):
            with self.subTest(quantity=quantity):
                bot, st = self.fixture(quantity)
                st.available_position = free
                order = bot.bilateral_requests(st)[False]
                self.assertLessEqual(order['sz'], free)
                self.assertLessEqual(order['sz']*order['limit_px'], 70)

    def test_resting_partial_ask_at_touch_is_not_cancelled_or_duplicated(self):
        bot, st = self.fixture(40)
        bot.safety = SafetyLatch(strict_maker_spot=True)
        bot.safety.phase = 'running'
        bot.running = True
        bot.exchange = SimpleNamespace(bulk_cancel=Mock(), order=Mock())
        bot.can_send_action = Mock(return_value=True)
        st.ask_oid, st.ask_quantity, st.quoted_ask = 123, 40, st.ask
        st.available_position = 0
        bot.place_bilateral(st)
        bot.exchange.bulk_cancel.assert_not_called()
        bot.exchange.order.assert_not_called()

    def test_manual_exit_and_stop_loss_remain_sell_only_and_passive(self):
        bot, st = self.fixture(40)
        bot.exit_reason = 'Manual stop: maker exit'
        bot.stats.gross_closed_pnl = -60
        plan = bot.bilateral_requests(st)
        self.assertNotIn(True, plan)
        self.assertEqual(plan[False]['sz'], 40)
        self.assertEqual(plan[False]['order_type']['limit']['tif'], 'Alo')
