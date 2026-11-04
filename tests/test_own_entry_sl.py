from copy import deepcopy
from decimal import Decimal as D
import unittest
import test_basket_latest_close as fixtures
from tradfi.control import ActionRejected,ActionGateway
from tradfi.basket_latest_close import LatestCloseBasketController

class OwnEntry(unittest.TestCase):
    def setUp(self):
        self.case=fixtures.LatestClose();self.case.setUp();self.addCleanup(self.case.doCleanups);self.f=self.case.f
    def prepare(self,side='SELL'):
        self.case.first_sl('BUY' if side=='SELL' else 'SELL');f=self.f
        level=D(next(o['price'] for o in f.b.data['orders'] if o['side']==side));sign=1 if side=='BUY' else -1
        for i in range(6):f.b.fill(side,price=str(level+sign*D(i)*D('.01')))
        f.b.quote(level-D('.05'),level+D('.05'));f.tick(5)
        if side=='BUY':f.b.quote(level+D('.10'),level+D('.15'))
        else:f.b.quote(level-D('.15'),level-D('.10'))
    def action(self):return dict(type='own_entry_sl',cycle_id=self.f.bot.state['cycle']['id'],expected_positions=sorted(p['id'] for p in self.f.b.data['positions']))
    def test_own_actual_entries_both_sides_tp_unchanged_restart(self):
        for side in ('BUY','SELL'):
            with self.subTest(side=side):
                case=OwnEntry();case.setUp();self.addCleanup(case.doCleanups);case.prepare(side);f=case.f
                old={p['id']:p['tp'] for p in f.b.data['positions']};f.bot.action(case.action())
                f.bot=LatestCloseBasketController(f.store,f.b);f.tick(5)
                self.assertEqual(len({p['sl'] for p in f.b.data['positions']}),6)
                for p in f.b.data['positions']:self.assertEqual(D(p['sl']),D(p['entry']));self.assertEqual(p['tp'],old[p['id']])
                count=len(f.b.commands);f.bot=LatestCloseBasketController(f.store,f.b);f.tick(3);self.assertEqual(len(f.b.commands),count)
    def test_initial_single_position_supported(self):
        self.f.start();self.f.b.fill('BUY');self.f.tick(3);p=self.f.b.data['positions'][0];self.f.b.quote(D(p['entry'])+D('.1'),D(p['entry'])+D('.15'))
        tp=p['tp'];self.f.bot.action(self.action());self.f.tick(4)
        self.assertEqual(D(p['sl']),D(p['entry']));self.assertEqual(p['tp'],tp)
    def test_duplicate_request_and_repeated_click_no_duplicate_modify(self):
        self.prepare();g=ActionGateway(self.f.bot,execute=self.f.bot.action);r=dict(id='own-entry-one',revision=self.f.bot.state.get('control_revision',0),action=self.action())
        self.assertEqual(g.apply(r)['status'],'APPLIED');self.assertEqual(g.apply(r)['status'],'APPLIED')
        with self.assertRaises(ActionRejected):self.f.bot.action(self.action())
        self.f.tick(5);count=len(self.f.b.commands);self.f.bot.action(self.action());self.f.tick(5);self.assertEqual(count,len(self.f.b.commands))
    def test_bad_quote_readonly_stale_ids_unknown_rejected_before_effects(self):
        self.prepare();f=self.f;original=deepcopy(f.bot.state);original_spec=deepcopy(f.b.data['spec'])
        for case in ('readonly','ids','cycle','unknown','quote','freeze','protection_pending'):
            with self.subTest(case=case):
                f.bot.state=deepcopy(original);f.b.data['spec']=deepcopy(original_spec);f.b.read_only=False;f.b.quote('3996.65','3996.70');a=self.action()
                if case=='readonly':f.b.read_only=True
                if case=='ids':a['expected_positions']=['wrong']
                if case=='cycle':a['cycle_id']='wrong'
                if case=='unknown':next(iter(f.bot.state['commands'].values()))['status']='UNKNOWN'
                if case=='quote':f.b.quote('4000','4000.1')
                if case=='freeze':f.b.data['spec']['freeze_level']=100
                if case=='protection_pending':next(g for g in f.bot.state['cycle']['groups'] if g.get('activated')=='SELL')['entry_sl_once']={'status':'PENDING'}
                count=len(f.b.commands)
                with self.assertRaises(ActionRejected):f.bot.action(a)
                self.assertEqual(count,len(f.b.commands));self.assertNotIn('edit',f.bot.state['cycle'])
    def test_ack_without_readback_is_not_success(self):
        self.prepare();f=self.f;f.bot.action(self.action());f.bot._advance_edit(f.bot.snapshot())
        for cid in f.bot.state['cycle']['edit']['modify_ids']:f.bot.state['commands'][cid]['status']='DONE'
        self.assertTrue(f.bot._advance_edit(f.bot.snapshot()))
        self.assertFalse(any(g.get('own_entry_sl_confirmed') for g in f.bot.state['cycle']['groups']))

    def test_close_during_edit_halts(self):
        self.prepare();self.f.bot.action(self.action());self.f.b.close('SL');self.f.tick(3)
        self.assertEqual(self.f.bot.state['cycle']['manual_reason'],'EDIT_FILL_OR_CLOSE_RACE')
    def test_partial_reject_no_success_and_unknown_no_resubmit(self):
        self.prepare();f=self.f;original=f.b.submit;n=[0]
        def reject(cmd):
            if cmd['kind']=='MODIFY':
                n[0]+=1
                if n[0]==2:f.b.commands.append(deepcopy(cmd));f.b.results[cmd['id']]={'status':'REJECTED'};return
            original(cmd)
        f.b.submit=reject;f.bot.action(self.action());f.tick(4)
        self.assertEqual(f.bot.state['cycle']['manual_reason'],'EDIT_PARTIAL_FAILURE_REVIEW_BROKER_TRUTH')
        self.assertTrue(any(D(p['sl'])!=D(p['entry']) for p in f.b.data['positions']))
        self.assertTrue(any(D(p['sl'])==D(p['entry']) for p in f.b.data['positions']))
    def test_unknown_modify_restart_never_blind_retry(self):
        self.prepare();f=self.f;original=f.b.submit
        def unknown(cmd):
            if cmd['kind']=='MODIFY':f.b.commands.append(deepcopy(cmd));f.b.results[cmd['id']]={'status':'UNKNOWN'};return
            original(cmd)
        f.b.submit=unknown;f.bot.action(self.action());f.tick(3);count=len(f.b.commands)
        f.bot=LatestCloseBasketController(f.store,f.b);f.tick(4);self.assertEqual(len(f.b.commands),count)
        self.assertIn('edit',f.bot.state['cycle'])

    def test_old_active_protection_policy_is_not_silently_changed(self):
        self.prepare();self.f.bot.state['cycle'].pop('protection_policy');before=deepcopy(self.f.bot.state['cycle']);self.f.bot.save('old_policy_fixture')
        self.f.bot=LatestCloseBasketController(self.f.store,self.f.b)
        self.assertEqual(self.f.bot.state['phase'],'MIGRATION_BLOCK');self.assertEqual(self.f.bot.state['cycle'],before)

