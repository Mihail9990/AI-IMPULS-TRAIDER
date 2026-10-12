"""Independent TradFi runner. Simulation is default; direct MT5 requires explicit opt-in."""
import argparse
from contextlib import contextmanager, ExitStack
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
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


def _run_mt5(config, inspect):
    from .mt5_adapter import MT5Broker
    if inspect:
        # No SQLite, account owner lock, Telegram imports or credentials. Never inherit execution opt-in.
        config = dict(config, enable_trading=False)
        broker = MT5Broker(config)
        try:
            broker.connect()
            print(json.dumps(broker.preflight(), indent=2, ensure_ascii=False))
        finally:
            broker.close()
        return
    if config.get('enable_trading') is not True:
        print('MT5 execution is disabled. Use --mode mt5 --inspect for read-only preflight.')
        return
    from .time_profile import require_execution_profile
    require_execution_profile(config)
    if config.get('legacy_ea_removed') is not True:
        raise ValueError('Legacy EA removal must be explicitly confirmed')
    if config.get('legacy_state_reconciled') is not True:
        raise ValueError('Legacy strategy state and obligations must be explicitly reconciled')
    login = config.get('expected_login')
    if isinstance(login, bool) or not isinstance(login, int) or login <= 0:
        raise ValueError('Explicit positive expected_login is required for execution')
    if not config.get('expected_server') or not config.get('symbol'):
        raise ValueError('Explicit expected_server and symbol are required for execution')
    token, chat = os.environ.get('TRADFI_TELEGRAM_TOKEN'), os.environ.get('TRADFI_TELEGRAM_CHAT_ID')
    if not token or not chat:
        raise ValueError('Telegram process environment credentials are required for execution')
    state_file = Path(config.get('state_file', 'tradfi_mt5_state.sqlite3')).resolve()
    state_file.parent.mkdir(parents=True, exist_ok=True)
    # Both locks precede Store construction and ANY MT5 initialize/connect call.
    with ExitStack() as resources:
        resources.enter_context(owner_lock(str(state_file)+'.lock'))
        resources.enter_context(owner_lock(account_lock_path(config)))
        store = Store(state_file)
        resources.callback(store.close)
        broker = MT5Broker(config, store=store)
        resources.callback(broker.close)
        broker.connect()
        controller = Controller(store, broker)
        handler = RotatingFileHandler(config.get('log_file', 'tradfi_mt5.log'),
                                      maxBytes=5_000_000, backupCount=5, encoding='utf-8')
        resources.callback(handler.close)
        logging.basicConfig(level=logging.INFO, handlers=[handler],
                            format='%(asctime)s %(levelname)s %(message)s')
        resources.callback(logging.getLogger().removeHandler, handler)
        from .telegram import Telegram
        telegram = Telegram(token, chat)
        resources.callback(telegram.http.close)
        resources.callback(telegram.close)
        if not controller.state.get('telegram_initialized'):
            backlog = telegram.updates(-1)
            if backlog:
                controller.state['telegram_offset'] = max(int(u['update_id']) for u in backlog) + 1
            controller.state['telegram_initialized'] = True
            controller.save('telegram_first_start_discard_backlog')
        telegram.start(controller.state['telegram_offset'])
        while True:
            telegram.process(controller)
            try:
                controller.tick()
            except (OSError, TimeoutError) as exc:
                # Terminal exception payloads can contain private account/order data.
                logging.warning('MT5 temporarily unavailable: %s', type(exc).__name__)
            time.sleep(.2)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Independent TradFi runner; no Capital.com imports')
    parser.add_argument('--mode', choices=('simulate', 'mt5', 'gateway'), default='simulate')
    parser.add_argument('--config', default='tradfi_config.json')
    parser.add_argument('--inspect', action='store_true', help='Read-only MT5 preflight; no SQLite/Telegram/trading')
    args = parser.parse_args(argv)
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
        _run_mt5(config, args.inspect)
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
