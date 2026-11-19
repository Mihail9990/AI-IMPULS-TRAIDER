import uvicorn
import tempfile
import threading
import unittest
from types import SimpleNamespace
from fastapi.testclient import TestClient
from recovery.native_runtime import NativeRuntime, NativeScheduler
from recovery.native_preflight import TerminalBinding
from test_native_support_cycle import CycleFake


class RuntimeTests(unittest.TestCase):
    def test_lifespan_idle_no_send_and_shutdown(self):
        with tempfile.TemporaryDirectory() as d:
            api = CycleFake(d)
            # Match the fixture account binding, not a native terminal.
            a = api.account_info()
            binding = TerminalBinding(a.login, a.server, 'XAUUSD.s', frozenset({123}))
            r = NativeRuntime(api, binding, database=d+'/s.db', magic=123,
                              token='x'*32, interval=0.05)
            app = r.open()
            self.assertIsNone(r.scheduler._thread)
            with TestClient(app) as client:
                result = client.get('/api/recovery/state', headers={'Authorization':'Bearer '+'x'*32})
                self.assertEqual(result.status_code, 200)
                self.assertTrue(r.scheduler._thread.is_alive())
                self.assertEqual(api.sent, [])
            self.assertTrue(r.session.closed)
            self.assertFalse(r.scheduler._thread.is_alive())
            r.close()
            with self.assertRaises(RuntimeError): r.open()

    def test_exception_stops_without_retry(self):
        called = threading.Event()
        class Broken:
            session = SimpleNamespace(_lock=threading.RLock(), ready=True)
            def tick(self):
                called.set()
                raise OSError('Do not expose terminal contents')
        c = Broken()
        s = NativeScheduler(c, interval=0.05)
        s.start()
        self.assertTrue(called.wait(2))
        s.stop()
        self.assertFalse(c.session.ready)
        self.assertEqual(s.failure, 'OSError')
        self.assertNotIn('contents', str(c.last_tick))
        with self.assertRaises(RuntimeError): s.start()

    def test_intervals(self):
        for interval in [True, 0, -1, float('inf'), float('nan'), '1']:
            with self.assertRaises(ValueError): NativeScheduler(None, interval=interval)

    def test_nonwindows_cli_never_imports_terminal(self):
        from unittest.mock import patch
        import run_recovery_native
        with patch.object(run_recovery_native.sys, 'platform', 'linux'):
            result = run_recovery_native.main(['--run-native','--terminal','not-real',
                '--account','1','--server','test','--symbol','XAUUSD','--magic','123',
                '--database','not-real'])
        self.assertEqual(result, 2)

    def test_start_failure_closes_runtime(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as d:
            api = CycleFake(d)
            r = NativeRuntime(api, TerminalBinding(1, 'demo', 'XAUUSD.s', frozenset({123})),
                database=d+'/s.db', magic=123, token='x'*32)
            app = r.open()
            with patch('threading.Thread.start', side_effect=RuntimeError('no threads')):
                with self.assertRaises(RuntimeError):
                    with TestClient(app): pass
            # TestClient itself starts an AnyIO thread, so exercise lifespan
            # directly to ensure the failure originates inside scheduler.start.
            import asyncio
            async def check():
                with patch('threading.Thread.start', side_effect=RuntimeError('no threads')):
                    with self.assertRaises(RuntimeError):
                        async with app.router.lifespan_context(app): pass
            asyncio.run(check())
            self.assertTrue(r.session.closed)
            r.close()

    def test_stop_waits_for_inflight_tick(self):
        entered, release, stopped = threading.Event(), threading.Event(), threading.Event()
        class Blocking:
            session = SimpleNamespace(_lock=threading.RLock(), ready=True)
            def tick(self):
                entered.set()
                release.wait(3)
        s = NativeScheduler(Blocking(), interval=0.05)
        s.start()
        self.assertTrue(entered.wait(2))
        closer = threading.Thread(target=lambda: (s.stop(), stopped.set()))
        closer.start()
        self.assertFalse(stopped.wait(0.05))
        release.set()
        closer.join(2)
        self.assertTrue(stopped.is_set())
        self.assertFalse(s._thread.is_alive())

    def test_occupied_port_prevents_terminal_initialization(self):
        from unittest.mock import Mock, patch
        import run_recovery_native as cli
        with tempfile.TemporaryDirectory() as d:
            terminal = d+'/terminal.exe'
            open(terminal, 'w').close()
            held = cli.reserve_loopback_listener(0)
            try:
                port = held.getsockname()[1]
                mt5 = SimpleNamespace(initialize=Mock(), shutdown=Mock())
                with patch.object(cli.sys, 'platform', 'win32'), patch.dict('os.environ', {'RECOVERY_API_TOKEN':'x'*32}), patch.dict('sys.modules', {'MetaTrader5':mt5}):
                    result = cli.main(['--run-native','--terminal',terminal,'--account','1',
                        '--server','demo','--symbol','XAUUSD.s','--magic','123',
                        '--database',d+'/s.db','--port',str(port)])
                self.assertEqual(result, 1)
                mt5.initialize.assert_not_called()
                mt5.shutdown.assert_not_called()
            finally:
                held.close()

    def test_cli_passes_reserved_socket_and_closes_in_order(self):
        from unittest.mock import Mock, patch
        import run_recovery_native as cli
        events = []
        captured = []
        mt5 = SimpleNamespace(initialize=lambda **kw: events.append('initialize') or True,
                              shutdown=lambda: events.append('shutdown'))
        class Runtime:
            def __init__(self, *a, **kw): pass
            def open(self): events.append('open'); return object()
            def close(self): events.append('close')
        class Server:
            def __init__(self, config): pass
            def run(self, *, sockets):
                captured.extend(sockets)
                self_socket = sockets[0]
                self_socket.getsockname()
                events.append('run')
        with tempfile.TemporaryDirectory() as d:
            terminal = d+'/terminal.exe'; open(terminal, 'w').close()
            # Have CLI request port zero through a test-only reservation wrapper.
            reserve = cli.reserve_loopback_listener
            with patch.object(cli.sys, 'platform', 'win32'), patch.dict('os.environ', {'RECOVERY_API_TOKEN':'x'*32}), patch.dict('sys.modules', {'MetaTrader5':mt5}), patch('recovery.native_runtime.NativeRuntime', Runtime), patch('uvicorn.Server', Server), patch.object(cli,'reserve_loopback_listener',side_effect=lambda port: reserve(0)):
                result = cli.main(['--run-native','--terminal',terminal,'--account','1',
                    '--server','demo','--symbol','XAUUSD.s','--magic','123','--database',d+'/s.db'])
            self.assertEqual(result, 0)
            self.assertEqual(events, ['initialize','open','run','close','shutdown'])
            self.assertEqual(captured[0].fileno(), -1)

    def test_windows_exclusive_option_precedes_bind_with_socket_double(self):
        from unittest.mock import Mock, patch
        import run_recovery_native as cli
        listener = Mock()
        calls = []
        listener.setsockopt.side_effect = lambda *a: calls.append(('exclusive', a))
        listener.bind.side_effect = lambda address: calls.append(('bind', address))
        listener.listen.side_effect = lambda backlog: calls.append(('listen', backlog))
        # Exercise Windows-only constant branch without using a native Windows
        # socket or claiming Windows runtime validation.
        with patch.object(cli.socket, 'SO_EXCLUSIVEADDRUSE', 999, create=True), patch.object(cli.socket, 'socket', return_value=listener):
            result = cli.reserve_loopback_listener(8767)
        self.assertIs(result, listener)
        self.assertEqual(calls, [('exclusive', (cli.socket.SOL_SOCKET, 999, 1)),
            ('bind', ('127.0.0.1', 8767)), ('listen', 128)])
        listener.close.assert_not_called()
