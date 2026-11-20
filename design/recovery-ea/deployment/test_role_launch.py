from unittest import TestCase, main
from unittest.mock import patch

from role_launch import RoleLaunch, executor_epoch, prepare_role_launch

SCOPE = '00112233-4455-6677-8899-aabbccddeeff'
CONTEXT = dict(login=123, server='Test-Broker', symbol='XAUUSD.s', magic=321)


class RoleLaunchTest(TestCase):
    def test_mql_wire_sha256_fixture_and_binding(self):
        self.assertEqual(executor_epoch(SCOPE, '0' * 64), 'aac8d6f5d492a34718f94982dcfdda71')
        plan = RoleLaunch(SCOPE, '0' * 64, 123, 'Test-Broker', 'XAUUSD.s', 321)
        verifier = plan.verifier_inputs()
        watcher = plan.watcher_inputs()
        self.assertEqual(verifier, {
            'ExpectedLogin': 123, 'ExpectedServer': 'Test-Broker', 'ExpectedSymbol': 'XAUUSD.s',
            'ExecutorEpoch': 'aac8d6f5d492a34718f94982dcfdda71', 'StrategyMagic': 321,
            'VerifierEpoch': 'db194bf8fe4d6206bfb0802c6dd1eae7',
        })
        self.assertEqual(watcher, {
            'ExpectedLogin': 123, 'ExpectedServer': 'Test-Broker', 'ExpectedSymbol': 'XAUUSD.s',
            'ExecutorEpoch': 'aac8d6f5d492a34718f94982dcfdda71',
            'WatcherEpoch': 'c0768d8a38c8ddd1246ee0a9cb9b4d87',
        })
        owner = plan.owner_binding(owner_chart=3, verifier_chart=1, watcher_chart=2)
        self.assertEqual(owner, {
            'profile_scope': SCOPE, 'instance_seed': '0' * 64,
            'verifier_epoch': verifier['VerifierEpoch'], 'verifier_chart': 1,
            'watcher_epoch': watcher['WatcherEpoch'], 'watcher_chart': 2,
        })

    def test_new_launch_never_reads_or_preserves_seed(self):
        with patch('role_launch.secrets.token_hex', side_effect=['1'*64, '2'*64]) as rand:
            first = prepare_role_launch(SCOPE, **CONTEXT)
            second = prepare_role_launch(SCOPE, **CONTEXT)
        self.assertEqual([c.args for c in rand.call_args_list], [(32,), (32,)])
        self.assertEqual(first.profile_scope, second.profile_scope)
        ids = {first.executor_epoch, first.verifier_epoch, first.watcher_epoch,
               second.executor_epoch, second.verifier_epoch, second.watcher_epoch}
        self.assertEqual(len(ids), 6)

    def test_requires_exact_positive_private_scope_context(self):
        for bad_scope in (None, SCOPE.upper(), SCOPE.replace('-', ''), '../scope'):
            with self.subTest(scope=bad_scope), self.assertRaises(ValueError):
                prepare_role_launch(bad_scope, **CONTEXT)
        for bad in ('', 'a' * 63, 'A' * 64, 'a' * 64 + '\n'):
            with self.subTest(seed=bad), self.assertRaises(ValueError):
                executor_epoch(SCOPE, bad)
        for bad in ({'login': True}, {'login': 1 << 63}, {'server': 'Bad\tServer'},
                    {'server': '😀' * 51}, {'symbol': ''}, {'magic': 0}):
            with self.subTest(context=bad), self.assertRaises(ValueError):
                prepare_role_launch(SCOPE, **{**CONTEXT, **bad})

    def test_rejects_missing_duplicate_and_not_exact_chart_ids(self):
        plan = prepare_role_launch(SCOPE, **CONTEXT)
        for charts in [(1, 1, 3), (1, 2, 1), (1, 2, 2), (1, 0, 3), (1, 2, True), ('1', 2, 3)]:
            with self.subTest(charts=charts), self.assertRaises(ValueError):
                plan.owner_binding(owner_chart=charts[0], verifier_chart=charts[1], watcher_chart=charts[2])


if __name__ == '__main__':
    main()
