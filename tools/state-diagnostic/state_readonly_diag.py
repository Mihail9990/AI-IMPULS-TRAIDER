"""Read-only local state diagnostics. No project imports, MT5, network or locks."""
import json
import os
from pathlib import Path
import re
import sqlite3
import time

BASE = 'AI_IMPULS_TRADFI_Manager'
TERMINAL = {'DONE', 'REJECTED', 'CANCELLED', 'FILLED', 'OBSOLETE'}

def read_json(path):
    with path.open(encoding='utf-8-sig') as stream:
        text = stream.read(8 * 1024 * 1024 + 1)
    if len(text) > 8 * 1024 * 1024:
        raise ValueError()
    return json.loads(text)

def diagnose(root):
    result = {'diagnostic': 'STATE_READ_ONLY_V1', 'live_state_checked': False}
    stage = 'current_pointer'
    db = None
    try:
        ptr = read_json(root / 'current.json')
        sha = ptr.get('current')
        valid = isinstance(sha, str) and re.fullmatch('[0-9a-f]{40}', sha)
        result['installed_commit'] = sha if valid else 'INVALID'
        if valid:
            code = root / 'releases' / sha / 'code'
            result['installed_pretrade_checker'] = (code / 'pretrade_checks.py').is_file()
    except Exception:
        result['installed_commit'] = 'UNREADABLE'
    try:
        stage = 'configuration_path'
        cfg = read_json(root / 'data' / 'service.json')
        p = Path(cfg.get('state_file', 'tradfi_mt5_state.sqlite3'))
        p = (p if p.is_absolute() else root / 'data' / p).resolve()
        result['state_exists'] = p.is_file()
        stage = 'state_binding'
        binding = root / 'data' / 'state-binding.json'
        result['binding_present'] = binding.exists()
        result['binding_ok'] = not binding.exists() or (read_json(binding) == {'path': str(p)} and p.is_file())
        if not p.is_file():
            result['result'] = 'STATE_FILE_MISSING'
            return result
        stage = 'sqlite_open_readonly'
        db = sqlite3.connect(p.as_uri() + '?mode=ro', uri=True, timeout=3)
        deadline = time.monotonic() + 10
        db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        stage = 'snapshot_read'
        row = db.execute('SELECT payload FROM snapshot WHERE id=1').fetchone()
        result['snapshot_present'] = row is not None
        if row is None:
            result['result'] = 'SNAPSHOT_MISSING'
            return result
        stage = 'snapshot_structure'
        s = json.loads(row[0])
        commands = s.get('commands', {})
        result['schema_ok'] = s.get('schema') == 1
        result['backend_ok'] = s.get('backend') == 'mt5_direct_v1'
        result['ledger_ok'] = s.get('ledger_contract') == 'deal_ledger_v1'
        result['scope_ok'] = not commands or s.get('scope_contract') == 'cycle_scope_v1'
        result['cycle_present'] = bool(s.get('cycle'))
        phase = s.get('phase')
        result['phase'] = phase if phase in ('IDLE', 'PAUSED', 'WAIT_START') else 'OTHER_OR_MISSING'
        result['nonterminal_commands'] = sum(v.get('status') not in TERMINAL for v in commands.values())
        stage = 'command_journal'
        ids = [r[0] for r in db.execute('SELECT id FROM mt5_commands')]
        result['journal_missing_in_snapshot'] = sum(cid not in commands for cid in ids)
        stage = 'deal_ledger'
        tickets = [r[0] for r in db.execute('SELECT ticket FROM consumed_deals')]
        seen = s.get('seen_deals', [])
        result['seen_duplicates'] = len(seen) != len(set(seen))
        result['ledger_duplicates'] = len(tickets) != len(set(tickets))
        result['deal_sets_match'] = set(tickets) == set(seen)
        result['result'] = 'READ_COMPLETE_NOT_TRADING_APPROVAL'
    except Exception:
        result['result'] = 'READ_FAILED'
        result['error_stage'] = stage
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:
                result['result'] = 'READ_FAILED'
                result['error_stage'] = 'sqlite_close'
    return result

def main():
    try:
        value = os.environ.get('LOCALAPPDATA')
        report = diagnose(Path(value) / BASE) if value else {'result': 'LOCALAPPDATA_MISSING'}
    except Exception:
        report = {'result': 'DIAGNOSTIC_FAILED_DETAILS_REDACTED'}
    print(json.dumps(report, indent=2, ensure_ascii=True))

if __name__ == '__main__':
    main()
