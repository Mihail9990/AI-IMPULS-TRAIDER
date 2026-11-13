"""Owner counter-trigger provenance through the actual V3 action, offline MT5."""
from copy import deepcopy
from unittest.mock import patch
import unittest
import test_tradfi_basket as fixtures
from tradfi.recovery_v3 import ControlledRecoveryController
from tradfi.related_close import authorized_counter_group
from tradfi.control import ActionRejected

class CounterAuthorization(unittest.TestCase):
    def setUp(self):
        self.f=f=fixtures.NativeBasketTest();f.setUp()
        self.addCleanup(f.doCleanups);self.addCleanup(f.tearDown)
        f.bot=ControlledRecoveryController(f.store,f.broker,clock=lambda:f.api.now,monotonic=lambda:f.api.now)
        f.bot.command('/start');f.api.now+=60
        f.api.tick=f.api.tick._replace(time_msc=f.api.now*1000)
        f.api.rates=[dict(time=int(f.api.now//60)*60-60,open=3999.,close=4000.)]
        f.tick(2);f.api.fill();f.tick(3)
        self.assertEqual(len(f.api.orders),0)
        self.old=deepcopy(next(iter(f.bot.state['cycle']['tickets'].values())))
        f.bot.action(self.action('recovery_profiles',selected_profile_id='native',profiles=[dict(id='native',name='Native',stages=[dict(volume='.01',tp_distance='.20'),dict(volume='.01',tp_distance='.40')])],recovery_sl_distance='1.50'))
    def action(self,kind,**kw):return dict(type=kind,cycle_id=self.f.bot.state['cycle']['id'],**kw)
    def link(self):
        f=self.f
        f.bot.action(self.action('v3_link',expected_positions=[self.old['id']]))
        return f.bot.state['cycle']['groups'][-1]
    def test_button_provenance_committed_before_any_send_and_survives_restart(self):
        f=self.f;before=len(f.api.sends);g=self.link()
        self.assertEqual(len(f.api.sends),before)
        durable=f.store.load();dg=durable['cycle']['groups'][-1]
        self.assertTrue(authorized_counter_group(durable,dg,self.old))
        f.bot=ControlledRecoveryController(f.store,f.broker,clock=lambda:f.api.now,monotonic=lambda:f.api.now)
        f.tick(3);self.assertEqual(len(f.api.orders),2)
    def test_preplaced_fill_before_missing_close_projects_old_only_no_duplicate(self):
        f=self.f;self.link();f.tick(3);f.api.fill()
        # Old position disappears before its closing history is available.
        f.api.positions=[p for p in f.api.positions if str(p.identifier)!=self.old['id']]
        before=len(f.api.sends);f.tick(2)
        c=f.bot.state['cycle'];row=c['provisional_recovery'][self.old['id']]
        self.assertEqual(row['volume'],self.old['size']);self.assertEqual(row['entry'],self.old['entry'])
        self.assertFalse(c['tickets'][self.old['id']]['closed']);self.assertEqual(float(c['realized']),0)
        self.assertEqual(len(c['tickets']),2)
        # Target management may MODIFY, but must never duplicate entry.
        self.assertFalse(any(r['action'] in (f.api.TRADE_ACTION_PENDING,f.api.TRADE_ACTION_DEAL) for r in f.api.sends[before:]))
    def test_unknown_auto_wrong_cycle_source_and_market_provenance_rejected(self):
        f=self.f;g=self.link();state=deepcopy(f.bot.state)
        for change in ('missing','cycle','source','market'):
            with self.subTest(change=change):
                s=deepcopy(state);x=s['cycle']['groups'][-1]
                if change=='missing':x.pop('counter_authorization')
                elif change=='cycle':x['counter_authorization']['cycle_id']='other'
                elif change=='source':x['counter_authorization']['source_position_ids']=['other']
                else:s['commands'][x['commands'][0]]['kind']='MARKET'
                self.assertFalse(authorized_counter_group(s,x,self.old))
    def test_manual_same_side_entry_is_not_allowed_over_live_source(self):
        f=self.f;before=deepcopy(f.bot.state['commands'])
        with self.assertRaises(ActionRejected):
            f.bot.action(self.action('v3_entry',order_type='STOP',timing='current',side='BUY'))
        self.assertEqual(f.bot.state['commands'],before)

    def test_unmarked_existing_counter_fill_does_not_create_provisional_estimate(self):
        import test_related_close as related
        f=self.f;g=self.link();g.pop('counter_authorization');f.bot.save('legacy_unmarked_fixture')
        f.tick(3);f.api.fill();related.RelatedCloseTest.candidate(f,reason=4)
        f.tick(2);c=f.bot.state['cycle']
        self.assertFalse(c.get('provisional_recovery'))
        self.assertFalse(c['tickets'][self.old['id']]['closed'])
        self.assertEqual(float(c['realized']),0)

    def test_internal_legacy_relink_cannot_mint_owner_provenance(self):
        f=self.f
        f.bot._request_link(self.action('v3_link',expected_positions=[self.old['id']]),owner_authorized=False)
        g=f.bot.state['cycle']['groups'][-1]
        self.assertNotIn('counter_authorization',g)
        self.assertFalse(authorized_counter_group(f.bot.state,g,self.old))
