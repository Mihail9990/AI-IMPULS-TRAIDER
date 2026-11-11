"""Shared contract for the isolated, non-trading analysis interpreter."""
import struct
import sys
import sysconfig

PYTHON_VERSION = (3, 13)
VENV_DIRECTORY = '.venv-analysis-313'


def supported_runtime():
    """Require the tested CPython ABI, not a free-threaded or 32-bit build."""
    return (sys.implementation.name == 'cpython'
            and sys.version_info[:2] == PYTHON_VERSION
            and struct.calcsize('P') == 8
            and not sysconfig.get_config_var('Py_GIL_DISABLED'))
