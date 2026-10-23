"""Offline Chromium UI regressions. Routes are fulfilled from disk; no server/API.
Run with Python that has Playwright installed: python3 tools/test_local_panel.py.
"""
from pathlib import Path
import unittest
from playwright.sync_api import sync_playwright, expect

ROOT=Path(__file__).resolve().parents[1]/'web'/'local-panel'
ORIGIN='http://127.0.0.1:8765'


class LocalPanelUI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright=sync_playwright().start()
        cls.browser=cls.playwright.chromium.launch(executable_path='/usr/bin/chromium',headless=True,
            args=['--no-sandbox','--disable-background-networking','--disable-component-update',
                  '--disable-sync','--no-first-run','--host-resolver-rules=MAP * ~NOTFOUND',
                  '--proxy-server=http://127.0.0.1:9'])

    @classmethod
    def tearDownClass(cls):
        cls.browser.close(); cls.playwright.stop()

    def setUp(self):
        self.context=self.browser.new_context(viewport={'width':390,'height':844},is_mobile=True,has_touch=True)
        self.blocked=[]; self.errors=[]
        def route(request):
            url=request.request.url
            names={ORIGIN+'/':'index.html',ORIGIN+'/panel.js':'panel.js',ORIGIN+'/panel.css':'panel.css'}
            if url in names:
                p=ROOT/names[url]
                mime={'.html':'text/html','.js':'text/javascript','.css':'text/css'}[p.suffix]
                request.fulfill(body=p.read_bytes(),content_type=mime)
            else:
                self.blocked.append(url); request.abort()
        self.context.route('**/*',route)
        self.page=self.context.new_page(); self.page.on('pageerror',lambda e:self.errors.append(str(e)))
        self.page.clock.install()
        self.page.goto(ORIGIN+'/'); self.page.wait_for_function('window.localPanel !== undefined')

    def tearDown(self):
        self.assertEqual(self.blocked,[], 'Unexpected network request')
        self.assertEqual(self.errors,[])
        self.context.close()

    def snapshot(self): return self.page.evaluate('window.localPanel.snapshot()')

    def test_mobile_no_horizontal_overflow_and_explicit_mock(self):
        self.assertLessEqual(self.page.evaluate('document.documentElement.scrollWidth'),390)
        self.assertIn('MT5 НЕ ПОДКЛЮЧЁН',self.page.locator('.demo-banner').inner_text())
        self.assertIn('XAUUSD.s',self.page.title()+self.page.locator('h1').inner_text())
        self.assertEqual(self.page.locator('[data-level=entry]').get_attribute('class'),'read-only')

    def test_preview_cancel_and_apply_are_separate(self):
        self.page.locator('[data-level=tp]').click()
        self.page.locator('#new-price').fill('4140.50')
        self.assertEqual(self.snapshot()['levels']['tp'],4140)
        self.page.locator('#cancel-preview').click(); self.assertEqual(self.snapshot()['levels']['tp'],4140)
        self.page.locator('[data-level=tp]').click(); self.page.locator('#new-price').fill('4140.50')
        self.page.locator('#apply-preview').click(); self.assertEqual(self.snapshot()['levels']['tp'],4140.5)
        self.assertIn('Команда брокеру не отправлена',self.page.locator('#log').inner_text())

    def test_delete_pending_requires_confirmation(self):
        self.page.locator('[data-scenario=pending]').click(); self.page.locator('[data-delete=buy]').click()
        self.assertIn('buy',self.snapshot()['levels'])
        self.page.locator('#apply-preview').click(); self.assertNotIn('buy',self.snapshot()['levels'])
        self.assertIn('sell',self.snapshot()['levels'])

    def test_close_position_is_explicit_and_waits_for_command(self):
        self.page.locator('#close-position').click(); self.assertIn('entry',self.snapshot()['levels'])
        self.page.locator('#apply-preview').click(); self.assertEqual(self.snapshot()['scenario'],'waiting')
        self.assertFalse(self.snapshot()['levels']); self.assertIn('RECOVERY не пересчитан',self.page.locator('#log').inner_text())

    def test_crossed_pending_does_not_become_market(self):
        self.page.locator('[data-scenario=pending]').click(); self.page.locator('[data-level=buy]').click()
        self.page.locator('#new-price').fill('4130'); self.page.locator('#apply-preview').click()
        self.assertEqual(self.snapshot()['levels']['buy'],4141)
        self.assertIn('MARKET не создаётся',self.page.locator('#preview-error').inner_text())

    def test_pan_and_pinch_never_open_order_preview(self):
        self.page.evaluate('''() => {const s=document.querySelector('#chart'),r=s.getBoundingClientRect();
          const send=(type,id,x,y)=>s.dispatchEvent(new PointerEvent(type,{pointerId:id,clientX:r.x+x,clientY:r.y+y,bubbles:true}));
          s.setPointerCapture=()=>{};
          send('pointerdown',1,80,120);send('pointermove',1,150,170);send('pointerup',1,150,170);
          send('pointerdown',1,50,100);send('pointerdown',2,150,100);send('pointermove',2,250,100);
          send('pointerup',1,50,100);send('pointerup',2,250,100);
        }''')
        self.assertIsNone(self.snapshot()['preview']); self.assertGreater(self.snapshot()['zoom'],1)
        self.assertNotEqual(self.snapshot()['panX'],0)


    def test_three_finger_gesture_never_becomes_tap(self):
        self.page.evaluate('''() => {const s=document.querySelector('#chart'),r=s.getBoundingClientRect();s.setPointerCapture=()=>{};
        const send=(type,id,x)=>s.dispatchEvent(new PointerEvent(type,{pointerId:id,clientX:r.x+x,clientY:r.y+100,bubbles:true}));
        send('pointerdown',1,50);send('pointerdown',2,150);send('pointermove',2,250);
        send('pointerdown',3,100);send('pointerup',3,100);send('pointerup',1,50);send('pointerup',2,250);}''')
        self.assertIsNone(self.snapshot()['preview'])

    def test_tap_existing_side_shows_old_price_as_move(self):
        self.page.locator('[data-scenario=pending]').click()
        self.page.evaluate('''() => {const s=document.querySelector('#chart'),r=s.getBoundingClientRect();s.setPointerCapture=()=>{};
        for(const type of ['pointerdown','pointerup'])s.dispatchEvent(new PointerEvent(type,{pointerId:1,clientX:r.x+60,clientY:r.y+30,bubbles:true}));}''')
        preview=self.snapshot()['preview']
        self.assertEqual(preview['kind'],'move')
        self.assertEqual(preview['old'],4141)
        self.assertEqual(self.snapshot()['levels']['buy'],4141)
        self.assertIn('Перенести',self.page.locator('#preview-title').inner_text())

    def test_chart_tap_only_creates_preview(self):
        self.page.evaluate('''() => {const s=document.querySelector('#chart'),r=s.getBoundingClientRect();s.setPointerCapture=()=>{};
        for(const type of ['pointerdown','pointerup'])s.dispatchEvent(new PointerEvent(type,{pointerId:1,clientX:r.x+60,clientY:r.y+60,bubbles:true}));}''')
        self.assertEqual(self.snapshot()['preview']['kind'],'create'); self.assertNotIn('buy',self.snapshot()['levels'])

    def test_mock_tick_age_stale_and_disconnect_are_visible(self):
        before=self.snapshot()['ticks']; self.page.clock.fast_forward(1100)
        self.assertGreater(self.snapshot()['ticks'],before)
        self.page.locator('#toggle-stream').click(); tick=self.snapshot()['lastTick']; self.page.clock.fast_forward(4100)
        self.assertEqual(self.snapshot()['lastTick'],tick)
        self.assertEqual(self.page.locator('#freshness').get_attribute('data-status'),'stale')
        self.page.locator('[data-level=tp]').click();self.page.locator('#apply-preview').click()
        self.assertIn('Нет свежего',self.page.locator('#preview-error').inner_text())
        self.page.locator('#toggle-connection').click()
        self.assertEqual(self.page.locator('#freshness').get_attribute('data-status'),'disconnected')

    def test_separate_chart_tab_and_settings_preview(self):
        self.page.locator('[data-tab=overview]').click();self.assertFalse(self.page.locator('#market').is_visible())
        self.page.locator('[data-tab=chart]').click();self.assertTrue(self.page.locator('#market').is_visible())
        self.page.locator('#parts').fill('5');self.page.locator('#preview-settings').click()
        self.assertEqual(self.snapshot()['settings']['parts'],3)
        self.page.locator('#apply-settings').click();self.assertEqual(self.snapshot()['settings']['parts'],5)

    def test_desktop_layout_and_zoom_buttons(self):
        self.page.set_viewport_size({'width':1440,'height':1000})
        self.assertLessEqual(self.page.evaluate('document.documentElement.scrollWidth'),1440)
        self.page.locator('#zoom-in').click();self.assertGreater(self.snapshot()['zoom'],1)
        self.page.locator('#reset-chart').click();self.assertEqual(self.snapshot()['zoom'],1)
        self.page.screenshot(path='/workspace/scratch/local-panel-desktop.png',full_page=True)




