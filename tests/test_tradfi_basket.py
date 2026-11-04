"""Actual basket controller on deterministic fake broker facts, never MT5/network."""
from copy import deepcopy
from decimal import Decimal as D
from pathlib import Path
import tempfile
import unittest
from tradfi.basket import BasketController
from tradfi.basket_math import BasketSettings,ladder,expanded_levels
from tradfi.broker import Spec
from tradfi.interactive_simulator import InteractiveSimulator
from tradfi.simulator import ARTIFICIAL_SPEC
from tradfi.storage import Store
from tradfi.control import ActionGateway

class BasketTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=Store(Path(self.tmp.name)/'basket.db');self.addCleanup(self.store.close)
        self.b=InteractiveSimulator(dict(ARTIFICIAL_SPEC,volume_min='0.01',volume_step='0.01',contract_size='100'))
        self.b.data.update(now=90,candle=dict(id=0,open='3999',close='4000'))
        self.bot=BasketController(self.store,self.b)
        self.bot.state['basket_settings']['INITIAL_SIZE']='0.01';self.bot.save('fixture')
    def tick(self,n=1):
        for _ in range(n):self.bot.tick()
    def bar(self,cid=60,side='BUY',close='4000'):
        self.b.data.update(now=cid+60,candle=dict(id=cid,open=str(D(close)+(-1 if side=='BUY' else 1)),close=close))
    def start(self,side='BUY'):
        self.bot.command('/start');self.bar(side=side);self.tick(2)
        self.assertEqual(len(self.b.data['orders']),1)
    def initial(self,side='BUY'):
        self.start(side);self.b.fill(side);entry=D(self.b.data['positions'][0]['entry']);self.b.quote(entry-D('.05'),entry+D('.05'));self.tick(3)
        self.assertEqual(len(self.b.data['orders']),6)
    def reversal(self,side='BUY'):
        self.initial(side);self.b.close('SL');other='SELL' if side=='BUY' else 'BUY'
        for _ in range(6):self.b.fill(other)
        entry=D(self.b.data['positions'][0]['entry']);self.b.quote(entry-D('.05'),entry+D('.05'));self.tick(3)
        self.assertEqual(self.bot.state['phase'],'RECOVERY_POSITION');self.assertEqual(len(self.b.data['positions']),6)
    def close_all(self,reason='SL'):
        for p in list(self.b.data['positions']):self.b.close(reason,position_id=p['id'])
    def restart(self):self.bot=BasketController(self.store,self.b)
    def test_november_2_mid_minute_start_and_24h_are_real_utc(self):
        from datetime import datetime,timezone
        now=datetime(2026,11,2,12,0,30,tzinfo=timezone.utc).timestamp()
        self.b.data.update(now=now,candle=dict(id=now-90,open='3999',close='4000'))
        self.bot=BasketController(self.store,self.b,clock=lambda:self.b.data['now'],monotonic=lambda:self.b.data['now'])
        self.bot.command('/start');self.tick(2);self.assertFalse(self.b.data['orders'])
        self.b.data.update(now=now+30,candle=dict(id=now-30,open='3999',close='4000'));self.tick(2)
        self.assertEqual(len(self.b.data['orders']),1)
        c=self.bot.state['cycle'];self.assertEqual(c['started_at_utc'],now+30)
        self.assertEqual(c['deadline_utc'],now+30+86400)
        self.assertEqual(c['signal']['id'],now-30)
        self.b.data['now']=c['deadline_utc']-1;self.assertFalse(self.bot._deadline_guard())
        self.b.data['now']+=1;self.assertTrue(self.bot._deadline_guard())
        self.assertEqual(c['manual_reason'],'CYCLE_24H_LIMIT')
    def test_realized_costs_are_retained_across_restart(self):
        self.reversal()
        self.b.close('TP',position_id=self.b.data['positions'][0]['id'],commission='-0.20',swap='-0.10',fee='-0.05')
        self.tick(2);c=self.bot.state['cycle']
        self.assertEqual(D(c['recovery']),D('3.20'))
        self.restart();self.tick(2);self.assertEqual(D(self.bot.state['cycle']['recovery']),D('3.20'))
    def test_costs_after_all_tps_do_not_falsely_complete(self):
        self.reversal()
        for p in list(self.b.data['positions']):self.b.close('TP',position_id=p['id'],commission='-1')
        self.tick(3)
        self.assertIsNotNone(self.bot.state['cycle']);self.assertEqual(D(self.bot.state['cycle']['recovery']),D('5.85'))
        self.assertFalse(self.bot.state['completed'])
    def test_retired_paid_round_waits_cancel_then_finishes_without_reentry(self):
        self.reversal();self.close_all('TP')
        c=self.bot.state['cycle'];self.bot._observe(self.bot.snapshot())
        self.assertLessEqual(D(c['recovery']),0)
        for g in c['groups']:g['retired']=True
        old_count=len(self.bot.state['commands']);self.restart();self.tick(5)
        self.assertIsNone(self.bot.state['cycle']);self.assertFalse(self.b.data['orders'])
        self.assertEqual(len(self.bot.state['commands']),old_count)
    def test_doji_and_stale_candle_keep_exact_pending(self):
        self.start();orders=deepcopy(self.b.data['orders'])
        self.b.data.update(now=180,candle=dict(id=120,open='4000',close='4000'));self.tick(2)
        self.assertEqual(self.b.data['orders'],orders)
        self.bar(cid=60,side='SELL');self.tick(2);self.assertEqual(self.b.data['orders'],orders)
    def test_initial_tp_cancels_reverse_basket_and_waits_new_close(self):
        self.initial();self.b.close('TP');self.tick(5)
        self.assertIsNone(self.bot.state['cycle']);self.assertFalse(self.b.data['orders']);self.assertFalse(self.b.data['positions'])
        self.assertEqual(self.bot.state['phase'],'WAIT_CANDLE')
    def test_initial_reversal_mirrors_and_protects_each_ticket(self):
        self.initial('SELL')
        self.assertEqual({o['side'] for o in self.b.data['orders']},{'BUY'})
        self.assertEqual({D(o['price']) for o in self.b.data['orders']},{D('4002')})
        self.assertEqual({D(o['sl']) for o in self.b.data['orders']},{D('4000.50')})
        self.assertEqual({D(o['size']) for o in self.b.data['orders']},{D('.01')})
    def test_equal_ladder_no_base_profit_and_no_trailing(self):
        self.reversal();c=self.bot.state['cycle'];self.assertEqual(D(c['recovery']),D(3))
        positions=self.b.data['positions'];tps=sorted(D(p['tp']) for p in positions)
        self.assertEqual(len(set(tps)),6);self.assertEqual(len({tps[i+1]-tps[i] for i in range(5)}),1)
        self.assertGreaterEqual(sum((self.b.profit(p['side'],D(p['size']),D(p['entry']),D(p['tp'])) for p in positions),D(0)),D(3))
        sl=[p['sl'] for p in positions];self.b.quote('3997.8','3997.9');self.tick(3);self.assertEqual([p['sl'] for p in positions],sl)
    def test_partial_tp_then_sl_expands_once_debt_persists_restart(self):
        self.reversal();initial_tps={p['id']:p['tp'] for p in self.b.data['positions']}
        for _ in range(2):self.b.close('TP',position_id=self.b.data['positions'][0]['id']);self.tick(2)
        self.assertEqual({p['id']:p['tp'] for p in self.b.data['positions']},{pid:tp for pid,tp in initial_tps.items() if pid in {p['id'] for p in self.b.data['positions']}})
        self.close_all('SL');self.tick(3);c=self.bot.state['cycle']
        self.assertEqual(c['expanded'],{'BUY':'4000.50','SELL':'3997.00'})
        frozen=deepcopy(c['expanded']);debt=c['recovery'];self.restart();self.tick(3)
        self.assertEqual(self.bot.state['cycle']['expanded'],frozen);self.assertEqual(self.bot.state['cycle']['recovery'],debt)
        self.assertEqual(len(self.b.data['orders']),12)
        for _ in range(6):self.b.fill('BUY')
        self.b.quote('4000.45','4000.55');self.tick(4);self.assertFalse(self.b.data['orders'])
        self.close_all('SL');self.tick(4);self.assertEqual(self.bot.state['cycle']['expanded'],frozen)
    def test_all_sl_debt_and_example_expansion(self):
        self.reversal();self.close_all();self.tick(3)
        self.assertEqual(D(self.bot.state['cycle']['recovery']),12)
        self.assertEqual(expanded_levels('SELL','3997','3998.5','1'),{'BUY':'3999.5','SELL':'3996'})
        self.assertEqual(expanded_levels('BUY','4003','4001.5','1'),{'BUY':'4004','SELL':'4000.5'})
    def test_partial_ticket_fill_halts_no_refill(self):
        self.initial();self.b.close('SL');self.b.fill('SELL',size='.005');self.tick(3)
        self.assertEqual(self.bot.state['phase'],'MANUAL');self.assertFalse(self.b.data['orders'])
    def test_recovery_fill_before_initial_close_halts_and_preserves_position(self):
        self.initial();self.b.fill('SELL');self.tick(3)
        self.assertEqual(self.bot.state['phase'],'MANUAL');self.assertEqual(len(self.b.data['positions']),2)
        self.restart();self.tick(2);self.assertEqual(len(self.b.data['positions']),2)
    def test_tp_cancel_fill_race_halts(self):
        self.initial();self.b.cancel_race='SELL';self.b.close('TP');self.tick(4)
        self.assertEqual(self.bot.state['phase'],'MANUAL');self.assertEqual(len(self.b.data['positions']),1)
    def test_unknown_entry_does_not_duplicate_on_restart(self):
        self.b.fail_before.add('STOP');self.bot.command('/start');self.bar();self.tick(3);self.restart();self.tick(3)
        self.assertEqual(len(self.b.commands),1)
    def test_live_settings_volume_only_future_and_sl_edit_reconciled(self):
        self.reversal();before={p['id']:p['size'] for p in self.b.data['positions']}
        cfg=dict(self.bot.state['basket_settings'],BASKET_COUNT=4,TICKET_SIZE='.02',RECOVERY_SL_DISTANCE='1.25')
        self.bot.edit(settings=cfg);self.tick(5)
        self.assertEqual({p['id']:p['size'] for p in self.b.data['positions']},before)
        self.assertEqual({D(p['sl']) for p in self.b.data['positions']},{D('3999.25')})
        self.assertEqual(self.bot.state['cycle']['cfg']['BASKET_COUNT'],4)
    def test_invalid_inputs_and_stale_levels_rejected_before_cancel(self):
        self.initial();before=len(self.b.commands)
        for settings in [dict(self.bot.state['basket_settings'],TICKET_SIZE='bad'),dict(self.bot.state['basket_settings'],TICKET_SIZE='.005')]:
            with self.assertRaises(ValueError):self.bot.edit(settings=settings)
        with self.assertRaises(ValueError):self.bot.edit(levels={'SELL':'3997'},expected={'SELL':'0'})
        self.assertEqual(len(self.b.commands),before)
    def test_count_pending_replacement_does_not_overspend(self):
        self.initial();self.bot.edit(settings=dict(self.bot.state['basket_settings'],BASKET_COUNT=4));self.tick(6)
        self.assertEqual(len(self.b.data['orders']),4);self.assertEqual(len(self.b.data['positions']),1)
    def test_fill_during_edit_cancellation_aborts(self):
        self.initial();self.bot.edit(settings=dict(self.bot.state['basket_settings'],BASKET_COUNT=4));self.b.cancel_race='SELL';self.tick(4)
        self.assertEqual(self.bot.state['phase'],'MANUAL');self.assertEqual(self.bot.state['cycle']['cfg']['BASKET_COUNT'],6)
    def test_old_cycle_is_preserved_and_blocked(self):
        self.start();self.bot.state['cycle'].pop('contract');debt=self.bot.state['cycle']['recovery'];self.bot.save('legacy_fixture');self.restart();self.tick()
        self.assertEqual(self.bot.state['phase'],'MIGRATION_BLOCK');self.assertEqual(self.bot.state['cycle']['recovery'],debt)
        with self.assertRaises(ValueError):self.bot.command('/start')
    def test_same_direction_keeps_initial_stop_flip_replaces(self):
        self.start();oid=self.b.data['orders'][0]['id'];self.bar(120,close='4000.2');self.tick(2);self.assertEqual(self.b.data['orders'][0]['id'],oid)
        self.bar(180,'SELL');self.tick(4);self.assertEqual(len(self.b.data['orders']),1);self.assertEqual(self.b.data['orders'][0]['side'],'SELL')

    def test_gap_profit_cleanup_waits_for_all_close_evidence(self):
        self.reversal();self.b.close('TP',profit='10',position_id=self.b.data['positions'][0]['id']);self.tick(6)
        self.assertIsNone(self.bot.state['cycle']);self.assertFalse(self.b.data['positions']);self.assertFalse(self.b.data['orders'])
        self.assertEqual(len([cmd for cmd in self.b.commands if cmd['kind']=='CLOSE']),5)
    def test_unknown_cleanup_close_never_repeated(self):
        self.reversal();self.b.fail_before.add('CLOSE');self.b.close('TP',profit='10',position_id=self.b.data['positions'][0]['id']);self.tick(4)
        sent=len(self.b.commands);self.restart();self.tick(4)
        self.assertEqual(len(self.b.commands),sent);self.assertIsNotNone(self.bot.state['cycle'])
    def test_stop_resume_waits_for_new_initial_candle(self):
        self.start();self.bot.command('/stop');self.tick(3);self.bot.command('/start');self.tick(2);self.assertFalse(self.b.data['orders'])
        self.bar(120);self.tick(3);self.assertEqual(len(self.b.data['orders']),1)
    def test_recovery_oco_double_fill_halts(self):
        self.reversal();self.close_all();self.tick(3);self.b.fill('BUY');self.b.fill('SELL');self.tick(3)
        self.assertEqual(self.bot.state['phase'],'MANUAL');self.assertEqual(len(self.b.data['positions']),2)
    def test_invalid_numbers_release_gateway_and_next_stop(self):
        gate=ActionGateway(self.bot,execute=self.bot.action)
        bad=dict(type='basket_settings',settings=dict(self.bot.state['basket_settings'],RECOVERY_SL_DISTANCE='bad'))
        self.assertEqual(gate.apply(dict(id='invalid-001',revision=0,action=bad))['status'],'REJECTED')
        self.assertEqual(gate.apply(dict(id='stop-00002',revision=1,action=dict(type='command',text='/stop')))['status'],'APPLIED')

