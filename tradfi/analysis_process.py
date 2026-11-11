"""Bounded subprocess transport for a separate, operator-installed analysis venv."""
import json
import os
from pathlib import Path
import struct
import subprocess

COMMAND_LIMIT = 2048


def validate_python(value):
    """Only an explicit absolute executable inside an isolated venv is accepted."""
    if not isinstance(value, str) or value.startswith(('\\\\', '//')):
        raise ValueError('ANALYSIS_LOCAL_PATH_REQUIRED')
    path = Path(value)
    if not path.is_absolute() or not path.is_file() or path.name.lower() not in {'python', 'python.exe', 'python3', 'python3.12'}:
        raise ValueError('ANALYSIS_PYTHON_INVALID')
    if path.parent.name not in {'Scripts', 'bin'}: raise ValueError('ANALYSIS_VENV_REQUIRED')
    cfg = path.parent.parent / 'pyvenv.cfg'
    if not cfg.is_file() or cfg.stat().st_size > 8192: raise ValueError('ANALYSIS_VENV_REQUIRED')
    settings = dict(line.lower().strip().split(' = ', 1) for line in cfg.read_text().splitlines() if ' = ' in line)
    if settings.get('include-system-site-packages') != 'false': raise ValueError('ANALYSIS_ISOLATION_REQUIRED')
    if not settings.get('version', '').startswith('3.12.'): raise ValueError('ANALYSIS_PYTHON_312_REQUIRED')
    return str(path)


def clean_environment():
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(('PYTHON', 'PIP_', 'TRADFI_'))}
    for key in ('OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS', 'NUMEXPR_NUM_THREADS', 'NUMBA_NUM_THREADS'):
        env[key] = '1'
    return env


class CommandWriter:
    def __init__(self, stream): self.stream = stream
    def put_nowait(self, value):
        payload = json.dumps(value, allow_nan=False, separators=(',', ':')).encode('utf-8') + b'\n'
        if len(payload) > COMMAND_LIMIT: raise ValueError('COMMAND_LIMIT')
        # At most one small proof is outstanding. Initial config + proof fit
        # below the minimum Windows anonymous-pipe buffer (4 KiB).
        self.stream.write(payload)
        self.stream.flush()
    def cancel_join_thread(self): pass
    def close(self): self.stream.close()


class FrameReader:
    def __init__(self, stream): self.stream = stream
    def _exact(self, length):
        chunks = []
        remaining = length
        while remaining:
            data = self.stream.read(remaining)
            if not data: raise EOFError()
            chunks.append(data); remaining -= len(data)
        return b''.join(chunks)
    def recv_bytes(self, maxlength):
        length = struct.unpack('!I', self._exact(4))[0]
        if length > maxlength: raise ValueError('RESULT_LIMIT')
        return self._exact(length)
    def close(self): self.stream.close()


class ExternalWorker:
    def __init__(self, executable, config):
        self.executable = validate_python(executable)
        self.config = config
        self.process = None
        self.is_external = True
    def start(self):
        entry = Path(__file__).resolve().parents[1] / 'tools' / 'analysis_worker.py'
        # Never shell-expand or inherit PYTHONPATH/user-site into the child.
        self.process = subprocess.Popen([self.executable, '-I', '-u', str(entry)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=clean_environment(), shell=False, bufsize=0,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.input = CommandWriter(self.process.stdin)
        self.output = FrameReader(self.process.stdout)
        try:
            self.input.put_nowait(self.config)
        except Exception:
            # Popen succeeded but service has not received these pipe handles.
            # Own cleanup here, including a closed-stdin/hung-child failure.
            try: self.process.kill()
            except Exception: pass
            try: self.process.wait(timeout=.5)
            except Exception: pass
            for stream in (self.process.stdin, self.process.stdout):
                try: stream.close()
                except Exception: pass
            raise
    def is_alive(self): return self.process is not None and self.process.poll() is None
    def join(self, timeout):
        if self.process is not None:
            try: self.process.wait(timeout=timeout)
            except subprocess.TimeoutExpired: pass
    def terminate(self): self.process.terminate()
    def kill(self): self.process.kill()
