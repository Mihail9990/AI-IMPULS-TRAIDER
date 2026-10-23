"""Independent TradFi runner. Simulation is default; direct MT5 requires explicit opt-in."""
import argparse
from contextlib import contextmanager, ExitStack
import hashlib
import json
import logging
import os
from pathlib import Path
import time

from .engine import Controller
from .simulator import Simulator
from .storage import Store, DurabilityError


@contextmanager
def owner_lock(path):
    """OS lock released on crash; protects the whole state-read/intent/send owner lifecycle."""
    handle = open(path, 'a+b')
    try:
        if os.name == 'nt':
            import msvcrt
            handle.seek(0); handle.write(b'0'); handle.flush(); handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        handle.close()


def account_lock_path(config):
    """Different project/DB paths must not create two owners of the same account/symbol."""
    identity = [config['expected_server'], config['expected_login'], config['symbol']]
    digest = hashlib.sha256(json.dumps(identity, ensure_ascii=True).encode('ascii')).hexdigest()
    base = Path(os.environ['LOCALAPPDATA']) if os.environ.get('LOCALAPPDATA') else Path.home()/'.local'/'state'
    directory = base/'AI IMPULS TradFi'/'locks'
    directory.mkdir(parents=True, exist_ok=True)
    return directory/(digest + '.lock')


