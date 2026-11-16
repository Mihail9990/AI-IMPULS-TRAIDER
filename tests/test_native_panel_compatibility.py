from pathlib import Path
import shutil
import subprocess
import unittest
ROOT=Path(__file__).resolve().parents[1]
class NativePanelCompatibilityTests(unittest.TestCase):
    def test_preserves_original_chart_navigation_and_confirmation(self):
        source=(ROOT/'web/native-panel/panel.js').read_text()
        original=(ROOT/'web/basket-panel/panel.js').read_text()
        # Preserve actual chart/navigation code, not a replacement line-chart.
        for begin,end in [(' function visibleCandles()', ' function domain()'),(' const pointers=new Map();',' $(\'cancel-level\').onclick')]:
            self.assertEqual(source[source.index(begin):source.index(end)],original[original.index(begin):original.index(end)].replace("$('new-price').value=preview.value.toFixed(2)","$('new-price').value=String(preview.value)"))
        for name in ['logs-download','writeoff-recovery','cancel-initial','reset-pause','reset','monitor-save','close_all','cancel_triggers']:
            self.assertIn('id="'+name+'"',(ROOT/'web/native-panel/index.html').read_text())
    @unittest.skipUnless(shutil.which('node'),'node required')
    def test_adapter_only_translates_and_keeps_next_settings(self):
        script=r"""
const assert=require('assert'),a=require('./web/native-panel/native-adapter.js');
const v=a.view({protocol:'native_state_v1',phase:'RUNNING',settings:{initial_sl:'1',initial_tp:'.25',offset:'.5',recovery_offset:'.7',recovery_sl:'1.5',profiles:{3:'.3,.7,1'}},positions:[],orders:[],history:[],candles:[],action_results:{x:{status:'AWAITING_BROKER',receipt:{reason:'waiting'}}}});
assert.equal(v.action_results.x.status,'AWAITING_BROKER');
assert.deepEqual(a.action({type:'recovery_profile_select',profile_id:'3'},v),{type:'select_profile',steps:'3'});
const p=a.action({type:'basket_settings',settings:{INITIAL_TP_DISTANCE:'.4'}},v);assert.equal(p.initial_tp,'.4');assert.equal(p.profile_3,'.3,.7,1');assert.equal(p.recovery_offset,'.7');
assert.throws(()=>a.action({type:'v3_entry',order_type:'MARKET'},v));
assert.deepEqual(a.action({type:'protection',position_id:'77',old:{sl:'99',tp:'102'},new:{sl:'100',tp:'102'}},v),{type:'modify_level',position_id:'77',level:'SL',price:'100',expected_sl:'99',expected_tp:'102'});
"""
        subprocess.run(['node','-e',script],cwd=ROOT,check=True,timeout=10)
