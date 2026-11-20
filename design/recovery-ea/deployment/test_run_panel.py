from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
from unittest import TestCase, main
from uuid import uuid4

from fastapi.testclient import TestClient

from run_panel import ConfigError, HOST, PORT, channel_root_from_data_dir, parse_origin, parser, read_config, serve


KEY = "R" * 43
ORIGIN = "https://vps.example"


class RunPanelTest(TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.data = self.root / "mt5-data"
        self.channel = self.data / "MQL5" / "Files" / "RECOVERY_PANEL"
        self.sessions = self.channel / "sessions"
        self.scope = str(uuid4())
        self.env = {"RECOVERY_PANEL_KEY": KEY, "RECOVERY_SCOPE_ID": self.scope}
        self.sessions.mkdir(parents=True)

    def files(self):
        return sorted(str(path.relative_to(self.root)) for path in self.root.rglob("*"))

    def config(self, error_env=None, profile=None):
        return read_config(self.data, ORIGIN, profile=profile,
                           env=self.env if error_env is None else error_env)

    def test_working_path_exact_no_file_mutation_or_fallback(self):
        before = self.files()
        self.assertEqual(channel_root_from_data_dir(self.data), self.channel)
        with self.assertRaises(ConfigError):
            channel_root_from_data_dir(self.root / "wrong-MT5")
        with self.assertRaises(ConfigError):
            channel_root_from_data_dir(Path("relative"))
        self.assertEqual(self.files(), before)
        self.sessions.rmdir()
        with self.assertRaises(ConfigError):
            self.config()
        self.assertFalse(self.sessions.exists())

    def test_refuses_aliased_candidate(self):
        alias = self.root / "linked-mt5"
        try:
            alias.symlink_to(self.data, target_is_directory=True)
        except OSError:
            self.skipTest("symbolic links unavailable")
        with self.assertRaises(ConfigError):
            channel_root_from_data_dir(alias)
        self.sessions.rmdir()
        other = self.root / "alternate-sessions"
        other.mkdir()
        self.sessions.symlink_to(other, target_is_directory=True)
        with self.assertRaises(ConfigError):
            channel_root_from_data_dir(self.data)

    def test_tls_origin_canonical_and_exact(self):
        for origin in (ORIGIN, "https://203.0.113.5", "https://[2001:db8::1]:8443"):
            self.assertEqual(parse_origin(origin), origin)
        for origin in ("http://vps.example", "https://vps.example/", "https://User@host",
                       "https://vps.example?x=1", "https://vps.example#", "https://VPS.example",
                       "https://vps.example:443", "https://vps.example\n",
                       "https://vps.example:70000", "https://vps.example:", "https://a\\b"):
            with self.subTest(origin=origin), self.assertRaises(ConfigError):
                parse_origin(origin)

    def test_credential_sources_hide_secret_and_detect_corrupt_file(self):
        profile = self.root / "recovery-profile.json"
        alternate = "X" * 43
        profile.write_text(json.dumps({"schema": 1, "panel_key": alternate,
                                       "install_id": self.scope, "mt5": {"login": None}}))
        self.assertEqual(self.config().key, KEY)
        current = self.config(profile=profile)
        self.assertEqual(current.key, alternate)
        self.assertNotIn(alternate, repr(current))
        profile.write_text('{"schema":1,"schema":1,"panel_key":"' + alternate + '"}')
        with self.assertRaises(ConfigError) as warning:
            self.config(profile=profile)
        self.assertNotIn(alternate, str(warning.exception))
        with self.assertRaises(ConfigError) as warning:
            self.config({**self.env, "RECOVERY_PANEL_KEY": KEY + "\n"})
        self.assertNotIn(KEY, str(warning.exception))
        with self.assertRaises(ConfigError):
            self.config({**self.env, "RECOVERY_SCOPE_ID": "bad"})

    def test_local_check_requires_real_state_does_not_start_server_or_write(self):
        captured = io.StringIO()
        before = self.files()
        def never(*args, **kwargs):
            self.fail("health check must not run a server")
        config = self.config()
        with redirect_stdout(captured):
            self.assertEqual(serve(config, check=True, run_server=never), 3)
        self.assertIn("Состояние EA не подтверждено", captured.getvalue())
        self.assertNotIn(KEY, captured.getvalue())
        self.assertEqual(self.files(), before)
        (self.channel / "state.json").write_text(json.dumps({
            "version": 1, "scope_id": self.scope, "boot_id": str(uuid4()),
            "context_id": str(uuid4()), "generation_id": str(uuid4()), "mode": "ready",
        }))
        with redirect_stdout(captured):
            self.assertEqual(serve(config, check=True, run_server=never), 0)

    def test_actual_app_safe_bind_and_no_native_write_without_ea_state(self):
        before = self.files()
        app_called = []

        def mocked_uvicorn(app, **options):
            app_called.append(options)
            c = TestClient(app, base_url=ORIGIN)
            self.assertEqual(c.get("/recovery").status_code, 200)
            headers = {"X-Recovery-Session": "1", "Origin": ORIGIN,
                       "Content-Type": "application/json", "Sec-Fetch-Site": "same-origin"}
            login = c.post("/api/recovery/session/login", json={},
                           headers={**headers, "Authorization": "Bearer " + KEY})
            self.assertEqual(login.status_code, 200)
            self.assertEqual(c.get("/api/recovery/ea/state", headers=headers).status_code, 503)
            csrf = login.json()["csrf"]
            self.assertEqual(c.get("/api/recovery/ea/support-settings", headers=headers).status_code, 503)
            command = {"request_id": str(uuid4()), "expected_boot": str(uuid4()),
                       "expected_context": str(uuid4()), "expected_generation": str(uuid4()), "confirmed": True}
            outcome = c.post("/api/recovery/ea/manual_pending_wait", json=command,
                             headers={**headers, "X-Recovery-CSRF": csrf})
            self.assertEqual(outcome.status_code, 202)
            self.assertEqual(outcome.json()["dispatch"], "unknown")

        self.assertEqual(serve(self.config(), run_server=mocked_uvicorn), 0)
        self.assertEqual(app_called, [{"host": HOST, "port": PORT, "workers": 1,
                                      "access_log": False, "proxy_headers": True,
                                      "forwarded_allow_ips": HOST}])
        self.assertEqual(HOST, "127.0.0.1")
        self.assertEqual(self.files(), before)

    def test_argument_error_and_launch_error_do_not_echo_untrusted_secret_text(self):
        capture = io.StringIO()
        with redirect_stderr(capture), self.assertRaises(SystemExit):
            parser().parse_args(["--key", KEY])
        with redirect_stderr(capture):
            def failing(*args, **kwargs):
                raise OSError(KEY)
            self.assertEqual(serve(self.config(), run_server=failing), 2)
        self.assertNotIn(KEY, capture.getvalue())


if __name__ == "__main__":
    main()
