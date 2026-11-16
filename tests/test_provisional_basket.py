"""Atomic previous-close projection integration. Synthetic MT5 only."""
from copy import deepcopy
from decimal import Decimal as D
from unittest.mock import patch
import unittest
import test_related_close as related
from test_tradfi_mt5 import record
from tradfi.storage import DurabilityError

class ProvisionalBasketTest(unittest.TestCase):
    setUp=related.RelatedCloseTest.setUp
    tearDown=related.RelatedCloseTest.tearDown
    restart=related.RelatedCloseTest.restart
    tick=related.RelatedCloseTest.tick
    initial=related.RelatedCloseTest.initial
    candidate=related.RelatedCloseTest.candidate

    def authorize_fixture_counter(self):
        from tradfi.related_close import authorize_counter_group
        c=self.bot.state['cycle']
        for g in c['groups']:
            if g['role']=='reversal':
                g['linked_source']=self.old['group']
                authorize_counter_group(self.bot.state,g,self.old['group'],[self.old['id']])
        self.bot.save('explicit_owner_counter_fixture')

    def pending_previous(self):
        self.initial();self.old=deepcopy(next(iter(self.bot.state['cycle']['tickets'].values())))
        self.authorize_fixture_counter()
        self.api.fill()  # actual opposite fill precedes final source closure history
        self.candidate(reason=4)
        self.tick()
        return self.bot.state['cycle']

    def finish_previous(self,profit=None):
        self.api.orders=[o for o in self.api.orders if o.ticket!=99999]
        self.api.close_position(reason=self.api.DEAL_REASON_SL,profit=profit)
        self.api.history[-1]=record(**dict(self.api.history[-1]._asdict(),ticket=99999,reason=4,position_by_id=0))
        self.api.deals[-1]=self.api.deals[-1]._replace(order=99999,commission=-.1,swap=-.2,fee=-.03)

    def test_actual_opposite_fill_uses_previous_entry_lot_sl_no_fake_ledger(self):
        c=self.pending_previous();row=c['provisional_recovery'][self.old['id']]
        self.assertEqual(row['volume'],self.old['size']);self.assertEqual(row['entry'],self.old['entry'])
        self.assertEqual(row['side'],self.old['side']);self.assertEqual(D(row['sl']),D(self.old['sl']))
        expected=self.broker.profit(self.old['side'],D(self.old['size']),D(self.old['entry']),D(self.old['sl']))
        self.assertEqual(D(row['estimated_close_net']),expected)
        self.assertEqual(D(c['recovery_effective']),-expected)
        self.assertEqual(D(c['realized']),0);self.assertEqual(D(c['recovery']),0)
        self.assertFalse(c['tickets'][self.old['id']]['closed'])
        self.assertEqual(self.bot.state['seen_deals'],[])
        self.assertEqual(len(c['tickets']),2)
        before=deepcopy(row);self.restart();self.tick(2)
        self.assertEqual(self.bot.state['cycle']['provisional_recovery'][self.old['id']],before)

    def test_actual_net_replaces_estimate_exactly_once(self):
        self.pending_previous();self.finish_previous(profit=-2)
        self.tick(2);c=self.bot.state['cycle']
        self.assertEqual(c['provisional_recovery'],{})
        self.assertEqual(D(c['realized']),D('-2.33'))
        self.assertEqual(D(c['recovery']),D('2.33'))
        self.assertEqual(D(c['recovery_effective']),D('2.33'))
        self.assertEqual(c['recovery_revision'],1)
        before=deepcopy(c);seen=deepcopy(self.bot.state['seen_deals'])
        self.restart();self.tick(2);c=self.bot.state['cycle']
        for key in ('realized','recovery','recovery_effective','recovery_revision'):
            self.assertEqual(c[key],before[key])
        self.assertEqual(self.bot.state['seen_deals'],seen)

    def test_crash_before_atomic_commit_does_not_drop_estimate_or_book(self):
        self.pending_previous();self.finish_previous(profit=-2)
        with patch.object(self.store,'commit_deals',side_effect=DurabilityError('crash')):
            with self.assertRaises(DurabilityError):self.tick()
        c=self.bot.state['cycle']
        self.assertIn(self.old['id'],c['provisional_recovery'])
        self.assertEqual(D(c['realized']),0);self.assertFalse(c['tickets'][self.old['id']]['closed'])
        self.restart();self.tick(2)
        self.assertEqual(self.bot.state['cycle']['provisional_recovery'],{})
        self.assertEqual(D(self.bot.state['cycle']['realized']),D('-2.33'))
        self.assertEqual(self.bot.state['cycle']['recovery_revision'],1)

    def test_no_opposite_fill_no_projection_and_tp_not_assumed_loss(self):
        self.initial();self.candidate(reason=4);self.tick()
        self.assertNotIn('provisional_recovery',self.bot.state['cycle'])
        self.api.fill();self.api.orders=[o for o in self.api.orders if o.ticket!=99999]
        self.candidate(reason=5);self.tick()
        self.assertNotIn('provisional_recovery',self.bot.state['cycle'])

    def test_disappeared_source_with_delayed_history_keeps_other_actions_independent(self):
        self.pending_previous();old_live=self.api.positions[0]
        self.api.positions=self.api.positions[1:]
        self.api.orders=[o for o in self.api.orders if o.ticket!=99999]
        target=self.api.orders[0]
        cid=self.bot._intent('CANCEL',order_id=str(target.ticket));self.bot.save('cancel_other_fixture')
        before=len(self.api.sends);self.tick()
        self.assertEqual(len(self.api.sends),before+1)
        c=self.bot.state['cycle']
        self.assertFalse(c['tickets'][self.old['id']]['closed'])
        self.assertIn(self.old['id'],c['provisional_recovery'])
        self.assertNotEqual(self.bot.state['phase'],'SAFE_PAUSE')
        self.restart();self.tick()
        self.assertIn(self.old['id'],self.bot.state['cycle']['provisional_recovery'])
        # Later exact source deals arrive, not fabricated from absence.
        self.api.positions.insert(0,old_live);self.finish_previous(profit=-2)
        self.tick()
        self.assertEqual(self.bot.state['cycle']['provisional_recovery'],{})
        self.assertEqual(D(self.bot.state['cycle']['realized']),D('-2.33'))

    def test_source_estimate_cannot_exempt_another_command(self):
        self.pending_previous();self.api.positions=self.api.positions[1:]
        self.api.orders=[o for o in self.api.orders if o.ticket!=99999]
        self.bot.state['cycle']['provisional_recovery'][self.old['id']]['command_id']='wrong'
        with self.assertRaises(OSError):self.bot.snapshot()

    def missed_transient(self):
        self.initial();self.old=deepcopy(next(iter(self.bot.state['cycle']['tickets'].values())))
        self.old_live=self.api.positions[0]
        self.authorize_fixture_counter()
        self.api.fill()
        self.api.positions=[p for p in self.api.positions if str(p.identifier)!=self.old['id']]

    def test_bootstrap_without_observed_closing_order_uses_durable_previous_sl(self):
        self.missed_transient();self.tick()
        c=self.bot.state['cycle'];row=c['provisional_recovery'][self.old['id']]
        self.assertEqual(row['protection_source'],'DURABLE_CONFIRMED_POSITION')
        self.assertEqual(D(row['sl']),D(self.old['confirmed_position']['sl']))
        self.assertFalse(c['tickets'][self.old['id']]['closed'])
        self.assertEqual(D(c['realized']),0);self.assertEqual(len(c['tickets']),2)
        self.assertNotEqual(self.bot.state['phase'],'SAFE_PAUSE')
        before=deepcopy(row);self.restart();self.tick()
        self.assertEqual(self.bot.state['cycle']['provisional_recovery'][self.old['id']],before)
        self.api.positions.insert(0,self.old_live);self.finish_previous(profit=-2)
        self.tick()
        self.assertEqual(self.bot.state['cycle']['provisional_recovery'],{})
        self.assertEqual(D(self.bot.state['cycle']['realized']),D('-2.33'))

    def test_bootstrap_rejects_missing_or_wrong_confirmed_protection(self):
        self.missed_transient()
        t=self.bot.state['cycle']['tickets'][self.old['id']];prior=deepcopy(t['confirmed_position'])
        for key,value in (('id','foreign'),('cycle_id','foreign'),('command_id','foreign'),('sl','0'),('size','0.02')):
            t['confirmed_position']=dict(prior,**{key:value})
            with self.subTest(key=key),self.assertRaises(OSError):self.bot.snapshot()
        t.pop('confirmed_position')
        with self.assertRaises(OSError):self.bot.snapshot()

    def test_bootstrap_rejects_unrelated_opposite_group(self):
        self.missed_transient()
        for g in self.bot.state['cycle']['groups']:
            if g['role']!='initial':g['role']='unrelated';g['linked_source']='foreign'
        with self.assertRaises(OSError):self.bot.snapshot()

    def test_bootstrap_with_prior_confirmed_modify_does_not_require_lost_readback(self):
        self.initial();self.old=deepcopy(next(iter(self.bot.state['cycle']['tickets'].values())))
        p=self.api.positions[0]
        self.authorize_fixture_counter()
        cid=self.bot._intent('MODIFY',position_id=str(p.identifier),sl=str(p.sl),tp=str(p.tp),purpose='ANCHOR')
        self.bot.save('confirmed_modify_fixture');self.bot.dispatch({'MODIFY'});self.tick()
        self.assertEqual(self.bot.state['commands'][cid]['status'],'DONE')
        self.api.fill();self.api.positions=[x for x in self.api.positions if x.identifier!=p.identifier]
        before=len(self.api.sends);self.tick()
        self.assertIn(self.old['id'],self.bot.state['cycle']['provisional_recovery'])
        self.assertEqual(self.bot.state['commands'][cid]['status'],'DONE')
        self.assertEqual(len(self.api.sends),before)
        self.restart();self.tick();self.assertEqual(len(self.api.sends),before)

    def test_invalid_projection_cannot_mutate_booked_state_before_commit(self):
        from tradfi.basket_math import net_money
        self.pending_previous();self.finish_previous(profit=-2)
        snap=self.bot.snapshot();c=self.bot.state['cycle'];pid=self.old['id'];t=c['tickets'][pid]
        c['provisional_recovery'][pid]['command_id']='foreign'
        exits=[d for d in snap['deals'] if d['entry']=='OUT' and d['position_id']==pid]
        before=deepcopy(self.bot.state)
        with self.assertRaises(ValueError):
            self.bot._book_ticket_close(snap,c,pid,t,exits,{'SL'},net_money(exits))
        self.assertEqual(self.bot.state,before)
