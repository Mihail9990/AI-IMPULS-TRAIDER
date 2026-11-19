import tempfile
import unittest
from types import SimpleNamespace as N
from pathlib import Path
from decimal import Decimal
from recovery.native_recovery_control import NativeRecoveryControl
from recovery.native_entry_protection import NativeEntryProtection
from recovery.native_initial_reverse import NativeInitialReverse
from recovery.native_support_reentry import NativeSupportReentry
from recovery.native_reentry_retirement import NativeReentryRetirement
from recovery.native_entry_observer import NativeEntryObserver
from recovery.native_close_ledger import NativeCloseLedger
import test_native_recovery_control as controls
from test_native_recovery_control import RANGES
from test_native_entry_observer import EntryFake


class CycleFake(EntryFake):
    DEAL_REASON_SL=4;DEAL_REASON_CLIENT=0;DEAL_REASON_MOBILE=1;DEAL_REASON_WEB=2
    def __init__(self,root):
        super().__init__();self.next_order=51;self.next_deal=601;self.root=root
    def terminal_info(self):
        info=super().terminal_info();info.data_path=self.root;return info
    def order_send(self,r):
        self.sent.append(r)
        if r['action']==self.TRADE_ACTION_PENDING:
            ticket=self.next_order;self.next_order+=1
            self.orders.append(N(ticket=ticket,magic=r['magic'],symbol=r['symbol'],type=r['type'],
                price_open=r['price'],sl=r['sl'],tp=r['tp'],volume_current=r['volume'],volume_initial=r['volume'],state=1,position_id=0))
            return N(retcode=self.TRADE_RETCODE_PLACED,order=ticket,deal=0)
        if r['action']==self.TRADE_ACTION_SLTP:
            p=next(p for p in self.positions if p.ticket==r['position']);p.sl=r['sl'];p.tp=r['tp']
            return N(retcode=self.TRADE_RETCODE_DONE,order=0,deal=0)
        if r['action']==self.TRADE_ACTION_REMOVE:
            o=next(o for o in self.orders if o.ticket==r['order']);self.orders.remove(o)
            o.state=self.ORDER_STATE_CANCELED;self.history.append(o)
            return N(retcode=self.TRADE_RETCODE_DONE,order=o.ticket,deal=0)
        raise AssertionError('Unexpected trading action')
    def fill_order(self,ticket,pid,price=None):
        o=next(o for o in self.orders if o.ticket==ticket);self.orders.remove(o)
        price=o.price_open if price is None else price
        volume=o.volume_current;o.volume_current=0;o.state=self.ORDER_STATE_FILLED;o.position_id=pid
        self.history.append(o);side=0 if o.type==self.ORDER_TYPE_BUY_STOP else 1
        self.deals.append(N(ticket=self.next_deal,order=ticket,position_id=pid,magic=o.magic,symbol=o.symbol,
            entry=0,type=side,volume=volume,price=price,time_msc=self.next_deal,reason=3,
            profit=0,commission=0,swap=0,fee=0));self.next_deal+=1
        self.positions.append(N(ticket=pid+100,identifier=pid,magic=o.magic,symbol=o.symbol,type=side,
            volume=volume,price_open=price,sl=o.sl,tp=o.tp))
    def close(self,pid,reason=4):
        p=next(p for p in self.positions if p.identifier==pid);self.positions.remove(p)
        self.deals.append(N(ticket=self.next_deal,order=1000+self.next_deal,position_id=pid,magic=0,symbol=p.symbol,
            entry=1,type=1-p.type,volume=p.volume,price=p.sl,time_msc=self.next_deal,reason=reason,
            profit=-.25,commission=-.01,swap=0,fee=0));self.next_deal+=1


def ea_response(ledger,order,deal,root,level):
    binding=ledger.evidence_request(order,deal)
    directory=Path(root)/'MQL5'/'Files'/'RECOVERY';directory.mkdir(parents=True,exist_ok=True)
    fields=['RECOVERY_DEAL_SL_V1',binding.request_id,str(binding.account),binding.server,str(binding.deal),
        str(binding.order),str(binding.position),binding.symbol,binding.exit_side,str(binding.time_msc),
        format(binding.volume,'.16f'),format(Decimal(level),'.16f'),'END']
    (directory/(binding.request_id+'.response')).write_text('\t'.join(fields)+'\n')


