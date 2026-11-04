"""Chromium on actual authenticated route handlers + two independent controllers. Offline."""
from pathlib import Path
import sys,json,time,secrets
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tests'))
from playwright.sync_api import sync_playwright
from test_tradfi_basket import BasketTest
from test_tradfi_paper import PaperTest
from tradfi.web_control import NativePanel
ROOT=Path(__file__).resolve().parents[1]/'web/basket-panel'

def main():
    live=BasketTest();live.setUp();live.reversal();fixed=live.bot.clock();live.bot.clock=lambda:fixed;live.bot.monotonic=lambda:fixed;live.bot._cycle_clock=None;paper=PaperTest();paper.setUp();paper.reversal()
    live.b.account='TEST-LIVE';live.b.symbol='XAUUSD.s'
    from tradfi.web_auth import resolve_password
    credential=secrets.token_urlsafe(6)[:5]
    panel=NativePanel(live.bot,password=resolve_password({'password':credential},{}),paper_session=paper.session)
    requests=[];errors=[];drop=[False];dialogs=[]
    def refresh():
        live.tick(2);panel.snapshot=live.b.snapshot()
        panel.feed.view=lambda:dict(source='OFFLINE_MT5_FIXTURE',status='FRESH',timestamp=paper.reader.now,age_seconds=0,quote=live.b.data['quote'],candles=[dict(open='3998',close='3998.2',high='3998.5',low='3997.5',utc_time=paper.reader.now-60*i) for i in reversed(range(60))])
        paper.reader.data['candles']=panel.feed.view()['candles'];paper.session.step()
        panel.mailbox.drain(panel.gateway,panel.view)
        panel.logs.capture('live',live.store,live.bot.state,fixed,panel.view())
        panel.logs.capture('paper',paper.session.store,paper.session.controller.state,paper.reader.now,paper.session.view())
    def route(r):
        path=r.request.url.split('8765',1)[-1]
        if path.startswith('/api/'):
            refresh()
            if path.endswith('/action'):
                requests.append((path,json.loads(r.request.post_data)))
                if drop[0] and path=='/api/paper/action':r.abort();return
            h=dict(r.request.headers);h['host']='127.0.0.1:8765'
            code,data,extra=panel.transport.request(r.request.method,path,h,(r.request.post_data or '').encode())
            if isinstance(data,bytes):r.fulfill(status=code,body=data,headers={'Content-Type':'text/plain; charset=utf-8',**extra})
            else:r.fulfill(status=code,json=data,headers=extra)
            return
        files={'/':'index.html','/panel.js':'panel.js','/panel.css':'panel.css'}
        if path not in files:r.abort();return
        r.fulfill(body=(ROOT/files[path]).read_text(),content_type={'/':'text/html','/panel.js':'text/javascript','/panel.css':'text/css'}[path])
    with sync_playwright() as pw:
        browser=pw.chromium.launch(executable_path='/usr/bin/chromium',headless=True,args=['--no-sandbox'])
        try:
            page=browser.new_page(viewport=dict(width=390,height=844),is_mobile=True,has_touch=True);page.route('**/*',route);page.on('pageerror',lambda e:errors.append(str(e)));page.on('dialog',lambda d:(dialogs.append(d.type),d.accept()))
            def wait(expression):
                end=time.monotonic()+10
                while not page.evaluate(expression):
                    if time.monotonic()>end:raise AssertionError(expression)
                    page.wait_for_timeout(40)
            page.goto('http://127.0.0.1:8765/');page.locator('#password').fill(credential);page.locator('#login-button').click();page.locator('#workspace').wait_for(state='visible')
            page.locator('#open-pattern').click();assert page.locator('#analysis-pane').is_visible();assert page.locator('#chart').is_visible()
            page.locator('#main-cycle').uncheck();assert page.locator('#chart g[visibility=visible]').count()==0;assert page.locator('#analysis-chart g[visibility=visible]').count()>0
            page.locator('#analysis-cycle').uncheck();page.locator('#analysis-trend').check();page.locator('#open-figure').click();assert page.locator('#analysis-cycle').is_checked();assert not page.locator('#analysis-trend').is_checked()
            page.locator('#open-pattern').click();assert not page.locator('#analysis-cycle').is_checked();assert page.locator('#analysis-trend').is_checked()
            page.locator('#main-cycle').check();page.locator('.appearance summary').click();page.locator('#color-BUY').fill('#39c5bb');page.locator('#zone-opacity').fill('37');page.locator('#liquidity-visible').check()
            page.reload();page.locator('#workspace').wait_for(state='visible');assert page.evaluate("JSON.parse(localStorage.getItem('chart-preferences-v1')).opacity===37")
            assert page.evaluate("JSON.parse(localStorage.getItem('chart-preferences-v1')).colors.BUY==='#39c5bb'")
            assert 'не подключён' in page.locator('.pending-analysis').inner_text()
            # A tab switch itself emits no trading action, and cancels the old preview.
            before=len(requests);live_before=live.store.load();page.locator('#tab-paper').click();wait("document.querySelector('#mode').textContent.startsWith('PAPER')")
            assert len(requests)==before;assert page.locator('#tickets tr').count()==6
            page.locator('#level-buttons button').filter(has_text='TP ').first.click();assert page.locator('#preview').is_visible()
            page.locator('#tab-live').click();wait("document.querySelector('#tab-live').getAttribute('aria-selected')==='true'");assert page.locator('#preview').is_hidden();assert len(requests)==before
            page.locator('#tab-paper').click();wait("document.querySelector('#mode').textContent.startsWith('PAPER')")
            page.locator('#setting-BASKET_COUNT').fill('0');page.locator('#save-settings').click();wait("localStorage.getItem('basket-request-paper')===null && document.querySelector('#action-error').textContent.includes('Неверные параметры')")
            page.locator('#setting-BASKET_COUNT').fill('6');page.locator('#save-settings').click();wait("localStorage.getItem('basket-request-paper')===null")
            page.locator('#refresh-settings').click();assert page.locator('#tp-grid-settings').is_visible()
            assert page.locator('#setting-TP_STEP_MULTIPLIER').count()==0
            before_tp_requests=len(requests);page.locator('#tp-distance-0').fill('');page.locator('#save-tp-grid').click()
            assert len(requests)==before_tp_requests;assert 'TP 1' in page.locator('#action-error').inner_text()
            page.locator('#tp-distance-0').fill('.30');page.locator('#tp-distance-2').fill('.50');page.locator('#save-tp-grid').click()
            wait("localStorage.getItem('basket-request-paper')===null && document.querySelector('#edit-status').textContent===''")
            assert paper.session.controller.state['cycle']['tp_distances'][:3]==['0.30',None,'0.50']
            page.locator('#paper-cost').fill('2');page.locator('#paper-save-cost').click();wait("localStorage.getItem('basket-request-paper')===null");assert paper.p['commission_per_lot_side']=='2'
            page.locator('#chart').scroll_into_view_if_needed()
            target=page.locator('#chart g[data-line$="-tp"] line').first;box=target.bounding_box();x=box['x']+box['width']*.6;y=box['y']
            page.mouse.move(x,y);page.mouse.down();page.wait_for_timeout(580);page.mouse.move(x,y+12);page.mouse.up();page.locator('#preview').wait_for(state='visible')
            old_free_tp=paper.p['positions'][1]['tp'];fixed_tp=paper.p['positions'][2]['tp'];old_sl=[p['sl'] for p in paper.p['positions']]
            changed=f"{float(paper.p['positions'][0]['tp'])-.02:.2f}"
            page.locator('#new-price').fill(changed);page.locator('#apply-level').click();wait("localStorage.getItem('basket-request-paper')===null && document.querySelector('#edit-status').textContent===''")
            assert float(paper.p['positions'][0]['tp'])==float(changed)
            assert paper.p['positions'][1]['tp']!=old_free_tp;assert paper.p['positions'][2]['tp']==fixed_tp
            assert [p['sl'] for p in paper.p['positions']]==old_sl
            page.locator('#level-buttons button').filter(has_text='TP ').first.click()
            pstate=paper.session.controller.state
            paper.session.gateway.apply(dict(id='other-tab-costs',revision=pstate['control_revision'],action=dict(type='paper_costs',commission_per_lot_side='3')))
            prior=[(p['sl'],p['tp']) for p in paper.p['positions']]
            page.locator('#apply-level').click();wait("localStorage.getItem('basket-request-paper')===null && document.querySelector('#action-error').textContent.includes('устарел')")
            assert [(p['sl'],p['tp']) for p in paper.p['positions']]==prior
            if page.locator('#cancel-level').is_visible():page.locator('#cancel-level').click()
            assert live.store.load()==live_before, {k:(live_before.get(k),v) for k,v in live.store.load().items() if live_before.get(k)!=v}
            for width in (360,390,768,1280):
                page.set_viewport_size(dict(width=width,height=1000));assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                assert page.locator('#chart').is_visible();assert page.locator('#analysis-pane').is_visible()
                assert page.evaluate("document.querySelector('#chart').getBoundingClientRect().bottom<=document.querySelector('#chart-workspace').getBoundingClientRect().bottom+1")
                assert page.evaluate("document.querySelector('#analysis-chart').getBoundingClientRect().bottom<=document.querySelector('#chart-workspace').getBoundingClientRect().bottom+1")
            page.locator('.chart-card').screenshot(path='/tmp/paper-chart-desktop.png')
            page.set_viewport_size(dict(width=390,height=844));page.locator('.chart-card').screenshot(path='/tmp/paper-chart-mobile.png')
            page.locator('#close-analysis').click();assert page.locator('#analysis-pane').is_hidden();assert page.locator('#chart').is_visible()
            assert page.locator('#demo-test').count()==0
            page.locator('#logs-section summary').click()
            for scope in ('live','paper','runtime'):
                page.locator('#log-scope').select_option(scope);page.locator('#logs-refresh').click();wait("document.querySelector('#logs-status').textContent.includes('Страница')")
                with page.expect_download() as event:page.locator('#logs-download').click()
                download=event.value;assert download.suggested_filename=='tradfi-'+scope+'-logs.txt'
                data=Path(download.path()).read_bytes();assert len(data)<=1048576;data.decode('utf-8')
                if scope!='runtime':assert 'Логи' in data.decode()
            assert page.locator('#log-scope option[value=demo]').count()==0
            assert page.locator('#reverse-enable').is_disabled();assert page.locator('#reverse-disable').is_disabled()
            # First control sends immediately, with no confirmation dialog.
            first=paper.p['positions'][0];entry=float(first['entry'])
            paper.reader.tick(entry-.15,entry-.10);paper.step(3)
            wait("!document.querySelector('#own-entry-sl').disabled")
            assert page.locator('.cycle-controls .controls button').first.get_attribute('id')=='own-entry-sl'
            dialog_count=len(dialogs);page.locator('#own-entry-sl').click()
            wait("localStorage.getItem('basket-request-paper')===null && document.querySelector('#edit-status').textContent===''")
            assert len(dialogs)==dialog_count
            assert all(float(p['sl'])==float(p['entry']) for p in paper.p['positions'])
            assert live.store.load()==live_before
            # Observed paper tick closes TP1; allow its one-shot SL transfer to confirm.
            first=paper.p['positions'][0];entry=float(first['entry']);tp=float(first['tp'])
            paper.reader.tick(tp-.1,tp);paper.step(6)
            paper.reader.tick(entry-.45,entry-.35);paper.step(3)
            wait("document.querySelectorAll('#tickets tr').length===5")
            wait("!document.querySelector('#reverse-enable').disabled && document.querySelector('#reverse-disable').disabled")
            page.locator('#reverse-enable').click()
            wait("localStorage.getItem('basket-request-paper')===null && document.querySelector('#edit-status').textContent==='' && document.querySelector('#reverse-status').textContent.includes('включена')")
            assert page.locator('#reverse-enable').is_disabled();assert page.locator('#reverse-disable').is_enabled()
            assert len({p['sl'] for p in paper.p['positions']})==5
            before_tps=[p['tp'] for p in paper.p['positions']]
            page.locator('#reverse-disable').click()
            wait("localStorage.getItem('basket-request-paper')===null && document.querySelector('#edit-status').textContent==='' && document.querySelector('#reverse-status').textContent.includes('выключена')")
            assert page.locator('#reverse-enable').is_enabled();assert page.locator('#reverse-disable').is_disabled()
            assert len({p['sl'] for p in paper.p['positions']})==1
            assert [p['tp'] for p in paper.p['positions']]==before_tps
            page.locator('#manual-close-controls summary').click()
            for width in (360,390,768,1280):
                page.set_viewport_size(dict(width=width,height=1000));assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            page.set_viewport_size(dict(width=390,height=844))
            page.locator('#close-range-mode').select_option('from_close');page.locator('#close-range-duration').select_option('once')
            page.locator('#manual-close-submit').click()
            wait("localStorage.getItem('basket-request-paper')===null && document.querySelector('#manual-close-status').textContent.includes('до закрытия следующей корзины') && document.querySelectorAll('#tickets tr').length===12")
            assert not paper.p['positions'];assert len(paper.p['orders'])==12
            assert live.store.load()==live_before
            # Writeoff is explicit, flat-only and stays entirely in paper scope.
            assert page.locator('#writeoff-recovery').is_disabled()
            page.locator('#stop').click()
            wait("localStorage.getItem('basket-request-paper')===null && !document.querySelector('#writeoff-recovery').disabled")
            ledger_before=list(paper.p['deals']);debt_before=paper.session.controller.state['cycle']['recovery']
            page.locator('#writeoff-recovery').click()
            wait("localStorage.getItem('basket-request-paper')===null && document.querySelector('#phase').textContent==='WAIT_CANDLE'")
            assert paper.session.controller.state['recovery_writeoffs'][-1]['amount']==debt_before
            assert paper.p['deals']==ledger_before and not paper.p['positions'] and not paper.p['orders']
            assert live.store.load()==live_before
            assert page.locator('#writeoff-recovery').is_disabled()
            # Network ambiguity belongs only to paper; neither tab switch nor reload replays it.
            drop[0]=True;page.locator('#stop').click();wait("localStorage.getItem('basket-request-paper')!==null");page.wait_for_timeout(100)
            page.locator('#tab-live').click();wait("document.querySelector('#tab-live').getAttribute('aria-selected')==='true'")
            wait("!document.querySelector('#mode').textContent.startsWith('PAPER') && !document.querySelector('#start').disabled");count=len(requests);page.reload();page.locator('#workspace').wait_for(state='visible');assert len(requests)==count
            page.locator('#tab-paper').click();wait("document.querySelector('#mode').textContent.startsWith('PAPER')");assert page.locator('#start').is_disabled();assert len(requests)==count
            assert not errors,errors
            print('PASS: actual auth/routes; isolated live/paper; no requests on tab switch; preview cancelled across mode; per-mode UNKNOWN persists; invalid setting then valid action; mandatory TP1 UI validation; active interpolated TP grid confirmed; retired multiplier hidden; paper long-press TP readback; stale preview rejected; independent panes/toggles; colors/opacity persist; main visible; mobile360/390 desktop768/1280; no live state changes; no JS errors; DEMO absent; live/paper/runtime UTF-8 log downloads PASS; local-config 5-character login PASS; reverse ladder activation/cancel after TP1 with actual paper SL readback; manual close via authenticated paper route, confirmed full closure and rearmed 12 STOPs, live DB unchanged, expanded controls responsive PASS.')
        finally:browser.close();paper.tearDown();paper.doCleanups();live.doCleanups()
if __name__=='__main__':main()
