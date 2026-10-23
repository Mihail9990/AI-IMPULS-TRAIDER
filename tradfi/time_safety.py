"""Independent public HTTPS UTC check; never sets the system clock.

MT5 ticks are not an independent reference. Production timestamp semantics stay
unverified until independent evidence can establish them, never by config flag.
"""
import math
import copy
import threading
import time
import requests

URL = 'https://api.bybit.com/v5/market/time'


class TimeSafety:
    def __init__(self, *, clock=time.time, monotonic=time.monotonic, get=None,
                 semantics_verifier=None, freshness=60., max_rtt=2., max_offset=2.,
                 max_uncertainty=1., jump_tolerance=1.):
        self.clock, self.monotonic = clock, monotonic
        self.get = get or requests.get
        self.semantics_verifier = semantics_verifier or (lambda: False)
        self.freshness, self.max_rtt = freshness, max_rtt
        self.max_offset, self.max_uncertainty = max_offset, max_uncertainty
        self.jump_tolerance = jump_tolerance
        self.result = None
        self.anchor = None
        self._worker = None
        self._pending = None
        self._generation = 0

    def trusted_utc(self):
        """Short-lived monotonic projection for protective quote validation only."""
        if not self.anchor or not self.result or not self.result.get('ok'):
            return None
        mono = float(self.monotonic())
        age = mono - self.result['checked_monotonic']
        if not math.isfinite(age) or not 0 <= age <= self.freshness:
            return None
        return self.anchor[0] + mono - self.anchor[1]

    def request_refresh(self):
        """One daemon at most; a stuck HTTP call cannot queue unbounded workers."""
        self.poll_refresh()
        if self._worker is not None and self._worker.is_alive():
            return None
        self._generation += 1
        token = self._generation
        worker_safety = copy.copy(self)
        worker_safety.result = None
        pending = dict(token=token, started=float(self.monotonic()), done=threading.Event(),
                       value=None, expired=False, finished=None)
        self._pending = pending
        def measure():
            try:
                worker_safety.refresh()
                pending['value'] = (worker_safety.result, worker_safety.anchor)
            finally:
                pending['finished'] = float(self.monotonic())
                pending['done'].set()
        self._worker = threading.Thread(target=measure, name='utc-check', daemon=True)
        self._worker.start()
        return token

    def poll_refresh(self):
        """Only the owner installs measurements. Late results never regain trust."""
        pending = self._pending
        if pending is None or pending['expired']:
            return None
        end = pending['finished'] if pending['done'].is_set() else float(self.monotonic())
        age = end - pending['started']
        if not math.isfinite(age) or age < 0 or age > 15.:
            pending['expired'] = True
            self.result = dict(ok=False, reason='EXTERNAL_UTC_TIMEOUT', samples=[])
            return pending['token']
        if not pending['done'].is_set():
            return None
        self._pending = None
        value = pending['value']
        if value is None:
            self.result = dict(ok=False, reason='EXTERNAL_UTC_UNAVAILABLE', samples=[])
        else:
            self.result, self.anchor = value
        return pending['token']

    def _clocks(self):
        wall, mono = float(self.clock()), float(self.monotonic())
        if not math.isfinite(wall) or not math.isfinite(mono) or wall <= 0 or mono < 0:
            raise ValueError('INVALID_LOCAL_CLOCK')
        return wall, mono

    def check(self):
        try:
            wall, mono = self._clocks()
            if self.anchor:
                old_wall, old_mono = self.anchor
                if mono < old_mono or abs(wall-old_wall-(mono-old_mono)) > self.jump_tolerance:
                    return 'LOCAL_CLOCK_JUMP'
            if not self.semantics_verifier():
                return 'MT5_TIMESTAMP_SEMANTICS_UNVERIFIED'
            if not self.result or not self.result['ok']:
                return (self.result or {}).get('reason', 'EXTERNAL_UTC_UNVERIFIED')
            age = mono-self.result['checked_monotonic']
            if age < 0 or age > self.freshness:
                return 'EXTERNAL_UTC_STALE'
            return None
        except Exception:
            return 'TIME_CHECK_FAILED'

    def refresh(self):
        samples = []
        self.result = dict(ok=False, reason='EXTERNAL_UTC_UNAVAILABLE', samples=samples)
        try:
            for _ in range(3):
                w0, m0 = self._clocks()
                response = self.get(URL, timeout=(2., 2.), allow_redirects=False)
                response.raise_for_status()
                if response.status_code != 200:
                    raise ValueError('EXTERNAL_UTC_HTTP_STATUS')
                payload = response.json()
                w1, m1 = self._clocks()
                rtt = m1-m0
                if rtt < 0 or rtt > self.max_rtt:
                    raise ValueError('EXTERNAL_UTC_RTT')
                if abs(w1-w0-rtt) > self.jump_tolerance:
                    raise ValueError('LOCAL_CLOCK_JUMP')
                if samples:
                    prev = samples[-1]
                    if m0 < prev['end_monotonic'] or abs(w0-prev['end_utc']-(m0-prev['end_monotonic'])) > self.jump_tolerance:
                        raise ValueError('LOCAL_CLOCK_JUMP')
                if payload.get('retCode') != 0 or isinstance(payload.get('retCode'), bool):
                    raise ValueError('EXTERNAL_UTC_RESPONSE')
                result = payload['result']
                remote = int(result['timeNano']) / 1e9
                seconds = int(result['timeSecond'])
                if not math.isfinite(remote) or remote <= 0 or not 0 <= remote-seconds < 1:
                    raise ValueError('EXTERNAL_UTC_RESPONSE')
                uncertainty = rtt/2 + 1e-6
                offset = remote-(w0+w1)/2
                if uncertainty > self.max_uncertainty or abs(offset)+uncertainty > self.max_offset:
                    raise ValueError('EXTERNAL_UTC_OFFSET_OR_UNCERTAINTY')
                samples.append(dict(offset_seconds=offset, rtt_seconds=rtt,
                    uncertainty_seconds=uncertainty, end_utc=w1, end_monotonic=m1))
            if max(s['offset_seconds']-s['uncertainty_seconds'] for s in samples) > min(s['offset_seconds']+s['uncertainty_seconds'] for s in samples):
                raise ValueError('EXTERNAL_UTC_INCONSISTENT')
            self.anchor = (w1, m1)
            self.result.update(ok=True, reason=None, checked_at_utc=w1, checked_monotonic=m1)
        except Exception as exc:
            self.result['reason'] = str(exc) if isinstance(exc, ValueError) and str(exc).startswith(('EXTERNAL_UTC_', 'LOCAL_CLOCK_', 'INVALID_LOCAL_')) else 'EXTERNAL_UTC_UNAVAILABLE'
        return self.check()
