"""Offline durable acceptance and no-replay checks; no broker imports or calls."""
from copy import deepcopy
from types import SimpleNamespace
import threading
import unittest
from tradfi.control import ActionGateway, ActionRejected, TransientDataWait, Conflict, OwnerMailbox


class ControllerDouble:
    def __init__(self):
        self.state = dict(control_revision=0, account='account-a', cycle={'id':'cycle-a'}, basket_settings={'size':'1'})
        self.waiting = True
        self.calls = []
        self.saved = []
        self.utc = 120.1
        self.clock = lambda: self.utc
        self.broker = SimpleNamespace(time_safety=SimpleNamespace(trusted_utc=lambda:self.utc))
    def _time_guard(self):
        return self.waiting or bool(self.state.get('time_block'))
    def _time_waiting(self):
        return self.waiting and not self.state.get('time_block')
    def save(self, event):
        self.saved.append((event, deepcopy(self.state)))
    def execute(self, action):
        self.calls.append((deepcopy(action), self._deferred_action_accepted_utc))


class DeferredControlTest(unittest.TestCase):
    def setUp(self):
        self.bot = ControllerDouble()
        self.gate = ActionGateway(self.bot, self.bot.execute)
    def request(self, suffix='0001', action=None):
        return dict(id='request_'+suffix, revision=self.bot.state['control_revision'], action=action or {'type':'v3_entry','cycle_id':'cycle-a','timing':'current'})
    def queue(self):
        request = self.request()
        self.assertEqual(self.gate.apply(request)['status'], 'WAITING_DATA')
        return request
    def test_accept_duplicate_and_release_once_with_original_time(self):
        request = self.queue()
        self.assertEqual(self.gate.apply(request)['status'],'WAITING_DATA')
        self.assertFalse(self.bot.calls)
        self.bot.waiting = False; self.bot.utc = 122
        self.gate.flush_waiting(); self.gate.flush_waiting()
        self.assertEqual(len(self.bot.calls),1)
        self.assertEqual(self.bot.calls[0][1],120.1)
        self.assertEqual(self.gate.apply(request)['status'],'APPLIED')
        reserved = [s for e,s in self.bot.saved if e=='control_action_reserved'][-1]
        self.assertEqual(reserved['control_receipts'][request['id']]['status'],'UNKNOWN')
        self.assertIsNone(self.bot._deferred_action_accepted_utc)
    def test_restart_cancels_wait_but_never_replays_unknown(self):
        request = self.queue()
        self.bot.state['control_receipts']['other']={'status':'UNKNOWN'}
        gate = ActionGateway(self.bot,self.bot.execute)
        self.bot.waiting = False; gate.flush_waiting()
        self.assertEqual(gate.apply(request)['status'],'REJECTED')
        self.assertEqual(self.bot.state['control_receipts']['other']['status'],'UNKNOWN')
        self.assertFalse(self.bot.calls)
    def test_hard_block_and_context_changes_reject(self):
        for change in ('time_block','account','basket_settings','cycle'):
            with self.subTest(change=change):
                self.setUp(); request=self.queue(); self.bot.waiting=False
                self.bot.state[change]={'reason':'hard'} if change=='time_block' else {'changed':True}
                self.gate.flush_waiting()
                self.assertEqual(self.gate.apply(request)['status'],'REJECTED')
                self.assertFalse(self.bot.calls)
    def test_stop_cancels_deferred_before_release(self):
        request=self.queue()
        self.gate.apply(self.request('0002',{'type':'command','text':'/stop'}))
        self.bot.waiting=False; self.gate.flush_waiting()
        self.assertEqual(self.gate.apply(request)['status'],'REJECTED')
        self.assertEqual([a['text'] for a,_ in self.bot.calls],['/stop'])
    def test_cancel_queued_only_is_applied_even_without_broker_group(self):
        request=self.queue()
        self.gate.execute=lambda a: (_ for _ in ()).throw(ActionRejected('No group yet'))
        outcome=self.gate.apply(self.request('0002',{'type':'v3_cancel_request','cycle_id':'cycle-a'}))
        self.assertEqual(outcome['status'],'APPLIED')
        self.assertEqual(self.gate.apply(request)['status'],'REJECTED')
    def test_wrong_cycle_cancel_does_not_invalidate(self):
        request=self.queue()
        self.gate.apply(self.request('0002',{'type':'v3_cancel_entry','cycle_id':'other'}))
        self.assertEqual(self.gate.apply(request)['status'],'WAITING_DATA')
    def test_unknown_execution_never_replays(self):
        request=self.queue()
        def unknown(a):
            self.bot.calls.append(a); raise TimeoutError()
        self.gate.execute=unknown; self.bot.waiting=False
        self.gate.flush_waiting(); self.gate.flush_waiting()
        self.assertEqual(self.gate.apply(request)['status'],'UNKNOWN')
        self.assertEqual(len(self.bot.calls),1)
    def test_mailbox_limit_does_not_release_before_pending_stop(self):
        request=self.queue(); self.bot.waiting=False
        box=OwnerMailbox()
        box.submit(self.request('0002',{'type':'command','text':'/status'}))
        stop=self.request('0003',{'type':'command','text':'/stop'});stop['revision']+=1;box.submit(stop)
        box.drain(self.gate,lambda:{},limit=1)
        self.assertEqual(self.gate.apply(request)['status'],'WAITING_DATA')
        box.drain(self.gate,lambda:{},limit=1)
        self.assertEqual(self.gate.apply(request)['status'],'REJECTED')
    def test_admission_race_redefers_with_pre_effect_timestamp(self):
        self.bot.waiting=False
        def race(action):
            self.bot.waiting=True;self.bot.utc=121.5
            raise TransientDataWait('M1 boundary')
        self.gate.execute=race;request=self.request()
        result=self.gate.apply(request)
        self.assertEqual(result['status'],'WAITING_DATA')
        self.assertEqual(result['deferred']['accepted_utc'],120.1)
        self.assertFalse(self.bot.calls)
        self.gate.execute=self.bot.execute;self.bot.waiting=False
        self.gate.flush_waiting()
        self.assertEqual(self.bot.calls[0][1],120.1)
    def test_flush_race_preserves_original_acceptance_context(self):
        request=self.queue();original=deepcopy(self.bot.state['control_receipts'][request['id']]['deferred'])
        def race(action):
            self.bot.waiting=True;self.bot.utc=122
            raise TransientDataWait('M1 boundary')
        self.bot.waiting=False;self.gate.execute=race;self.gate.flush_waiting()
        result=self.gate.apply(request)
        self.assertEqual(result['status'],'WAITING_DATA')
        self.assertEqual(result['deferred'],original)
        self.assertFalse(self.bot.calls)
    def test_race_rejects_changed_context_or_hard_block(self):
        for changed in ('account','time_block','phase'):
            with self.subTest(changed=changed):
                self.setUp();self.bot.waiting=False
                def race(action):
                    self.bot.waiting=True
                    self.bot.state[changed]='SAFE_PAUSE' if changed=='phase' else {'changed':True}
                    raise TransientDataWait('M1 boundary')
                self.gate.execute=race
                self.assertEqual(self.gate.apply(self.request())['status'],'REJECTED')
                self.assertFalse(self.bot.calls)
    def test_cancel_initial_does_not_cancel_reset_manual(self):
        reset=self.request(action={'type':'command','text':'/reset_manual'})
        self.gate.apply(reset)
        start=self.request('0002',{'type':'command','text':'/start auto'})
        self.gate.apply(start)
        self.gate.apply(self.request('0003',{'type':'cancel_initial','cycle_id':'cycle-a','expected_orders':[]}))
        self.assertEqual(self.gate.apply(reset)['status'],'WAITING_DATA')
        self.assertEqual(self.gate.apply(start)['status'],'REJECTED')
    def test_reservation_failure_never_executes(self):
        self.queue(); self.bot.waiting=False
        def fail(event):
            if event=='control_action_reserved':raise RuntimeError('disk failure')
        self.bot.save=fail
        with self.assertRaises(RuntimeError):self.gate.flush_waiting()
        self.assertFalse(self.bot.calls)
        self.gate.flush_waiting()
        self.assertFalse(self.bot.calls)
    def test_new_hard_guard_prevents_release(self):
        request=self.queue();self.bot.waiting=False;self.bot.state['phase']='SAFE_PAUSE'
        self.gate.flush_waiting()
        self.assertEqual(self.gate.apply(request)['status'],'REJECTED')
        self.assertFalse(self.bot.calls)
    def test_ui_wait_tracking_survives_reload_and_unknown_locks(self):
        import shutil, subprocess
        from pathlib import Path
        if not shutil.which('node'):self.skipTest('node unavailable')
        js=(Path(__file__).resolve().parents[1]/'web/basket-panel/panel.js').read_text()
        tracking=js[js.index(' let waitingRequests={}'):js.index(' async function send(')]
        script="""
const assert=require('assert'),vm=require('vm');
const data={};const storage={getItem:k=>data[k]??null,setItem:(k,v)=>data[k]=v,removeItem:k=>delete data[k]};
function context(){return vm.createContext({localStorage:storage,storageKey:x=>x,pending:null,pendingScope:null,view:{scope:'scope-a',action_results:{}},lastActionReceipt:null});}
const first=context();vm.runInContext(TRACKING,first);first.pending='request_0001';first.pendingScope='scope-a';vm.runInContext('rememberWaiting();clearPending()',first);
assert.equal(first.pending,null);assert.ok(data['waiting-requests'].includes('request_0001'));
const reloaded=context();vm.runInContext(TRACKING,reloaded);reloaded.view.action_results.request_0001={status:'UNKNOWN'};vm.runInContext('reconcileWaiting()',reloaded);
assert.equal(reloaded.pending,'request_0001');assert.equal(data.request,'request_0001');assert.equal(reloaded.lastActionReceipt.status,'UNKNOWN');
""".replace('TRACKING',__import__('json').dumps(tracking))
        subprocess.run(['node','-e',script],check=True,capture_output=True,text=True)
    def test_worker_flush_forbidden(self):
        self.queue(); errors=[]
        def worker():
            try:self.gate.flush_waiting()
            except RuntimeError:errors.append(True)
        t=threading.Thread(target=worker);t.start();t.join()
        self.assertEqual(errors,[True]);self.assertFalse(self.bot.calls)
    def test_conflicting_duplicate_is_rejected(self):
        request=self.queue();request['action']['timing']='next'
        with self.assertRaises(Conflict):self.gate.apply(request)

if __name__=='__main__':unittest.main()
