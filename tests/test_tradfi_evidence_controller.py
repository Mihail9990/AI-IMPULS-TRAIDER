"""Controller -> MT5 double -> durable evidence -> atomic money/state integration."""
from copy import deepcopy
from decimal import Decimal as D
import json
import multiprocessing
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import ModuleType

from test_tradfi_evidence import API, MAGIC
from test_tradfi_mt5 import record
from tradfi.engine import Controller
from tradfi.evidence_broker import MT5EvidenceBroker
from tradfi.storage import Store, DurabilityError
from tradfi.time_profile import HistoryTimeUnverified, TimeProfileError


class TradingAPI(API):
    def __init__(self):
        super().__init__()
        self.sequence=100
        self.unknown=False
        self.delay_cancel=False

    def next_id(self):
        self.sequence+=1
        return self.sequence

    def order_send(self, req):
        self.sends.append(deepcopy(req))
        if self.unknown: return None
        action=req['action']; ticket=0
        if action==self.TRADE_ACTION_PENDING:
            ticket=self.next_id()
            self.orders.append(record(ticket=ticket,symbol=req['symbol'],magic=req['magic'],comment=req['comment'],
                type=req['type'],state=self.ORDER_STATE_PLACED,volume_initial=req['volume'],volume_current=req['volume'],
                price_open=req['price'],sl=req['sl'],tp=req['tp'],position_id=0,
                time_setup_msc=self.now*1000,time_done_msc=0))
        elif action==self.TRADE_ACTION_REMOVE:
            order=next(o for o in self.orders if o.ticket==req['order'])
            self.orders.remove(order)
            if not self.delay_cancel:
                self.history.append(order._replace(state=self.ORDER_STATE_CANCELED,time_done_msc=self.now*1000))
        elif action==self.TRADE_ACTION_SLTP:
            p=next(p for p in self.positions if p.ticket==req['position'])
            self.positions[self.positions.index(p)]=p._replace(sl=req['sl'],tp=req['tp'])
        else: raise AssertionError('Unexpected action')
        return record(retcode=self.TRADE_RETCODE_DONE,order=ticket,deal=0,comment='ok')

    def fill(self, index=0, volume=None):
        order=self.orders.pop(index); pid=self.next_id(); side=0 if order.type==4 else 1
        volume=order.volume_initial if volume is None else volume
        self.history.append(order._replace(state=self.ORDER_STATE_FILLED,volume_current=0.,position_id=pid,time_done_msc=self.now*1000))
        self.deals.append(record(ticket=self.next_id(),order=order.ticket,position_id=pid,symbol=order.symbol,
            magic=order.magic,entry=0,type=side,volume=volume,price=order.price_open,profit=0.,reason=3,time_msc=self.now*1000))
        self.positions.append(record(ticket=self.next_id(),identifier=pid,symbol=order.symbol,magic=order.magic,
            type=side,volume=volume,price_open=order.price_open,sl=order.sl,tp=order.tp))
        self.tick=self.tick._replace(bid=order.price_open,ask=order.price_open+.1)
        return pid

    def close_position(self, reason=5, volume=None, profit=None):
        p=self.positions[0]; size=p.volume if volume is None else volume
        price=p.tp if reason==5 else p.sl
        profit=(price-p.price_open)*size*100*(1 if p.type==0 else -1) if profit is None else profit
        order=record(ticket=self.next_id(),symbol=p.symbol,magic=p.magic,comment='close',type=1-p.type,
            state=self.ORDER_STATE_FILLED,volume_initial=size,volume_current=0.,price_open=price,
            sl=p.sl,tp=p.tp,position_id=p.identifier,time_setup_msc=self.now*1000,time_done_msc=self.now*1000+1)
        self.history.append(order)
        deal=record(ticket=self.next_id(),order=order.ticket,position_id=p.identifier,symbol=p.symbol,
            magic=p.magic,entry=1,type=1-p.type,volume=size,price=price,profit=round(profit,8),reason=reason,time_msc=self.now*1000+1)
        self.deals.append(deal)
        if size==p.volume: self.positions.remove(p)
        else: self.positions[0]=p._replace(volume=round(p.volume-size,8))
        return deal


class EvidenceControllerTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'state.db'; self.api=TradingAPI()
        self.config=dict(backend='mt5_direct_v1',time_profile='injected_utc_test_v1',
            terminal_path='C:/Program Files/MetaTrader 5/terminal64.exe',expected_login=111111111,
            expected_server='Bybit-Live-6',symbol='XAUUSD.s',enable_trading=True,
            legacy_ea_removed=True,legacy_state_reconciled=True,magic=MAGIC)
        self.restart()

    def restart(self):
        if hasattr(self,'store'):
            self.broker.close(); self.store.close()
        self.store=Store(self.path)
        self.broker=MT5EvidenceBroker(self.config,self.store,api=self.api,clock=lambda:self.api.now)
        self.broker.connect(); self.bot=Controller(self.store,self.broker,clock=lambda:self.api.now,monotonic=lambda:self.api.now)

    def tearDown(self): self.broker.close(); self.store.close()
    def start_pair(self):
        self.bot.command('/start'); self.api.rates[0]['time']+=60; self.bot.tick()
        self.assertEqual(self.bot.state['phase'],'PAIR')
        self.assertEqual(len(self.api.orders),2)
    def position(self):
        self.start_pair(); self.api.fill(); self.bot.tick(); self.bot.tick()
        self.assertEqual(self.bot.state['phase'],'POSITION')
        self.assertFalse(self.api.orders)
    def ledger(self):
        return [json.loads(r[0]) for r in self.store.db.execute('SELECT payload FROM consumed_deals')]

    def test_controller_completes_and_restart_duplicates_do_not_book_again(self):
        self.position(); deal=self.api.close_position()
        self.api.deals=[deal,*reversed(self.api.deals)]
        self.bot.tick()
        self.assertEqual(self.bot.state['phase'],'COOLDOWN')
        self.assertEqual(self.bot.state['seen_deals'],[str(deal.ticket)])
        self.assertEqual(D(self.bot.state['completed'][0]['realized']),D('1.8'))
        self.assertEqual(len(self.ledger()),1)
        before=deepcopy(self.bot.state); sends=len(self.api.sends)
        self.restart(); self.bot.tick(); self.bot.tick()
        self.assertEqual(self.bot.state,before); self.assertEqual(len(self.api.sends),sends)
        self.assertFalse(self.broker.snapshot()['history_complete'])

    def test_sl_recovery_and_new_pair_after_atomic_consumption(self):
        self.position(); deal=self.api.close_position(reason=4); self.bot.tick()
        c=self.bot.state['cycle']
        self.assertEqual(self.bot.state['phase'],'NEXT_PAIR')
        self.assertEqual(c['branch'],'BRANCH1_RECOVERY'); self.assertIsNone(c['position'])
        self.assertEqual(D(c['recovery']),D('19.8')); self.assertEqual(D(c['realized']),D('-18'))
        self.api.tick=self.api.tick._replace(bid=4000.,ask=4000.1)
        self.restart(); self.bot.tick()
        self.assertEqual(self.bot.state['phase'],'PAIR'); self.assertEqual(len(self.api.orders),2)
        self.assertEqual(len(self.ledger()),1); self.assertEqual(self.bot.state['seen_deals'],[str(deal.ticket)])

    def test_crash_each_atomic_boundary_replays_exactly_once(self):
        for stage in ('deals_inserted','closure_snapshot_written','closure_before_commit','closure_after_commit'):
            with self.subTest(stage=stage):
                if self.bot.state['phase']!='IDLE':
                    self.broker.close(); self.store.close()
                    self.path=Path(self.tmp.name)/(stage+'.db'); self.api=TradingAPI()
                    del self.store; self.restart()
                self.position(); deal=self.api.close_position(reason=4)
                def crash(event):
                    if event==stage: raise RuntimeError('power loss')
                self.store.fault=crash
                with self.assertRaises(DurabilityError): self.bot.tick()
                committed=stage=='closure_after_commit'
                self.assertEqual(len(self.ledger()),int(committed))
                self.assertEqual(self.store.load()['phase'],'NEXT_PAIR' if committed else 'POSITION')
                self.assertEqual(self.bot.state,self.store.load())
                self.restart(); sends=len(self.api.sends)
                if not committed: self.bot.tick()
                self.assertEqual(self.bot.state['seen_deals'],[str(deal.ticket)])
                self.assertEqual(D(self.bot.state['cycle']['recovery']),D('19.8'))
                self.assertEqual(len(self.ledger()),1); self.assertEqual(len(self.api.sends),sends)

    def test_delayed_exit_and_oco_history_never_justify_next_cycle(self):
        self.start_pair(); self.api.delay_cancel=True; self.api.fill(); self.bot.tick()
        deal=self.api.close_position(); self.api.deals.remove(deal)
        self.bot.tick(); self.assertEqual(self.ledger(),[])
        self.api.deals.append(deal); self.bot.tick()
        self.assertEqual(self.ledger(),[]); self.assertEqual(self.bot.state['phase'],'POSITION')
        self.assertEqual(len(self.api.sends),3)
        self.restart(); self.bot.tick(); self.assertEqual(self.ledger(),[])

    def test_partial_close_manual_exit_and_conflict_fail_closed(self):
        self.position(); self.api.close_position(volume=.04)
        self.bot.tick(); self.assertEqual(self.bot.state['phase'],'SAFE_PAUSE'); self.assertFalse(self.ledger())

    def test_unknown_without_id_never_repeats_or_sends_second_side(self):
        self.api.unknown=True; self.bot.command('/start'); self.api.rates[0]['time']+=60
        self.bot.tick(); self.assertEqual(len(self.api.sends),1)
        self.restart()
        for _ in range(3): self.bot.tick()
        self.assertEqual(len(self.api.sends),1); self.assertEqual(self.ledger(),[])

    def test_changed_fact_after_restart_does_not_change_booked_money(self):
        self.position(); deal=self.api.close_position(); self.bot.tick(); self.restart()
        self.api.deals[-1]=deal._replace(profit=100.)
        self.bot.tick()
        self.assertEqual(self.bot.state['phase'],'COOLDOWN')
        self.assertEqual(D(self.bot.state['completed'][0]['realized']),D('1.8')); self.assertEqual(len(self.ledger()),1)

    def test_stale_controller_cannot_overwrite_newer_snapshot(self):
        self.position(); stale=Controller(self.store,self.broker,clock=lambda:self.api.now,monotonic=lambda:self.api.now)
        self.bot.command('/stop')
        with self.assertRaises(DurabilityError): stale.command('/status')
        self.assertTrue(self.store.load()['stop']); self.assertEqual(stale.state,self.store.load())

    def test_missing_terminal_journal_blocks_next_branch(self):
        self.position(); self.api.close_position(reason=4); self.bot.tick()
        cid=self.bot.state['cycle']['entries'][0]
        self.store.db.execute('DELETE FROM mt5_commands WHERE id=?',(cid,))
        self.restart(); count=len(self.api.sends); self.bot.tick()
        self.assertEqual(self.bot.state['phase'],'SAFE_PAUSE')
        self.assertEqual(len(self.api.sends),count); self.assertEqual(len(self.ledger()),1)

    def test_delayed_exit_recovers_without_losing_deal(self):
        self.position(); deal=self.api.close_position(); self.api.deals.remove(deal)
        self.bot.tick(); self.assertEqual(self.bot.state['phase'],'POSITION')
        self.assertEqual(self.ledger(),[])
        self.restart(); self.api.deals.append(deal); self.bot.tick()
        self.assertEqual(self.bot.state['phase'],'COOLDOWN')
        self.assertEqual(self.bot.state['seen_deals'],[str(deal.ticket)])

    def test_lost_send_result_recovers_by_exact_marker_after_restart(self):
        original=self.broker._save_row
        def crash(cid,row,event):
            if event=='send_result': raise DurabilityError('lost response persistence')
            return original(cid,row,event)
        self.bot.command('/start'); self.api.rates[0]['time']+=60
        with patch.object(self.broker,'_save_row',side_effect=crash):
            with self.assertRaises(DurabilityError): self.bot.tick()
        self.assertEqual(len(self.api.orders),1); first=self.api.orders[0].ticket
        self.restart(); self.bot.tick()
        self.assertEqual(len(self.api.orders),2)
        self.assertEqual(sum(o.ticket==first for o in self.api.orders),1)
        self.api.fill(); self.bot.tick(); self.bot.tick(); self.api.close_position(); self.bot.tick()
        self.assertEqual(self.bot.state['phase'],'COOLDOWN'); self.assertEqual(len(self.ledger()),1)

    def test_ready_cancel_survives_crash_before_dispatch(self):
        self.start_pair(); self.api.fill()
        with patch.object(self.bot,'dispatch',side_effect=DurabilityError('before dispatch')):
            with self.assertRaises(DurabilityError): self.bot.tick()
        self.restart(); self.bot.tick(); self.bot.tick()
        self.assertEqual(self.bot.state['phase'],'POSITION'); self.assertFalse(self.api.orders)
        self.assertEqual(len([r for r in self.api.sends if r['action']==8]),1)

    def test_ready_modify_survives_crash_before_dispatch(self):
        self.position()
        p=self.api.positions[0]; self.api.positions[0]=p._replace(sl=p.sl-.5)
        self.bot.state['cycle']['position']['reanchored']=False; self.bot.save('test_anchor')
        with patch.object(self.bot,'dispatch') as dispatch:
            # Allow the cancellation-only no-op, stop before the final dispatch.
            dispatch.side_effect=lambda kinds=None: (_ for _ in ()).throw(DurabilityError('crash')) if kinds is None else None
            with self.assertRaises(DurabilityError): self.bot.tick()
        self.restart(); self.bot.tick(); self.bot.tick()
        self.assertEqual(self.bot.state['phase'],'POSITION')
        self.assertTrue(self.bot.state['cycle']['position']['reanchored'])
        self.assertEqual(len([r for r in self.api.sends if r['action']==6]),1)

    def test_explicit_presend_rejection_resolves_without_repeat(self):
        self.api.check=record(retcode=10016)
        self.bot.command('/start'); self.api.rates[0]['time']+=60; self.bot.tick(); self.bot.tick()
        self.assertEqual(self.bot.state['phase'],'SAFE_PAUSE'); self.assertFalse(self.api.sends)
        self.assertTrue(all(c['status']=='REJECTED' for c in self.bot.state['commands'].values()))

    def test_manual_exit_is_not_booked(self):
        self.position(); self.api.close_position(reason=0); self.bot.tick()
        self.assertEqual(self.bot.state['phase'],'SAFE_PAUSE'); self.assertFalse(self.ledger())

    def test_partial_fill_is_not_adopted_or_refilled(self):
        self.start_pair(); self.api.fill(volume=.04); count=len(self.api.sends); self.bot.tick()
        self.assertEqual(self.bot.state['phase'],'SAFE_PAUSE'); self.assertFalse(self.ledger())
        self.assertEqual(len(self.api.sends),count)

    def test_none_history_during_presend_prevents_order_send(self):
        self.bot.command('/start'); self.api.rates[0]['time']+=60
        original=self.broker._save_row
        def unavailable(cid,row,event):
            original(cid,row,event)
            if event=='intent': self.api.discovery_none=True
        with patch.object(self.broker,'_save_row',side_effect=unavailable): self.bot.tick()
        self.assertFalse(self.api.sends); self.assertEqual(self.ledger(),[])

    def test_freshness_after_slow_fact_write_blocks_return(self):
        from tradfi.mt5_adapter import QuoteFreshnessError
        original=self.broker._transaction
        def slow(fn):
            original(fn); self.api.now+=6
        with patch.object(self.broker,'_transaction',side_effect=slow):
            with self.assertRaises(QuoteFreshnessError): self.broker.snapshot()
        self.assertFalse(self.api.sends)

    def test_missing_ledger_fact_rolls_memory_back(self):
        self.position(); deal=self.api.close_position()
        original=self.bot._save_closure
        def corrupt(event,expected,deals,snap):
            snap['ledger_facts'].pop(str(deal.ticket))
            return original(event,expected,deals,snap)
        with patch.object(self.bot,'_save_closure',side_effect=corrupt):
            with self.assertRaises(KeyError): self.bot.tick()
        self.assertEqual(self.bot.state,self.store.load()); self.assertFalse(self.ledger())
        self.assertEqual(self.bot.state['phase'],'POSITION')

    def test_atomic_exact_replay_and_conflicting_ticket(self):
        self.position(); self.api.close_position(reason=4)
        original=self.store.commit_deals; captured=[]
        def capture(expected,data,records,event):
            captured.append(deepcopy((expected,data,records,event)))
            return original(expected,data,records,event)
        with patch.object(self.store,'commit_deals',side_effect=capture): self.bot.tick()
        expected,data,records,event=captured[0]
        self.assertFalse(original(expected,data,records,event))
        rows=self.store.db.execute('SELECT COUNT(*) FROM audit WHERE event=?',(event,)).fetchone()[0]
        self.assertEqual(rows,1)
        records[0]['raw']['profit']=999
        with self.assertRaises(DurabilityError): original(expected,data,records,event)
        self.assertEqual(self.store.load(),data); self.assertEqual(len(self.ledger()),1)

    def test_malformed_ledger_batch_is_atomic(self):
        self.position(); self.api.close_position(reason=4)
        original=self.store.commit_deals
        def malformed(expected,data,records,event):
            records[0]['raw']=dict(records[0]['raw'],ticket=999999)
            return original(expected,data,records,event)
        with patch.object(self.store,'commit_deals',side_effect=malformed):
            with self.assertRaises(DurabilityError): self.bot.tick()
        self.assertFalse(self.ledger()); self.assertEqual(self.bot.state,self.store.load())
        self.assertEqual(self.bot.state['phase'],'POSITION')

    @unittest.skipUnless('fork' in multiprocessing.get_all_start_methods(),'POSIX crash fixture')
    def test_process_death_before_and_after_commit_preserves_tp_completion(self):
        for stage in ('closure_before_commit','closure_after_commit'):
            with self.subTest(stage=stage):
                if self.bot.state['phase']!='IDLE':
                    self.broker.close(); self.store.close(); del self.store
                    self.path=Path(self.tmp.name)/(stage+'-process.db'); self.api=TradingAPI(); self.restart()
                self.position(); deal=self.api.close_position()
                self.broker.close(); self.store.close(); del self.store
                def child():
                    self.restart()
                    self.store.fault=lambda event: os._exit(73) if event==stage else None
                    self.bot.tick()
                    os._exit(74)
                worker=multiprocessing.get_context('fork').Process(target=child)
                worker.start(); worker.join(10)
                if worker.is_alive(): worker.terminate(); worker.join(); self.fail('crash child hung')
                self.assertEqual(worker.exitcode,73)
                self.restart()
                self.assertEqual(len(self.ledger()),int(stage=='closure_after_commit'))
                self.bot.tick()
                self.assertEqual(self.bot.state['phase'],'COOLDOWN')
                self.assertEqual(self.bot.state['seen_deals'],[str(deal.ticket)])
                self.assertEqual(D(self.bot.state['completed'][0]['realized']),D('1.8'))
                self.assertEqual(len(self.ledger()),1)

    def test_test_profile_native_and_unactivated_production_stay_blocked(self):
        for api,profile in ((None,'injected_utc_test_v1'),(ModuleType('MetaTrader5'),'injected_utc_test_v1'),(self.api,'bybit_live6_time_v1')):
            with self.assertRaises(TimeProfileError): MT5EvidenceBroker(dict(self.config,time_profile=profile),api=api)

if __name__=='__main__': unittest.main()
