import importlib.util
import json
import os
from pathlib import Path
import re
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from test_computer_use import module, C, APP

B=module('browser_worker')


class Policy(unittest.TestCase):
    def test_private_addresses_and_credentials_and_file_urls_are_rejected(self):
        service=B.BrowserService()
        with patch.object(B.socket,'getaddrinfo',return_value=[(socket.AF_INET,socket.SOCK_STREAM,6,'',('127.0.0.1',80))]):
            for url in ('http://localhost','https://example.test'):
                with self.assertRaises(B.BrowserError):service.check_url(url)
        for url in ('file:///C:/secret','javascript:alert(1)','https://user:password@example.com','data:text/html,hello'):
            with self.assertRaises(B.BrowserError):service.check_url(url)

    def test_public_addresses_and_about_blank_are_allowed(self):
        service=B.BrowserService()
        service.check_url('about:blank')
        with patch.object(B.socket,'getaddrinfo',return_value=[(socket.AF_INET,socket.SOCK_STREAM,6,'',('93.184.216.34',443))]):
            service.check_url('https://example.com')
            service.check_url('wss://example.com/socket',websocket=True)

    def test_lan_opt_in_does_not_enable_non_http_navigation(self):
        service=B.BrowserService(allow_lan=True)
        service.check_url('http://127.0.0.1:12345')
        with self.assertRaises(B.BrowserError):service.check_url('file:///C:/secret')

    def test_diagnostic_urls_do_not_include_secrets(self):
        self.assertEqual(B.safe_url('https://user:secret@example.com/api?token=secret#fragment'),'https://example.com/api')


class FakeLocator:
    def __init__(self):self.value='';self.enabled=True;self.fail=False
    def aria_snapshot(self,**kw):return '- textbox "Name" [ref=e1]\n- button "Save" [ref=e2]'
    def wait_for(self,**kw):pass
    def is_enabled(self):return self.enabled
    def fill(self,value):
        if self.fail:raise RuntimeError('fill '+value+' failed')
        self.value=value
    def input_value(self):return self.value
    def click(self):pass


class FakePage:
    def __init__(self):self.origin=1;self.url='https://example.com';self.frames=[];self.loc=FakeLocator();self.closed=False
    def is_closed(self):return self.closed
    def locator(self,*args):return self.loc
    def get_by_ref(self,ref):return self.loc
    def evaluate(self,*args):return self.origin
    def title(self):return 'Fixture'
    def close(self):self.closed=True


class Snapshots(unittest.TestCase):
    def setUp(self):
        self.service=B.BrowserService();self.page=FakePage();self.service.pages['p']=self.page;self.service.active='p'
        self.snap=self.service.snapshot('p')

    def test_find_is_compact_and_ref_bound(self):
        r=self.service.find('p',self.snap['snapshot_id'],text='Save',role='button')
        self.assertEqual(r['matches'][0]['ref'],'e2');self.assertEqual(r['total_matches'],1)

    def test_navigation_and_new_snapshots_invalidate_old_refs(self):
        self.page.origin=2
        with self.assertRaises(B.BrowserError):self.service.action('p',self.snap['snapshot_id'],'e2')
        snap=self.service.snapshot('p');self.service.snapshot('p')
        with self.assertRaises(B.BrowserError):self.service.find('p',snap['snapshot_id'])

    def test_fill_verified_and_no_replay(self):
        r=self.service.action('p',self.snap['snapshot_id'],'e1','fill','hello')
        self.assertTrue(r['verification']['verified']);self.assertTrue(r['action_executed'])
        with self.assertRaises(B.BrowserError):self.service.action('p',self.snap['snapshot_id'],'e1','fill','replay')
        self.assertEqual(self.page.loc.value,'hello')

    def test_disabled_and_unknown_ref_are_not_invoked(self):
        self.page.loc.enabled=False
        with self.assertRaises(B.BrowserError) as e:self.service.action('p',self.snap['snapshot_id'],'e2')
        self.assertEqual(e.exception.code,'element_not_ready')
        with self.assertRaises(B.BrowserError):self.service.action('p',self.snap['snapshot_id'],'unknown')

    def test_failure_reports_unknown_without_echoing_value(self):
        self.page.loc.fail=True
        with self.assertRaises(B.BrowserError) as e:self.service.action('p',self.snap['snapshot_id'],'e1','fill','SECRET_INPUT')
        self.assertEqual(e.exception.details['outcome'],'unknown')
        self.assertNotIn('SECRET_INPUT',str(e.exception.details))
        self.assertNotIn('p',self.service.current)

    def test_diagnostics_pumps_queued_events_without_input(self):
        with patch.object(self.page,'evaluate',return_value=0) as evaluate:
            self.service.diagnostics()
        evaluate.assert_called_once_with('0')

    def test_trace_download_paths_and_unknown_ops(self):
        with self.assertRaises(B.BrowserError):self.service.trace('stop','relative.zip')
        with self.assertRaises(B.BrowserError):self.service.download_save('missing','relative')
        with self.assertRaises(B.BrowserError):self.service.dispatch('evaluate',{'code':'secret'})


