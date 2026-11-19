import importlib.util
from pathlib import Path
from copy import deepcopy
import unittest
spec=importlib.util.spec_from_file_location('aclpolicy',Path(__file__).parents[1]/'native_evidence_preflight.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
ME='S-1-5-21-100';USERS='S-1-5-32-545';LEAF='C:\\Safe\\RECOVERY'
def ace(rights,io=False,flags=0,sid=USERS):return dict(type='Allow',sid=sid,rights=rights,inherit_only=io,inheritance=flags)
def node(path,owner=ME,rules=None,kind='directory'):return dict(path=path,owner=owner,rules=rules or [],kind=kind,reparse=False,null_dacl=False)
class PolicyTest(unittest.TestCase):
 def setUp(self):self.rows=[node('C:\\',m.TRUSTED_INSTALLER_SID,[ace(4,False,1),ace(2,True,3),ace(0x1F01FF,True,3,'S-1-3-0')]),node('C:\\Safe'),node(LEAF)]
 def check(self):m._verify_acl_records(self.rows,mailbox=LEAF,system_drive='C:',current_sid=ME)
 def test_default_root(self):self.check()
 def test_root_dangerous_rights(self):
  for bit in (0x40,0x10000,0x40000,0x80000,0x10,0x100):
   with self.subTest(bit=bit):
    rows=deepcopy(self.rows);self.rows[0]['rules'].append(ace(bit))
    with self.assertRaises(m.CheckError):self.check()
    self.rows=rows
 def test_trustedinstaller_only_exact_root(self):
  self.rows[1]['owner']=m.TRUSTED_INSTALLER_SID
  with self.assertRaises(m.CheckError):self.check()
 def test_wrong_systemdrive(self):
  with self.assertRaises(m.CheckError):m._verify_acl_records(self.rows,mailbox=LEAF,system_drive='D:',current_sid=ME)
 def test_actual_child_inherited_write_not_ignored(self):
  self.rows[1]['rules']=[ace(4,False,1)]
  with self.assertRaises(m.CheckError):self.check()
 def test_ancestor_inheritonly_allowed(self):
  self.rows[1]['rules']=[ace(0x1F01FF,True,3)];self.check()
 def test_leaf_future_untrusted_writer_denied(self):
  self.rows[2]['rules']=[ace(2,True,2)]
  with self.assertRaises(m.CheckError):self.check()
 def test_leaf_creator_owner_template(self):
  self.rows[2]['rules']=[ace(0x1F01FF,True,3,'S-1-3-0')];self.check()
 def test_effective_creator_owner_not_exempt(self):
  self.rows[2]['rules']=[ace(2,False,3,'S-1-3-0')]
  with self.assertRaises(m.CheckError):self.check()
 def test_existing_file_acl_checked(self):
  self.rows.append(node(LEAF+'\\nonce.response',rules=[ace(2)],kind='file'))
  with self.assertRaises(m.CheckError):self.check()
 def test_null_reparse_unknown_owner(self):
  for key,value in [('null_dacl',True),('reparse',True),('owner',USERS)]:
   old=self.rows[1][key];self.rows[1][key]=value
   with self.assertRaises(m.CheckError):self.check()
   self.rows[1][key]=old
 def test_missing_duplicate_unexpected_directory(self):
  saved=deepcopy(self.rows)
  for rows in (saved[1:],saved+[saved[0]],saved+[node(LEAF+'\\subdir')]):
   self.rows=rows
   with self.assertRaises(m.CheckError):self.check()
 def test_generic_mapping_keeps_specific_bits(self):
  self.assertEqual(m._map_file_generic(0xE0040000), 0x1601BF)
  self.assertEqual(m._map_file_generic(0x10000000), 0x1F01FF)
 def test_generic_reads_and_templates_are_allowed_when_safe(self):
  self.rows[1]['rules']=[ace(0x80000000),ace(0x10000000,True,3)]
  self.rows[2]['rules']=[ace(0xA0000000),ace(0x10000000,True,3,'S-1-3-0')]
  self.check()
 def test_generic_writes_existing_and_future_still_rejected(self):
  for index,rule in ((0,ace(0x40000000)),(1,ace(0x40000000)),(2,ace(0x10000000)),
                     (2,ace(0x40000000,True,2)),(2,ace(0x80040000)),
                     (2,dict(ace(2),type='Callback'))):
   with self.subTest(index=index,rule=rule):
    saved=self.rows[index]['rules'];self.rows[index]['rules']=[rule]
    with self.assertRaises(m.CheckError):self.check()
    self.rows[index]['rules']=saved
  self.rows.append(node(LEAF+'\\nonce.response',rules=[ace(0x40000000)],kind='file'))
  with self.assertRaises(m.CheckError):self.check()
 def test_denies_dont_override_dangerous_allow(self):
  self.rows[2]['rules']=[dict(ace(2),type='Deny'),ace(2)]
  with self.assertRaises(m.CheckError):self.check()
if __name__=='__main__':unittest.main()
