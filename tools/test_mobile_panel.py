"""Optional browser regression: Python Playwright + Chromium; all HTTP intercepted offline."""
import json
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT=Path(__file__).resolve().parents[1]

def main():
    from tradfi.settings import Settings
    from tradfi.strategies.fixed_recovery import RecoveryConfig
    view=dict(source='INJECTED_MT5',scope='offline-test-scope',revision=0,symbol='XAUUSD.s',phase='WAIT_CANDLE',
        mode='auto',modules=dict(engine='legacy',recovery=RecoveryConfig().dump()),settings=Settings().dump(),
        read_only=False,stopped=False,cycle_id=None,recovery=None,legacy_recovery=None,time_block=None,
        quote=dict(bid='4000',ask='4000.10'),timestamp=1793385600,orders=[],positions=[],
        journal=['Офлайн проверка панели'],action_results={},feed=dict(source='INJECTED_MT5',feed_version='mt5_chart_v1',
            status='FRESH',connected=True,age_seconds=0,timestamp_semantics='VERIFIED_UTC',
            candles=[dict(open='3999',high='4001',low='3998',close='4000') for _ in range(70)]))
    authenticated=False;actions=[];errors=[]
    def route(r):
        nonlocal authenticated
        path=r.request.url.split('8765',1)[-1].split('?',1)[0]
        if path=='/api/login':authenticated=True;r.fulfill(json={'status':'AUTHENTICATED'});return
        if path=='/api/logout':authenticated=False;r.fulfill(json={'status':'SIGNED_OUT'});return
        if path=='/api/state':r.fulfill(status=200 if authenticated else 401,json=view if authenticated else {'error':'AUTH_REQUIRED'});return
        if path=='/api/action':
            assert authenticated
            request=r.request.post_data_json;actions.append(request)
            assert request['revision']==view['revision']
            view['revision']+=1;view['action_results'][request['id']]={'status':'APPLIED','revision':view['revision']}
            r.fulfill(status=202,json={'status':'QUEUED'});return
        files={'/':'index.html','/panel.js':'panel.js','/panel.css':'panel.css'}
        if path not in files:r.abort();return
        body=(ROOT/'web/local-panel'/files[path]).read_text()
        if path=='/':body=body.replace('<body>','<body data-controller="native">')
        r.fulfill(body=body,content_type={'/':'text/html','/panel.js':'text/javascript','/panel.css':'text/css'}[path])
    with sync_playwright() as pw:
        browser=pw.chromium.launch(executable_path='/usr/bin/chromium',headless=True,args=['--no-sandbox'])
        try:
            page=browser.new_page(viewport={'width':390,'height':844},is_mobile=True,has_touch=True)
            page.route('**/*',route);page.on('pageerror',lambda error:errors.append(str(error)))
            page.on('dialog',lambda dialog:dialog.accept())
            page.goto('http://127.0.0.1:8765/')
            page.locator('#native-login').wait_for(state='visible')
            page.locator('#web-password').fill('offline-only-random-length-password-123')
            page.get_by_role('button',name='Войти',exact=True).click()
            page.locator('#native-login').wait_for(state='hidden')
            assert page.locator('#web-password').input_value()==''
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), 'Mobile horizontal overflow'
            page.locator('#start-auto').click();page.wait_for_function("document.querySelector('#service-mode').textContent.includes('EXECUTION')")
            page.locator('#stop-bot').click()
            page.get_by_text('Настройки стратегии',exact=True).click()
            page.locator('#strategy-INITIAL_ENTRY_DISTANCE').fill('1.25')
            page.locator('#save-strategy').click()
            page.get_by_text('Ручные уровни SEMI',exact=True).click()
            page.locator('#trigger-buy').fill('4002');page.locator('#send-triggers').click()
            page.locator('#reset-manual').click()
            page.locator('#resume-time').click()
            assert len(actions)==6, actions
            assert len({r['id'] for r in actions})==len(actions)
            assert actions[0]['action']=={'type':'start','mode':'auto'}
            assert actions[1]['action']=={'type':'command','text':'/stop'}
            assert 'INITIAL_ENTRY_DISTANCE=1.25' in actions[2]['action']['text']
            assert actions[3]['action']['levels']=={'BUY':'4002'}
            assert actions[4]['action']['text']=='/reset_manual'
            assert actions[5]['action']['text']=='/resume_time'
            assert not errors,errors
            for width in (360,412,768,1280):
                page.set_viewport_size({'width':width,'height':900})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'),width
            print('PASS: mobile login, 6 commands, unique request IDs, settings/manual previews, no JS errors, widths 360/390/412/768/1280; all HTTP intercepted.')
        finally:browser.close()

if __name__=='__main__':main()
