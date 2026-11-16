"""Display-only alignment cannot authorize a trade or label broker time UTC."""
from types import SimpleNamespace
import unittest

from tradfi.raw_market_time import MT5DisplayClock
from tradfi.engine import Controller
import test_raw_market_mode as raw_tests
from test_m1_deferred_control import ControllerDouble


class DisplayClockTests(unittest.TestCase):
    def setUp(self):
        self.mono=123.
        self.clock=MT5DisplayClock(lambda:self.mono)
        self.tick=SimpleNamespace(time=36000,time_msc=36000500)

    def test_explicit_alignment_and_progress_is_never_utc_proof(self):
        self.clock.connection(True)
        self.assertEqual(self.clock.view()['status'],'UNALIGNED')
        initial=self.clock.align(self.tick)
        self.assertEqual(initial['estimated_broker_time'],36000.5)
        self.mono+=12.25;value=self.clock.view()
        self.assertEqual(value['estimated_broker_time'],36012.75)
        self.assertEqual(value['broker_time'],36000.5)
        self.assertEqual(value['timestamp_semantics'],'BROKER_RAW')
        self.assertFalse(value['verified_utc'])

    def test_disconnect_restart_and_monotonic_regression_remove_alignment(self):
        self.clock.align(self.tick);self.clock.connection(False)
        self.assertEqual(self.clock.view()['status'],'DISCONNECTED')
        self.clock.connection(True)
        self.assertEqual(self.clock.view()['status'],'UNALIGNED')
        self.clock.align(self.tick);self.mono-=1
        self.assertIsNone(self.clock.view()['estimated_broker_time'])
        self.assertIsNone(MT5DisplayClock(lambda:self.mono).view()['estimated_broker_time'])

    def test_ordinary_connected_observations_do_not_realign_clock(self):
        self.clock.align(self.tick);self.mono+=40
        self.clock.connection(True)
        self.assertEqual(self.clock.view()['elapsed_seconds'],40)


class DisplayCommandTests(unittest.TestCase):
    # READ_ONLY transport setup forbids every order_check/order_send.
    setUp=raw_tests.RawBrokerTests.setUp
    def test_alignment_command_changes_only_display_and_notification(self):
        bot=ControllerDouble();bot.broker=self.broker;bot.notices=[]
        bot.notify=bot.notices.append
        original_mode=self.broker.market_time_mode
        original_state=dict(bot.state)
        Controller._sync_mt5_time(bot)
        self.assertEqual(bot.state,original_state)
        self.assertEqual(self.broker.market_time_mode,original_mode)
        self.assertTrue(self.broker.read_only)
        self.assertEqual(self.broker.display_clock.view()['status'],'ALIGNED')
        self.mono+=2
        self.assertEqual(self.broker.display_clock.view()['elapsed_seconds'],2)

    def test_source_identity_change_clears_display_alignment(self):
        self.broker.sync_display_clock()
        original=self.api.account_info
        self.api.account_info=lambda:original()._replace(login=222222222)
        with self.assertRaises(ValueError):self.broker._context()
        self.assertEqual(self.broker.display_clock.view()['status'],'DISCONNECTED')


if __name__=='__main__':unittest.main()