class ConnectedMockUI(unittest.TestCase):
    setUpClass=classmethod(LocalPanelUI.setUpClass.__func__)
    tearDownClass=classmethod(LocalPanelUI.tearDownClass.__func__)

    def setUp(self):
        import sys
        sys.path[:0]=[str(ROOT.parents[1]),str(ROOT.parents[1]/'tests')]
        import test_tradfi_interactive as fixture
        from tradfi.local_panel import LocalPanel,MockTransport
        from tradfi.control import OwnerMailbox
        self.fixture=fixture.InteractiveTest()
        self.fixture.setUp();self.fixture.recovery()
        self.app=LocalPanel(self.fixture.bot);self.app.step()
        self.mailbox=OwnerMailbox();self.transport=MockTransport(self.mailbox)
        self.mailbox.drain(self.app.gateway,self.app.view)
        self.context=self.browser.new_context(viewport={'width':390,'height':844},is_mobile=True,has_touch=True)
        self.errors=[];self.blocked=[];self.posts=[];self.lose_reply=False;self.reply_500=False
        def route(request):
            from urllib.parse import urlsplit
            url=request.request.url;path=urlsplit(url).path
            names={'/':'index.html','/panel.js':'panel.js','/panel.css':'panel.css'}
            if url.startswith(ORIGIN) and path in names:
                p=ROOT/names[path]
                request.fulfill(body=p.read_bytes(),content_type={'.html':'text/html','.js':'text/javascript','.css':'text/css'}[p.suffix])
            elif url.startswith(ORIGIN) and path.startswith('/api/'):
                import json
                if path=='/api/state':
                    self.app.step();self.mailbox.drain(self.app.gateway,self.app.view)
                body=request.request.post_data_buffer or b''
                headers=dict(request.request.headers,Host='127.0.0.1:8765')
                code,data=self.transport.request(request.request.method,path,headers,body)
                if path=='/api/action':
                    self.posts.append(json.loads(body))
                    if self.lose_reply:request.abort();return
                    if self.reply_500:request.fulfill(status=502,body='{}',content_type='application/json');return
                request.fulfill(status=code,body=json.dumps(data),content_type='application/json')
            else:self.blocked.append(url);request.abort()
        self.context.route('**/*',route)
        self.page=self.context.new_page();self.page.on('pageerror',lambda e:self.errors.append(str(e)))
        self.page.goto(ORIGIN+'/?controller=mock')
        self.page.wait_for_function("window.localPanel?.snapshot().scenario === 'position'")
        self.page.wait_for_function("document.querySelector('.demo-banner').textContent.includes('CONTROLLER')")

    def tearDown(self):
        self.assertFalse(self.blocked);self.assertFalse(self.errors)
        self.context.close();self.fixture.doCleanups()

    def test_protection_preview_reaches_shared_controller_only_after_apply(self):
        p=self.fixture.broker.data['positions'][0]
        self.page.locator('[data-level=tp]').click();self.page.locator('#new-price').fill('4140.50')
        self.assertEqual(p['tp'],'4140.00')
        self.page.locator('#apply-preview').click()
        expect(self.page.locator('[data-level=tp]')).to_have_attribute('data-price','4140.50')
        self.assertEqual(float(p['tp']),4140.5);self.assertEqual(len(self.posts),1)

    def test_manual_close_confirms_actual_fake_debt_and_waits(self):
        self.fixture.broker.quote('4139.5','4139.6')
        self.page.locator('#close-position').click()
        self.assertTrue(self.fixture.broker.data['positions'])
        self.page.locator('#apply-preview').click()
        expect(self.page.locator('#phase-value')).to_contain_text('WAIT_TRIGGER')
        self.assertEqual(float(self.fixture.bot.state['cycle']['fixed_recovery']['debt']),45)
        self.assertEqual(self.fixture.bot.state['phase'],'WAIT_TRIGGER')

    def test_lost_reply_reconciles_receipt_without_second_post(self):
        self.lose_reply=True
        self.page.locator('[data-level=tp]').click();self.page.locator('#new-price').fill('4140.50')
        self.page.locator('#apply-preview').click()
        expect(self.page.locator('[data-level=tp]')).to_have_attribute('data-price','4140.50')
        self.assertEqual(len(self.posts),1)
        self.assertIn('Сверка команды',self.page.locator('#log').inner_text())

    def test_ambiguous_http_502_reconciles_without_repeating(self):
        self.reply_500=True
        self.page.locator('[data-level=tp]').click();self.page.locator('#new-price').fill('4140.50')
        self.page.locator('#apply-preview').click()
        expect(self.page.locator('[data-level=tp]')).to_have_attribute('data-price','4140.50')
        self.assertEqual(len(self.posts),1)
        self.assertIn('Сверка команды',self.page.locator('#log').inner_text())

    def test_n_settings_apply_recalculates_controller_tp(self):
        self.page.locator('#parts').fill('5');self.page.locator('#preview-settings').click()
        self.assertEqual(self.fixture.bot.state['cycle']['fixed_recovery']['parts'],3)
        self.page.locator('#apply-settings').click()
        expect(self.page.locator('[data-level=tp]')).to_have_attribute('data-price','4139.60')
        self.assertEqual(self.fixture.bot.state['cycle']['fixed_recovery']['parts'],5)
        self.assertEqual(float(self.fixture.broker.data['positions'][0]['sl']),4136)

    def test_backend_mobile_screenshot(self):
        self.page.screenshot(path='/workspace/scratch/local-panel-mock-controller-mobile.png',full_page=True)
        self.assertLessEqual(self.page.evaluate('document.documentElement.scrollWidth'),390)


if __name__=='__main__': unittest.main(verbosity=2)
