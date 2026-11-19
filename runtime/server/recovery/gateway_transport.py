"""Test-only native gateway + validated transport + durable outbox integration."""
from .mt5_transport import MT5Transport
from .mt5_execution_gateway import GuardedMT5Gateway


class GatewayTransport(MT5Transport):
    def __init__(self,api,*,account,server):
        super().__init__(api)
        self.gateway=GuardedMT5Gateway.for_test_double(api,account=account,server=server)
        self._execution_checked=None

    def _execution_policy(self,request):
        action=request['action']
        if action not in ('TRADE_ACTION_PENDING','TRADE_ACTION_DEAL'):return (False,None)
        info=self.api.symbol_info(request['symbol'])
        if info is None:raise ValueError('Symbol execution metadata unavailable')
        mode=getattr(info,'trade_exemode',None);flags=getattr(info,'filling_mode',None)
        names=('REQUEST','INSTANT','MARKET','EXCHANGE')
        modes={getattr(self.api,'SYMBOL_TRADE_EXECUTION_'+n):n for n in names}
        if type(mode) is not int or mode not in modes or type(flags) is not int or flags<0:
            raise ValueError('Unknown execution/filling metadata')
        name=modes[mode];filling=request['type_filling']
        if action=='TRADE_ACTION_PENDING':
            if filling!='ORDER_FILLING_RETURN':raise ValueError('Pending orders require RETURN')
            return (False,(mode,flags))
        if filling not in ('ORDER_FILLING_FOK','ORDER_FILLING_IOC'):
            raise ValueError('Close requires explicitly selected FOK or IOC')
        if name in ('MARKET','EXCHANGE'):
            bit=getattr(self.api,'SYMBOL_FILLING_FOK' if filling=='ORDER_FILLING_FOK' else 'SYMBOL_FILLING_IOC')
            if flags&bit!=bit:raise ValueError('Requested filling is unsupported by symbol')
        return (name=='MARKET',(mode,flags))

    def _order_check(self,request):
        self._execution_checked=None
        market,fingerprint=self._execution_policy(request)
        result=self.gateway.order_check(request,market_execution=market)
        if result is not None and result.retcode==0:self._execution_checked=(market,fingerprint)
        return result

    def _order_send(self,request):
        approved=self._execution_checked;self._execution_checked=None
        current=self._execution_policy(request)
        if approved is None or current!=approved:raise ValueError('Execution policy changed after check')
        return self.gateway.order_send(request,market_execution=current[0])
