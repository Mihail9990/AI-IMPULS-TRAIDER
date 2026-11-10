"""Offline exact-profile contracts; synthetic broker facts, never MT5/network."""
from copy import deepcopy
from decimal import Decimal as D
import unittest
import test_tradfi_basket as fixtures
from tradfi.broker import Spec
from tradfi.control import ActionRejected
from tradfi.recovery_v3 import ControlledRecoveryController
from tradfi.recovery_profiles import (defaults, validate_settings, validate_snapshot,
                                      selected_snapshot, exact_targets)


class RecoveryProfiles(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.BasketTest()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.bot = ControlledRecoveryController(self.f.store, self.f.b)

    def action(self, kind, **kw):
        return self.f.bot.action(dict(type=kind, cycle_id=(self.f.bot.state.get('cycle') or {}).get('id'), **kw))

    def select(self, profile_id):
        self.action('recovery_profile_select', profile_id=profile_id)

    def save_profiles(self, settings):
        self.action('recovery_profiles', **settings)

    def initial(self):
        f = self.f
        f.start(); f.b.fill('BUY'); f.tick(3)

    def loss(self, debt=None):
        self.initial()
        self.f.b.close('SL', profit=None if debt is None else str(-D(debt)))
        self.f.tick(4)
        self.assertEqual(self.f.bot.state['phase'], 'V3_WAIT_ENTRY')

    def entry(self, side='SELL', timing='now', kind='MARKET', **kw):
        self.action('v3_entry', order_type=kind, timing=timing, side=side, **kw)
        self.f.tick(6)

    def link(self, **kw):
        self.action('v3_link', expected_positions=sorted(p['id'] for p in self.f.b.data['positions']), **kw)
        self.f.tick(4)

    def group(self):
        return self.f.bot.state['cycle']['groups'][-1]

    def assert_positions(self, snapshot, side):
        c = self.f.bot.state['cycle']
        sign = 1 if side == 'BUY' else -1
        self.assertFalse(c.get('manual'), c.get('manual_reason'))
        for p in self.f.b.data['positions']:
            t = c['tickets'][p['id']]
            stage = snapshot['stages'][t['index']]
            self.assertEqual(D(p['size']), D(stage['volume']))
            self.assertEqual(sign * (D(p['tp']) - D(p['entry'])), D(stage['tp_distance']))

    def test_default_six_exact_stages(self):
        s = selected_snapshot(self.f.bot.state['recovery_profiles'])
        self.assertEqual(len(s['stages']), 6)
        self.assertEqual([D(x['volume']) for x in s['stages']], [D('.01')]*6)
        self.assertEqual([D(x['tp_distance']) for x in s['stages']], [D('.25')*i for i in range(1, 7)])
        self.loss(); self.entry()
        self.assertEqual(len(self.f.b.data['positions']), 6)
        self.assert_positions(s, 'SELL')

    def test_mixed_exact_sizes_mirrored_and_no_debt_stretch(self):
        for side in ('BUY', 'SELL'):
            with self.subTest(side=side):
                x = RecoveryProfiles(); x.setUp(); self.addCleanup(x.doCleanups)
                x.select('mixed'); x.loss('10000'); x.entry(side=side)
                s = x.group()['recovery_profile']
                self.assertEqual([D(p['size']) for p in x.f.b.data['positions']], [D('.05'), D('.02'), D('.01')])
                x.assert_positions(s, side)

    def test_single_custom_tp_and_residual_debt_wait(self):
        settings = defaults(); settings['selected_profile_id'] = 'single'
        settings['profiles'][-1]['stages'][0]['tp_distance'] = '2.37'
        self.save_profiles(settings); self.loss('1000'); self.entry()
        s = self.group()['recovery_profile']
        self.assert_positions(s, 'SELL')
        self.assertEqual(D(self.f.b.data['positions'][0]['size']), D('.10'))
        self.f.b.close('TP'); self.f.tick(8)
        self.assertEqual(self.f.bot.state['phase'], 'V3_WAIT_ENTRY')
        self.assertGreater(D(self.f.bot.state['cycle']['recovery']), 0)
        before = len(self.f.b.commands); self.f.tick(10)
        self.assertEqual(len(self.f.b.commands), before)
        self.assertFalse(self.f.b.data['orders']); self.assertFalse(self.f.b.data['positions'])

    def test_active_snapshot_unchanged_next_basket_uses_edit(self):
        self.select('mixed'); self.loss('1000'); self.entry()
        old = deepcopy(self.group()); positions = deepcopy(self.f.b.data['positions'])
        settings = defaults('2.00'); settings['selected_profile_id'] = 'single'
        settings['profiles'][1]['stages'][0]['volume'] = '.09'
        settings['profiles'][1]['stages'][0]['tp_distance'] = '.31'
        settings['profiles'][-1]['stages'][0]['tp_distance'] = '1.17'
        self.save_profiles(settings); self.f.tick(3)
        self.assertEqual(self.group()['recovery_profile'], old['recovery_profile'])
        self.assertEqual(self.f.b.data['positions'], positions)
        for p in list(self.f.b.data['positions']): self.f.b.close('SL', position_id=p['id'])
        self.f.tick(6); self.entry(side='BUY')
        self.assertEqual(len(self.f.b.data['positions']), 1)
        self.assertEqual(self.group()['recovery_profile']['id'], 'single')
        self.assert_positions(self.group()['recovery_profile'], 'BUY')
        p = self.f.b.data['positions'][0]
        self.assertEqual(D(p['entry'])-D(p['sl']), D('2.00'))

    def test_scheduled_snapshot_survives_settings_edits(self):
        self.select('mixed'); self.loss(); self.entry(timing='current', kind='STOP')
        request = deepcopy(self.f.bot.state['cycle']['v3_request'])
        settings = defaults('2.00'); settings['selected_profile_id'] = 'single'
        settings['profiles'][1]['stages'][0]['volume'] = '.09'
        settings['profiles'][1]['stages'][0]['tp_distance'] = '.31'
        self.save_profiles(settings)
        self.assertEqual(self.f.bot.state['cycle']['v3_request'], request)
        target = request['bar_id']
        self.f.b.data.update(now=target+60, candle=dict(id=target, open='3999', close='4000'))
        self.f.b.quote('4000', '4000.10'); self.f.tick(5)
        self.assertEqual(self.group()['recovery_profile'], request['recovery_profile'])
        self.assertEqual(len(self.f.b.data['orders']), 3)
        before = deepcopy(self.f.b.data['orders']); self.select('six'); self.f.tick(3)
        self.assertEqual(self.f.b.data['orders'], before)

    def test_link_snapshot_survives_selection_and_follow_sl_replacement(self):
        self.select('mixed'); self.initial(); self.link()
        old = deepcopy(self.group()['recovery_profile'])
        self.select('single')
        self.action('v3_adjust_sl', distance='2.90', expected_positions=sorted(p['id'] for p in self.f.b.data['positions']))
        self.f.tick(16)
        self.assertFalse(self.f.bot.state['cycle'].get('manual'))
        self.assertEqual(self.group()['recovery_profile'], old)
        self.assertEqual(self.f.bot.state['cycle']['v3_link']['recovery_profile'], old)
        self.assertEqual(len(self.f.b.data['orders']), 3)
        self.assertEqual({D(o['price']) for o in self.f.b.data['orders']}, {D(self.f.b.data['positions'][0]['sl'])})

    def test_restart_keeps_active_and_link_snapshots(self):
        self.select('mixed'); self.initial(); self.link()
        snapshot = deepcopy(self.group()['recovery_profile']); orders = deepcopy(self.f.b.data['orders'])
        self.select('single'); self.f.bot = ControlledRecoveryController(self.f.store, self.f.b); self.f.tick(4)
        self.assertEqual(self.group()['recovery_profile'], snapshot)
        self.assertEqual(self.f.b.data['orders'], orders)
        self.assertEqual(self.f.bot.state['recovery_profiles']['selected_profile_id'], 'single')
        self.f.b.close('SL')
        for _ in range(3): self.f.b.fill('SELL')
        p = self.f.b.data['positions'][0]; self.f.b.quote(D(p['entry'])-D('.05'), D(p['entry'])+D('.05')); self.f.tick(8)
        self.f.bot = ControlledRecoveryController(self.f.store, self.f.b); self.f.tick(4)
        self.assertEqual(self.group()['recovery_profile'], snapshot)
        self.assert_positions(snapshot, 'SELL')

    def test_partial_link_cancel_preserves_out_of_order_stage_and_slipped_tp(self):
        self.select('mixed'); self.initial(); self.link()
        snapshot = deepcopy(self.group()['recovery_profile'])
        orders = self.f.b.data['orders']; orders[:] = [orders[2], orders[1], orders[0]]
        level = D(orders[0]['price']); self.f.b.close('SL')
        self.f.b.fill('SELL', str(level-D('.03')))
        self.f.b.fill('SELL', str(level-D('.07')))
        self.f.b.quote(level-D('.08'), level+D('.02')); self.f.tick(2)
        self.action('v3_cancel_link'); self.f.tick(10)
        self.assertFalse(self.f.b.data['orders'])
        tickets = [t for t in self.f.bot.state['cycle']['tickets'].values() if not t['closed']]
        self.assertEqual({t['index'] for t in tickets}, {1, 2})
        self.assert_positions(snapshot, 'SELL')

    def test_full_link_unequal_slipped_fills_keep_exact_targets(self):
        self.select('mixed'); self.initial(); self.link()
        snapshot = deepcopy(self.group()['recovery_profile'])
        orders = self.f.b.data['orders']; orders[:] = [orders[2], orders[0], orders[1]]
        level = D(orders[0]['price']); self.f.b.close('SL')
        for slip in ('.03', '.07', '.11'): self.f.b.fill('SELL', str(level-D(slip)))
        self.f.b.quote(level-D('.12'), level-D('.02')); self.f.tick(10)
        self.assertEqual(len(self.f.b.data['positions']), 3)
        self.assert_positions(snapshot, 'SELL')
        tickets = self.f.bot.state['cycle']['tickets']
        self.assertEqual([tickets[p['id']]['index'] for p in self.f.b.data['positions']], [2, 0, 1])
        self.assertEqual(len({p['entry'] for p in self.f.b.data['positions']}), 3)

    def test_restart_cancels_schedule_without_replay(self):
        self.loss(); self.entry(timing='next')
        target = self.f.bot.state['cycle']['v3_request']['bar_id']
        self.f.bot = ControlledRecoveryController(self.f.store, self.f.b)
        self.f.b.data.update(now=target+60, candle=dict(id=target, open='3999', close='4000'))
        self.f.tick(8)
        self.assertNotIn('v3_request', self.f.bot.state['cycle'])
        self.assertFalse(self.f.b.data['positions']); self.assertFalse(self.f.b.data['orders'])

    def test_preupgrade_link_restart_and_sl_replacement_keep_legacy_count(self):
        self.f.bot.state['tp_distances'] = ['.20'] + [None]*19
        self.initial()
        c = self.f.bot.state['cycle']
        # Create the historical contract, then persist exactly its old schema.
        self.f.bot._request_link(dict(type='v3_link', cycle_id=c['id'], count=2,
            expected_positions=sorted(p['id'] for p in self.f.b.data['positions'])), legacy=True)
        c['v3_link'].pop('recovery_profile', None)
        self.f.bot.save('legacy_link_fixture'); self.f.tick(4)
        self.assertNotIn('recovery_profile', self.group())
        self.f.bot = ControlledRecoveryController(self.f.store, self.f.b); self.f.tick(3)
        self.select('single')
        self.action('v3_adjust_sl', distance='2.90', expected_positions=sorted(p['id'] for p in self.f.b.data['positions']))
        self.f.tick(16)
        c = self.f.bot.state['cycle']
        self.assertFalse(c.get('manual'), c.get('manual_reason'))
        self.assertEqual(c['v3_link']['count'], 2)
        self.assertEqual(self.group()['v3_count'], 2)
        self.assertFalse(self.group().get('recovery_profile'))
        self.assertEqual(len(self.f.b.data['orders']), 2)
        self.assertEqual([D(o['size']) for o in self.f.b.data['orders']], [D('.01')]*2)
        self.assertEqual({D(o['price']) for o in self.f.b.data['orders']}, {D(self.f.b.data['positions'][0]['sl'])})

    def partial_first_and_third(self):
        self.select('mixed'); self.initial(); self.link()
        orders = self.f.b.data['orders']; orders[:] = [orders[2], orders[0], orders[1]]
        level = D(orders[0]['price']); self.f.b.close('SL')
        self.f.b.fill('SELL', str(level-D('.03')))
        self.f.b.fill('SELL', str(level-D('.07')))
        self.f.b.quote(level-D('.08'), level+D('.02')); self.f.tick(2)
        self.action('v3_cancel_link'); self.f.tick(10)
        c = self.f.bot.state['cycle']
        self.assertFalse(c.get('manual'), c.get('manual_reason'))
        by_index = {c['tickets'][p['id']]['index']: p for p in self.f.b.data['positions']}
        self.assertEqual(set(by_index), {0, 2})
        return by_index

    def test_partial_first_and_third_tp1_moves_third_to_own_entry(self):
        positions = self.partial_first_and_third()
        third = positions[2]; tp = third['tp']
        self.f.b.close('TP', position_id=positions[0]['id']); self.f.tick(10)
        self.assertEqual(len(self.f.b.data['positions']), 1)
        remaining = self.f.b.data['positions'][0]
        self.assertEqual(remaining['id'], third['id'])
        self.assertEqual(D(remaining['sl']), D(remaining['entry']))
        self.assertEqual(remaining['tp'], tp)
        self.assertEqual(self.f.bot.state['cycle']['tickets'][remaining['id']]['index'], 2)
        self.assertFalse(self.f.bot.state['cycle'].get('manual'))

    def test_partial_third_chart_tp_edit_changes_only_original_stage(self):
        positions = self.partial_first_and_third()
        first = deepcopy(positions[0]); third = positions[2]
        snapshot = deepcopy(self.group()['recovery_profile'])
        new_tp = str(D(third['entry'])-D('1.17'))
        self.f.bot.edit(protection=dict(position_id=third['id'],
            old=dict(sl=third['sl'], tp=third['tp']), new=dict(sl=third['sl'], tp=new_tp)))
        self.f.tick(10)
        live = {p['id']: p for p in self.f.b.data['positions']}
        self.assertEqual(live[first['id']], first)
        self.assertEqual(live[third['id']]['tp'], new_tp)
        self.assertEqual(self.group()['manual_tp_distances'], {'2': '1.17'})
        self.assertEqual(self.group()['recovery_profile'], snapshot)
        self.assertEqual(self.f.bot.state['cycle']['tickets'][third['id']]['index'], 2)
        self.assertFalse(self.f.bot.state['cycle'].get('manual'))

    def test_broker_min_point_one_can_start_and_recover_single(self):
        self.f.b.data['spec'].update(volume_min='.10', volume_step='.10')
        self.f.bot.state['basket_settings']['INITIAL_SIZE'] = '.10'
        self.f.bot.state['basket_settings']['TICKET_SIZE'] = '.01'
        self.select('single'); self.loss('1000'); self.entry()
        self.assertEqual(len(self.f.b.data['positions']), 1)
        self.assertEqual(D(self.f.b.data['positions'][0]['size']), D('.10'))
        self.assert_positions(self.group()['recovery_profile'], 'SELL')

    def test_generic_settings_edit_preserves_profile_pending_with_no_legacy_tp(self):
        self.select('mixed'); self.loss(); self.entry(timing='current', kind='STOP')
        c = self.f.bot.state['cycle']; target = c['v3_request']['bar_id']
        self.f.b.data.update(now=target+60, candle=dict(id=target, open='3999', close='4000'))
        self.f.b.quote('4000', '4000.10'); self.f.tick(6)
        c['tp_distances'] = [None]*20
        self.f.bot.state['tp_distances'] = [None]*20
        original = deepcopy(self.group()['recovery_profile'])
        expected = [{k: o[k] for k in ('side', 'size', 'price', 'sl', 'tp')} for o in self.f.b.data['orders']]
        settings = deepcopy(c['cfg']); settings['INITIAL_TP_DISTANCE'] = '2.00'
        self.action('basket_settings', settings=settings); self.f.tick(16)
        self.assertFalse(c.get('manual'), c.get('manual_reason'))
        self.assertEqual(len(self.f.b.data['orders']), 3)
        self.assertEqual([{k: o[k] for k in ('side', 'size', 'price', 'sl', 'tp')} for o in self.f.b.data['orders']], expected)
        self.assertEqual(self.group()['recovery_profile'], original)
        self.assertEqual(D(c['cfg']['INITIAL_TP_DISTANCE']), D('2.00'))

    def start_slipped_link(self, close_source=True):
        self.select('mixed'); self.initial(); self.link()
        level = D(self.f.b.data['orders'][0]['price'])
        if close_source: self.f.b.close('SL')
        return level

    def fill_slipped(self, level, slip):
        pid = self.f.b.fill('SELL', str(level-D(slip)))
        self.f.b.quote(level-D(slip)-D('.05'), level-D(slip)+D('.05'))
        return pid

    def assert_ticket_target(self, pid, index):
        c = self.f.bot.state['cycle']; p = next(p for p in self.f.b.data['positions'] if p['id']==pid)
        self.assertFalse(c.get('manual'), c.get('manual_reason'))
        self.assertEqual(c['tickets'][pid]['index'], index)
        distance = D(self.group()['recovery_profile']['stages'][index]['tp_distance'])
        self.assertEqual(D(p['entry'])-D(p['tp']), distance)

    def test_partial_slipped_fill_anchors_before_source_exit_and_remaining_stops(self):
        level = self.start_slipped_link(close_source=False)
        source = self.f.b.data['positions'][0]['id']
        pid = self.fill_slipped(level, '.07'); self.f.tick(6)
        self.assert_ticket_target(pid, 0)
        self.assertFalse(self.f.bot.state['cycle']['tickets'][source]['closed'])
        self.assertEqual(len(self.f.b.data['orders']), 2)
        self.assertEqual(self.group()['actual_ladder_tx']['status'], 'DONE')

    def test_later_slipped_fill_gets_new_transaction_after_prior_done(self):
        level = self.start_slipped_link()
        first = self.fill_slipped(level, '.07'); self.f.tick(6)
        self.assert_ticket_target(first, 0)
        old_tx = deepcopy(self.group()['actual_ladder_tx'])
        self.assertEqual(old_tx['status'], 'DONE')
        second = self.fill_slipped(level, '.11'); self.f.tick(6)
        self.assert_ticket_target(first, 0); self.assert_ticket_target(second, 1)
        self.assertEqual([t['position_id'] for t in self.group()['actual_ladder_tx']['targets']], [second])
        self.assertEqual(set(self.group()['profile_anchored_positions']), {first, second})
        self.assertEqual(len(self.f.b.data['orders']), 1)

    def test_earlier_tp_close_before_later_fill_preserves_identity_and_target(self):
        level = self.start_slipped_link()
        first = self.fill_slipped(level, '.07'); self.f.tick(6)
        self.f.b.close('TP', position_id=first); self.f.tick(6)
        second = self.fill_slipped(level, '.11'); self.f.tick(8)
        self.assert_ticket_target(second, 1)
        self.assertTrue(self.f.bot.state['cycle']['tickets'][first]['closed'])
        self.assertEqual(self.f.bot.state['cycle']['tickets'][first]['index'], 0)
        self.assertEqual(len(self.f.b.data['orders']), 1)

    def test_unknown_anchor_restart_never_duplicates_or_overwrites_transaction(self):
        level = self.start_slipped_link()
        self.f.b.fail_before.add('MODIFY')
        first = self.fill_slipped(level, '.07'); self.f.tick(6)
        old_tx = deepcopy(self.group()['actual_ladder_tx'])
        self.assertEqual(old_tx['status'], 'PENDING')
        self.assertEqual([t['position_id'] for t in old_tx['targets']], [first])
        before = deepcopy(self.f.b.commands)
        second = self.fill_slipped(level, '.11')
        self.f.bot = ControlledRecoveryController(self.f.store, self.f.b); self.f.tick(12)
        self.assertEqual(self.group()['actual_ladder_tx'], old_tx)
        self.assertEqual(self.f.b.commands, before)
        self.assertNotIn(second, self.group()['profile_anchored_positions'])
        self.assertEqual(len([cmd for cmd in self.f.b.commands if cmd['kind']=='MODIFY']), 1)

    def test_unequal_fills_sl_adjust_uses_profile_baseline_in_view_and_action(self):
        for original, adjusted in [('1.50', '1.40'), ('1.75', '1.65')]:
            with self.subTest(sl=original):
                x = RecoveryProfiles(); x.setUp(); self.addCleanup(x.doCleanups)
                settings = defaults(original); settings['selected_profile_id'] = 'mixed'
                x.save_profiles(settings)
                level = x.start_slipped_link()
                for slip in ('.03', '.07', '.11'): x.fill_slipped(level, slip)
                x.f.tick(10)
                view = x.f.bot.v3_view(x.f.bot.snapshot())
                self.assertEqual(D(view['v3_sl_distance']), D(original))
                self.assertGreater(len({abs(D(p['entry'])-D(p['sl'])) for p in x.f.b.data['positions']}), 1)
                x.action('v3_adjust_sl', distance=adjusted, expected_positions=sorted(p['id'] for p in x.f.b.data['positions']))
                x.f.tick(10)
                self.assertFalse(x.f.bot.state['cycle'].get('manual'))
                self.assertEqual({D(p['sl'])-D(p['entry']) for p in x.f.b.data['positions']}, {D(adjusted)})
                self.assertEqual(D(x.f.bot.v3_view(x.f.bot.snapshot())['v3_sl_distance']), D(adjusted))

    def test_count_compatibility_rejects_mismatch_without_intents(self):
        self.loss(); before = deepcopy(self.f.bot.state['commands'])
        for count in (3, 0, 21, True, '6'):
            with self.subTest(count=count), self.assertRaises(ActionRejected): self.entry(count=count)
        self.assertEqual(self.f.bot.state['commands'], before)
        self.entry(count=6); self.assertEqual(len(self.f.b.data['positions']), 6)

    def test_link_count_mismatch_is_atomic(self):
        self.initial(); before = deepcopy(self.f.bot.state['commands'])
        with self.assertRaises(ActionRejected): self.link(count=3)
        self.assertEqual(self.f.bot.state['commands'], before)
        self.assertNotIn('v3_link', self.f.bot.state['cycle'])

    def test_profile_validation_is_atomic(self):
        before = deepcopy(self.f.bot.state['recovery_profiles'])
        bad = []
        for key, value in [('volume', '.001'), ('volume', 'NaN'), ('volume', 'Infinity'), ('volume', '0'), ('tp_distance', '.251'), ('tp_distance', 'NaN'), ('tp_distance', '-.25')]:
            s = defaults(); s['profiles'][0]['stages'][0][key] = value; bad.append(s)
        for sl in ('NaN', 'Infinity', '-1', '1.505'):
            s = defaults(sl); bad.append(s)
        s = defaults(); s['profiles'][0]['stages'] *= 4; bad.append(s)
        s = defaults(); s['profiles'][1]['id'] = 'six'; bad.append(s)
        s = defaults(); s['selected_profile_id'] = 'missing'; bad.append(s)
        for s in bad:
            with self.subTest(settings=s), self.assertRaises(ActionRejected): self.save_profiles(s)
            self.assertEqual(self.f.bot.state['recovery_profiles'], before)
            self.assertFalse(self.f.b.commands)

    def test_broker_min_point_one_rejects_point_zero_one_no_rounding(self):
        self.f.b.data['spec'].update(volume_min='.10', volume_step='.10')
        before = deepcopy(self.f.bot.state['recovery_profiles'])
        with self.assertRaises(ActionRejected): self.save_profiles(defaults())
        self.assertEqual(self.f.bot.state['recovery_profiles'], before)
        self.select('single')
        self.assertEqual(self.f.bot.state['recovery_profiles']['selected_profile_id'], 'single')

    def test_read_only_and_time_guards(self):
        self.loss()
        for guard in ('read_only', 'time_block'):
            with self.subTest(guard=guard):
                if guard == 'read_only': self.f.b.read_only = True
                else: self.f.bot.state['time_block'] = 'test blocked time'
                before = deepcopy(self.f.bot.state['commands'])
                with self.assertRaises(ActionRejected): self.entry()
                with self.assertRaises(ActionRejected): self.select('mixed')
                self.assertEqual(self.f.bot.state['commands'], before)
                self.f.b.read_only = False; self.f.bot.state.pop('time_block', None)

    def test_uncertain_entry_never_duplicate(self):
        self.loss(); self.f.b.fail_before.add('MARKET'); self.entry()
        before = len(self.f.b.commands)
        with self.assertRaises(ActionRejected): self.entry()
        self.f.tick(10); self.assertEqual(len(self.f.b.commands), before)

    def test_snapshot_detached_validated_and_original_indexes(self):
        spec = Spec.load(self.f.b.data['spec']); settings = defaults(); settings['selected_profile_id'] = 'mixed'
        snapshot = selected_snapshot(settings, spec); original = deepcopy(snapshot)
        settings['profiles'][1]['stages'][0]['volume'] = '.09'
        self.assertEqual(snapshot, original)
        self.assertEqual(D(validate_snapshot(snapshot, spec)['total_volume']), D('.08'))
        tickets = [dict(index=2, size='.01', entry='4000.17'), dict(index=0, size='.05', entry='4000.03')]
        self.assertEqual(exact_targets(spec, snapshot, 'BUY', tickets), ['4000.92', '4000.28'])
        self.assertEqual(exact_targets(spec, snapshot, 'SELL', tickets), ['3999.42', '3999.78'])
        for bad in [dict(snapshot, total_volume='.09'), dict(snapshot, sl_distance='NaN')]:
            with self.assertRaises(ValueError): validate_snapshot(bad, spec)
        for bad in [tickets+[tickets[0]], [dict(index=True, size='.02', entry='4000')], [dict(index=3, size='.01', entry='4000')], [dict(index=2, size='.02', entry='4000')]]:
            with self.assertRaises(ValueError): exact_targets(spec, snapshot, 'BUY', bad)

    def test_twenty_stage_limit(self):
        spec = Spec.load(self.f.b.data['spec'])
        settings = defaults(); settings['profiles'] = [settings['profiles'][0]]
        settings['profiles'][0]['stages'] = [dict(volume='.01', tp_distance=str(D('.25')*i)) for i in range(1, 21)]
        self.assertEqual(len(validate_settings(settings, spec)['profiles'][0]['stages']), 20)
        settings['profiles'][0]['stages'].append(dict(volume='.01', tp_distance='5.25'))
        with self.assertRaises(ValueError): validate_settings(settings, spec)


if __name__ == '__main__': unittest.main()
