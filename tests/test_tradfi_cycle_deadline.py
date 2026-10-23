"""24h manual handoff: deterministic UTC/monotonic clocks, injected offline MT5."""
from copy import deepcopy
from decimal import Decimal as D
import unittest
from unittest.mock import patch

import test_tradfi_evidence_controller as fixtures
from tradfi.engine import Controller
from tradfi.evidence_broker import MT5EvidenceBroker
from tradfi.storage import Store, DurabilityError


class CycleDeadlineTest(unittest.TestCase):
    setUp=fixtures.EvidenceControllerTest.setUp
    tearDown=fixtures.EvidenceControllerTest.tearDown
    start_pair=fixtures.EvidenceControllerTest.start_pair
    position=fixtures.EvidenceControllerTest.position
    ledger=fixtures.EvidenceControllerTest.ledger

    def restart(self):
        if hasattr(self,'store'): self.broker.close(); self.store.close()
        if not hasattr(self,'mono'): self.mono=10.
        self.store=Store(self.path)
        self.broker=MT5EvidenceBroker(self.config,self.store,api=self.api,clock=lambda:self.api.now)
        self.broker.connect()
        self.bot=Controller(self.store,self.broker,clock=lambda:self.api.now,monotonic=lambda:self.mono)

    def advance(self, seconds):
        self.api.now+=seconds; self.mono+=seconds
        self.api.tick=self.api.tick._replace(time_msc=self.api.now*1000)

    def notices(self):
        return [n for n in self.bot.state['notifications'] if n['text'].startswith('MANUAL:')]

    def test_expiry_cancels_pending_and_single_notification_survives_restart(self):
        self.start_pair(); orders=deepcopy(self.api.orders); sends=len(self.api.sends)
        start=self.bot.state['cycle']['started_at_utc']; self.advance(86400); self.bot.tick()
        self.assertEqual(self.bot.state['phase'],'MANUAL'); self.assertEqual(self.api.orders,[])
        self.assertEqual(len(self.api.sends),sends+2); self.assertEqual(len(self.notices()),1)
        self.restart(); self.bot.tick(); self.bot.dispatch(); self.bot.command('/stop')
        with self.assertRaises(ValueError): self.bot.command('/start')
        self.assertEqual(self.bot.state['cycle']['started_at_utc'],start)
        self.assertEqual(self.bot.state['phase'],'MANUAL'); self.assertEqual(len(self.notices()),1)
        self.assertEqual(self.api.orders,[]); self.assertEqual(len(self.api.sends),sends+2)
        self.assertEqual(len(self.bot.state['cycle']['manual_observation']['orders']),0)

    def test_limit_with_open_position_preserves_protection_and_recovery(self):
        self.position(); before=deepcopy(self.api.positions); recovery=self.bot.state['cycle']['recovery']
        count=len(self.api.sends); self.advance(86400); self.bot.tick()
        self.assertEqual(self.api.positions,before); self.assertEqual(len(self.api.sends),count)
        self.assertEqual(self.bot.state['cycle']['recovery'],recovery)
        self.assertEqual(self.bot.state['phase'],'MANUAL')

    def test_fill_racing_expiry_preserves_position_and_cancels_remaining_pending(self):
        self.start_pair(); self.advance(86400)
        with patch.object(self.bot,'dispatch'): self.bot.tick()
        count=len(self.api.sends); self.api.fill(); before=deepcopy(self.api.positions); self.bot.tick()
        self.assertEqual(self.bot.state['phase'],'MANUAL'); self.assertEqual(len(self.api.orders),0)
        self.assertEqual(self.api.positions,before)
        self.assertEqual(len(self.api.sends),count+1); self.assertIsNotNone(self.bot.state['cycle']['position'])
        self.assertEqual(len(self.bot.state['cycle']['manual_observation']['positions']),1)

    def test_natural_tp_after_deadline_books_once_but_does_not_restart(self):
        self.position(); self.advance(86400); self.bot.tick(); count=len(self.api.sends)
        deal=self.api.close_position(); self.bot.tick()
        self.assertEqual(self.bot.state['phase'],'MANUAL'); self.assertIsNone(self.bot.state['cycle']['position'])
        self.assertEqual(D(self.bot.state['cycle']['realized']),D('1.8'))
        self.assertEqual(D(self.bot.state['cycle']['recovery']),D(0))
        self.assertEqual(self.bot.state['seen_deals'],[str(deal.ticket)])
        self.assertEqual(self.bot.state['completed'],[])
        self.restart(); self.advance(120); self.bot.tick(); self.bot.dispatch()
        self.assertEqual(len(self.api.sends),count); self.assertEqual(len(self.ledger()),1)
        self.assertEqual(self.bot.state['phase'],'MANUAL')
        with self.assertRaises(ValueError): self.bot.command('/start')

    def test_natural_sl_in_manual_keeps_recovery_without_new_pair(self):
        self.position(); self.advance(86400); self.bot.tick()
        self.api.close_position(reason=4); count=len(self.api.sends); self.bot.tick()
        self.assertEqual(D(self.bot.state['cycle']['recovery']),D('19.8'))
        self.assertEqual(self.bot.state['phase'],'MANUAL'); self.assertEqual(len(self.api.sends),count)

    def test_slow_order_check_crossing_deadline_cannot_send(self):
        self.bot.command('/start'); self.api.rates[0]['time']+=60
        # Persist intents without dispatch, then reach the last second of the cycle.
        with patch.object(self.bot,'dispatch'): self.bot.tick()
        self.advance(86399)
        original=self.api.order_check
        def slow(request):
            result=original(request); self.advance(2); return result
        with patch.object(self.api,'order_check',side_effect=slow): self.bot.dispatch()
        self.assertFalse(self.api.sends); self.assertEqual(self.bot.state['phase'],'MANUAL')
        self.assertEqual(len(self.notices()),1)

    def test_ready_cancel_is_sent_but_modify_is_not_sent_after_limit(self):
        self.start_pair(); self.api.fill()
        with patch.object(self.bot,'dispatch'): self.bot.tick()
        # Existing broker SL/TP are deliberately left intact.
        p=self.bot.state['cycle']['position']
        self.bot._intent('MODIFY',position_id=p['id'],sl='4001',tp='4003.2',purpose='ANCHOR')
        self.bot.save('queued_modify'); count=len(self.api.sends)
        self.advance(86400); self.restart(); self.bot.tick(); self.bot.dispatch()
        self.assertEqual(len(self.api.sends),count+1); self.assertEqual(len(self.api.orders),0)
        self.assertTrue(any(c['kind']=='MODIFY' and c['status']=='READY' for c in self.bot.state['commands'].values()))

    def test_restart_uses_original_deadline(self):
        self.start_pair(); deadline=self.bot.state['cycle']['deadline_utc']
        self.advance(86399); self.restart(); self.bot.tick()
        self.assertEqual(self.bot.state['phase'],'PAIR'); self.assertEqual(self.bot.state['cycle']['deadline_utc'],deadline)
        self.advance(1); self.bot.tick(); self.assertEqual(self.bot.state['phase'],'MANUAL')

    def test_backwards_wall_clock_cannot_extend_cycle_in_process_or_restart(self):
        self.start_pair(); self.advance(100); self.bot.tick()
        self.api.now-=10; self.api.tick=self.api.tick._replace(time_msc=self.api.now*1000)
        self.restart()
        self.assertEqual(self.bot.state['phase'],'MANUAL')
        self.assertEqual(self.bot.state['cycle']['manual_reason'],'UTC_CLOCK_MOVED_BACKWARDS')

    def test_monotonic_crossing_is_fail_closed_even_if_wall_clock_freezes(self):
        self.start_pair(); self.mono+=86400; self.bot.tick()
        self.assertEqual(self.bot.state['phase'],'MANUAL'); self.assertEqual(len(self.api.sends),4)
        self.assertEqual(self.api.orders,[])

    def test_missing_saved_deadline_never_starts_new_24h_window(self):
        self.start_pair(); self.bot.state['cycle'].pop('started_at_utc'); self.bot.save('legacy_time_missing')
        self.restart(); self.assertEqual(self.bot.state['phase'],'MANUAL'); self.assertEqual(len(self.notices()),1)

    def test_manual_latch_and_notification_roll_back_together_on_failure(self):
        self.start_pair(); self.advance(86400)
        def crash(event):
            if event=='cycle_manual_required': raise RuntimeError('power loss')
        self.store.fault=crash
        with self.assertRaises(DurabilityError): self.bot.tick()
        self.assertNotEqual(self.store.load()['phase'],'MANUAL'); self.assertFalse(self.notices())
        self.restart(); self.assertEqual(self.bot.state['phase'],'MANUAL'); self.assertEqual(len(self.notices()),1)
        self.assertEqual(len(self.api.sends),2)


    def test_nonfinite_clocks_fail_closed_before_dispatch(self):
        self.start_pair(); count=len(self.api.sends)
        self.bot.monotonic=lambda:float('nan'); self.bot.dispatch()
        self.assertEqual(self.bot.state['phase'],'MANUAL')
        self.assertEqual(self.bot.state['cycle']['manual_reason'],'INVALID_CYCLE_CLOCK')
        self.assertEqual(len(self.api.sends),count); self.assertEqual(len(self.notices()),1)

    def test_nonfinite_utc_can_persist_manual_latch(self):
        self.start_pair(); self.bot.clock=lambda:float('inf'); self.bot.dispatch()
        self.assertEqual(self.store.load()['phase'],'MANUAL')
        self.assertIsNone(self.store.load()['cycle']['manual_since_utc'])
        self.assertEqual(len(self.api.sends),2)

    def test_manual_readonly_reconciles_with_stale_market_tick(self):
        self.position(); self.api.now+=86400; self.mono+=86400
        self.bot.tick()
        self.assertEqual(self.bot.state['phase'],'MANUAL')
        self.assertIn('manual_observation',self.bot.state['cycle'])
        count=len(self.api.sends); self.api.close_position(); self.bot.tick()
        self.assertEqual(D(self.bot.state['cycle']['realized']),D('1.8'))
        self.assertEqual(len(self.api.sends),count)

    def test_manual_evidence_conflict_does_not_clear_latch_or_spam(self):
        self.position(); self.advance(86400); self.bot.tick()
        self.api.deals[0]=self.api.deals[0]._replace(price=4100.)
        count=len(self.api.sends)
        for _ in range(3): self.bot.tick()
        self.assertEqual(self.store.load()['phase'],'MANUAL')
        self.assertEqual(len(self.notices()),1); self.assertEqual(len(self.api.sends),count)
        self.assertIn('reconciliation_error',self.bot.state['cycle'])

    def test_slow_profit_read_crossing_limit_never_exits_manual(self):
        self.start_pair(); self.advance(86399); self.api.fill()
        original=self.api.order_calc_profit
        def slow(*args):
            value=original(*args); self.advance(2); return value
        with patch.object(self.api,'order_calc_profit',side_effect=slow): self.bot.tick()
        self.assertEqual(self.bot.state['phase'],'MANUAL')
        # A cancellation sent before the deadline is allowed; nothing is sent after it.
        count=len(self.api.sends); self.bot.tick(); self.bot.dispatch()
        self.assertEqual(len(self.api.sends),count); self.assertEqual(len(self.notices()),1)

if __name__=='__main__': unittest.main()
