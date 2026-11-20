"""Windows process isolation using a synthetic owner and a temporary private mailbox."""
import ctypes
from ctypes import wintypes
import multiprocessing as mp
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from control_host import LocalClient, _read, _write
from control_process import DesktopControlProcess
from test_control_host import FakeStrategy


KERNEL = ctypes.WinDLL("kernel32", use_last_error=True)
KERNEL.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
KERNEL.OpenProcess.restype = wintypes.HANDLE
KERNEL.GetPriorityClass.argtypes = [wintypes.HANDLE]
KERNEL.CloseHandle.argtypes = [wintypes.HANDLE]
KERNEL.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
KERNEL.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
NT = ctypes.WinDLL("ntdll", use_last_error=True)
NT.NtSuspendProcess.argtypes = NT.NtResumeProcess.argtypes = [wintypes.HANDLE]


def _fake_owner(root, info):
    api = LocalClient(root)
    worker = FakeStrategy()
    with DesktopControlProcess(root) as control:
        announced = False
        while True:
            control.poll(worker)
            if not announced and api.status() is not None:
                info.send(control.pid)
                announced = True
            time.sleep(.01)


@unittest.skipUnless(os.name == "nt", "Windows handles and Job Objects")
class ProcessTests(unittest.TestCase):
    def setUp(self):
        fixture = tempfile.TemporaryDirectory(prefix="recovery-control-process-")
        self.addCleanup(fixture.cleanup)
        self.root = Path(fixture.name)
        self.api = LocalClient(self.root)
        self.worker = FakeStrategy()

    def start(self):
        self.service = self.enterContext(DesktopControlProcess(self.root))
        self.drive(self.api.status)

    def drive(self, condition, *, cooperate=True, timeout=20):
        limit = time.monotonic() + timeout
        while time.monotonic() < limit:
            result = condition()
            if result:
                return result
            if cooperate:
                self.service.poll(self.worker)
            time.sleep(.01)
        self.fail("Timed out waiting for the local synthetic desktop service")

    def wait_for_command(self):
        def pending():
            x = self.service._pending
            return x is not None and x.request.get("kind") == "control"
        limit = time.monotonic() + 15
        while time.monotonic() < limit:
            if pending():
                return
            # Serve snapshots only, so a just-claimed action remains in the bridge.
            x = self.service._pending
            if x is not None and x.request.get("kind") == "snapshot":
                self.service.poll(self.worker)
            time.sleep(.01)
        self.fail("Child did not make its single control RPC")

    def handle(self, pid):
        self.assertIsInstance(pid, int)
        self.assertNotEqual(pid, os.getpid())
        handle = KERNEL.OpenProcess(0x100000 | 0x1000 | 0x0800 | 1, False, pid)
        self.assertTrue(handle)
        self.addCleanup(KERNEL.CloseHandle, handle)
        return handle

    def test_real_child_once_only_mutation_and_expired_owner_epoch(self):
        self.start()
        self.assertEqual(KERNEL.GetPriorityClass(self.handle(self.service.pid)), 0x4000)
        old = self.api.status()
        ticket = self.api.submit("start", old)
        self.wait_for_command()
        self.worker.data["strategy_epoch"] += 1
        self.service.poll(self.worker)
        self.drive(lambda: (self.api.receipt(ticket) or {}).get("status") == "rejected")
        self.assertFalse(self.worker.calls)
        self.drive(lambda: (self.api.status() or {}).get("generation") != old["generation"])
        ticket = self.api.submit("start", self.api.status())
        path = self.root/ticket[0]/"requests"/(ticket[2]+".json")
        original = _read(path)
        self.drive(lambda: (self.api.receipt(ticket) or {}).get("status") == "applied")
        self.drive(lambda: not path.exists())
        _write(path, original, new=True)
        self.drive(lambda: not path.exists())
        self.assertEqual(self.worker.calls, ["start"])
        current = self.drive(lambda: (state if (state := self.api.status()) and state["running"] else None))
        ticket = self.api.submit("pause", current)
        self.drive(lambda: (self.api.receipt(ticket) or {}).get("status") == "applied")
        self.drive(lambda: (state if (state := self.api.status()) and not state["running"] else None))
        ticket = self.api.submit("update_settings", self.api.status(), self.worker.data["settings"])
        self.drive(lambda: (self.api.receipt(ticket) or {}).get("status") == "applied")
        self.assertEqual(self.worker.calls, ["start", "pause", "update_settings"])

    def test_suspended_child_and_reboot_do_not_block_or_replay(self):
        self.start()
        prior = self.api.status()
        ticket = self.api.submit("start", prior)
        self.wait_for_command()
        handle = self.handle(self.service.pid)
        self.assertEqual(NT.NtSuspendProcess(handle), 0)
        try:
            began = time.monotonic()
            for _ in range(500):
                self.service.poll(self.worker)
            self.assertLess(time.monotonic()-began, 2)
            self.assertEqual(self.worker.calls, ["start"])
            self.assertEqual(self.api.receipt(ticket)["status"], "accepted")
            self.assertTrue(KERNEL.TerminateProcess(handle, 7))
        finally:
            NT.NtResumeProcess(handle)
        self.drive(lambda: (s if (s := self.api.status()) and s["boot"] != prior["boot"] else None))
        self.assertEqual(self.api.receipt(ticket)["status"], "accepted")
        self.assertEqual(self.worker.calls, ["start"])

    def test_child_dies_with_owner_even_if_suspended(self):
        ctx = mp.get_context("spawn")
        recv, send = ctx.Pipe(duplex=False)
        owner = ctx.Process(target=_fake_owner, args=(self.root, send))
        owner.start()
        send.close()
        try:
            self.assertTrue(recv.poll(25))
            child = self.handle(recv.recv())
            self.assertEqual(NT.NtSuspendProcess(child), 0)
            try:
                owner.terminate()
                owner.join(5)
                self.assertEqual(KERNEL.WaitForSingleObject(child, 5000), 0)
            finally:
                NT.NtResumeProcess(child)
        finally:
            recv.close()
            if owner.is_alive():
                owner.terminate()
            owner.join(5)
            owner.close()

    def test_windows_gui_interpreter_spawn_under_isolated_flags(self):
        marker = self.root / "fixture-ok"
        script = self.root / "fixture_owner.pyw"
        script.write_text(
            "import multiprocessing,sys,time\nfrom pathlib import Path\n"
            f"sys.path.insert(0, {str(Path(__file__).resolve().parent)!r})\n"
            "from control_process import DesktopControlProcess\nfrom control_host import LocalClient\n"
            "from test_control_host import FakeStrategy\n"
            "if __name__ == '__main__':\n"
            "    multiprocessing.freeze_support()\n"
            "    assert sys.flags.isolated and sys.dont_write_bytecode\n"
            "    root=Path(__file__).resolve().parent\n"
            "    api=LocalClient(root); worker=FakeStrategy()\n"
            "    with DesktopControlProcess(root) as service:\n"
            "        limit=time.monotonic()+20\n"
            "        while not api.status() and time.monotonic()<limit:\n"
            "            service.poll(worker);time.sleep(.01)\n"
            "        assert api.status() and not worker.calls\n"
            "    import ctypes,runpy,types\n"
            "    app_dir=Path(sys.path[0]); gui=runpy.run_path(str(app_dir/'recovery_desktop.pyw'))['main']\n"
            "    fake=types.ModuleType('python_supervisor'); fake.ensure_supervisor=lambda:None\n"
            "    sys.modules['python_supervisor']=fake; gui.__globals__['LocalClient']=lambda:LocalClient(root)\n"
            "    tk=gui.__globals__['tk']; real_tk=tk.Tk\n"
            "    def hidden_window():\n"
            "        w=real_tk();w.withdraw();w.after(100,w.quit);return w\n"
            "    tk.Tk=hidden_window;gui()\n"
            "    k=ctypes.windll.kernel32;k.GetCurrentProcess.restype=ctypes.c_void_p\n"
            "    k.GetPriorityClass.argtypes=[ctypes.c_void_p]\n"
            "    assert k.GetPriorityClass(k.GetCurrentProcess())==0x4000\n"
            "    (root/'fixture-ok').write_text('ok')\n", encoding="utf-8")
        # Only the temporary owner is started; it imports no MT5 runtime.
        result = subprocess.run([str(Path(sys.executable).with_name("pythonw.exe")), "-I", "-B", "-X", "utf8",
                                 str(script)], cwd=self.root, timeout=35)
        self.assertEqual(result.returncode, 0)
        self.assertTrue(marker.is_file())


if __name__ == "__main__":
    mp.freeze_support()
    unittest.main()
