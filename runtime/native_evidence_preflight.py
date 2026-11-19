"""Read-only prerequisite attestation on an already initialized terminal.

No trading, initialization, login, shutdown, or permission changes. A fresh
historical SL mailbox response proves the local EA path, not safe future fills.
"""
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4
import math
import os
import subprocess
import time

class CheckError(RuntimeError): pass

TRUSTED_INSTALLER_SID = 'S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464'
_FULL_WRITE_MASK = 0xD0156
_NAMESPACE_MASK = 0xD0150
_GENERIC_MASK = 0xF0000000
# Windows file/directory mappings (FILE_GENERIC_* and FILE_ALL_ACCESS).
_FILE_GENERIC = ((0x80000000, 0x120089), (0x40000000, 0x120116),
                 (0x20000000, 0x1200A0), (0x10000000, 0x1F01FF))


def _map_file_generic(rights):
    mapped = rights & ~_GENERIC_MASK
    for generic, specific in _FILE_GENERIC:
        if rights & generic:
            mapped |= specific
    return mapped


def _verify_acl_records(records, *, mailbox, system_drive, current_sid):
    """Pure policy over an exhaustive Windows ACL snapshot. No ACL changes.

    Creation-only relaxation and TrustedInstaller ownership apply ONLY to the
    exact system volume root. All other ancestors retain the original strict
    write mask. Inherit-only grants are checked on actual children, and at the
    mailbox as future child grants. Denies never erase a dangerous allow.
    """
    import ntpath
    def norm(value):
        if not isinstance(value, str) or not ntpath.isabs(value) or value.startswith('\\\\'):
            raise CheckError('Local absolute ACL path required')
        return ntpath.normcase(ntpath.normpath(value))
    if not isinstance(system_drive,str) or len(system_drive)!=2 or system_drive[1]!=':' or not system_drive[0].isalpha():
        raise CheckError('System drive not verified')
    root=norm(system_drive+'\\'); leaf=norm(mailbox)
    if not isinstance(current_sid,str) or not current_sid.startswith('S-1-'):
        raise CheckError('Current Windows SID unavailable')
    trusted={current_sid,'S-1-5-18','S-1-5-32-544'}
    expected=set(); cursor=leaf
    while True:
        expected.add(cursor); parent=ntpath.dirname(cursor)
        if parent==cursor: break
        cursor=parent
    seen=set()
    for record in records:
        path=norm(record['path'])
        if path in seen: raise CheckError('Duplicate ACL object')
        seen.add(path)
        is_chain=path in expected
        is_file=record.get('kind')=='file'
        if not is_chain and not (is_file and ntpath.dirname(path)==leaf):
            raise CheckError('Unexpected mailbox object')
        if is_chain and record.get('kind')!='directory': raise CheckError('Directory required')
        if record.get('reparse') is not False or record.get('null_dacl') is not False:
            raise CheckError('Reparse or null DACL rejected')
        owner=record.get('owner')
        if owner not in trusted and not (path==root and owner==TRUSTED_INSTALLER_SID):
            raise CheckError('Owner rejected')
        rules=record.get('rules')
        if not isinstance(rules,list): raise CheckError('DACL unavailable')
        for rule in rules:
            if rule.get('type') not in ('Allow','Deny'): raise CheckError('Unsupported ACE')
            if rule['type']=='Deny': continue
            sid=rule.get('sid'); rights=rule.get('rights')
            io=rule.get('inherit_only'); flags=rule.get('inheritance')
            if type(rights) is not int or not 0<=rights<=0xFFFFFFFF or type(io) is not bool or type(flags) is not int or flags not in (0,1,2,3):
                raise CheckError('Malformed ACE')
            rights = _map_file_generic(rights)
            if sid in trusted: continue
            if io:
                # On ancestors this has no current access effect. Every actual
                # child is independently examined; mailbox child templates must
                # also stay safe for files created after this snapshot.
                if path!=leaf: continue
                if sid=='S-1-3-0' and flags:
                    # CREATOR OWNER resolves to the trusted creating identity;
                    # effective untrusted creation rights are separately denied.
                    continue
                if flags and rights & _FULL_WRITE_MASK:
                    raise CheckError('Unsafe future mailbox child ACE')
                continue
            mask=_NAMESPACE_MASK if path==root else _FULL_WRITE_MASK
            if rights & mask: raise CheckError('Untrusted effective writer')
    if not expected.issubset(seen): raise CheckError('Incomplete ancestor ACL snapshot')


