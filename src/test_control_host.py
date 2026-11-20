from copy import deepcopy
import ast
from importlib.machinery import SourceFileLoader
import importlib.util
from pathlib import Path
import tempfile
import time
import unittest
import re
from unittest import mock

import control_host as c
from local_settings import DEFAULT_SETTINGS, validate_settings


class FakeStrategy:
    def __init__(self):
        self.data = dict(running=False, available=True, can_start=True, can_pause=False,
                         mt5_connected=True, market_rearm_enabled=False, mode="ready",
                         account="***42", server="test", symbol="TEST",
                         strategy_epoch=1, settings=deepcopy(DEFAULT_SETTINGS))
        self.calls = []

    def snapshot(self):
        return self.data

    def control(self, op):
        self.calls.append(op)
        if op in {"start", "pause"}:
            run = op == "start"
            self.data.update(running=run, can_start=not run, can_pause=run)
        elif op == "manual_wait":
            self.data.update(mode="manual_wait", market_rearm_enabled=True, can_pause=False)
        elif op == "manual_pending_wait":
            self.data.update(mode="pending_wait", can_pause=False)
        elif op == "manual_cancel":
            mode = "active" if self.data["mode"] == "active" else "ready"
            self.data.update(mode=mode, market_rearm_enabled=False, can_pause=mode == "ready")
        return True

    def update_settings(self, settings):
        self.calls.append("update_settings")
        self.data["settings"] = settings
        return True


