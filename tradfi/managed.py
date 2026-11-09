"""Coordinate independently owned live/paper services with managed updates."""
from contextlib import contextmanager, ExitStack
import os
from pathlib import Path
import re
import time


def managed_root():
    code=Path(__file__).resolve().parent.parent
    if code.name=='code' and code.parent.parent.name=='releases':
        return code.parent.parent.parent
    return None


def _role(instance):
    if instance not in {'live','paper'}:raise ValueError('Unknown service instance')
    return instance


def binding_path(root,instance):
    return root/'data'/('state-binding.json' if _role(instance)=='live' else 'paper-state-binding.json')


@contextmanager
def runtime_guard(state_file,*,instance='live'):
    """Admission is serialized with updates; role ownership lasts until exit.

    Updates take operation.lock then BOTH role locks in live,paper order. A
    service takes admission then only its role lock, releasing admission before
    running. No shared byte-range lock assumptions are needed on Windows.
    """
    _role(instance);root=managed_root()
    if root is None:
        yield
        return
    from install_tradfi import lock, read_json, SetupError
    with ExitStack() as owned:
        with lock(root/'operation.lock'):
            owned.enter_context(lock(root/('instance-'+instance+'.lock')))
            code=Path(__file__).resolve().parent.parent
            if read_json(root/'managed-install.json')!={'format':1} or read_json(root/'current.json')['current']!=code.parent.name:
                raise SetupError('Managed release is not current')
            binding=binding_path(root,instance)
            value={'path':str(Path(state_file).resolve())}
            if binding.exists() and (read_json(binding)!=value or not Path(state_file).is_file()):
                raise SetupError('Managed state missing or moved; automatic reset forbidden')
            other=binding_path(root,'paper' if instance=='live' else 'live')
            if other.exists() and read_json(other)==value:
                raise SetupError('Live and paper state files must be separate')
        yield


def bind_state(state_file,*,instance='live'):
    _role(instance);root=managed_root()
    if root is not None:
        from install_tradfi import atomic_json
        binding=binding_path(root,instance)
        if not binding.exists():atomic_json(binding,{'path':str(Path(state_file).resolve())})


class StopRequest:
    def __init__(self,path):self.path=path
    def is_set(self):return self.path.is_file()
    def wait(self,seconds):
        time.sleep(seconds)
        return self.is_set()


def stop_request(*,instance='live'):
    _role(instance);root=managed_root();session=os.environ.get('TRADFI_LAUNCH_SESSION','')
    launch_instance=os.environ.get('TRADFI_LAUNCH_INSTANCE')
    if launch_instance is not None and launch_instance!=instance:
        raise ValueError('Launch instance does not match service')
    if root is not None and re.fullmatch('[0-9a-f]{32}',session):
        # Preserve stop compatibility with an older live launcher; new launchers
        # always specify a role and never share a stop request with each other.
        stem='stop-'+instance+'-' if launch_instance is not None else 'stop-'
        if launch_instance is None and instance=='paper':return None
        return StopRequest(root/'data'/(stem+session+'.request'))
    return None
