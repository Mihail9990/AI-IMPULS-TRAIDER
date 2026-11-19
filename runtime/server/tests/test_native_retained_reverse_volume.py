"""Owner decision: preserve a prearmed reverse lot after manual/support exits.

At four full stops the current .01 position has a prearmed .02 opposite.
A non-full-stop exit must NOT resize/cancel that opposite, even though the
full-stop counter remains four. These tests use only the in-memory MT5 double.
"""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from recovery.native_coordinator import NativeCoordinator
from recovery.native_recovery_control import NativeRecoveryControl
from test_native_support_cycle import CycleFake, ea_response
import test_native_recovery_control as controls


class RetainedReverseVolumeTests(unittest.TestCase):
    session = controls.ControlTests.session

    def quote_near_entry(self, api, position):
        api.ask = position.price_open + (.21 if position.type == 0 else -.20)

    def reach_four_stops(self, api, session, root, side):
        n = NativeCoordinator(NativeRecoveryControl(session, magic=123))
        n.command('start', dict(kind='ranges', ranges=[dict(
            id='1', side=side, price='4000' if side == 'BUY' else '3950', volume='.01')],
            initial_sl_distance='.70', reentry_distance='.70'))
        current = n.control._read()['orders'][0]
        pid = 88
        api.fill_order(current['broker_order'], pid)
        for full_count in range(1, 5):
            p = api.positions[0]
            self.quote_near_entry(api, p)
            self.assertEqual(n.tick()['tick']['status'], 'ok')
            following = n.control._read()['reverse_commands'][current['key'] + ':initial-reverse']
            stop = str(p.sl)
            api.close(pid)
            deal = api.deals[-1].ticket
            pid += 1
            api.fill_order(following['broker_order'], pid)
            self.quote_near_entry(api, api.positions[0])
            self.assertEqual(n.tick()['tick']['status'], 'waiting_evidence')
            ea_response(n.ledger, current['broker_order'], deal, root, stop)
            self.assertEqual(n.tick()['tick']['status'], 'ok')
            self.assertEqual(n.ledger.totals()['1']['full_sl_count'], full_count)
            current = following
        self.assertEqual(api.positions[0].volume, .01)
        reverse = n.control._read()['reverse_commands'][current['key'] + ':initial-reverse']
        self.assertEqual(float(reverse['intent']['volume']), .02)
        return n, current, reverse, pid

    def run_retention(self, exit_kind):
        for side in ('BUY', 'SELL'):
            with self.subTest(side=side, exit_kind=exit_kind), tempfile.TemporaryDirectory() as root:
                Path(root, 'MQL5', 'Files', 'RECOVERY').mkdir(parents=True)
                api = CycleFake(root)
                database = root + '/state.db'
                with self.session(api, database) as session:
                    n, current, reverse, pid = self.reach_four_stops(api, session, root, side)
                    ticket = reverse['broker_order']
                    original = deepcopy(vars(next(o for o in api.orders if o.ticket == ticket)))
                    if exit_kind == 'support':
                        p = api.positions[0]
                        api.ask = p.price_open + (.46 if p.type == 0 else -.25)
                        self.assertEqual(n.tick()['tick']['status'], 'ok')
                        support_stop = str(p.sl)
                        self.assertNotEqual(support_stop, current['intent']['sl'])
                        api.close(pid)
                        api.ask = p.price_open + (-.20 if p.type == 0 else .40)
                        deal = api.deals[-1].ticket
                        self.assertEqual(n.tick()['tick']['status'], 'waiting_evidence')
                        ea_response(n.ledger, current['broker_order'], deal, root, support_stop)
                    else:
                        api.close(pid, reason=api.DEAL_REASON_MOBILE)
                    sends_before = len(api.sent)
                    self.assertEqual(n.tick()['tick']['status'], 'ok')
                    self.assertEqual(n.ledger.totals()['1']['full_sl_count'], 4)
                    self.assertEqual(vars(next(o for o in api.orders if o.ticket == ticket)), original)
                    if exit_kind == 'manual':
                        self.assertEqual(len(api.sent), sends_before)
                    else:
                        reentry = next(iter(n.control._read()['reentry_commands'].values()))
                        self.assertEqual(float(reentry['intent']['volume']), .01)
                        self.assertEqual(len(api.sent), sends_before + 1)
                    retained_sends = len(api.sent)
                # A real durable session reopen must retain identity, level, SL,
                # volume, and count, without cancel/recreate to the current slot.
                with self.session(api, database) as session:
                    control = NativeRecoveryControl(session, magic=123)
                    control.reconcile()
                    n = NativeCoordinator(control)
                    self.assertEqual(n.tick()['tick']['status'], 'ok')
                    self.assertEqual(len(api.sent), retained_sends)
                    self.assertEqual(vars(next(o for o in api.orders if o.ticket == ticket)), original)
                    api.fill_order(ticket, pid + 1)
                    self.quote_near_entry(api, api.positions[0])
                    self.assertEqual(n.tick()['tick']['status'], 'ok')
                    self.assertEqual(api.positions[0].volume, .02)
                    self.assertEqual(n.ledger.totals()['1']['full_sl_count'], 4)
                    self.assertFalse(any(r['action'] == api.TRADE_ACTION_REMOVE and r.get('order') == ticket
                                         for r in api.sent))
                    if exit_kind == 'support':
                        # Opposite wins: retire the unused same-side .01 reentry,
                        # not the preserved .02 reverse which already executed.
                        self.assertTrue(any(r['action'] == api.TRADE_ACTION_REMOVE and
                                            r.get('order') == reentry['broker_order'] for r in api.sent))

    def test_manual_exit_preserves_prearmed_point02_at_four_full_stops(self):
        self.run_retention('manual')

    def test_support_exit_preserves_prearmed_point02_at_four_full_stops(self):
        self.run_retention('support')
