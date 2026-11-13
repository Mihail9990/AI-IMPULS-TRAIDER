import json
import unittest
from decimal import Decimal as D
from tradfi.provisional_recovery import estimate_previous_close, effective_recovery, without_settled_estimate


class Broker:
    def __init__(self): self.calls=[]
    def profit(self, side, volume, entry, close):
        self.calls.append((side,volume,entry,close))
        return (close-entry)*volume*D('100')*(1 if side=='BUY' else -1)


def ticket(pid='old', **kw):
    return dict(id=pid,command_id='cmd-'+pid,side='BUY',size='0.05',entry='2000',sl='1998',closed=False,**kw)


class ProvisionalRecoveryTests(unittest.TestCase):
    def test_exact_previous_inputs_and_costs_once(self):
        b=Broker();r=estimate_previous_close(b,ticket(),cycle_id='c')
        self.assertEqual(b.calls,[('BUY',D('.05'),D('2000'),D('1998'))])
        self.assertEqual(effective_recovery('-0.30',{'old':r},cycle_id='c'),D('10.30'))

    def test_actual_replaces_not_adds(self):
        r=estimate_previous_close(Broker(),ticket(),cycle_id='c');rows={'old':r}
        new=without_settled_estimate(rows,'old',cycle_id='c',command_id='cmd-old')
        self.assertIn('old',rows)
        self.assertEqual(effective_recovery('-11.70',new,cycle_id='c'),D('11.70'))
        self.assertEqual(without_settled_estimate(new,'old',cycle_id='c',command_id='cmd-old'),{})

    def test_verified_partial_volume(self):
        r=estimate_previous_close(Broker(),ticket(),cycle_id='c',remaining_volume='.03')
        # Already booked .02 lot loss plus costs; estimate only remaining .03.
        self.assertEqual(effective_recovery('-4.30',{'old':r},cycle_id='c'),D('10.30'))

    def test_multiple_restart_and_waiver(self):
        rows={p:estimate_previous_close(Broker(),ticket(p),cycle_id='c') for p in ('a','b')}
        restored=json.loads(json.dumps(rows))
        self.assertEqual(effective_recovery('3',restored,cycle_id='c',waived='2'),D('15'))
        self.assertEqual(effective_recovery('30',restored,cycle_id='c'),D(0))

    def test_sell_and_moved_sl(self):
        t=ticket();t.update(side='SELL',sl='2001')
        r=estimate_previous_close(Broker(),t,cycle_id='c')
        self.assertEqual(D(r['estimated_close_net']),D('-5'))

    def test_failure_does_not_return_zero_or_mutate(self):
        class Bad:
            def profit(self,*args): raise OSError('unavailable')
        t=ticket();before=dict(t)
        with self.assertRaises(OSError):estimate_previous_close(Bad(),t,cycle_id='c')
        self.assertEqual(t,before)
        for volume in ('0','-.01','.06','NaN'):
            with self.assertRaises((ValueError,ArithmeticError)):
                estimate_previous_close(Broker(),t,cycle_id='c',remaining_volume=volume)

    def test_identity_boundaries(self):
        r=estimate_previous_close(Broker(),ticket(),cycle_id='c')
        with self.assertRaises(ValueError):effective_recovery('0',{'old':r},cycle_id='other')
        with self.assertRaises(ValueError):without_settled_estimate({'old':r},'old',cycle_id='c',command_id='wrong')
        t=ticket();t['closed']=True
        with self.assertRaises(ValueError):estimate_previous_close(Broker(),t,cycle_id='c')

class TargetTests(unittest.TestCase):
    def test_only_last_open_rung_and_closed_profit_not_added_twice(self):
        from tradfi.provisional_recovery import last_open_target
        class Spec:
            tick_size=D('.01')
            def price(self,x):
                x=D(x)
                if x<=0 or x % self.tick_size: raise ValueError('price')
                return x
        ts=[dict(id='a',index=0,side='BUY',size='.01',entry='2000',tp='2001',closed=False),
            dict(id='b',index=1,side='BUY',size='.02',entry='2000',tp='2002',closed=False)]
        result=last_open_target(Broker(),Spec(),ts,'10',reserve='.10')
        self.assertEqual(result,dict(position_id='b',tp='2004.55'))
        self.assertEqual(ts[0]['tp'],'2001')
        ts[1]['closed']=True
        self.assertEqual(last_open_target(Broker(),Spec(),ts,'3')['position_id'],'a')
        self.assertEqual(last_open_target(Broker(),Spec(),ts,'0')['tp'],'2000.01')

    def test_sell_target_and_impossible_target(self):
        from tradfi.provisional_recovery import last_open_target
        class Spec:
            tick_size=D('.01')
            def price(self,x):
                x=D(x)
                if x<=0 or x % self.tick_size: raise ValueError('price')
                return x
        ts=[dict(id='s',index=4,side='SELL',size='.01',entry='10',tp='9',closed=False)]
        self.assertEqual(last_open_target(Broker(),Spec(),ts,'3')['tp'],'7.00')
        with self.assertRaises(ValueError):last_open_target(Broker(),Spec(),ts,'20')

    def test_target_failure_preserves_inputs(self):
        from tradfi.provisional_recovery import last_open_target
        class Spec:
            tick_size=D('.01')
            def price(self,x):return D(x)
        class Bad:
            def profit(self,*args):raise OSError('no calculation')
        ts=[dict(id='a',index=0,side='BUY',size='.01',entry='10',tp='11',closed=False)]
        before=json.dumps(ts)
        with self.assertRaises(OSError):last_open_target(Bad(),Spec(),ts,'20')
        self.assertEqual(json.dumps(ts),before)

    def test_both_directions_and_quote_limits(self):
        from tradfi.provisional_recovery import last_open_target
        class Spec:
            tick_size=D('.01');point=D('.01');stops_level=D(10);freeze_level=D(20)
            def price(self,x):
                x=D(x)
                if x<=0 or x % self.tick_size:raise ValueError('price')
                return x
        t=dict(id='a',index=0,side='BUY',size='.01',entry='10',tp='15',closed=False)
        self.assertEqual(last_open_target(Broker(),Spec(),[t],'2')['tp'],'12.00')
        self.assertEqual(last_open_target(Broker(),Spec(),[t],'-2',quote={'bid':'11','ask':'11.1'})['tp'],'11.20')
        t.update(side='SELL',tp='5')
        self.assertEqual(last_open_target(Broker(),Spec(),[t],'2')['tp'],'8.00')
        self.assertEqual(last_open_target(Broker(),Spec(),[t],'-2',quote={'bid':'8.9','ask':'9'})['tp'],'8.80')

    def test_previous_rungs_fixed_and_outward_grid(self):
        from tradfi.provisional_recovery import last_open_target
        class Spec:
            tick_size=D('.1');point=D('.01');stops_level=D(13);freeze_level=D(0)
            def price(self,x):
                x=D(x)
                if x<=0 or x % self.tick_size:raise ValueError('price')
                return x
        ts=[dict(id='a',index=0,side='BUY',size='.01',entry='10',tp='12',closed=False),
            dict(id='b',index=1,side='BUY',size='.02',entry='10.1',tp='20',closed=False)]
        before=json.dumps(ts)
        self.assertEqual(last_open_target(Broker(),Spec(),ts,'-4',quote={'bid':'12.11','ask':'12.2'})['tp'],'12.3')
        self.assertEqual(json.dumps(ts),before)
