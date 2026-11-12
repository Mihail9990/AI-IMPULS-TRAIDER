"""The optional dependency checker must not install or connect anything."""
import contextlib
import importlib.metadata
import io
import json
import unittest
from unittest.mock import patch

from tools import check_analysis_dependencies as check


class DependencyCheckTest(unittest.TestCase):
    def test_missing_packages_are_explicit_not_installed(self):
        with patch.object(importlib.metadata, 'version',
                          side_effect=importlib.metadata.PackageNotFoundError):
            with contextlib.redirect_stdout(io.StringIO()) as output:
                code = check.main([])
        report = json.loads(output.getvalue())
        self.assertEqual(code, 2)
        self.assertTrue(all(v['status'] == 'UNAVAILABLE' for v in report['modules'].values()))
        self.assertFalse(report['installed_or_changed'])
        self.assertFalse(report['terminal_connected'])
        self.assertNotIn('smoke', report)

    def test_present_metadata_does_not_claim_windows_compatibility(self):
        with patch.object(importlib.metadata, 'version', return_value='1.0'):
            with contextlib.redirect_stdout(io.StringIO()) as output:
                code = check.main([])
        report = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertFalse(report['windows_release_verified'])
        self.assertEqual(set(report['modules']), {'smc'})

    def test_partial_smc_fails_smoke(self):
        modules={key:{'status':'READY'} for key in check.PACKAGES}
        modules['smc']['components']={key:{'status':'READY'} for key in ('swings','fvg','bos_choch','liquidity')}
        for broken in ('swings','fvg','bos_choch','liquidity'):
            import copy
            value=copy.deepcopy(modules)
            value['smc']['components'][broken]['status']='ERROR'
            with patch.object(check,'inspect_packages',return_value={k:{'status':'PRESENT'} for k in check.PACKAGES}),patch.object(check,'smoke_check',return_value={'modules':value}):
                with contextlib.redirect_stdout(io.StringIO()):self.assertEqual(check.main(['--smoke']),2)

    def test_smc_only_smoke_passes_without_removed_modules(self):
        modules = {'smc': {'status': 'READY', 'components': {
            key: {'status': 'READY'} for key in ('swings','fvg','bos_choch','liquidity')}}}
        with patch.object(check, 'inspect_packages', return_value={'smc': {'status': 'PRESENT'}}), \
                patch.object(check, 'smoke_check', return_value={'modules': modules}):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(check.main(['--smoke']), 0)
        self.assertEqual(check.PACKAGES, {'smc': 'smartmoneyconcepts'})

    def test_smoke_error_is_bounded_public_message(self):
        with patch.object(check, 'inspect_packages', return_value={k: {'status': 'PRESENT'} for k in check.PACKAGES}), \
                patch.object(check, 'smoke_check', side_effect=RuntimeError('private exception')):
            with contextlib.redirect_stdout(io.StringIO()) as output:
                code = check.main(['--smoke'])
        self.assertEqual(code, 2)
        self.assertNotIn('private exception', output.getvalue())
        self.assertEqual(json.loads(output.getvalue())['smoke']['reason'], 'OFFLINE_ADAPTER_SMOKE_FAILED')


if __name__ == '__main__':
    unittest.main()
