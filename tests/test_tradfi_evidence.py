"""Per-obligation evidence regressions with no terminal/network/trade calls."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest

from test_tradfi_mt5 import FakeMT5, record
from tradfi.evidence import collect, EvidenceConflict
from tradfi.mt5_adapter import MT5Broker
from tradfi.storage import Store
from tradfi.time_profile import HistoryTimeUnverified

CID='a'*24
ACCOUNT='111111111@Bybit-Live-6'
MAGIC=25101102


class API(FakeMT5):
    def __init__(self):
        super().__init__()
        self.calls=[]; self.order_none=False; self.position_none=False
        self.discovery_none=False; self.omit_discovery=False
    def history_orders_get(self,*args,**kwargs):
        self.calls.append(('orders',args,kwargs))
        if 'ticket' in kwargs:
            if self.order_none: return None
            return [r for r in self.history if r.ticket==kwargs['ticket']]
        if self.discovery_none: return None
        if self.omit_discovery: return []
        lo,hi=(v.timestamp() for v in args)
        return [r for r in self.history if lo<=r.time_done_msc/1000<=hi]
    def history_deals_get(self,*args,**kwargs):
        self.calls.append(('deals',args,kwargs))
        if 'ticket' in kwargs: raise AssertionError('deals(ticket) is not supported evidence')
        if 'position' in kwargs:
            if self.position_none: return None
            return [r for r in self.deals if r.position_id==kwargs['position']]
        if self.discovery_none: return None
        if self.omit_discovery: return []
        lo,hi=(v.timestamp() for v in args)
        return [r for r in self.deals if lo<=r.time_msc/1000<=hi]


class EvidenceTest(unittest.TestCase):
    def setUp(self):
        self.api=API()
        self.api.now=datetime(2026,10,12,12,tzinfo=timezone.utc).timestamp()
        self.api.tick=self.api.tick._replace(time_msc=self.api.now*1000)
        self.cmd=dict(id=CID,kind='STOP',side='BUY',size='.01',price='4003',sl='4001',tp='4004')
        self.rows={CID:dict(command=deepcopy(self.cmd),account=ACCOUNT,send_started=True,
                           raw_result=dict(order=101),result=dict(status='UNKNOWN'))}
        self.state=dict(schema=1,backend='mt5_direct_v1',account=ACCOUNT,currency='UST',
                        phase='WAIT_FILL',cycle=None,commands={CID:dict(self.cmd,status='UNKNOWN')},
                        seen_deals=[],completed=[])
        raw=(self.api.now+10800)*1000
        self.order=record(ticket=101,symbol='XAUUSD.s',magic=MAGIC,comment=CID,type=4,state=4,
                          volume_initial=.01,volume_current=0.,price_open=4003.,sl=4001.,tp=4004.,
                          position_id=501,time_setup_msc=raw-1000,time_done_msc=raw)
        self.entry=record(ticket=301,order=101,position_id=501,symbol='XAUUSD.s',magic=MAGIC,
                          entry=0,type=0,volume=.01,price=4003.,profit=0.,reason=3,time_msc=raw)
        self.exit=self.entry._replace(ticket=302,order=102,entry=1,type=1,price=4004.,profit=.2,
                                       reason=5,time_msc=raw+1)
        self.position=record(ticket=999,identifier=501,symbol='XAUUSD.s',magic=MAGIC,type=0,
                             volume=.01,price_open=4003.,sl=4001.,tp=4004.)
        self.api.history=[self.order]; self.api.deals=[self.entry]; self.api.positions=[self.position]

    def run_evidence(self,cached=None):
        return collect(self.api,rows=self.rows,state=self.state,account=ACCOUNT,
                       symbol='XAUUSD.s',magic=MAGIC,now=self.api.now,
                       check_context=lambda:None,cached=cached)

    def status(self,result): return result['obligations'][CID]['status']
    def close(self): self.api.deals.append(self.exit); self.api.positions=[]

    def test_future_three_hour_raw_event_found_and_not_execution_ready(self):
        r=self.run_evidence()
        self.assertEqual(self.status(r),'FILLED_OPEN_EVIDENCE')
        self.assertFalse(r['history_complete']); self.assertFalse(r['execution_ready'])
        self.assertEqual(r['deal_times']['301']['raw_time_msc'],self.entry.time_msc)
        self.assertEqual(r['deal_times']['301']['utc_candidate_s'],self.api.now)
        self.assertEqual(r['deal_times']['301']['utc_status'],'UNVERIFIED')
        ranges=[c for c in self.api.calls if c[1]]
        self.assertTrue(all(c[1][0].timestamp()==0 for c in ranges))
        self.assertTrue(all(c[1][1].timestamp()==self.api.now+10860 for c in ranges))

    def test_winter_two_hour_event_also_found(self):
        self.api.now=datetime(2026,1,12,12,tzinfo=timezone.utc).timestamp()
        raw=(self.api.now+7200)*1000
        self.api.history=[self.order._replace(time_setup_msc=raw-1000,time_done_msc=raw)]
        self.api.deals=[self.entry._replace(time_msc=raw)]
        r=self.run_evidence()
        self.assertEqual(self.status(r),'FILLED_OPEN_EVIDENCE')
        self.assertEqual(r['deal_times']['301']['utc_candidate_s'],self.api.now)

    def test_lost_response_exact_marker_recovers_but_absence_never_proves_rejection(self):
        self.rows[CID]['raw_result']=None
        self.assertEqual(self.status(self.run_evidence()),'FILLED_OPEN_EVIDENCE')
        self.api.history=[]; self.api.deals=[]; self.api.positions=[]
        r=self.run_evidence()
        self.assertEqual(self.status(r),'UNKNOWN')
        self.assertFalse(r['obligations'][CID]['repeat_allowed'])
        self.assertTrue(r['evidence_blocked'])

    def test_magic_without_marker_or_returned_ticket_cannot_claim_position(self):
        self.rows[CID]['raw_result']=None
        self.api.history=[self.order._replace(comment='manual')]
        r=self.run_evidence()
        self.assertEqual(self.status(r),'UNKNOWN'); self.assertEqual(r['foreign_positions'],['501'])

    def test_unknown_matching_marker_multiple_orders_is_conflict(self):
        self.rows[CID]['raw_result']=None
        self.api.history.append(self.order._replace(ticket=103))
        with self.assertRaises(EvidenceConflict): self.run_evidence()

    def test_known_order_ticket_does_not_override_intent_mismatch(self):
        for change in (dict(magic=99),dict(symbol='OTHER'),dict(volume_initial=.02),dict(type=5),dict(price_open=4005)):
            self.api.history=[self.order._replace(**change)]
            with self.subTest(change=change),self.assertRaises(EvidenceConflict): self.run_evidence()

    def test_partial_fill_and_partial_close_stay_blocked(self):
        self.api.deals=[self.entry._replace(volume=.005)]
        self.api.positions=[self.position._replace(volume=.005)]
        self.assertEqual(self.status(self.run_evidence()),'PARTIAL_FILL')
        self.api.deals=[self.entry,self.exit._replace(volume=.005)]
        self.api.positions=[self.position._replace(volume=.005)]
        r=self.run_evidence(); self.assertEqual(self.status(r),'PARTIAL_CLOSE')
        self.assertEqual(r['observed_closed_profit'],'0')

    def test_delayed_history_known_ticket_and_position_queries_recover(self):
        self.api.omit_discovery=True
        self.assertEqual(self.status(self.run_evidence()),'FILLED_OPEN_EVIDENCE')
        self.api.deals=[]
        self.assertEqual(self.status(self.run_evidence()),'AWAIT_FILL_EVIDENCE')

    def test_none_ticket_or_position_queries_never_ready_or_book_profit(self):
        self.close()
        for key in ('order_none','position_none','discovery_none'):
            setattr(self.api,key,True)
            r=self.run_evidence()
            self.assertEqual(self.status(r),'API_EVIDENCE_UNAVAILABLE')
            self.assertEqual(r['observed_closed_profit'],'0')
            self.assertTrue(r['evidence_blocked'])
            setattr(self.api,key,False)

    def test_duplicate_deals_do_not_double_profit_or_mutate_inputs(self):
        self.close(); self.api.deals.append(self.exit)
        before=deepcopy(self.state)
        r=self.run_evidence()
        again=self.run_evidence(cached=r['raw_facts'])
        self.assertEqual(self.status(r),'CLOSED_EVIDENCE')
        self.assertEqual(r['observed_closed_profit'],'0.2')
        self.assertEqual(again['closed_profit_by_deal'],{'302':'0.2'})
        self.assertEqual(self.state,before)

    def test_conflicting_duplicate_and_overfill_are_rejected(self):
        self.api.deals.append(self.entry._replace(price=4005))
        with self.assertRaises(EvidenceConflict): self.run_evidence()
        self.api.deals=[self.entry._replace(volume=.02)]
        with self.assertRaises(EvidenceConflict): self.run_evidence()

    def test_foreign_position_and_manual_exit_stay_blocked(self):
        self.api.positions.append(self.position._replace(identifier=600,ticket=900,magic=99))
        r=self.run_evidence(); self.assertTrue(r['evidence_blocked']); self.assertEqual(r['foreign_positions'],['600'])
        self.close(); self.api.deals[-1]=self.exit._replace(reason=0,magic=0)
        r=self.run_evidence(); self.assertEqual(self.status(r),'EXTERNAL_EXIT')
        self.assertEqual(r['observed_closed_profit'],'0')

    def test_close_by_and_unrelated_entry_on_position_are_not_owned_exit(self):
        self.close(); self.api.deals[-1]=self.exit._replace(entry=3)
        self.assertEqual(self.status(self.run_evidence()),'UNSUPPORTED_EXIT')
        self.api.deals=[self.entry,self.entry._replace(ticket=303,order=103)]
        with self.assertRaises(EvidenceConflict): self.run_evidence()

    def test_dst_ambiguity_retains_raw_and_blocks_closure(self):
        self.close()
        fold=datetime(2026,11,1,4,30,tzinfo=timezone.utc).timestamp()*1000
        self.api.deals=[self.entry._replace(time_msc=fold),self.exit._replace(time_msc=fold+1)]
        # position lookup retrieves facts outside discovery; do not discard them.
        r=self.run_evidence()
        self.assertEqual(self.status(r),'AMBIGUOUS_HISTORY_TIME')
        self.assertIsNone(r['deal_times']['301']['utc_candidate_s'])
        self.assertEqual(r['deal_times']['301']['raw_time_msc'],fold)
        self.assertEqual(r['observed_closed_profit'],'0')

    def test_protection_readback_uses_live_ticket_without_mutation(self):
        cid='b'*24; cmd=dict(id=cid,kind='MODIFY',position_id='501',sl='4001',tp='4004')
        self.rows[cid]=dict(command=cmd,account=ACCOUNT,send_started=True)
        self.state['commands'][cid]=dict(cmd,status='UNKNOWN')
        r=self.run_evidence()
        self.assertEqual(r['obligations'][cid]['status'],'PROTECTION_READBACK')
        self.assertEqual(r['obligations'][cid]['current_position_ticket'],'999')
        self.assertFalse(self.api.sends); self.assertFalse(self.api.checks)

    def test_incompatible_durable_intents_block_before_io(self):
        for changed in ('account','backend','currency'):
            old=self.state[changed]; self.state[changed]='wrong'
            with self.assertRaises(EvidenceConflict): self.run_evidence()
            self.state[changed]=old
        self.assertFalse(self.api.calls)
        self.state['commands'][CID]['size']='.02'
        with self.assertRaises(EvidenceConflict): self.run_evidence()
        self.assertFalse(self.api.calls)

    def test_unknown_market_without_returned_ticket_stays_unowned(self):
        self.rows[CID]['raw_result']=None
        self.rows[CID]['command']['kind']='MARKET'; self.state['commands'][CID]['kind']='MARKET'
        self.api.history=[self.order._replace(type=0)]
        r=self.run_evidence()
        self.assertEqual(self.status(r),'UNKNOWN'); self.assertTrue(r['evidence_blocked'])

    def test_missing_durable_status_and_nonboolean_send_started_block(self):
        self.state['commands'][CID].pop('status')
        with self.assertRaises(EvidenceConflict): self.run_evidence()
        self.state['commands'][CID]['status']='UNKNOWN'; self.rows[CID]['send_started']=1
        with self.assertRaises(EvidenceConflict): self.run_evidence()
        self.assertFalse(self.api.calls)

    def test_terminal_filled_order_with_remainder_is_conflict(self):
        self.api.history=[self.order._replace(volume_current=.005)]
        with self.assertRaises(EvidenceConflict): self.run_evidence()

    def test_existing_journal_audit_is_readonly_and_never_creates_missing_file(self):
        from tradfi.evidence import load_journal
        import sqlite3
        binding=dict(backend='mt5_direct_v1',time_profile='bybit_live6_time_v1',account=ACCOUNT,
                     symbol='XAUUSD.s',magic=MAGIC,terminal='test',currency='UST')
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'journal.sqlite3'
            with self.assertRaises(EvidenceConflict): load_journal(path,binding)
            self.assertFalse(path.exists())
            with sqlite3.connect(path) as db:
                db.executescript('CREATE TABLE mt5_binding(id PRIMARY KEY,payload); CREATE TABLE snapshot(id PRIMARY KEY,payload); CREATE TABLE mt5_commands(id PRIMARY KEY,payload); CREATE TABLE mt5_facts(kind,ticket,payload);')
                db.execute('INSERT INTO mt5_binding VALUES(1,?)',(json.dumps(binding),))
                db.execute('INSERT INTO snapshot VALUES(1,?)',(json.dumps(self.state),))
                db.execute('INSERT INTO mt5_commands VALUES(?,?)',(CID,json.dumps(self.rows[CID])))
            before=path.read_bytes()
            rows,state,facts=load_journal(path,binding)
            self.assertEqual(rows,self.rows); self.assertEqual(state,self.state)
            self.assertEqual(facts,{'order':{},'deal':{}})
            self.assertEqual(path.read_bytes(),before)
            with self.assertRaises(EvidenceConflict): load_journal(path,dict(binding,time_profile='old'))
            self.assertEqual(path.read_bytes(),before)

    def test_snapshot_unknown_missing_journal_row_is_not_ignored(self):
        self.close()
        cid='c'*24
        self.state['commands'][cid]=dict(self.cmd,id=cid,status='UNKNOWN')
        r=self.run_evidence()
        self.assertEqual(r['obligations'][cid]['status'],'MISSING_DURABLE_JOURNAL')
        self.assertTrue(r['evidence_blocked'])
        self.assertFalse(r['obligations'][cid]['repeat_allowed'])

    def test_cached_payload_ticket_mismatch_does_not_inflate_volume(self):
        r=self.run_evidence(); facts=r['raw_facts']
        facts['deal']['9999']=deepcopy(facts['deal']['301'])
        with self.assertRaises(EvidenceConflict): self.run_evidence(cached=facts)

    def test_wal_source_is_refused_without_creating_sidecars(self):
        from tradfi.evidence import load_journal
        import sqlite3
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'wal.sqlite3'
            db=sqlite3.connect(path)
            db.execute('PRAGMA journal_mode=WAL'); db.execute('CREATE TABLE x(id)')
            db.commit(); db.close()
            before={p.name:p.read_bytes() for p in Path(tmp).iterdir()}
            with self.assertRaises(EvidenceConflict): load_journal(path,{})
            self.assertEqual(before,{p.name:p.read_bytes() for p in Path(tmp).iterdir()})

    def test_runtime_evidence_summary_forces_readonly_and_hides_private_facts(self):
        from tradfi.runtime import _run_mt5
        from unittest.mock import patch
        from contextlib import redirect_stdout
        import io
        self.close(); audit=self.run_evidence()
        with patch('tradfi.mt5_adapter.MT5Broker') as factory, patch('tradfi.runtime.Store') as store:
            factory.return_value.preflight.return_value={'mode':'READ_ONLY'}
            factory.return_value.reconciliation_evidence.return_value=audit
            output=io.StringIO()
            with redirect_stdout(output): _run_mt5({'enable_trading':True},True,evidence=True)
            self.assertFalse(factory.call_args.args[0]['enable_trading'])
            store.assert_not_called(); factory.return_value.close.assert_called_once()
            summary=json.loads(output.getvalue())['reconciliation']
            self.assertEqual(summary['obligation_status_counts'],{'CLOSED_EVIDENCE':1})
            self.assertFalse(summary['execution_ready']); self.assertFalse(summary['history_complete'])
            for private in (CID,ACCOUNT,'raw_facts','closed_profit_by_deal','observed_closed_profit'):
                self.assertNotIn(private,output.getvalue())

    def test_cli_evidence_flag_cannot_start_execution_or_mix_mode(self):
        from tradfi.runtime import main
        from unittest.mock import patch
        from contextlib import redirect_stderr
        import io
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'inspect.json'
            path.write_text(json.dumps(dict(backend='mt5_direct_v1',enable_trading=True)))
            with patch('tradfi.runtime._run_mt5') as runner:
                main(['--mode','mt5','--inspect-evidence','--config',str(path)])
                self.assertTrue(runner.call_args.args[1])
                self.assertTrue(runner.call_args.kwargs['evidence'])
            with patch('tradfi.runtime._run_mt5') as runner, redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit): main(['--mode','simulate','--inspect-evidence'])
                with self.assertRaises(SystemExit): main(['--mode','mt5','--journal-snapshot',str(path)])
                runner.assert_not_called()

    def test_readonly_adapter_integration_restart_keeps_ledger_and_journal(self):
        self.close()
        config=dict(backend='mt5_direct_v1',time_profile='injected_utc_test_v1',
                    terminal_path='C:/Program Files/MetaTrader 5/terminal64.exe',expected_server='Bybit-Live-6',
                    expected_login=111111111,symbol='XAUUSD.s',enable_trading=False,magic=MAGIC)
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'state.sqlite3'
            for attempt in range(2):
                store=Store(path)
                if attempt==0: store.save(self.state,'seed')
                broker=MT5Broker(config,store=store,api=self.api,clock=lambda:self.api.now)
                broker.connect()
                if attempt==0: broker._save_row(CID,self.rows[CID],'durable_intent')
                before=list(store.db.iterdump())
                result=broker.reconciliation_evidence()
                self.assertEqual(result['closed_profit_by_deal'],{'302':'0.2'})
                self.assertEqual(before,list(store.db.iterdump()))
                self.assertEqual(store.load()['seen_deals'],[])
                broker.close(); store.close()
        self.assertFalse(self.api.sends)


if __name__=='__main__': unittest.main()
