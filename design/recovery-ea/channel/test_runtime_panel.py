import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, main
from uuid import uuid4

from fastapi.testclient import TestClient

from channel import Channel
from runtime_panel import create_panel_app

URL = 'https://vps.example'
EA = '/api/recovery/ea'
SESSION = '/api/recovery/session'
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
            'payload': {'ranges': [{'range_id': 2, 'side': 'SELL', 'price': '3999.850'}]},
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
        self.assertIn('Подтвердить и отправить', home.text)
        self.assertIn("frame-ancestors 'none'", home.headers['content-security-policy'])
        self.assertEqual(home.headers['cache-control'], 'no-store')
        for file in ('runtime_panel.mjs', 'runtime_panel.css', 'recovery_ranges.mjs', 'session.js'):
            route = '/recovery/assets/' + file
            asset = self.client.get(route)
            self.assertEqual(asset.status_code, 200)
            self.assertTrue(asset.headers['content-type'].startswith(
                'text/css' if file.endswith('.css') else 'text/javascript'))
            self.assertNotIn(KEY, asset.text)
        self.assertNotIn(KEY, home.text)
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
            (self.headers, EA + '/configure_ranges'),
            ({**self.headers, 'X-Recovery-CSRF': 'incorrect'}, EA + '/configure_ranges'),
            ({**self.headers, 'Origin': 'https://wrong.example', 'X-Recovery-CSRF': csrf},
             EA + '/configure_ranges'),
            ({**self.headers, 'Sec-Fetch-Site': 'cross-site', 'X-Recovery-CSRF': csrf},
             EA + '/configure_ranges'),
            ({**self.headers, 'X-Recovery-CSRF': csrf}, EA + '/configure_ranges?x=1'),
            ({**self.headers, 'X-Recovery-CSRF': csrf}, EA + '/unknown'),
        ]
        for headers, route in bad_cases:
            response = self.client.post(route, headers=headers, json=self.request)
            self.assertIn(response.status_code, (400, 403))
            self.assertFalse(self.command.exists())
        self.assertEqual(self.client.get(EA + '/state?x=1', headers=self.headers).status_code, 400)

    def test_browser_configure_and_exact_status_query(self):
        csrf = self.login().json()['csrf']
        response = self.client.post(EA + '/configure_ranges',
                    headers={**self.headers, 'X-Recovery-CSRF': csrf}, json=self.request)
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()['status'], 'unknown')
        self.assertEqual(json.loads(self.command.read_text())['payload'], self.request['payload'])
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

    def test_logout_denies_following_request(self):
        csrf = self.login().json()['csrf']
        response = self.client.post(SESSION + '/logout',
                                    headers={**self.headers, 'X-Recovery-CSRF': csrf}, json={})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get(EA + '/state', headers=self.headers).status_code, 401)


if __name__ == '__main__':
    main()
