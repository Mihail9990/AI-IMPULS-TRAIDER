import os
import tempfile
import unittest
from unittest.mock import patch
from service import Settings
from run import make_backend

class Tests(unittest.TestCase):
    def test_ea_selection_without_mt5_package(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ,{'WIDGET_BACKEND':'ea','WIDGET_EA_DIRECTORY':d}):
            backend=make_backend(Settings('x'*32,123,'server',''))
            self.assertTrue(backend.IS_EA_BRIDGE)
            self.assertTrue(backend.initialize())
    def test_invalid_backend(self):
        with patch.dict(os.environ,{'WIDGET_BACKEND':'bad'}):
            with self.assertRaises(ValueError): make_backend(Settings('x'*32,123,'server',''))
    def test_python_requires_terminal(self):
        with patch.dict(os.environ,{'WIDGET_BACKEND':'python'}):
            with self.assertRaises(ValueError): make_backend(Settings('x'*32,123,'server',''))
    def test_ea_requires_absolute_path(self):
        with patch.dict(os.environ,{'WIDGET_BACKEND':'ea','WIDGET_EA_DIRECTORY':'relative'}):
            with self.assertRaises(ValueError): make_backend(Settings('x'*32,123,'server',''))
