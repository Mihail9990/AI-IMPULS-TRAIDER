import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import uuid

from tradfi.native_transport import NativeOutbox, encoded

class NativeTransportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.identity = dict(installation=str(uuid.uuid4()), login='123', server='Mock', symbol='XAUUSD.s')
        self.box = NativeOutbox(self.tmp.name, self.identity)
        self.addCleanup(lambda: self.box.close())
        self.id = str(uuid.uuid4())
        self.action = dict(type='sl_at_entry', cycle_id='cycle', positions=['100'])

    def submit(self):
        return self.box.submit(self.id, self.action, 7)

    def receipt(self, status='EA_ACCEPTED', sequence=1):
        payload = json.loads((Path(self.tmp.name)/'native_requests'/f'{self.id}.json').read_text())
        return dict(protocol='native_panel_receipt_v1', id=self.id, identity=self.identity,
                    digest=payload['digest'], sequence=sequence, status=status, reason='mock')

    def restart(self):
        self.box.close()
        self.box = NativeOutbox(self.tmp.name, self.identity)

    def test_acceptance_is_not_broker_confirmation(self):
        self.assertEqual(self.submit()['status'], 'AWAITING_EA')
        self.assertEqual(self.box.accept_receipt(self.receipt())['status'], 'EA_ACCEPTED')
        self.assertEqual(self.box.accept_receipt(self.receipt('AWAITING_BROKER', 2))['status'], 'AWAITING_BROKER')
        self.assertEqual(self.box.accept_receipt(self.receipt('BROKER_CONFIRMED', 3))['status'], 'BROKER_CONFIRMED')

    def test_removed_file_not_republished_even_after_restart(self):
        self.submit()
        dest=Path(self.tmp.name)/'native_requests'/f'{self.id}.json'
        dest.unlink()
        self.restart()
        self.submit()
        self.assertFalse(dest.exists())

    def test_disconnect_before_and_after_publication_never_retries(self):
        for point in ('before_publication', 'after_publication'):
            self.id=str(uuid.uuid4())
            def fail(at):
                if at==point:raise OSError('mock disconnect')
            self.box.fault=fail
            self.assertEqual(self.submit()['status'],'UNKNOWN')
            dest=Path(self.tmp.name)/'native_requests'/f'{self.id}.json'
            self.assertEqual(dest.exists(),point=='after_publication')
            if dest.exists():dest.unlink()
            self.restart()
            self.assertEqual(self.submit()['status'],'UNKNOWN')
            self.assertFalse(dest.exists())

    def test_other_resource_accepted_during_unknown(self):
        self.box.fault=lambda at: (_ for _ in ()).throw(OSError('disconnected'))
        self.assertEqual(self.submit()['status'],'UNKNOWN')
        self.box.fault=None
        r=self.box.submit(str(uuid.uuid4()),dict(type='cancel_triggers', cycle_id='cycle'),7)
        self.assertEqual(r['status'],'AWAITING_EA')

    def test_duplicate_and_reordered_receipts(self):
        self.submit()
        r=self.receipt('AWAITING_BROKER',2)
        self.box.accept_receipt(r)
        self.box.accept_receipt(self.receipt('EA_ACCEPTED',1))
        self.box.accept_receipt(r)
        self.assertEqual(self.box.status(self.id)['status'],'AWAITING_BROKER')
        with self.assertRaises(ValueError):self.box.accept_receipt(self.receipt('REJECTED',2))

    def test_unknown_can_be_reconciled_without_resend(self):
        self.box.fault=lambda at: (_ for _ in ()).throw(OSError('lost reply')) if at=='after_publication' else None
        self.submit()
        self.assertEqual(self.box.accept_receipt(self.receipt('BROKER_CONFIRMED',1))['status'],'BROKER_CONFIRMED')

    def test_final_result_cannot_regress(self):
        self.submit()
        self.box.accept_receipt(self.receipt('BROKER_CONFIRMED',2))
        with self.assertRaises(ValueError):self.box.accept_receipt(self.receipt('UNKNOWN',3))
        with self.assertRaises(ValueError):self.box.accept_receipt(self.receipt('REJECTED',4))

    def test_rejected_not_retried(self):
        self.submit()
        self.box.accept_receipt(self.receipt('REJECTED'))
        self.assertEqual(self.submit()['status'],'REJECTED')

    def test_identity_digest_validation(self):
        self.submit()
        for field,value in [('identity',dict(self.identity,login='456')),('digest','bad'),('id',str(uuid.uuid4()))]:
            r=self.receipt();r[field]=value
            with self.assertRaises(ValueError):self.box.accept_receipt(r)
        with self.assertRaises(ValueError):NativeOutbox(self.tmp.name,dict(self.identity,login='456'))

    def test_same_request_id_different_action_rejected(self):
        self.submit()
        with self.assertRaises(ValueError):self.box.submit(self.id,dict(type='close_all'),7)

    def test_no_manual_order_api_or_nan_or_traversal(self):
        for kind in ('MARKET','STOP','order_send','v3_entry'):
            with self.assertRaises(ValueError):self.box.submit(str(uuid.uuid4()),dict(type=kind),0)
        with self.assertRaises(ValueError):self.box.submit('../file',self.action,0)
        with self.assertRaises(ValueError):self.box.submit(self.id,dict(type='modify_level',price=float('nan')),0)
        with self.assertRaises(ValueError):self.box.submit(self.id,self.action,True)

    def test_conflicting_existing_file_is_preserved_unknown(self):
        dest=Path(self.tmp.name)/'native_requests'/f'{self.id}.json';dest.write_text('preserve')
        self.assertEqual(self.submit()['status'],'UNKNOWN')
        self.assertEqual(dest.read_text(),'preserve')

    def test_request_hash_exact_and_settings_unmodified(self):
        action=dict(type='save_settings',sl_distance='1.70',initial_tp='0.23',profiles=[])
        before=encoded(action)
        self.box.submit(self.id,action,0)
        record=json.loads((Path(self.tmp.name)/'native_requests'/f'{self.id}.json').read_text())
        self.assertEqual(record['digest'],hashlib.sha256(encoded(record['payload']).encode()).hexdigest())
        self.assertEqual(encoded(action),before)

    def test_pending_receipt_survives_restart(self):
        self.submit();self.box.accept_receipt(self.receipt('AWAITING_BROKER'))
        self.restart()
        self.assertEqual(self.submit()['status'],'AWAITING_BROKER')