def _run_mt5(config, inspect, *, evidence=False, journal_path=None, service=False, execute=False,
             api=None, telegram_factory=None, stop_event=None, clock=time.time, monotonic=time.monotonic,
             config_path=None, panel_factory=None):
    from .mt5_adapter import MT5Broker
    config = dict(config)
    credentials_file = config.pop('telegram_credentials_file', None)
    if inspect:
        # No SQLite, account owner lock, Telegram imports or credentials. Never inherit execution opt-in.
        config = dict(config, enable_trading=False)
        broker = MT5Broker(config,api=api,clock=clock,monotonic=monotonic)
        try:
            broker.connect()
            result = broker.preflight()
            if evidence:
                from collections import Counter
                audit = broker.reconciliation_evidence(journal_path=journal_path)
                result['reconciliation'] = dict(
                    obligation_status_counts=dict(Counter(r['status'] for r in audit['obligations'].values())),
                    discovered_orders=len(audit['raw_facts']['order']),
                    discovered_deals=len(audit['raw_facts']['deal']),
                    unavailable_sources=sum(v=='UNAVAILABLE' for v in audit['source_status'].values()),
                    foreign_order_count=len(audit['foreign_orders']),
                    foreign_position_count=len(audit['foreign_positions']),
                    evidence_blocked=audit['evidence_blocked'],
                    history_complete=False, execution_ready=False,
                    journal_source='offline_snapshot' if journal_path else 'none')
            print(json.dumps(result, indent=2, ensure_ascii=False))
        finally:
            broker.close()
        return
    if not service and config.get('enable_trading') is not True:
        print('MT5 execution is disabled. Use --mode mt5 --inspect for read-only preflight.')
        return
    from .time_profile import require_execution_profile
    if execute and not service:
        raise ValueError('Execution requires explicit service mode')
    if not service:
        require_execution_profile(config)  # no implicit activation through the old CLI
    if execute:
        require_execution_profile(config,evidence_contract='per_obligation_v1')
    config=dict(config,enable_trading=bool(execute))
    if execute and config.get('legacy_ea_removed') is not True:
        raise ValueError('Legacy EA removal must be explicitly confirmed')
    if execute and config.get('legacy_state_reconciled') is not True:
        raise ValueError('Legacy strategy state and obligations must be explicitly reconciled')
    login = config.get('expected_login')
    if isinstance(login, bool) or not isinstance(login, int) or login <= 0:
        raise ValueError('Explicit positive expected_login is required for a persistent service')
    if not config.get('expected_server') or not config.get('symbol'):
        raise ValueError('Explicit expected_server and symbol are required for execution')
    from .credentials import load_telegram_credentials
    token, chat, user = load_telegram_credentials(credentials_file, config_path=config_path)
    state_file = Path(config.get('state_file', 'tradfi_mt5_state.sqlite3')).resolve()
    # Both locks precede Store construction and ANY MT5 initialize/connect call.
    with ExitStack() as resources:
        from .managed import runtime_guard, stop_request, bind_state
        resources.enter_context(runtime_guard(state_file))
        if stop_event is None:
            stop_event = stop_request()
        state_file.parent.mkdir(parents=True, exist_ok=True)
        resources.enter_context(owner_lock(str(state_file)+'.lock'))
        resources.enter_context(owner_lock(account_lock_path(config)))
        store = Store(state_file)
        resources.callback(store.close)
        bind_state(state_file)
        from .evidence_broker import MT5EvidenceBroker
        broker = MT5EvidenceBroker(config, store=store,api=api,clock=clock,monotonic=monotonic)
        resources.callback(broker.close)
        broker.connect()
        from .interactive import InteractiveController
        controller = InteractiveController(store, broker,clock=clock,monotonic=monotonic)
        panel=None
        web_config=config.get('web',{})
        if not isinstance(web_config,dict):raise ValueError('web must be a configuration object')
        if not isinstance(web_config.get('enabled',False),bool):raise ValueError('web.enabled must be boolean')
        if web_config.get('enabled',False):
            if web_config.get('host','127.0.0.1')!='127.0.0.1':raise ValueError('Web server requires loopback')
            port=web_config.get('port',8765)
            if isinstance(port,bool) or not isinstance(port,int) or not 1024<=port<=65535:raise ValueError('Invalid loopback port')
            from .web_control import NativePanel
            authority='127.0.0.1:'+str(port)
            panel=(panel_factory or NativePanel)(controller,authority=authority,password=os.environ.get('TRADFI_WEB_PASSWORD'),clock=clock,monotonic=monotonic)
            resources.callback(panel.close)
            panel.start()
            controller.panel_url='http://'+authority+'/?controller=native'
        from .log_export import diagnostic_handler
        log_path=Path(config.get('log_file','tradfi_mt5.log')).resolve()
        handler = diagnostic_handler(log_path)
        resources.callback(handler.close)
        previous_log_level=logging.getLogger().level
        resources.callback(logging.getLogger().setLevel,previous_log_level)
        logging.getLogger().addHandler(handler)
        logging.getLogger().setLevel(logging.INFO)
        resources.callback(logging.getLogger().removeHandler, handler)
        from .telegram import Telegram
        telegram = (telegram_factory or Telegram)(token, chat, user_id=user,
                            log_path=log_path)
        resources.callback(telegram.http.close)
        resources.callback(telegram.close)
        controller.notify('TradFi READ_ONLY service: native observations; /start and trading mutations disabled.'
                          if broker.read_only else 'TradFi EXECUTION service: explicit trading activation enabled.')
        controller.save('service_started')
        if not controller.state.get('telegram_initialized'):
            backlog = telegram.updates(-1)
            if backlog:
                controller.state['telegram_offset'] = max(int(u['update_id']) for u in backlog) + 1
            controller.state['telegram_initialized'] = True
            controller.save('telegram_first_start_discard_backlog')
        telegram.start(controller.state['telegram_offset'])
        next_time_check = 0.
        while stop_event is None or not stop_event.is_set():
            safety = getattr(broker, 'time_safety', None)
            if safety is not None and not broker.read_only and monotonic() >= next_time_check:
                controller.refresh_time()
                next_time_check = monotonic() + 30.
            telegram.process(controller)
            try:
                controller.tick()
                if panel is not None:panel.step()
            except (OSError, TimeoutError) as exc:
                # Terminal exception payloads can contain private account/order data.
                logging.warning('MT5 temporarily unavailable: %s', type(exc).__name__)
            if stop_event is None: time.sleep(.2)
            else: stop_event.wait(.2)


