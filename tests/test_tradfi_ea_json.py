"""Run the EA's actual Snapshot/Ack/quoting code with offline MT5 data adapters.

C++ checks string assembly, not MQL compilation. No JSON is built by Python for the
fixtures under test. Changes to EA serializers automatically change these outputs.
"""
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
EA = ROOT / 'mt5/TradFiGateway.mq5'


def function(source, name):
    match = re.search(r'^(?:string|void) ' + name + r'\([^\n]*\)\s*\{', source, re.M)
    if not match:
        raise AssertionError('Missing EA serializer ' + name)
    start, pos, depth = match.start(), match.end(), 1
    while depth:
        token = re.match(r'"(?:\\.|[^"\\])*"|//[^\n]*|/\*[\s\S]*?\*/|.', source[pos:], re.S)
        value = token[0]
        if value == '{': depth += 1
        elif value == '}': depth -= 1
        pos += len(value)
    return source[start:pos]


class EAJsonSourceTests(unittest.TestCase):
    def test_all_datetime_json_fields_use_explicit_integer_conversion(self):
        snapshot = function(EA.read_text(), 'Snapshot')
        self.assertIn('IntegerToString((long)TimeGMT())', snapshot)
        self.assertIn('IntegerToString((long)bar[0].time)', snapshot)
        self.assertIn('IntegerToString((long)TimeGMT()-age)', snapshot)
        self.assertNotRegex(snapshot, r'\(string\)\s*(?:TimeGMT\(\)|bar\[0\]\.time)')


