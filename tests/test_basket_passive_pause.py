"""Passive LIVE accounting uses only a deterministic MT5 double, never a terminal."""
from copy import deepcopy
from decimal import Decimal as D
from unittest.mock import patch
import unittest
import test_tradfi_basket as fixtures
from tradfi.storage import DurabilityError


class PassivePauseTest(unittest.TestCase):
    setUp=fixtures.NativeBasketTest.setUp
    tearDown=fixtures.NativeBasketTest.tearDown
    restart=fixtures.NativeBasketTest.restart
    tick=fixtures.NativeBasketTest.tick
    initial=fixtures.NativeBasketTest.initial
    reversal=fixtures.NativeBasketTest.reversal

    def paused(self):
        self.initial()
        self.bot.pause('UNOWNED_BROKER_OBLIGATIONS')
        self.bot.state['stop']=True;self.bot.save('fixture_stop')
        self.before=deepcopy(self.bot.state)

    def no_trades_tick(self,n=1):
        with patch.object(self.api,'order_send',side_effect=AssertionError('send forbidden')), \
             patch.object(self.api,'order_check',side_effect=AssertionError('check forbidden')), \
             patch.object(self.bot,'dispatch',side_effect=AssertionError('dispatch forbidden')), \
             patch.object(self.bot,'_cancel',side_effect=AssertionError('cancel forbidden')), \
             patch.object(self.bot,'_reconcile_commands',side_effect=AssertionError('command mutation forbidden')):
            self.tick(n)
        self.assertEqual(self.bot.state['phase'],'SAFE_PAUSE')
        self.assertTrue(self.bot.state['stop'])

    def ledger_count(self):
        return self.store.db.execute('SELECT count(*) FROM consumed_deals').fetchone()[0]

    def test_tp_atomic_accounting_and_repeated_ticks_restart(self):
        self.paused();self.api.close_position(reason=self.api.DEAL_REASON_TP,profit=.17)
        self.no_trades_tick(3)
        c=self.bot.state['cycle'];t=next(iter(c['tickets'].values()))
        self.assertTrue(t['closed']);self.assertEqual(t['reason'],'TP')
        self.assertEqual(D(c['realized']),D('.17'));self.assertEqual(D(c['recovery']),0)
        self.assertEqual(self.ledger_count(),1)
        self.restart();self.no_trades_tick(2)
        self.assertEqual(D(self.bot.state['cycle']['realized']),D('.17'));self.assertEqual(self.ledger_count(),1)
        self.assertEqual(self.bot.state['completed'],self.before['completed'])

    def test_sl_accounts_actual_costs(self):
        self.paused();self.api.close_position(reason=self.api.DEAL_REASON_SL,profit=-1.25)
        d=self.api.deals[-1];self.api.deals[-1]=d._replace(commission=-.1,swap=-.2,fee=-.03)
        self.no_trades_tick()
        c=self.bot.state['cycle']
        self.assertEqual(D(c['realized']),D('-1.58'));self.assertEqual(D(c['recovery']),D('1.58'))
        self.assertEqual(next(iter(c['tickets'].values()))['reason'],'SL')

    def test_open_ticket_is_unchanged(self):
        self.paused();self.no_trades_tick();self.assertEqual(self.bot.state,self.before)

    def test_partial_close_is_not_booked(self):
        self.paused();self.api.close_position(reason=self.api.DEAL_REASON_SL,volume=.005)
        self.no_trades_tick();self.assertEqual(self.bot.state,self.before);self.assertEqual(self.ledger_count(),0)

    def test_external_close_is_not_booked(self):
        self.paused();self.api.close_position(reason=0)
        self.no_trades_tick();self.assertEqual(self.bot.state,self.before);self.assertEqual(self.ledger_count(),0)

    def test_missing_costs_and_ledger_facts_are_not_booked(self):
        self.paused();self.api.close_position(reason=self.api.DEAL_REASON_TP)
        snap=self.bot.snapshot()
        for what in ('cost','ledger','identity','volume','opening_cost','proof','live','mixed','command_cycle','duplicate'):
            with self.subTest(what=what):
                bad=deepcopy(snap);out=next(d for d in bad['deals'] if d['entry']=='OUT')
                if what=='cost':out.pop('commission')
                elif what=='ledger':bad['ledger_facts'].pop(out['id'])
                elif what=='identity':next(v for v in bad['obligations'].values() if v['status']=='CLOSED_EVIDENCE')['position_id']='foreign'
                elif what=='volume':out['volume']='0.005'
                elif what=='opening_cost':next(d for d in bad['deals'] if d['entry']=='IN')['fee']='-1'
                elif what=='live':bad['positions'].append(dict(id=out['position_id'],owned=True))
                elif what=='mixed':
                    out['volume']='0.005';other=dict(out,id='second',reason='SL');bad['deals'].append(other)
                elif what=='command_cycle':
                    cid=next(iter(self.bot.state['cycle']['tickets'].values()))['command_id']
                    old=self.bot.state['commands'][cid]['cycle_id'];self.bot.state['commands'][cid]['cycle_id']='foreign'
                elif what=='duplicate':bad['deals'].append(deepcopy(out))
                elif what=='proof':next(v for v in bad['obligations'].values() if v['status']=='CLOSED_EVIDENCE')['status']='PARTIAL_CLOSE'
                with patch.object(self.bot,'snapshot',return_value=bad):self.no_trades_tick()
                if what=='command_cycle':self.bot.state['commands'][cid]['cycle_id']=old
                self.assertEqual(self.bot.state,self.before);self.assertEqual(self.ledger_count(),0)

    def test_foreign_obligations_do_not_resume_or_cancel(self):
        self.paused();self.api.close_position(reason=self.api.DEAL_REASON_TP,profit=.17)
        snap=self.bot.snapshot();snap['orders'].append(dict(id='foreign-order',owned=False))
        snap['positions'].append(dict(id='foreign-position',owned=False))
        with patch.object(self.bot,'snapshot',return_value=snap):self.no_trades_tick()
        self.assertEqual(D(self.bot.state['cycle']['realized']),D('.17'));self.assertEqual(self.ledger_count(),1)
        self.assertEqual(self.bot.state.get('last_error'),self.before.get('last_error'))

    def test_expired_deadline_does_not_leave_pause(self):
        self.paused();self.api.close_position(reason=self.api.DEAL_REASON_TP)
        with patch.object(self.bot,'_deadline_guard',side_effect=AssertionError('deadline must not advance pause')):
            self.no_trades_tick()

    def test_transaction_failure_rolls_back_and_restart_books_once(self):
        self.paused();self.api.close_position(reason=self.api.DEAL_REASON_SL)
        def crash(event):
            if event=='closure_snapshot_written':raise RuntimeError('power loss')
        self.store.fault=crash
        with self.assertRaises(DurabilityError):self.no_trades_tick()
        self.assertEqual(self.bot.state,self.store.load());self.assertEqual(self.ledger_count(),0)
        self.store.fault=None;self.restart();self.no_trades_tick(2);self.assertEqual(self.ledger_count(),1)

    def test_unavailable_evidence_does_not_change_state(self):
        self.paused()
        for exc in (OSError('unavailable'),ValueError('invalid evidence')):
            with patch.object(self.bot,'snapshot',side_effect=exc):self.no_trades_tick()
            self.assertEqual(self.bot.state,self.before)

    def test_two_closed_recovery_tickets_book_once_each(self):
        self.reversal();self.bot.pause('fixture');self.bot.state['stop']=True;self.bot.save('fixture_stop')
        initial_net=D(self.bot.state['cycle']['realized']);initial_count=self.ledger_count()
        self.api.close_position(reason=self.api.DEAL_REASON_TP,profit=.17)
        self.api.close_position(reason=self.api.DEAL_REASON_SL,profit=-1.5)
        self.no_trades_tick(2)
        self.assertEqual(D(self.bot.state['cycle']['realized']),initial_net+D('.17')-D('1.5'))
        self.assertEqual(self.ledger_count(),initial_count+2)
        self.restart();self.no_trades_tick();self.assertEqual(self.ledger_count(),initial_count+2)

    def test_after_commit_fault_reloads_durable_accounting_without_replay(self):
        self.paused();self.api.close_position(reason=self.api.DEAL_REASON_TP,profit=.17)
        def crash(event):
            if event=='closure_after_commit':raise RuntimeError('power loss')
        self.store.fault=crash
        with self.assertRaises(DurabilityError):self.no_trades_tick()
        self.store.fault=None;self.restart();self.no_trades_tick(2)
        self.assertEqual(self.ledger_count(),1);self.assertEqual(D(self.bot.state['cycle']['realized']),D('.17'))
