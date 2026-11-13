"""Injected APIs only: pre-send M1 wait proof and exact-payload resume."""
from copy import deepcopy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import test_tradfi_mt5 as fixtures
from tradfi.mt5_adapter import EntryDataWait, MT5Broker, NotSent
from tradfi.evidence_broker import MT5EvidenceBroker
from tradfi.storage import DurabilityError


class BrokerWaitTest(unittest.TestCase):
    setUp=fixtures.MT5Test.setUp
    make_broker=fixtures.MT5Test.make_broker
    command=fixtures.MT5Test.command
    submit=fixtures.MT5Test.submit

    def test_wait_at_each_request_stage_then_exact_resume_once(self):
        for stage in (1,2,3):
            with self.subTest(stage=stage):
                self.setUp()
                cmd=self.command(id=('%024x'%stage))
                original=self.broker._request;calls=[]
                def request(command):
                    calls.append(True)
                    if len(calls)==stage:raise EntryDataWait('transition')
                    return original(command)
                with patch.object(self.broker,'_request',side_effect=request):self.submit(cmd)
                self.assertTrue(self.broker.entry_deferred(cmd['id']))
                self.assertFalse(self.api.sends)
                self.assertEqual(self.broker.rows[cmd['id']]['command'],cmd)
                self.broker.submit(cmd);self.broker.submit(cmd)
                self.assertEqual(len(self.api.sends),1)
                self.assertEqual(self.api.sends[0]['price'],4003.)
                self.api.sends.clear()
    def test_wait_during_order_check(self):
        original=self.broker._request;waiting=False
        def check(request):
            nonlocal waiting
            waiting=True
            return self.api.check
        def request(command):
            if waiting:raise EntryDataWait('transition')
            return original(command)
        with patch.object(self.api,'order_check',side_effect=check),patch.object(self.broker,'_request',side_effect=request):
            cmd=self.submit()
        self.assertTrue(self.broker.entry_deferred(cmd['id']));self.assertFalse(self.api.sends)
    def test_resume_revalidates_no_reprice(self):
        with patch.object(self.broker,'_request',side_effect=EntryDataWait('transition')):cmd=self.submit()
        self.api.tick=self.api.tick._replace(ask=4004.)
        self.broker.submit(cmd)
        self.assertEqual(self.broker.result(cmd['id'])['status'],'REJECTED')
        self.assertFalse(self.api.sends)
    def test_changed_payload_refused(self):
        with patch.object(self.broker,'_request',side_effect=EntryDataWait('transition')):cmd=self.submit()
        with self.assertRaises(DurabilityError):self.broker.submit(dict(cmd,price='4005'))
        self.assertFalse(self.api.sends)
    def test_unknown_never_resumed_even_after_restart(self):
        self.api.response=None
        cmd=self.submit();self.assertEqual(self.broker.result(cmd['id'])['status'],'UNKNOWN')
        self.broker.submit(cmd)
        restarted=self.make_broker();self.addCleanup(restarted.close);restarted.submit(cmd)
        self.assertFalse(restarted.entry_deferred(cmd['id']));self.assertEqual(len(self.api.sends),1)
    def test_failed_final_no_send_proof_stays_unknown(self):
        original=self.broker._request;calls=[];save=self.broker._save_row
        def request(command):
            calls.append(True)
            if len(calls)==3:raise EntryDataWait('transition')
            return original(command)
        def fail(cid,row,event):
            if event=='entry_waiting_data_not_sent':raise DurabilityError('disk failure')
            return save(cid,row,event)
        with patch.object(self.broker,'_request',side_effect=request),patch.object(self.broker,'_save_row',side_effect=fail):
            with self.assertRaises(DurabilityError):self.submit()
        self.assertEqual(self.broker.result('a'*24)['status'],'UNKNOWN')
        self.assertFalse(self.broker.entry_deferred('a'*24));self.assertFalse(self.api.sends)
        self.broker.submit(self.command());self.assertFalse(self.api.sends)
    def test_proof_excludes_send_evidence(self):
        with patch.object(self.broker,'_request',side_effect=EntryDataWait('transition')):cmd=self.submit()
        for key,value in [('raw_result',None),('last_error',[1,'ok'])]:
            self.broker.rows[cmd['id']][key]=value
            self.assertFalse(self.broker.entry_deferred(cmd['id']))
            del self.broker.rows[cmd['id']][key]
        self.broker.rows[cmd['id']]['send_started']=True
        self.assertFalse(self.broker.entry_deferred(cmd['id']))
    def test_restart_can_load_ready_only_with_positive_wait_proof(self):
        with patch.object(self.broker,'_request',side_effect=EntryDataWait('transition')):cmd=self.submit()
        self.bot.state['commands'][cmd['id']]['status']='READY'
        self.bot.save('test_restore_ready')
        restarted=self.make_broker();self.addCleanup(restarted.close)
        self.assertTrue(restarted.entry_deferred(cmd['id']))
        # Broker alone never initiates a replay during construction.
        self.assertFalse(self.api.sends)
        restarted.rows[cmd['id']]['result']={'status':'UNKNOWN'}
        restarted._save_row(cmd['id'],restarted.rows[cmd['id']],'test_uncertain')
        with self.assertRaises(DurabilityError):self.make_broker()
    def test_evidence_guard_and_quote_wait_only_for_entry(self):
        # Unbound requests use injected guards, never instantiate a live API.
        broker=object.__new__(MT5EvidenceBroker)
        broker.before_mutation=lambda kind:False
        broker.entry_waiting=lambda:True
        with self.assertRaises(EntryDataWait):broker._request({'kind':'STOP'})
        with self.assertRaises(OSError):broker._request({'kind':'MODIFY'})
        broker._timestamp_semantics_verified=lambda:False
        broker.time_safety=SimpleNamespace(semantics_verifier=SimpleNamespace(reason='MT5_UTC_M1_TRANSITION_WAIT'))
        broker._strict_quote=True;broker._entry_request=True
        with self.assertRaises(EntryDataWait):broker._check_quote_time(None,'request')
        broker.time_safety.semantics_verifier.reason='MT5_UTC_INVALID_TIMESTAMP'
        with self.assertRaises(NotSent) as failure:broker._check_quote_time(None,'request')
        self.assertNotIsInstance(failure.exception,EntryDataWait)
        broker.time_safety.semantics_verifier.reason='MT5_UTC_M1_TRANSITION_WAIT'
        broker.entry_waiting=lambda:False
        with self.assertRaises(NotSent) as failure:broker._check_quote_time(None,'request')
        self.assertNotIsInstance(failure.exception,EntryDataWait)
        broker.entry_waiting=lambda:True;broker._entry_request=False
        with self.assertRaises(NotSent) as failure:broker._check_quote_time(None,'request')
        self.assertNotIsInstance(failure.exception,EntryDataWait)

if __name__=='__main__':unittest.main()
