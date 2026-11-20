from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
from threading import Barrier
import unittest
from uuid import uuid4

from fastapi.testclient import TestClient

from channel import Channel, VERSION, digest
from web_handoff import create_handoff_app


TOKEN = 'test-token' * 4
API = '/api/recovery/ea'


class TestHandoff(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.binding = {name: str(uuid4()) for name in ('boot_id', 'context_id', 'generation_id')}
        self.scope = str(uuid4())
        self.state = dict(version=VERSION, scope_id=self.scope, mode='ready',
                          internal='not-for-browser', **self.binding)
        self.make_session(self.binding['boot_id'])
        self.put_state()
        self.channel = Channel(self.root, self.scope, clock=lambda: 42)
        self.client = self.make_client()
        self.req = dict(request_id=str(uuid4()), expected_boot=self.binding['boot_id'],
                        expected_context=self.binding['context_id'],
                        expected_generation=self.binding['generation_id'], confirmed=True)
        self.command_file = self.session / 'commands' / (self.req['request_id'] + '.json')
        self.receipt_file = self.session / 'receipts' / (self.req['request_id'] + '.json')

    def make_session(self, boot):
        self.session = self.root / 'sessions' / boot
        for name in ('commands', 'receipts'):
            (self.session / name).mkdir(parents=True)

    def put_state(self):
        (self.root / 'state.json').write_text(json.dumps(self.state))

    def make_client(self, channel=None):
        return TestClient(create_handoff_app(TOKEN, channel or self.channel),
                          headers={'Authorization': 'Bearer ' + TOKEN})

    def submit(self, op='manual_wait', **delta):
        return self.client.post(f'{API}/{op}', json={**self.req, **delta})

    def poll(self):
        return self.client.get(f"{API}/commands/{self.req['request_id']}",
                               params={'expected_boot': self.req['expected_boot']})

    def receipt(self, status='applied', **delta):
        c = json.loads(self.command_file.read_text())
        row = {k: c[k] for k in ('version', 'scope_id', 'command_id', 'boot_id', 'context_id',
                                  'generation_id', 'content_sha256')}
        self.receipt_file.write_text(json.dumps(dict(row, status=status, internal='not-for-browser', **delta)))

    def test_auth_and_state_projection(self):
        anonymous = TestClient(create_handoff_app(TOKEN, self.channel))
        self.assertEqual(anonymous.get(f'{API}/state').status_code, 401)
        self.assertEqual(anonymous.post(f'{API}/manual_wait', json=self.req).status_code, 401)
        r = self.client.get(f'{API}/state')
        self.assertEqual(r.json(), dict(version=1, mode='ready', **self.binding))
        self.assertEqual(r.headers['cache-control'], 'no-store')
        self.assertFalse(self.command_file.exists())

    def test_exact_wire_and_publication_is_not_applied(self):
        response = self.submit()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()['dispatch'], 'published')
        self.assertEqual(response.json()['status'], 'unknown')
        self.assertEqual(response.json()['reason'], 'no_receipt')
        wire = json.loads(self.command_file.read_text())
        sha = wire.pop('content_sha256')
        self.assertEqual(wire, dict(version=1, scope_id=self.scope, **self.binding,
                                   command_id=self.req['request_id'], created_at=42,
                                   operation='manual_wait', payload={}))
        self.assertEqual(sha, digest(wire))

    def test_web_restart_keeps_status_only_for_same_ea_session(self):
        self.submit('manual_cancel')
        initial = self.command_file.read_bytes()
        self.receipt('accepted')
        self.client = self.make_client(Channel(self.root, self.scope))
        self.assertEqual(self.submit('manual_cancel').json()['status'], 'accepted')
        self.assertEqual(self.command_file.read_bytes(), initial)
        self.receipt('applied')
        self.assertEqual(self.poll().json(), dict(request_id=self.req['request_id'],
                         dispatch='published', status='applied', reason=None, retry_allowed=False))
        self.assertEqual(self.submit().status_code, 409)

    def test_ea_restart_uses_new_empty_queue_and_ignores_previous_data(self):
        self.submit('manual_cancel')
        self.receipt('applied')
        old_record = self.command_file.read_bytes()
        next_boot = str(uuid4())
        self.make_session(next_boot)
        self.state['boot_id'] = next_boot
        self.put_state()
        self.client = self.make_client(Channel(self.root, self.scope))
        self.assertEqual(self.submit('manual_cancel').json()['dispatch'], 'unknown')
        self.assertEqual(self.poll().json()['status'], 'unknown')
        self.assertEqual(list((self.session / 'commands').iterdir()), [])
        self.assertEqual(list((self.session / 'receipts').iterdir()), [])
        # An independently confirmed action can use the current session immediately.
        self.assertEqual(self.submit(expected_boot=next_boot).json()['dispatch'], 'published')
        self.assertEqual(self.poll().json()['status'], 'unknown')
        self.assertEqual(self.command_file.read_bytes(), old_record)
        self.assertEqual(len(list((self.session / 'commands').iterdir())), 1)
        self.assertEqual(self.client.get(f"{API}/commands/{self.req['request_id']}").status_code, 422)

    def test_legacy_flat_queue_is_not_a_fallback(self):
        for category, path in [('commands', self.command_file), ('receipts', self.receipt_file)]:
            (self.root / category).mkdir()
            (self.root / category / path.name).write_text('{"status":"applied"}')
        self.assertEqual(self.poll().json()['reason'], 'no_command_record')
        self.assertEqual(self.submit().json()['dispatch'], 'published')
        self.assertTrue(self.command_file.exists())

    def test_stale_boundary_cannot_publish(self):
        r = self.submit(expected_generation=str(uuid4()))
        self.assertEqual(r.json()['status'], 'unknown')
        self.assertFalse(self.command_file.exists())
        self.state['mode'] = 'unknown'
        self.put_state()
        self.assertFalse(self.submit().json()['retry_allowed'])
        self.assertFalse(self.command_file.exists())

    def test_binding_mismatch_never_passes_applied(self):
        self.submit()
        self.receipt()
        wrong = json.loads(self.receipt_file.read_text())
        wrong['generation_id'] = str(uuid4())
        self.receipt_file.write_text(json.dumps(wrong))
        p = self.poll().json()
        self.assertEqual(p['status'], 'unknown')
        self.assertEqual(p['reason'], 'receipt_unverified')
        self.assertFalse(p['retry_allowed'])

    def test_corrupt_command_not_replaced(self):
        self.command_file.write_text('{}')
        self.assertEqual(self.poll().json()['reason'], 'command_unverified')
        self.assertEqual(self.submit().json()['reason'], 'command_unverified')
        self.assertEqual(self.command_file.read_text(), '{}')

    def test_failure_after_publish_reads_immutable_file(self):
        class PublicationLost(Channel):
            def publish(self, *args, **kwargs):
                super().publish(*args, **kwargs)
                raise OSError('lost outward acknowledgement')
        self.client = self.make_client(PublicationLost(self.root, self.scope))
        r = self.submit()
        self.assertEqual(r.json()['dispatch'], 'published')
        self.assertEqual(r.json()['status'], 'unknown')
        self.assertEqual(len(list((self.session / 'commands').iterdir())), 1)

    def test_concurrent_conflicting_ui_requests_cannot_overwrite(self):
        barrier = Barrier(2)

        class ConcurrentPublication(Channel):
            def publish(self, *args, **kwargs):
                barrier.wait(timeout=3)
                return super().publish(*args, **kwargs)

        channel = ConcurrentPublication(self.root, self.scope)
        first, second = self.make_client(channel), self.make_client(channel)
        with ThreadPoolExecutor(max_workers=2) as tasks:
            pending = [tasks.submit(client.post, f'{API}/{op}', json=self.req)
                       for client, op in ((first, 'manual_wait'), (second, 'manual_cancel'))]
            results = [task.result() for task in pending]
        self.assertEqual(sorted(r.status_code for r in results), [202, 409])
        self.assertEqual(len(list((self.session / 'commands').iterdir())), 1)
        winner = ('manual_wait' if results[0].status_code == 202 else 'manual_cancel')
        self.assertEqual(json.loads(self.command_file.read_text())['operation'], winner)

    def test_unsupported_and_invalid_commands(self):
        self.assertEqual(self.submit('configure_ranges').status_code, 404)
        self.assertEqual(self.submit(confirmed=1).status_code, 422)
        self.assertEqual(self.submit(request_id='insecure').status_code, 422)
        self.assertEqual(self.submit(payload={}).status_code, 422)
        raw = json.dumps(self.req)[:-1] + ',"confirmed":true}'
        self.assertEqual(self.client.post(f'{API}/manual_wait', content=raw).status_code, 422)
        self.assertEqual(self.client.post(f'{API}/manual_wait', content=' ' * 4097).status_code, 413)
        self.assertFalse(self.command_file.exists())


if __name__ == '__main__':
    unittest.main()
