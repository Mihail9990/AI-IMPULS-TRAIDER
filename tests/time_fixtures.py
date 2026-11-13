"""Explicit independent clock double for offline execution fixtures only."""
from tradfi.time_safety import TimeSafety


def verified_time(**kwargs):
    clock = kwargs['clock']
    class Response:
        status_code = 200
        def raise_for_status(self): pass
        def json(self):
            now = clock()
            return {'retCode': 0, 'result': {'timeNano': str(int(now*1e9)), 'timeSecond': str(int(now))}}
    safety = TimeSafety(**kwargs, get=lambda *a, **k: Response(), semantics_verifier=lambda: True)
    safety.refresh()
    return safety