def main(argv=None):
    parser = argparse.ArgumentParser(description='TradFi MT5 runner')
    parser.add_argument('--mode', choices=('simulate', 'mt5', 'gateway'), default='simulate')
    parser.add_argument('--config', default='tradfi_config.json')
    parser.add_argument('--inspect', action='store_true', help='Read-only MT5 preflight; no SQLite/Telegram/trading')
    parser.add_argument('--inspect-evidence', action='store_true', help='Read-only obligation evidence summary; never execute')
    parser.add_argument('--journal-snapshot', help='Existing compatible offline rollback-journal SQLite snapshot for evidence audit')
    parser.add_argument('--service',action='store_true',help='Native MT5 + Telegram service; trading disabled by default')
    parser.add_argument('--execute',action='store_true',help='Explicit execution activation; requires --service and config opt-ins')
    args = parser.parse_args(argv)
    if (args.service and (args.mode!='mt5' or args.inspect or args.inspect_evidence)
            or args.execute and not args.service):
        parser.error('--service requires --mode mt5 without inspect; --execute requires --service')
    if (args.inspect_evidence and args.mode != 'mt5') or (args.journal_snapshot and not args.inspect_evidence):
        parser.error('Evidence inspection requires --mode mt5 --inspect-evidence')
    if args.mode == 'simulate':
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp)/'state.sqlite3')
            try:
                broker = Simulator(); bot = Controller(store, broker)
                bot.command('/start'); broker.candle(60); bot.tick()
                broker.fill('BUY'); broker.quote('4003', '4003.10')
                for _ in range(4): bot.tick()
                broker.close('TP'); bot.tick()
                print(json.dumps({'mode': 'OFFLINE_SIMULATOR', 'state': bot.status(),
                                  'completed': bot.state['completed'], 'commands': len(broker.commands)}, indent=2))
            finally:
                store.close()
        return
    if args.mode == 'gateway':
        parser.exit(2, 'Legacy file gateway runtime is retired. Preserve its database/journal; '
                    'no automatic migration. Use --mode mt5 --inspect with a direct-MT5 config.\n')
    try:
        config = json.loads(Path(args.config).read_text(encoding='utf-8-sig'))
        if config.get('backend') != 'mt5_direct_v1':
            raise ValueError('Direct MT5 backend config is required')
        for key in ('enable_trading', 'legacy_ea_removed', 'legacy_state_reconciled'):
            if not isinstance(config.get(key, False), bool):
                raise ValueError('Execution flags must be JSON booleans')
        _run_mt5(config, args.inspect or args.inspect_evidence,
                 evidence=args.inspect_evidence, journal_path=args.journal_snapshot,
                 service=args.service,execute=args.execute,config_path=args.config)
    except KeyboardInterrupt:
        parser.exit(130, 'TradFi stopped.\n')
    except (Exception, DurabilityError) as exc:
        from .mt5_adapter import QuoteFreshnessError
        from .time_profile import TimeProfileError
        if isinstance(exc, TimeProfileError):
            parser.exit(2, 'MT5 time profile: ' + exc.code + '. Trading permissions were not changed.\n')
        if isinstance(exc, QuoteFreshnessError):
            parser.exit(2, 'MT5 quote timing: ' + json.dumps(exc.details, allow_nan=False) +
                        '\nCheck incoming ticks and Windows clock synchronization; '
                        'do not compensate broker timestamps or relax freshness without evidence. '
                        'Trading permissions were not changed.\n')
        # Never echo raw MT5 errors, config paths, account identity or Telegram credentials.
        parser.exit(2, 'MT5 check/runner stopped (' + type(exc).__name__ + '). '
                    'Verify config, terminal connection, UST hedging account and fresh quotes; '
                    'preserve existing state. Trading permissions were not changed.\n')
