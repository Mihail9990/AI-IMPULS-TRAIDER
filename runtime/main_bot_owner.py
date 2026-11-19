"""Share AI-IMPULS-TRAIDER's account/symbol ownership lock on the VPS."""
from contextlib import contextmanager
from pathlib import Path
import hashlib
import json
import os


class AccountAlreadyOwned(RuntimeError):
    pass


def account_lock_path(scope, local_appdata=None):
    # Match AI-IMPULS-TRAIDER work@48130516 tradfi/runtime.py exactly.
    identity=[scope['server'],scope['account'],scope['symbol']]
    digest=hashlib.sha256(json.dumps(identity,ensure_ascii=True).encode('ascii')).hexdigest()
    base=local_appdata or os.environ.get('LOCALAPPDATA') or Path.home()/'.local'/'state'
    return Path(base)/'AI IMPULS TradFi'/'locks'/(digest+'.lock')


@contextmanager
def main_bot_owner(scope, local_appdata=None):
    path=account_lock_path(scope,local_appdata)
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('a+b') as handle:
        try:
            if os.name=='nt':
                import msvcrt
                # Match main-bot byte range. No payload is stored here.
                if not path.stat().st_size:
                    handle.write(b'0')
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError as exc:
            raise AccountAlreadyOwned('MAIN_BOT_ACCOUNT_BUSY') from exc
        yield