class SupportCycleTests(unittest.TestCase):
    session=controls.ControlTests.session
    def test_support_reentry_keeps_then_retires_old_opposite_before_replacement(self):
        with tempfile.TemporaryDirectory() as d:
            a=CycleFake(d)
            with self.session(a,d+'/s.db') as s:
                c=NativeRecoveryControl(s,magic=123);c.stage_initial_ranges(RANGES);c.submit_initial('1')
                a.fill_order(51,88);a.ask=4000.21
                p=NativeEntryProtection(c);p.initial('1')
                reverse=NativeInitialReverse(c);old=reverse.arm('1')
                self.assertEqual(old['broker_order'],52)
                a.ask=4000.46;p.support('1');a.close(88);a.ask=3999.8
                NativeEntryObserver(c).observe('1');ledger=NativeCloseLedger(c);ledger.record(51)
                # No reentry before authoritative close classification.
                reentries=NativeSupportReentry(c)
                with self.assertRaises(RuntimeError):reentries.arm('1')
                ea_response(ledger,51,602,d,'3999.75');ledger.resolve_sl(51)
                new=reentries.arm('1');self.assertEqual(new['broker_order'],53)
                self.assertEqual(float(new['intent']['price']),4000.45)
                self.assertEqual({o.ticket for o in a.orders},{52,53})
                retirement=NativeReentryRetirement(c)
                with self.assertRaises(RuntimeError):retirement.retire('1',order_key=new['key'])
                self.assertEqual({o.ticket for o in a.orders},{52,53})
                a.fill_order(53,89);a.ask=4000.5
                p.initial('1',order_key=new['key'])
                # Old reverse must be cancelled, not silently ignored.
                with self.assertRaises(RuntimeError):reverse.arm('1',order_key=new['key'])
                retired=retirement.retire('1',order_key=new['key'])
                self.assertEqual(retired['phase'],'verified');self.assertEqual(a.orders,[])
                replacement=reverse.arm('1',order_key=new['key'])
                self.assertEqual(float(replacement['intent']['price']),3999.75)
                self.assertEqual(replacement['broker_order'],54)
                self.assertEqual(ledger.totals()['1']['full_sl_count'],0)
                count=len(a.sent);retirement.reconcile(retired['key']);reverse.arm('1',order_key=new['key'])
                self.assertEqual(len(a.sent),count)
    def test_full_sl_reverse_chain_uses_close_count_and_stops_at_24(self):
        from recovery.volume_schedule import volume_for_slot
        with tempfile.TemporaryDirectory() as d:
            a=CycleFake(d)
            with self.session(a,d+'/s.db') as s:
                c=NativeRecoveryControl(s,magic=123);c.stage_initial_ranges(RANGES);c.submit_initial('1')
                observer=NativeEntryObserver(c);ledger=NativeCloseLedger(c)
                protection=NativeEntryProtection(c);reverse=NativeInitialReverse(c)
                current=c.state()['data']['orders'][0];pid=88
                a.fill_order(current['broker_order'],pid)
                for slot in range(1,25):
                    key=current['key'];side=current['intent']['side']
                    p=a.positions[0]
                    self.assertEqual(Decimal(str(p.volume)),volume_for_slot(slot))
                    a.ask=p.price_open+.21 if side=='BUY' else p.price_open-.20
                    protection.initial('1',order_key=key)
                    if slot<24:
                        following=reverse.arm('1',order_key=key if slot>1 else None)
                        self.assertEqual(Decimal(following['intent']['volume']),volume_for_slot(slot+1))
                    else:
                        with self.assertRaises(RuntimeError):reverse.arm('1',order_key=key)
                        self.assertEqual(a.orders,[])
                    stop=str(p.sl);a.close(pid)
                    exit_ticket=a.deals[-1].ticket
                    if slot<24:
                        pid+=1;a.fill_order(following['broker_order'],pid)
                    observer.observe('1',order_key=key);ledger.record(current['broker_order'])
                    self.assertEqual(ledger.totals()['1']['full_sl_count'],slot-1)
                    ea_response(ledger,current['broker_order'],exit_ticket,d,stop)
                    ledger.resolve_sl(current['broker_order'])
                    self.assertEqual(ledger.totals()['1']['full_sl_count'],slot)
                    if slot<24:current=following
                self.assertEqual(len(a.positions),0);self.assertEqual(a.orders,[])
                self.assertEqual(len(a.sent),24) # no market entries, duplicate or 25th trigger
    def test_opposite_wins_before_or_after_reentry_arming(self):
        for armed in (False,True):
            with self.subTest(armed=armed), tempfile.TemporaryDirectory() as d:
                a=CycleFake(d)
                with self.session(a,d+'/s.db') as s:
                    c=NativeRecoveryControl(s,magic=123);c.stage_initial_ranges(RANGES);c.submit_initial('1')
                    a.fill_order(51,88);a.ask=4000.21
                    p=NativeEntryProtection(c);p.initial('1')
                    reverse=NativeInitialReverse(c);old=reverse.arm('1')
                    a.ask=4000.46;p.support('1');a.close(88);a.ask=3999.8
                    NativeEntryObserver(c).observe('1');ledger=NativeCloseLedger(c);ledger.record(51)
                    ea_response(ledger,51,602,d,'3999.75');ledger.resolve_sl(51)
                    reentries=NativeSupportReentry(c)
                    if armed:reentries.arm('1')
                    a.fill_order(52,89);a.ask=3999.1
                    if not armed:
                        before=len(a.sent)
                        with self.assertRaises(RuntimeError):reentries.arm('1')
                        self.assertEqual(len(a.sent),before);self.assertEqual(a.orders,[])
                    else:
                        p.initial('1',order_key=old['key'])
                        self.assertEqual([o.ticket for o in a.orders],[53])
                        # Symmetric: reverse fill retires the still-pending same-side reentry.
                        result=NativeReentryRetirement(c).retire('1',order_key=old['key'])
                        self.assertEqual(result['phase'],'verified');self.assertEqual(a.orders,[])
                        following=reverse.arm('1',order_key=old['key'])
                        self.assertEqual(following['intent']['side'],'BUY')
                        self.assertEqual(float(following['intent']['price']),4000)
