from copy import deepcopy
from decimal import Decimal as D
import unittest
from tradfi.control import ActionRejected,ActionGateway
from tradfi.basket_latest_close import LatestCloseBasketController
import test_basket_latest_close as fixtures

class Writeoff(unittest.TestCase):
    def setUp(self):
        c=fixtures.LatestClose();c.setUp();self.addCleanup(c.doCleanups);self.case=c;self.f=c.f
        c.first_sl();c.fill_basket();self.f.tick(2)
        for p in list(self.f.b.data['positions']):
            self.f.b.data['now']+=1;self.f.b.close('TP',position_id=p['id'],commission='-1')
        self.f.b.quote('3998','3998.1');self.f.bot.command('/stop');self.f.tick(5)
    def action(self):
        c=self.f.bot.state.get('cycle');return dict(type='writeoff_recovery',cycle_id=(c or {}).get('id'),expected_debt=(c or {}).get('recovery',self.f.bot.state['basket_carry']),confirmed=True)
    def test_flat_writeoff_preserves_pnl_and_waits_fresh_candle(self):
        f=self.f;before=deepcopy(f.b.data['deals']);calls=len(f.b.commands);net=f.bot.state['cycle']['realized'];debt=f.bot.state['cycle']['recovery'];a=self.action()
        f.bot.action(a)
        self.assertEqual(len(f.b.commands),calls);self.assertEqual(f.b.data['deals'],before)
        self.assertIsNone(f.bot.state['cycle']);self.assertEqual(f.bot.state['completed'][-1]['realized'],net)
        self.assertEqual(f.bot.state['recovery_writeoffs'][-1]['amount'],debt)
        self.assertEqual(f.bot.state['phase'],'WAIT_CANDLE');f.tick(2);self.assertFalse(f.b.data['orders'])
        f.bot=LatestCloseBasketController(f.store,f.b);f.tick(2);self.assertEqual(len(f.bot.state['recovery_writeoffs']),1)
        with self.assertRaises(ActionRejected):f.bot.action(a)
        self.assertFalse(f.bot.state['stop'])
        boundary=f.bot.state['last_candle']
        f.bar(cid=boundary+60,side='BUY',close='3998');f.tick(3)
        self.assertEqual(len(f.b.data['orders']),1)
        self.assertNotEqual(f.bot.state['cycle']['id'],a['cycle_id'])
        self.assertEqual(D(f.bot.state['cycle']['recovery']),0)
        self.assertEqual(f.b.data['orders'][0]['side'],'BUY')
    def test_audit_and_download_preserve_writeoff_amount(self):
        import json
        from tradfi.web_logs import DiagnosticLogs
        self.f.bot.action(self.action());event,payload=self.f.store.db.execute('SELECT event,payload FROM audit ORDER BY seq DESC LIMIT 1').fetchone()
        self.assertEqual(event,'recovery_waived_without_pnl_debit')
        self.assertEqual(json.loads(payload)['recovery_writeoff']['amount'],self.f.bot.state['recovery_writeoffs'][-1]['amount'])
        logs=DiagnosticLogs();logs.capture('live',self.f.store,self.f.bot.state,self.f.b.data['now'])
        self.assertIn(b'RECOVERY_WAIVED',logs.response('live',0,download=True))

    def test_gateway_duplicate_once(self):
        g=ActionGateway(self.f.bot,execute=self.f.bot.action);r=dict(id='waive-request-1',revision=self.f.bot.state.get('control_revision',0),action=self.action())
        self.assertEqual(g.apply(r)['status'],'APPLIED');self.assertEqual(g.apply(r)['status'],'APPLIED')
        self.assertEqual(len(self.f.bot.state['recovery_writeoffs']),1)
    def test_reject_stale_unconfirmed_readonly_unknown_time_and_incomplete(self):
        original=deepcopy(self.f.bot.state)
        for kind in ('stale','confirmation','readonly','unknown','time','incomplete','running'):
            with self.subTest(kind=kind):
                self.f.bot.state=deepcopy(original);a=self.action();self.f.b.read_only=False
                if kind=='stale':a['expected_debt']='999'
                if kind=='confirmation':a['confirmed']=False
                if kind=='readonly':self.f.b.read_only=True
                if kind=='unknown':next(iter(self.f.bot.state['commands'].values()))['status']='UNKNOWN'
                if kind=='time':self.f.bot.state['time_block']={'reason':'test'}
                if kind=='incomplete':next(iter(self.f.bot.state['cycle']['tickets'].values()))['closed']=False
                if kind=='running':self.f.bot.state['stop']=False
                calls=len(self.f.b.commands)
                with self.assertRaises(ActionRejected):self.f.bot.action(a)
                self.assertEqual(len(self.f.b.commands),calls);self.assertNotIn('recovery_writeoffs',self.f.bot.state)
    def test_prior_unknown_ui_receipt_blocks_direct_and_gateway(self):
        self.f.bot.state['control_receipts']={'old-uncertain':dict(status='UNKNOWN',revision=1,digest='test')}
        self.f.bot.state['control_revision']=1
        with self.assertRaises(ActionRejected):self.f.bot.action(self.action())
        r=dict(id='new-writeoff',revision=1,action=self.action())
        self.assertEqual(ActionGateway(self.f.bot,execute=self.f.bot.action).apply(r)['status'],'REJECTED')
        self.assertNotIn('recovery_writeoffs',self.f.bot.state)
    def test_crash_after_durable_writeoff_no_replay_or_double_writeoff(self):
        bot=self.f.bot;save=bot.save
        def crash(event):
            save(event)
            if event=='recovery_waived_without_pnl_debit':raise OSError('simulated lost completion')
        bot.save=crash;g=ActionGateway(bot,execute=bot.action)
        r=dict(id='lost-completion',revision=bot.state.get('control_revision',0),action=self.action())
        self.assertEqual(g.apply(r)['status'],'UNKNOWN');self.assertEqual(len(bot.state['recovery_writeoffs']),1)
        bot=LatestCloseBasketController(self.f.store,self.f.b)
        self.assertEqual(ActionGateway(bot,execute=bot.action).apply(r)['status'],'UNKNOWN')
        self.assertEqual(len(bot.state['recovery_writeoffs']),1)
    def test_retained_carry_without_cycle_no_pnl_double_debit(self):
        bot=self.f.bot;bot.state.update(cycle=None,basket_carry='.30',phase='WAIT_START',stop=True)
        completed=deepcopy(bot.state['completed']);bot.save('carry_fixture');bot.action(self.action())
        self.assertEqual(bot.state['completed'],completed);self.assertEqual(bot.state['basket_carry'],'0')
        self.assertEqual(bot.state['recovery_writeoffs'][-1]['amount'],'0.30')

    def test_live_order_rejected_without_cancel(self):
        self.f.b.data['orders'].append(dict(id='foreign',owned=False));calls=len(self.f.b.commands)
        with self.assertRaises(ActionRejected):self.f.bot.action(self.action())
        self.assertEqual(len(self.f.b.commands),calls)

class NativeWriteoff(unittest.TestCase):
    def test_native_confirmed_flat_no_broker_mutation(self):
        c=fixtures.NativeLatestClose();c.setUp();self.addCleanup(c.doCleanups);c.first_sl();f=c.f
        for _ in range(6):f.api.fill(index=next(i for i,o in enumerate(f.api.orders) if o.type==f.api.ORDER_TYPE_SELL_STOP))
        f.tick(5)
        for _ in range(6):
            f.api.now+=1;f.api.close_position(reason=f.api.DEAL_REASON_TP);f.api.deals[-1]=f.api.deals[-1]._replace(commission=-1.)
        f.api.tick=f.api.tick._replace(bid=3998.,ask=3998.1,time_msc=f.api.now*1000)
        f.bot.command('/stop');f.tick(5);c=f.bot.state['cycle'];before=len(f.api.sends)
        f.bot.action(dict(type='writeoff_recovery',cycle_id=c['id'],expected_debt=c['recovery'],confirmed=True))
        self.assertEqual(len(f.api.sends),before);self.assertEqual(f.bot.state['phase'],'WAIT_CANDLE')
