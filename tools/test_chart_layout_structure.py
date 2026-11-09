"""Static structure checks only: not a substitute for mobile browser rendering."""
from html.parser import HTMLParser
from pathlib import Path
import re
ROOT=Path(__file__).resolve().parents[1]/'web/basket-panel'
class Structure(HTMLParser):
    def __init__(self):
        super().__init__();self.stack=[];self.nodes={};self.void={'meta','link','input','hr','br','img'}
    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs);key=attrs.get('id')
        if key:
            assert key not in self.nodes,key
            self.nodes[key]=(tag,attrs,tuple(self.stack))
        if tag not in self.void:self.stack.append((tag,key))
    def handle_endtag(self,tag):
        assert self.stack and self.stack[-1][0]==tag,(tag,self.stack[-3:]);self.stack.pop()
p=Structure();p.feed((ROOT/'index.html').read_text());assert not p.stack
inside=lambda key,ancestor:any(id==ancestor for _,id in p.nodes[key][2])
for id in ('start','stop','resume','reset','logout','save-settings','refresh-settings','save-tp-grid','save-v3-settings','logs-refresh','logs-download','paper-save-cost','writeoff-recovery'):
    assert inside(id,'settings-page'),id
for id in ('own-entry-sl','reverse-enable','reverse-disable','cancel-initial','v3-submit-entry','v3-cancel-request','v3-link-toggle','v3-sl-plus','v3-sl-minus','level-buttons'):
    assert inside(id,'drawer-content'),id
assert not inside('bot-activity','chart-page') and not inside('bot-activity','settings-page')
assert p.nodes['bot-activity'][1]['tabindex']=='0'
assert not inside('chart-notices','chart-page') and not inside('chart-notices','settings-page')
assert inside('preview-error','preview')
for id in ('chart-height-mode','chart-height-range','chart-height-reset'):assert inside(id,'display-settings')
assert p.nodes['chart-height-range'][1]['min']=='30' and p.nodes['chart-height-range'][1]['max']=='85' and p.nodes['chart-height-range'][1]['step']=='5'
assert inside('chart','chart-page') and inside('preview','chart-page')
assert not inside('bottom-tabs','chart-page') and not inside('bottom-tabs','settings-page')
assert 'hidden' in p.nodes['settings-page'][1] and 'hidden' in p.nodes['drawer-content'][1]
assert p.nodes['drawer-toggle'][1]['aria-controls']=='drawer-content'
for tab in ('chart','settings'):assert p.nodes['tab-'+tab][1]['aria-controls']==tab+'-page'
js=(ROOT/'panel.js').read_text()
for id in re.findall(r"\$\('([^']+)'\)",js):
    if id.endswith('-'):continue
    assert id in p.nodes,id
assert "mode=document.body.dataset.role" in js and 'switchMode' not in js
print('PASS static chart-first structure, command reachability, IDs, tabs, separate role contract. Browser layout NOT verified.')
