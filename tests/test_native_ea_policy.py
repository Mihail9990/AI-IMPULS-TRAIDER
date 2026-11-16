"""Compile and execute the exact terminal-independent MQL policy, not a rewrite."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

class NativePolicyTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('g++'), 'C++ compiler required for shared policy test')
    def test_actual_mqh_with_cpp_math_shim(self):
        with tempfile.TemporaryDirectory() as folder:
            binary = str(Path(folder) / 'policy')
            subprocess.run(['g++', '-std=c++17', '-Wall', '-Wextra', '-Werror',
                            str(ROOT/'tests/native/policy.cpp'), '-o', binary], check=True)
            subprocess.run([binary], check=True, timeout=10)
