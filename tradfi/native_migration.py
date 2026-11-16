"""Offline native migration review. No terminal import, connection or activation."""
import json
from pathlib import Path
import sqlite3
import uuid


def prepare_review(legacy_state, legacy_config, destination, common_files_directory, *, native_magic):
    source=Path(legacy_state).resolve()
    config_path=Path(legacy_config).resolve()
    if not source.is_file() or not config_path.is_file():raise ValueError('Existing legacy state/config required')
    if type(native_magic) is not int or native_magic<=0:raise ValueError('Explicit positive native magic required')
    config=json.loads(config_path.read_text(encoding='utf-8-sig'))
    db=sqlite3.connect(source.as_uri()+'?mode=ro',uri=True)
    try:
        row=db.execute('SELECT payload FROM snapshot WHERE id=1').fetchone()
        if not row:raise ValueError('Legacy snapshot missing')
        state=json.loads(row[0])
        if state.get('schema')!=1:raise ValueError('Unsupported legacy schema')
        settings=state.get('basket_settings',{})
        required=['INITIAL_SL_DISTANCE','INITIAL_TP_DISTANCE','INITIAL_ENTRY_DISTANCE','RECOVERY_SL_DISTANCE']
        if any(k not in settings for k in required):raise ValueError('Missing explicit saved distances; defaults are not substituted')
        if native_magic==config.get('magic'):raise ValueError('Choose a separate native magic; old state is not adopted')
        commands=state.get('commands',{})
        unresolved=[cid for cid,c in commands.items() if c.get('status') not in {'FILLED','DONE','REJECTED','OBSOLETE','CANCELLED'}]
        cycle=state.get('cycle') or {}
        open_tickets=[pid for pid,t in cycle.get('tickets',{}).items() if not t.get('closed')]
        identity=dict(installation=str(uuid.uuid4()),login=str(config.get('expected_login') or ''),server=config.get('expected_server',''),symbol=config.get('symbol',''))
        if any(not v for v in identity.values()):raise ValueError('Explicit saved account identity required')
        root=Path(destination)
        root.mkdir(parents=True,exist_ok=False)
        root.chmod(0o700)
        backup_path=root/'legacy-state-backup.sqlite3'
        backup=sqlite3.connect(backup_path)
        try:db.backup(backup)
        finally:backup.close()
        backup_path.chmod(0o600)
        # Configuration and settings are preserved verbatim in this PRIVATE local
        # archive. Do not print or upload them: they may include panel secrets.
        (root/'legacy-config-backup.json').write_bytes(config_path.read_bytes())
        (root/'legacy-config-backup.json').chmod(0o600)
        native=dict(backend='ea_native_v1',common_files_directory=str(common_files_directory),identity=identity,
                    log_file=str(root/'native-panel.log'),web=config.get('web',{}))
        inputs=dict(InstallationId=identity['installation'],ExpectedLogin=identity['login'],ExpectedServer=identity['server'],TradeSymbol=identity['symbol'],Magic=native_magic,
                    AllowTrading=False,LegacyOwnerStopped=False,MigrationConfirmed=False,
                    InitialSLDistance=settings['INITIAL_SL_DISTANCE'],InitialTPDistance=settings['INITIAL_TP_DISTANCE'],
                    CandleOffset=settings['INITIAL_ENTRY_DISTANCE'],RecoveryCandleOffset=state.get('v3_settings',{}).get('entry_distance'),
                    RecoverySLDistance=state.get('recovery_profiles',{}).get('recovery_sl_distance',settings['RECOVERY_SL_DISTANCE']),
                    StartingRecovery=cycle.get('recovery',state.get('basket_carry','0')),StartingSteps=None,CarryStageConfirmed=False)
        review=dict(protocol='native_migration_review_v1',activation_authorized=False,broker_checked=False,
                    unresolved_command_count=len(unresolved),open_ticket_count=len(open_tickets),
                    legacy_phase=state.get('phase'),ea_inputs_to_review=inputs,
                    preserved_profiles=state.get('recovery_profiles'),
                    blockers=['Verify broker is flat and old owner stopped',
                              'Reconcile all unknown operations and provisional recovery before migration',
                              'Confirm StartingRecovery and StartingSteps; no automatic volume/debt reset',
                              'Compile all three EAs with MetaEditor and review logs',
                              'Saved old profiles are archived, not silently replaced by native defaults'])
        for name,body in [('native-config.json',native),('migration-review.json',review)]:
            path=root/name;path.write_text(json.dumps(body,ensure_ascii=False,indent=2),encoding='utf-8');path.chmod(0o600)
        return review
    finally:db.close()


def prepare_new_installation(settings, destination):
    """Create a new accounting identity without reading or deleting old history."""
    import math
    distances=['InitialSLDistance','InitialTPDistance','CandleOffset','RecoveryCandleOffset','RecoverySLDistance']
    for name in distances:
        value=settings.get(name)
        if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or value<=0:
            raise ValueError('Explicit positive price distance required: '+name)
    if type(settings.get('magic')) is not int or settings['magic']<=0:
        raise ValueError('Explicit positive new installation magic required')
    login=str(settings.get('login',''))
    if not login.isascii() or not login.isdigit() or int(login)<=0:
        raise ValueError('Explicit positive account login required')
    for name in ['server','symbol','common_files_directory']:
        if not isinstance(settings.get(name),str) or not settings[name].strip():raise ValueError('Required: '+name)
    root=Path(destination);root.mkdir(parents=True,exist_ok=False);root.chmod(0o700)
    installation=str(uuid.uuid4())
    identity=dict(installation=installation,login=login,server=settings['server'],symbol=settings['symbol'])
    inputs=dict(InstallationId=installation,ExpectedLogin=login,ExpectedServer=identity['server'],TradeSymbol=identity['symbol'],Magic=settings['magic'],
                NewAccountingStart=True,StartingRecovery=0,StartingSteps=2,AllowTrading=False,LegacyOwnerStopped=False,MigrationConfirmed=False,
                **{k:settings[k] for k in distances})
    review=dict(protocol='native_new_installation_review_v1',activation_authorized=False,broker_checked=False,
                historical_closed_deals_imported=False,ea_inputs_to_review=inputs,
                blockers=['Check every open position and pending order on the target symbol before first initialization',
                          'Use a new empty mailbox and unique installation UUID; do not delete an existing journal',
                          'Compile with MetaEditor; review configuration; explicitly start only after checks'])
    native=dict(backend='ea_native_v1',common_files_directory=settings['common_files_directory'],identity=identity,
                log_file=str(root/'native-panel.log'),web=dict(host='127.0.0.1',port=8765,password=''))
    for name,body in [('native-config.json',native),('new-installation-review.json',review)]:
        path=root/name;path.write_text(json.dumps(body,ensure_ascii=False,indent=2),encoding='utf-8');path.chmod(0o600)
    return review
