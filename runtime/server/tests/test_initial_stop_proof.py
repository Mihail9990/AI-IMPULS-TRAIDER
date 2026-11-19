import tempfile
import unittest
from copy import deepcopy
from recovery.initial_stop_proof import InitialStopProof
from recovery.native_entry_observer import NativeEntryObserver
from recovery.native_recovery_control import NativeRecoveryControl
from test_native_entry_protection import ProtectFake
import test_native_recovery_control as controls
from test_native_recovery_control import RANGES


class InitialProofTests(unittest.TestCase):
    session = controls.ControlTests.session

    def setup_proof(self, api, path):
        s=self.session(api,path);s.__enter__();self.addCleanup(s.close)
        c=NativeRecoveryControl(s,magic=123);c.stage_initial_ranges(RANGES);c.submit_initial('1')
        api.fill(.01,price=3999.95);api.ask=4000.21
        observed=NativeEntryObserver(c).observe('1')
        data=c._read();item=data['orders'][0];key=item['key']+':protection:0'
        b=s.transport.binding
        expected=dict(account=b.account,server=b.server,symbol=b.symbol,magic=123,identifier=88,initial_stop_key=key)
        req=dict(action='TRADE_ACTION_SLTP',symbol=b.symbol,position=77,sl='3999.25',tp='0')
        data['protection_commands']={key:dict(key=key,range_id='1',position_identifier=88,stage=0,request=req,scope=expected,phase='dispatching')}
        c._save(data)
        s.transport.validator.initial_stop_guard=InitialStopProof(c).validate
        return s,c,req,expected,observed

    def test_exact_first_actual_fill_widen_allowed(self):
        with tempfile.TemporaryDirectory() as d:
            a=ProtectFake();s,c,r,e,_=self.setup_proof(a,d+'/x')
            result=s.transport.validator.validate(r,e)
            self.assertEqual(result['sl'],3999.25)
            self.assertEqual(len(a.sent),1)

    def test_scope_alone_and_wrong_target_cannot_authorize(self):
        with tempfile.TemporaryDirectory() as d:
            a=ProtectFake();s,c,r,e,_=self.setup_proof(a,d+'/x')
            for req,exp in [(dict(r,sl='3999.24'),e),(r,dict(e,initial_stop_key='invented'))]:
                with self.assertRaises(ValueError):s.transport.validator.validate(req,exp)
            s.transport.validator.initial_stop_guard=None
            with self.assertRaises(ValueError):s.transport.validator.validate(r,e)

    def test_external_or_confirmed_stop_and_changed_history_reject(self):
        for case in ('external','confirmed','deal','exits'):
            with self.subTest(case=case),tempfile.TemporaryDirectory() as d:
                a=ProtectFake();s,c,r,e,_=self.setup_proof(a,d+'/x')
                if case=='external':a.positions[0].sl=3999.4
                if case=='confirmed':
                    data=c._read();data['confirmed_protection']={'88':{'stage':0,'sl':'3999.3'}};c._save(data)
                if case=='deal':a.deals[0].time_msc+=1
                if case=='exits':
                    data=c._read();data['entry_observations']['51']['phase']='partial_remainder_pending';c._save(data)
                with self.assertRaises(ValueError):s.transport.validator.validate(r,e)
                self.assertEqual(len(a.sent),1)
