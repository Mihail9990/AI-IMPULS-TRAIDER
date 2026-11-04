"""Offline command idempotency, stale previews, owner confinement, durability."""
import threading
import unittest
from unittest.mock import patch
import test_tradfi_evidence_controller as fixtures
from tradfi.control import ActionGateway, OwnerMailbox, Conflict
from tradfi.storage import DurabilityError


class ControlTest(unittest.TestCase):
    setUp = fixtures.EvidenceControllerTest.setUp
    tearDown = fixtures.EvidenceControllerTest.tearDown
    restart = fixtures.EvidenceControllerTest.restart

    def request(self, name='request_0001', revision=0):
        return dict(id=name, revision=revision, action=dict(type='command', text='/status'))

    def test_duplicate_after_restart_never_reexecutes(self):
        calls=[]; gate=ActionGateway(self.bot, execute=lambda a:calls.append(a))
        first=gate.apply(self.request()); self.assertEqual(first['status'],'APPLIED')
        self.restart(); gate=ActionGateway(self.bot, execute=lambda a:calls.append(a))
        self.assertEqual(gate.apply(self.request()),first); self.assertEqual(len(calls),1)

    def test_stale_preview_and_conflicting_id_are_rejected(self):
        gate=ActionGateway(self.bot); gate.apply(self.request())
        with self.assertRaises(Conflict): gate.apply(self.request('request_0002'))
        conflicting=self.request(); conflicting['action']['text']='/stop'
        with self.assertRaises(Conflict): gate.apply(conflicting)
        self.assertFalse(self.bot.state['stop'])

    def test_crash_after_execution_is_unknown_without_replay(self):
        calls=[]
        def crash(event):
            if event=='control_action_completed': raise RuntimeError('power loss')
        self.store.fault=crash
        with self.assertRaises(DurabilityError): ActionGateway(self.bot,lambda a:calls.append(a)).apply(self.request())
        self.restart()
        result=ActionGateway(self.bot,lambda a:calls.append(a)).apply(self.request())
        self.assertEqual(result['status'],'UNKNOWN'); self.assertEqual(len(calls),1)

    def test_transport_error_retains_unknown_without_replay(self):
        calls=[]
        def uncertain(action):
            calls.append(action); raise TimeoutError('reply unavailable')
        gate=ActionGateway(self.bot,uncertain)
        self.assertEqual(gate.apply(self.request())['status'],'UNKNOWN')
        self.assertEqual(gate.apply(self.request())['status'],'UNKNOWN')
        self.assertEqual(len(calls),1)

    def test_generic_value_error_after_effect_remains_unknown(self):
        def uncertain(action):
            self.bot.command('/stop'); raise ValueError('late validation')
        result=ActionGateway(self.bot,uncertain).apply(self.request())
        self.assertEqual(result['status'],'UNKNOWN')
        self.assertTrue(self.store.load()['stop'])

    def test_mailbox_returns_conflict_instead_of_pending_forever(self):
        gate=ActionGateway(self.bot); gate.apply(self.request())
        box=OwnerMailbox(); box.submit(self.request('request_0002',0))
        box.drain(gate,lambda:{'revision':self.bot.state['control_revision']})
        self.assertEqual(box.view()['action_results']['request_0002']['status'],'CONFLICT')

    def test_failed_reservation_cannot_execute(self):
        calls=[]
        def crash(event):
            if event=='control_action_reserved': raise RuntimeError('power loss')
        self.store.fault=crash
        with self.assertRaises(DurabilityError): ActionGateway(self.bot,lambda a:calls.append(a)).apply(self.request())
        self.assertFalse(calls)

    def test_worker_cannot_touch_controller_and_mailbox_copies_inputs(self):
        calls=[]; gate=ActionGateway(self.bot,lambda a:calls.append(a)); errors=[]
        def worker():
            try: gate.apply(self.request())
            except RuntimeError: errors.append(True)
        thread=threading.Thread(target=worker); thread.start(); thread.join(1)
        self.assertEqual(errors,[True]); self.assertFalse(calls)
        box=OwnerMailbox(); request=self.request(); box.submit(request); request['action']['text']='/stop'
        box.drain(gate,lambda:{'revision':self.bot.state['control_revision']})
        self.assertEqual(calls[0]['text'],'/status')
        view=box.view(); view['revision']=900
        self.assertEqual(box.view()['revision'],1)

    def test_empty_mailbox_worker_cannot_read_controller_view(self):
        box=OwnerMailbox(); gate=ActionGateway(self.bot); calls=[]; errors=[]
        def worker():
            try: box.drain(gate,lambda:calls.append(self.bot.state))
            except RuntimeError: errors.append(True)
        thread=threading.Thread(target=worker); thread.start(); thread.join(1)
        self.assertEqual(errors,[True]); self.assertFalse(calls)

    def test_time_guard_is_not_bypassed_by_gateway(self):
        self.bot.state['time_block']={'reason':'EXTERNAL_UTC_UNVERIFIED'}; self.bot.save('test_block')
        req=self.request(); req['action']['text']='/start'
        result=ActionGateway(self.bot).apply(req)
        self.assertEqual(result['status'],'REJECTED'); self.assertFalse(self.api.sends)
        self.assertIn('time_block',self.store.load())
