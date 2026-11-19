"""Opt-in READ-ONLY MT5 adapter scaffold. No trading, initialize or login path.

Not imported by runners. Caller must supply an already initialized API session.
This module does not bypass or impersonate the mock transport's test-double lock.
No configuration, environment variable, or constructor option enables writes.
"""
from datetime import datetime, timedelta, timezone
from .mt5_transport import MT5Transport


class RealMT5ReadOnlyAdapter:
    # Reuse pure field normalization only, NOT transport preflight/send methods.
    _normalize_order = MT5Transport._normalize_order
    _normalize_position = MT5Transport._normalize_position

    def __init__(self, api, *, account, server):
        if not isinstance(account, int) or isinstance(account, bool) or account <= 0 or not server:
            raise ValueError('Explicit expected account/server required')
        self.api=api
        self.account=account
        self.server=server

    def _test_lock(self):
        raise RuntimeError('Read-only real adapter cannot enter the mock execution pipeline')

    def preflight(self, *args, **kwargs):
        raise RuntimeError('Real MT5 execution safety lock: preflight disabled')

    def send(self, *args, **kwargs):
        raise RuntimeError('Real MT5 execution safety lock: submission disabled')

    def _verify_session(self):
        a=self.api.account_info();t=self.api.terminal_info()
        if a is None or t is None or not t.connected:
            raise ValueError('Initialized connected MT5 session required')
        if a.login!=self.account or a.server!=self.server:
            raise ValueError('Account/server mismatch')
        if a.margin_mode!=self.api.ACCOUNT_MARGIN_MODE_RETAIL_HEDGING:
            raise ValueError('Hedging account required for independent ranges')
        return a,t

    def snapshot(self, expected, *, now=None, history_days=30):
        """Read only, bounded history; never a certification of trading readiness.

        Caller selects explicit expected scope; credentials are neither accepted
        nor returned. Account identity is verified before AND after the reads.
        """
        if expected.get('account')!=self.account or expected.get('server')!=self.server:
            raise ValueError('Requested scope does not match adapter binding')
        if not isinstance(history_days,int) or not 1<=history_days<=30:
            raise ValueError('Explicit history window must be 1..30 days')
        now=now or datetime.now(timezone.utc)
        if now.tzinfo is None:raise ValueError('Timezone-aware timestamp required')
        self._verify_session()
        orders=self.api.orders_get();positions=self.api.positions_get()
        history=self.api.history_orders_get(now-timedelta(days=history_days),now)
        deals=self.api.history_deals_get(now-timedelta(days=history_days),now)
        if any(value is None for value in (orders,positions,history,deals)):
            raise ValueError('Incomplete terminal snapshot/history')
        result={'account':self.account,'server':self.server,
                'mode':'read_only','live_trading':False,'safe_to_trade':False,
                'orders':[self._normalize_order(o) for o in orders],
                'positions':[self._normalize_position(p) for p in positions],
                'history_orders':[self._normalize_order(o) for o in history],
                'close_deals':[{'ticket':d.ticket,'order':d.order,'position_identifier':d.position_id,
                               'symbol':d.symbol,'entry':'OUT','volume':str(d.volume)}
                              for d in deals if d.entry==self.api.DEAL_ENTRY_OUT],
                'warning':'Non-atomic bounded read; no live execution or restart readiness certification'}
        self._verify_session()
        return result
