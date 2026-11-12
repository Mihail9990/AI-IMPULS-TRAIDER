import json
import tempfile
import unittest
from pathlib import Path
from tradfi.telegram_logs import TelegramLogs, DeliveryUnknown

TOKEN='123456:'+'x'*30
RECORD=json.dumps({'scope':'live','observed_utc':'2026-11-12T16:00:00+00:00','context':{'phase':'SAFE_PAUSE','password':'SECRET','account_id':'SECRET','raw_message':'SECRET'}})

class TelegramLogsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.path=Path(self.tmp.name)/'queue.db'; self.now=1000
    def tearDown(self):self.tmp.cleanup()
    def queue(self,sender):return TelegramLogs(self.path,TOKEN,'123',sender=sender,clock=lambda:self.now)
    def test_delivered_deleted_only_after_ten_minutes(self):
        calls=[]
        q=self.queue(lambda *a:calls.append(a) or 1);q.append(RECORD)
        q.tick();self.assertFalse(calls)
        self.now+=600;q.tick()
        self.assertEqual(q.status()['queued_records'],0)
        self.assertNotIn(b'SECRET',calls[0][2]);self.assertIn(b'SAFE_PAUSE',calls[0][2]);q.close()
    def test_rejection_retained_and_retried(self):
        q=self.queue(lambda *a:None);q.append(RECORD);self.now+=600;q.tick()
        self.assertEqual(q.status()['status'],'RETRY');q.close()
        q=self.queue(lambda *a:4);self.now+=600;q.tick()
        self.assertEqual(q.status()['queued_records'],0);q.close()
    def test_unknown_survives_restart_no_repeat(self):
        def fail(*a):raise DeliveryUnknown()
        q=self.queue(fail);q.append(RECORD);self.now+=600;q.tick();q.close()
        calls=[];q=self.queue(lambda *a:calls.append(a));self.now+=600;q.tick()
        self.assertEqual(q.status()['status'],'UNKNOWN');self.assertEqual(q.status()['queued_records'],1);self.assertFalse(calls);q.close()
    def test_records_added_during_send_survive(self):
        def send(*a):q.append(RECORD);return 5
        q=self.queue(send);q.append(RECORD);self.now+=600;q.tick()
        self.assertEqual(q.status()['queued_records'],1);q.close()
    def test_crash_sending_becomes_unknown(self):
        q=self.queue(lambda *a:1);q.append(RECORD)
        q.db.execute("UPDATE delivery SET status='SENDING'");q.db.commit();q.close()
        q=self.queue(lambda *a:self.fail('Must not send'));self.now+=1000;q.tick()
        self.assertEqual(q.status()['status'],'UNKNOWN');q.close()
    def test_destination_cannot_change_for_existing_outbox(self):
        q=self.queue(lambda *a:1);q.close()
        with self.assertRaises(ValueError):TelegramLogs(self.path,TOKEN,'456')
    def test_two_owners_rejected(self):
        q=self.queue(lambda *a:1)
        with self.assertRaises(ValueError):self.queue(lambda *a:1)
        q.close()
    def test_unknown_resolution_requires_exact_batch(self):
        def fail(*a):raise DeliveryUnknown()
        q=self.queue(fail);q.append(RECORD);self.now+=600;q.tick()
        with self.assertRaises(ValueError):q.resolve_unknown('wrong',received=True)
        self.assertEqual(q.status()['queued_records'],1)
        q.resolve_unknown(q.status()['batch'],received=True)
        self.assertEqual(q.status()['queued_records'],0);q.close()
    def test_only_live_and_private_chat(self):
        q=self.queue(lambda *a:1);q.append('{"scope":"paper"}')
        self.assertEqual(q.status()['queued_records'],0);q.close()
        with self.assertRaises(ValueError):TelegramLogs(self.path,TOKEN,'-123')

if __name__=='__main__':unittest.main()
