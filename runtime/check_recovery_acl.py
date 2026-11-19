"""Collect only a status and object role, never account IDs, paths or ACL data."""
import os
from pathlib import Path
import subprocess
import native_evidence_preflight as acl053

POLICY = {
    'Local absolute ACL path required': 'PATH_FORMAT',
    'System drive not verified': 'SYSTEM_DRIVE',
    'Current Windows SID unavailable': 'CURRENT_IDENTITY',
    'Duplicate ACL object': 'DUPLICATE_OBJECT',
    'Unexpected mailbox object': 'UNEXPECTED_OBJECT',
    'Directory required': 'NOT_DIRECTORY',
    'Reparse or null DACL rejected': 'REPARSE_OR_NULL_DACL',
    'Owner rejected': 'OWNER_NOT_ALLOWED',
    'DACL unavailable': 'DACL_UNAVAILABLE',
    'Unsupported ACE': 'UNSUPPORTED_ACE',
    'Malformed ACE': 'MALFORMED_ACE',
    'Unexpanded generic ACE': 'GENERIC_ACE',
    'Unsafe future mailbox child ACE': 'UNSAFE_CHILD_PERMISSION',
    'Untrusted effective writer': 'UNTRUSTED_WRITER',
    'Incomplete ancestor ACL snapshot': 'INCOMPLETE_SNAPSHOT',
    'Local existing mailbox required': 'MAILBOX_NOT_FOUND',
    'Missing/reparse mailbox path rejected': 'MISSING_OR_REPARSE_PATH',
    'ACL read failed': 'COLLECTOR_FAILED',
}

def collector_status(stderr):
    s = stderr.casefold()
    for token, code in [
        ('unexpected mailbox subdirectory', 'UNEXPECTED_SUBDIRECTORY'),
        ('unsupported raw ace', 'UNSUPPORTED_RAW_ACE'),
        ('identitynotmappedexception', 'UNMAPPED_IDENTITY'),
        ('identity references could not be translated', 'UNMAPPED_IDENTITY'),
        ('unauthorizedaccessexception', 'ACCESS_DENIED'),
        ('parameterbindingexception', 'POWERSHELL_BINDING'),
        ('parsererror', 'POWERSHELL_PARSE'),
        ('itemnotfoundexception', 'ITEM_NOT_FOUND'),
    ]:
        if token in s:
            return code
    return 'COLLECTOR_FAILED'

def diagnose(mailbox):
    original = acl053.subprocess.run
    report = None
    def observing_run(*args, **kwargs):
        nonlocal report
        r = original(*args, **kwargs)
        if r.returncode:
            report = collector_status(r.stderr or '')
        return r
    acl053.subprocess.run = observing_run
    try:
        acl053.verify_mailbox_acl(mailbox)
        return 'ACL_OK'
    except Exception as outer:
        exc = outer.__cause__ or outer
        if report:
            return report
        if isinstance(exc, subprocess.TimeoutExpired):
            return 'COLLECTOR_TIMEOUT'
        if isinstance(exc, FileNotFoundError):
            return 'POWERSHELL_OR_PATH_NOT_FOUND'
        if not isinstance(exc, acl053.CheckError):
            return 'ACL_IO_ERROR' if isinstance(exc, OSError) else 'SNAPSHOT_FORMAT'
        code = POLICY.get(str(exc), 'CHECK_FAILED')
        role = ''
        tb = exc.__traceback__
        while tb is not None:
            if tb.tb_frame.f_code is acl053._verify_acl_records.__code__:
                loc = tb.tb_frame.f_locals
                path = loc.get('path')
                if path:
                    role = ('MAILBOX' if path == loc.get('leaf') else
                            'SYSTEM_ROOT' if path == loc.get('root') else
                            'ANCESTOR' if path in loc.get('expected', set()) else
                            'MAILBOX_ITEM')
            tb = tb.tb_next
        return code + (f' OBJECT={role}' if role else '')
    finally:
        acl053.subprocess.run = original

def main():
    print('RECOVERY 0.5.6 / Read-only permissions check')
    print('No trading, no installation, no permission changes.')
    if os.name != 'nt':
        print('STATUS=WINDOWS_REQUIRED')
        return
    try:
        base = Path(os.environ['APPDATA']) / 'MetaQuotes' / 'Terminal'
        found = sorted(base.glob('*/MQL5/Files/RECOVERY'))
        if not found:
            print('STATUS=MAILBOX_NOT_FOUND')
        for index, candidate in enumerate(found, 1):
            print(f'MAILBOX={index} STATUS={diagnose(candidate)}')
    except (OSError, KeyError, ValueError):
        print('STATUS=SCAN_ERROR')

if __name__ == '__main__':
    main()
