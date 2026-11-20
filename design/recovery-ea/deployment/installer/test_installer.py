"""Exercise a packaged Windows installer in disposable mock terminal directories."""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import runpy
import secrets
import subprocess
import sys
import tempfile
import time
import unittest
from uuid import uuid4


ROLES = {"RecoveryTrader.ex5", "RecoveryVerifier.ex5", "RecoveryPriceWatcher.ex5"}
ORIGIN = "https://panel.example.test"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def snapshot(root, *, exclude=()):
    result = {}
    for path in root.rglob("*"):
        if any(path.is_relative_to(item) for item in exclude):
            continue
        relative = path.relative_to(root).as_posix()
        if path.is_dir():
            result[relative] = "dir"
        else:
            info = path.stat()
            result[relative] = (info.st_ino, info.st_mtime_ns, digest(path))
    return result


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


async def get(app, path):
    received = False
    events = []

    async def receive():
        nonlocal received
        if not received:
            received = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await asyncio.Event().wait()

    async def send(message):
        events.append(message)

    scope = {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1", "method": "GET", "scheme": "https", "path": path,
        "root_path": "", "raw_path": path.encode("ascii"), "query_string": b"",
        "headers": [(b"host", b"panel.example.test")], "server": ("panel.example.test", 443),
        "client": ("127.0.0.1", 12345),
    }
    await asyncio.wait_for(app(scope, receive, send), timeout=10)
    status, = [event["status"] for event in events if event["type"] == "http.response.start"]
    body = b"".join(event["body"] for event in events if event["type"] == "http.response.body")
    return status, body


def probe_web_panel(script, data, profile):
    # Run inside the installed embedded interpreter. Exercise the production app
    # factory and route without binding a port or connecting to a live terminal.
    from fastapi import FastAPI

    panel = runpy.run_path(str(script))
    config = panel["read_config"](data, ORIGIN, profile=profile, env=os.environ)
    check = unittest.TestCase()
    called = []

    def inspect_app(app, **options):
        called.append(True)
        check.assertIsInstance(app.app, FastAPI)
        status, body = asyncio.run(get(app, "/recovery"))
        check.assertEqual(status, 200)
        html = body.decode("utf-8")
        for label in ("Ожидай рыночный ордер", "Ожидай триггер ордер и выставь SL", "Отменить ожидание"):
            check.assertIn(label, html)
        check.assertNotIn("диапазон", html.casefold())
        check.assertNotIn("configure_ranges", html)
        status, asset = asyncio.run(get(app, "/recovery/assets/recovery_manual.mjs"))
        check.assertEqual(status, 200)
        check.assertTrue(asset)
        status, _ = asyncio.run(get(app, "/recovery/assets/recovery_ranges.mjs"))
        check.assertIn(status, (401, 404))

    check.assertEqual(panel["serve"](config, run_server=inspect_app), 0)
    check.assertEqual(called, [True])


