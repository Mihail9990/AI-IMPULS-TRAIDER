"""Explicit operator setup for isolated Windows Python 3.12 x64 analytics.

Never invoked by the LIVE installer. No trading configuration, broker calls,
terminal initialization or automatic panel launch is performed here.
"""
import argparse
import hashlib
import re
import json
import os
from pathlib import Path
import struct
import subprocess
import sys


def run(args, env):
    result = subprocess.run(args, env=env, shell=False, timeout=900)
    if result.returncode: raise RuntimeError('ANALYSIS_SETUP_STEP_FAILED')


def resolve_target(managed_root, local_root):
    if managed_root is None: return local_root, None
    root = Path(managed_root)
    if not root.is_absolute(): raise ValueError('ABSOLUTE_MANAGED_ROOT_REQUIRED')
    pointer = root / 'current.json'
    if not pointer.is_file() or pointer.stat().st_size > 8192: raise ValueError('INVALID_MANAGED_POINTER')
    current = json.loads(pointer.read_text())
    sha = current.get('current') if isinstance(current, dict) else None
    if not isinstance(sha, str) or not re.fullmatch(r'[0-9a-f]{40}', sha): raise ValueError('INVALID_RELEASE_SHA')
    code = root / 'releases' / sha / 'code'
    if code.resolve() != code.absolute() or not code.is_dir(): raise ValueError('INVALID_RELEASE_DIRECTORY')
    contract = code / 'independent-install-contract.json'
    if not contract.is_file() or contract.stat().st_size > 8192: raise ValueError('INVALID_RELEASE_CONTRACT')
    if json.loads(contract.read_text()) != {'format': 2, 'roles': ['live'], 'layout': 'independent_roots_v1'}:
        raise ValueError('INVALID_RELEASE_CONTRACT')
    for required in ('requirements-analysis.txt', 'tools/check_analysis_dependencies.py', 'tradfi/analysis_process.py', 'tools/analysis_worker.py'):
        if not (code / required).is_file(): raise ValueError('ACTIVE_RELEASE_HAS_NO_ANALYSIS_SUPPORT')
    return code, (pointer, sha)


def main(argv=None):
    if sys.platform != 'win32' or sys.version_info[:2] != (3, 12) or struct.calcsize('P') != 8:
        print('Requires separately installed official Python 3.12 x64 on Windows. LIVE Python is not modified.')
        return 2
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--managed-root', help='Explicit absolute AI_IMPULS_LIVE install root; reads current.json only.')
    args = parser.parse_args(argv)
    root, binding = resolve_target(args.managed_root, Path(__file__).resolve().parents[1])
    print('Optional analysis target: ' + str(root / '.venv-analysis'))
    sys.path.insert(0, str(root))
    from tradfi.analysis_process import clean_environment, validate_python
    env = clean_environment();env['PIP_CONFIG_FILE'] = os.devnull
    target = root / '.venv-analysis'
    python = target / 'Scripts' / 'python.exe'
    requirements = root / 'requirements-analysis.txt'
    if target.is_symlink() or (hasattr(target, 'is_junction') and target.is_junction()):
        raise ValueError('ANALYSIS_TARGET_MUST_NOT_REDIRECT')
    if target.resolve() == Path(sys.prefix).resolve() or target.resolve() != target.absolute():
        raise ValueError('ANALYSIS_TARGET_MUST_BE_SEPARATE')
    if target.exists():
        validate_python(str(python))  # Never overwrite an unrelated directory.
    else:
        run([sys.executable, '-I', '-m', 'venv', str(target)], env)
    validate_python(str(python))
    run([str(python), '-I', '-m', 'pip', 'install', '--index-url', 'https://pypi.org/simple',
         '--only-binary=:all:', '--disable-pip-version-check', '--no-input',
         '-r', str(requirements)], env)
    run([str(python), '-I', '-m', 'pip', 'check'], env)
    run([str(python), '-I', str(root / 'tools' / 'check_analysis_dependencies.py'), '--smoke', '--require-lock'], env)
    if binding and json.loads(binding[0].read_text()).get('current') != binding[1]:
        raise ValueError('ACTIVE_RELEASE_CHANGED_DURING_SETUP')
    # Marker is evidence only; it never enables trading or modifies config.
    marker = {'python': '3.12', 'bits': 64, 'synthetic_smoke_passed': True,
              'requirements_sha256': hashlib.sha256(requirements.read_bytes()).hexdigest(),
              'terminal_connected': False}
    (target / 'analysis-smoke.json').write_text(json.dumps(marker, indent=2), encoding='utf-8')
    return 0


if __name__ == '__main__':
    try: raise SystemExit(main())
    except Exception:
        print('Optional analysis setup failed. LIVE environment/settings were not changed.')
        raise SystemExit(2) from None
