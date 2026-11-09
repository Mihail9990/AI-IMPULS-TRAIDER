"""Independent paper service; MT5 is a read-only market source, never a broker."""
import argparse
from contextlib import ExitStack
import importlib
import json
import os
from pathlib import Path
import time

from .runtime import owner_lock
from .paper_market import MarketReader
from .paper import PaperSession


def paper_state_path(config):
    """Keep the former embedded simulator's database when no explicit path is set."""
    if config.get('state_file'):
        return Path(config['state_file']).resolve()
    live = Path(config.get('live_state_file', 'tradfi_mt5_state.sqlite3')).resolve()
    return live.with_name(live.stem + '.paper.sqlite3')


def _run_paper(config, *, api=None, stop_event=None, clock=time.time,
               monotonic=time.monotonic, panel_factory=None, reader_factory=None):
    config = dict(config)
    if config.get('backend', 'paper_mt5_quotes_v1') != 'paper_mt5_quotes_v1':
        raise ValueError('Independent paper backend config is required')
    if config.get('enable_trading', False) is not False:
        raise ValueError('Paper config cannot enable live trading')
    login=config.get('expected_login')
    if isinstance(login,bool) or not isinstance(login,int) or login<=0:
        raise ValueError('Explicit positive expected_login is required')
    if config.get('expected_server')!='Bybit-Live-6' or config.get('symbol')!='XAUUSD.s':
        raise ValueError('Expected Bybit-Live-6 and XAUUSD.s market source')
    path=config.get('terminal_path')
    if not isinstance(path,str) or not path:raise ValueError('Explicit terminal_path required')
    state=paper_state_path(config)
    live=Path(config.get('live_state_file','tradfi_mt5_state.sqlite3')).resolve()
    if state==live:raise ValueError('Paper and live databases must be separate')
    web=config.get('web',{})
    if not isinstance(web,dict) or not isinstance(web.get('enabled',True),bool):raise ValueError('Invalid paper web configuration')
    host=web.get('host','127.0.0.1');port=web.get('port',8766)
    if host not in {'127.0.0.1','::1'}:raise ValueError('Paper web requires loopback')
    if isinstance(port,bool) or not isinstance(port,int) or not 1024<=port<=65535:raise ValueError('Invalid paper loopback port')
    password=None
    if web.get('enabled',True) and panel_factory is None:
        from .web_auth import resolve_password
        password=resolve_password(web,environ={})
    from .managed import runtime_guard,stop_request,bind_state
    with ExitStack() as resources:
        resources.enter_context(runtime_guard(state,instance='paper'))
        if stop_event is None:stop_event=stop_request(instance='paper')
        state.parent.mkdir(parents=True,exist_ok=True)
        resources.enter_context(owner_lock(str(state)+'.lock'))
        # No account execution lock, no live Store, no MT5Broker, no login.
        api=api if api is not None else importlib.import_module('MetaTrader5')
        resources.callback(api.shutdown)
        if not api.initialize(path,timeout=10000):raise OSError('MT5 initialize failed')
        reader=(reader_factory or MarketReader)(api,symbol=config['symbol'],clock=clock,monotonic=monotonic,
            expected_account=str(login)+'@'+config['expected_server'])
        paper=PaperSession(state,reader,clock=clock,monotonic=monotonic)
        paper.log_file=config.get("log_file")
        resources.callback(paper.close)
        bind_state(state,instance='paper')
        panel=None
        if web.get('enabled',True):
            from .web_control import PaperPanel
            authority=('[::1]' if host=='::1' else host)+':'+str(port)
            panel=(panel_factory or PaperPanel)(paper,authority=authority,password=password if panel_factory is None else os.environ.get('TRADFI_WEB_PASSWORD'),
                clock=clock,monotonic=monotonic,public_origin=web.get('public_origin'))
            resources.callback(panel.close);panel.start()
        while stop_event is None or not stop_event.is_set():
            paper.step()
            if panel is not None:panel.step()
            if stop_event is None:time.sleep(.2)
            else:stop_event.wait(.2)


def main(argv=None):
    parser=argparse.ArgumentParser(description='Independent MT5 read-only paper simulator')
    parser.add_argument('--config',default='tradfi_paper_config.json')
    args=parser.parse_args(argv)
    try:
        config=json.loads(Path(args.config).read_text(encoding='utf-8-sig'))
        _run_paper(config)
    except KeyboardInterrupt:parser.exit(130,'Paper stopped.\n')
    except Exception as exc:
        from .startup_errors import write_failure
        parser.exit(2,write_failure(args.config,exc))


if __name__=='__main__':main()
