import unittest
from decimal import Decimal
from recovery.volume_schedule import volume_for_slot

class VolumeTableTests(unittest.TestCase):
    def test_all_user_slots(self):
        groups=[(1,5,'.01'),(6,8,'.02'),(9,10,'.03'),(11,12,'.04'),(13,14,'.05'),
                (15,16,'.06'),(17,18,'.07'),(19,20,'.08'),(21,22,'.09'),(23,24,'.10')]
        for first,last,value in groups:
            for slot in range(first,last+1):self.assertEqual(volume_for_slot(slot),Decimal(value))
    def test_no_invented_continuation(self):
        for value in (0,25,-1,1.0,True,'1',None):
            with self.assertRaises(ValueError):volume_for_slot(value)
