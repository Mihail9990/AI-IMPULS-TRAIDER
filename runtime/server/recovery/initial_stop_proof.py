"""Narrow owner-bound proof for the first actual-fill SL correction.

Caller-supplied scope is only a reference, not evidence. All authorization is
rederived from the owner journal and fresh exact broker entry history.
"""
from .engine import number, sign
from .native_entry_reader import NativeEntryReader


class InitialStopProof:
    def __init__(self, control):
        self.control = control

    def validate(self, request, expected, position):
        c = self.control
        with c.session._lock:
            c._guard()
            config = c.strategy_config()
            data = c._read()
            key = expected.get('initial_stop_key')
            record = (data or {}).get('protection_commands', {}).get(key)
            if (not isinstance(key, str) or not record or record['stage'] != 0
                    or record['phase'] != 'dispatching' or record['request'] != request
                    or record['scope'] != expected):
                raise ValueError('Durable initial protection checkpoint required')
            pid = record['position_identifier']
            if str(pid) in data.get('confirmed_protection', {}):
                raise ValueError('Initial correction already confirmed')
            item = next((x for x in data['orders']
                         if x['intent']['range_id'] == record['range_id']), None)
            if (not item or item['phase'] != 'verified'
                    or key != item['key'] + ':protection:0'):
                raise ValueError('Verified original entry required')
            prior = data.get('entry_observations', {}).get(str(item['broker_order']))
            if not prior or prior['phase'] != 'awaiting_protection':
                raise ValueError('Complete observed entry required')
            evidence = NativeEntryReader(c.session).read(item, magic=c.magic)
            if (evidence['pending_order'] is not None or not evidence['terminal_order']
                    or evidence['position_identifier'] != pid or evidence['position'] is None
                    or evidence['exit_deals']):
                raise ValueError('Open complete initial entry without exits required')
            baseline = prior['evidence']
            # No late entry/exit change may silently change a previously planned SL.
            for field in ('deals', 'exit_deals', 'position_identifier', 'filled_volume', 'average_price'):
                if evidence[field] != baseline[field]:
                    raise ValueError('Entry evidence changed after protection planning')
            fresh = evidence['position']
            for field in ('ticket','identifier','magic','symbol','side','entry','volume','sl','tp'):
                if fresh[field] != position[field]:
                    raise ValueError('Position changed during initial protection validation')
            direction = sign(item['intent']['side'])
            target = number(evidence['average_price']) - direction * number(config['settings']['initial_sl_distance'])
            if (number(position['sl']) != number(item['request']['sl'])
                    or number(request['sl']) != target
                    or number(request['tp']) != number(position['tp'])
                    or position['identifier'] != expected['identifier']
                    or request['position'] != position['ticket']):
                raise ValueError('Only exact original-stop actual-fill correction allowed')
            return True