class InstallerSmoke(unittest.TestCase):
    setup_exe = None

    @classmethod
    def setUpClass(cls):
        if cls.setup_exe is None:
            raise unittest.SkipTest("Run test_installer.py with --setup <installer.exe>.")

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="recovery-installer-smoke-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        home = self.root / "Test User"
        self.local = home / "AppData/Local"
        self.roaming = home / "AppData/Roaming"
        self.tmp = self.root / "temp"
        self.data = self.root / "Selected MT5"
        self.experts = self.data / "MQL5/Experts/RECOVERY"
        self.versions = self.local / "AI-IMPULS-RECOVERY/versions"
        self.profile = self.local / "AI-IMPULS-RECOVERY/profiles/old/recovery-profile.json"
        for folder in (self.tmp, self.experts.parent, self.roaming):
            folder.mkdir(parents=True)
        self.env = {k: v for k, v in os.environ.items()
                    if not k.upper().startswith(("PYTHON", "RECOVERY_"))}
        self.env.update(LOCALAPPDATA=str(self.local), APPDATA=str(self.roaming),
                        USERPROFILE=str(home), HOME=str(home), TEMP=str(self.tmp), TMP=str(self.tmp))
        self.scope = str(uuid4())
        write_json(self.profile, {"schema": 1, "install_id": self.scope, "panel_key": secrets.token_hex(32)})
        for path in (self.data / "MQL5/Profiles/Charts/Default/chart01.chr",
                     self.data / "config/common.ini", self.experts.parent / "Unrelated.ex5",
                     self.roaming / "MetaQuotes/Terminal/OTHER/MQL5/Experts/Other.ex5"):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(os.urandom(32))

    def invoke(self, args, *, expected=0, cwd=None, stdin=None):
        result = subprocess.run([str(arg) for arg in args], cwd=cwd or self.root, env=self.env,
                                input=stdin, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                timeout=150, creationflags=subprocess.CREATE_NO_WINDOW)
        output = result.stdout.decode("utf-8", errors="replace")
        self.assertEqual(result.returncode, expected, f"{Path(args[0]).name}: {output[-1200:]}")
        return output

    def install(self, **options):
        return self.invoke([self.setup_exe, "--install", self.data], **options)

    def test_help_without_installation(self):
        before = snapshot(self.root)
        help_text = self.invoke([self.setup_exe, "--help"])
        self.assertIn("--install", help_text)
        self.assertIn("--help", help_text)
        self.assertEqual(before, snapshot(self.root))

    def test_conflict_preserves_existing_files(self):
        self.experts.mkdir()
        (self.experts / "RecoveryVerifier.ex5").write_bytes(b"existing unrelated build")
        before = snapshot(self.root)
        self.install(expected=1)
        self.assertEqual(before, snapshot(self.root))

    def test_install_reinstall_and_packaged_panel(self):
        targets = (self.versions, self.experts)
        untouched = snapshot(self.root, exclude=targets)
        self.install()
        self.assertEqual(untouched, snapshot(self.root, exclude=targets))
        app, = self.versions.iterdir()
        manifest = json.loads((app / "stage-manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(len(manifest["roles"]), 3)
        self.assertSetEqual({role["file"] for role in manifest["roles"]}, ROLES)
        self.assertSetEqual({path.name for path in self.experts.iterdir()}, ROLES)
        for role in manifest["roles"]:
            path = self.experts / role["file"]
            self.assertGreater(role["bytes"], 0)
            self.assertEqual(path.stat().st_size, role["bytes"])
            self.assertEqual(digest(path), role["sha256"])
        python = app / "python/python.exe"
        self.assertTrue(python.is_file())
        installed = snapshot(self.root)
        self.install()
        self.assertEqual(installed, snapshot(self.root))

        panel = app / "design/recovery-ea/deployment/run_panel.py"
        embedded = [python, "-I", "-B", "-X", "utf8"]
        args = [*embedded, panel, "--check", "--mt5-data-dir", self.data,
                "--origin", ORIGIN, "--profile", self.profile]
        channel = self.data / "MQL5/Files/RECOVERY_PANEL"
        boot = str(uuid4())
        for folder in ("commands", "receipts"):
            (channel / "sessions" / boot / folder).mkdir(parents=True)
        self.invoke(args, expected=3)
        write_json(channel / "state.json", {
            "version": 1, "scope_id": self.scope, "boot_id": boot, "context_id": str(uuid4()),
            "generation_id": str(uuid4()), "written_at": int(time.time()), "mode": "ready",
        })
        ready = snapshot(self.root)
        self.invoke(args)
        self.invoke([*embedded, Path(__file__).resolve(), "--asgi-probe", panel, self.data, self.profile])
        cmd = Path(os.environ["SystemRoot"]) / "System32/cmd.exe"
        self.assertTrue((app / "RECOVERY.cmd").is_file())
        self.invoke([cmd, "/d", "/c", r".\RECOVERY.cmd", "existing-check"], cwd=app,
                    stdin=f"{self.profile}\n{self.data}\n{ORIGIN}\n".encode("utf-8"))
        self.assertEqual(ready, snapshot(self.root))


def main():
    if len(sys.argv) == 5 and sys.argv[1] == "--asgi-probe":
        probe_web_panel(*(Path(arg) for arg in sys.argv[2:]))
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--setup", type=Path, required=True, help="Windows installer executable")
    args = parser.parse_args()
    if os.name != "nt" or not args.setup.is_file():
        parser.error("Run on Windows and supply an existing installer executable.")
    InstallerSmoke.setup_exe = args.setup.resolve()
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(InstallerSmoke))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