class NativeOwnEntry(unittest.TestCase):
    def test_native_initial_modify_readback_and_restart(self):
        case=fixtures.NativeLatestClose();case.setUp();self.addCleanup(case.doCleanups);f=case.f
        f.api.fill();f.tick(3);p=f.api.positions[0]
        f.api.tick=f.api.tick._replace(bid=p.price_open+.1,ask=p.price_open+.15,time_msc=f.api.now*1000)
        snap=f.bot.snapshot();tp=snap['positions'][0]['tp'];pid=snap['positions'][0]['id']
        f.bot.action(dict(type='own_entry_sl',cycle_id=f.bot.state['cycle']['id'],expected_positions=[pid]));f.tick(5)
        p=f.bot.snapshot()['positions'][0];self.assertEqual(D(p['sl']),D(p['entry']));self.assertEqual(p['tp'],tp)
        count=len(f.api.sends);f.bot=LatestCloseBasketController(f.store,f.broker,clock=lambda:f.api.now,monotonic=lambda:f.api.now);f.tick(3)
        self.assertEqual(len(f.api.sends),count)

class DistinctReverse(unittest.TestCase):
    def test_enable_cancel_own_entry_and_button_supersedes_reverse(self):
        from test_tp1_own_entry_sl import TP1OwnEntry
        case=TP1OwnEntry();case.setUp();self.addCleanup(case.doCleanups);f=case.f
        f.b.data['spec'].update(tick_size='0.001',point='0.001',digits=3)
        f.b.close('TP',position_id=case.ts[0]['id']);f.tick(6)
        ids=sorted(p['id'] for p in f.b.data['positions']);c=f.bot.state['cycle']
        action=dict(type='reverse_ladder',cycle_id=c['id'],expected_positions=ids,enabled=True)
        old={p['id']:p['tp'] for p in f.b.data['positions']}
        f.bot.action(action);f.tick(5);self.assertTrue(case.g['reverse_ladder']['enabled'])
        self.assertGreater(len({p['sl'] for p in f.b.data['positions']}),1)
        action['enabled']=False;f.bot.action(action);f.tick(5)
        for p in f.b.data['positions']:self.assertEqual(D(p['sl']),D(p['entry']));self.assertEqual(p['tp'],old[p['id']])
        action['enabled']=True;f.bot.action(action);f.tick(5)
        f.bot.action(dict(type='own_entry_sl',cycle_id=c['id'],expected_positions=ids));f.tick(5)
        self.assertFalse(case.g['reverse_ladder']['enabled'])
        self.assertTrue(all(D(p['sl'])==D(p['entry']) for p in f.b.data['positions']))
