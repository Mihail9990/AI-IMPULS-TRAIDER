import unittest
from types import SimpleNamespace as NS
from uuid import uuid4
from fastapi import HTTPException
from pydantic import ValidationError
from service import Bridge, Change, Settings, create_app

class MT5:
    TRADE_ACTION_SLTP = 6
    def __init__(self, side=0):
        self.p = NS(ticket=1, symbol='XAUUSD.s', type=side, volume=.01, price_open=2000., sl=1990. if side==0 else 2010., tp=2010. if side==0 else 1990., profit=0.)
        self.calls = []; self.login=123; self.retcode=10009; self.apply=True; self.fail=False
    def account_info(self): return NS(login=self.login, server='test')
    def terminal_info(self): return NS(connected=True)
    def positions_get(self, **kw): return (self.p,) if self.p else ()
    def symbol_info(self, symbol): return NS(trade_tick_size=.01, point=.01, trade_stops_level=0, trade_freeze_level=0)
    def symbol_info_tick(self, symbol): return NS(bid=2000.,ask=2000.)
    def order_send(self, request):
        self.calls.append(request)
        if self.fail: raise RuntimeError('timeout')
        if self.apply and self.retcode==10009: self.p.sl=request['sl']; self.p.tp=request['tp']
        return NS(retcode=self.retcode)

class Test(unittest.TestCase):
    def setup_bridge(self, side=0, live=True):
        mt=MT5(side); b=Bridge(mt,Settings('x'*32,123,'test','unused',live,':memory:')); return mt,b
    def req(self, **kwargs): return Change(request_id=uuid4(), expected_symbol='XAUUSD.s',expected_entry=2000,expected_side=kwargs.pop('expected_side','BUY'),expected_volume=.01,expected_identifier=0,**kwargs)
    def test_buy(self):
        mt,b=self.setup_bridge(); r=b.change(1,self.req(tp_distance=.25,sl_distance=.7)); self.assertEqual(r['status'],'verified'); self.assertEqual(mt.p.tp,2000.25); self.assertEqual(mt.p.sl,1999.3)
    def test_sell(self):
        mt,b=self.setup_bridge(1); b.change(1,self.req(tp_distance=.25,sl_distance=.7,expected_side='SELL')); self.assertEqual(mt.p.tp,1999.75); self.assertEqual(mt.p.sl,2000.7)
    def test_preserve_sl(self):
        mt,b=self.setup_bridge(); b.change(1,self.req(tp_distance=.25)); self.assertEqual(mt.p.sl,1990)
    def test_preserve_tp(self):
        mt,b=self.setup_bridge(); b.change(1,self.req(sl_distance=.7)); self.assertEqual(mt.p.tp,2010)
    def test_empty(self):
        mt,b=self.setup_bridge()
        with self.assertRaises(HTTPException): b.change(1,self.req())
        self.assertFalse(mt.calls)
    def test_bad_distance(self):
        for value in [0,-1,float('nan'),float('inf')]:
            with self.assertRaises(ValidationError): self.req(tp_distance=value)
    def test_account(self):
        mt,b=self.setup_bridge(); mt.login=999
        with self.assertRaises(HTTPException): b.change(1,self.req(tp_distance=1))
        self.assertFalse(mt.calls)
    def test_stale(self):
        mt,b=self.setup_bridge(); mt.p.price_open=2001
        with self.assertRaises(HTTPException): b.change(1,self.req(tp_distance=1))
        self.assertFalse(mt.calls)
    def test_read_only(self):
        mt,b=self.setup_bridge(live=False); self.assertEqual(b.change(1,self.req(tp_distance=1))['status'],'rejected'); self.assertFalse(mt.calls)
    def test_idempotence(self):
        mt,b=self.setup_bridge(); req=self.req(tp_distance=1); b.change(1,req); b.change(1,req); self.assertEqual(len(mt.calls),1)
    def test_unknown_no_resend(self):
        mt,b=self.setup_bridge(); mt.fail=True; req=self.req(tp_distance=1)
        self.assertEqual(b.change(1,req)['status'],'unknown'); b.change(1,req); self.assertEqual(len(mt.calls),1)
    def test_unconfirmed(self):
        mt,b=self.setup_bridge(); mt.apply=False; self.assertEqual(b.change(1,self.req(tp_distance=1))['status'],'unknown')
    def test_rejected(self):
        mt,b=self.setup_bridge(); mt.retcode=10016; self.assertEqual(b.change(1,self.req(tp_distance=1))['status'],'rejected')
    def test_closed(self):
        mt,b=self.setup_bridge(); mt.p=None
        with self.assertRaises(HTTPException): b.change(1,self.req(tp_distance=1))
        self.assertFalse(mt.calls)
    def test_broker_timeout_unknown(self):
        mt,b=self.setup_bridge(); mt.retcode=10012
        self.assertEqual(b.change(1,self.req(tp_distance=1))['status'],'unknown')
    def test_expected_identity(self):
        for field,value in [('type',1),('volume',.02),('identifier',77)]:
            mt,b=self.setup_bridge(); setattr(mt.p,field,value)
            with self.assertRaises(HTTPException): b.change(1,self.req(tp_distance=1))
            self.assertFalse(mt.calls)
    def test_final_read_preserves_new_stop(self):
        mt,b=self.setup_bridge(); base=mt.positions_get; count=0
        def positions(**kw):
            nonlocal count
            count+=1
            if count==2:
                mt.p=NS(**vars(mt.p)); mt.p.sl=1998
            return base(**kw)
        mt.positions_get=positions
        self.assertEqual(b.change(1,self.req(tp_distance=1))['status'],'verified')
        self.assertEqual(mt.calls[0]['sl'],1998)
    def test_final_read_rejects_changed_identity(self):
        mt,b=self.setup_bridge(); base=mt.positions_get; count=0
        def positions(**kw):
            nonlocal count
            count+=1
            if count==2:
                mt.p=NS(**vars(mt.p)); mt.p.volume=.02
            return base(**kw)
        mt.positions_get=positions
        with self.assertRaises(HTTPException): b.change(1,self.req(tp_distance=1))
        self.assertFalse(mt.calls)
    def test_token(self):
        import asyncio, httpx
        mt,b=self.setup_bridge()
        app=create_app(mt,b.settings)
        async def check():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as c:
                self.assertEqual((await c.get('/api/positions')).status_code,401)
                self.assertEqual((await c.get('/api/positions',headers={'Authorization':'Bearer '+'x'*32})).status_code,200)
        asyncio.run(check())

if __name__=='__main__': unittest.main()
