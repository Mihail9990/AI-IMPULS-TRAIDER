import json
from pathlib import Path
import tempfile
import unittest
import uuid
from channel import Channel, ProtocolError, atomic_new, read_json, validate_payload

def uid(): return str(uuid.uuid4())

class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.scope,self.boot,self.context,self.generation=uid(),uid(),uid(),uid()
        self.state=dict(version=1,scope_id=self.scope,boot_id=self.boot,context_id=self.context,generation_id=self.generation,written_at=100,mode='ready')
        self.session=self.root/'sessions'/self.boot
        for folder in ('commands','receipts'): (self.session/folder).mkdir(parents=True)
        self.save_state(); self.channel=Channel(self.root,self.scope,clock=lambda:100)
    def save_state(self): (self.root/'state.json').write_text(json.dumps(self.state))
    def publish(self, **kwargs):
        args=dict(command_id=uid(),expected_boot=self.boot,expected_context=self.context,expected_generation=self.generation,confirmed=True)
        args.update(kwargs)
        return self.channel.publish('manual_wait',{},**args)
    def test_publish_read_no_receipt_is_unknown(self):
        cmd=self.publish()
        self.assertEqual(read_json(self.session/'commands'/f"{cmd['command_id']}.json"),cmd)
        self.assertEqual(self.channel.receipt(cmd)['status'],'unknown')
        self.assertFalse(self.channel.receipt(cmd)['retry_allowed'])
    def test_identity_never_overwritten(self):
        cmd=self.publish()
        with self.assertRaises(FileExistsError): self.publish(command_id=cmd['command_id'])
    def test_requires_confirmation(self):
        with self.assertRaises(ProtocolError): self.publish(confirmed=False)
    def test_boot_and_context_guard(self):
        for field in ('expected_boot','expected_context','expected_generation'):
            with self.assertRaises(ProtocolError): self.publish(**{field:uid()})
    def test_unknown_state_blocks_without_wall_clock_gate(self):
        for time in (-10000, 99999):
            self.state['written_at']=time; self.save_state(); self.publish()
        for change in ({'mode':'unknown'},):
            original=dict(self.state);self.state.update(change);self.save_state()
            with self.assertRaises(ProtocolError): self.publish()
            self.state=original
    def test_receipt_binds_all_fields(self):
        cmd=self.publish();receipt={k:cmd[k] for k in ('version','scope_id','boot_id','context_id','generation_id','command_id','content_sha256')}
        receipt['status']='accepted'
        path=self.session/'receipts'/f"{cmd['command_id']}.json"
        path.write_text(json.dumps(receipt))
        self.assertEqual(self.channel.receipt(cmd)['status'],'accepted')
        receipt['boot_id']=uid();path.write_text(json.dumps(receipt))
        with self.assertRaises(ProtocolError): self.channel.receipt(cmd)
    def test_pending_wait_and_cancel_have_empty_payload_only(self):
        for operation in ('manual_pending_wait', 'manual_cancel'):
            args = dict(command_id=uid(), expected_boot=self.boot, expected_context=self.context,
                        expected_generation=self.generation, confirmed=True)
            with self.assertRaises(ProtocolError):
                self.channel.publish(operation, {'ticket': 17}, **args)
            cmd = self.channel.publish(operation, {}, **args)
            self.assertEqual(cmd['operation'], operation)
            self.assertEqual(cmd['payload'], {})
        with self.assertRaises(ProtocolError):
            validate_payload('configure_ranges', {})

    def test_waiting_and_active_state_projection(self):
        for mode in ('manual_wait', 'pending_wait', 'active'):
            self.state['mode'] = mode
            self.save_state()
            self.assertEqual(self.channel.state()['mode'], mode)
        self.state['mode'] = 'manual_cancel'
        self.save_state()
        with self.assertRaises(ProtocolError):
            self.channel.state()

    def test_malformed_duplicate_oversized(self):
        path=self.root/'bad.json'
        for raw in ('{"a":1,"a":2}','[]','{"a":NaN}',' ' * 65537):
            path.write_text(raw)
            with self.assertRaises(ProtocolError):read_json(path)
    def test_no_temp_visible_after_publish(self):
        self.publish();self.assertEqual(len(list((self.session/'commands').iterdir())),1)

if __name__=='__main__':unittest.main()