@unittest.skipUnless(shutil.which('g++'), 'g++ needed for offline EA serialization adapter')
class EAJsonRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.source = EA.read_text()
        names = ['Q', 'N', 'U', 'Bool', 'StateName', 'Side', 'Add', 'OrderJSON', 'Snapshot', 'Ack']
        actual = '\n'.join(function(cls.source, name) for name in names)
        # Only adapter syntax changes: numeric/date casts and MQL dynamic output array.
        actual = actual.replace('(string)', '(MqlString)').replace('MqlRates bar[];', 'MqlRates bar[1];')
        constants = sorted(set(re.findall(r'\b(?:ORDER|POSITION|DEAL|ACCOUNT|TERMINAL|SYMBOL|PERIOD)_[A-Z0-9_]+\b',
                                         actual + (ROOT/'tests/fixtures/mt5_json_shim.hpp').read_text())))
        enums = 'enum {' + ','.join(constants) + '};\n'
        cls.prefix = enums + (ROOT/'tests/fixtures/mt5_json_shim.hpp').read_text()
        cls.actual = actual
        # Extract additional acknowledgement payloads directly from Execute, not duplicate literals.
        cls.trade_extra = re.search(r'^\s+string extra=([^;]+);', cls.source, re.M)[1]
        cls.profit_extra = re.search(r'else Ack\(id,"DONE","",([^;]+)\);', cls.source)[1]
        cls.program = cls.compile(actual)

    @classmethod
    def compile(cls, serializers, suffix='fixed'):
        runner = r'''
int main(int argc,char **argv) {
    string mode=argc>1?argv[1]:"empty";
    if(mode=="quote") {
        string all; for(int i=0;i<32;i++) all+=char(i);
        all+="\"\\Брокер🙂"; std::cout << Q(all); return 0;
    }
    if(mode=="filled") {populated=true; server="Сервер\t\\\""; comment="comment\t\b\f\r\n\"\\";}
    if(mode=="future") {clock_value=datetime(4294967296L); candle_value=datetime(4294967280L);}
    if(mode=="disconnected") connected=false;
    if(mode=="acks") {
        string id="aaaaaaaaaaaaaaaaaaaaaaaa";
        struct {ulong order=18446744073709551615UL,deal=9007199254740993UL; unsigned int retcode=10009;} result;
        double profit=-12.375;
        string extra=TRADE_EXTRA;
        Ack(id,"DONE","reason\t\b\f\r\n\"\\Брокер",extra);
        std::cout << files["acks\\"+id+".json"] << "\n";
        Ack(id,"REJECTED","",PROFIT_EXTRA);
        std::cout << files["acks\\"+id+".json"] << "\n";
        Ack(id,"UNKNOWN","missing position");
        std::cout << files["acks\\"+id+".json"]; return 0;
    }
    Snapshot(); std::cout << files["snapshot.json"];
}
'''.replace('TRADE_EXTRA', cls.trade_extra.replace('(string)', '(MqlString)')).replace('PROFIT_EXTRA', cls.profit_extra)
        path = Path(cls.tmp.name)/suffix
        path.with_suffix('.cpp').write_text(cls.prefix + serializers + runner)
        run = subprocess.run(['g++', '-std=c++17', '-Wall', '-Wextra', str(path.with_suffix('.cpp')), '-o', str(path)],
                             text=True, capture_output=True, timeout=30)
        if run.returncode:
            raise AssertionError(run.stderr)
        return path

    def output(self, mode, program=None):
        return subprocess.check_output([str(program or self.program), mode], text=True, timeout=5)

    def test_empty_snapshot_is_json_with_integer_epoch_timestamps(self):
        data = json.loads(self.output('empty'))
        self.assertEqual(data['now'], 1791720000)
        self.assertEqual(data['candle']['id'], 1791719940)
        self.assertEqual(data['quote']['utc_time'], 1791720000)
        self.assertTrue(data['hedging'])
        for key in ('orders', 'positions', 'order_history', 'deals'): self.assertEqual(data[key], [])

    def test_populated_snapshot_unicode_comments_and_integer_deal_time(self):
        data = json.loads(self.output('filled'))
        for key in ('orders', 'positions', 'order_history', 'deals'): self.assertEqual(len(data[key]), 2)
        self.assertIn('\t\\"', data['account'])
        self.assertEqual(data['orders'][0]['command_id'], 'comment\t\b\f\r\n"\\')
        self.assertEqual(data['deals'][0]['time'], 1791720000123)
        self.assertEqual(data['orders'][0]['id'], '9007199254740993')

    def test_control_characters_and_unicode_round_trip(self):
        self.assertEqual(json.loads(self.output('quote')), ''.join(map(chr, range(32))) + '"\\Брокер🙂')

    def test_ack_trade_profit_error_and_unknown_paths(self):
        trade, profit, unknown = map(json.loads, self.output('acks').splitlines())
        self.assertEqual(trade['order_id'], '18446744073709551615')
        self.assertEqual(trade['retcode'], 10009)
        self.assertEqual(trade['reason'], 'reason\t\b\f\r\n"\\Брокер')
        self.assertEqual(profit['profit'], '-12.37500000')
        self.assertEqual(unknown['status'], 'UNKNOWN')

    def test_64_bit_epoch_and_false_boolean(self):
        data = json.loads(self.output('future'))
        self.assertEqual(data['now'], 4294967296)
        self.assertEqual(data['candle']['id'], 4294967280)
        self.assertIs(json.loads(self.output('disconnected'))['connected'], False)

    def test_original_datetime_cast_reproduces_reported_char_118(self):
        broken = self.actual.replace('IntegerToString((long)TimeGMT())', '(MqlString)TimeGMT()')
        program = self.compile(broken, 'original_datetime_bug')
        with self.assertRaises(json.JSONDecodeError) as caught:
            json.loads(self.output('empty', program))
        self.assertEqual(caught.exception.pos, 118)
        self.assertEqual(caught.exception.colno, 119)

    def test_candle_cast_independently_breaks_json_if_only_now_is_fixed(self):
        broken = self.actual.replace('IntegerToString((long)bar[0].time)', '(MqlString)bar[0].time')
        program = self.compile(broken, 'candle_datetime_bug')
        with self.assertRaises(json.JSONDecodeError): json.loads(self.output('empty', program))
