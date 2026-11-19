"""One-shot READ-ONLY terminal diagnostic session; no strategy or order calls."""
from .real_mt5_adapter import RealMT5ReadOnlyAdapter
from .engine import number


def diagnose(api, *, terminal_path, account, server, symbol, history_days=1):
    if not isinstance(terminal_path,str) or not terminal_path.strip():
        raise ValueError('Explicit terminal executable path required')
    if type(account) is not int or account<=0 or not isinstance(server,str) or not server.strip():
        raise ValueError('Explicit expected account and server required')
    if not isinstance(symbol,str) or not symbol.strip():raise ValueError('Explicit symbol required')
    if type(history_days) is not int or not 1<=history_days<=30:raise ValueError('History window 1..30 required')
    # Do not provide login/password/server to initialize: never request an account
    # switch. The terminal may use its saved current account; verify it before reads.
    try:
        if api.initialize(terminal_path,timeout=10000) is not True:
            raise RuntimeError('MT5 initialization failed')
        adapter=RealMT5ReadOnlyAdapter(api,account=account,server=server)
        adapter._verify_session()
        info=api.symbol_info(symbol);tick=api.symbol_info_tick(symbol)
        if info is None or tick is None:raise ValueError('Symbol metadata or quote unavailable')
        bid,ask=number(tick.bid),number(tick.ask)
        if bid<=0 or ask<bid:raise ValueError('Invalid symbol quote')
        snapshot=adapter.snapshot({'account':account,'server':server},history_days=history_days)
        a,t=adapter._verify_session()
        # Only a minimal report is returned; no balance, account number, position
        # tickets, credentials, local terminal path or raw history records.
        return {'mode':'read_only_diagnostic','live_trading':False,'safe_to_trade':False,
            'account_match':True,'hedging_account':True,'symbol':symbol,
            'symbol_selected':getattr(info,'select',None),
            'terminal_trade_allowed':getattr(t,'trade_allowed',None),
            'terminal_tradeapi_disabled':getattr(t,'tradeapi_disabled',None),
            'account_trade_allowed':getattr(a,'trade_allowed',None),
            'account_trade_expert':getattr(a,'trade_expert',None),
            'symbol_trade_mode':getattr(info,'trade_mode',None),
            'symbol_tick_size':str(getattr(info,'trade_tick_size','unknown')),
            'symbol_volume_step':str(getattr(info,'volume_step','unknown')),
            'quote_available':True,'history_days':history_days,
            'symbol_pending_count':sum(o['symbol']==symbol for o in snapshot['orders']),
            'symbol_position_count':sum(p['symbol']==symbol for p in snapshot['positions']),
            'warning':'Connection/read test only. No execution, EA validation or deployment readiness proof.'}
    finally:
        api.shutdown()
