import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock
from tradfi import telegram_settings as settings
from tradfi.telegram_logs import from_settings, TelegramLogs

TOKEN='123456:'+'x'*30
VALUE={'enabled':True,'bot_token':TOKEN,'chat_id':'123'}

class TelegramSettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.log=Path(self.tmp.name)/'runtime.log'
    def tearDown(self):self.tmp.cleanup()
    def test_roundtrip_uses_encrypt_and_does_not_touch_service_json(self):
        service=self.log.parent/'service.json';service.write_text('{"unchanged":true}')
        ciphertext=b'encrypted-test-fixture'
        seen=[]
        settings.save(self.log,VALUE,encrypt=lambda raw:seen.append(json.loads(raw)) or ciphertext)
        self.assertEqual(settings.path_for(self.log).read_bytes(),ciphertext)
        self.assertNotIn(TOKEN.encode(),settings.path_for(self.log).read_bytes())
        self.assertEqual(seen,[VALUE]);self.assertEqual(service.read_text(),'{"unchanged":true}')
        self.assertEqual(settings.load(self.log,decrypt=lambda data:json.dumps(VALUE).encode()),VALUE)
    def test_bad_values_and_crypto_errors_do_not_leak(self):
        for value in ({},dict(VALUE,chat_id='-42'),dict(VALUE,bot_token='secret-that-must-not-leak'),dict(VALUE,enabled='true')):
            with self.assertRaises(settings.TelegramSettingsError) as caught:settings.save(self.log,value)
            self.assertNotIn('secret-that-must-not-leak',str(caught.exception))
        def broken(raw):raise RuntimeError(TOKEN)
        with self.assertRaises(settings.TelegramSettingsError) as caught:settings.save(self.log,VALUE,encrypt=broken)
        self.assertNotIn(TOKEN,str(caught.exception));self.assertFalse(settings.path_for(self.log).exists())
    def test_absence_preserves_environment_and_explicit_disabled_wins(self):
        with patch('tradfi.telegram_logs.from_environment',return_value='old-env') as old:
            self.assertEqual(from_settings(self.log),'old-env');old.assert_called_once()
        with patch('tradfi.telegram_settings.load',return_value=dict(VALUE,enabled=False)),patch('tradfi.telegram_logs.from_environment') as old:
            self.assertIsNone(from_settings(self.log));old.assert_not_called()
    def test_local_credentials_not_in_status(self):
        with patch('tradfi.telegram_settings.load',return_value=VALUE):
            q=from_settings(self.log)
        self.assertNotIn(TOKEN,json.dumps(q.status()));q.close()
    def test_corrupt_file_does_not_fallback_to_environment(self):
        settings.path_for(self.log).write_bytes(b'bad')
        with patch('tradfi.telegram_logs.from_environment') as old:
            with self.assertRaises(settings.TelegramSettingsError):from_settings(self.log)
            old.assert_not_called()
    def test_existing_queue_destination_cannot_be_redirected(self):
        q=TelegramLogs(str(self.log)+'.telegram-outbox.sqlite3',TOKEN,'123');q.close()
        with self.assertRaises(settings.TelegramSettingsError):settings.save(self.log,dict(VALUE,chat_id='456'),encrypt=lambda raw:b'x')
        settings.save(self.log,VALUE,encrypt=lambda raw:b'x')
    def test_queue_connection_is_closed_on_success_and_rejection(self):
        queue=Path(str(self.log)+'.telegram-outbox.sqlite3');queue.touch()
        for destination in ('123','456'):
            db=MagicMock();db.execute.return_value.fetchone.return_value=(destination,)
            with patch('tradfi.telegram_settings.sqlite3.connect',return_value=db):
                if destination=='123':settings.save(self.log,VALUE,encrypt=lambda raw:b'x')
                else:
                    with self.assertRaises(settings.TelegramSettingsError):settings.save(self.log,VALUE,encrypt=lambda raw:b'x')
            db.close.assert_called_once()
    def test_symlink_rejected(self):
        other=self.log.parent/'other';other.write_bytes(b'target')
        settings.path_for(self.log).symlink_to(other)
        with self.assertRaises(settings.TelegramSettingsError):settings.load(self.log)
        with self.assertRaises(settings.TelegramSettingsError):settings.save(self.log,VALUE,encrypt=lambda raw:b'x')
        self.assertEqual(other.read_bytes(),b'target')
    def test_crypto_requires_windows(self):
        with patch('tradfi.telegram_settings.os.name','posix'):
            with self.assertRaises(settings.TelegramSettingsError):settings._crypt(b'x')

if __name__=='__main__':unittest.main()
