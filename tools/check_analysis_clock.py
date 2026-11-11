r"""Offline cross-interpreter analytics clock probe. Never imports or starts MT5.

Windows operator check:
  py -3.13 tools/check_analysis_clock.py --worker-python .venv-analysis-313\Scripts\python.exe
Run after a release restart, with the real owner's Python and installed analysis
interpreter. This does not install packages or read LIVE settings/accounts.
"""
import argparse
import json
import math
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tradfi.analysis_service import analysis_clock, proof_status


def sample():
    before = time.perf_counter()
    value = analysis_clock()
    after = time.perf_counter()
    info = time.get_clock_info('perf_counter')
    return dict(python='.'.join(map(str, sys.version_info[:3])), platform=sys.platform,
                implementation=info.implementation, monotonic=info.monotonic,
                adjustable=info.adjustable, clock=value,
                uses_perf_counter=before <= value <= after)


def check(executable):
    parent = sample()
    observed = analysis_clock()
    proof = dict(connected=True, status='FRESH', timestamp_semantics='VERIFIED',
                 monotonic=observed, age=0, max_age=5)
    result = subprocess.run([str(Path(executable).resolve()), '-I', str(Path(__file__).resolve()), '--sample'],
                            input=json.dumps(proof), text=True, encoding='utf-8',
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10, check=True)
    received = analysis_clock()
    child = json.loads(result.stdout)
    age = child['clock'] - observed
    valid = (parent['uses_perf_counter'] and child['uses_perf_counter']
             and parent['monotonic'] and child['monotonic']
             and not parent['adjustable'] and not child['adjustable']
             and parent['implementation'] == child['implementation']
             and math.isfinite(age) and observed <= child['clock'] <= received
             and child['proof_status'] == 'READY')
    # No executable path, account/configuration, secret or raw native output.
    for value in (parent, child): value.pop('clock', None)
    return dict(passed=bool(valid), parent=parent, worker=child,
                proof_age_seconds=age, round_trip_seconds=received-observed,
                native_mt5_started=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--worker-python')
    parser.add_argument('--sample', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.sample:
        proof = json.loads(sys.stdin.read(2048))
        value = sample(); value['proof_status'] = proof_status(proof, value['clock'])
        print(json.dumps(value, allow_nan=False)); return 0
    if not args.worker_python: parser.error('--worker-python is required')
    try: result = check(args.worker_python)
    except Exception:
        result = dict(passed=False, reason='CLOCK_PROBE_FAILED', native_mt5_started=False)
    print(json.dumps(result, ensure_ascii=True, indent=2, allow_nan=False))
    return 0 if result['passed'] else 1


if __name__ == '__main__': raise SystemExit(main())
