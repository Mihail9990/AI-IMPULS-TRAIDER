"""Offline Chromium route interception + real basket controller/gateway, no server."""
from pathlib import Path
import sys,time,tempfile
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tests'))
from playwright.sync_api import sync_playwright
from test_tradfi_basket import BasketTest
from tradfi.control import ActionGateway

ROOT=Path(__file__).resolve().parents[1]/'web/basket-panel'

def main():
    f=BasketTest();f.setUp();f.reversal();gate=ActionGateway(f.bot,execute=f.bot.action)
    auth=False;requests=[];errors=[]
    def view():
        f.tick(2);s=f.bot.state;c=s.get('cycle') or {};snap=f.b.snapshot()
        return dict(contract='ticket_basket_v1',source='OFFLINE_FIXTURE',scope='basket-browser-test',revision=s.get('control_revision',0),phase=s['phase'],cycle_id=c.get('id'),settings=s['basket_settings'],active_settings=c.get('cfg'),debt=c.get('recovery','0'),realized=c.get('realized','0'),stopped=s['stop'],read_only=False,migration_block=None,manual_reason=c.get('manual_reason'),time_block=None,
            feed=dict(status='FRESH',quote=snap['quote'],age_seconds=0,candles=[dict(open='3998',close='3998.2',high='3998.5',low='3997.5')]*60),
            frozen_levels=c.get('expanded'),levels=f.bot.levels(),positions=snap['positions'],orders=snap['orders'],tickets=list(c.get('tickets',{}).values()),edit=c.get('edit'),projection=None,journal=[n['text'] for n in s['notifications']],action_results=s.get('control_receipts',{}))
    def route(r):
        nonlocal auth
        path=r.request.url.split('8765',1)[-1].split('?',1)[0]
        if path=='/api/login':auth=True;r.fulfill(json={'status':'AUTHENTICATED'});return
        if path=='/api/logout':auth=False;r.fulfill(json={'status':'SIGNED_OUT'});return
        if path=='/api/state':r.fulfill(status=200 if auth else 401,json=view() if auth else {'error':'AUTH_REQUIRED'});return
        if path=='/api/action':
            assert auth;request=r.request.post_data_json;requests.append(request);gate.apply(request);r.fulfill(status=202,json={'status':'QUEUED'});return
        files={'/':'index.html','/panel.js':'panel.js','/panel.css':'panel.css'}
        if path not in files:r.abort();return
        r.fulfill(body=(ROOT/files[path]).read_text(),content_type={'/':'text/html','/panel.js':'text/javascript','/panel.css':'text/css'}[path])
    with sync_playwright() as pw:
        browser=pw.chromium.launch(executable_path='/usr/bin/chromium',headless=True,args=['--no-sandbox'])
        try:
            page=browser.new_page(viewport=dict(width=390,height=844),is_mobile=True,has_touch=True)
            page.route('**/*',route);page.on('pageerror',lambda e:errors.append(str(e)));page.on('dialog',lambda d:d.accept())
            def wait(condition):
                end=time.monotonic()+8
                while not page.evaluate(condition):
                    if time.monotonic()>end:raise AssertionError(condition)
                    page.wait_for_timeout(30)
            page.goto('http://127.0.0.1:8765/');page.locator('#password').fill('offline-not-a-real-password');page.locator('#login-button').click();page.locator('#workspace').wait_for(state='visible')
            assert page.locator('#tickets tr').count()==6
            page.locator('#setting-BASKET_COUNT').fill('0');page.locator('#save-settings').click()
            wait("document.querySelector('#action-error').textContent.includes('Неверные параметры')")
            assert f.bot.state['basket_settings']['BASKET_COUNT']==6
            page.locator('#setting-BASKET_COUNT').fill('4');page.locator('#save-settings').click()
            wait("document.querySelector('#edit-status').textContent==='' && localStorage.getItem('basket-request')===null")
            assert f.bot.state['basket_settings']['BASKET_COUNT']==4;assert len(f.b.data['positions'])==6
            page.locator('#chart').scroll_into_view_if_needed()
            target=page.locator('g[data-line$="-tp"] line').first
            box=target.bounding_box();x=box['x']+box['width']*.6;y=box['y']
            before=len(requests);page.mouse.move(x,y);page.mouse.down();page.wait_for_timeout(80);page.mouse.move(x,y+18);page.mouse.up()
            assert page.locator('#preview').is_hidden();assert len(requests)==before
            page.locator('#chart-reset').click();page.locator('#chart').scroll_into_view_if_needed();target=page.locator('g[data-line$="-tp"] line').first;box=target.bounding_box();x=box['x']+box['width']*.6;y=box['y']
            page.mouse.move(x,y);page.mouse.down();page.wait_for_timeout(580);page.mouse.move(x,y+12);page.mouse.up();page.locator('#preview').wait_for(state='visible')
            assert len(requests)==before
            page.locator('#new-price').fill('3997.60');page.locator('#apply-level').click()
            wait("localStorage.getItem('basket-request')===null && document.querySelector('#edit-status').textContent===''")
            assert any(str(p['tp'])=='3997.6' for p in f.b.data['positions'])
            # Browser reload retains actual ticket table and confirmed state, not a mock order.
            page.reload();page.locator('#workspace').wait_for(state='visible');assert page.locator('#tickets tr').count()==6
            page.locator('#stop').click();wait("localStorage.getItem('basket-request')===null");assert f.bot.state['stop']
            assert len({r['id'] for r in requests})==len(requests)
            for width in (360,390,412,768,1280):
                page.set_viewport_size(dict(width=width,height=900));assert page.evaluate('document.documentElement.scrollWidth<=innerWidth'),width
            assert not errors,errors
            print('PASS: actual six-ticket state; invalid count rejected, next edit/Stop allowed; count applies to future baskets; short drag sends nothing; 500ms long-press -> preview -> explicit TP confirmation -> broker readback; reload; widths360/390/412/768/1280; all HTTP intercepted.')
        finally:browser.close();f.doCleanups()

if __name__=='__main__':main()
