"""No real Telegram traffic: export privacy, bounded queues and worker isolation."""
import json
import logging
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from tradfi.engine import Controller
from tradfi.log_export import (build_document, diagnostic_handler, sanitize_line,
                               DOCUMENT_BYTES, INPUT_TAIL_BYTES)
from tradfi.simulator import Simulator
from tradfi.storage import Store, DurabilityError
from tradfi.telegram import Telegram


class Reply:
    def __init__(self, result=None, ok=True): self.result=result or []; self.ok=ok
    def raise_for_status(self): pass
    def json(self): return {'ok':self.ok,'result':self.result}


class HTTP:
    def __init__(self):
        self.calls=[]; self.gets=0; self.upload_started=threading.Event(); self.release=threading.Event()
        self.block_upload=False; self.failure=None; self.closed=False
    def post(self,url,**kwargs):
        self.calls.append((url,kwargs))
        if url.endswith('/sendDocument'):
            self.upload_started.set()
            if self.block_upload: self.release.wait(3)
            if self.failure: raise self.failure
        return Reply()
    def get(self,*args,**kw):
        self.gets+=1; threading.Event().wait(.01); return Reply()
    def close(self): self.closed=True


class SendlogTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'tradfi.log'; self.path.write_text(self.line())
        self.store=Store(Path(self.tmp.name)/'state.db'); self.addCleanup(self.store.close)
        self.bot=Controller(self.store,Simulator())
        self.http=HTTP(); self.doc=HTTP()
        self.tg=Telegram('123456:SECRET_TOKEN','123',self.http,log_path=self.path,document_session=self.doc)
        self.addCleanup(self.tg.close)
    def line(self, **fields):
        return json.dumps(dict(version=1,utc='2026-10-12T12:00:00Z',level='INFO',
                               event='cycle_manual_required',phase='MANUAL',**fields))+'\n'
    def enqueue(self, uid=1, text='/sendlog', chat=123, sender=123, **fields):
        self.tg.incoming.put(dict(update_id=uid,message=dict(chat={'id':chat},
            **{'from':dict(id=sender,is_bot=False)},text=text,**fields)))
    def run_document(self):
        worker=threading.Thread(target=self.tg._document_worker,daemon=True); worker.start()
        self.tg.threads.append(worker)
        self.assertTrue(self.doc.upload_started.wait(2))
        self.doc.release.set()
        self.tg.documents.join(); self.tg.process(self.bot)
    def documents(self): return [kw for url,kw in self.doc.calls if url.endswith('/sendDocument')]

    def test_authorized_file_same_chat_and_durable_duplicate_suppression(self):
        self.enqueue(); self.enqueue(); self.tg.process(self.bot)
        self.assertEqual(self.tg.documents.qsize(),1); self.assertEqual(self.bot.state['telegram_offset'],2)
        self.run_document()
        request=self.documents()[0]
        self.assertEqual(request['data'],{'chat_id':'123'})
        name,payload,mime=request['files']['document']
        self.assertEqual(name,'tradfi-diagnostics.log'); self.assertLessEqual(len(payload),DOCUMENT_BYTES)
        self.assertIn(b'cycle_manual_required',payload); self.assertNotIn(b'SECRET_TOKEN',payload)
        self.enqueue(); self.tg.process(self.bot); self.assertEqual(len(self.documents()),1)
        self.assertEqual(self.bot.state['phase'],'IDLE')

    def test_wrong_chat_sender_anonymous_and_bot_cannot_request_file_or_commands(self):
        self.enqueue(1,chat=999); self.enqueue(2,sender=999)
        self.enqueue(3,sender_chat={'id':123}); self.enqueue(4,text='/start',sender=999)
        self.tg.incoming.put(dict(update_id=5,message={'chat':{'id':123},'from':{'id':123,'is_bot':True},'text':'/sendlog'}))
        self.tg.process(self.bot)
        self.assertTrue(self.tg.documents.empty()); self.assertFalse(self.bot.state['notifications'])
        self.assertEqual(self.bot.state['phase'],'IDLE'); self.assertEqual(self.bot.state['telegram_offset'],6)

    def test_group_requires_explicit_authorized_sender(self):
        with self.assertRaises(ValueError): Telegram('fake','-100123',self.http)
        group=Telegram('fake','-100123',self.http,user_id=123)
        self.assertTrue(group._authorized({'chat':{'id':-100123},'from':{'id':123}}))
        self.assertFalse(group._authorized({'chat':{'id':-100123},'from':{'id':999}}))

    def test_prepare_and_upload_do_not_run_on_controller_thread(self):
        self.enqueue()
        with patch('tradfi.log_export.build_document',side_effect=AssertionError('must not build in owner')):
            self.tg.process(self.bot)
        self.assertFalse(self.doc.calls)
        self.doc.block_upload=True; self.tg.start(self.bot.state['telegram_offset'])
        self.assertTrue(self.doc.upload_started.wait(2)); before=self.http.gets
        self.bot.command('/status'); self.bot.tick()
        deadline=time.monotonic()+1
        while self.http.gets<=before and time.monotonic()<deadline: threading.Event().wait(.01)
        self.assertGreater(self.http.gets,before)
        self.assertFalse(self.doc.release.is_set()); self.doc.release.set()

    def test_spam_coalesces_to_one_file_job_and_bounded_feedback(self):
        for uid in range(100): self.enqueue(uid)
        self.tg.process(self.bot)
        self.assertEqual(self.tg.documents.qsize(),1)
        self.assertLessEqual(len(self.bot.state['notifications']),2)
        self.assertEqual(self.tg.documents.maxsize,1); self.assertEqual(self.tg.document_results.maxsize,1)

    def test_failure_is_safe_no_automatic_upload_retry(self):
        self.doc.failure=OSError('password=secret '+self.tg.base)
        self.enqueue(); self.tg.process(self.bot); self.run_document()
        for _ in range(3): self.tg.process(self.bot)
        self.assertEqual(len(self.documents()),1)
        texts=' '.join(x['text'] for x in self.bot.state['notifications'])
        self.assertNotIn('secret',texts); self.assertNotIn('SECRET_TOKEN',texts)
        self.assertIn('не подтверждена',texts)

    def test_offset_commit_failure_never_enqueues_upload(self):
        self.enqueue()
        self.store.fault=lambda event: (_ for _ in ()).throw(RuntimeError('disk full'))
        with self.assertRaises(DurabilityError): self.tg.process(self.bot)
        self.assertTrue(self.tg.documents.empty()); self.assertFalse(self.doc.calls)

    def test_restart_does_not_replay_accepted_document_request(self):
        self.enqueue(); self.tg.process(self.bot)
        self.bot=Controller(self.store,Simulator())
        new=Telegram('fake','123',self.http,log_path=self.path,document_session=self.doc)
        new.incoming.put(dict(update_id=1,message={'chat':{'id':123},'from':{'id':123},'text':'/sendlog'}))
        new.process(self.bot); self.assertTrue(new.documents.empty())

    def test_path_argument_is_not_read_or_accepted(self):
        self.enqueue(text='/sendlog /etc/passwd'); self.tg.process(self.bot)
        self.assertTrue(self.tg.documents.empty())

    def test_projection_removes_raw_secrets_private_ids_and_extra_json_fields(self):
        secret='123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'
        self.path.write_text(self.line(token=secret,password='secretpassword',account=999999999,raw_facts={'private':'dump'})+
            '2026-10-12 12:00:00,000 WARNING Unknown gateway outcome abc: token='+secret+'\n'+
            '2026-10-12 12:00:00,000 INFO may_send:'+('a'*24)+' phase=PAIR\n'+
            'RAW password=secretpassword https://host/private\n')
        payload=build_document(self.path).decode()
        for value in (secret,'secretpassword','999999999','raw_facts','private','a'*24,'https://host'):
            self.assertNotIn(value,payload)
        self.assertIn('unknown_gateway_outcome',payload); self.assertIn('may_send',payload)

    def test_output_and_input_segment_count_are_bounded(self):
        line=self.line()
        for suffix in ('','.1','.2','.3','.4'):
            Path(str(self.path)+suffix).write_text(line*(INPUT_TAIL_BYTES//len(line)+2000))
        result=build_document(self.path)
        self.assertLessEqual(len(result),DOCUMENT_BYTES)
        self.assertIn(b'files_read=4;',result); self.assertIn(b'truncated_tails=4;',result)

    def test_empty_missing_oversized_and_malformed_logs_are_safe(self):
        for path in (self.path,Path(str(self.path)+'.1')):
            path.write_text('X'*5000+'\n'+json.dumps({'version':1,'utc':'2026-10-12T12:00:00Z','event':{},'level':[]})+'\n')
        result=build_document(self.path)
        self.assertIn(b'exported_records=0',result)
        self.path.unlink(); Path(str(self.path)+'.1').unlink()
        self.assertIn(b'files_read=0',build_document(self.path))
        self.assertIsNone(sanitize_line('not a log'))

    def test_handler_never_formats_raw_exception_and_rotates(self):
        handler=diagnostic_handler(self.path); self.addCleanup(handler.close); handler.maxBytes=200
        log=logging.getLogger('tradfi.engine'); old=log.level; log.setLevel(logging.INFO); log.addHandler(handler)
        self.addCleanup(log.removeHandler,handler); self.addCleanup(log.setLevel,old)
        for _ in range(6): log.warning('Unknown gateway outcome %s: %s','private-id',OSError('secretpassword'))
        handler.flush()
        self.assertTrue(Path(str(self.path)+'.1').exists())
        for suffix in ('','.1','.2','.3'):
            data=Path(str(self.path)+suffix).read_text()
            self.assertNotIn('secretpassword',data); self.assertNotIn('private-id',data)
        self.assertIn(b'unknown_gateway_outcome',build_document(self.path))

    def test_missing_sender_is_denied_even_for_authorized_chat(self):
        self.tg.incoming.put(dict(update_id=1,message={'chat':{'id':123},'text':'/sendlog'}))
        self.tg.process(self.bot); self.assertTrue(self.tg.documents.empty())


    def test_reset_manual_uses_the_same_sender_guard_and_command_path(self):
        self.enqueue(1,text='/reset_manual',sender=999)
        with patch.object(self.bot,'command') as command:
            self.tg.process(self.bot); command.assert_not_called()
            self.enqueue(2,text='/reset_manual'); self.tg.process(self.bot)
            command.assert_called_once_with('/reset_manual',2)
        self.assertTrue(self.tg.documents.empty())

    def test_diagnostics_feedback_does_not_grow_unbounded_when_delivery_is_down(self):
        self.bot.notify('Important manual trading alert')
        for index in range(100): self.tg._document_notice(self.bot,f'diagnostics {index}')
        self.assertEqual(len(self.bot.state['notifications']),2)
        self.assertEqual(self.bot.state['notifications'][0]['text'],'Important manual trading alert')
        self.assertEqual(self.bot.state['notifications'][-1]['text'],'diagnostics 99')

if __name__=='__main__': unittest.main()
