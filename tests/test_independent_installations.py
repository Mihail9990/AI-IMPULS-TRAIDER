"""Offline installation isolation and restartable legacy-migration regressions.

All releases come from in-memory archives. Python preparation, child processes,
and network access are replaced; no MT5, pip, GUI, or trading service is started.
"""
from contextlib import ExitStack, closing, contextmanager
import json
import os
from pathlib import Path
import runpy
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

import independent_install as independent
import install_tradfi as legacy
from tradfi import managed

try:
    from tests import test_installer as fixtures
except ImportError:
    import test_installer as fixtures


A = fixtures.A
B = fixtures.B
SOURCE = fixtures.SOURCE


def archive(sha):
    """Extend the original installer fixture with the independent contract."""
    names = (
        'independent-install-contract.json', 'independent_install.py',
        'tradfi_paper_config.example.json', 'install_tradfi.py',
    )
    return fixtures.archive(sha, [
        ('AI-IMPULS-TRAIDER-' + sha + '/' + name, (SOURCE / name).read_bytes())
        for name in names
    ])


def database_dump(path):
    with closing(sqlite3.connect(Path(path).as_uri() + '?mode=ro', uri=True)) as db:
        return list(db.iterdump())


class IndependentInstallationsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.shas = {'live': A, 'paper': A, 'legacy': A}
        self.downloads = []
        self.messages = []
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        self.patches.enter_context(patch.dict(os.environ, {'LOCALAPPDATA': str(self.base)}))
        # A test must explicitly mock a launch. Any accidental external work fails.
        self.patches.enter_context(patch('urllib.request.urlopen', side_effect=AssertionError('No network')))
        self.patches.enter_context(patch('subprocess.run', side_effect=AssertionError('No real commands')))
        self.patches.enter_context(patch('subprocess.Popen', side_effect=AssertionError('No real processes')))
        self.live = self.manager('live')
        self.paper = self.manager('paper')

    def manager(self, role, root=None):
        def fetch(url, limit=legacy.MAX_DOWNLOAD):
            self.downloads.append((role, url))
            sha = self.shas[role]
            if '/git/ref/' in url:
                return json.dumps({
                    'ref': 'refs/heads/work',
                    'object': {'type': 'commit', 'sha': sha},
                }).encode()
            return archive(sha)

        manager = independent.IndependentManager(
            root or self.base / role, role, fetch=fetch,
            runner=Mock(side_effect=AssertionError('No dependency installation')),
            report=self.messages.append,
        )
        self.patches.enter_context(patch.object(manager, 'prepare_python', side_effect=self.fake_python))
        return manager

    @staticmethod
    def fake_python(release):
        python = release / 'venv' / 'Scripts' / 'python.exe'
        python.parent.mkdir(parents=True, exist_ok=True)
        python.write_bytes(b'offline-placeholder')

    def install_pair(self):
        self.live.install()
        self.paper.install()
        self.passwords()

    def passwords(self):
        for manager in (self.live, self.paper):
            config = manager.config()
            config['web']['password'] = 'own-' + manager.role + '-password'
            legacy.atomic_json(manager.config_path, config)

    @staticmethod
    def live_state(manager):
        path = legacy.state_path(manager.root, manager.config())
        snapshot = dict(
            schema=1, backend='mt5_direct_v1', ledger_contract='deal_ledger_v1',
            scope_contract='cycle_scope_v1', phase='PAUSED', cycle=None,
            commands={}, seen_deals=[], preserved_live_identity='LIVE-original',
        )
        with closing(sqlite3.connect(path)) as db, db:
            db.execute('CREATE TABLE snapshot(id INTEGER PRIMARY KEY,payload TEXT)')
            db.execute('CREATE TABLE mt5_commands(id TEXT PRIMARY KEY,payload TEXT)')
            db.execute('CREATE TABLE consumed_deals(ticket TEXT PRIMARY KEY,payload TEXT)')
            db.execute('INSERT INTO snapshot VALUES(1,?)', (json.dumps(snapshot),))
        return path

    @staticmethod
    def paper_state(manager, instance=None):
        from tradfi.paper import PaperSession
        config = manager.config(instance) if instance else manager.config()
        path = legacy.state_path(manager.root, config)
        reader = Mock(_expected_account='123@Bybit-Live-6', market_time_offset_seconds=0)
        session = PaperSession(path, reader, clock=lambda: 1, monotonic=lambda: 1)
        try:
            session.exchange.p.update(
                paused_gap=True,
                order_history=[{'id': 'paper-original-order', 'status': 'CANCELLED'}],
                commission_per_lot_side='2.5',
            )
            session.exchange.persist()
            identity = session.exchange.account
        finally:
            session.close()
        return path, identity

    @contextmanager
    def runtime(self, manager, state=None, role=None):
        code = manager.version(manager.pointer()['current']) / 'code'
        state = state or legacy.state_path(manager.root, manager.config())
        with patch.object(managed, '__file__', str(code / 'tradfi' / 'managed.py')):
            with managed.runtime_guard(state, instance=role or manager.role):
                yield

    def seed_legacy(self):
        source = legacy.Manager(
            self.base / 'legacy',
            fetch=lambda url, limit=legacy.MAX_DOWNLOAD: (
                json.dumps({'ref': 'refs/heads/work', 'object': {'type': 'commit', 'sha': A}}).encode()
                if '/git/ref/' in url else archive(A)
            ),
            report=self.messages.append,
        )
        with patch.object(source, 'prepare_python', side_effect=self.fake_python):
            source.install()
        for role in ('live', 'paper'):
            config = source.config(role)
            config.update(expected_login=123, market_time_offset_seconds=0)
            config['web']['password'] = 'legacy-' + role + '-password'
            config['preserved_setting'] = role + '-setting'
            legacy.atomic_json(source.path_for(role), config)
            log = Path(config['log_file'])
            log.write_text(role + ' current log', encoding='utf-8')
            log.with_name(log.name + '.1').write_text(role + ' rotated log', encoding='utf-8')
        live_state = self.live_state(source)
        paper_state, identity = self.paper_state(source, 'paper')
        for role, state in (('live', live_state), ('paper', paper_state)):
            legacy.atomic_json(legacy.binding_path(source.root, role), {'path': str(state)})
        return source, live_state, paper_state, identity

    def test_fresh_installations_have_own_code_environment_settings_and_pointer(self):
        self.install_pair()
        for manager in (self.live, self.paper):
            with self.subTest(role=manager.role):
                self.assertEqual(manager.config_path, manager.root / 'data' / 'service.json')
                self.assertEqual(Path(manager.config()['state_file']), manager.root / 'data' / 'state.sqlite3')
                self.assertEqual(manager.pointer(), {'current': A, 'previous': None})
                self.assertEqual(legacy.read_json(manager.root / 'application.json'), {'format': 1, 'role': manager.role})
                self.assertFalse(manager.config()['enable_trading'])
                self.assertNotIn('live_state_file', manager.config())
                self.assertFalse((manager.root / 'data' / 'paper-service.json').exists())
                self.assertTrue((manager.current_release() / 'venv' / 'Scripts' / 'python.exe').is_file())
        for name in ('independent_install.py', 'tradfi_main.py', 'tradfi/managed.py'):
            live_file = self.live.current_release() / 'code' / name
            paper_file = self.paper.current_release() / 'code' / name
            self.assertEqual(live_file.read_bytes(), paper_file.read_bytes())
            self.assertFalse(live_file.is_symlink())
            self.assertFalse(live_file.samefile(paper_file))
        live_python = self.live.current_release() / 'venv' / 'Scripts' / 'python.exe'
        paper_python = self.paper.current_release() / 'venv' / 'Scripts' / 'python.exe'
        self.assertFalse(live_python.samefile(paper_python))

    def test_launch_does_not_read_other_application_missing_or_corrupt_config(self):
        self.install_pair()
        for manager, other in ((self.live, self.paper), (self.paper, self.live)):
            original = other.config_path.read_bytes()
            for corrupt in (False, True):
                with self.subTest(role=manager.role, corrupt=corrupt):
                    if corrupt:
                        other.config_path.write_text('{invalid JSON', encoding='utf-8')
                    else:
                        other.config_path.unlink()
                    with patch('subprocess.Popen') as proc:
                        manager.launch()
                        proc.assert_called_once()
                        args = proc.call_args.args[0]
                        self.assertEqual(args[0], str(manager.current_release() / 'venv' / 'Scripts' / 'python.exe'))
                        self.assertEqual(args[args.index('--config') + 1], str(manager.config_path))
                        self.assertEqual(args[args.index('--mode') + 1], 'mt5' if manager.role == 'live' else 'paper')
                        self.assertNotIn('--execute', args)
                        self.assertEqual(proc.call_args.kwargs['cwd'], manager.root / 'data')
                        self.assertEqual(proc.call_args.kwargs['env']['TRADFI_LAUNCH_INSTANCE'], manager.role)
                    with self.runtime(manager):
                        pass
                    other.config_path.write_bytes(original)

    def test_installed_launcher_resolves_only_its_own_current_release(self):
        self.install_pair()
        self.shas['live'] = B
        self.live.install()
        for manager, other in ((self.live, self.paper), (self.paper, self.live)):
            original = other.config_path.read_bytes()
            other.config_path.write_text('unreadable peer configuration', encoding='utf-8')
            launcher = manager.root / 'launcher.py'
            batch = manager.root / ('START_' + manager.role.upper() + '.bat')
            self.assertTrue(launcher.is_file())
            self.assertIn('%~dp0launcher.py', batch.read_text())
            with patch('subprocess.call', return_value=0) as call:
                with self.assertRaises(SystemExit) as stopped:
                    runpy.run_path(str(launcher), run_name='__main__')
            self.assertEqual(stopped.exception.code, 0)
            call.assert_called_once()
            args = call.call_args.args[0]
            self.assertEqual(args[1], str(manager.current_release() / 'code' / 'independent_install.py'))
            self.assertEqual(args[args.index('--role') + 1], manager.role)
            self.assertEqual(args[args.index('--root') + 1], str(manager.root))
            other.config_path.write_bytes(original)

    def test_each_application_updates_and_rolls_back_while_other_runtime_is_held(self):
        self.install_pair()
        for manager, other in ((self.live, self.paper), (self.paper, self.live)):
            original = other.config_path.read_bytes()
            with self.subTest(role=manager.role), self.runtime(other):
                other.config_path.write_text('broken unrelated settings', encoding='utf-8')
                with legacy.lock(other.root / 'operation.lock'):
                    self.shas[manager.role] = B
                    manager.install()
                    self.assertEqual(manager.pointer()['current'], B)
                    manager.rollback()
                    self.assertEqual(manager.pointer()['current'], A)
            other.config_path.write_bytes(original)

    def test_own_runtime_lock_blocks_update_and_rollback(self):
        self.install_pair()
        for manager in (self.live, self.paper):
            self.shas[manager.role] = B
            manager.install()
            with self.subTest(role=manager.role), self.runtime(manager):
                with self.assertRaises(legacy.SetupError):
                    manager.install()
                with self.assertRaises(legacy.SetupError):
                    manager.rollback()
            self.assertEqual(manager.pointer()['current'], B)

    def test_paper_update_does_not_take_live_account_execution_lock(self):
        self.install_pair()
        config = self.live.config()
        config.update(expected_login=123, expected_server='Bybit-Live-6', symbol='XAUUSD.s')
        legacy.atomic_json(self.live.config_path, config)
        paper = self.paper.config()
        paper.update({key: config[key] for key in ('expected_login', 'expected_server', 'symbol')})
        legacy.atomic_json(self.paper.config_path, paper)
        with legacy.account_lock(self.live.root, config):
            self.shas['paper'] = B
            self.paper.install()
            self.paper.rollback()
            with self.assertRaises(legacy.SetupError):
                self.live.install()

    def test_other_role_api_and_reopening_root_under_another_role_are_rejected(self):
        self.install_pair()
        for manager, other in ((self.live, self.paper), (self.paper, self.live)):
            for operation in (
                lambda: manager.config(other.role),
                lambda: manager.path_for(other.role),
                lambda: manager.launch(instance=other.role),
                lambda: manager.request_stop('1' * 32, other.role),
            ):
                with self.subTest(role=manager.role, operation=operation), self.assertRaises(legacy.SetupError):
                    operation()
            wrong = self.manager(other.role, manager.root)
            with self.assertRaises(legacy.SetupError):
                wrong.install()
            with self.assertRaises(legacy.SetupError):
                wrong.launch()
            with self.assertRaises(legacy.SetupError):
                with self.runtime(manager, role=other.role):
                    pass

    def test_missing_role_marker_cannot_reinterpret_independent_installation_as_legacy(self):
        self.install_pair()
        for manager in (self.live, self.paper):
            marker = manager.root / 'application.json'
            before = marker.read_bytes()
            marker.unlink()
            with self.subTest(role=manager.role):
                with self.assertRaises(legacy.SetupError):
                    manager.launch()
                with self.assertRaises(legacy.SetupError):
                    manager.install()
                with self.assertRaises(legacy.SetupError):
                    with self.runtime(manager):
                        pass
            marker.write_bytes(before)

    def test_external_state_or_log_path_is_rejected_without_touching_foreign_data(self):
        self.install_pair()
        foreign = self.base / 'foreign.sqlite3'
        foreign.write_bytes(b'do not change')
        for manager in (self.live, self.paper):
            original = manager.config_path.read_bytes()
            for key in ('state_file', 'log_file'):
                for value in (str(foreign), '../../foreign.sqlite3'):
                    with self.subTest(role=manager.role, key=key, value=value):
                        config = json.loads(original)
                        config[key] = value
                        legacy.atomic_json(manager.config_path, config)
                        with self.assertRaises(legacy.SetupError):
                            manager.install()
                        with self.assertRaises(legacy.SetupError):
                            manager.launch()
                        self.assertEqual(foreign.read_bytes(), b'do not change')
            manager.config_path.write_bytes(original)
            with self.assertRaises(legacy.SetupError):
                with self.runtime(manager, state=foreign):
                    pass

    def test_fresh_install_rejects_data_directory_symlink_without_external_writes(self):
        for manager in (self.live, self.paper):
            manager.root.mkdir()
            external = self.base / ('external-' + manager.role)
            external.mkdir()
            sentinel = external / 'existing-user-data.txt'
            sentinel.write_bytes(b'preserve external user data')
            try:
                (manager.root / 'data').symlink_to(external, target_is_directory=True)
            except (OSError, NotImplementedError) as exc:
                self.skipTest('Directory symlinks are unavailable: ' + str(exc))
            with self.subTest(role=manager.role):
                with self.assertRaises(legacy.SetupError):
                    manager.install()
                self.assertEqual(sentinel.read_bytes(), b'preserve external user data')
                self.assertEqual(list(external.iterdir()), [sentinel])
                self.assertFalse((manager.root / 'current.json').exists())
                self.assertFalse((manager.root / 'managed-install.json').exists())

    def test_replaced_data_directory_symlink_blocks_config_launch_update_and_runtime(self):
        self.install_pair()
        self.live_state(self.live)
        self.paper_state(self.paper)
        for manager in (self.live, self.paper):
            data = manager.root / 'data'
            external = self.base / ('replaced-data-' + manager.role)
            data.rename(external)
            try:
                data.symlink_to(external, target_is_directory=True)
            except (OSError, NotImplementedError) as exc:
                external.rename(data)
                self.skipTest('Directory symlinks are unavailable: ' + str(exc))
            before = {path: path.read_bytes() for path in external.rglob('*') if path.is_file()}
            with self.subTest(role=manager.role):
                for operation in (manager.config, manager.launch, manager.install):
                    with self.assertRaises(legacy.SetupError):
                        operation()
                with self.assertRaises(legacy.SetupError):
                    with self.runtime(manager, state=external / 'state.sqlite3'):
                        pass
                self.assertEqual(before, {path: path.read_bytes() for path in before})
                self.assertEqual(manager.pointer()['current'], A)

    def test_paper_launch_never_inherits_live_password_or_accepts_execute(self):
        self.install_pair()
        with patch.dict(os.environ, {'TRADFI_WEB_PASSWORD': 'live-only-password'}):
            with patch('subprocess.Popen') as proc:
                self.paper.launch()
                self.assertNotIn('TRADFI_WEB_PASSWORD', proc.call_args.kwargs['env'])
            with patch('subprocess.Popen') as proc, self.assertRaises(legacy.SetupError):
                self.paper.launch(True)
            proc.assert_not_called()

    def test_pending_migration_blocks_manager_and_direct_runtime(self):
        self.install_pair()
        for manager in (self.live, self.paper):
            legacy.atomic_json(manager.root / 'migration-pending.json', {'format': 1})
            with self.subTest(role=manager.role):
                with self.assertRaises(legacy.SetupError):
                    manager.launch()
                with self.assertRaises(legacy.SetupError):
                    manager.install()
                with self.assertRaises(legacy.SetupError):
                    with self.runtime(manager):
                        pass

    def test_update_backup_and_rollback_preserve_own_data_and_other_installation(self):
        self.install_pair()
        live_state = self.live_state(self.live)
        paper_state, identity = self.paper_state(self.paper)
        paths = [live_state, paper_state, self.live.config_path, self.paper.config_path]
        for manager in (self.live, self.paper):
            log = Path(manager.config()['log_file'])
            log.write_text(manager.role + ' retained log', encoding='utf-8')
            secret = manager.root / 'data' / 'telegram.json'
            secret.write_text('local-only-' + manager.role, encoding='utf-8')
            paths.extend([log, secret])
        before = {path: path.read_bytes() for path in paths}
        for manager, other in ((self.live, self.paper), (self.paper, self.live)):
            other_pointer = other.pointer()
            self.shas[manager.role] = B
            manager.install()
            manager.rollback()
            self.assertEqual(manager.pointer()['current'], A)
            self.assertEqual(other.pointer(), other_pointer)
            self.assertEqual(before, {path: path.read_bytes() for path in paths})
            manifests = list((manager.root / 'backups').glob('*/manifest.json'))
            self.assertTrue(manifests)
            for path in manifests:
                manifest = legacy.read_json(path)
                self.assertEqual(manifest['role'], manager.role)
                self.assertTrue(manifest['state_copied'])
                self.assertFalse(manifest['credentials_copied'])
                saved = path.parent / 'state.sqlite3'
                self.assertEqual(database_dump(saved), database_dump(legacy.state_path(manager.root, manager.config())))
                self.assertFalse((path.parent / 'telegram.json').exists())
                self.assertFalse((path.parent / 'paper-state.sqlite3').exists())
                if manager.role == 'paper':
                    with closing(sqlite3.connect(saved)) as db:
                        paper = json.loads(db.execute('SELECT payload FROM paper_exchange').fetchone()[0])
                    self.assertEqual(paper['identity'], identity)
                    self.assertTrue(paper['paused_gap'])

    def test_failed_update_keeps_only_own_pointer_and_both_data_unchanged(self):
        self.install_pair()
        live_state = self.live_state(self.live)
        paper_state, _ = self.paper_state(self.paper)
        before = {path: path.read_bytes() for path in (live_state, paper_state, self.live.config_path, self.paper.config_path)}
        self.shas['paper'] = B
        with patch.object(self.paper, 'prepare_python', side_effect=legacy.SetupError('offline setup failure')):
            with self.assertRaises(legacy.SetupError):
                self.paper.install()
        self.assertEqual(self.paper.pointer()['current'], A)
        self.assertEqual(self.live.pointer()['current'], A)
        self.assertEqual(before, {path: path.read_bytes() for path in before})

    def test_bound_missing_or_moved_own_database_never_resets(self):
        self.install_pair()
        self.live_state(self.live)
        self.paper_state(self.paper)
        for manager in (self.live, self.paper):
            with self.subTest(role=manager.role):
                state = Path(manager.config()['state_file'])
                legacy.atomic_json(legacy.binding_path(manager.root, manager.role), {'path': str(state)})
                saved = state.read_bytes()
                state.unlink()
                self.shas[manager.role] = B
                with self.assertRaises(legacy.SetupError):
                    manager.install()
                with self.assertRaises(legacy.SetupError):
                    with self.runtime(manager, state=state):
                        pass
                self.assertFalse(state.exists())
                self.assertEqual(manager.pointer()['current'], A)
                state.write_bytes(saved)
                config = manager.config()
                other_path = state.with_name('different.sqlite3')
                config['state_file'] = str(other_path)
                legacy.atomic_json(manager.config_path, config)
                with self.assertRaises(legacy.SetupError):
                    manager.install()
                with self.assertRaises(legacy.SetupError):
                    with self.runtime(manager, state=other_path):
                        pass
                self.assertFalse(other_path.exists())
                self.assertEqual(state.read_bytes(), saved)

    def test_sqlite_backup_includes_committed_wal_without_changing_paper_identity(self):
        self.install_pair()
        state, identity = self.paper_state(self.paper)
        with closing(sqlite3.connect(state)) as writer:
            self.assertEqual(writer.execute('PRAGMA journal_mode=WAL').fetchone()[0], 'wal')
            writer.execute('PRAGMA wal_autocheckpoint=0')
            row = json.loads(writer.execute('SELECT payload FROM paper_exchange WHERE id=1').fetchone()[0])
            row['order_history'].append({'id': 'committed-in-wal', 'status': 'CANCELLED'})
            writer.execute('UPDATE paper_exchange SET payload=? WHERE id=1', (json.dumps(row),))
            writer.commit()
            self.assertTrue(Path(str(state) + '-wal').is_file())
            backup = self.paper.backup()
            self.assertEqual(database_dump(backup / 'state.sqlite3'), database_dump(state))
        with closing(sqlite3.connect(backup / 'state.sqlite3')) as db:
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            saved = json.loads(db.execute('SELECT payload FROM paper_exchange WHERE id=1').fetchone()[0])
        self.assertEqual(saved['identity'], identity)
        self.assertEqual(saved['order_history'][-1]['id'], 'committed-in-wal')

    def test_database_copy_rejects_open_destination_wal_without_changing_any_bytes(self):
        source = self.base / 'source.sqlite3'
        destination = self.base / 'destination.sqlite3'
        with closing(sqlite3.connect(source)) as db:
            db.execute('CREATE TABLE identity(value TEXT)')
            db.execute("INSERT INTO identity VALUES('source-identity')")
            db.commit()
        with closing(sqlite3.connect(destination)) as writer:
            writer.execute('CREATE TABLE identity(value TEXT)')
            writer.execute("INSERT INTO identity VALUES('destination-identity')")
            writer.commit()
            self.assertEqual(writer.execute('PRAGMA journal_mode=WAL').fetchone()[0], 'wal')
            writer.execute('PRAGMA wal_autocheckpoint=0')
            writer.execute("INSERT INTO identity VALUES('destination-wal-progress')")
            writer.commit()
            files = [destination, Path(str(destination) + '-wal'), Path(str(destination) + '-shm')]
            self.assertTrue(all(path.is_file() for path in files))
            before = {path: path.read_bytes() for path in files}
            with self.assertRaises(legacy.SetupError):
                independent.copy_database(source, destination)
            self.assertEqual(before, {path: path.read_bytes() for path in files})
            self.assertEqual(writer.execute('SELECT value FROM identity').fetchall(), [
                ('destination-identity',), ('destination-wal-progress',),
            ])
        self.assertFalse(list(self.base.glob('*.migration')))

    def test_database_copy_rejects_orphan_destination_sidecars_without_deleting_them(self):
        source = self.base / 'source.sqlite3'
        destination = self.base / 'destination.sqlite3'
        with closing(sqlite3.connect(source)) as db:
            db.execute('CREATE TABLE identity(value TEXT)')
            db.execute("INSERT INTO identity VALUES('source-identity')")
            db.commit()
        for suffix in ('-wal', '-shm'):
            with self.subTest(suffix=suffix):
                destination.write_bytes(b'preserve crashed destination main file')
                sidecar = Path(str(destination) + suffix)
                sidecar.write_bytes(b'preserve unexamined sidecar bytes')
                before = {path: path.read_bytes() for path in (destination, sidecar)}
                with self.assertRaises(legacy.SetupError):
                    independent.copy_database(source, destination)
                self.assertEqual(before, {path: path.read_bytes() for path in before})
                sidecar.unlink()
        self.assertFalse(list(self.base.glob('*.migration')))

    def test_rollback_rejects_old_shared_release_contract_without_switching(self):
        self.install_pair()
        for manager in (self.live, self.paper):
            self.shas[manager.role] = B
            manager.install()
            (manager.version(A) / 'code' / 'independent-install-contract.json').unlink()
            with self.subTest(role=manager.role), self.assertRaises(legacy.SetupError):
                manager.rollback()
            self.assertEqual(manager.pointer()['current'], B)

    def test_legacy_migration_preserves_source_identity_settings_logs_and_own_code(self):
        source, live_state, paper_state, identity = self.seed_legacy()
        preserved = {path: path.read_bytes() for path in source.root.rglob('*') if path.is_file()}
        originals = {role: source.config(role) for role in ('live', 'paper')}
        result = independent.migrate_legacy(source.root, self.live, self.paper)
        self.assertEqual(result, {'live': str(self.live.root), 'paper': str(self.paper.root)})
        for path, data in preserved.items():
            if path != source.root / 'managed-install.json':
                self.assertEqual(path.read_bytes(), data, str(path))
        marker = legacy.read_json(source.root / 'managed-install.json')
        self.assertEqual(marker['format'], 2)
        self.assertTrue(marker['retired'])
        self.assertEqual(marker['targets'], result)
        self.assertEqual(legacy.read_json(source.root / 'separation-migration.json')['stage'], 'completed')
        for manager, old_state in ((self.live, live_state), (self.paper, paper_state)):
            with self.subTest(role=manager.role):
                config = manager.config()
                self.assertEqual(config['web'], originals[manager.role]['web'])
                self.assertEqual(config['preserved_setting'], originals[manager.role]['preserved_setting'])
                self.assertEqual(config['market_time_offset_seconds'], originals[manager.role]['market_time_offset_seconds'])
                self.assertEqual(Path(config['state_file']), manager.root / 'data' / 'state.sqlite3')
                self.assertNotIn('live_state_file', config)
                self.assertEqual(database_dump(Path(config['state_file'])), database_dump(old_state))
                self.assertFalse(old_state.samefile(config['state_file']))
                self.assertEqual(Path(config['log_file']).read_text(), manager.role + ' current log')
                self.assertEqual(Path(config['log_file'] + '.1').read_text(), manager.role + ' rotated log')
                self.assertFalse((manager.root / 'migration-pending.json').exists())
                self.assertTrue((manager.root / 'migration-complete.json').is_file())
                self.assertEqual(legacy.read_json(legacy.binding_path(manager.root, manager.role)), {'path': config['state_file']})
                source_code = source.version(A) / 'code' / 'tradfi_main.py'
                target_code = manager.current_release() / 'code' / 'tradfi_main.py'
                self.assertEqual(source_code.read_bytes(), target_code.read_bytes())
                self.assertFalse(source_code.samefile(target_code))
                with self.runtime(manager):
                    pass
        with closing(sqlite3.connect(self.paper.config()['state_file'])) as db:
            paper = json.loads(db.execute('SELECT payload FROM paper_exchange').fetchone()[0])
        self.assertEqual(paper['identity'], identity)
        self.assertEqual(paper['order_history'], [{'id': 'paper-original-order', 'status': 'CANCELLED'}])
        self.assertTrue(paper['paused_gap'])
        with patch.object(managed, '__file__', str(source.version(A) / 'code' / 'tradfi' / 'managed.py')):
            for role, state in (('live', live_state), ('paper', paper_state)):
                with self.assertRaises(legacy.SetupError):
                    with managed.runtime_guard(state, instance=role):
                        pass

    def test_both_migration_targets_remain_blocked_until_source_is_retired(self):
        source, _, _, _ = self.seed_legacy()
        atomic_json = legacy.atomic_json
        inspected = []

        def observe_retirement(path, value):
            if Path(path) == source.root / 'managed-install.json' and value.get('retired'):
                self.assertEqual(legacy.read_json(path), {'format': 1})
                for manager in (self.live, self.paper):
                    self.assertEqual(manager.pointer()['current'], A)
                    self.assertTrue((manager.root / 'migration-pending.json').exists())
                    with self.assertRaises(legacy.SetupError):
                        manager.launch()
                    with self.assertRaises(legacy.SetupError):
                        with self.runtime(manager):
                            pass
                inspected.append(True)
            return atomic_json(path, value)

        with patch.object(legacy, 'atomic_json', side_effect=observe_retirement):
            independent.migrate_legacy(source.root, self.live, self.paper)
        self.assertEqual(inspected, [True])

    def test_failed_second_preparation_keeps_source_and_retries_without_reset(self):
        source, live_state, paper_state, _ = self.seed_legacy()
        original = {p: p.read_bytes() for p in (live_state, paper_state, source.config_path, source.path_for('paper'))}
        with patch.object(self.paper, 'prepare_python', side_effect=legacy.SetupError('paper preparation failed')):
            with self.assertRaises(legacy.SetupError):
                independent.migrate_legacy(source.root, self.live, self.paper)
        self.assertEqual(legacy.read_json(source.root / 'managed-install.json'), {'format': 1})
        self.assertEqual(original, {p: p.read_bytes() for p in original})
        for manager in (self.live, self.paper):
            self.assertFalse(manager._migrating)
            self.assertTrue((manager.root / 'migration-pending.json').exists())
            with self.assertRaises(legacy.SetupError):
                manager.launch()
        independent.migrate_legacy(source.root, self.live, self.paper)
        self.assertEqual(database_dump(self.live.root / 'data' / 'state.sqlite3'), database_dump(live_state))
        self.assertEqual(database_dump(self.paper.root / 'data' / 'state.sqlite3'), database_dump(paper_state))

    def test_retirement_write_failure_keeps_targets_blocked_and_retry_finishes(self):
        source, _, _, _ = self.seed_legacy()
        atomic_json = legacy.atomic_json

        def fail_retirement(path, value):
            if Path(path) == source.root / 'managed-install.json' and value.get('retired'):
                raise OSError('simulated retirement write failure')
            return atomic_json(path, value)

        with patch.object(legacy, 'atomic_json', side_effect=fail_retirement):
            with self.assertRaises(OSError):
                independent.migrate_legacy(source.root, self.live, self.paper)
        self.assertEqual(legacy.read_json(source.root / 'managed-install.json'), {'format': 1})
        for manager in (self.live, self.paper):
            with self.assertRaises(legacy.SetupError):
                manager.launch()
            with self.assertRaises(legacy.SetupError):
                with self.runtime(manager):
                    pass
        independent.migrate_legacy(source.root, self.live, self.paper)
        self.assertTrue(legacy.read_json(source.root / 'managed-install.json')['retired'])

    def test_retry_after_partial_activation_never_overwrites_activated_target(self):
        source, _, _, _ = self.seed_legacy()
        unlink = Path.unlink
        blocked_path = self.paper.root / 'migration-pending.json'

        def fail_paper_activation(path, *args, **kwargs):
            if path == blocked_path:
                raise OSError('simulated activation cleanup failure')
            return unlink(path, *args, **kwargs)

        with patch.object(Path, 'unlink', fail_paper_activation):
            with self.assertRaises(OSError):
                independent.migrate_legacy(source.root, self.live, self.paper)
        self.assertTrue(legacy.read_json(source.root / 'managed-install.json')['retired'])
        self.assertFalse((self.live.root / 'migration-pending.json').exists())
        self.assertTrue(blocked_path.exists())
        # The live application can now progress. Its newer state is authoritative.
        config = self.live.config()
        config['post_migration_setting'] = 'must survive retry'
        legacy.atomic_json(self.live.config_path, config)
        state = Path(config['state_file'])
        with closing(sqlite3.connect(state)) as db, db:
            snapshot = json.loads(db.execute('SELECT payload FROM snapshot WHERE id=1').fetchone()[0])
            snapshot['newer_live_progress'] = {'revision': 17}
            db.execute('UPDATE snapshot SET payload=? WHERE id=1', (json.dumps(snapshot),))
        log = Path(config['log_file'])
        with log.open('a', encoding='utf-8') as output:
            output.write('\nnew live activity')
        before = {p: p.read_bytes() for p in (self.live.config_path, state, log)}
        with self.runtime(self.live):
            independent.migrate_legacy(source.root, self.live, self.paper)
        self.assertEqual(before, {p: p.read_bytes() for p in before})
        self.assertFalse(blocked_path.exists())

    def test_migration_rejects_existing_or_overlapping_target_without_overwriting(self):
        source, live_state, paper_state, _ = self.seed_legacy()
        before = {p: p.read_bytes() for p in (live_state, paper_state, source.root / 'managed-install.json')}
        self.live.root.mkdir()
        unknown = self.live.root / 'existing-user-file.txt'
        unknown.write_text('leave alone', encoding='utf-8')
        with self.assertRaises(legacy.SetupError):
            independent.migrate_legacy(source.root, self.live, self.paper)
        self.assertEqual(unknown.read_text(), 'leave alone')
        nested = self.manager('live', source.root / 'nested-live')
        with self.assertRaises(legacy.SetupError):
            independent.migrate_legacy(source.root, nested, self.paper)
        same_root = self.manager('paper', self.live.root)
        with self.assertRaises(legacy.SetupError):
            independent.migrate_legacy(source.root, self.live, same_root)
        self.assertEqual(before, {p: p.read_bytes() for p in before})

    def test_migration_blocks_if_either_legacy_process_or_state_lock_is_held(self):
        source, live_state, paper_state, _ = self.seed_legacy()
        for lock_path in (
            source.root / 'instance-live.lock', source.root / 'instance-paper.lock',
            Path(str(live_state) + '.lock'), Path(str(paper_state) + '.lock'),
        ):
            with self.subTest(lock_path=lock_path), legacy.lock(lock_path):
                with self.assertRaises(legacy.SetupError):
                    independent.migrate_legacy(source.root, self.live, self.paper)
            self.assertEqual(legacy.read_json(source.root / 'managed-install.json'), {'format': 1})
            self.assertFalse((self.live.root / 'current.json').exists())
            self.assertFalse((self.paper.root / 'current.json').exists())

    def test_target_reservation_lock_blocks_before_source_journal_or_target_changes(self):
        source, live_state, paper_state, _ = self.seed_legacy()
        files = (source.root / 'managed-install.json', source.config_path,
                 source.path_for('paper'), live_state, paper_state)
        before = {path: path.read_bytes() for path in files}
        for manager in (self.live, self.paper):
            reservation = manager.root.parent / ('.' + manager.root.name + '.migration.lock')
            with self.subTest(role=manager.role), legacy.lock(reservation):
                with self.assertRaises(legacy.SetupError):
                    independent.migrate_legacy(source.root, self.live, self.paper)
            self.assertFalse((source.root / 'separation-migration.json').exists())
            self.assertFalse(self.live.root.exists())
            self.assertFalse(self.paper.root.exists())
            self.assertEqual(before, {path: path.read_bytes() for path in before})

    def test_changed_pending_owner_before_retirement_blocks_activation(self):
        source, live_state, paper_state, _ = self.seed_legacy()
        before = {path: path.read_bytes() for path in (live_state, paper_state)}
        atomic_json = legacy.atomic_json
        altered = []

        def replace_pending_owner(path, value):
            result = atomic_json(path, value)
            if Path(path) == self.paper.root / 'migration-complete.json':
                pending_path = self.live.root / 'migration-pending.json'
                pending = legacy.read_json(pending_path)
                pending['id'] = 'different-migration-owner'
                atomic_json(pending_path, pending)
                altered.append(True)
            return result

        with patch.object(legacy, 'atomic_json', side_effect=replace_pending_owner):
            with self.assertRaises(legacy.SetupError):
                independent.migrate_legacy(source.root, self.live, self.paper)
        self.assertEqual(altered, [True])
        self.assertEqual(legacy.read_json(source.root / 'managed-install.json'), {'format': 1})
        self.assertEqual(before, {path: path.read_bytes() for path in before})
        for manager in (self.live, self.paper):
            self.assertTrue((manager.root / 'migration-pending.json').exists())
            with self.assertRaises(legacy.SetupError):
                manager.launch()
            with self.assertRaises(legacy.SetupError):
                with self.runtime(manager):
                    pass


if __name__ == '__main__':
    unittest.main()
