import tempfile
import unittest
from recovery.native_executor_session import NativeExecutorSession
from recovery.native_preflight import TerminalBinding
from test_recovery_native_transport import NativeSendFake
from test_recovery_mt5_transport import REQUEST,EXPECTED

class NativeSessionTests(unittest.TestCase):
    def make(self,a,path,account=1):
        return NativeExecutorSession(a,TerminalBinding(account,'demo','XAUUSD.s',frozenset({123})),database=path)
    def test_startup_is_read_only_and_uncertainty_blocks_next_action_and_restart(self):
        with tempfile.TemporaryDirectory() as d:
            a=NativeSendFake();a.lose=True;path=d+'/state.db'
            with self.make(a,path) as session:
                self.assertTrue(session.ready);self.assertEqual(a.sent,[])
                self.assertEqual(session.execute('one',REQUEST,EXPECTED)['state'],'UNKNOWN')
                self.assertFalse(session.ready)
                with self.assertRaises(RuntimeError):session.execute('two',REQUEST,EXPECTED)
            with self.make(a,path) as session:
                self.assertFalse(session.ready)
                with self.assertRaises(RuntimeError):session.execute('one',REQUEST,EXPECTED)
            self.assertEqual(len(a.sent),1)
    def test_competing_owner_cannot_steal_and_release_allows_new_owner(self):
        with tempfile.TemporaryDirectory() as d:
            a=NativeSendFake();path=d+'/state.db'
            with self.make(a,path) as first:
                with self.assertRaises(RuntimeError):
                    with self.make(a,path):pass
                self.assertTrue(first.ready)
            with self.make(a,path) as second:self.assertTrue(second.ready)
    def test_scope_cannot_change_for_same_database(self):
        with tempfile.TemporaryDirectory() as d:
            a=NativeSendFake();path=d+'/state.db'
            with self.make(a,path):pass
            a.login=2
            with self.assertRaisesRegex(ValueError,'another native runtime binding'):
                with self.make(a,path,account=2):pass
            self.assertEqual(a.sent,[])
    def test_closed_session_and_nondurable_database_rejected(self):
        a=NativeSendFake()
        with self.assertRaises(ValueError):self.make(a,':memory:')
        with tempfile.TemporaryDirectory() as d:
            session=self.make(a,d+'/state.db')
            with session:pass
            with self.assertRaises(RuntimeError):session.execute('one',REQUEST,EXPECTED)
            with self.assertRaises(RuntimeError):
                with session:pass

    def test_persistence_failure_after_send_blocks_new_key(self):
        from unittest.mock import patch
        import sqlite3
        with tempfile.TemporaryDirectory() as d:
            a=NativeSendFake()
            with self.make(a,d+'/state.db') as session:
                with patch.object(session.executor,'_update',side_effect=sqlite3.OperationalError('disk')):
                    with self.assertRaises(sqlite3.OperationalError):session.execute('one',REQUEST,EXPECTED)
                self.assertFalse(session.ready)
                self.assertEqual(session.executor.read('one')['state'],'SENT')
                with self.assertRaises(RuntimeError):session.execute('two',REQUEST,EXPECTED)
                self.assertEqual(len(a.sent),1)

    def test_baseexceptions_during_send_leave_session_disabled(self):
        for error in (KeyboardInterrupt,SystemExit):
            with self.subTest(error=error),tempfile.TemporaryDirectory() as d:
                a=NativeSendFake()
                def interrupt(request):
                    a.sent.append(request);raise error()
                a.order_send=interrupt
                with self.make(a,d+'/state.db') as session:
                    with self.assertRaises(error):session.execute('one',REQUEST,EXPECTED)
                    self.assertFalse(session.ready)
                    self.assertEqual(session.executor.read('one')['state'],'SENT')
                    with self.assertRaises(RuntimeError):session.execute('two',REQUEST,EXPECTED)
                    self.assertEqual(len(a.sent),1)
