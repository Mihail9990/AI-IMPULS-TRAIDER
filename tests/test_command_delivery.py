import unittest
from copy import deepcopy
from types import SimpleNamespace
from tradfi.command_delivery import trigger_delivery


class CommandDeliveryTests(unittest.TestCase):
    def controller(self, result=None):
        return SimpleNamespace(state={'cycle': {'id': 'cycle'}, 'commands': {
            'command': {'cycle_id': 'cycle', 'kind': 'STOP', 'status': 'UNKNOWN'}}},
            broker=SimpleNamespace(result=lambda _: result or {'status': 'UNKNOWN'}))

    def stage(self, controller):
        return trigger_delivery(controller)['counts']

    def test_ack_is_not_fill_and_does_not_mutate_durable_unknown(self):
        c = self.controller({'status': 'ACCEPTED', 'order_id': 'secret', 'reason': 'MT5_RESULT'})
        before = deepcopy(c.state)
        self.assertEqual(self.stage(c), {'ACKNOWLEDGED_AWAITING_READBACK': 1})
        self.assertEqual(c.state, before)
        self.assertNotIn('secret', str(trigger_delivery(c)))

    def test_absent_response_distinct_from_unconfirmed(self):
        c = self.controller({'status': 'UNKNOWN', 'reason': 'No order_send result'})
        self.assertEqual(self.stage(c), {'NO_RESPONSE_RECONCILING': 1})
        self.assertEqual(self.stage(self.controller()), {'OUTCOME_UNCONFIRMED': 1})

    def test_sending_is_volatile_and_restart_never_claims_sending(self):
        c = self.controller()
        c._submitting_command_id = 'command'
        self.assertEqual(self.stage(c), {'SENDING': 1})
        del c._submitting_command_id
        self.assertEqual(self.stage(c), {'OUTCOME_UNCONFIRMED': 1})

    def test_confirmed_pending_and_terminal_not_reported_as_waiting(self):
        for status in ('PENDING', 'FILLED', 'DONE', 'REJECTED', 'OBSOLETE'):
            c = self.controller()
            c.state['commands']['command']['status'] = status
            self.assertEqual(self.stage(c), {})

    def test_other_cycle_and_kind_and_superseded_are_ignored(self):
        for field, value in [('cycle_id', 'old'), ('kind', 'MARKET'), ('edit_superseded', True)]:
            c = self.controller()
            c.state['commands']['command'][field] = value
            self.assertEqual(self.stage(c), {})

    def test_no_cycle_does_not_display_old_commands(self):
        c = self.controller()
        c.state['cycle'] = None
        self.assertEqual(self.stage(c), {})

    def test_rejected_response_not_claimed_unknown(self):
        self.assertEqual(self.stage(self.controller({'status': 'REJECTED'})),
                         {'REJECTED_AWAITING_RECONCILIATION': 1})

    def test_read_failure_never_claims_accepted(self):
        c = self.controller()
        def unavailable(_):
            raise OSError('private raw error')
        c.broker.result = unavailable
        self.assertEqual(self.stage(c), {'OUTCOME_UNCONFIRMED': 1})

    def test_identity_mismatch_never_breaks_view_or_claims_acceptance(self):
        c = self.controller()
        def mismatch(_):
            raise ValueError('ack identity mismatch: private ID')
        c.broker.result = mismatch
        self.assertEqual(self.stage(c), {'OUTCOME_UNCONFIRMED': 1})

    def test_dispatch_marker_clears_and_unknown_never_resends(self):
        from tradfi.engine import Controller
        for raises in (False, True):
            c = self.controller()
            c.state['commands']['command'].update(id='command', status='READY')
            calls = []
            c._deadline_guard = c._time_guard = lambda: None
            c._mutation_allowed = lambda _: True
            c.save = lambda _: None
            c.observation = SimpleNamespace(fail=lambda _: None)
            c._restore_deferred_entry = lambda _: False
            def submit(_):
                calls.append(1)
                self.assertEqual(c.state['commands']['command']['status'], 'UNKNOWN')
                self.assertEqual(self.stage(c), {'SENDING': 1})
                if raises:
                    raise OSError('offline test')
            c.broker.submit = submit
            Controller.dispatch(c)
            self.assertIsNone(c._submitting_command_id)
            self.assertEqual(self.stage(c), {'OUTCOME_UNCONFIRMED': 1})
            Controller.dispatch(c)
            self.assertEqual(len(calls), 1)

    def test_ready_does_not_read_broker_or_send(self):
        c = self.controller()
        c.state['commands']['command']['status'] = 'READY'
        c.broker = object()
        self.assertEqual(self.stage(c), {'QUEUED': 1})


if __name__ == '__main__':
    unittest.main()
