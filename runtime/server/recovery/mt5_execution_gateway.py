"""Native MT5 request boundary, locked for real terminals; not a runner.

Official request field mapping is available offline. Check/send interfaces run
only in an explicitly constructed test harness. No live unlock option exists.
Durable intent/reconciliation belongs to Executor, not this low-level boundary.
"""
from copy import deepcopy
from math import isfinite
from threading import RLock
from .real_mt5_adapter import RealMT5ReadOnlyAdapter

_FIELDS={
 'TRADE_ACTION_PENDING': {'action','symbol','magic','type','price','sl','tp','volume','type_time','type_filling','comment'},
 'TRADE_ACTION_SLTP': {'action','symbol','position','sl','tp'},
 'TRADE_ACTION_REMOVE': {'action','order'},
 'TRADE_ACTION_DEAL': {'action','symbol','position','magic','type','volume','price','deviation','type_filling','comment'},
}
_REQUIRED={
 'TRADE_ACTION_PENDING': {'symbol','magic','type','price','sl','tp','volume','type_time','type_filling'},
 'TRADE_ACTION_SLTP': {'position','sl','tp'},
 'TRADE_ACTION_REMOVE': {'order'},
 'TRADE_ACTION_DEAL': {'symbol','position','magic','type','volume','type_filling'},
}
_ENUMS={'type':{'ORDER_TYPE_BUY_STOP','ORDER_TYPE_SELL_STOP','ORDER_TYPE_BUY','ORDER_TYPE_SELL'},
        'type_time':{'ORDER_TIME_GTC'},'type_filling':{'ORDER_FILLING_RETURN','ORDER_FILLING_FOK','ORDER_FILLING_IOC'}}


def map_native_request(api, request, *, market_execution=False):
    """Map the supported MqlTradeRequest subset, without any terminal calls.

    DEAL is close-only (position required). Market Execution omits price as
    specified by MT5 docs. Filling policy is explicit, never auto-selected.
    """
    r=deepcopy(request);action=r.get('action')
    if action not in _FIELDS:raise ValueError('Unsupported MT5 action')
    if set(r)-_FIELDS[action] or _REQUIRED[action]-set(r):raise ValueError('Unexpected or missing request fields')
    if action=='TRADE_ACTION_PENDING' and r['type'] not in ('ORDER_TYPE_BUY_STOP','ORDER_TYPE_SELL_STOP'):
        raise ValueError('Only stop pending orders supported')
    if action=='TRADE_ACTION_DEAL':
        if r['type'] not in ('ORDER_TYPE_BUY','ORDER_TYPE_SELL'):raise ValueError('Invalid close side')
        if market_execution:r.pop('price',None)
        elif 'price' not in r:raise ValueError('Price required outside Market Execution')
    for k in ('order','position','magic','deviation'):
        if k in r and (type(r[k]) is not int or r[k] < (0 if k=='deviation' else 1)):
            raise ValueError('Invalid integer '+k)
    for k in ('price','volume','sl','tp'):
        if k in r:
            if isinstance(r[k],bool):raise ValueError('Invalid numeric '+k)
            r[k]=float(r[k])
            if not isfinite(r[k]) or r[k]<0 or (k in ('price','volume') and r[k]==0):raise ValueError('Invalid numeric '+k)
    for k,allowed in dict(_ENUMS,action=set(_FIELDS)).items():
        if k in r:
            if r[k] not in allowed:raise ValueError('Unsupported enum '+k)
            r[k]=getattr(api,r[k])
    return r


class GuardedMT5Gateway:
    def __init__(self,api,*,account,server):
        self.api=api;self.session=RealMT5ReadOnlyAdapter(api,account=account,server=server)
        self._test_enabled=False;self._checked=None;self._lock=RLock()

    @classmethod
    def for_test_double(cls,api,*,account,server):
        if getattr(api,'_RECOVERY_TEST_DOUBLE',False) is not True:
            raise RuntimeError('Explicit test double required; real execution remains locked')
        obj=cls(api,account=account,server=server);obj._test_enabled=True
        return obj

    def _guard(self):
        if not self._test_enabled or getattr(self.api,'_RECOVERY_TEST_DOUBLE',False) is not True:
            raise RuntimeError('Real MT5 check/send safety lock')
        account,terminal=self.session._verify_session()
        if not account.trade_allowed or not terminal.trade_allowed:raise ValueError('Trading disabled')

    def order_check(self,request,*,market_execution=False):
        with self._lock:
            self._checked=None;self._guard()
            native=map_native_request(self.api,request,market_execution=market_execution)
            result=self.api.order_check(native)
            if result is not None and result.retcode==0:self._checked=deepcopy(native)
            return result

    def order_send(self,request,*,market_execution=False):
        with self._lock:
            approved=self._checked;self._checked=None  # consume BEFORE uncertain call
            self._guard()
            native=map_native_request(self.api,request,market_execution=market_execution)
            if approved is None or approved!=native:raise ValueError('Matching successful one-use order_check required')
            return self.api.order_send(native)
