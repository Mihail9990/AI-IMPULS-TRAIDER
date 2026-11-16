"""The standalone installer accepts only its LIVE role."""
import unittest
import install_tradfi as tool

class StandaloneInstallerTest(unittest.TestCase):
    def test_rejects_foreign_role_in_all_paths(self):
        for fn,args in ((tool.config_path,('unused','other')),(tool.binding_path,('unused','other')),(tool.validate_instance,('other',))):
            with self.subTest(fn=fn.__name__),self.assertRaises(tool.SetupError):fn(*args)
    def test_contract_is_live_only(self):
        from independent_install import CONTRACT
        self.assertEqual(CONTRACT,{'format':2,'roles':['live'],'layout':'independent_roots_v1'})

if __name__=='__main__':unittest.main()
