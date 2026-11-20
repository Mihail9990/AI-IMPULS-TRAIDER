import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import RLock
from unittest import TestCase, main
from unittest.mock import patch

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from support_routes import support_settings_router
from support_store import SettingsConflict, SettingsContext, SettingsUnavailable, SupportStore

URL = "/api/recovery/ea/support-settings"
AUTH = {"x-test-session": "valid", "x-test-csrf": "valid"}


class TestSupportRuntime(TestCase):
    def setUp(self):
        directory = self.enterContext(TemporaryDirectory())
        self.path = Path(directory) / "support.json"
        self.active, self.step = False, "0.01"
        self.lock = RLock()

        def context():
            # Caller contract: both operations must use the owner's same lock.
            self.assertTrue(self.lock._is_owned())
            return SettingsContext(self.step, self.active)

        self.store = SupportStore(self.path, self.lock, context)
        self.new_store = lambda: SupportStore(self.path, self.lock, context)
        app = FastAPI()

        def auth(request: Request):
            if request.headers.get("x-test-session") != "valid":
                raise HTTPException(401)
            if request.method == "PUT" and request.headers.get("x-test-csrf") != "valid":
                raise HTTPException(403)

        app.include_router(support_settings_router(self.store, auth))
        self.client = TestClient(app)

    @staticmethod
    def body(**kwargs):
        return dict(dict(revision=0, support_threshold="0.40"), **kwargs)

    def test_defaults_and_unchanged(self):
        self.assertEqual(self.store.projection(), dict(self.body(), editable=True))
        self.store.save(self.body())
        self.assertFalse(self.path.exists())
        with self.store.locked_next_generation() as (revision, ticks):
            self.assertEqual((revision, ticks.support_first_ticks, ticks.support_second_ticks,
                              ticks.support_offset_ticks), (0, 0, 40, 0))

    def test_legacy_breakeven_preference_survives_and_stage_is_removed(self):
        self.path.write_text(json.dumps({"version": 1, "revision": 3, "settings": {
            "support_first": "0.31", "support_second": "0.20", "support_first_enabled": False,
        }}), encoding="utf-8")
        self.assertEqual(self.store.projection(), {
            "revision": 3, "editable": True, "support_threshold": "0.20",
        })
        self.assertEqual(json.loads(self.path.read_text())["version"], 1)  # read is non-mutating
        updated = self.store.save(self.body(revision=3, support_threshold="0.42"))
        self.assertEqual(json.loads(self.path.read_text()), {
            "version": 2, "revision": 4, "settings": {"support_threshold": "0.42"},
        })
        other = self.new_store()
        self.assertEqual(other.projection(), updated)
        with other.locked_next_generation() as (revision, ticks):
            self.assertEqual((revision, ticks.support_first_ticks, ticks.support_second_ticks,
                              ticks.support_offset_ticks), (4, 0, 42, 0))

    def test_no_edit_after_generation_reserved_under_owner_lock(self):
        with self.store.locked_next_generation() as (_, ticks):
            self.active = True  # real owner would reserve and publish its state here
        with self.assertRaises(SettingsConflict):
            self.store.save(self.body(support_threshold="0.60"))
        with self.assertRaises(SettingsConflict):
            with self.store.locked_next_generation():
                self.fail("active owner cannot reserve another snapshot")
        self.assertEqual(ticks.support_second_ticks, 40)

    def test_corrupt_store_and_write_failure_do_not_silently_reset(self):
        self.store.save(self.body(support_threshold="0.60"))
        before = self.path.read_bytes()
        with patch("support_store.os.replace", side_effect=OSError):
            with self.assertRaises(SettingsUnavailable):
                self.store.save(self.body(revision=1, support_threshold="0.70"))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])
        self.path.write_bytes(b"bad json")
        with self.assertRaises(SettingsUnavailable):
            self.store.save(self.body())

    def test_http_auth_csrf_stale_and_unknown(self):
        self.assertEqual(self.client.get(URL).status_code, 401)
        self.assertEqual(self.client.put(URL, headers={"x-test-session": "valid"},
                                         json=self.body()).status_code, 403)
        self.assertEqual(self.client.get(URL, headers=AUTH).headers["cache-control"], "no-store")
        response = self.client.put(URL, headers=AUTH, json=self.body(support_threshold="0.42"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["revision"], 1)
        self.assertEqual(self.client.put(URL, headers=AUTH, json=self.body()).status_code, 409)
        self.active = None
        self.assertFalse(self.client.get(URL, headers=AUTH).json()["editable"])
        self.assertEqual(self.client.put(URL, headers=AUTH,
                                         json=self.body(revision=1)).status_code, 409)

    def test_http_rejects_forged_metadata_duplicate_keys_and_offgrid(self):
        self.assertEqual(self.client.put(URL, headers=AUTH,
                            json=self.body(symbol_tick_size="0.001")).status_code, 422)
        self.assertEqual(self.client.put(URL, headers=AUTH,
                            json=self.body(support_first_enabled=True)).status_code, 422)
        response = self.client.put(URL, headers=AUTH, json=self.body(support_threshold="0.401"))
        self.assertEqual(response.status_code, 422)
        headers = dict(AUTH, **{"content-type": "application/json"})
        self.assertEqual(self.client.put(URL, headers=headers,
                            content=b'{"revision":0,"revision":1}').status_code, 422)
        self.assertEqual(self.client.put(URL, headers=headers, content=b" "*4097).status_code, 413)
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    main()
