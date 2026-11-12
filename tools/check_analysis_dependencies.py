"""Inspect optional analysis packages without MT5, networking or installation.

The default mode reads distribution metadata only. --smoke additionally runs
the analysis adapters on explicitly synthetic, closed historical test bars.
It never connects a terminal or reads LIVE configuration/state.
"""
from __future__ import annotations

import argparse
import contextlib
import os
import re
import importlib
import importlib.metadata
import json
import platform
from pathlib import Path
import struct
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tradfi.analysis_runtime import supported_runtime

PACKAGES = {'smc': 'smartmoneyconcepts'}


def inspect_packages():
    result = {}
    for module, package in PACKAGES.items():
        try:
            version = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            result[module] = {'status': 'UNAVAILABLE', 'package': package}
        except Exception:
            result[module] = {'status': 'ERROR', 'package': package,
                              'reason': 'PACKAGE_METADATA_UNAVAILABLE'}
        else:
            result[module] = {'status': 'PRESENT', 'package': package, 'version': version}
    return result


def smoke_check():
    # Tests only: deliberately no MT5 import, real account or external data.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from tradfi.analysis_adapters import analyze_bars
    start = 1704067200  # 2024-01-01T00:00:00Z, not a current market claim.
    bars = []
    for index in range(180):
        price = 2000 + (index % 17) * .25 + index * .01
        close = price + (.35 if index % 3 else -.2)
        bars.append({'raw_time': start + index * 60, 'utc_time': start + index * 60,
                     'open': price, 'high': max(price, close) + .15,
                     'low': min(price, close) - .15, 'close': close,
                     'tick_volume': 100 + index % 30, 'real_volume': 0})
    with open(os.devnull, 'w', encoding='utf-8', errors='replace') as quiet, contextlib.redirect_stdout(quiet):
        result = analyze_bars(bars, 'M1', start + len(bars) * 60)
    # Prove the renderer's transport contract rejects NaN and Infinity.
    json.dumps(result, allow_nan=False)
    native = {'status': 'NOT_APPLICABLE', 'initialized': False}
    if sys.platform == 'win32':
        mt5 = importlib.import_module('MetaTrader5')
        native = {'status': 'READY' if all(callable(getattr(mt5, name, None)) for name in ('initialize', 'shutdown', 'account_info', 'terminal_info', 'copy_rates_from_pos')) else 'ERROR', 'initialized': False}
    return {'input': 'SYNTHETIC_OFFLINE_TEST_ONLY', 'native_binding_import': native, 'modules': result['modules'],
            'counts': {key: len(value) for key, value in result['overlays'].items()}}


def inspect_lock():
    requirements = Path(__file__).resolve().parents[1] / 'requirements-analysis.txt'
    mismatches = []
    for line in requirements.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#'): continue
        if ';' in line:
            line, marker = line.split(';', 1)
            if marker.strip() != 'sys_platform == "win32"': raise ValueError('UNSUPPORTED_LOCK_MARKER')
            if sys.platform != 'win32': continue
        match = re.fullmatch(r'([A-Za-z0-9_.-]+)==([A-Za-z0-9_.+-]+)', line.strip())
        if not match: raise ValueError('UNPINNED_DEPENDENCY')
        package, expected = match.groups()
        try: actual = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError: actual = None
        if actual != expected: mismatches.append({'package': package, 'expected': expected, 'actual': actual})
    return {'matches': not mismatches, 'mismatches': mismatches}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--smoke', action='store_true',
                        help='Run installed adapters on synthetic offline bars; never connect MT5.')
    parser.add_argument('--require-lock', action='store_true', help='Require exact isolated-environment dependency pins.')
    args = parser.parse_args(argv)
    report = {'python': platform.python_version(), 'bits': struct.calcsize('P') * 8,
              'platform': sys.platform, 'modules': inspect_packages(),
              'installed_or_changed': False, 'terminal_connected': False}
    passed = all(value['status'] == 'PRESENT' for value in report['modules'].values())
    if args.require_lock:
        report['lock'] = inspect_lock()
        report['runtime_supported'] = supported_runtime()
        passed = passed and report['lock']['matches'] and report['runtime_supported']
    if args.smoke:
        try:
            report['smoke'] = smoke_check()
            passed = passed and all(report['smoke']['modules'].get(key, {}).get('status') == 'READY'
                                    for key in PACKAGES)
            passed = passed and report['smoke'].get('native_binding_import', {}).get('status') != 'ERROR'
            components = report['smoke']['modules'].get('smc', {}).get('components', {})
            passed = passed and all(components.get(key, {}).get('status') == 'READY' for key in ('swings', 'fvg', 'bos_choch', 'liquidity'))
        except Exception:
            report['smoke'] = {'status': 'ERROR', 'reason': 'OFFLINE_ADAPTER_SMOKE_FAILED'}
            passed = False
    report['check_passed'] = passed
    report['windows_vendor_smoke_verified'] = bool(passed and args.smoke and args.require_lock and sys.platform == 'win32')
    report['windows_release_verified'] = False  # No live MT5/multi-client runtime test here.
    print(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False))
    return 0 if passed else 2


if __name__ == '__main__':
    raise SystemExit(main())
