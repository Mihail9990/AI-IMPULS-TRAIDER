"""Offline bounded diagnostic export privacy; retired messaging transport removed."""
import json
import logging
from pathlib import Path
import tempfile
import unittest
from tradfi.log_export import build_document, diagnostic_handler, sanitize_line, DOCUMENT_BYTES, INPUT_TAIL_BYTES

class LogExportTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'tradfi.log';self.path.write_text(self.line())
    def line(self, **fields):
        return json.dumps(dict(version=1,utc='2026-10-12T12:00:00Z',level='INFO',event='cycle_manual_required',phase='MANUAL',**fields))+'\n'
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
