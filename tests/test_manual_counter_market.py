"""Explicit MARKET counter entries with complete synthetic native readback."""
from copy import deepcopy
from unittest.mock import patch
import unittest
import test_manual_counter_entry as fixtures
from test_tradfi_mt5 import record

class ManualCounterMarket(unittest.TestCase):
    setUp=fixtures.ManualCounterEntry.setUp
    action=fixtures.ManualCounterEntry.action
    request=fixtures.ManualCounterEntry.request
    restart=fixtures.ManualCounterEntry.restart
    def fill_sender(self,*,lose_reply=False,change_source=False):
        api=self.f.api;original=api.order_send
        def send(req):
            if req['action']!=api.TRADE_ACTION_DEAL or 'position' in req:return original(req)
            api.sends.append(deepcopy(req));oid,pid,did=api.next_id(),api.next_id(),api.next_id()
            price=api.tick.ask if req['type']==0 else api.tick.bid
            common={k:req[k] for k in ('symbol','magic','comment','type')}
            api.history.append(record(**common,ticket=oid,state=api.ORDER_STATE_FILLED,volume_initial=req['volume'],volume_current=0.,price_open=price,sl=req['sl'],tp=req['tp'],position_id=pid,time_setup_msc=api.now*1000,time_done_msc=api.now*1000))
            api.deals.append(record(**common,ticket=did,order=oid,position_id=pid,entry=0,volume=req['volume'],price=price,profit=0.,reason=3,time_msc=api.now*1000,commission=0.,swap=0.,fee=0.))
            api.positions.append(record(ticket=api.next_id(),identifier=pid,symbol=req['symbol'],magic=req['magic'],type=req['type'],volume=req['volume'],price_open=price,sl=req['sl'],tp=req['tp']))
            if change_source:api.positions[0]=api.positions[0]._replace(sl=3997.)
            return None if lose_reply else record(retcode=api.TRADE_RETCODE_DONE,order=oid,deal=did,comment='synthetic')
        return send
    def test_explicit_market_full_fill_restart_then_source_pending_estimate(self):
        f=self.f
        with patch.object(f.api,'order_send',side_effect=self.fill_sender()):
            self.request('MARKET','now');f.tick(5)
        self.assertEqual(len(f.api.positions),3)
        self.assertEqual(sum(c['kind']=='MARKET' and c['status']=='FILLED' for c in f.bot.state['commands'].values()),2)
        before=deepcopy(f.api.sends);self.restart();f.tick(3);self.assertEqual(f.api.sends,before)
        f.api.positions=[p for p in f.api.positions if str(p.identifier)!=self.old['id']];f.tick(3)
        self.assertEqual(float(f.bot.state['cycle']['provisional_recovery'][self.old['id']]['estimated_close_net']),-3.)
    def test_source_change_between_siblings_does_not_send_second(self):
        f=self.f
        with patch.object(f.api,'order_send',side_effect=self.fill_sender(change_source=True)):
            self.request('MARKET','now');f.tick(4)
        market=[r for r in f.api.sends if r['action']==f.api.TRADE_ACTION_DEAL and 'position' not in r]
        self.assertEqual(len(market),1);before=deepcopy(f.api.sends)
        self.restart();f.tick(3);self.assertEqual(f.api.sends,before)
    def test_lost_reply_never_replays_same_entry_or_blindly_sends_sibling(self):
        f=self.f
        with patch.object(f.api,'order_send',side_effect=self.fill_sender(lose_reply=True)):
            self.request('MARKET','now');f.tick(6)
        sent=[r['comment'] for r in f.api.sends if r['action']==f.api.TRADE_ACTION_DEAL and 'position' not in r]
        self.assertEqual(len(sent),1);self.assertEqual(len(set(sent)),1)