class BrowserRegistration(unittest.TestCase):
    def load(self,browser=True,input_enabled=True,internet=True):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);root=Path(tmp.name)
        (root/'config.json').write_text(json.dumps({'workspace':str(root/'ws'),'secret':'z'*64,'enable_browser':browser,
            'enable_internet':internet,'enable_input':input_enabled}),encoding='utf-8')
        import sys,types,contextlib
        input_stub=patch.dict(sys.modules,{'pyautogui':types.SimpleNamespace()}) if os.name!='nt' else contextlib.nullcontext()
        with patch.dict(os.environ,{'PCBRIDGE_ROOT':str(root)}),input_stub:m=module('server')
        def cleanup():
            if hasattr(m,'_BROWSER'):m._BROWSER.close()
            m.log.removeHandler(m._log_handler);m._log_handler.close()
        self.addCleanup(cleanup)
        import anyio
        return m,{t.name for t in anyio.run(m.mcp.list_tools)}

    def test_enable_browser_and_internet_gates_tools(self):
        _,names=self.load(browser=False);self.assertNotIn('browser_open',names)
        _,names=self.load(internet=False);self.assertNotIn('browser_open',names)
        _,names=self.load(input_enabled=False)
        self.assertIn('browser_snapshot',names);self.assertNotIn('browser_action',names)

    def test_readonly_refuses_browser_writes_before_launching(self):
        import anyio,functools
        m,names=self.load();m.RO_FLAG.touch()
        self.assertIn('browser_action',names)
        for name,args in [('browser_open',{}),('browser_navigate',{'page_id':'bad','url':'https://example.com'}),
                          ('browser_action',{'page_id':'bad','snapshot_id':'bad','ref':'e1'}),('browser_trace',{})]:
            r=anyio.run(functools.partial(m.mcp.call_tool,name,args))
            self.assertTrue(r.isError,name);self.assertIsNone(m._BROWSER.proc)


HTML='''<!doctype html><title>PCBridge browser fixture</title>
<label>Name <input aria-label="Name" id="name"></label>
<label>Password <input aria-label="Password" type="password" value="BROWSER_TEST_SECRET"></label>
<button onclick="document.getElementById('status').textContent='Saved: '+document.getElementById('name').value">Save</button>
<p style="display:none">Ready</p><p id="status">Ready</p>
<button onclick="console.error('fixture console error');fetch('/missing')">Diagnostics</button>
<a href="/download" download="fixture.txt">Download fixture</a>
<a href="/next" target="_blank">New tab</a>
<iframe src="/frame" title="Fixture frame"></iframe>'''


