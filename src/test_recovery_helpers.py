"""File-protocol tests against temporary synthetic MT5 Files on Windows."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parent))
import recovery_helpers as bridge


class HelpersTest(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory(prefix="recovery-helper-fixture-")
        self.addCleanup(tmp.cleanup)
        self.h=bridge.Helpers(Path(tmp.name),12,"TEST","X","a"*32)
        self.h.directory.mkdir(parents=True)
        self.vi="b"*32;self.sl="c"*32;self.run="d"*32
        self.clock=patch.object(bridge,"_uptime",return_value=10000)
        self.clock.start();self.addCleanup(self.clock.stop)
        self.verifier();self.write("sl-status.tsv","PYSL1",self.sl,["ok","-",0,0,1])

    def write(self,name,schema,instance,fields,rows=(),stamp=10000):
        header=[schema,self.h.scope,12,"TEST","X",instance,1,stamp,*fields]
        wire="\t".join(map(str,header))+"\n"+"\n".join([*rows,"END",""])
        (self.h.directory/name).write_text(wire,encoding="utf-8",newline="")

    def verifier(self):
        self.write("verifier.tsv","PYINV1",self.vi,["ok","0.0100000000000000","0.0100000000000000",1,1,0],
                   ["P\t11\t12\t0\t0\tBUY\t900\t2\t10000\t9910\t10500",
                    "O\t30\t0\t99\t5\t1\t3\t898\t3\t3\t9900\t10000\t0\t0"])

    def test_complete_inventory_and_rights(self):
        snap=self.h.snapshot()
        self.assertEqual(snap["positions"][0]["volume"],.02)
        self.assertEqual(snap["orders"][0]["remaining_volume"],.03)
        self.assertEqual(snap["orders"][0]["type"],5)
        self.assertFalse(snap["expert_trade_allowed"])
        self.assertTrue(self.h.sl_status()["trade_allowed"])

    def test_unavailable_is_not_empty(self):
        self.write("verifier.tsv","PYINV1",self.vi,["unavailable","0.01","0.01",0,0,0])
        with self.assertRaises(bridge.HelperUnavailable):self.h.snapshot()

    def test_stale_or_wrong_binding_rejected(self):
        self.write("sl-status.tsv","PYSL1",self.sl,["ok","-",0,0,1],stamp=1)
        with self.assertRaises(bridge.HelperUnavailable):self.h.sl_status()
        self.verifier()
        p=self.h.directory/"verifier.tsv"
        p.write_bytes(p.read_bytes().replace(b"\tTEST\t",b"\tOTHER\t"))
        with self.assertRaises(bridge.HelperUnavailable):self.h.snapshot()

    def test_register_replace_drop_and_single_writer(self):
        p=dict(position_id=11,ticket=12,magic=0,opened_msc=900,entry_ticks=10000,advance_ticks=40,side="BUY")
        self.assertEqual(self.h.register([p],self.run),self.sl)
        path=self.h.directory/"sl-register.tsv"
        original=path.read_bytes()
        self.assertIn(b"\t10000\t16000\t1\nR\t11\t12\t0\t900\tBUY\t10000\t40\nEND\n",original)
        with self.assertRaises(bridge.HelperUnavailable):self.h.register([],"e"*32)
        self.assertEqual(path.read_bytes(),original)
        with self.assertRaises(ValueError):self.h.register([p,p],self.run)
        self.h.register([],self.run)
        self.assertTrue(path.read_bytes().endswith(b"\t0\nEND\n"))
        with bridge._register_lock(self.h.directory/"register.lock"):
            with self.assertRaises(bridge.HelperUnavailable):self.h.register([p],self.run)

    def test_stop_confirmed_or_archived(self):
        for status in ("confirmed","protected","last_confirmed","last_protected","changed","pending","rejected"):
            self.write("sl-status.tsv","PYSL1",self.sl,["ok",self.run,16000,1,1],
                       [f"W\t11\t12\t0\t900\tBUY\t10000\t40\t10000\t12000\t1550\t{status}\t10009"])
            out=self.h.sl_status()
            self.assertEqual(out["rows"][0]["status"],status)
            self.assertEqual(out["run_id"],self.run)

    def test_order_history_requires_exact_challenge(self):
        with self.assertRaises(bridge.HelperUnavailable):self.h.history(11)
        request=(self.h.directory/"history-11.req").read_text().strip().split("\t")
        self.assertEqual(request[:7],["PYHREQ1",self.h.scope,"12","TEST","X",self.vi,"11"])
        body=["D\t2\t3\t11\t99\t1\t1\t4\t1234\t2\t9980\t10000\t10500"]
        payload=["ok",11,"e"*32,"0.0100000000000000","0.0100000000000000",1,"USD","ok",1]
        body+=["M\t2\t11\t3\t1\t0.0000000000000000\t-0.1000000000000000\t0.0000000000000000\t-0.0200000000000000"]
        self.write("history-11.tsv","PYHIST2",self.vi,payload,body)
        with self.assertRaises(bridge.HelperUnavailable):self.h.history(11)
        payload[2]=request[7]
        self.write("history-11.tsv","PYHIST2",self.vi,payload,body)
        d=self.h.history(11)["deals"][0]
        self.assertEqual((d["ticket"],d["reason"],d["price_ticks"],d["deal_sl_ticks"]),(2,4,9980,10000))
        self.assertNotIn("profit",d)
        self.assertNotIn("cash",self.h.history(11))
        legacy=d.copy()
        payload[-3:]=["-","unavailable",0]
        self.write("history-11.tsv","PYHIST2",self.vi,payload,body[:1])
        history=self.h.history(11)
        self.assertNotIn("currency",history)
        self.assertEqual(history["deals"][0],legacy)
        # A second structural D row cannot be hidden inside the legacy tail.
        payload[-1]=1
        self.write("history-11.tsv","PYHIST2",self.vi,payload,body[:1]*2)
        with self.assertRaises(bridge.HelperUnavailable):self.h.history(11)

    def test_priority_lists_all_recent_same_kind_challenges(self):
        for pid in (11,12):
            with self.assertRaises(bridge.HelperUnavailable):self.h.history(pid)
        with self.assertRaises(bridge.HelperUnavailable):self.h.order(31)
        def index(kind):
            return (self.h.directory/(kind+"-priority.tsv")).read_text().split("\t")
        self.assertEqual(index("history"),[self.vi,"10000","11","12","END\n"])
        self.assertEqual(index("order"),[self.vi,"10000","31","END\n"])
        self.vi="2"*32;self.verifier()
        with self.assertRaises(bridge.HelperUnavailable):self.h.history(13)
        self.assertEqual(index("history"),[self.vi,"10000","13","END\n"])

    def test_exact_historical_order_is_separate_from_position_id(self):
        with self.assertRaises(bridge.HelperUnavailable): self.h.order(31)
        request=(self.h.directory/"order-31.req").read_text().strip().split("\t")
        self.assertEqual(request[0],"PYOQ1")
        fields=["ok",31,request[7],"0.0100000000000000","0.0100000000000000"]
        row="H\t31\t0\t99\t5\t2\t3\t1234\t2500\t4\t4\t9999\t10079\t0\t0"
        self.write("order-31.tsv","PYORD1",self.vi,fields,[row])
        o=self.h.order(31)["order"]
        self.assertEqual((o["ticket"],o["position_id"],o["state"],o["remaining_steps"],o["initial_steps"]),(31,0,2,4,4))
        self.write("order-31.tsv","PYORD1",self.vi,fields,[row.replace("H\t31\t","H\t32\t")])
        with self.assertRaises(bridge.HelperUnavailable): self.h.order(31)

    def test_reinitialized_instance_rebinds_history_and_lease(self):
        with self.assertRaises(bridge.HelperUnavailable):self.h.history(11)
        req=self.h.directory/"history-11.req";before=req.read_bytes()
        self.vi="1"*32;self.verifier()
        with self.assertRaises(bridge.HelperUnavailable):self.h.history(11)
        self.assertNotEqual(req.read_bytes(),before)
        self.sl="2"*32;self.write("sl-status.tsv","PYSL1",self.sl,["ok","-",0,0,0])
        self.assertFalse(self.h.sl_status()["trade_allowed"])
        self.assertEqual(self.h.register([],self.run),self.sl)


if __name__=="__main__":
    unittest.main(verbosity=2)
