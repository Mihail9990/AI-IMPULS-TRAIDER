import tempfile
import unittest
from pathlib import Path
from recovery.native_coordinator import NativeCoordinator
from recovery.native_recovery_control import NativeRecoveryControl
from test_native_support_cycle import CycleFake, ea_response
import test_native_recovery_control as controls
from test_native_recovery_control import RANGES


class CoordinatorTests(unittest.TestCase):
    session = controls.ControlTests.session

    def setup_native(self, d):
        Path(d, 'MQL5', 'Files', 'RECOVERY').mkdir(parents=True)
        a = CycleFake(d)
        return a, self.session(a, d + '/s.db')

    def start(self, s):
        n = NativeCoordinator(NativeRecoveryControl(s, magic=123))
        n.command('start', dict(kind='ranges', ranges=RANGES,
                              initial_sl_distance='.70', reentry_distance='.70'))
        return n

    def test_no_startup_or_get_sends_and_command_replay(self):
        with tempfile.TemporaryDirectory() as d:
            a, session = self.setup_native(d)
            with session as s:
                n = NativeCoordinator(NativeRecoveryControl(s, magic=123))
                n.state(); n.tick(); self.assertFalse(a.sent)
                payload = dict(kind='ranges', ranges=RANGES, initial_sl_distance='.70', reentry_distance='.70')
                first = n.command('x', payload)
                self.assertTrue(first['executor_ready'])
                self.assertFalse(first['api_reconciliation_required'])
                self.assertEqual(n.command('x', payload), first)
                self.assertEqual(len(a.sent), len(RANGES))
                with self.assertRaises(ValueError): n.command('x', {'kind':'cancel'})
                count = len(a.sent); n.state(); self.assertEqual(len(a.sent), count)

    def test_support_cycle_and_external_manual_close_keeps_pending(self):
        with tempfile.TemporaryDirectory() as d:
            a, session = self.setup_native(d)
            with session as s:
                n = self.start(s); a.fill_order(51, 88); a.ask = 4000.21
                self.assertEqual(n.tick()['tick']['status'], 'ok')
                reverse = n.control._read()['reverse_commands']
                opposite = next(iter(reverse.values()))['broker_order']
                a.ask = 4000.46; n.tick(); a.close(88); a.ask = 3999.8
                self.assertEqual(n.tick()['tick']['status'], 'waiting_evidence')
                ea_response(n.ledger, 51, 602, d, '3999.75')
                self.assertEqual(n.tick()['tick']['status'], 'ok')
                reentry = next(iter(n.control._read()['reentry_commands'].values()))
                a.fill_order(reentry['broker_order'], 89); a.ask = 4000.5
                self.assertEqual(n.tick()['tick']['status'], 'ok')
                self.assertNotIn(opposite, [o.ticket for o in a.orders])
                a.close(89, reason=0)
                pending = [o.ticket for o in a.orders]; n.tick()
                self.assertEqual([o.ticket for o in a.orders], pending)

    def test_unknown_checkpoint_never_replayed(self):
        with tempfile.TemporaryDirectory() as d:
            a, session = self.setup_native(d)
            with session as s:
                n = self.start(s)
                with n.control.db:
                    n.control.db.execute("INSERT INTO native_api_commands VALUES('unknown','{}','dispatching',NULL)")
                count = len(a.sent)
                with self.assertRaises(RuntimeError): n.command('unknown', {})
            with self.session(a, d+'/s.db') as s:
                n = NativeCoordinator(NativeRecoveryControl(s,magic=123))
                self.assertTrue(s.ready)
                self.assertFalse(n.state()['executor_ready'])
                self.assertTrue(n.state()['api_reconciliation_required'])
                a.fill_order(51,88); a.ask=4000.21
                self.assertEqual(n.tick()['tick']['status'], 'reconciliation_required')
                with self.assertRaises(RuntimeError): n.command('new-id', {'kind':'cancel'})
                self.assertEqual(len(a.sent), count)

    def test_full24_automatic_tick_stops_in_manual_and_restart_no_send(self):
        with tempfile.TemporaryDirectory() as d:
            a, session = self.setup_native(d)
            with session as s:
                n = self.start(s)
                current = n.control._read()['orders'][0]
                pid = 88
                a.fill_order(current['broker_order'], pid)
                for slot in range(1, 25):
                    p = next(p for p in a.positions if p.identifier == pid)
                    a.ask = p.price_open + .21 if p.type == 0 else p.price_open - .20
                    outcome = n.tick()
                    self.assertEqual(outcome['tick']['status'], 'ok', outcome['tick'])
                    data = n.control._read()
                    following = data.get('reverse_commands', {}).get(current['key']+':initial-reverse')
                    if slot == 24: self.assertIsNone(following)
                    else: self.assertIsNotNone(following)
                    stop = str(p.sl)
                    a.close(pid)
                    deal = a.deals[-1].ticket
                    if following:
                        pid += 1; a.fill_order(following['broker_order'],pid)
                        p = next(p for p in a.positions if p.identifier == pid)
                        a.ask = p.price_open + .21 if p.type == 0 else p.price_open - .20
                    self.assertEqual(n.tick()['tick']['status'], 'waiting_evidence')
                    ea_response(n.ledger,current['broker_order'],deal,d,stop)
                    self.assertEqual(n.tick()['tick']['status'], 'ok')
                    self.assertEqual(n.ledger.totals()['1']['full_sl_count'],slot)
                    if following: current = following
                self.assertEqual(n.control._read()['range_modes']['1'],'manual')
                count = len(a.sent)
                n = NativeCoordinator(NativeRecoveryControl(s,magic=123))
                n.tick(); self.assertEqual(len(a.sent),count)

    def test_unmapped_owned_exposure_pauses_without_send(self):
        from copy import deepcopy
        with tempfile.TemporaryDirectory() as d:
            a, session = self.setup_native(d)
            with session as s:
                n = self.start(s)
                unknown = deepcopy(a.orders[0]); unknown.ticket = 999
                a.orders.append(unknown)
                count = len(a.sent)
                outcome = n.tick()
                self.assertEqual(outcome['tick']['status'], 'reconciliation_required')
                self.assertEqual(len(a.sent), count)

    def test_disabled_cancel_and_bad_close_leave_protection_running(self):
        with tempfile.TemporaryDirectory() as d:
            a, session = self.setup_native(d)
            with session as s:
                n = self.start(s); a.fill_order(51,88); a.ask=4000.21; n.tick()
                for key, payload in [('cancel', {'kind':'cancel'}),
                                     ('bad-close', {'kind':'close_cancel','range_id':'99'})]:
                    with self.assertRaises((RuntimeError,ValueError)): n.command(key,payload)
                    self.assertTrue(s.ready)
                    self.assertFalse(n._unfinished_api_command())
                a.ask=4000.46
                self.assertEqual(n.tick()['tick']['status'],'ok')
                self.assertEqual(a.positions[0].sl,3999.75)