@unittest.skipUnless(os.name=='nt' and os.environ.get('PCBRIDGE_BROWSER_TESTS')=='1','opt-in real Chromium browser test')
class NativeBrowser(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path=='/download':
                    self.send_response(200);self.send_header('Content-Type','application/octet-stream')
                    self.send_header('Content-Disposition','attachment; filename="fixture.txt"');self.end_headers();self.wfile.write(b'bridge-download');return
                if self.path=='/frame':body=b'<label>Frame value <input aria-label="Frame value"></label>'
                elif self.path=='/next':body=b'<title>Next fixture</title><h1>Next page</h1>'
                elif self.path=='/missing':self.send_response(404);self.end_headers();self.wfile.write(b'missing');return
                else:body=HTML.encode()
                self.send_response(200);self.send_header('Content-Type','text/html; charset=utf-8');self.end_headers();self.wfile.write(body)
            def log_message(self,*args):pass
        cls.http=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        cls.thread=threading.Thread(target=cls.http.serve_forever,daemon=True);cls.thread.start()
        cls.url='http://127.0.0.1:'+str(cls.http.server_port)
        cls.tmp=tempfile.TemporaryDirectory()
        cls.client=C.ComputerUseClient(APP/'server'/'browser_worker.py',Path(cls.tmp.name)/'worker.log',timeout=30,
                                      worker_args=['--allow-lan'],kill_tree=True)

    @classmethod
    def tearDownClass(cls):
        cls.client.close();cls.http.shutdown();cls.http.server_close();cls.tmp.cleanup()

    def setUp(self):
        self.snapshot=self.client.call('open',{'url':self.url,'headless':True},timeout=45)
        self.page_id=self.snapshot['page_id']

    def tearDown(self):
        try:self.client.call('close_page',{'page_id':self.page_id})
        except C.ComputerUseError:pass

    def ref(self,text,role):
        r=self.client.call('find',{'page_id':self.page_id,'snapshot_id':self.snapshot['snapshot_id'],'text':text,'role':role})
        self.assertEqual(r['total_matches'],1,r)
        return r['matches'][0]['ref']

    def act(self,text,role,action='click',value='',expect_text=''):
        ref=self.ref(text,role)
        r=self.client.call('action',{'page_id':self.page_id,'snapshot_id':self.snapshot['snapshot_id'],
                                    'ref':ref,'action':action,'value':value,'expect_text':expect_text})
        if 'snapshot' in r:self.snapshot=r['snapshot']
        return r

    def test_fill_click_iframe_masking_and_stale_refs(self):
        self.assertNotIn('BROWSER_TEST_SECRET',json.dumps(self.snapshot))
        observed=self.client.call('wait',{'page_id':self.page_id,'text':'Ready','timeout_seconds':1})
        self.assertTrue(observed['condition_met'])
        self.snapshot=observed['snapshot']
        old=self.snapshot['snapshot_id']
        r=self.act('Name','textbox','fill','Browser value')
        self.assertTrue(r['verification']['verified'],r)
        r=self.act('Save','button',expect_text='Saved: Browser value')
        self.assertTrue(r['verification']['verified'],r)
        with self.assertRaises(C.ComputerUseError):self.client.call('find',{'page_id':self.page_id,'snapshot_id':old})
        r=self.act('Frame value','textbox','fill','Inside frame')
        self.assertTrue(r['verification']['verified'],r)

    def test_console_network_download_and_trace(self):
        self.client.call('trace',{'action':'start'})
        self.act('Diagnostics','button')
        deadline=time.monotonic()+5
        while True:
            self.client.call('tabs')
            d=self.client.call('diagnostics',{'page_id':self.page_id})
            if any(e.get('status')==404 for e in d['events']):break
            if time.monotonic()>deadline:self.fail(d)
        self.assertTrue(any(e['kind']=='console' for e in d['events']))
        self.act('Download fixture','link')
        d=self.client.call('diagnostics',{'page_id':self.page_id})
        self.assertTrue(d['downloads'],d)
        destination=Path(self.tmp.name)/'download.txt'
        r=self.client.call('download_save',{'download_id':d['downloads'][-1]['download_id'],'path':str(destination)})
        self.assertEqual(destination.read_bytes(),b'bridge-download')
        trace=Path(self.tmp.name)/'trace.zip'
        self.client.call('trace',{'action':'stop','path':str(trace)})
        self.assertTrue(trace.is_file());self.assertGreater(trace.stat().st_size,0)

    def test_tabs_navigation_wait_and_close(self):
        self.act('New tab','link')
        tabs=self.client.call('tabs')['pages']
        extra=[p for p in tabs if p['page_id']!=self.page_id]
        self.assertTrue(extra,tabs)
        for p in extra:self.client.call('close_page',{'page_id':p['page_id']})
        self.client.call('navigate',{'page_id':self.page_id,'url':self.url+'/next'})
        r=self.client.call('wait',{'page_id':self.page_id,'text':'Next page','timeout_seconds':1})
        self.assertTrue(r['condition_met'])
        missing=self.client.call('wait',{'page_id':self.page_id,'text':'Never appears','timeout_seconds':.05})
        self.assertFalse(missing['condition_met'])


if __name__=='__main__':unittest.main()
