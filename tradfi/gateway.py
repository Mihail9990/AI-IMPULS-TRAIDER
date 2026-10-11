"""Local same-VPS MT5 Common Files mailbox; no listening network server."""
import json
import os
from pathlib import Path
import time
import uuid
from .settings import number


class FileGateway:
    def __init__(self, directory, *, enable_trading=False, max_age=5, timeout=5):
        self.root = Path(directory)
        self.enable_trading = enable_trading
        self.max_age, self.timeout = max_age, timeout
        self.bound_account = None
        for name in ('commands', 'acks', 'ledger'):
            (self.root / name).mkdir(parents=True, exist_ok=True)

    def snapshot(self):
        path = self.root / 'snapshot.json'
        if time.time() - path.stat().st_mtime > self.max_age:
            raise TimeoutError('Stale MT5 snapshot')
        try:
            data = json.loads(path.read_text(encoding='utf-8-sig'))
        except (ValueError, UnicodeError) as exc:
            raise OSError('Incomplete MT5 snapshot') from exc
        if data.get('protocol') != 1 or not data.get('connected'):
            raise OSError('Gateway not connected / unsupported protocol')
        if abs(time.time() - float(data['now'])) > self.max_age:
            raise TimeoutError('MT5 snapshot clock is stale')
        if abs(time.time() - float(data['quote']['utc_time'])) > self.max_age:
            raise TimeoutError('MT5 quote is stale')
        if self.bound_account is not None and self.bound_account != data['account']:
            raise ValueError('Gateway account changed during command planning')
        self.bound_account = data['account']
        return data

    def _write(self, command):
        cid = command['id']
        if len(cid) != 24 or any(c not in '0123456789abcdef' for c in cid):
            raise ValueError('Invalid command id')
        fields = {k: str(v) for k, v in command.items() if k not in {'status', 'result'}}
        fields['protocol'] = '1'
        fields['expires'] = str(int(time.time() + self.timeout))
        fields['account'] = self.snapshot()['account']
        if any('\n' in v or '\r' in v or '=' in v for v in fields.values()):
            raise ValueError('Invalid mailbox field')
        destination = self.root / 'commands' / (cid + '.cmd')
        # An existing command/ledger is immutable and must never be overwritten/replayed.
        if (destination.exists() or (self.root / 'ledger' / (cid + '.intent')).exists()
                or (self.root / 'acks' / (cid + '.json')).exists()):
            return
        temporary = destination.with_suffix('.tmp')
        with temporary.open('x', encoding='ascii', newline='\n') as handle:
            handle.write('\n'.join(k + '=' + v for k, v in fields.items()) + '\n')
            handle.flush(); os.fsync(handle.fileno())
        os.replace(temporary, destination)

    def submit(self, command):
        if not self.enable_trading:
            raise OSError('Gateway mutation disabled; explicit local configuration required')
        self._write(command)

    def result(self, cid):
        path = self.root / 'acks' / (cid + '.json')
        if not path.exists():
            return {'status': 'UNKNOWN'}
        try:
            result = json.loads(path.read_text(encoding='utf-8-sig'))
        except (ValueError, UnicodeError):
            return {'status': 'UNKNOWN'}
        if result.get('id') != cid:
            raise ValueError('Gateway reply identity mismatch')
        return result

    def profit(self, side, size, entry, close):
        cid = uuid.uuid4().hex[:24]
        self._write(dict(id=cid, kind='PROFIT', side=side, size=size, price=entry, close=close))
        until = time.monotonic() + self.timeout
        while time.monotonic() < until:
            result = self.result(cid)
            if result.get('status') == 'DONE':
                return number(result['profit'])
            if result.get('status') == 'REJECTED':
                raise ValueError('OrderCalcProfit rejected: ' + result.get('reason', ''))
            time.sleep(.05)
        raise TimeoutError('OrderCalcProfit gateway timeout (read-only)')
