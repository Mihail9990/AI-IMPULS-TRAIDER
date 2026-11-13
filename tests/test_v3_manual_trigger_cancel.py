"""Cancellation uses fake broker evidence only, never a real order connection."""
from copy import deepcopy
from unittest.mock import patch
import unittest
import test_recovery_v3 as fixtures
from tradfi.control import ActionRejected,ActionGateway
from tradfi.recovery_v3 import ControlledRecoveryController

class ManualTriggerCancel(unittest.TestCase):
    def setUp(self):
        self.x=fixtures.V3();self.x.setUp();self.addCleanup(self.x.doCleanups)
        self.x.loss();self.f=self.x.f
    def pending(self):
        self.x.entry('STOP','current',count=3)
        target=self.f.bot.state['cycle']['v3_request']['bar_id']
        self.f.b.data.update(now=target+60,candle=dict(id=target,open='4001',close='4000'))
        self.f.b.quote('3999.7','3999.8');self.f.tick(3)
        self.assertEqual(len(self.f.b.data['orders']),3)
    def cancel(self):self.f.bot.action(self.x.a('v3_cancel_entry'))
    def test_cancel_consumed_schedule_and_repeat_gateway(self):
        self.pending();f=self.f;debt=f.bot.state['cycle']['recovery']
        self.assertNotIn('v3_request',f.bot.state['cycle'])
        self.assertTrue(f.bot.v3_view(f.bot.snapshot())['v3_controls']['can_cancel_entry'])
        gateway=ActionGateway(f.bot,execute=f.bot.action)
        request=dict(id='manual-cancel',revision=f.bot.state.get('control_revision',0),action=self.x.a('v3_cancel_entry'))
        self.assertEqual(gateway.apply(request)['status'],'APPLIED')
        self.assertEqual(gateway.apply(request)['status'],'APPLIED');f.tick(5)
        self.assertFalse(f.b.data['orders']);self.assertFalse(f.b.data['positions'])
        self.assertEqual(f.bot.state['cycle']['recovery'],debt)
        self.assertEqual(f.bot.state['phase'],'V3_WAIT_ENTRY')
        self.assertFalse(f.bot.v3_view(f.bot.snapshot())['v3_controls']['can_cancel_entry'])
        before=len(f.b.commands);f.tick(3);self.assertEqual(len(f.b.commands),before)
    def test_restart_finishes_durable_cancel(self):
        self.pending();f=self.f
        with patch.object(f.bot,'_cancel'):self.cancel()
        self.assertTrue(f.bot.state['cycle']['groups'][-1]['cancel_entry'])
        f.bot=ControlledRecoveryController(f.store,f.b);f.tick(5)
        self.assertFalse(f.b.data['orders']);self.assertEqual(f.bot.state['phase'],'V3_WAIT_ENTRY')
    def test_already_filled_subset_preserves_position_and_exact_profile(self):
        self.pending();f=self.f;group=f.bot.state['cycle']['groups'][-1]
        profile=deepcopy(group['recovery_profile'])
        # Fill the middle stage, so renumbering or shifting its TP is detectable.
        f.b.data['orders'][0],f.b.data['orders'][1]=f.b.data['orders'][1],f.b.data['orders'][0]
        f.b.fill('SELL');before=deepcopy(f.b.data['positions'][0])
        f.b.quote(str(float(before['entry'])-.05),str(float(before['entry'])+.05))
        self.cancel();f.tick(6)
        self.assertFalse(f.b.data['orders']);self.assertEqual(len(f.b.data['positions']),1)
        self.assertFalse(f.bot.state['cycle'].get('manual'))
        self.assertEqual(group['recovery_profile'],profile)
        after=f.b.data['positions'][0]
        self.assertEqual((after['id'],after['sl'],after['tp']),(before['id'],before['sl'],before['tp']))
        self.assertEqual(next(t['index'] for t in f.bot.state['cycle']['tickets'].values() if t['id']==after['id']),1)
    def test_readonly_stale_cycle_extra_fields_rejected(self):
        self.pending();f=self.f;before=deepcopy(f.b.data['orders'])
        for action in [dict(type='v3_cancel_entry',cycle_id='stale'),dict(self.x.a('v3_cancel_entry'),other=True)]:
            with self.assertRaises(ActionRejected):f.bot.action(action)
        f.b.read_only=True
        with self.assertRaises(ActionRejected):self.cancel()
        self.assertFalse(f.bot.v3_view(f.bot.snapshot())['v3_controls']['can_cancel_entry'])
        self.assertEqual(f.b.data['orders'],before)
    def test_no_pending_does_not_close_market_positions(self):
        self.x.entry();f=self.f;before=deepcopy(f.b.data['positions'])
        with self.assertRaises(ActionRejected):self.cancel()
        self.assertEqual(f.b.data['positions'],before)
    def test_linked_batch_has_separate_cancel_owner(self):
        self.x.entry();self.x.link();f=self.f;before=deepcopy(f.b.data['orders'])
        self.assertFalse(f.bot.v3_view(f.bot.snapshot())['v3_controls']['can_cancel_entry'])
        with self.assertRaises(ActionRejected):self.cancel()
        self.assertEqual(f.b.data['orders'],before)
    def test_unknown_cancel_never_duplicates_or_reenters(self):
        self.pending();f=self.f;f.b.fail_before.add('CANCEL');self.cancel()
        count=len(f.b.commands);f.tick(5)
        self.assertEqual(len(f.b.commands),count)
        self.assertTrue(f.b.data['orders'])
        self.assertTrue(f.bot.state['cycle']['groups'][-1]['cancel_entry'])
        self.assertFalse(f.bot.v3_view(f.bot.snapshot())['v3_controls']['can_enter'])
    def test_initial_trigger_is_not_owned_by_manual_cancel(self):
        x=fixtures.V3();x.setUp();self.addCleanup(x.doCleanups);x.f.start()
        before=deepcopy(x.f.b.data['orders'])
        with self.assertRaises(ActionRejected):x.f.bot.action(x.a('v3_cancel_entry'))
        self.assertEqual(x.f.b.data['orders'],before)
    def test_prepared_not_sent_stop_cancelled_without_dispatch(self):
        self.pending();f=self.f
        # A separate fresh fixture catches cancellation between durable prepare
        # and dispatch, rather than fabricating a READY broker order.
        self.cancel();f.tick(4)
        self.x.entry('STOP','current',count=3)
        target=f.bot.state['cycle']['v3_request']['bar_id']
        f.b.data.update(now=target+60,candle=dict(id=target,open='4001',close='4000'))
        with patch.object(f.bot,'dispatch'):f.tick()
        ids=f.bot.state['cycle']['groups'][-1]['commands']
        self.assertTrue(all(f.bot.state['commands'][cid]['status']=='READY' for cid in ids))
        self.cancel();f.tick(4)
        self.assertFalse(f.b.data['orders']);self.assertFalse(f.b.data['positions'])
        self.assertTrue(all(f.bot.state['commands'][cid]['status']=='CANCELLED' for cid in ids))
    def test_broker_fills_during_cancel_dispatch(self):
        self.pending();f=self.f
        expected=deepcopy(f.b.data['orders'][0])
        f.b.cancel_race='SELL'
        self.cancel()  # Broker consumes cancel intent and fills instead.
        self.assertEqual(len(f.b.data['positions']),1)
        self.assertTrue(any(cmd['kind']=='CANCEL' and f.b.results[cmd['id']].get('status')=='UNKNOWN' for cmd in f.b.commands))
        f.b.quote(str(float(expected['price'])-.05),str(float(expected['price'])+.05))
        f.tick(6)
        self.assertFalse(f.b.data['orders']);self.assertFalse(f.bot.state['cycle'].get('manual'))
        self.assertEqual(len(f.b.data['positions']),1)
        position=f.b.data['positions'][0]
        self.assertEqual((position['sl'],position['tp']),(expected['sl'],expected['tp']))
        self.assertFalse(f.bot.state['cycle']['groups'][-1].get('cancel_entry'))
        before=len(f.b.commands);f.tick(3);self.assertEqual(len(f.b.commands),before)
    def test_partial_subset_cancel_survives_restart_without_reindexing(self):
        self.pending();f=self.f
        f.b.data['orders'][0],f.b.data['orders'][2]=f.b.data['orders'][2],f.b.data['orders'][0]
        f.b.fill('SELL');before=deepcopy(f.b.data['positions'][0])
        f.b.quote(str(float(before['entry'])-.05),str(float(before['entry'])+.05));f.tick(3)
        with patch.object(f.bot,'_cancel'):self.cancel()
        f.bot=ControlledRecoveryController(f.store,f.b);f.tick(6)
        self.assertFalse(f.b.data['orders']);self.assertEqual(len(f.b.data['positions']),1)
        self.assertFalse(f.bot.state['cycle'].get('manual'))
        self.assertEqual(next(t['index'] for t in f.bot.state['cycle']['tickets'].values() if t['id']==before['id']),2)
        after=f.b.data['positions'][0];self.assertEqual((after['sl'],after['tp']),(before['sl'],before['tp']))
    def test_partial_volume_requires_review_preserves_broker_protection(self):
        self.pending();f=self.f;f.b.fill('SELL',size='0.005')
        before=deepcopy(f.b.data['positions'][0]);self.cancel();f.tick(5)
        self.assertTrue(f.bot.state['cycle'].get('manual'))
        self.assertEqual(f.b.data['positions'][0],before)
        self.assertFalse(f.bot.v3_view(f.bot.snapshot())['v3_controls']['can_enter'])
