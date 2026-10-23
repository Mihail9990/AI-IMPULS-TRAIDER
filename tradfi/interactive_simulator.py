"""Explicit artificial adapter for interactive development; never imports MT5."""
from copy import deepcopy
import time
from decimal import Decimal as D
from .simulator import Simulator


class InteractiveSimulator(Simulator):
    backend_id = 'interactive_simulator_v1'

    def quote(self,bid,ask):
        super().quote(bid,ask)
        self.data['quote']['observed_at_utc']=time.time()

    def fill(self, *args, **kwargs):
        pid=super().fill(*args, **kwargs)
        self.data['deals'][-1].update(commission='0',swap='0',fee='0')
        return pid

    def close(self, reason='SL', price=None, profit=None, *, commission='0',swap='0',fee='0',position_id=None):
        p=next(x for x in self.data['positions'] if position_id is None or x['id']==position_id)
        self.data['positions'].remove(p)
        price=str(price if price is not None else p['sl' if reason=='SL' else 'tp'])
        pnl=self.profit(p['side'],D(p['size']),D(p['entry']),D(price)) if profit is None else D(profit)
        self.quote(D(price)-D('.05'),D(price)+D('.05'))
        self.data['deals'].append(dict(id='out-'+p['id'],position_id=p['id'],order_id='exit-'+p['id'],
            command_id='',entry='OUT',side=p['side'],volume=p['size'],price=price,reason=reason,
            profit=str(pnl),commission=str(commission),swap=str(swap),fee=str(fee),sl=p['sl'],tp=p['tp'],time=self.data['now']))
        return p

    def submit(self, cmd):
        if cmd['kind']!='CLOSE':return super().submit(cmd)
        if any(x['id']==cmd['id'] for x in self.commands):raise AssertionError('Duplicate close intent')
        self.commands.append(deepcopy(cmd))
        if 'CLOSE' in self.fail_before:raise TimeoutError('Simulated unknown close request')
        p=next((x for x in self.data['positions'] if x['id']==cmd['position_id']),None)
        if not p or not p['owned'] or D(p['size'])!=D(cmd['size']):
            self.results[cmd['id']]={'status':'REJECTED','never_sent':True};return
        price=self.data['quote']['bid' if p['side']=='BUY' else 'ask']
        self.close(reason=cmd['purpose'],price=price,position_id=p['id'])
        self.data['deals'][-1]['close_command_id']=cmd['id']
        # Acknowledgement cannot prove execution; controller requires deal readback.
        self.results[cmd['id']]={'status':'ACCEPTED'}
        if 'CLOSE' in self.fail_after:
            self.results.pop(cmd['id'],None)
            raise TimeoutError('Simulated lost close response')
