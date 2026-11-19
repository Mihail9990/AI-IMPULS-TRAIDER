"""Native MT5 transport for durable Executor, not an autonomous strategy runner.

Accepts a caller-owned initialized MT5 API and mandatory runtime ownership guard.
No initialize/login, account-type switching or automatic retries. This module CAN
send a validated request when used by an active owner; tests inject API doubles.
Do not wire it into the mock-only strategy by deleting that strategy's guards.
"""
from copy import deepcopy
from threading import RLock
from .native_preflight import NativeMT5Preflight, TerminalBinding


class NativeMT5Transport:
    def __init__(self, api, binding, *, owner_guard):
        if not isinstance(binding,TerminalBinding):raise TypeError('TerminalBinding required')
        if not callable(owner_guard):raise TypeError('Active runtime owner guard required')
        self.api=api
        self.binding=binding
        self.validator=NativeMT5Preflight(api,binding)
        self._owner_guard=owner_guard
        self._approved=None
        self._lock=RLock()

    def _guard(self):
        if self._owner_guard() is not True:
            self._approved=None
            raise RuntimeError('Active native runtime ownership required')

    def preflight(self,request,expected):
        with self._lock:
            self._approved=None
            self._guard()
            request,expected=deepcopy(request),deepcopy(expected)
            checked=self.validator.check(request,expected)
            self._guard()
            fingerprint=self.validator._execution_policy(request)
            self._approved=(request,expected,deepcopy(checked['request']),fingerprint)

    def send(self,request):
        with self._lock:
            approved=self._approved
            self._approved=None  # consume before any fallible/uncertain API call
            self._guard()
            if approved is None or request!=approved[0]:
                raise ValueError('Matching one-use preflight required')
            original,expected,native,fingerprint=approved
            # Re-check against current broker state, then revalidate after the
            # blocking check call. Account/position/policy changes fail closed.
            checked=self.validator.check(original,expected)
            current=self.validator.validate(original,expected)
            current_policy=self.validator._execution_policy(original)
            self._guard()
            if checked['request']!=native or current!=native or current_policy!=fingerprint:
                raise ValueError('Native request or execution policy changed since preflight')
            # Final metadata lookup above may itself observe a switched terminal.
            # Recheck identity after all metadata reads, immediately before send.
            self.validator._flat_cancel(original, expected)
            self.validator._session()
            result=self.api.order_send(deepcopy(native))
            # Any owner/account fault after sending is uncertain, not rejection.
            self._guard()
            self.validator._session()
            if result is None:return {'status':'unknown'}
            retcode=getattr(result,'retcode',None)
            order=getattr(result,'order',0);deal=getattr(result,'deal',0)
            if type(retcode) is not int or type(order) is not int or type(deal) is not int or order<0 or deal<0:
                return {'status':'unknown'}
            receipt={'order':order,'deal':deal,'retcode':retcode}
            accepted={self.api.TRADE_RETCODE_DONE,self.api.TRADE_RETCODE_PLACED}
            if retcode in accepted:return dict(receipt,status='accepted')
            # TIMEOUT, DONE_PARTIAL and unfamiliar codes must never be retried.
            rejection_names=('TRADE_RETCODE_REJECT','TRADE_RETCODE_INVALID',
                'TRADE_RETCODE_INVALID_VOLUME','TRADE_RETCODE_INVALID_PRICE',
                'TRADE_RETCODE_INVALID_STOPS','TRADE_RETCODE_TRADE_DISABLED',
                'TRADE_RETCODE_MARKET_CLOSED','TRADE_RETCODE_NO_MONEY')
            rejected={getattr(self.api,n) for n in rejection_names if hasattr(self.api,n)}
            return dict(receipt,status='rejected' if retcode in rejected else 'unknown')

    def snapshot(self,expected):
        with self._lock:
            self._guard()
            self.validator._scope(expected)
            result=self.validator.session.snapshot(expected)
            self._guard()
            return result

    def invalidate(self):
        with self._lock:self._approved=None
