"""Local protocol, durable fault and Telegram boundary tests (no network)."""
from copy import deepcopy
from decimal import Decimal as D
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from tradfi.engine import Controller
from tradfi.gateway import FileGateway
from tradfi.runtime import owner_lock
from tradfi.settings import Settings
from tradfi.storage import Store, DurabilityError
from tradfi.telegram import Telegram
from tradfi.simulator import Simulator
import test_tradfi as fixture


class FaultTest(unittest.TestCase):
    def setUp(self):
        self.f = fixture.RecoveryTest(); self.f.setUp(); self.addCleanup(self.f.doCleanups)

    def test_crash_after_may_send_before_gateway_never_resends(self):
        f = self.f
        original = f.broker.submit
        def crash(cmd): raise KeyboardInterrupt('power failure')
        f.broker.submit = crash
        with self.assertRaises(KeyboardInterrupt): f.start()
        f.broker.submit = original
        f.bot = Controller(f.store, f.broker); f.tick(3)
        self.assertEqual(len(f.broker.commands), 1)  # second READY intent sent once, first UNKNOWN never resent
        self.assertEqual(sum(c['status'] == 'UNKNOWN' for c in f.bot.state['commands'].values()), 1)

    def test_repeated_stop_between_branches_keeps_finish_cycle_intent(self):
        f = self.f; f.start(); f.fill(); f.bot.command('/stop'); f.broker.close('SL'); f.tick()
        f.bot.command('/stop'); f.bot = Controller(f.store, f.broker); f.tick()
        self.assertEqual(f.bot.state['stop_mode'], 'FINISH')
        self.assertEqual(f.bot.state['phase'], 'PAIR')
        self.assertEqual(len(f.mutations('STOP')), 4)
        f.fill(); f.broker.close('TP'); f.tick()
        self.assertEqual(f.bot.state['phase'], 'PAUSED')

    def test_oco_cancel_does_not_wait_for_profit_calculator(self):
        f = self.f; f.start(); f.broker.fill('BUY')
        f.broker.profit = Mock(side_effect=TimeoutError('calculator unavailable'))
        with self.assertRaises(TimeoutError): f.tick()
        self.assertEqual(len(f.mutations('CANCEL')), 1)
        self.assertFalse(f.broker.data['orders'])

    def test_unknown_anchor_retired_by_full_close_under_original_protection(self):
        f = self.f; f.broker.fail_before.add('MODIFY'); f.start(); f.fill(price='4003.10')
        f.broker.close('TP'); f.bot = Controller(f.store, f.broker); f.tick()
        self.assertEqual(f.bot.state['phase'], 'NEXT_PAIR')  # original TP leaves >1 UST Recovery
        self.assertEqual(D(f.bot.state['cycle']['recovery']), D('1.8'))
        self.assertEqual(len(f.mutations('MODIFY')), 1)
        self.assertEqual(f.bot.state['commands'][f.mutations('MODIFY')[0]['id']]['status'], 'OBSOLETE')

    def test_same_millisecond_close_deals_use_numeric_mt5_ticket_order(self):
        f = self.f; f.recovery(); f.broker.close('SL')
        last = f.broker.data['deals'].pop()
        f.broker.data['deals'].extend([
            dict(last, id='9', volume='.03', price='4001.00', profit='-12'),
            dict(last, id='10', volume='.06', price='4000.90', profit='-25.2')])
        f.tick()
        self.assertEqual(f.bot.state['cycle']['center'], '4000.90')
        self.assertEqual(D(f.bot.state['cycle']['recovery']), D('76.8'))

    def test_unknown_step_closed_on_original_sl_is_not_claimed_as_stepped(self):
        f = self.f; f.recovery(); f.broker.fail_before.add('MODIFY')
        entry = D(f.bot.state['cycle']['position']['entry'])
        f.broker.quote(entry+D('.8'), entry+D('.9')); f.tick()
        f.broker.close('SL'); f.bot = Controller(f.store, f.broker); f.tick()
        self.assertEqual(f.bot.state['cycle']['branch'], 'AFTER_SL')
        self.assertEqual(len([x for x in f.mutations('MODIFY') if x['purpose']=='STEP']), 1)

    def test_explicit_cancel_rejection_retries_with_new_intent_and_live_order(self):
        f = self.f; f.start(); original = f.broker.submit; rejected = []
        def submit(cmd):
            if cmd['kind']=='CANCEL' and not rejected:
                rejected.append(cmd['id']); f.broker.commands.append(deepcopy(cmd))
                f.broker.results[cmd['id']] = {'status': 'REJECTED', 'reason': 'OrderCheck: frozen'}
            else: original(cmd)
        f.broker.submit = submit; f.fill()
        self.assertEqual(len(f.mutations('CANCEL')), 2)
        self.assertNotEqual(f.mutations('CANCEL')[0]['id'], f.mutations('CANCEL')[1]['id'])
        self.assertFalse(f.broker.data['orders'])

    def test_repeated_cancel_rejection_is_bounded_and_notifies_safe_pause(self):
        f = self.f; f.start(); original = f.broker.submit
        def reject(cmd):
            if cmd['kind']=='CANCEL':
                f.broker.commands.append(deepcopy(cmd))
                f.broker.results[cmd['id']] = {'status': 'REJECTED', 'reason': 'OrderCheck: frozen'}
            else: original(cmd)
        f.broker.submit = reject; f.fill(); f.tick(4)
        self.assertEqual(len(f.mutations('CANCEL')), 3)
        self.assertEqual(f.bot.state['phase'], 'SAFE_PAUSE')
        self.assertTrue(f.bot.state['notifications'])
        self.assertEqual(len(f.broker.data['orders']), 1)

    def test_unsent_step_retired_when_position_closes_before_restart(self):
        f = self.f; f.recovery(); entry = D(f.bot.state['cycle']['position']['entry'])
        f.broker.quote(entry+D('.8'), entry+D('.9'))
        real = f.bot.dispatch
        def crash(kinds=None):
            if kinds is None: raise KeyboardInterrupt('after step intent commit')
            return real(kinds)
        with patch.object(f.bot, 'dispatch', side_effect=crash):
            with self.assertRaises(KeyboardInterrupt): f.tick()
        self.assertTrue(any(c['kind']=='MODIFY' and c['status']=='READY' for c in f.bot.state['commands'].values()))
        before = len(f.mutations('MODIFY')); f.broker.close('SL')
        f.bot = Controller(f.store, f.broker); f.tick()
        self.assertEqual(f.bot.state['cycle']['branch'], 'AFTER_SL')
        self.assertEqual(len(f.mutations('MODIFY')), before)

    def test_stop_cancels_saved_but_never_sent_intents_without_broker_io(self):
        f = self.f
        with patch.object(f.bot, 'dispatch', side_effect=KeyboardInterrupt('before dispatch')):
            with self.assertRaises(KeyboardInterrupt): f.start()
        f.bot = Controller(f.store, f.broker); f.bot.command('/stop'); f.tick(3)
        self.assertEqual(f.bot.state['phase'], 'PAUSED_CYCLE')
        self.assertFalse(f.broker.commands)

    def test_missing_close_history_never_starts_next_pair(self):
        f = self.f; f.start(); f.fill(); f.broker.data['positions'].clear(); f.tick(5)
        self.assertEqual(f.bot.state['phase'], 'POSITION')
        self.assertEqual(len(f.mutations('STOP')), 2)

    def test_real_loss_sl_cannot_use_one_dollar_completion_tolerance(self):
        f = self.f; f.recovery(); f.bot.state['cycle']['recovery'] = '.50'
        f.broker.close('SL', profit='-.50'); f.tick()
        self.assertEqual(D(f.bot.state['cycle']['recovery']), D('1'))
        self.assertEqual(f.bot.state['cycle']['branch'], 'AFTER_SL')
        self.assertFalse(f.bot.state['completed'])

    def test_exact_activation_and_step_boundaries(self):
        from tradfi.broker import stepped_stop
        for move, expected in [('.69', None), ('.70', '4000'), ('.79', '4000'),
                               ('.80', '4000.1'), ('.90', '4000.2')]:
            with self.subTest(move=move):
                result = stepped_stop('BUY', D('4000'), {'bid': str(D('4000')+D(move)), 'ask': '9999'}, D('.7'), D('.1'))
                self.assertEqual(result, None if expected is None else D(expected))

    def test_invalid_spec_settings_block_does_not_change_state(self):
        f = self.f; before = deepcopy(f.bot.state)
        with self.assertRaises(ValueError):
            f.bot.command(Settings().template().replace('INITIAL_SIZE=0.09', 'INITIAL_SIZE=0.10'))
        self.assertEqual(f.bot.state, before)

    def test_no_wal_writes_for_identical_broker_snapshot(self):
        f = self.f; f.start(); f.tick()
        before = f.store.db.execute('SELECT COUNT(*) FROM audit').fetchone()[0]
        f.tick(10)
        self.assertEqual(f.store.db.execute('SELECT COUNT(*) FROM audit').fetchone()[0], before)

    def test_cancel_unknown_response_reconciles_history_without_second_delete(self):
        f = self.f; f.start(); f.broker.fail_after.add('CANCEL'); f.bot.command('/stop'); f.tick()
        f.bot = Controller(f.store, f.broker); f.tick(4)
        self.assertEqual(len(f.mutations('CANCEL')), 2)
        self.assertEqual(f.bot.state['phase'], 'PAUSED_CYCLE')

    def test_cancel_unknown_request_empty_snapshot_does_not_mean_cancelled(self):
        f = self.f; f.start(); f.broker.fail_before.add('CANCEL'); f.bot.command('/stop'); f.tick()
        f.broker.data['orders'].clear(); f.bot = Controller(f.store, f.broker); f.tick(4)
        self.assertEqual(f.bot.state['phase'], 'PAIR')
        self.assertEqual(len(f.mutations('STOP')), 2)
        self.assertEqual(len(f.mutations('CANCEL')), 2)

    def test_late_second_fill_after_first_closed_still_oco_safe_pause(self):
        f = self.f; f.start(); f.broker.fill('BUY'); f.broker.close('TP'); f.broker.fill('SELL'); f.tick()
        self.assertEqual(f.bot.state['phase'], 'SAFE_PAUSE')
        self.assertFalse(f.bot.state['completed'])

    def test_explicit_crossed_rejection_allows_one_protected_market(self):
        f = self.f; f.start()
        order = next(x for x in f.broker.data['orders'] if x['side'] == 'BUY')
        f.broker.data['orders'].remove(order); order['status'] = 'REJECTED'
        f.broker.data['order_history'].append(order)
        f.broker.results[order['command_id']] = {'status': 'REJECTED', 'reason': 'CROSSED'}
        f.broker.quote('4004', '4004.10'); f.tick(5)
        self.assertEqual(len(f.mutations('MARKET')), 1)
        self.assertEqual(f.bot.state['phase'], 'POSITION')
        self.assertEqual(f.mutations('MARKET')[0]['trigger'], '4003.00')

    def test_normal_rejection_never_allows_market(self):
        f = self.f; f.start()
        order = f.broker.data['orders'].pop(0); order['status'] = 'REJECTED'
        f.broker.data['order_history'].append(order)
        f.broker.results[order['command_id']] = {'status': 'REJECTED', 'reason': 'NO_MONEY'}
        f.tick(4)
        self.assertEqual(f.bot.state['phase'], 'SAFE_PAUSE'); self.assertFalse(f.mutations('MARKET'))

    def test_favorable_slippage_reanchor_keeps_actual_entry_sl_distance(self):
        f = self.f; f.start(); f.fill(price='4002.90')
        self.assertEqual(D(f.broker.data['positions'][0]['sl']), D('4000.90'))
        self.assertEqual(D(f.broker.data['positions'][0]['tp']), D('4003.10'))

    def test_duplicate_deal_inside_same_snapshot_not_double_counted(self):
        f = self.f; f.start(); f.broker.fill('BUY')
        f.broker.data['deals'].append(deepcopy(f.broker.data['deals'][0])); f.tick(3)
        self.assertEqual(f.bot.state['phase'], 'POSITION')
        self.assertEqual(D(f.bot.state['cycle']['target']), D('3.6'))

    def test_unknown_step_resolves_from_native_deal_sl_tp_after_close(self):
        f = self.f; f.recovery(); f.broker.fail_after.add('MODIFY')
        entry = D(f.bot.state['cycle']['position']['entry'])
        f.broker.quote(entry + D('.8'), entry + D('.9')); f.tick()
        f.broker.close('SL', price=str(entry - D('.1')))
        f.bot = Controller(f.store, f.broker); f.tick()
        self.assertEqual(f.bot.state['cycle']['branch'], 'PARTIAL_TP')
        self.assertEqual(len([c for c in f.mutations('MODIFY') if c['purpose'] == 'STEP']), 1)

    def test_settings_apply_next_logical_cycle_only(self):
        f = self.f; f.start(); f.fill()
        f.bot.command(Settings().template().replace('BRANCH1_RANGE=3.00', 'BRANCH1_RANGE=4.00'))
        f.broker.close('TP'); f.tick(); f.broker.candle(120); f.tick()
        f.broker.candle(180, '4100'); f.broker.quote('4100', '4100.1'); f.tick()
        self.assertEqual({c['trigger'] for c in f.mutations('STOP')[-2:]}, {'4104.00', '4096.00'})

    def test_start_cannot_bypass_cooldown(self):
        f = self.f; f.start(); f.fill(); f.broker.close('TP'); f.tick()
        f.bot.command('/start'); f.broker.candle(119); f.tick()
        self.assertEqual(f.bot.state['phase'], 'COOLDOWN'); self.assertEqual(len(f.mutations('STOP')), 2)

    def test_account_change_or_incomplete_history_blocks_trading(self):
        f = self.f; f.start(); f.broker.data['account'] = 'OTHER_ACCOUNT'; f.tick()
        self.assertEqual(f.bot.state['phase'], 'SAFE_PAUSE')
        self.assertEqual(len(f.broker.commands), 2)

    def test_stale_gateway_sends_no_command(self):
        f = self.f; f.bot.command('/start')
        f.broker.snapshot = Mock(side_effect=TimeoutError('stale'))
        f.tick(3); self.assertFalse(f.broker.commands)

    def test_closure_commit_failure_restores_and_books_once_on_restart(self):
        f = self.f; f.start(); f.fill(); f.broker.close('SL')
        def fail(event):
            if event == 'closure_accounted_and_next_branch': raise OSError('disk fault')
        f.store.fault = fail
        with self.assertRaises(DurabilityError): f.tick()
        f.store.fault = None; f.bot = Controller(f.store, f.broker); f.tick(3)
        self.assertEqual(D(f.bot.state['cycle']['recovery']), D('39.6'))
        self.assertEqual(len(f.mutations('STOP')), 4)


class MailboxTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.gateway = FileGateway(self.root, enable_trading=True)
        self.snap = Simulator().snapshot()
        self.snap.update(protocol=1, connected=True, now=time.time())
        self.snap['quote']['utc_time'] = time.time()
        (self.root/'snapshot.json').write_text(json.dumps(self.snap))

    def test_atomic_command_identity_expiry_account_and_no_overwrite(self):
        cmd = dict(id='a'*24, kind='CANCEL', order_id='123')
        self.gateway.submit(cmd)
        file = self.root/'commands'/('a'*24+'.cmd'); before = file.read_text()
        self.assertIn('account=SIMULATOR_ONLY', before); self.assertIn('expires=', before)
        self.gateway.submit(dict(cmd, order_id='456')); self.assertEqual(file.read_text(), before)
        self.assertFalse(list((self.root/'commands').glob('*.tmp')))

    def test_disabled_mutation_and_stale_snapshot(self):
        self.gateway.enable_trading = False
        with self.assertRaises(OSError): self.gateway.submit(dict(id='a'*24, kind='CANCEL'))
        self.snap['now'] = 0; (self.root/'snapshot.json').write_text(json.dumps(self.snap))
        with self.assertRaises(TimeoutError): self.gateway.snapshot()

    def test_reply_id_mismatch_and_partial_reply_fail_closed(self):
        file = self.root/'acks'/('a'*24+'.json'); file.write_text('{')
        self.assertEqual(self.gateway.result('a'*24)['status'], 'UNKNOWN')
        file.write_text(json.dumps({'id': 'b'*24, 'status': 'DONE'}))
        with self.assertRaises(ValueError): self.gateway.result('a'*24)

    def test_duplicate_owner_lock_is_rejected(self):
        path = self.root/'owner.lock'
        with owner_lock(path):
            with self.assertRaises(OSError):
                with owner_lock(path): pass

    def test_read_only_order_calc_profit_mailbox_contract(self):
        original = self.gateway._write
        def respond(cmd):
            original(cmd)
            (self.root/'acks'/(cmd['id']+'.json')).write_text(json.dumps(
                dict(id=cmd['id'], status='DONE', profit='3.6')))
        self.gateway._write = respond; self.gateway.enable_trading = False
        self.assertEqual(self.gateway.profit('BUY', D('.09'), D('4000'), D('4000.2')), D('3.6'))

    def test_account_switch_between_planning_and_publish_blocks_command(self):
        self.gateway.snapshot()
        self.snap['account'] = 'DIFFERENT'; (self.root/'snapshot.json').write_text(json.dumps(self.snap))
        with self.assertRaises(ValueError):
            self.gateway.submit(dict(id='a'*24, kind='CANCEL', order_id='123'))
        self.assertFalse(list((self.root/'commands').glob('*.cmd')))

    def test_non_ascii_account_server_uses_ea_utf8_protocol(self):
        self.snap['account'] = '123@Брокер'
        (self.root/'snapshot.json').write_text(json.dumps(self.snap))
        self.gateway.submit(dict(id='a'*24, kind='CANCEL', order_id='123'))
        self.assertIn('account=123@Брокер', (self.root/'commands'/('a'*24+'.cmd')).read_text(encoding='utf-8'))

    def test_offline_diagnostic_export_redacts_account(self):
        from tradfi.diagnostics import export
        store = Store(self.root/'state.sqlite3'); self.addCleanup(store.close)
        controller = Controller(store, Simulator()); controller.command('/start')
        export(store.path, self.root/'report.json')
        report = json.loads((self.root/'report.json').read_text())
        self.assertEqual(report['state']['account'], '<redacted>')
        self.assertTrue(report['audit'])