import test_tradfi_native_interactive as native
from tradfi.evidence_broker import MT5EvidenceBroker

class NativeBasketTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.path=Path(self.tmp.name)/'native.db'
        self.api=native.NativeAPI();self.api.info=self.api.info._replace(trade_stops_level=1)
        self.config=dict(backend='mt5_direct_v1',time_profile='injected_utc_test_v1',terminal_path='C:/Program Files/MetaTrader 5/terminal64.exe',expected_login=111111111,expected_server='Bybit-Live-6',symbol='XAUUSD.s',enable_trading=True,legacy_ea_removed=True,legacy_state_reconciled=True,magic=native.MAGIC)
        self.restart();self.bot.state['basket_settings']['INITIAL_SIZE']='0.01';self.bot.save('native_fixture')
    def restart(self):
        if hasattr(self,'store'):self.broker.close();self.store.close()
        self.store=Store(self.path);self.broker=MT5EvidenceBroker(self.config,self.store,api=self.api,clock=lambda:self.api.now,monotonic=lambda:self.api.now);self.broker.connect()
        self.bot=BasketController(self.store,self.broker,clock=lambda:self.api.now,monotonic=lambda:self.api.now)
    def tearDown(self):self.broker.close();self.store.close()
    def tick(self,n=1):
        for _ in range(n):self.bot.tick()
    def initial(self):
        self.bot.command('/start');self.api.now+=60;self.api.tick=self.api.tick._replace(time_msc=self.api.now*1000);self.api.rates=[dict(time=int(self.api.now//60)*60-60,open=3999.,close=4000.)];self.tick(2)
        self.assertEqual(len(self.api.orders),1);self.api.fill();self.tick(3);self.assertEqual(len(self.api.orders),6)
    def reversal(self):
        self.initial();self.api.close_position(reason=self.api.DEAL_REASON_SL)
        for _ in range(6):self.api.fill()
        self.tick(4);self.assertEqual(self.bot.state['phase'],'RECOVERY_POSITION')
    def test_native_six_tickets_ledger_tp_and_restart(self):
        self.reversal();self.assertEqual(D(self.bot.state['cycle']['recovery']),3)
        for _ in range(6):self.api.close_position(reason=self.api.DEAL_REASON_TP)
        self.tick(5);self.assertIsNone(self.bot.state['cycle'])
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM consumed_deals').fetchone()[0],7)
        sent=len(self.api.sends);self.restart();self.tick(3);self.assertEqual(len(self.api.sends),sent)
    def test_native_sl_expansion_persisted_before_reentry(self):
        self.reversal()
        for _ in range(6):self.api.close_position(reason=self.api.DEAL_REASON_SL)
        self.api.tick=self.api.tick._replace(bid=3999.45,ask=3999.55);self.tick(3)
        self.assertEqual(self.bot.state['cycle']['expanded'],{'BUY':'4000.50','SELL':'3997.00'});self.assertEqual(len(self.api.orders),12)
        self.restart();self.tick(3);self.assertEqual(len(self.api.orders),12)
    def test_delayed_cancel_history_blocks_edit_replacement(self):
        self.initial();self.api.delay_cancel=True;self.bot.edit(settings=dict(self.bot.state['basket_settings'],BASKET_COUNT=4));self.tick(4);self.restart();self.tick(3)
        self.assertEqual(len([r for r in self.api.sends if r['action']==self.api.TRADE_ACTION_PENDING]),7)
        self.assertIsNotNone(self.bot.state['cycle']['edit'])
    def test_netting_account_rejected_before_requests(self):
        self.api.account=self.api.account._replace(margin_mode=0)
        with self.assertRaises(ValueError):self.bot.command('/start')
        self.assertFalse(self.api.sends)
    def test_unsupported_point01_volume_rejected_not_rounded(self):
        self.api.info=self.api.info._replace(volume_min=.03,volume_step=.03)
        with self.assertRaises(ValueError):self.bot.command('/start')
        self.assertFalse(self.api.sends)

    def test_native_manual_reset_retains_debt_no_writeoff(self):
        self.reversal();self.bot._enter_manual(self.bot.state['cycle'],'TEST_MANUAL',self.api.now)
        for _ in range(6):self.api.close_position(reason=self.api.DEAL_REASON_SL)
        self.bot.command('/reset_manual')
        self.assertEqual(self.bot.state['phase'],'WAIT_START');self.assertEqual(D(self.bot.state['basket_carry']),12)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM consumed_deals').fetchone()[0],7)
    def test_native_two_partial_tps_then_sl_exact_debt(self):
        self.reversal();self.api.close_position(reason=self.api.DEAL_REASON_TP);self.tick(2);self.api.close_position(reason=self.api.DEAL_REASON_TP);self.tick(2)
        # Tick-rounded spacing is .15, hence profits .15+.30; four SLs cost 6.
        for _ in range(4):self.api.close_position(reason=self.api.DEAL_REASON_SL)
        self.api.tick=self.api.tick._replace(bid=3999.45,ask=3999.55);self.tick(3)
        self.assertEqual(D(self.bot.state['cycle']['recovery']),D('8.55'))
    def test_native_gap_cleanup_close_witnesses_and_ledger(self):
        self.reversal();self.api.close_position(reason=self.api.DEAL_REASON_TP,profit=10);self.tick(6)
        self.assertFalse(self.api.positions);self.assertFalse(self.api.orders);self.assertIsNone(self.bot.state['cycle'])
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM consumed_deals').fetchone()[0],7)
    def test_native_lost_modify_response_reconciles_without_repeat(self):
        self.reversal();original=self.api.order_send
        def lost(req):
            result=original(req)
            return None if req['action']==self.api.TRADE_ACTION_SLTP else result
        self.api.order_send=lost
        self.bot.edit(settings=dict(self.bot.state['basket_settings'],RECOVERY_SL_DISTANCE='1.25'));self.tick(6)
        sent=len(self.api.sends);self.restart();self.tick(3);self.assertEqual(len(self.api.sends),sent);self.assertIsNone(self.bot.state['cycle'].get('edit'))
    def test_native_partial_modify_failure_preserves_actual_readback(self):
        self.reversal();original=self.api.order_send;count=[0]
        def reject(req):
            if req['action']==self.api.TRADE_ACTION_SLTP:
                count[0]+=1
                if count[0]==2:return native.record(retcode=self.api.TRADE_RETCODE_INVALID_STOPS,order=0,deal=0,comment='test')
            return original(req)
        self.api.order_send=reject
        self.bot.edit(settings=dict(self.bot.state['basket_settings'],RECOVERY_SL_DISTANCE='1.25'));self.tick(6)
        self.assertEqual(self.bot.state['phase'],'MANUAL')
        self.assertGreater(len({p.sl for p in self.api.positions}),1)
    def test_native_crash_closure_transaction_no_duplicate_debt(self):
        from tradfi.storage import DurabilityError
        self.reversal();self.api.close_position(reason=self.api.DEAL_REASON_TP)
        def crash(event):
            if event=='closure_snapshot_written':raise RuntimeError('power loss')
        self.store.fault=crash
        with self.assertRaises(DurabilityError):self.tick()
        self.store.fault=None;self.restart();self.tick(3)
        self.assertEqual(D(self.bot.state['cycle']['recovery']),D('2.85'))
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM consumed_deals').fetchone()[0],2)

class BasketArithmeticTest(unittest.TestCase):
    def test_normalized_confirmed_formula_and_mirror(self):
        b=InteractiveSimulator(dict(ARTIFICIAL_SPEC,tick_size='0.00000001',point='0.00000001',volume_min='.01',volume_step='.01',contract_size='1'))
        spec=Spec.load(b.data['spec'])
        for side in ('BUY','SELL'):
            tps=ladder(b,spec,side,['4000']*6,['.01']*6,'.03')
            step=abs(D(tps[0])-4000)
            self.assertLess(abs(step-D(1)/7),D('0.00000001'))
            two=sum((b.profit(side,D('.01'),D(4000),D(tp)) for tp in tps[:2]),D(0))
            self.assertLess(abs((D('.03')-two+D('.06'))-D('.08571428571428571428571428571')),D('.000000001'))
            self.assertEqual(D('.03')+D('.09'),D('.12'))

class BasketEditRaceTest(BasketTest):
    # Reuse fixtures; inherited core cases are also run for this test class.
    def test_edit_crash_before_modify_send_is_resumed_once(self):
        from tradfi.storage import DurabilityError
        self.reversal();self.bot.edit(settings=dict(self.bot.state['basket_settings'],RECOVERY_SL_DISTANCE='1.25'))
        def crash(event):
            if event=='basket_edit_modifications':raise RuntimeError('power loss before intents committed')
        self.store.fault=crash
        with self.assertRaises(DurabilityError):self.tick()
        self.store.fault=None;self.restart();self.tick(6)
        modifications=[c for c in self.b.commands if c['kind']=='MODIFY']
        self.assertEqual(len(modifications),6);self.assertIsNone(self.bot.state['cycle'].get('edit'))
    def test_duplicate_confirmations_and_stale_tabs_do_not_repeat(self):
        from tradfi.control import Conflict
        self.reversal();gate=ActionGateway(self.bot,execute=self.bot.action)
        action=dict(type='basket_settings',cycle_id=self.bot.state['cycle']['id'],settings=dict(self.bot.state['basket_settings'],RECOVERY_SL_DISTANCE='1.25'))
        request=dict(id='same-edit-001',revision=0,action=action)
        self.assertEqual(gate.apply(request)['status'],'APPLIED');self.assertEqual(gate.apply(request)['status'],'APPLIED');self.tick(5)
        self.assertEqual(len([c for c in self.b.commands if c['kind']=='MODIFY']),6)
        with self.assertRaises(Conflict):gate.apply(dict(id='stale-tab-01',revision=0,action=action))
    def test_stale_cycle_and_order_ids_reject_before_cancel(self):
        self.initial();before=len(self.b.commands);gate=ActionGateway(self.bot,execute=self.bot.action)
        a=dict(type='basket_levels',cycle_id='old-cycle',levels={'SELL':'3997'},expected=self.bot.levels(),expected_orders=[o['id'] for o in self.b.data['orders']])
        self.assertEqual(gate.apply(dict(id='old-cycle-01',revision=0,action=a))['status'],'REJECTED')
        a['cycle_id']=self.bot.state['cycle']['id'];a['expected_orders']=['old-order']
        self.assertEqual(gate.apply(dict(id='old-orders-01',revision=1,action=a))['status'],'REJECTED');self.assertEqual(len(self.b.commands),before)
    def test_manual_sl_updates_whole_live_group_tp_only_one_ticket(self):
        self.reversal();p=deepcopy(self.b.data['positions'][0]);other_tps={q['id']:q['tp'] for q in self.b.data['positions'][1:]}
        self.bot.edit(protection=dict(position_id=p['id'],old={k:p[k] for k in ('sl','tp')},new=dict(sl='3999.25',tp='3997.6')));self.tick(5)
        self.assertEqual({D(q['sl']) for q in self.b.data['positions']},{D('3999.25')})
        self.assertEqual({q['id']:q['tp'] for q in self.b.data['positions'][1:]},other_tps)
    def test_gap_fills_recalculate_tp_from_actual_entries_without_widening_sl(self):
        self.initial();self.b.close('SL')
        for i in range(6):self.b.fill('SELL',price=str(D('3998')-D(i)/100))
        self.b.quote('3997.90','3998.00');self.tick(4)
        self.assertEqual({D(p['sl']) for p in self.b.data['positions']},{D('3999.50')})
        tps=sorted(D(p['tp']) for p in self.b.data['positions']);self.assertEqual(len({tps[i+1]-tps[i] for i in range(5)}),1)
        self.assertGreaterEqual(sum((self.b.profit(p['side'],D(p['size']),D(p['entry']),D(p['tp'])) for p in self.b.data['positions']),D(0)),D(3))
    def test_pending_level_manual_edit_persists_frozen_anchor(self):
        self.reversal();self.close_all();self.tick(3)
        self.bot.edit(levels={'BUY':'4001.00','SELL':'3996.50'},expected=self.bot.levels());self.tick(5);self.restart();self.tick(3)
        self.assertEqual(self.bot.state['cycle']['expanded'],{'BUY':'4001.00','SELL':'3996.50'})
        self.assertEqual({D(o['price']) for o in self.b.data['orders']},{D('4001'),D('3996.5')})
