import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from recovery.ea_stop_evidence import ExitBinding,parse_stop_evidence,StopEvidenceUnavailable

class StopEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.b=ExitBinding('a'*32,123,'Broker',456,789,100,'XAUUSD.s','SELL',123456,Decimal('.01'))
        self.p=('RECOVERY_DEAL_SL_V1\t'+'a'*32+'\t123\tBroker\t456\t789\t100\tXAUUSD.s\tSELL\t123456\t0.0100000000000000\t3999.3000000000000000\tEND\n').encode()
    def test_valid_and_idempotent(self):
        for _ in range(2):
            self.assertEqual(parse_stop_evidence(self.p,self.b),Decimal('3999.3'))
    def test_identity_and_volume_mismatch(self):
        for key,value in dict(request_id='b'*32,account=124,server='Other',deal=455,order=790,position=101,symbol='Other',exit_side='BUY',time_msc=123457,volume=Decimal('.02')).items():
            with self.subTest(key=key),self.assertRaises(StopEvidenceUnavailable):
                parse_stop_evidence(self.p,replace(self.b,**{key:value}))
    def test_malformed(self):
        for p in (self.p[:-1],self.p+self.p,self.p[:-5],b'\xff',self.p.replace(b'3999.3000000000000000',b'NaN'),self.p.replace(b'3999.3000000000000000',b'0.0000000000000000')):
            with self.assertRaises(StopEvidenceUnavailable):parse_stop_evidence(p,self.b)
    def test_mirrored(self):
        self.assertEqual(parse_stop_evidence(self.p.replace(b'\tSELL\t',b'\tBUY\t'),replace(self.b,exit_side='BUY')),Decimal('3999.3'))
    def test_no_trade_calls_in_exporter(self):
        source=(Path(__file__).parents[1]/'recovery/mql5/RecoveryDealEvidence.mq5').read_text()
        for forbidden in ('OrderSend(', 'OrderSendAsync(', 'CTrade', 'WebRequest(', '#import'):
            self.assertNotIn(forbidden,source)

    def test_windows_crlf(self):
        self.assertEqual(parse_stop_evidence(self.p[:-1]+b'\r\n',self.b),Decimal('3999.3'))
        for p in (self.p[:-1]+b'\r',self.p[:-1]+b'\r\r\n',self.p.replace(b'Broker',b'Bro\rker')):
            with self.assertRaises(StopEvidenceUnavailable):parse_stop_evidence(p,self.b)

    def test_binary_price_tail_is_preserved_not_classified(self):
        p=self.p.replace(b'3999.3000000000000000',b'3999.3000000000001819')
        self.assertEqual(parse_stop_evidence(p,self.b),Decimal('3999.3000000000001819'))
