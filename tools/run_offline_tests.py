"""Run all unittest tests with real network connections forbidden."""
import socket
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

root = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(root), str(root / 'tests')]


blocked_attempts = []

def forbidden(*args, **kwargs):
    blocked_attempts.append(True)
    raise AssertionError('Real network access forbidden in offline tests')


if __name__ == '__main__':
    with patch.object(socket.socket, 'connect', forbidden), patch.object(socket.socket, 'connect_ex', forbidden), patch.object(socket, 'getaddrinfo', forbidden):
        suite = unittest.defaultTestLoader.discover(str(root / 'tests'))
        result = unittest.TextTestRunner(verbosity=1).run(suite)
    print('Blocked real network attempts:', len(blocked_attempts))
    raise SystemExit(not result.wasSuccessful() or bool(blocked_attempts))
