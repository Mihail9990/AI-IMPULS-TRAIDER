"""Manual panel counter orders with native injected evidence, no live orders."""
from copy import deepcopy
from unittest.mock import patch
import unittest
import test_counter_authorization as fixtures
from tradfi.recovery_v3 import ControlledRecoveryController
from tradfi.control import ActionRejected
from tradfi.related_close import authorized_counter_group

class ManualCounterEntry(unittest.TestCase):
    setUp=fixtures.CounterAuthorization.setUp
    action=fixtures.CounterAuthorization.action
    def request(self,kind='STOP',timing='current',side='SELL'):
        f=self.f;f.bot.action(self.action('v3_entry',order_type=kind,timing=timing,side=side))
    def advance(self,up=False):
        f=self.f;r=f.bot.state['cycle']['v3_request'];f.api.now=r['bar_id']+60
        f.api.rates=[dict(time=r['bar_id'],open=3999. if up else 4001.,close=4000.)]
        f.api.tick=f.api.tick._replace(time_msc=f.api.now*1000,bid=3999.7,ask=3999.8);f.tick(3)
    def restart(self):
        f=self.f;f.bot=ControlledRecoveryController(f.store,f.broker,clock=lambda:f.api.now,monotonic=lambda:f.api.now)
    def test_manual_stop_source_provenance_restart_fill_and_no_duplicate(self):
        f=self.f;self.assertTrue(f.bot.v3_view(f.bot.snapshot())['v3_controls']['can_enter'])
        self.request();self.advance();self.restart()
        self.assertEqual(len(f.api.orders),2);c=f.bot.state['cycle'];g=c['groups'][-1]
        self.assertTrue(g['manual_counter']);self.assertTrue(authorized_counter_group(f.bot.state,g,self.old));self.assertNotIn('v3_link',c)
        f.api.fill();f.api.positions=[p for p in f.api.positions if str(p.identifier)!=self.old['id']]
        f.tick(2);self.assertIn(self.old['id'],f.bot.state['cycle']['provisional_recovery'])
        before=deepcopy(f.api.sends);self.restart();f.tick(3)
        self.assertEqual(f.api.sends,before)
    def test_candle_must_be_opposite_never_auto_same_side(self):
        f=self.f;before=len(f.api.sends);self.request();self.advance(up=True)
        self.assertFalse(f.api.orders);self.assertNotIn('v3_request',f.bot.state['cycle']);self.assertEqual(len(f.api.sends),before)
    def test_source_change_before_candle_cancels_request(self):
        f=self.f;self.request();f.api.positions[0]=f.api.positions[0]._replace(sl=3997.)
        self.advance();self.assertFalse(f.api.orders);self.assertNotIn('v3_request',f.bot.state['cycle'])
    def test_final_recheck_source_changed_during_order_check_no_send(self):
        f=self.f;self.request();r=f.bot.state['cycle']['v3_request'];f.api.now=r['bar_id']+60
        f.api.rates=[dict(time=r['bar_id'],open=4001.,close=4000.)]
        f.api.tick=f.api.tick._replace(time_msc=f.api.now*1000,bid=3999.7,ask=3999.8)
        original=f.api.order_check;before=len(f.api.sends)
        def changed(req):
            result=original(req);f.api.positions[0]=f.api.positions[0]._replace(sl=3997.);return result
        with patch.object(f.api,'order_check',side_effect=changed):f.tick()
        self.assertEqual(len(f.api.sends),before)
    def test_duplicate_or_different_active_group_rejected(self):
        f=self.f;self.request();before=deepcopy(f.bot.state['commands'])
        with self.assertRaises(ActionRejected):self.request()
        self.assertEqual(f.bot.state['commands'],before)
    def test_no_owner_request_no_auto_counter(self):
        f=self.f;before=deepcopy(f.api.sends);f.tick(5)
        self.assertEqual(f.api.sends,before);self.assertFalse(f.api.orders)
    def test_manual_market_now_explicit_only_and_no_repeat(self):
        f=self.f;self.request('MARKET','now');f.tick(3)
        self.assertEqual(len(f.api.positions),1)  # fake has not supplied MARKET fill evidence
        self.assertEqual(sum(c['status']=='UNKNOWN' for c in f.bot.state['commands'].values()),1)
        g=f.bot.state['cycle']['groups'][-1];self.assertTrue(authorized_counter_group(f.bot.state,g,self.old))
        before=deepcopy(f.api.sends);self.restart();f.tick(3);self.assertEqual(f.api.sends,before)
    def test_manual_pending_cancel_uses_manual_button(self):
        f=self.f;self.request();self.advance();f.bot.action(self.action('v3_cancel_entry'));f.tick(3)
        self.assertFalse(f.api.orders);self.assertEqual(len(f.api.positions),1)

    def test_scheduled_request_restart_stays_cancelled_not_replayed(self):
        f=self.f;self.request();self.restart();f.tick(3)
        self.assertNotIn('v3_request',f.bot.state['cycle']);self.assertFalse(f.api.orders)

    def test_source_without_sl_rejected_without_estimated_loss(self):
        f=self.f;f.api.positions[0]=f.api.positions[0]._replace(sl=0.)
        before=deepcopy(f.bot.state['commands'])
        with self.assertRaises(ActionRejected):self.request()
        self.assertEqual(f.bot.state['commands'],before)

    def test_two_active_source_groups_reject_ambiguous_counter(self):
        f=self.f;self.request();self.advance();f.api.fill();f.tick(2)
        before=deepcopy(f.bot.state['commands'])
        with self.assertRaises(ActionRejected):self.request('MARKET','now','BUY')
        self.assertEqual(f.bot.state['commands'],before)

    def test_profit_unavailable_no_partial_intents_or_fake_authorization(self):
        f=self.f;before=deepcopy(f.bot.state['commands']);groups=deepcopy(f.bot.state['cycle']['groups'])
        with patch.object(f.broker,'profit',side_effect=OSError('unavailable')):
            with self.assertRaises(ActionRejected):self.request('MARKET','now')
        self.assertEqual(f.bot.state['commands'],before);self.assertEqual(f.bot.state['cycle']['groups'],groups)

    def test_manual_source_does_not_require_already_closed_first_rung(self):
        f=self.f;c=f.bot.state['cycle'];t=c['tickets'][self.old['id']]
        # The identity/protection proof is unchanged; ordinal is not authority.
        t['index']=2;f.bot.save('surviving_rung_fixture')
        self.request();self.advance();self.assertEqual(len(f.api.orders),2)

    def test_cancel_manual_pending_during_protective_close_only_own_orders(self):
        import test_related_close as related
        f=self.f;self.request();self.advance();related.RelatedCloseTest.candidate(f,reason=4)
        before=len(f.api.sends);f.bot.action(self.action('v3_cancel_entry'));f.tick(2)
        removed=[r['order'] for r in f.api.sends[before:] if r['action']==f.api.TRADE_ACTION_REMOVE]
        self.assertEqual(len(removed),2);self.assertNotIn(99999,removed)
        self.assertEqual([o.ticket for o in f.api.orders],[99999])
        prior=deepcopy(f.api.sends);self.restart();f.tick(3);self.assertEqual(f.api.sends,prior)

    def test_cancel_sl_link_during_protective_close_preserves_close_order(self):
        import test_related_close as related
        f=self.f;f.bot.action(self.action('v3_link',expected_positions=[self.old['id']]));f.tick(3)
        related.RelatedCloseTest.candidate(f,reason=4)
        f.bot.action(self.action('v3_cancel_link'));f.tick(2)
        self.assertEqual([o.ticket for o in f.api.orders],[99999])
