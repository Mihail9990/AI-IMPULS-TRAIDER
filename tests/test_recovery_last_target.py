"""Offline durable last-open-rung retargeting; no MT5 or network."""
from copy import deepcopy
from decimal import Decimal as D
import unittest
import test_recovery_profiles as fixtures
from tradfi.recovery_v3 import ControlledRecoveryController


class LastTargetTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.RecoveryProfiles();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.f.loss('10');self.f.entry()
        self.bot=self.f.f.bot;self.c=self.bot.state['cycle'];self.g=self.f.group()
        self.g['recovery_target_tracking']=True
        self.c['realized']='-10';self.c['recovery']='10';self.c['recovery_revision']=1
        self.snap=deepcopy(self.f.f.b.snapshot())
        self.bot.save('test_target_tracking')

    def plan(self):
        self.bot._recovery_retarget(self.snap,self.g)
        return self.g.get('recovery_target_tx')

    def confirm(self,tx):
        cmd=self.bot.state['commands'][tx['command_id']];cmd['status']='DONE'
        p=next(p for p in self.snap['positions'] if p['id']==tx['position_id']);p['tp']=tx['tp']
        self.bot._advance_recovery_target(self.snap,self.g)

    def test_only_last_tp_and_same_sl(self):
        before=deepcopy(self.snap);tx=self.plan();self.assertIsNotNone(tx)
        last=max(self.c['tickets'].values(),key=lambda t:t['index'] if t['group']==self.g['id'] else -1)
        self.assertEqual(tx['position_id'],last['id'])
        p=next(p for p in self.snap['positions'] if p['id']==last['id'])
        self.assertEqual(tx['sl'],p['sl']);self.assertEqual(before,self.snap)
        self.assertEqual(self.bot.state['commands'][tx['command_id']]['purpose'],'RECOVERY_TARGET')

    def test_no_duplicate_ready_unknown_and_restart(self):
        tx=self.plan();cid=tx['command_id'];count=len(self.bot.state['commands'])
        self.plan();self.assertEqual(len(self.bot.state['commands']),count)
        self.bot.state['commands'][cid]['status']='UNKNOWN';self.c['realized']='-20';self.plan()
        self.assertEqual(len(self.bot.state['commands']),count)
        self.bot.save('test_unknown')
        bot=ControlledRecoveryController(self.f.f.store,self.f.f.b)
        g=bot.state['cycle']['groups'][-1];bot._recovery_retarget(self.snap,g)
        self.assertEqual(g['recovery_target_tx']['command_id'],cid)

    def test_confirmed_readback_required_and_revision_retargets_closer(self):
        tx=self.plan();self.bot.state['commands'][tx['command_id']]['status']='DONE'
        self.assertTrue(self.bot._advance_recovery_target(self.snap,self.g))
        self.confirm(tx);self.assertEqual(tx['status'],'DONE')
        self.c['realized']='-5';self.c['recovery_revision']=2
        newer=self.plan();self.assertNotEqual(newer['command_id'],tx['command_id'])
        # SELL's closer target is higher, same other rungs and SL.
        self.assertGreater(D(newer['tp']),D(tx['tp']))

    def test_stale_ready_replaced_without_resend(self):
        old=self.plan();cid=old['command_id'];self.c['realized']='-12';self.c['recovery_revision']=2
        new=self.plan();self.assertNotEqual(new['command_id'],cid)
        self.assertEqual(self.bot.state['commands'][cid]['status'],'OBSOLETE')

    def test_sibling_protection_change_invalidates_ready(self):
        old=self.plan();other=next(p for p in self.snap['positions'] if p['id']!=old['position_id'])
        other['tp']=str(D(other['tp'])-D('.01'));new=self.plan()
        self.assertNotEqual(new['command_id'],old['command_id'])
        self.assertEqual(other['tp'],str(D(next(t for t in self.c['tickets'].values() if t['id']==other['id'])['tp'])-D('.01')))

    def test_partial_volume_mismatch_does_not_plan(self):
        self.snap['positions'][-1]['size']='0.005';self.assertIsNone(self.plan())
        self.assertEqual(self.g['recovery_target_wait'],'TARGET_OR_EVIDENCE_UNAVAILABLE')

    def test_manual_final_target_preserved(self):
        self.g['manual_tp_distances']={'5':'2.0'};self.assertIsNone(self.plan())
        self.assertEqual(self.g['recovery_target_wait'],'MANUAL_LAST_TP_PRESERVED')

    def test_all_closed_no_plan(self):
        for t in self.c['tickets'].values():t['closed']=True
        self.snap['positions']=[];self.assertIsNone(self.plan())

    def test_final_closed_selects_highest_remaining(self):
        t=max((t for t in self.c['tickets'].values() if t['group']==self.g['id']),key=lambda t:t['index'])
        t['closed']=True;self.snap['positions']=[p for p in self.snap['positions'] if p['id']!=t['id']]
        tx=self.plan();self.assertEqual(self.c['tickets'][tx['position_id']]['index'],4)

    def test_rejection_no_blind_retry(self):
        tx=self.plan();cmd=self.bot.state['commands'][tx['command_id']];cmd['status']='REJECTED';cmd['result']={'reason':'INVALID_STOPS'}
        count=len(self.bot.state['commands']);self.plan();self.plan()
        self.assertEqual(len(self.bot.state['commands']),count);self.assertEqual(tx['status'],'FAILED')

    def test_proven_never_sent_basis_rejection_can_replan(self):
        tx=self.plan();cmd=self.bot.state['commands'][tx['command_id']];cmd.update(status='REJECTED',result={'never_sent':True,'reason':'RECOVERY_TARGET_BASIS_CHANGED'})
        new=self.plan();self.assertNotEqual(new['command_id'],tx['command_id']);self.assertEqual(cmd['status'],'OBSOLETE')

    def test_anchor_not_overwritten_after_tracking(self):
        self.g['profile_anchored_positions']=[];before=len(self.bot.state['commands'])
        self.bot._live_ladder(self.snap,self.g);self.assertEqual(len(self.bot.state['commands']),before)

    def test_pending_hook_marks_only_linked_basket_and_preserves_booked_cash(self):
        self.g.pop('recovery_target_tracking')
        old=next(t for t in self.c['tickets'].values() if t['role']=='initial')
        self.g['linked_source']=old['group']
        self.c['provisional_recovery']={old['id']:dict(command_id=old['command_id'],estimated_close_net='-7')}
        before=self.c['realized'];self.bot._related_management(self.snap)
        self.assertTrue(self.g['recovery_target_tracking'])
        self.assertEqual(self.c['realized'],before)
        self.assertIsNotNone(self.g.get('recovery_target_tx'))

    def test_unknown_then_final_close_does_not_repeat_old_modify(self):
        tx=self.plan();cmd=self.bot.state['commands'][tx['command_id']];cmd['status']='UNKNOWN'
        t=self.c['tickets'][tx['position_id']];t['closed']=True;cmd['status']='OBSOLETE'
        self.snap['positions']=[p for p in self.snap['positions'] if p['id']!=t['id']]
        new=self.plan();self.assertEqual(tx['status'],'CLOSED')
        self.assertNotEqual(new['position_id'],tx['position_id'])
        self.assertEqual(cmd['status'],'OBSOLETE')

    def test_pending_anchor_waits_before_target(self):
        self.g['actual_ladder_tx']={'status':'PENDING','targets':[]}
        self.assertIsNone(self.plan())

    def test_impossible_stops_keep_existing_levels(self):
        for p in self.snap['positions']:p['sl']=p['entry']
        # SELL live ask lies beyond its SL, so protection is not modifiable.
        self.snap['quote']['ask']=str(D(self.snap['positions'][0]['entry'])+D('1'))
        before=deepcopy(self.snap['positions']);self.assertIsNone(self.plan())
        self.assertEqual(before,self.snap['positions'])

    def test_unestimated_source_does_not_use_confirmed_zero_debt(self):
        old=next(t for t in self.c['tickets'].values() if t['role']=='initial');old['closed']=False
        self.g['linked_source']=old['group'];self.c['realized']='0';self.c['provisional_recovery']={}
        self.assertIsNone(self.plan())
        self.assertEqual(self.g['recovery_target_wait'],'SOURCE_RESULT_OR_ESTIMATE_REQUIRED')

    def test_full_fake_gateway_dispatch_and_readback_no_duplicate(self):
        tx=self.plan();cid=tx['command_id']
        self.bot.dispatch({'MODIFY'})
        snap=self.bot.snapshot();self.bot._reconcile_commands(snap)
        self.assertEqual(self.bot.state['commands'][cid]['status'],'DONE')
        self.assertFalse(self.bot._advance_recovery_target(snap,self.g))
        self.assertEqual(tx['status'],'DONE')
        before=len(self.bot.state['commands']);self.bot._recovery_retarget(snap,self.g)
        self.assertEqual(len(self.bot.state['commands']),before)

    def test_external_final_tp_changed_before_send_is_preserved(self):
        tx=self.plan();p=next(p for p in self.snap['positions'] if p['id']==tx['position_id'])
        p['tp']=str(D(p['tp'])-D('.5'));changed=p['tp'];count=len(self.bot.state['commands'])
        self.plan();self.assertEqual(tx['status'],'FAILED')
        self.assertEqual(len(self.bot.state['commands']),count);self.assertEqual(p['tp'],changed)
        self.assertEqual(self.bot.state['commands'][tx['command_id']]['status'],'OBSOLETE')

    def test_new_later_fill_changes_final_rung_even_same_financial_revision(self):
        tx=self.plan();self.confirm(tx)
        t=deepcopy(self.c['tickets'][tx['position_id']]);t.update(id='later',index=6,command_id='latercmd')
        self.c['tickets']['later']=t
        p=deepcopy(next(p for p in self.snap['positions'] if p['id']==tx['position_id']));p.update(id='later',tp=str(D(p['entry'])-D('2')))
        self.snap['positions'].append(p)
        new=self.plan();self.assertEqual(new['position_id'],'later')

    def test_changed_sibling_tp_after_confirmation_recomputes_remaining_total(self):
        tx=self.plan();self.confirm(tx)
        p=next(p for p in self.snap['positions'] if p['id']!=tx['position_id']);p['tp']=str(D(p['tp'])-D('.10'))
        new=self.plan();self.assertNotEqual(new['command_id'],tx['command_id'])
        self.assertGreater(D(new['tp']),D(tx['tp']))

    def test_overlapping_recovery_groups_do_not_double_allocate_debt(self):
        t=deepcopy(next(t for t in self.c['tickets'].values() if t['group']==self.g['id']))
        t.update(id='othergroup',group='another');self.c['tickets'][t['id']]=t
        p=deepcopy(self.snap['positions'][0]);p['id']=t['id'];self.snap['positions'].append(p)
        self.assertIsNone(self.plan());self.assertEqual(self.g['recovery_target_wait'],'MULTIPLE_ACTIVE_GROUPS_REQUIRE_REVIEW')

    def first_tp_closed(self):
        t=next(t for t in self.c['tickets'].values() if t['group']==self.g['id'] and t['index']==0)
        t.update(closed=True,reason='TP',exit_facts=[{'id':'verified-tp1'}])
        self.snap['positions']=[p for p in self.snap['positions'] if p['id']!=t['id']]
        entry=D(t['entry']);self.snap['quote'].update(bid=str(entry-D('.20')),ask=str(entry-D('.10')))

    def test_unknown_last_tp_does_not_block_independent_sibling_sl(self):
        tx=self.plan();self.bot.state['commands'][tx['command_id']]['status']='UNKNOWN'
        self.first_tp_closed();count=len(self.bot.state['commands'])
        self.bot._independent_first_tp_sl(self.snap)
        created=list(self.bot.state['commands'].values())[count:]
        self.assertEqual(len(created),4)
        self.assertTrue(all(x['position_id']!=tx['position_id'] and x['purpose']=='ANCHOR' for x in created))
        for cmd in created:
            p=next(p for p in self.snap['positions'] if p['id']==cmd['position_id'])
            self.assertEqual(cmd['tp'],p['tp']);self.assertEqual(cmd['sl'],p['entry'])
        self.bot._independent_first_tp_sl(self.snap)
        self.assertEqual(len(self.bot.state['commands']),count+4)
        self.assertEqual(self.bot.state['commands'][tx['command_id']]['status'],'UNKNOWN')

    def test_step_advances_independent_protection_while_target_unknown(self):
        from unittest.mock import patch
        tx=self.plan();self.bot.state['commands'][tx['command_id']]['status']='UNKNOWN';self.first_tp_closed()
        with patch.object(self.bot,'_observe',return_value=True):
            self.bot._step(self.snap)
        self.assertEqual(self.g['entry_sl_once']['status'],'INDEPENDENT')
        self.assertEqual(sum(x['command_id'] is not None for x in self.g['entry_sl_once']['targets']),4)

    def test_deferred_same_resource_sl_waits_for_target_readback_then_progresses(self):
        tx=self.plan();self.bot.state['commands'][tx['command_id']]['status']='UNKNOWN';self.first_tp_closed()
        self.bot._independent_first_tp_sl(self.snap)
        sltx=self.g['entry_sl_once'];last=next(u for u in sltx['targets'] if u['position_id']==tx['position_id'])
        self.assertIsNone(last['command_id']);self.confirm(tx)
        self.bot._advance_entry_sl_once(self.snap,self.g)
        self.assertIsNotNone(last['command_id'])
        self.assertEqual(self.bot.state['commands'][last['command_id']]['tp'],tx['tp'])
        count=len(self.bot.state['commands']);self.bot._advance_entry_sl_once(self.snap,self.g)
        self.assertEqual(len(self.bot.state['commands']),count)

    def test_estimated_previous_recovery_group_is_source_not_second_recipient(self):
        t=deepcopy(next(t for t in self.c['tickets'].values() if t['group']==self.g['id']))
        t.update(id='old-source',group='old-group',command_id='old-command',side='BUY')
        self.c['tickets'][t['id']]=t;self.g['linked_source']='old-group'
        p=deepcopy(self.snap['positions'][0]);p.update(id=t['id'],side='BUY');self.snap['positions'].append(p)
        self.c['provisional_recovery']={t['id']:dict(command_id=t['command_id'],estimated_close_net='-3')}
        tx=self.plan();self.assertIsNotNone(tx);self.assertNotEqual(tx['position_id'],t['id'])

    def test_estimated_closing_source_itself_is_never_retargeted(self):
        self.c['provisional_recovery']={t['id']:dict(command_id=t['command_id'],estimated_close_net='-1') for t in self.c['tickets'].values() if t['group']==self.g['id']}
        self.assertIsNone(self.plan())

    def test_legacy_partial_group_does_not_finalize_missing_rung_sl_tracking(self):
        tx=self.plan();self.bot.state['commands'][tx['command_id']]['status']='UNKNOWN';self.first_tp_closed()
        self.g.pop('recovery_profile',None)
        late=next(t for t in self.c['tickets'].values() if t['group']==self.g['id'] and t['index']==4)
        self.c['tickets'].pop(late['id']);self.snap['positions']=[p for p in self.snap['positions'] if p['id']!=late['id']]
        from unittest.mock import patch
        with patch.object(self.bot,'_group_indexes_valid',return_value=True):
            self.bot._independent_first_tp_sl(self.snap)
        self.assertNotIn('entry_sl_once',self.g)
