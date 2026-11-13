"""Cycle-isolation regressions for current-cycle accounting and archive separation.

No native API, TTL, automatic closing, history deletion or publication.
"""
from copy import deepcopy
from decimal import Decimal as D
import unittest

import test_tradfi_evidence_controller as fixtures
from test_tradfi_mt5 import record
from tradfi.engine import Controller


class CycleScopeAuditTest(unittest.TestCase):
    setUp=fixtures.EvidenceControllerTest.setUp
    tearDown=fixtures.EvidenceControllerTest.tearDown
    restart=fixtures.EvidenceControllerTest.restart
    start_pair=fixtures.EvidenceControllerTest.start_pair
    position=fixtures.EvidenceControllerTest.position
    ledger=fixtures.EvidenceControllerTest.ledger

    def finish_first(self):
        self.position(); cid=self.bot.state['cycle']['id']
        deal=self.api.close_position(); self.bot.tick()
        self.assertEqual(self.bot.state['phase'],'COOLDOWN')
        return cid,deal

    def next_candle(self):
        self.api.now+=61
        self.api.tick=self.api.tick._replace(bid=4000.,ask=4000.1,time_msc=self.api.now*1000)
        self.api.rates[0]['time']+=60
        self.bot.tick()
        if self.bot.state['phase']=='WAIT_CANDLE':
            self.api.rates[0]['time']+=60; self.bot.tick()

    def seed_manual_history(self):
        # Same symbol and magic deliberately cannot establish ownership by themselves.
        stamp=(self.api.now-3600)*1000
        self.api.history.append(record(ticket=901,symbol='XAUUSD.s',magic=self.broker.magic,
            comment='manual',type=4,state=4,volume_initial=.09,volume_current=0.,
            price_open=4003.,sl=4001.,tp=4003.2,position_id=902,
            time_setup_msc=stamp-1,time_done_msc=stamp))
        entry=record(ticket=903,order=901,position_id=902,symbol='XAUUSD.s',magic=self.broker.magic,
            entry=0,type=0,volume=.09,price=4003.,profit=0.,reason=3,time_msc=stamp)
        self.api.deals.extend([entry,entry._replace(ticket=904,order=905,entry=1,type=1,
            reason=5,profit=99999.,time_msc=stamp+1)])

    def test_two_cycles_keep_separate_recovery_and_ledger_cycle_ids(self):
        first,old_exit=self.finish_first(); self.restart(); self.next_candle()
        self.assertEqual(self.bot.state['phase'],'PAIR')
        current=self.bot.state['cycle']; second=current['id']
        self.assertNotEqual(first,second); self.assertEqual(D(current['realized']),D(0))
        self.assertIsNone(current['recovery'])
        self.api.fill(); self.bot.tick(); self.bot.tick()
        self.assertEqual(D(self.bot.state['cycle']['recovery']),D('1.8'))
        new_exit=self.api.close_position(reason=4); self.bot.tick()
        current=deepcopy(self.bot.state['cycle'])
        self.assertEqual(D(current['realized']),D('-18'))
        self.assertEqual(D(current['recovery']),D('19.8'))
        self.assertEqual(D(self.bot.state['completed'][0]['realized']),D('1.8'))
        self.assertEqual({r['ticket']:r['cycle_id'] for r in self.ledger()},
            {str(old_exit.ticket):first,str(new_exit.ticket):second})
        self.restart(); self.assertEqual(self.bot.state['cycle'],current)

    def test_manual_history_same_magic_never_enters_current_money(self):
        self.seed_manual_history(); self.position()
        self.assertEqual(D(self.bot.state['cycle']['recovery']),D('1.8'))
        self.assertEqual(D(self.bot.state['cycle']['realized']),D(0))
        snap=self.bot.snapshot()
        self.assertNotIn('904',{d['id'] for d in snap['deals']})
        exit=self.api.close_position(); self.bot.tick()
        self.assertEqual(D(self.bot.state['completed'][0]['realized']),D('1.8'))
        self.assertEqual([r['ticket'] for r in self.ledger()],[str(exit.ticket)])

    def test_old_unknown_obligation_cannot_be_discarded_at_new_cycle(self):
        first,_=self.finish_first(); cid='f'*24
        cmd=dict(id=cid,cycle_id=first,kind='STOP',side='BUY',size='.09',price='4003',
                 trigger='4003',sl='4001',tp='4003.2')
        self.bot.state['commands'][cid]=dict(cmd,status='UNKNOWN'); self.bot.save('audit_old_unknown')
        self.broker._save_row(cid,dict(command=cmd,account=self.broker.account,send_started=True,
                                      raw_result=None,result=dict(status='UNKNOWN')),'audit_old_unknown')
        count=len(self.api.sends); self.restart(); self.next_candle()
        self.assertIsNone(self.bot.state['cycle']); self.assertEqual(len(self.api.sends),count)
        self.assertEqual(self.bot.state['commands'][cid]['status'],'UNKNOWN')
        with self.assertRaises(ValueError): self.bot.command('/start')

    def test_requirement_every_intent_has_current_cycle_binding(self):
        self.position()
        cycle=self.bot.state['cycle']['id']
        self.assertTrue(all(cmd.get('cycle_id')==cycle for cmd in self.bot.state['commands'].values()))
        self.assertTrue(all(row['command'].get('cycle_id')==cycle for row in self.broker.rows.values()))

    def test_requirement_finished_cycle_history_is_diagnostic_only(self):
        self.finish_first(); self.restart()
        # Only historical order(ticket) queries fail; current observations are available.
        archived={o.ticket for o in self.api.history}
        original=self.api.history_orders_get
        self.api.history_orders_get=lambda *args,**kw: None if kw.get('ticket') in archived else original(*args,**kw)
        self.next_candle()
        self.assertEqual(self.bot.state['phase'],'PAIR')
        self.assertEqual(len(self.api.orders),2)

    def test_requirement_manual_history_conflict_cannot_block_own_cycle(self):
        self.seed_manual_history(); self.position()
        self.api.deals[1]=self.api.deals[1]._replace(profit=12345.)
        self.bot.tick()
        self.assertEqual(self.bot.state['phase'],'POSITION')
        self.assertEqual(D(self.bot.state['cycle']['recovery']),D('1.8'))


    def test_cross_cycle_entry_reference_is_rejected(self):
        self.position(); old_entry=self.bot.state['cycle']['entries'][0]
        self.api.close_position(); self.bot.tick(); self.next_candle()
        self.bot.state['cycle']['entries'][0]=old_entry; self.bot.save('audit_cross_cycle_reference')
        count=len(self.api.sends); self.bot.tick()
        self.assertEqual(self.bot.state['phase'],'SAFE_PAUSE')
        self.assertEqual(len(self.api.sends),count)

    def test_current_discovery_lower_bound_is_durable_cycle_start(self):
        self.position(); self.api.calls.clear(); self.bot.tick()
        lower=self.bot.state['cycle']['started_at_utc']-60
        ranges=[args for _,args,_ in self.api.calls if args]
        self.assertTrue(ranges)
        self.assertTrue(all(args[0].timestamp()==lower for args in ranges))

    def test_foreign_live_position_still_blocks_current_cycle(self):
        self.position(); p=self.api.positions[0]
        self.api.positions.append(p._replace(ticket=900,identifier=902,magic=0))
        count=len(self.api.sends); self.bot.tick()
        self.assertEqual(self.bot.state['phase'],'SAFE_PAUSE'); self.assertEqual(len(self.api.sends),count)

if __name__=='__main__': unittest.main()
