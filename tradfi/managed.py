"""Coordinate direct runtime starts with the standalone managed installer."""
from contextlib import contextmanager
import os
from pathlib import Path
import re
import time


def managed_root():
    code=Path(__file__).resolve().parent.parent
    if code.name=='code' and code.parent.parent.name=='releases':
        return code.parent.parent.parent
    return None


@contextmanager
def runtime_guard(state_file):
    root=managed_root()
    if root is None:
        yield
        return
    from install_tradfi import lock, read_json, SetupError
    with lock(root/'operation.lock'):
        code=Path(__file__).resolve().parent.parent
        if read_json(root/'managed-install.json')!={'format':1} or read_json(root/'current.json')['current']!=code.parent.name:
            raise SetupError('Managed release is not current')
        binding=root/'data'/'state-binding.json'
        value={'path':str(Path(state_file).resolve())}
        if binding.exists() and (read_json(binding)!=value or not Path(state_file).is_file()):
            raise SetupError('Managed state missing or moved; automatic reset forbidden')
        yield


def bind_state(state_file):
    root=managed_root()
    if root is not None:
        from install_tradfi import atomic_json
        binding=root/'data'/'state-binding.json'
        if not binding.exists(): atomic_json(binding,{'path':str(Path(state_file).resolve())})


class StopRequest:
    def __init__(self,path): self.path=path
    def is_set(self): return self.path.is_file()
    def wait(self,seconds):
        time.sleep(seconds)
        return self.is_set()


def stop_request():
    root=managed_root(); session=os.environ.get('TRADFI_LAUNCH_SESSION','')
    if root is not None and re.fullmatch('[0-9a-f]{32}',session):
        return StopRequest(root/'data'/('stop-'+session+'.request'))
    return None
