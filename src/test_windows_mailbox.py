"""Real NTFS handles exercise GUI polling, exclusive readers and once-only claims."""
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

import control_host as c
import ipc_files
from test_control_host import FakeStrategy


@unittest.skipUnless(os.name == "nt", "requires Windows file sharing modes")
class WindowsMailboxTests(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.root = Path(work.name)
        self.host = self.enterContext(c.ControlHost(self.root))
        self.strategy = FakeStrategy()
        self.host.poll(self.strategy)
        self.client = c.LocalClient(self.root)
        self.status = self.root / "status.json"

    def test_normal_open_reader_keeps_prior_snapshot_while_new_name_is_published(self):
        self.assertIs(c.open_shared_read, ipc_files.open_shared_read)
        with c.open_shared_read(self.status) as old:
            self.strategy.data["reason"] = "updated"
            self.host.poll(self.strategy)
            self.assertEqual(self.client.status()["reason"], "updated")
            self.assertNotEqual(json.load(old).get("reason"), "updated")

    def test_held_restrictive_reader_keeps_last_good_then_recovers_with_no_second_start(self):
        ticket = self.client.submit("start", self.client.status())
        with self.status.open("rb") as held:
            last_good = held.read()
            with self.assertLogs(c.__name__, level="WARNING"):
                self.host.poll(self.strategy)
            self.host.poll(self.strategy)
            self.assertEqual(self.status.read_bytes(), last_good)
            self.assertFalse(self.client.status()["running"])
            self.assertEqual(self.client.receipt(ticket)["status"], "applied")
            self.assertEqual(self.strategy.calls, ["start"])
        time.sleep(.17)
        self.host.poll(self.strategy)
        self.assertTrue(self.client.status()["running"])
        self.assertEqual(self.strategy.calls, ["start"])

    def test_short_restrictive_reader_releases_during_retry(self):
        held = self.status.open("rb")
        self.addCleanup(held.close)
        move = os.replace
        attempts = []
        def replace(source, target):
            try:
                return move(source, target)
            except OSError as exc:
                # Release only after observing a real error from MoveFileEx.
                if target == self.status and c.sharing_error(exc):
                    attempts.append(exc.winerror)
                    held.close()
                raise
        self.strategy.data["reason"] = "recovered"
        with patch.object(c.os, "replace", replace):
            self.host.poll(self.strategy)
        self.assertTrue(attempts)
        self.assertEqual(self.client.status()["reason"], "recovered")

    def test_busy_receipt_and_request_never_repeat_an_accepted_action(self):
        handle = self.client.submit("start", self.client.status())
        request = self.host.commands / (handle[2] + ".json")
        receipt = self.host.receipts / request.name
        claimed = []
        run = self.strategy.control
        def hold_receipt(action):
            claimed.append(receipt.open("rb"))
            return run(action)
        try:
            with request.open("rb"), patch.object(self.strategy, "control", hold_receipt):
                self.host.poll(self.strategy)
                self.host.poll(self.strategy)
                self.assertTrue(request.exists())
                self.assertEqual(self.client.receipt(handle)["status"], "accepted")
                self.assertEqual(self.strategy.calls, ["start"])
                with self.assertRaises(FileExistsError):
                    c._write(request, {"duplicate": True}, new=True)
        finally:
            for held in claimed:
                held.close()
        self.host.poll(self.strategy)
        self.assertFalse(request.exists())
        self.assertEqual(self.client.receipt(handle)["status"], "applied")
        self.assertEqual(self.strategy.calls, ["start"])

    def test_invalid_request_with_deny_delete_reader_is_safely_revisited(self):
        path = self.host.commands / (uuid4().hex + ".json")
        path.write_bytes(b"not-json")
        with path.open("rb"):
            self.host.poll(self.strategy)
            self.assertTrue(path.exists())
        self.host.poll(self.strategy)
        self.assertFalse(path.exists())
        self.assertEqual(self.strategy.calls, [])

    def test_restart_can_wait_out_a_retired_status_reader(self):
        with tempfile.TemporaryDirectory() as temp:
            api = c.LocalClient(temp)
            path = Path(temp) / "status.json"
            with c.ControlHost(temp) as before:
                before.poll(self.strategy)
                old = api.status()
                reader = path.open("rb")
            try:
                with c.ControlHost(temp) as after:
                    with self.assertLogs(c.__name__, level="WARNING"):
                        after.poll(self.strategy)
                    self.assertEqual(api.status()["boot"], old["boot"])
                    stale = api.submit("start", old)
                    reader.close()
                    time.sleep(.17)
                    after.poll(self.strategy)
                    self.assertNotEqual(api.status()["boot"], old["boot"])
                    self.assertIsNone(api.receipt(stale))
                    self.assertEqual(self.strategy.calls, [])
            finally:
                reader.close()


if __name__ == "__main__":
    unittest.main()