class MailboxTests(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.root = Path(td.name)
        self.client = c.LocalClient(self.root)
        self.strategy = FakeStrategy()
        self.host = self.enterContext(c.ControlHost(self.root))
        self.host.poll(self.strategy)

    def submit(self, op, settings=None):
        handle = self.client.submit(op, self.client.status(), settings)
        self.host.poll(self.strategy)
        self.assertEqual(self.client.receipt(handle)["status"], "applied")
        return handle

    def test_single_owner_and_all_controls(self):
        with self.assertRaises(RuntimeError):
            with c.ControlHost(self.root):
                self.fail("second owner")
        self.submit("start")
        self.assertFalse(self.client.status()["settings_editable"])
        self.submit("manual_wait")
        self.strategy.data["mode"] = "active"
        self.host.poll(self.strategy)
        self.submit("manual_cancel")
        self.assertEqual(self.client.status()["mode"], "active")
        self.assertFalse(self.client.status()["market_rearm_enabled"])
        self.strategy.data.update(mode="ready", can_pause=True)
        self.host.poll(self.strategy)
        self.submit("manual_pending_wait")
        self.submit("manual_cancel")
        self.submit("pause")
        self.assertEqual(self.strategy.calls, ["start", "manual_wait", "manual_cancel", "manual_pending_wait", "manual_cancel", "pause"])

    def test_settings_are_validated_then_applied_by_owner(self):
        edits = deepcopy(DEFAULT_SETTINGS)
        edits["initial_sl_distance"] = "1.25"
        edits["lot_schedule"][-1] = "0.16"
        self.submit("update_settings", edits)
        self.assertEqual(self.client.status()["settings"], edits)
        for bad in ("0", "NaN", "-1", "1e2", "1,2"):
            with self.assertRaises(ValueError):
                self.client.submit("update_settings", self.client.status(), dict(edits, support_threshold=bad))
        with self.assertRaises(ValueError):
            validate_settings(dict(edits, lot_schedule=edits["lot_schedule"][:-1]))
        self.submit("start")
        self.submit("manual_wait")
        with self.assertRaises(ValueError):
            self.client.submit("update_settings", self.client.status(), edits)

    def test_duplicate_request_id_is_consumed_once(self):
        ticket = self.client.submit("start", self.client.status())
        path = self.host.commands / (ticket[2]+".json")
        request = path.read_bytes()
        self.host.poll(self.strategy)
        path.write_bytes(request)
        self.host.poll(self.strategy)
        self.assertEqual(self.strategy.calls, ["start"])
        self.assertEqual(self.client.receipt(ticket)["status"], "applied")

    def test_receipts_name_only_known_actions_and_accept_legacy_without_name(self):
        original = self.client.status()
        ticket = self.client.submit("update_settings", original, deepcopy(DEFAULT_SETTINGS))
        self.host.poll(self.strategy)
        path = self.host.receipts / (ticket[2] + ".json")
        ack = self.client.receipt(ticket)
        self.assertEqual(ack["action"], "update_settings")
        self.assertEqual(set(ack), {"id", "boot", "generation", "status", "reason", "action"})
        ack.pop("action")
        c._write(path, ack)
        self.assertEqual(self.client.receipt(ticket), ack)
        forged = self.client.submit("start", self.client.status())
        request = self.host.commands / (forged[2] + ".json")
        row = c._read(request)
        del row["mac"]
        row["action"] = "private-fixture"
        row["mac"] = c.hmac.digest(self.host.key, c._encode(row), "sha256").hex()
        c._write(request, row)
        self.host.poll(self.strategy)
        rejected = self.client.receipt(forged)
        self.assertEqual(rejected["status"], "rejected")
        self.assertNotIn("action", rejected)

    def test_tampering_stale_generation_and_expiry(self):
        ticket = self.client.submit("start", self.client.status())
        path = self.host.commands/(ticket[2]+".json")
        row=c._read(path)
        c._write(path, dict(row, action="pause"))
        self.host.poll(self.strategy)
        self.assertIsNone(self.client.receipt(ticket))
        s = self.client.status()
        ticket = self.client.submit("start", s)
        self.strategy.data["strategy_epoch"] += 1
        self.host.poll(self.strategy)
        self.assertEqual(self.client.receipt(ticket)["reason"], "session_changed")
        with self.assertRaises(ValueError):
            self.client.submit("start", s)
        ticket = self.client.submit("start", self.client.status())
        with mock.patch.object(c.time, "time", return_value=time.time()+60):
            self.host.poll(self.strategy)
        self.assertEqual(self.client.receipt(ticket)["reason"], "expired")
        self.assertEqual(self.strategy.calls, [])

    def test_partial_ack_failure_keeps_claim_and_retries_only_receipt(self):
        t = self.client.submit("start", self.client.status())
        write = c._write
        def fails(path, value, **kw):
            if value.get("status") == "applied":
                raise PermissionError
            return write(path, value, **kw)
        with mock.patch.object(c, "_write", fails):
            self.host.poll(self.strategy)
        self.assertEqual(self.client.receipt(t)["status"], "accepted")
        self.host.poll(self.strategy)
        self.assertEqual(self.client.receipt(t)["status"], "applied")
        self.assertEqual(self.strategy.calls, ["start"])

    def test_accepted_unknown_after_interrupt_is_never_replayed(self):
        with tempfile.TemporaryDirectory() as separate:
            api=c.LocalClient(separate)
            with c.ControlHost(separate) as first:
                first.poll(self.strategy)
                ticket=api.submit("start",api.status())
                with mock.patch.object(self.strategy,"control",side_effect=SystemExit):
                    with self.assertRaises(SystemExit):
                        first.poll(self.strategy)
            self.assertIsNone(api.status())
            with c.ControlHost(separate) as second:
                second.poll(self.strategy)
                self.assertNotEqual(api.status()["boot"],ticket[0])
                self.assertEqual(api.receipt(ticket)["status"],"accepted")
                self.assertEqual(self.strategy.calls,[])

    def test_stale_status_and_schema(self):
        with mock.patch.object(c.time,"time",return_value=time.time()+6):
            self.assertIsNone(self.client.status())
        self.strategy.data.update(unknown_secret="private", support_applied=True)
        self.host.poll(self.strategy)
        self.assertNotIn("unknown_secret", self.client.status())
        self.assertIs(self.client.status()["support_applied"], True)

    def test_retired_owner_fields_are_ignored_in_both_directions(self):
        legacy = dict(realized_total="34.50", pending_outcomes=2, closed_outcomes=3,
                      currency="USD", reason="money_exits_unconfirmed")
        self.strategy.data.update(legacy)
        self.host.poll(self.strategy)
        path = self.root / "status.json"
        published = c._read(path)
        self.assertTrue(legacy.keys().isdisjoint(published))
        c._write(path, dict(published, **legacy))
        observed = self.client.status()
        self.assertTrue(legacy.keys().isdisjoint(observed))
        self.assertEqual(observed["boot"], published["boot"])
        self.assertEqual(observed["mode"], published["mode"])

    def test_hidden_tk_gui_reads_exact_snapshot_without_request(self):
        loader=SourceFileLoader("test_recovery_desktop", str(Path(__file__).with_name("recovery_desktop.pyw")))
        spec=importlib.util.spec_from_loader(loader.name,loader)
        mod=importlib.util.module_from_spec(spec)
        loader.exec_module(mod)
        for filename in ("recovery_strategy.py", "recovery_book.py"):
            tree = ast.parse(Path(__file__).with_name(filename).read_text(encoding="utf-8"))
            codes = {arg.value for node in ast.walk(tree) if isinstance(node, ast.Call)
                     and getattr(node.func, "id", "") in {"checked", "Wait"}
                     for arg in node.args if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
                     and re.fullmatch("[a-z_]+", arg.value)}
            self.assertFalse(codes - mod.REASONS.keys(), codes - mod.REASONS.keys())
        root=mod.tk.Tk()
        root.withdraw()
        self.addCleanup(root.destroy)
        ui=mod.Desktop(root,self.client,lambda: None)
        self.assertIn("***42",ui.identity.get())
        self.assertFalse(ui.buttons["start"].instate(["disabled"]))
        self.assertTrue(ui.buttons["manual_wait"].instate(["disabled"]))
        ui.edit()
        ui.editor.withdraw()
        self.assertEqual(len(ui.fields),26)
        self.assertEqual(self.strategy.calls,[])
        self.assertEqual(list(self.host.commands.glob("*.json")),[])


if __name__=="__main__":
    unittest.main()
