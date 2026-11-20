from importlib import import_module
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import RLock
from unittest import TestCase, main
from uuid import uuid4

from fastapi.testclient import TestClient

from channel import Channel
from runtime_panel import create_panel_app

URL = 'https://vps.example'
EA = '/api/recovery/ea'
SESSION = '/api/recovery/session'
SUPPORT = EA + '/support-settings'
prefs = import_module('design.recovery-ea.settings.support_store')
KEY = 'offline-test-panel-key-no-production' * 2


class PanelAssemblyTest(TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.scope, self.boot, self.context, self.generation = (str(uuid4()) for _ in range(4))
        self.boot_root = self.root / 'sessions' / self.boot
        for folder in ('commands', 'receipts'):
            (self.boot_root / folder).mkdir(parents=True)
        (self.root / 'state.json').write_text(json.dumps({
            'version': 1, 'scope_id': self.scope, 'boot_id': self.boot,
            'context_id': self.context, 'generation_id': self.generation, 'mode': 'ready',
        }), encoding='utf-8')
        self.channel = Channel(self.root, self.scope, clock=lambda: 42)
        self.app = create_panel_app(KEY, URL, self.channel)
        self.client = TestClient(self.app, base_url=URL)
        self.headers = {'X-Recovery-Session': '1', 'Sec-Fetch-Site': 'same-origin',
                        'Origin': URL, 'Content-Type': 'application/json'}
        self.request_id = str(uuid4())
        self.request = {
            'request_id': self.request_id, 'expected_boot': self.boot, 'expected_context': self.context,
            'expected_generation': self.generation, 'confirmed': True,
        }
        self.command = self.boot_root / 'commands' / (self.request_id + '.json')

    def login(self):
        response = self.client.post(SESSION + '/login', headers={
            **self.headers, 'Authorization': 'Bearer ' + KEY,
        }, json={})
        self.assertEqual(response.status_code, 200)
        return response

    def test_public_shell_and_fixed_assets_contain_no_key(self):
        home = self.client.get('/recovery')
        self.assertEqual(home.status_code, 200)
        self.assertIn('Ожидай триггер ордер и выставь SL', home.text)
        self.assertIn("frame-ancestors 'none'", home.headers['content-security-policy'])
        self.assertEqual(home.headers['cache-control'], 'no-store')
        for file in ('runtime_panel.mjs', 'runtime_panel.css', 'recovery_manual.mjs', 'session.js',
                     'support_controls.mjs', 'support_panel.mjs'):
            route = '/recovery/assets/' + file
            asset = self.client.get(route)
            self.assertEqual(asset.status_code, 200)
            self.assertTrue(asset.headers['content-type'].startswith(
                'text/css' if file.endswith('.css') else 'text/javascript'))
            self.assertNotIn(KEY, asset.text)
        self.assertNotIn(KEY, home.text)
        self.assertIn('Ожидай рыночный ордер', home.text)
        self.assertIn('Отменить ожидание', home.text)
        self.assertIn('Ордер и SL остаются в MT5.', home.text)
        self.assertNotIn('Диапазон', home.text)
        self.assertNotEqual(self.client.get('/recovery/assets/recovery_ranges.mjs').status_code, 200)
        self.assertNotEqual(self.client.get('/recovery/assets/state.json').status_code, 200)
        self.assertNotEqual(self.client.get('/recovery/assets/%2e%2e/%2e%2e/state.json').status_code, 200)

    def test_cookie_is_httponly_and_auth_does_not_leak_bearer(self):
        self.assertEqual(self.client.get(EA + '/state', headers=self.headers).status_code, 401)
        self.assertEqual(self.client.get(EA + '/state', headers={
            **self.headers, 'Authorization': 'Bearer ' + KEY}).status_code, 401)
        result = self.login()
        self.assertIn('__Host-RecoverySession=', result.headers['set-cookie'])
        for flag in ('Secure', 'HttpOnly', 'SameSite=Strict', 'Path=/'):
            self.assertIn(flag, result.headers['set-cookie'])
        self.assertNotIn(KEY, result.text + str(result.headers))
        self.assertEqual(self.client.get('/recovery', headers={}).status_code, 200)
        self.assertEqual(self.client.get('/recovery/assets/session.js').status_code, 200)
        read = self.client.get(EA + '/state', headers=self.headers)
        self.assertEqual(read.status_code, 200)
        self.assertEqual(read.json()['boot_id'], self.boot)
        self.assertNotIn(KEY, read.text)

    def test_wrong_csrf_origin_method_and_query_do_not_publish(self):
        csrf = self.login().json()['csrf']
        bad_cases = [
            (self.headers, EA + '/manual_pending_wait'),
            ({**self.headers, 'X-Recovery-CSRF': 'incorrect'}, EA + '/manual_pending_wait'),
            ({**self.headers, 'Origin': 'https://wrong.example', 'X-Recovery-CSRF': csrf},
             EA + '/manual_pending_wait'),
            ({**self.headers, 'Sec-Fetch-Site': 'cross-site', 'X-Recovery-CSRF': csrf},
             EA + '/manual_pending_wait'),
            ({**self.headers, 'X-Recovery-CSRF': csrf}, EA + '/manual_pending_wait?x=1'),
            ({**self.headers, 'X-Recovery-CSRF': csrf}, EA + '/unknown'),
        ]
        for headers, route in bad_cases:
            response = self.client.post(route, headers=headers, json=self.request)
            self.assertIn(response.status_code, (400, 403))
            self.assertFalse(self.command.exists())
        self.assertEqual(self.client.get(EA + '/state?x=1', headers=self.headers).status_code, 400)

    def test_browser_pending_wait_and_exact_status_query(self):
        csrf = self.login().json()['csrf']
        response = self.client.post(EA + '/manual_pending_wait',
                    headers={**self.headers, 'X-Recovery-CSRF': csrf}, json=self.request)
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()['status'], 'unknown')
        written = json.loads(self.command.read_text())
        self.assertEqual(written['payload'], {})
        self.assertEqual(written['operation'], 'manual_pending_wait')
        self.assertEqual(self.client.post(EA + '/configure_ranges', headers={
            **self.headers, 'X-Recovery-CSRF': csrf}, json=self.request).status_code, 403)
        route = EA + '/commands/' + self.request_id
        self.assertEqual(self.client.get(route, headers=self.headers).status_code, 400)
        self.assertEqual(self.client.get(route + '?expected_boot=' + self.boot + '&x=1',
                                         headers=self.headers).status_code, 400)
        self.assertEqual(self.client.get(route + '?expected_boot=' + self.boot, headers={
            **self.headers, 'Origin': 'https://wrong.example'}).status_code, 403)
        status = self.client.get(route + '?expected_boot=' + self.boot, headers=self.headers)
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.json()['reason'], 'no_receipt')
        self.assertFalse(self.client.get(route + '?expected_boot=' + str(uuid4()),
                          headers=self.headers).json()['retry_allowed'])

    def test_settings_markup_and_defaults_without_authoritative_owner(self):
        self.assertIn('name="support_threshold"', self.client.get('/recovery').text)
        self.assertNotIn('support_first_enabled', self.client.get('/recovery').text)
        self.assertEqual(self.client.get(SUPPORT, headers=self.headers).status_code, 401)
        csrf = self.login().json()['csrf']
        self.assertEqual(self.client.get(SUPPORT, headers=self.headers).status_code, 503)
        put = self.client.put(SUPPORT, headers={**self.headers, 'X-Recovery-CSRF': csrf},
                              json={'revision': 0, 'support_threshold': '0.45'})
        self.assertEqual(put.status_code, 503)
        self.assertFalse(self.command.exists())

    def test_explicit_owner_projection_save_auth_csrf_and_active_interlock(self):
        active = False
        prefs_path = self.root / 'support.json'
        lock = RLock()

        def observe():
            self.assertTrue(lock._is_owned())
            return prefs.SettingsContext('0.01', active)

        store = prefs.SupportStore(prefs_path, lock, observe)
        app = create_panel_app(KEY, URL, self.channel, support_store=store)
        self.client = TestClient(app, base_url=URL)
        csrf = self.login().json()['csrf']
        get = self.client.get(SUPPORT, headers=self.headers)
        self.assertEqual(get.json(), {'revision': 0, 'editable': True, 'support_threshold': '0.40'})
        payload = {'revision': 0, 'support_threshold': '0.450'}
        self.assertEqual(self.client.put(SUPPORT, headers=self.headers, json=payload).status_code, 403)
        headers = {**self.headers, 'X-Recovery-CSRF': csrf}
        self.assertEqual(self.client.put(SUPPORT, headers={**headers, 'Origin': 'https://wrong.example'},
                                         json=payload).status_code, 403)
        self.assertEqual(self.client.put(SUPPORT, headers={**headers, 'Authorization': 'Bearer ' + KEY},
                                         json=payload).status_code, 401)
        self.assertEqual(self.client.post(SUPPORT, headers=headers, json=payload).status_code, 403)
        self.assertFalse(prefs_path.exists())
        result = self.client.put(SUPPORT, headers=headers, json=payload)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json(), {'revision': 1, 'editable': True, 'support_threshold': '0.450'})
        self.assertEqual(json.loads(prefs_path.read_text())['settings'], {'support_threshold': '0.450'})
        with lock:
            active = True
        self.assertFalse(self.client.get(SUPPORT, headers=self.headers).json()['editable'])
        self.assertEqual(self.client.put(SUPPORT, headers=headers, json={
            'revision': 1, 'support_threshold': '0.50',
        }).status_code, 409)
        self.assertFalse(self.command.exists())

    def test_logout_denies_following_request(self):
        csrf = self.login().json()['csrf']
        response = self.client.post(SESSION + '/logout',
                                    headers={**self.headers, 'X-Recovery-CSRF': csrf}, json={})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get(EA + '/state', headers=self.headers).status_code, 401)


if __name__ == '__main__':
    main()