class TelegramTest(unittest.TestCase):
    def test_full_settings_copyable_block_without_truncation(self):
        template = Settings().template()
        payload = Telegram.payload('123', template)
        self.assertGreater(len(template), 256)
        self.assertEqual(payload['text'], '<pre>' + template + '</pre>')
        self.assertEqual(payload['parse_mode'], 'HTML')
        self.assertEqual({x['command'] for x in Telegram.COMMANDS}, {'start', 'stop', 'settings', 'status', 'sendlog', 'reset_manual'})

    def test_allowed_chat_durable_offset_and_invalid_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp)/'s.sqlite3'); self.addCleanup(store.close)
            bot = Controller(store, Simulator()); tg = Telegram('fake', '123', Mock())
            def event(i, chat, text): return dict(update_id=i, message={'chat': {'id': chat}, 'from': {'id': chat}, 'text': text})
            tg.incoming.put(event(1, 999, '/start')); tg.incoming.put(event(2, 123, '/start'))
            tg.incoming.put(event(2, 123, '/stop')); tg.incoming.put(event(3, 123, '/SETTINGS:\nINITIAL_SIZE=NaN'))
            tg.process(bot)
            self.assertEqual(bot.state['phase'], 'WAIT_CANDLE'); self.assertFalse(bot.state['stop'])
            self.assertEqual(Controller(store, Simulator()).state['telegram_offset'], 4)
            self.assertEqual(bot.state['settings'], Settings().dump())
            tg.http.assert_not_called()
