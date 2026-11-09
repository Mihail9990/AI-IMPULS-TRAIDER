"""V3 panel UI smoke/regression. All HTTP intercepted; no broker/service/network."""
from pathlib import Path
import copy
import sys
from urllib.parse import urlsplit
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from tradfi.web_control import panel_html
from playwright.sync_api import sync_playwright
ROOT=Path(__file__).resolve().parents[1]/'web/basket-panel'

def main():
    base=dict(contract='ticket_basket_v1',scope='live-ui',revision=1,cycle_id=None,phase='WAIT_START',debt='0',realized='0',stopped=True,read_only=False,migration_block=None,manual_reason=None,time_block=None,
        settings=dict(INITIAL_SIZE='0.09',INITIAL_ENTRY_DISTANCE='1',INITIAL_SL_DISTANCE='3',INITIAL_TP_DISTANCE='.25',BASKET_COUNT=6,TICKET_SIZE='.01',RECOVERY_SL_DISTANCE='1.5',EXPANSION_DISTANCE='1',CYCLE_COOLDOWN_SECONDS='0'),
        feed=dict(status='FRESH',quote=dict(bid='4000',ask='4000.23'),age_seconds=0,candles=[dict(open='3999',close='4000',high='4001',low='3998',utc_time=1804800000+i*60) for i in range(20)]),levels={},positions=[],orders=[],journal=[],action_results={},projection=None,
        recovery_mode='V3',v3_settings=dict(entry_distance='1',count_presets=[4,6,10]),v3_request=None,v3_link=None,v3_sl_distance='1.50',v3_controls=dict(can_enter=True,can_link=True,can_cancel_link=False,can_adjust_sl=True,reason=''))
    state=copy.deepcopy(base);paper=copy.deepcopy(base);paper.update(scope='paper-ui',mode='PAPER',recovery_mode='V3')
    requests=[];errors=[];dialogs=[];auth={'live':True,'paper':True};fail_state=False;unknown=False;dismiss=False
    def route(r):
        url=urlsplit(r.request.url)
        role={8765:'live',8766:'paper'}.get(url.port)
        if role is None:r.abort();return
        path=url.path
        headers=r.request.all_headers()
        prefix='/api/paper/' if role=='paper' else '/api/'
        cookie_name='tradfi_'+role+'_session'
        if path=='/api/login':
            auth[role]=True;r.fulfill(json={},headers={'Set-Cookie':cookie_name+'=offline-'+role+'; HttpOnly; SameSite=Strict; Path=/'});return
        if path.startswith('/api/'):
            if not auth[role] or cookie_name+'=offline-'+role not in headers.get('cookie',''):
                r.fulfill(status=401,json={});return
        if path=='/api/logout':
            auth[role]=False;r.fulfill(json={},headers={'Set-Cookie':cookie_name+'=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0'});return
        if path==prefix+'state':
            if fail_state:r.fulfill(status=503,json={});return
            r.fulfill(json=state if role=='live' else paper);return
        if path==prefix+'action':
            req=r.request.post_data_json;s=state if role=='live' else paper
            if headers.get('x-controller-scope')!=s['scope']:
                r.fulfill(status=409,json={});return
            requests.append(req);a=req['action']
            assert a['type'] not in ('recovery_mode','manual_close'), 'Retired action must not be sent'
            if a['type']=='v3_settings':s['v3_settings']=dict(entry_distance=a['entry_distance'],count_presets=a['count_presets'])
            if a['type']=='v3_link':s['v3_link']=dict(stage='ARMED',count=a['count']);s['v3_controls']['can_cancel_link']=True
            if a['type']=='v3_cancel_link':s['v3_link']=None;s['v3_controls']['can_cancel_link']=False
            if a['type']=='v3_adjust_sl':s['v3_sl_distance']=a['distance']
            if a['type']=='v3_entry' and a['timing']!='now':s['v3_request']=a.copy();s['v3_controls']['can_enter']=False
            if a['type']=='v3_cancel_request':s['v3_request']=None;s['v3_controls']['can_enter']=True
            status='UNKNOWN' if unknown else 'APPLIED';s['action_results'][req['id']]=dict(status=status);s['revision']+=1
            r.fulfill(json=dict(status=status));return
        if path.startswith('/api/'):r.fulfill(status=404,json={});return
        files={'/':'index.html','/panel.js':'panel.js','/panel.css':'panel.css'}
        if path not in files:r.abort();return
        body=(ROOT/files[path]).read_bytes()
        if path=='/':body=panel_html(body,role)
        r.fulfill(body=body,content_type={'/':'text/html','/panel.js':'text/javascript','/panel.css':'text/css'}[path])
    with sync_playwright() as pw:
        b=pw.chromium.launch(executable_path='/usr/bin/chromium',headless=True,args=['--no-sandbox'])
        try:
            context=b.new_context(viewport=dict(width=390,height=844),is_mobile=True,has_touch=True)
            context.add_cookies([dict(name='tradfi_'+role+'_session',value='offline-'+role,url='http://127.0.0.1:8765/',httpOnly=True,sameSite='Strict') for role in ('live','paper')])
            context.route('**/*',route)
            p=context.new_page();p.on('pageerror',lambda e:errors.append(str(e)))
            def dialog(d):
                dialogs.append(d.message)
                d.dismiss() if dismiss else d.accept()
            p.on('dialog',dialog)
            def wait(js):p.wait_for_function(js,timeout=7000)
            p.goto('http://127.0.0.1:8765/');p.locator('#workspace').wait_for(state='visible')
            assert p.locator('#chart-page').is_visible();assert not p.locator('#settings-page').is_visible()
            assert not p.locator('#start').is_visible();assert p.locator('#chart [data-quote]').count()==2
            p.locator('#drawer-toggle').click();p.locator('#v3-panels').wait_for(state='visible');assert not p.locator('#v3-settings').is_visible()
            for removed in ('manual-close-controls','manual-close-submit','recovery-mode','save-recovery-mode'):
                assert p.locator('#'+removed).count()==0
            assert p.locator('#v3-side').is_disabled()
            assert p.locator('#v3-timing option[value=now]').is_disabled()
            assert p.locator('#v3-presets button').count()==3
            p.locator('#v3-presets button').nth(2).click();assert p.locator('#v3-count').input_value()=='10'
            before=len(requests);p.locator('#v3-count').fill('1.5');p.locator('#v3-submit-entry').click();assert len(requests)==before
            p.locator('#v3-count').fill('8');p.locator('#v3-order-type').select_option('MARKET');p.locator('#v3-timing').select_option('now');assert p.locator('#v3-side').is_enabled()
            p.locator('#v3-side').select_option('SELL');dismiss=True;p.locator('#v3-submit-entry').click();assert len(requests)==before
            dismiss=False;p.locator('#v3-submit-entry').click();wait("localStorage.getItem('basket-request')===null")
            assert requests[-1]['action']==dict(type='v3_entry',cycle_id=None,order_type='MARKET',timing='now',side='SELL',count=8)
            p.locator('#v3-order-type').select_option('STOP');assert p.locator('#v3-timing').input_value()=='current';assert p.locator('#v3-side').is_disabled()
            p.locator('#v3-timing').select_option('next');p.locator('#v3-submit-entry').click();p.locator('#v3-cancel-request').wait_for(state='visible');assert requests[-1]['action']['timing']=='next'
            p.locator('#v3-cancel-request').click();wait("document.querySelector('#v3-cancel-request').hidden")
            p.locator('#v3-sl-minus').click();wait("document.querySelector('#v3-sl-value').textContent==='1.40'")
            p.locator('#v3-sl-plus').click();wait("document.querySelector('#v3-sl-value').textContent==='1.50'")
            p.locator('#v3-link-toggle').click();wait("document.querySelector('#v3-link-toggle').dataset.cancel==='true'")
            n=len(dialogs);p.locator('#v3-link-toggle').click();wait("document.querySelector('#v3-link-toggle').dataset.cancel==='false'");assert len(dialogs)==n
            assert requests[-1]['action']['type']=='v3_cancel_link'
            # Each side panel collapses independently, persists after reload, never sends a command.
            before=len(requests);p.locator('#v3-sl-panel > summary').click();assert not p.locator('#v3-sl-panel').evaluate('(x)=>x.open');assert p.locator('#v3-entry-panel').evaluate('(x)=>x.open')
            p.reload();p.locator('#workspace').wait_for(state='visible');p.locator('#drawer-toggle').click();assert not p.locator('#v3-sl-panel').evaluate('(x)=>x.open');assert len(requests)==before
            p.locator('#v3-sl-panel > summary').click()
            for w in (320,360,390,430,768,900,1280):
                p.set_viewport_size(dict(width=w,height=900));assert p.evaluate('document.documentElement.scrollWidth<=innerWidth'),w
            p.set_viewport_size(dict(width=390,height=844))
            # Separate addresses and fixed roles share a host cookie jar, not controllers.
            pp=context.new_page();pp.on('pageerror',lambda e:errors.append(str(e)));pp.on('dialog',dialog)
            pp.goto('http://127.0.0.1:8766/');pp.locator('#workspace').wait_for(state='visible');pp.locator('#drawer-toggle').click()
            assert p.locator('body').get_attribute('data-role')=='live'
            assert pp.locator('body').get_attribute('data-role')=='paper'
            for page in (p,pp):assert page.locator('#tab-live, #tab-paper').count()==0
            pp.locator('#v3-sl-plus').click();pp.wait_for_function("document.querySelector('#v3-sl-value').textContent==='1.60'")
            assert paper['v3_sl_distance']=='1.60';assert state['v3_sl_distance']=='1.50'
            assert p.locator('#v3-sl-value').inner_text()=='1.50'
            for page,path in [(p,'/api/paper/state'),(pp,'/api/state')]:
                assert page.evaluate("async path => (await fetch(path,{headers:{'X-Local-Control':'1'}})).status",path)==404
            # Local chart height, separate roles, reload and drawer shrink; no action request.
            before=len(requests)
            for page,percent in ((p,'40'),(pp,'75')):
                page.locator('#tab-settings').click();page.locator('#display-settings > summary').click()
                page.locator('#chart-height-mode').select_option('custom')
                page.locator('#chart-height-range').evaluate("(el,value)=>{el.value=value;el.dispatchEvent(new Event('input',{bubbles:true}));}",percent)
                page.locator('#tab-chart').click()
                assert page.locator('#chart-page').get_attribute('data-chart-height')=='custom'
                assert page.locator('#chart').bounding_box()['height']>50
                assert page.locator('#bottom-tabs').bounding_box()['y']+page.locator('#bottom-tabs').bounding_box()['height']<=844+1
                page.reload();page.locator('#workspace').wait_for(state='visible')
                assert page.locator('#chart-height-range').input_value()==percent
                page.locator('#tab-settings').click();page.locator('#display-settings > summary').click()
                page.locator('#chart-height-reset').click();page.locator('#tab-chart').click();page.locator('#drawer-toggle').click()
            assert len(requests)==before
            p.locator('#tab-settings').click();p.locator('#logout').click();p.locator('#login').wait_for(state='visible')
            assert pp.evaluate("async () => (await fetch('/api/paper/state',{headers:{'X-Local-Control':'1'}})).status")==200
            assert {c['name'] for c in context.cookies()}=={'tradfi_paper_session'}
            p.locator('#password').fill('offline-live');p.locator('#login-button').click();p.locator('#workspace').wait_for(state='visible');p.locator('#drawer-toggle').click()
            pp.close()
            # Chart-first shell; all supported mobile widths and landscape, no action side effects.
            before=len(requests)
            for width,height in ((320,640),(360,780),(390,844),(430,932),(768,1024),(900,650),(1280,800),(844,390)):
                p.set_viewport_size(dict(width=width,height=height))
                p.locator('#tab-chart').click()
                if p.locator('#drawer-toggle').get_attribute('aria-expanded')=='true':p.locator('#drawer-toggle').click()
                chart=p.locator('#chart').bounding_box();nav=p.locator('#bottom-tabs').bounding_box()
                activity=p.locator('#bot-activity').bounding_box()
                assert activity['height']<=height*.1+1 and chart['y']>=activity['height']-1
                assert chart['height']>150,(width,height,chart)
                assert nav['y']+nav['height']<=height+1
                assert p.evaluate('document.documentElement.scrollWidth<=innerWidth'),width
                p.locator('#drawer-toggle').click()
                assert p.locator('#chart').bounding_box()['height']>80
                assert p.locator('#drawer-content').bounding_box()['y']>=p.locator('#chart').bounding_box()['y']
                p.locator('#tab-settings').click();assert p.locator('#start').is_visible();assert not p.locator('#chart').is_visible()
                assert p.evaluate('document.documentElement.scrollWidth<=innerWidth'),width
            assert len(requests)==before,'view navigation never sends commands'
            p.set_viewport_size(dict(width=390,height=844));p.locator('#tab-chart').click()
            p.screenshot(path='/tmp/trader-command-drawer.png')
            p.locator('#drawer-toggle').click();p.screenshot(path='/tmp/trader-clean-chart.png')
            p.locator('#tab-settings').click();p.screenshot(path='/tmp/trader-settings.png')
            p.locator('#tab-chart').click();p.locator('#drawer-toggle').click()
            # Legacy active snapshots remain visible but fail closed with a reason.
            state['recovery_mode']='V1';state['migration_block']='Legacy active cycle needs reconciliation'
            wait("document.querySelector('#safety').textContent.includes('Legacy active cycle')")
            assert p.locator('#v3-panels').is_visible();assert p.locator('#v3-submit-entry').is_disabled();assert p.locator('#save-v3-settings').is_disabled()
            state['recovery_mode']='V3';state['migration_block']=None;wait("!document.querySelector('#v3-submit-entry').disabled")
            # Read-only and stale snapshots block V3 commands.
            state['read_only']=True;wait("document.querySelector('#v3-submit-entry').disabled");assert p.locator('#v3-link-toggle').is_disabled()
            state['read_only']=False;wait("!document.querySelector('#v3-submit-entry').disabled")
            fail_state=True;wait("document.querySelector('#v3-control-reason').textContent.includes('Нет свежего')");assert p.locator('#v3-link-toggle').is_disabled()
            fail_state=False;wait("!document.querySelector('#v3-submit-entry').disabled")
            # UNKNOWN persists across reload; neither repeated press nor reload resends.
            unknown=True;p.locator('#v3-link-toggle').click();wait("localStorage.getItem('basket-request')!==null");n=len(requests)
            p.reload();p.locator('#workspace').wait_for(state='visible');assert p.locator('#v3-link-toggle').is_disabled();assert len(requests)==n
            assert not errors,errors
            p.screenshot(path='/tmp/v3-panel-mobile.png',full_page=True)
            print('PASS V3 UI: V3-only defaults, retired controls absent, legacy migration block, STOP/MARKET timing, count validation/presets, cancel preview, SL confirmation, immediate linked cancellation, independent collapse/reload, mobile widths, separate fixed-role addresses, scoped cookies/logout, cross-route rejection, read-only/stale/UNKNOWN. All HTTP mocked; no real trading.')
        finally:b.close()
if __name__=='__main__':main()
