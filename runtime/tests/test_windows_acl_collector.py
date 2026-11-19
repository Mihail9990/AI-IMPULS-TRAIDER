"""Regression check for the exact directory expression used by the collector."""

import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest


class WindowsACLCollectorRegression(unittest.TestCase):
    def test_type_test_is_applied_to_parent_chain_and_children(self):
        code = (Path(__file__).resolve().parents[1] / 'native_evidence_preflight.py').read_text()
        self.assertIn('$cursor=$cursor.Parent', code)
        self.assertNotIn('.PSIsContainer', code)
        self.assertIn('if($child -is [IO.DirectoryInfo])', code)
        self.assertIn('kind=$(if($item -is [IO.DirectoryInfo])', code)

    @unittest.skipUnless(os.name == 'nt', 'Requires Windows PowerShell')
    def test_provider_leaf_plain_parent_and_file(self):
        code = (Path(__file__).resolve().parents[1] / 'native_evidence_preflight.py').read_text()
        expression = re.search(r'kind=\$\((if\(\$item .+?else\{\'file\'\})\);', code).group(1)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'leaf').mkdir()
            (root / 'leaf' / 'untrusted_name').write_text('')
            script = (
                "$ErrorActionPreference='Stop'; "
                "function Get-Kind($item){ " + expression + " }; "
                "$leaf=Get-Item -LiteralPath $env:RECOVERY_TEST_LEAF; "
                "Get-Kind $leaf; Get-Kind ($leaf.Parent); "
                "Get-Kind (Get-Item -LiteralPath $env:RECOVERY_TEST_FILE)"
            )
            out = subprocess.run(
                ['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
                env={**os.environ, 'RECOVERY_TEST_LEAF': str(root/'leaf'),
                     'RECOVERY_TEST_FILE': str(root/'leaf'/'untrusted_name')},
                capture_output=True, text=True, check=True, timeout=20,
            )
            self.assertEqual(out.stdout.split(), ['directory', 'directory', 'file'])
