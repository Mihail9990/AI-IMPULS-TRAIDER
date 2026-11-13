"""Explicit pause reset never trades or erases accounting; fake MT5 only."""
from copy import deepcopy
from decimal import Decimal as D
from unittest.mock import patch
import unittest
import test_tradfi_basket as fixtures
from tradfi.control import ActionRejected
from tradfi.storage import DurabilityError

class PauseResetTest(unittest.TestCase):
    setUp=fixtures.NativeBasketTest.setUp
    tearDown=fixtures.NativeBasketTest.tearDown
    restart=fixtures.NativeBasketTest.restart
    tick=fixtures.NativeBasketTest.tick
    initial=fixtures.NativeBasketTest.initial

    def closed_pause(self,profit=-1.25):
        self.initial()
        # Fixture: reversals are cancelled and positively reconciled before pause.
        c=self.bot.state['cycle'];self.bot._cancel(self.bot.snapshot(),c['entries']);self.bot.dispatch({'CANCEL'})
        self.bot._reconcile_commands(self.bot.snapshot())
        self.bot.pause('UNOWNED_BROKER_OBLIGATIONS')
        self.bot.state['stop']=True;self.bot.save('fixture_stop')
        self.api.close_position(reason=self.api.DEAL_REASON_SL,profit=profit)
        self.tick()
        self.assertTrue(all(t['closed'] for t in self.bot.state['cycle']['tickets'].values()))

    def reset(self):
        with patch.object(self.api,'order_send',side_effect=AssertionError('no send')), \
             patch.object(self.api,'order_check',side_effect=AssertionError('no check')), \
             patch.object(self.bot,'dispatch',side_effect=AssertionError('no dispatch')):
            self.bot.command('/reset_pause')

    def test_closed_flat_reset_keeps_loss_and_stays_stopped(self):
        self.closed_pause();before=len(self.bot.state['seen_deals']);self.reset()
        self.assertEqual(self.bot.state['phase'],'WAIT_START');self.assertTrue(self.bot.state['stop'])
        self.assertIsNone(self.bot.state['cycle']);self.assertEqual(D(self.bot.state['basket_carry']),D('1.25'))
        self.assertEqual(len(self.bot.state['seen_deals']),before)
        self.assertEqual(self.bot.state['completed'][-1]['completion'],'PAUSE_RESET_RETAINED')
        self.tick(3);self.assertEqual(self.bot.state['phase'],'WAIT_START')
        self.assertFalse(self.api.positions);self.assertFalse(self.api.orders)

    def test_open_ticket_rejected(self):
        self.initial();self.bot.pause('fixture');before=deepcopy(self.bot.state)
        with self.assertRaises(ActionRejected):self.reset()
        self.assertEqual(before,self.bot.state)

    def test_unbooked_closure_rejected(self):
        self.initial();self.bot.pause('fixture');self.api.close_position(reason=self.api.DEAL_REASON_SL)
        before=deepcopy(self.bot.state)
        with self.assertRaises(ActionRejected):self.reset()
        self.assertEqual(before,self.bot.state)

    def test_pending_command_rejected(self):
        self.closed_pause();cmd=next(iter(self.bot.state['commands'].values()));cmd['status']='UNKNOWN'
        self.bot.save('fixture_unknown');before=deepcopy(self.bot.state)
        with self.assertRaises(ActionRejected):self.reset()
        self.assertEqual(before,self.bot.state)

    def test_current_foreign_or_owned_obligation_rejected(self):
        self.closed_pause();snap=self.bot.snapshot(for_reset=True)
        for field in ('positions','orders'):
            for owned in (True,False):
                bad=deepcopy(snap);bad[field].append(dict(id='new',owned=owned))
                before=deepcopy(self.bot.state)
                with patch.object(self.bot,'snapshot',return_value=bad),self.assertRaises(ActionRejected):self.reset()
                self.assertEqual(before,self.bot.state)

    def test_unavailable_evidence_rejected(self):
        self.closed_pause();before=deepcopy(self.bot.state)
        with patch.object(self.bot,'snapshot',side_effect=OSError('offline')),self.assertRaises(OSError):self.reset()
        self.assertEqual(before,self.bot.state)

    def test_changed_ledger_rejected(self):
        self.closed_pause();snap=self.bot.snapshot(for_reset=True)
        out=next(d for d in snap['deals'] if d['entry']=='OUT')
        snap['ledger_facts'][out['id']]['profit']='999'
        before=deepcopy(self.bot.state)
        with patch.object(self.bot,'snapshot',return_value=snap),self.assertRaises(Exception):self.reset()
        self.assertEqual(before,self.bot.state)

    def test_repeated_reset_cannot_restart_or_duplicate(self):
        self.closed_pause(profit=.17);self.reset();before=deepcopy(self.bot.state)
        with self.assertRaises(ActionRejected):self.reset()
        self.assertEqual(before,self.bot.state);self.assertEqual(D(self.bot.state['basket_carry']),0)
        self.restart();self.assertTrue(self.bot.state['stop']);self.assertIsNone(self.bot.state['cycle'])

    def test_missing_or_conflicting_proof_rejected(self):
        self.closed_pause();snap=self.bot.snapshot();cid=next(iter(self.bot.state['cycle']['tickets'].values()))['command_id']
        for field,value in [('status','UNKNOWN'),('cycle_id','other'),('position_id','other')]:
            bad=deepcopy(snap);bad['obligations'][cid][field]=value;before=deepcopy(self.bot.state)
            with patch.object(self.bot,'snapshot',return_value=bad),self.assertRaises(ActionRejected):self.reset()
            self.assertEqual(before,self.bot.state)

    def test_waiting_panel_command_blocks_reset(self):
        self.closed_pause();self.bot.state.setdefault('control_receipts',{})['waiting']=dict(status='WAITING_DATA',deferred={'action':{'type':'start','mode':'auto'}})
        self.bot.save('fixture_waiting');before=deepcopy(self.bot.state)
        with self.assertRaises(ActionRejected):self.reset()
        self.assertEqual(before,self.bot.state)

    def test_latest_old_unknown_blocks_direct_and_gateway_reset(self):
        from tradfi.control import ActionGateway
        self.closed_pause();self.bot.state['control_revision']=7
        self.bot.state.setdefault('control_receipts',{})['old-request']=dict(status='UNKNOWN',revision=7,digest='old')
        self.bot.save('fixture_unknown_receipt');before=deepcopy(self.bot.state)
        with self.assertRaises(ActionRejected):self.reset()
        self.assertEqual(before,self.bot.state)
        result=ActionGateway(self.bot).apply(dict(id='safe-reset-old-unknown',revision=7,action=dict(type='command',text='/reset_pause')))
        self.assertEqual(result['status'],'REJECTED');self.assertEqual(self.bot.state['phase'],'SAFE_PAUSE')
        self.assertIsNotNone(self.bot.state['cycle']);self.assertIsNone(self.bot._active_control_id)

    def test_control_gateway_accepts_reset_but_never_starts(self):
        from tradfi.control import ActionGateway
        self.closed_pause();gateway=ActionGateway(self.bot)
        result=gateway.apply(dict(id='safe-reset-001',revision=self.bot.state.get('control_revision',0),action=dict(type='command',text='/reset_pause')))
        self.assertEqual(result['status'],'APPLIED');self.assertTrue(self.bot.state['stop']);self.assertEqual(self.bot.state['phase'],'WAIT_START')

    def test_commit_failure_preserves_pause(self):
        self.closed_pause();before=deepcopy(self.bot.state)
        def fail(event):
            if event=='basket_pause_reset':raise RuntimeError('disk')
        self.store.fault=fail
        with self.assertRaises(DurabilityError):self.reset()
        self.assertEqual(before,self.bot.state);self.assertEqual(before,self.store.load())

if __name__=='__main__':unittest.main()
