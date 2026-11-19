import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from main_bot_owner import account_lock_path, main_bot_owner, AccountAlreadyOwned

S={'server':'Bybit-Live-6','account':123,'symbol':'XAUUSD.s','magic':5}
class MainBotOwnerTest(unittest.TestCase):
    def test_compatible_identity_and_path(self):
        expected=hashlib.sha256(json.dumps(['Bybit-Live-6',123,'XAUUSD.s'],ensure_ascii=True).encode('ascii')).hexdigest()
        self.assertEqual(account_lock_path(S,'root'),Path('root/AI IMPULS TradFi/locks')/(expected+'.lock'))
        self.assertEqual(account_lock_path(S,'root'),account_lock_path(dict(S,magic=6),'root'))
        self.assertNotEqual(account_lock_path(S,'root'),account_lock_path(dict(S,account=124),'root'))
    def test_refuses_parallel_owner_and_releases_after_close(self):
        with tempfile.TemporaryDirectory() as temp:
            with main_bot_owner(S,temp):
                with self.assertRaises(AccountAlreadyOwned):
                    with main_bot_owner(dict(S,magic=9),temp):
                        self.fail('A second process must not own the same symbol')
                with main_bot_owner(dict(S,account=124),temp):
                    pass
            with main_bot_owner(S,temp):
                pass
