"""Explicit entry point; default mode is offline simulation, gateway defaults to read-only."""
import argparse
from contextlib import contextmanager
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import time

from .engine import Controller
from .gateway import FileGateway
from .simulator import Simulator
from .storage import Store


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


def main(argv=None):
    parser = argparse.ArgumentParser(description='Independent TradFi runner; no Capital.com imports')
    parser.add_argument('--mode', choices=('simulate', 'gateway'), default='simulate')
    parser.add_argument('--config', default='tradfi_config.json')
    parser.add_argument('--inspect', action='store_true', help='Read gateway snapshot only, no trading/Telegram loop')
    args = parser.parse_args(argv)
    if args.mode == 'simulate':
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp)/'state.sqlite3'); broker = Simulator(); bot = Controller(store, broker)
            bot.command('/start'); broker.candle(60); bot.tick()
            broker.fill('BUY'); broker.quote('4003', '4003.10')
            for _ in range(4): bot.tick()
            broker.close('TP'); bot.tick()
            print(json.dumps({'mode': 'OFFLINE_SIMULATOR', 'state': bot.status(),
                              'completed': bot.state['completed'], 'commands': len(broker.commands)}, indent=2))
            store.close()
        return
    config = json.loads(Path(args.config).read_text(encoding='utf-8'))
    if not isinstance(config.get('enable_trading', False), bool):
        raise ValueError('enable_trading must be a JSON boolean')
    gateway = FileGateway(config['gateway_directory'], enable_trading=config.get('enable_trading', False))
    if args.inspect:
        print(json.dumps(gateway.snapshot(), indent=2)); return
    state_file = Path(config.get('state_file', 'tradfi_state.sqlite3')).resolve()
    state_file.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(config.get('log_file', 'tradfi.log'), maxBytes=5_000_000, backupCount=5, encoding='utf-8')
    logging.basicConfig(level=logging.INFO, handlers=[handler], format='%(asctime)s %(levelname)s %(message)s')
    # These are optional process environment credentials, never written to a settings template/log.
    token, chat = os.environ.get('TRADFI_TELEGRAM_TOKEN'), os.environ.get('TRADFI_TELEGRAM_CHAT_ID')
    if not token or not chat:
        raise ValueError('Set TRADFI_TELEGRAM_TOKEN and TRADFI_TELEGRAM_CHAT_ID for this process')
    from .telegram import Telegram
    # One owner per database AND mailbox, including two processes with different state paths.
    with owner_lock(str(state_file)+'.lock'), owner_lock(gateway.root/'python.lock'):
        store = Store(state_file); controller = Controller(store, gateway)
        telegram = Telegram(token, chat)
        if not gateway.enable_trading:
            print('Gateway read-only. Use --inspect; no command loop started.')
            store.close(); return
        if not controller.state.get('telegram_initialized'):
            backlog = telegram.updates(-1)
            if backlog:
                controller.state['telegram_offset'] = max(int(u['update_id']) for u in backlog) + 1
            controller.state['telegram_initialized'] = True
            controller.save('telegram_first_start_discard_backlog')
        telegram.start(controller.state['telegram_offset'])
        try:
            while True:
                telegram.process(controller)
                try:
                    controller.tick()
                except (OSError, TimeoutError) as exc:
                    logging.warning('Gateway temporarily unavailable: %s', exc)
                time.sleep(.2)
        finally:
            telegram.close(); store.close()
