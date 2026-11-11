"""Private subprocess entrypoint. No CLI action, trading API or state access."""
import json
import os
from pathlib import Path
import struct
import sys


def main():
    # Preserve IPC on a private duplicate, then silence Python AND native vendor
    # stdout (SMC prints a banner on import). Never mix it into framed JSON.
    output = os.fdopen(os.dup(sys.stdout.fileno()), 'wb', buffering=0)
    null = open(os.devnull, 'w', encoding='utf-8', errors='replace')
    os.dup2(null.fileno(), sys.stdout.fileno())
    sys.stdout = null
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from tradfi.analysis_process import COMMAND_LIMIT
    from tradfi.analysis_service import RESULT_LIMIT, _worker

    class Input:
        def get(self):
            line = sys.stdin.buffer.readline(COMMAND_LIMIT + 1)
            if not line or len(line) > COMMAND_LIMIT or not line.endswith(b'\n'): raise ValueError('COMMAND_LIMIT')
            return json.loads(line)
    class Output:
        def send_bytes(self, payload):
            if len(payload) > RESULT_LIMIT: raise ValueError('RESULT_LIMIT')
            output.write(struct.pack('!I', len(payload)))
            output.write(payload)
            output.flush()
        def close(self): output.close()
    incoming = Input()
    config = incoming.get()
    required = {'terminal_path', 'symbol', 'expected_login', 'expected_server', 'time_mode', 'offset'}
    if not isinstance(config, dict) or set(config) != required: raise ValueError('INVALID_CONFIG')
    if sys.version_info[:2] != (3, 12) or struct.calcsize('P') != 8: raise ValueError('PYTHON_312_X64_REQUIRED')
    _worker(config, incoming, Output())


if __name__ == '__main__':
    try: main()
    except Exception: raise SystemExit(2) from None