def verify_mailbox_acl(directory):
    if os.name != 'nt': raise CheckError('Windows required')
    import json
    p=Path(directory)
    if not p.is_absolute() or not p.is_dir() or str(p).startswith('\\\\'):
        raise CheckError('Local existing mailbox required')
    for item in [p,*p.parents]:
        if not item.exists() or item.is_symlink() or getattr(item.stat(),'st_file_attributes',0)&1024:
            raise CheckError('Missing/reparse mailbox path rejected')
    # Get-Acl can normalize access rules; reject unsupported raw ACE types and
    # detect a null DACL from the raw descriptor BEFORE producing policy data.
    # DirectoryInfo.Parent is a CLR object without provider note properties.
    script=r"""$ErrorActionPreference='Stop'; [Console]::OutputEncoding=[Text.UTF8Encoding]::new($false);
function Read-Node($item){
 $a=Get-Acl -LiteralPath $item.FullName;
 $raw=[Security.AccessControl.RawSecurityDescriptor]::new($a.GetSecurityDescriptorBinaryForm(),0);
 $rules=@();
 if($null -ne $raw.DiscretionaryAcl){foreach($ace in $raw.DiscretionaryAcl){if($ace.AceType -notin @([Security.AccessControl.AceType]::AccessAllowed,[Security.AccessControl.AceType]::AccessDenied)){throw 'Unsupported raw ACE'}}};
 foreach($r in $a.Access){$rules+=@{type=$r.AccessControlType.ToString();sid=$r.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value;rights=([long]$r.FileSystemRights -band 4294967295);inherit_only=(($r.PropagationFlags -band [Security.AccessControl.PropagationFlags]::InheritOnly) -ne 0);inheritance=[int]$r.InheritanceFlags}};
 return @{path=$item.FullName;kind=$(if($item -is [IO.DirectoryInfo]){'directory'}else{'file'});owner=([Security.Principal.NTAccount]$a.Owner).Translate([Security.Principal.SecurityIdentifier]).Value;reparse=(($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0);null_dacl=($null -eq $raw.DiscretionaryAcl);rules=@($rules)}
};
$p=$env:RECOVERY_CHECK_MAILBOX; $leaf=Get-Item -LiteralPath $p -Force; $records=@(); $cursor=$leaf;
while($null -ne $cursor){$records+=Read-Node $cursor; $cursor=$cursor.Parent};
foreach($child in @(Get-ChildItem -LiteralPath $p -Force)){if($child -is [IO.DirectoryInfo]){throw 'Unexpected mailbox subdirectory'};$records+=Read-Node $child};
@{current_sid=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value;system_drive=[IO.Path]::GetPathRoot([Environment]::SystemDirectory).TrimEnd('\');records=@($records)} | ConvertTo-Json -Depth 8 -Compress
"""
    env=os.environ.copy();env['RECOVERY_CHECK_MAILBOX']=str(p)
    try:
        result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',script],
            env=env,capture_output=True,encoding='utf-8',errors='replace',timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode: raise CheckError('ACL read failed')
        data=json.loads(result.stdout)
        _verify_acl_records(data['records'],mailbox=str(p),system_drive=data['system_drive'],current_sid=data['current_sid'])
    except (CheckError,ValueError,KeyError,TypeError,OSError,subprocess.TimeoutExpired) as exc:
        # Native launch maps only this fixed code; no directory or raw ACL is exposed.
        raise CheckError('Mailbox and ancestor permissions not verified') from exc


