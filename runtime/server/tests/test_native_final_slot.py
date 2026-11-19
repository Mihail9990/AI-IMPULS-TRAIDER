"""Final-slot owner decision, tested solely against the in-memory MT5 double."""
from pathlib import Path
import tempfile
import unittest

from recovery.native_coordinator import NativeCoordinator
from recovery.native_recovery_control import NativeRecoveryControl
from test_native_support_cycle import CycleFake, ea_response
import test_native_recovery_control as controls


class FinalSlotTests(unittest.TestCase):
    session = controls.ControlTests.session

    def quote(self, api):
        p = api.positions[0]
        api.ask = p.price_open + (.21 if p.type == 0 else -.20)

    def open_coordinator(self, session):
        c = NativeRecoveryControl(session, magic=123)
        c.reconcile()
        return NativeCoordinator(c)

    def reach_final_slot(self, api, session, root):
        n = self.open_coordinator(session)
        n.command('start', dict(kind='ranges', ranges=controls.RANGES,
                               initial_sl_distance='.70', reentry_distance='.70'))
        current = n.control._read()['orders'][0]
        pid = 88
        api.fill_order(current['broker_order'], pid)
        for count in range(1, 24):
            self.quote(api)
            self.assertEqual(n.tick()['tick']['status'], 'ok')
            following = n.control._read()['reverse_commands'][current['key'] + ':initial-reverse']
            stop = str(api.positions[0].sl)
            api.close(pid)
            deal = api.deals[-1].ticket
            pid += 1
            api.fill_order(following['broker_order'], pid)
            self.quote(api)
            self.assertEqual(n.tick()['tick']['status'], 'waiting_evidence')
            ea_response(n.ledger, current['broker_order'], deal, root, stop)
            self.assertEqual(n.tick()['tick']['status'], 'ok')
            self.assertEqual(n.ledger.totals()['1']['full_sl_count'], count)
            current = following
        self.assertEqual(api.positions[0].volume, .10)
        self.assertEqual(api.orders, [])
        self.assertNotIn(current['key'] + ':initial-reverse', n.control._read()['reverse_commands'])
        return current, pid

    def run_exit(self, exit_kind):
        with tempfile.TemporaryDirectory() as root:
            Path(root, 'MQL5', 'Files', 'RECOVERY').mkdir(parents=True)
            api = CycleFake(root)
            database = root + '/state.db'
            with self.session(api, database) as session:
                current, pid = self.reach_final_slot(api, session, root)
                sent = len(api.sent)
            # Restart with the final .10 position still open: never prearm #25.
            with self.session(api, database) as session:
                n = self.open_coordinator(session)
                self.assertEqual(n.tick()['tick']['status'], 'ok')
                self.assertEqual(len(api.sent), sent)
                self.assertEqual(api.orders, [])
                if exit_kind == 'support':
                    p = api.positions[0]
                    api.ask = p.price_open + (.46 if p.type == 0 else -.25)
                    self.assertEqual(n.tick()['tick']['status'], 'ok')
                stop = str(api.positions[0].sl)
                p = api.positions[0]
                api.close(pid, reason=api.DEAL_REASON_MOBILE if exit_kind == 'manual' else api.DEAL_REASON_SL)
                api.ask = p.price_open + (-.20 if p.type == 0 else .40)
                deal = api.deals[-1].ticket
                if exit_kind != 'manual':
                    self.assertEqual(n.tick()['tick']['status'], 'waiting_evidence')
                    ea_response(n.ledger, current['broker_order'], deal, root, stop)
                result = n.tick()
                if exit_kind == 'manual':
                    self.assertEqual(result['tick']['status'], 'interrupted_chain')
                    self.assertEqual(result['data']['range_modes']['1'], 'interrupted')
                    self.assertEqual(n.ledger.totals()['1']['full_sl_count'], 23)
                    self.assertEqual(api.orders, [])
                elif exit_kind == 'full':
                    self.assertEqual(result['data']['range_modes']['1'], 'manual')
                    self.assertEqual(n.ledger.totals()['1']['full_sl_count'], 24)
                    self.assertEqual(api.orders, [])
                else:
                    self.assertEqual(result['tick']['status'], 'ok')
                    self.assertEqual(n.ledger.totals()['1']['full_sl_count'], 23)
                    reentry = next(iter(n.control._read()['reentry_commands'].values()))
                    self.assertEqual(float(reentry['intent']['volume']), .10)
                    self.assertEqual(len(api.orders), 1)
                    self.assertEqual(api.orders[0].ticket, reentry['broker_order'])
                sent = len(api.sent)
            # Restart flat or with the support reentry pending must not duplicate.
            with self.session(api, database) as session:
                n = self.open_coordinator(session)
                n.tick()
                self.assertEqual(len(api.sent), sent)
                if exit_kind == 'support':
                    api.fill_order(reentry['broker_order'], pid + 1)
                    self.quote(api)
                    self.assertEqual(n.tick()['tick']['status'], 'ok')
                    self.assertEqual(api.positions[0].volume, .10)
                    self.assertEqual(api.orders, [])
                    self.assertNotIn(reentry['key'] + ':initial-reverse', n.control._read()['reverse_commands'])
                    stop = str(api.positions[0].sl)
                    api.close(pid + 1)
                    deal = api.deals[-1].ticket
                    self.assertEqual(n.tick()['tick']['status'], 'waiting_evidence')
                    ea_response(n.ledger, reentry['broker_order'], deal, root, stop)
                    result = n.tick()
                    self.assertEqual(result['data']['range_modes']['1'], 'manual')
                    self.assertEqual(n.ledger.totals()['1']['full_sl_count'], 24)
                    self.assertEqual(api.orders, [])
                    sent = len(api.sent)
                    n.tick()
                    self.assertEqual(len(api.sent), sent)

    def test_final_full_stop_enters_manual_without_25th_order_across_restart(self):
        self.run_exit('full')

    def test_final_manual_close_stays_interrupted_at_23_across_restart(self):
        self.run_exit('manual')

    def test_final_support_close_reenters_point10_without_opposite_across_restart(self):
        self.run_exit('support')
