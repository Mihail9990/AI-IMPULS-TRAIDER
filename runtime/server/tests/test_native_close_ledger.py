import tempfile
import unittest
from copy import deepcopy
from recovery.native_close_ledger import NativeCloseLedger
from recovery.native_entry_observer import NativeEntryObserver
from recovery.native_executor_session import NativeExecutorSession
from recovery.native_recovery_control import NativeRecoveryControl
from recovery.native_preflight import TerminalBinding
from test_native_entry_reader import EntryFake
from test_native_recovery_control import RANGES


class CloseLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.a=EntryFake();self.a.DEAL_REASON_SL=4;self.a.DEAL_REASON_CLIENT=0
        self.a.DEAL_REASON_MOBILE=1;self.a.DEAL_REASON_WEB=2
        self.binding=TerminalBinding(1,'demo','XAUUSD.s',frozenset({123}))
        self.path=self.tmp.name+'/s.db'
        self.s=NativeExecutorSession(self.a,self.binding,database=self.path)
        self.s.__enter__();self.addCleanup(self.s.close)
        self.c=NativeRecoveryControl(self.s,magic=123);self.c.stage_initial_ranges(RANGES)
        self.c.submit_initial('1');self.a.fill()
        self.observer=NativeEntryObserver(self.c);self.ledger=NativeCloseLedger(self.c)
    def exit(self,volume=.01,reason=4):
        d=deepcopy(self.a.entry_deals[0]);d.ticket=102;d.order=52;d.entry=1;d.type=1
        d.magic=0;d.volume=volume;d.reason=reason;d.profit=-.25;d.commission=-.02;d.price=3999.3
        self.a.exit_deals=[d]
        if volume==.01:self.a.positions=[]
        else:self.a.positions[0].volume=.01-volume
        self.observer.observe('1')
    def test_sl_close_stays_pending_without_inference_or_double_account(self):
        self.exit();row=self.ledger.record(51)
        self.assertTrue(row['fully_closed']);self.assertIsNone(row['classification'])
        self.assertEqual(row['realized_net'],'-0.28')
        request=self.ledger.evidence_request(51,102)
        self.assertEqual(request.position,88);self.assertEqual(request.order,52)
        self.assertEqual(request.exit_side,'SELL')
        self.ledger.record(51)
        self.assertEqual(self.ledger.totals()['1']['closed_positions'],1)
        self.assertEqual(self.ledger.totals()['1']['full_sl_count'],0)
        self.assertEqual(self.ledger.totals()['1']['classification_pending_count'],1)
        self.assertEqual(len(self.a.sent),1)
    def test_manual_and_partial_accounting(self):
        self.exit(.004,reason=0);row=self.ledger.record(51)
        self.assertFalse(row['fully_closed']);self.assertEqual(row['status'],'open_or_partial')
        second=deepcopy(self.a.exit_deals[0]);second.ticket=103;second.order=53;second.volume=.006
        second.profit=.10;second.commission=-.01;self.a.exit_deals.append(second);self.a.positions=[]
        self.observer.observe('1');row=self.ledger.record(51)
        self.assertEqual(row['classification'],'manual');self.assertEqual(row['realized_net'],'-0.19')
        self.assertEqual(self.ledger.totals()['1']['full_sl_count'],0)
    def test_unknown_reason_not_manual(self):
        self.exit(reason=99);row=self.ledger.record(51)
        self.assertIsNone(row['classification']);self.assertEqual(row['status'],'classification_pending')
        with self.assertRaises(ValueError):self.ledger.evidence_request(51,102)
    def test_restart_keeps_request_and_totals(self):
        self.exit();self.ledger.record(51)
        expected=self.ledger.evidence_request(51,102);totals=self.ledger.totals()
        self.s.close()
        with NativeExecutorSession(self.a,self.binding,database=self.path) as s:
            c=NativeRecoveryControl(s,magic=123);ledger=NativeCloseLedger(c)
            self.assertEqual(ledger.evidence_request(51,102),expected)
            self.assertEqual(ledger.totals(),totals)
            ledger.record(51);self.assertEqual(ledger.totals(),totals)
    def test_saved_accounted_deal_mutation_rejected(self):
        self.exit();self.ledger.record(51)
        data=self.c._read();data['entry_observations']['51']['evidence']['exit_deals'][0]['profit']='-100'
        self.c._save(data)
        with self.assertRaises(ValueError):self.ledger.record(51)
        self.assertEqual(self.ledger.totals()['1']['realized_net'],'-0.28')
    def test_no_authoritative_observation_no_accounting(self):
        with self.assertRaises(ValueError):self.ledger.record(51)
        self.assertEqual(self.ledger.totals(),{})

    def mailbox(self, level):
        from pathlib import Path
        root=Path(self.tmp.name)/'terminal';directory=root/'MQL5'/'Files'/'RECOVERY'
        directory.mkdir(parents=True,exist_ok=True)
        original=self.a.terminal_info
        def terminal():
            info=original();info.data_path=str(root);return info
        self.a.terminal_info=terminal
        b=self.ledger.evidence_request(51,102)
        payload='\t'.join(['RECOVERY_DEAL_SL_V1',b.request_id,str(b.account),b.server,str(b.deal),
            str(b.order),str(b.position),b.symbol,b.exit_side,str(b.time_msc),format(b.volume,'.16f'),
            format(__import__('decimal').Decimal(level),'.16f'),'END'])+'\n'
        (directory/(b.request_id+'.response')).write_text(payload)
        return directory

    def test_initial_sl_resolve_and_record_preserves_once(self):
        self.exit();self.ledger.record(51);self.mailbox('3999.30')
        row=self.ledger.resolve_sl(51)
        self.assertEqual(row['classification'],'initial_sl')
        self.assertEqual(self.ledger.totals()['1']['full_sl_count'],1)
        self.ledger.record(51);self.ledger.resolve_sl(51)
        self.assertEqual(self.ledger.totals()['1']['full_sl_count'],1)
        self.assertEqual(len(self.a.sent),1)

    def test_support_sl_unique_level_and_ambiguous_rejected(self):
        self.exit();self.ledger.record(51)
        data=self.c._read();data['confirmed_protection']={'88':{'stage':1,'sl':'3999.85',
            'position_identifier':88,'position_ticket':77,'source':'observed_existing_stop'}}
        self.c._save(data);self.mailbox('3999.85')
        row=self.ledger.resolve_sl(51)
        self.assertEqual(row['classification'],'support_sl');self.assertEqual(row['stop_level'],'3999.85')
        self.assertEqual(self.ledger.totals()['1']['full_sl_count'],0)
        # Changed bytes cannot rewrite an already accepted evidence record.
        self.mailbox('3999.30')
        with self.assertRaises(ValueError):self.ledger.resolve_sl(51)
        self.assertEqual(self.ledger.record(51)['classification'],'support_sl')

    def test_unknown_and_equal_initial_support_stop_do_not_increment(self):
        self.exit();self.ledger.record(51);self.mailbox('3998')
        with self.assertRaises(ValueError):self.ledger.resolve_sl(51)
        data=self.c._read();data['confirmed_protection']={'88':{'stage':1,'sl':'3999.30',
            'position_identifier':88,'position_ticket':77,'source':'observed_existing_stop'}}
        self.c._save(data);self.mailbox('3999.30')
        with self.assertRaises(ValueError):self.ledger.resolve_sl(51)
        self.assertEqual(self.ledger.totals()['1']['full_sl_count'],0)

    def test_reverse_mapping_supported_duplicate_mapping_rejected(self):
        self.exit()
        data=self.c._read();item=data['orders'].pop();data['reverse_commands']={item['key']:item};self.c._save(data)
        self.assertTrue(self.ledger.record(51)['fully_closed'])
        data=self.c._read();data['reentry_commands']={item['key']:deepcopy(item)};self.c._save(data)
        with self.assertRaises(ValueError):self.ledger.record(51)
