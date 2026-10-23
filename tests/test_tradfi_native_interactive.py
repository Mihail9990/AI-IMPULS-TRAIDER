"""Native adapter/evidence/controller integration using injected MT5 bindings only."""
from copy import deepcopy
from decimal import Decimal as D
import json
from pathlib import Path
import tempfile
import unittest
from test_tradfi_evidence_controller import TradingAPI
from test_tradfi_evidence import MAGIC
from test_tradfi_mt5 import record
from tradfi.evidence_broker import MT5EvidenceBroker
from tradfi.interactive import InteractiveController
from tradfi.storage import Store,DurabilityError
from tradfi.strategies.fixed_recovery import RecoveryConfig


class NativeAPI(TradingAPI):
    DEAL_REASON_EXPERT=3
    is_test_binding=True
    def __init__(self):
        super().__init__();self.lose_close_reply=False;self.skip_close=False
    def copy_rates_from_pos(self,symbol,timeframe,start,count):
        if count==120:
            end=int(self.now//60)*60
            return [dict(time=end-60*i,open=4000.,high=4001.,low=3999.,close=4000.) for i in range(119,-1,-1)]
        return [dict(r,open=r['close'],high=r['close']+1,low=r['close']-1) for r in self.rates]
    def fill(self,*args,**kwargs):
        result=super().fill(*args,**kwargs)
        self.deals[-1]=record(**dict(self.deals[-1]._asdict(),commission=0.,swap=0.,fee=0.))
        return result
    def close_position(self,*args,**kwargs):
        result=super().close_position(*args,**kwargs)
        self.deals[-1]=record(**dict(result._asdict(),commission=0.,swap=0.,fee=0.))
        return self.deals[-1]
    def order_send(self,req):
        if req['action']!=self.TRADE_ACTION_DEAL:return super().order_send(req)
        self.sends.append(deepcopy(req))
        if self.skip_close:return None
        p=next(p for p in self.positions if p.ticket==req['position'])
        assert req['type']==1-p.type and req['volume']==p.volume
        price=self.tick.bid if p.type==0 else self.tick.ask
        ticket=self.next_id();deal=self.next_id()
        self.history.append(record(ticket=ticket,symbol=p.symbol,magic=req['magic'],comment=req['comment'],
            type=req['type'],state=self.ORDER_STATE_FILLED,volume_initial=p.volume,volume_current=0.,
            price_open=price,sl=p.sl,tp=p.tp,position_id=p.identifier,time_setup_msc=self.now*1000,time_done_msc=self.now*1000+1))
        self.deals.append(record(ticket=deal,order=ticket,position_id=p.identifier,symbol=p.symbol,magic=req['magic'],
            entry=1,type=req['type'],volume=p.volume,price=price,profit=round((price-p.price_open)*p.volume*100*(1 if p.type==0 else -1),8),
            commission=0.,swap=0.,fee=0.,reason=3,time_msc=self.now*1000+1))
        self.positions.remove(p)
        return None if self.lose_close_reply else record(retcode=self.TRADE_RETCODE_DONE,order=ticket,deal=deal,comment='fake')


class NativeInteractiveTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'native.db';self.api=NativeAPI()
        self.config=dict(backend='mt5_direct_v1',time_profile='injected_utc_test_v1',
            terminal_path='C:/Program Files/MetaTrader 5/terminal64.exe',expected_login=111111111,
            expected_server='Bybit-Live-6',symbol='XAUUSD.s',enable_trading=True,
            legacy_ea_removed=True,legacy_state_reconciled=True,magic=MAGIC)
        self.restart()
    def restart(self):
        if hasattr(self,'store'):self.broker.close();self.store.close()
        self.store=Store(self.path)
        self.broker=MT5EvidenceBroker(self.config,self.store,api=self.api,clock=lambda:self.api.now,monotonic=lambda:self.api.now)
        self.broker.connect();self.bot=InteractiveController(self.store,self.broker,clock=lambda:self.api.now,monotonic=lambda:self.api.now)
    def tearDown(self):self.broker.close();self.store.close()
    def tick(self,n=1):
        for _ in range(n):self.bot.tick()
    def start(self):
        self.bot.configure('fixed',RecoveryConfig().dump());self.bot.command('/start semi')
        self.bot.triggers('ranges',dict(BUY='3',SELL='3'));self.tick(2)
        self.assertEqual(len(self.api.orders),2)
    def position(self):
        self.start();self.api.fill();self.tick(3)
        self.assertEqual(self.bot.state['phase'],'POSITION');self.assertFalse(self.api.orders)
    def recovery(self):
        self.position();self.api.close_position(reason=self.api.DEAL_REASON_SL)
        p=self.api.deals[-1].price;self.api.tick=self.api.tick._replace(bid=p-.05,ask=p+.05)
        self.tick(2);self.api.fill();self.tick(3)
        self.assertEqual(self.bot.state['phase'],'POSITION')
    def close(self):
        p=self.api.positions[0];self.bot.close_position(str(p.identifier),str(p.volume));self.tick(3)
    def ledger(self):return [json.loads(r[0]) for r in self.store.db.execute('SELECT payload FROM consumed_deals')]

    def test_native_pending_fill_oco_and_manual_close_atomic_ledger(self):
        self.position();self.close()
        self.assertFalse(self.api.positions);self.assertEqual(self.bot.state['phase'],'WAIT_TRIGGER')
        self.assertEqual(len(self.ledger()),1)
        self.assertEqual(self.ledger()[0]['net_accounting']['contract'],'full_position_costs_v1')
        self.assertTrue(self.ledger()[0]['net_accounting']['opening_facts'])
        sent=len(self.api.sends);self.restart();self.tick(3)
        self.assertEqual(len(self.ledger()),1);self.assertEqual(len(self.api.sends),sent)

    def test_lost_close_reply_correlates_exact_order_marker_after_restart(self):
        self.position();self.api.lose_close_reply=True
        p=self.api.positions[0];self.bot.close_position(str(p.identifier),str(p.volume))
        self.restart();self.tick(3)
        self.assertEqual(len(self.ledger()),1)
        self.assertEqual(len([r for r in self.api.sends if r['action']==self.api.TRADE_ACTION_DEAL]),1)

    def test_unknown_not_executed_close_never_repeated(self):
        self.position();self.api.skip_close=True;self.close();self.restart();self.tick(3)
        self.assertTrue(self.api.positions);self.assertFalse(self.ledger())
        self.assertEqual(len([r for r in self.api.sends if r['action']==self.api.TRADE_ACTION_DEAL]),1)

    def test_native_fixed_recovery_n_immediately_modifies_tp_not_sl(self):
        self.recovery();p=self.api.positions[0];sl=p.sl
        self.bot.configure('fixed',RecoveryConfig(parts=5).dump());self.tick(3)
        self.assertEqual(self.api.positions[0].sl,sl)
        self.assertAlmostEqual(self.api.positions[0].tp,p.price_open+.4)
        self.assertEqual(len(self.ledger()),1)

    def test_native_open_and_exit_costs_once(self):
        self.position()
        # Change is intentionally before first cost witness of exit; opening facts are immutable.
        self.api.close_position(reason=self.api.DEAL_REASON_TP)
        self.api.deals[-1]=record(**dict(self.api.deals[-1]._asdict(),commission=-3.))
        self.tick(1)
        self.assertEqual(D(self.bot.state['cycle']['fixed_recovery']['debt']),D('.75'))
        self.assertEqual(len(self.ledger()),1)
        self.restart();self.tick()
        self.assertEqual(D(self.bot.state['cycle']['fixed_recovery']['debt']),D('.75'))

    def test_atomic_cost_commit_failure_rolls_back_and_restart_consumes_once(self):
        self.position();self.api.close_position(reason=self.api.DEAL_REASON_TP)
        def crash(event):
            if event=='closure_snapshot_written':raise RuntimeError('power loss')
        self.store.fault=crash
        with self.assertRaises(DurabilityError):self.tick()
        self.assertFalse(self.ledger());self.restart();self.tick()
        self.assertEqual(len(self.ledger()),1)

    def test_unrelated_expert_exit_is_not_manual_close_proof(self):
        self.position();self.api.close_position(reason=3);self.tick()
        self.assertEqual(self.bot.state['phase'],'SAFE_PAUSE');self.assertFalse(self.ledger())

    def test_existing_legacy_cycle_bound_before_settings_command(self):
        self.position();c=self.bot.state['cycle']
        for key in ('engine','operation_mode','module_config','fixed_recovery'):c.pop(key,None)
        c['recovery']='54';self.bot.state.pop('modules');self.bot.state.pop('interactive_contract')
        self.bot.save('legacy_fixture');self.restart()
        self.bot.configure('fixed',RecoveryConfig().dump());self.tick()
        self.assertEqual(self.bot.state['cycle']['engine'],'legacy')
        self.assertEqual(self.bot.state['cycle']['recovery'],'54')
        self.assertNotIn('fixed_recovery',self.bot.state['cycle'])

    def test_recovery_close_rechecks_target_after_order_check(self):
        self.recovery();p=self.api.positions[0]
        self.api.tick=self.api.tick._replace(bid=p.price_open+.5,ask=p.price_open+.6)
        original=self.api.order_check
        def checked(request):
            result=original(request)
            if request['action']==self.api.TRADE_ACTION_DEAL:
                self.api.tick=self.api.tick._replace(bid=p.price_open-1,ask=p.price_open-.9)
            return result
        self.api.order_check=checked
        self.bot.configure('fixed',RecoveryConfig(parts=5).dump());self.tick(3)
        self.assertTrue(self.api.positions)
        self.assertFalse([r for r in self.api.sends if r['action']==self.api.TRADE_ACTION_DEAL])
        rows=[r for r in self.broker.rows.values() if r['command']['kind']=='CLOSE']
        self.assertTrue(rows);self.assertTrue(all(r['send_started'] is False for r in rows))

    def test_native_replace_pending_confirmed_and_restart_no_duplicates(self):
        self.start();old={o.ticket for o in self.api.orders}
        self.bot.triggers('ranges',{'BUY':'4','SELL':'4'});self.tick(4)
        self.assertEqual(len(self.api.orders),2);self.assertFalse(old & {o.ticket for o in self.api.orders})
        sent=len(self.api.sends);self.restart();self.tick(3);self.assertEqual(len(self.api.sends),sent)

    def test_native_replace_fill_race_protects_position_without_replacing_pair(self):
        self.start();self.api.fill()
        self.bot.triggers('ranges',{'BUY':'4','SELL':'4'});self.tick(4)
        self.assertEqual(len(self.api.positions),1);self.assertFalse(self.api.orders)
        self.assertEqual(self.bot.state['phase'],'POSITION')
        self.assertGreater(self.api.positions[0].sl,0)

    def test_native_unknown_cancel_prevents_replacement_and_restart_replay(self):
        self.start();self.api.unknown=True
        self.bot.triggers('ranges',{'BUY':'4','SELL':'4'});self.tick(3)
        before=len(self.api.sends);self.restart();self.tick(3)
        self.assertEqual(len(self.api.sends),before)
        self.assertEqual(len([r for r in self.api.sends if r['action']==self.api.TRADE_ACTION_PENDING]),2)

    def test_fixed_emergency_reset_explicitly_retains_unsettled_debt(self):
        self.recovery();self.close();c=self.bot.state['cycle'];debt=c['fixed_recovery']['debt']
        c['manual']=True;self.bot.state['phase']='MANUAL';self.bot.save('manual_fixture')
        with self.assertRaisesRegex(ValueError,'debt write-off is not authorized'):self.bot.command('/reset_manual')
        self.assertEqual(self.bot.state['cycle']['fixed_recovery']['debt'],debt)
        self.assertEqual(self.bot.state['phase'],'MANUAL')

    def test_native_time_latch_cancels_pending_and_survives_restart(self):
        self.start();self.bot.state['time_block']={'reason':'CLOCK_JUMP'};self.bot.save('time_fixture');self.tick(3)
        self.assertFalse(self.api.orders);sent=len(self.api.sends);self.restart();self.tick(3)
        self.assertEqual(len(self.api.sends),sent);self.assertIn('time_block',self.bot.state)
        with self.assertRaises(ValueError):self.bot.triggers('ranges',{'BUY':'3','SELL':'3'})

    def test_native_time_latch_still_allows_confirmed_position_protection(self):
        self.position();self.bot.state['time_block']={'reason':'CLOCK_JUMP'};self.bot.save('time_fixture')
        p=self.bot.snapshot()['positions'][0];target=str(D(p['tp'])+D('.25'))
        self.bot.protection(p['id'],{'sl':p['sl'],'tp':p['tp']},{'sl':p['sl'],'tp':target});self.tick(2)
        self.assertEqual(D(str(self.api.positions[0].tp)),D(target));self.assertIn('time_block',self.bot.state)
