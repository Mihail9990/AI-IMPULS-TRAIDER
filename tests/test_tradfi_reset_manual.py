"""Manual settlement/reset is read-only at the broker and atomic in SQLite."""
from copy import deepcopy
from decimal import Decimal as D
import json
import unittest
from unittest.mock import patch

import test_tradfi_cycle_deadline as fixtures
from tradfi.storage import DurabilityError


class ResetManualTest(unittest.TestCase):
    setUp=fixtures.CycleDeadlineTest.setUp
    tearDown=fixtures.CycleDeadlineTest.tearDown
    restart=fixtures.CycleDeadlineTest.restart
    start_pair=fixtures.CycleDeadlineTest.start_pair
    position=fixtures.CycleDeadlineTest.position
    ledger=fixtures.CycleDeadlineTest.ledger
    advance=fixtures.CycleDeadlineTest.advance

    def manual_position(self):
        self.position(); self.advance(86400); self.bot.tick()
        self.assertEqual(self.bot.state['phase'],'MANUAL')

    def manual_exit(self, profit=-7):
        deal=self.api.close_position(reason=0,profit=profit)
        self.api.deals[-1]=deal._replace(magic=0)
        return self.api.deals[-1]

    def test_external_manual_close_books_actual_loss_and_writes_off_recovery(self):
        self.manual_position(); deal=self.manual_exit(); count=len(self.api.sends)
        self.bot.command('/reset_manual',10)
        self.assertEqual(self.bot.state['phase'],'WAIT_START'); self.assertIsNone(self.bot.state['cycle'])
        self.assertEqual(len(self.api.sends),count)
        result=self.bot.state['completed'][-1]
        self.assertEqual(result['completion'],'MANUAL_RESET'); self.assertFalse(result['profitable_completion'])
        self.assertEqual(D(result['realized']),D('-7')); self.assertEqual(D(result['written_off_recovery']),D('8.8'))
        self.assertEqual(self.bot.state['seen_deals'],[str(deal.ticket)])
        self.assertEqual(len(self.ledger()),1)
        audit=self.store.db.execute("SELECT payload FROM audit WHERE event='manual_reset'").fetchone()
        self.assertEqual(json.loads(audit[0])['settlement'],result)

    def test_already_accounted_native_close_not_counted_again(self):
        self.manual_position(); self.api.close_position(reason=4); self.bot.tick()
        self.assertEqual(D(self.bot.state['cycle']['realized']),D('-18'))
        self.bot.command('/reset_manual')
        self.assertEqual(D(self.bot.state['completed'][-1]['realized']),D('-18'))
        self.assertEqual(D(self.bot.state['completed'][-1]['written_off_recovery']),D('19.8'))
        self.assertEqual(len(self.ledger()),1)

    def test_unfilled_pair_requires_positive_cancellation_not_empty_snapshot(self):
        self.start_pair(); self.bot._enter_manual(self.bot.state['cycle'],'MANUAL',self.api.now)
        orders=list(self.api.orders); self.api.orders=[]
        with self.assertRaises(ValueError): self.bot.command('/reset_manual')
        self.assertEqual(self.bot.state['phase'],'MANUAL')
        self.api.history.extend(o._replace(state=2,time_done_msc=self.api.now*1000) for o in orders)
        count=len(self.api.sends); self.bot.command('/reset_manual')
        self.assertEqual(self.bot.state['phase'],'WAIT_START'); self.assertEqual(len(self.api.sends),count)
        self.assertEqual(self.bot.state['completed'][-1]['realized'],'0')
        self.assertFalse(self.bot.state['completed'][-1]['recovery_initialized'])
        self.assertEqual(self.ledger(),[])

    def test_live_pending_or_position_blocks_without_trading(self):
        self.manual_position(); count=len(self.api.sends)
        with self.assertRaises(ValueError): self.bot.command('/reset_manual')
        self.assertEqual(len(self.api.sends),count); self.assertEqual(self.bot.state['phase'],'MANUAL')

    def test_foreign_live_position_blocks_even_after_owned_close(self):
        self.manual_position(); p=self.api.positions[0]; self.manual_exit()
        self.api.positions=[p._replace(identifier=99999,ticket=88888,magic=0)]
        with self.assertRaises(ValueError): self.bot.command('/reset_manual')
        self.assertEqual(self.ledger(),[])

    def test_unknown_no_id_and_missing_journal_cannot_be_reset(self):
        self.api.unknown=True; self.bot.command('/start'); self.api.rates[0]['time']+=60; self.bot.tick()
        self.advance(86400); self.bot.tick()
        with self.assertRaises(ValueError): self.bot.command('/reset_manual')
        self.assertEqual(len(self.api.sends),1); self.assertEqual(self.ledger(),[])

    def test_missing_entry_journal_preserves_manual_cycle(self):
        self.manual_position(); self.manual_exit()
        cid=self.bot.state['cycle']['entries'][0]
        self.store.db.execute('DELETE FROM mt5_commands WHERE id=?',(cid,)); self.restart()
        with self.assertRaises(ValueError): self.bot.command('/reset_manual')
        self.assertEqual(self.bot.state['phase'],'MANUAL'); self.assertEqual(self.ledger(),[])

    def test_partial_external_exit_needs_entire_owned_volume(self):
        self.manual_position(); self.api.close_position(reason=0,volume=.04,profit=-3)
        with self.assertRaises(ValueError): self.bot.command('/reset_manual')
        self.api.close_position(reason=0,profit=-4); self.bot.command('/reset_manual')
        self.assertEqual(D(self.bot.state['completed'][-1]['realized']),D('-7'))
        self.assertEqual(len(self.ledger()),2)

    def test_two_fills_during_manual_require_both_settled(self):
        self.start_pair(); self.bot._enter_manual(self.bot.state['cycle'],'MANUAL',self.api.now)
        self.api.fill(); self.api.fill(); self.bot.tick()
        self.manual_exit(-2)
        with self.assertRaises(ValueError): self.bot.command('/reset_manual')
        self.manual_exit(-3); count=len(self.api.sends); self.bot.command('/reset_manual')
        self.assertEqual(D(self.bot.state['completed'][-1]['realized']),D('-5'))
        self.assertEqual(len(self.ledger()),2); self.assertEqual(len(self.api.sends),count)
        self.assertIsNone(self.bot.state['completed'][-1]['written_off_recovery'])

    def test_stale_quote_blocks_reset_but_not_manual_observation(self):
        self.manual_position(); self.manual_exit(); self.api.now+=10; self.mono+=10
        with self.assertRaises(OSError): self.bot.command('/reset_manual')
        self.assertEqual(self.bot.state['phase'],'MANUAL'); self.assertEqual(self.ledger(),[])

    def test_repeat_restart_and_next_cycle_use_new_settings_and_fresh_candle(self):
        self.manual_position(); old=self.bot.state['cycle']['id']; self.manual_exit()
        self.bot.command('/reset_manual',10); self.restart(); self.bot.command('/reset_manual',11)
        self.assertEqual(len(self.bot.state['completed']),1); self.assertEqual(len(self.ledger()),1)
        with self.assertRaises(ValueError): self.bot.command('/start')  # fixture's M1 is deliberately old
        self.bot.state['settings']['INITIAL_SIZE']='0.10'; self.bot.save('test_new_settings')
        self.api.tick=self.api.tick._replace(bid=4000.,ask=4000.1)
        self.api.rates[0]['time']=self.api.now//60*60-60
        self.bot.command('/start'); self.assertEqual(self.bot.state['phase'],'WAIT_CANDLE')
        self.bot.tick(); self.assertIsNone(self.bot.state['cycle'])
        self.advance(60); self.api.rates[0]['time']=self.api.now//60*60-60; self.bot.tick()
        c=deepcopy(self.bot.state['cycle'])
        self.assertNotEqual(c['id'],old); self.assertEqual(c['settings']['INITIAL_SIZE'],'0.10')
        self.assertEqual(c['realized'],'0'); self.assertIsNone(c['recovery'])
        self.assertEqual(c['deadline_utc'],self.api.now+86400)
        self.bot.command('/reset_manual',10)  # duplicate old Telegram update cannot touch new cycle
        with self.assertRaises(ValueError): self.bot.command('/reset_manual',12)
        self.assertEqual(self.bot.state['cycle'],c)

    def test_reset_faults_are_atomic_and_lost_ack_is_idempotent(self):
        self.manual_position(); self.manual_exit()
        for event in ('manual_reset_deals_inserted','manual_reset_snapshot_written','manual_reset_before_commit'):
            def crash(actual):
                if actual==event: raise RuntimeError('crash')
            self.store.fault=crash
            with self.subTest(event=event),self.assertRaises(DurabilityError): self.bot.command('/reset_manual',10)
            self.assertEqual(self.store.load()['phase'],'MANUAL'); self.assertEqual(self.ledger(),[])
            self.assertEqual(self.bot.state,self.store.load()); self.restart()
        self.store.fault=lambda e: (_ for _ in ()).throw(RuntimeError('lost ack')) if e=='manual_reset_after_commit' else None
        with self.assertRaises(DurabilityError): self.bot.command('/reset_manual',10)
        self.restart(); self.bot.command('/reset_manual',10); self.bot.command('/reset_manual',11)
        self.assertEqual(self.bot.state['phase'],'WAIT_START'); self.assertEqual(len(self.ledger()),1)
        self.assertEqual(len(self.bot.state['completed']),1)

    def test_conflicting_history_blocks_reset_without_loss(self):
        self.manual_position(); deal=self.manual_exit()
        self.api.deals.append(deal._replace(profit=123))
        with self.assertRaises(ValueError): self.bot.command('/reset_manual')
        self.assertEqual(self.bot.state['phase'],'MANUAL'); self.assertEqual(self.ledger(),[])

    def test_nonmanual_reset_rejected(self):
        self.position()
        with self.assertRaises(ValueError): self.bot.command('/reset_manual')
        self.assertEqual(self.bot.state['phase'],'POSITION')


    def prepare_partial_opening(self, state, remainder):
        self.start_pair(); self.bot._enter_manual(self.bot.state['cycle'],'MANUAL',self.api.now); self.api.fill(volume=.04)
        self.api.history[0]=self.api.history[0]._replace(state=state,volume_current=remainder)
        opposite=self.api.orders.pop()
        self.api.history.append(opposite._replace(state=2,time_done_msc=self.api.now*1000))
        self.manual_exit(-2)

    def test_partial_filled_cancelled_order_settles_actual_filled_volume(self):
        self.prepare_partial_opening(state=2,remainder=.05)
        self.bot.command('/reset_manual')
        self.assertEqual(self.bot.state['phase'],'WAIT_START')
        self.assertEqual(D(self.bot.state['completed'][-1]['realized']),D('-2'))
        self.assertEqual(len(self.ledger()),1)

    def test_partial_filled_order_with_wrong_remainder_cannot_reset(self):
        self.prepare_partial_opening(state=2,remainder=.04)
        with self.assertRaises(ValueError): self.bot.command('/reset_manual')
        self.assertEqual(self.bot.state['phase'],'MANUAL'); self.assertEqual(self.ledger(),[])

    def test_partial_filled_nonterminal_order_cannot_reset(self):
        self.prepare_partial_opening(state=3,remainder=.05)
        with self.assertRaises(ValueError): self.bot.command('/reset_manual')
        self.assertEqual(self.bot.state['phase'],'MANUAL'); self.assertEqual(self.ledger(),[])

    def test_consumed_fingerprint_disagreement_blocks_reset_even_without_cached_deal(self):
        self.manual_position(); closed=self.api.close_position(reason=4); self.bot.tick()
        self.broker.facts['deal'].pop(str(closed.ticket))
        self.store.db.execute("DELETE FROM mt5_facts WHERE kind='deal' AND ticket=?",(str(closed.ticket),))
        self.api.deals[-1]=closed._replace(profit=-99)
        with self.assertRaises(DurabilityError): self.bot.command('/reset_manual')
        self.assertEqual(self.bot.state['phase'],'MANUAL'); self.assertEqual(len(self.ledger()),1)
        self.assertEqual(D(self.bot.state['cycle']['realized']),D('-18'))

    def test_stale_controller_cannot_commit_reset_over_newer_state(self):
        self.manual_position(); self.manual_exit()
        from tradfi.engine import Controller
        stale=Controller(self.store,self.broker,clock=lambda:self.api.now,monotonic=lambda:self.mono)
        self.bot.command('/status')
        with self.assertRaises(DurabilityError): stale.command('/reset_manual')
        self.assertEqual(self.store.load()['phase'],'MANUAL'); self.assertEqual(self.ledger(),[])

if __name__=='__main__': unittest.main()
