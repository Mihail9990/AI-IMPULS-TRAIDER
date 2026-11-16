import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from tradfi.native_migration import prepare_review, prepare_new_installation

class NativeMigrationTests(unittest.TestCase):
    def test_preserves_state_tp_distances_and_profiles_without_activation(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);dbfile=root/'old.sqlite3';config=root/'old.json'
            state=dict(schema=1,phase='RUNNING',basket_settings=dict(INITIAL_SL_DISTANCE='1.73',INITIAL_TP_DISTANCE='.29',INITIAL_ENTRY_DISTANCE='.63',RECOVERY_SL_DISTANCE='2.17'),v3_settings={'entry_distance':'.91'},recovery_profiles={'recovery_sl_distance':'2.37','profiles':[{'id':'owner-custom'}]},commands={'x':{'status':'UNKNOWN'}},cycle={'recovery':'12.34','tickets':{'p':{'closed':False}}})
            db=sqlite3.connect(dbfile);db.execute('CREATE TABLE snapshot(id INTEGER PRIMARY KEY,payload TEXT)');db.execute('INSERT INTO snapshot VALUES(1,?)',(json.dumps(state),));db.commit();db.close()
            config.write_text(json.dumps(dict(expected_login=123,expected_server='Mock',symbol='XAUUSD.s',magic=10,web={'password':'private'})))
            before=dbfile.read_bytes()
            report=prepare_review(dbfile,config,root/'review','mock/common/files',native_magic=11)
            self.assertEqual(before,dbfile.read_bytes())
            self.assertEqual(report['ea_inputs_to_review']['InitialTPDistance'],'.29')
            self.assertEqual(report['ea_inputs_to_review']['InitialSLDistance'],'1.73')
            self.assertEqual(report['ea_inputs_to_review']['RecoveryCandleOffset'],'.91')
            self.assertEqual(report['ea_inputs_to_review']['StartingRecovery'],'12.34')
            self.assertIsNone(report['ea_inputs_to_review']['StartingSteps'])
            self.assertFalse(report['activation_authorized']);self.assertFalse(report['broker_checked'])
            self.assertEqual(report['unresolved_command_count'],1);self.assertEqual(report['open_ticket_count'],1)
            self.assertEqual(report['preserved_profiles'],state['recovery_profiles'])
            backup=sqlite3.connect(root/'review/legacy-state-backup.sqlite3')
            self.assertEqual(json.loads(backup.execute('SELECT payload FROM snapshot').fetchone()[0]),state);backup.close()
            with self.assertRaises(FileExistsError):prepare_review(dbfile,config,root/'review','mock',native_magic=11)
            with self.assertRaises(ValueError):prepare_review(dbfile,config,root/'bad','mock',native_magic=10)

    def test_new_installation_does_not_read_or_overwrite_history(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);old=root/'old.sqlite3';old.write_bytes(b'old history remains untouched')
            settings=dict(login=123,server='Mock',symbol='XAUUSD.s',magic=1234,common_files_directory='new/mailbox',InitialSLDistance=1.73,InitialTPDistance=.29,CandleOffset=.63,RecoveryCandleOffset=.91,RecoverySLDistance=2.17)
            r=prepare_new_installation(settings,root/'new')
            self.assertEqual(old.read_bytes(),b'old history remains untouched')
            self.assertFalse(r['historical_closed_deals_imported']);self.assertFalse(r['activation_authorized'])
            self.assertTrue(r['ea_inputs_to_review']['NewAccountingStart'])
            self.assertEqual(r['ea_inputs_to_review']['StartingRecovery'],0)
            self.assertFalse(r['ea_inputs_to_review']['AllowTrading'])
            with self.assertRaises(FileExistsError):prepare_new_installation(settings,root/'new')