def verify_native_prerequisites(api, *, terminal, account, server, symbol, magic,
        expected_data_path=None, timeout=20.0, _acl_check=None, _bridge_type=None,
        _binding_type=None, _pending_error=None, _clock=time.monotonic,
        _sleep=time.sleep, _now=None):
    """Fail closed unless REAL identity, secure mailbox and fresh EA reply agree.

    Underscored hooks exist for offline doubles only. No cached attestation is
    accepted. Caller owns initialized MT5 session and subsequent execution.
    """
    if type(account) is not int or account <= 0 or type(magic) is not int or magic <= 0:
        raise CheckError('Invalid account or magic')
    if not isinstance(server,str) or not server or not isinstance(symbol,str) or not symbol:
        raise CheckError('Exact server and symbol required')
    terminal=Path(terminal)
    if not terminal.is_absolute() or not terminal.is_file():
        raise CheckError('Existing absolute terminal executable required')
    if isinstance(timeout,bool) or not isinstance(timeout,(int,float)) or not math.isfinite(timeout) or not 0 < timeout <= 60:
        raise CheckError('Invalid timeout')
    if _bridge_type is None:
        from recovery.local_evidence_bridge import LocalEvidenceBridge
        from recovery.ea_stop_evidence import ExitBinding, StopEvidenceUnavailable
        _bridge_type=LocalEvidenceBridge; _binding_type=ExitBinding; _pending_error=StopEvidenceUnavailable
    acl_check=_acl_check or verify_mailbox_acl
    now=_now or datetime.now(timezone.utc)
    lower=now-timedelta(hours=72)
    def canonical(path): return os.path.normcase(os.path.abspath(str(path)))
    def session(expected=None):
        a=api.account_info(); t=api.terminal_info()
        real=getattr(api,'ACCOUNT_TRADE_MODE_REAL',None)
        if (a is None or t is None or type(real) is not int or
            getattr(a,'trade_mode',None)!=real or a.login!=account or
            a.server!=server or not t.connected):
            raise CheckError('REAL account/session mismatch')
        if canonical(t.path)!=canonical(terminal.parent): raise CheckError('Terminal path mismatch')
        data=Path(t.data_path)
        if not data.is_absolute() or (expected is not None and canonical(data)!=canonical(expected)):
            raise CheckError('Data path mismatch')
        if expected_data_path is not None and canonical(data)!=canonical(expected_data_path):
            raise CheckError('Expected data path mismatch')
        return data
    def signature(d):
        return (d.ticket,d.order,d.position_id,d.symbol,d.type,d.entry,d.reason,d.time_msc,str(d.volume))
    data=session(); mailbox=data/'MQL5'/'Files'/'RECOVERY'; acl_check(mailbox); session(data)
    history=api.history_deals_get(lower,now); session(data)
    if history is None: raise CheckError('History unavailable')
    candidates=[d for d in history if d.symbol==symbol and d.entry==api.DEAL_ENTRY_OUT
        and d.reason==api.DEAL_REASON_SL and int(lower.timestamp()*1000)<=d.time_msc<=int(now.timestamp()*1000)]
    if not candidates: raise CheckError('No recent historical SL; evidence cannot be proven without it')
    deal=max(candidates,key=lambda d:(d.time_msc,d.ticket))
    same=[d for d in history if d.ticket==deal.ticket]
    if len(same)!=1: raise CheckError('Duplicate historical exit')
    if deal.type not in (api.DEAL_TYPE_BUY,api.DEAL_TYPE_SELL): raise CheckError('Exit side invalid')
    binding=_binding_type(request_id=uuid4().hex,account=account,server=server,
        deal=int(deal.ticket),order=int(deal.order),position=int(deal.position_id),symbol=symbol,
        exit_side='BUY' if deal.type==api.DEAL_TYPE_BUY else 'SELL',
        time_msc=int(deal.time_msc),volume=Decimal(str(deal.volume)))
    bridge=_bridge_type(mailbox); session(data); bridge.publish(binding)
    deadline=_clock()+timeout
    while _clock()<deadline:
        session(data)
        try: level=bridge.collect(binding)
        except _pending_error:
            _sleep(.25); continue
        if not isinstance(level,Decimal) or not level.is_finite() or level<=0:
            raise CheckError('Invalid SL evidence')
        history=api.history_deals_get(position=int(deal.position_id));session(data)
        current=None if history is None else [d for d in history if d.ticket==deal.ticket]
        if current is None or len(current)!=1 or signature(current[0])!=signature(deal):
            raise CheckError('History changed')
        acl_check(mailbox);session(data)
        return {'ok':True,'mode':'native_prerequisite_read_only','trading':False,
            'evidence_verified':True,'account':account,'server':server,'symbol':symbol,
            'magic':magic,'terminal':str(terminal),'data_path':str(data),
            'evidence_deal':deal.ticket,'verified_at':datetime.now(timezone.utc).isoformat()}
    raise CheckError('EA evidence timeout')
