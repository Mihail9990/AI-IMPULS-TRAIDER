"""Offline Chromium test of the real authenticated DEMO routes; native API mocked."""
from pathlib import Path
import sys,json,time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tests'))
from playwright.sync_api import sync_playwright
from test_tradfi_demo import DemoTest
from tradfi.demo_runtime import DemoPanel
ROOT=Path(__file__).resolve().parents[1]/'web/basket-panel'

def main():
    case=DemoTest();case.setUp();panel=DemoPanel(case.s,authority='127.0.0.1:8765',password='x'*32)
    errors=[]
    def route(r):
        path=r.request.url.split('8765',1)[-1]
        if path.startswith('/api/'):
            panel.step();h=r.request.all_headers();h['host']='127.0.0.1:8765';code,data,headers=panel.transport.request(r.request.method,path,h,r.request.post_data_buffer or b'')
            r.fulfill(status=code,body=data if isinstance(data,bytes) else json.dumps(data),headers={'Content-Type':'application/json',**headers})
        else:
            filename='index.html' if path=='/' else path[1:]
            r.fulfill(body=(ROOT/filename).read_bytes(),content_type='text/html' if filename.endswith('.html') else 'text/javascript' if filename.endswith('.js') else 'text/css')
    try:
        with sync_playwright() as p:
            browser=p.chromium.launch(executable_path='/usr/bin/chromium',headless=True,args=['--no-sandbox'])
            page=browser.new_page(viewport={'width':390,'height':844});page.on('pageerror',lambda e:errors.append(str(e)));page.on('dialog',lambda d:d.accept());page.route('**/*',route)
            def wait(expression):
                end=time.monotonic()+10
                while not page.evaluate(expression):
                    if time.monotonic()>end:raise AssertionError(expression)
                    page.wait_for_timeout(40)
            page.goto('http://127.0.0.1:8765/');page.locator('#password').fill('x'*32);page.locator('#login-button').click();page.locator('#demo-test').wait_for(state='visible')
            assert 'ВЫКЛЮЧЕНО' in page.locator('#demo-status').inner_text();assert not case.api.sent
            page.locator('#demo-enable').click();wait("document.querySelector('#demo-status').textContent.startsWith('ВКЛЮЧЕНО')")
            page.locator('#demo-sl').fill('3995');page.locator('#demo-tp').fill('4005');page.get_by_role('button',name='Открыть BUY…',exact=True).click()
            wait("localStorage.getItem('demo-test-pending-v1')===null");assert len(case.api.ps)==1
            page.locator('#demo-ticket').fill('1');page.locator('#demo-sl').fill('NaN');page.get_by_role('button',name='Изменить SL / TP…',exact=True).click();assert len(case.api.sent)==1
            page.locator('#demo-sl').fill('3996');page.get_by_role('button',name='Изменить SL / TP…',exact=True).click();wait("localStorage.getItem('demo-test-pending-v1')===null");assert case.api.ps[0].sl==3996
            page.locator('#demo-disable').click();wait("document.querySelector('#demo-status').textContent.startsWith('ВЫКЛЮЧЕНО')");assert len(case.api.ps)==1 and case.api.ps[0].sl==3996
            page.locator('#demo-enable').click();wait("document.querySelector('#demo-status').textContent.startsWith('ВКЛЮЧЕНО')")
            page.get_by_role('button',name='Закрыть свой билет…',exact=True).click();wait("localStorage.getItem('demo-test-pending-v1')===null");assert not case.api.ps
            page.locator('#logs-section summary').click();page.locator('#log-scope').select_option('demo');page.locator('#logs-refresh').click();wait("document.querySelector('#logs-view').textContent.includes('demo_result')")
            with page.expect_download() as event:page.locator('#logs-download').click()
            download=event.value;assert download.suggested_filename=='tradfi-demo-logs.txt'
            body=Path(download.path()).read_text();assert 'demo_result' in body and '123456' not in body and 'Логи' in body
            for width in [360,390,768,1280]:
                page.set_viewport_size({'width':width,'height':900});assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            case.api.a.trade_mode=2;wait("document.querySelector('#demo-status').textContent.includes('НЕ DEMO')");assert not case.s.enabled
            assert not errors,errors
            browser.close();print('PASS: authenticated mobile/desktop DEMO lifecycle, invalid values, disable keeps protection, real account blocks; UTF-8 authenticated log download; all native sends mocked')
    finally:case.doCleanups()

if __name__=='__main__':main()
