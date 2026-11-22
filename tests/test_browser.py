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


class BrowserSessionCleanup(unittest.TestCase):
    def test_context_creation_failure_releases_idle_shared_browser(self):
        from unittest.mock import Mock

        driver = Mock()
        chromium = driver.chromium.launch.return_value
        chromium.new_context.side_effect = RuntimeError("failed to create context")
        sessions = B.BrowserContexts(factory=lambda: driver)

        with self.assertRaises(B.BrowserError) as err:
            sessions.dispatch("session_open", {"headless": True})
        self.assertEqual(err.exception.code, "browser_start_failed")
        chromium.close.assert_called_once()
        self.assertEqual(sessions.browsers, {})
        self.assertEqual(sessions.dispatch("session_inventory", {})["sessions"], [])

    def test_page_creation_failure_releases_context_without_closing_other_owner(self):
        from unittest.mock import Mock

        driver = Mock()
        chromium = driver.chromium.launch.return_value
        first, second = Mock(), Mock()
        first.new_page.side_effect = RuntimeError("failed to create page")
        chromium.new_context.return_value = first
        sessions = B.BrowserContexts(factory=lambda: driver)

        # An existing owner already has a live context in the shared browser.
        retained = B.BrowserService(shared_browser=sessions.browser)
        retained.browser = sessions.browser(True)
        retained.context = second
        retained.headless = True
        sessions.sessions[("other-owner", "existing")] = retained

        with self.assertRaises(B.BrowserError) as err:
            sessions.dispatch("session_open", {"headless": True, "_owner": "new-owner"})
        self.assertEqual(err.exception.code, "browser_page_start_failed")
        self.assertEqual(err.exception.details["_live_sessions"], [("other-owner", "existing")])
        first.close.assert_called_once()
        second.close.assert_not_called()
        chromium.close.assert_not_called()

        sessions.dispatch("session_close", {"_owner": "other-owner", "session_id": "existing"})
        second.close.assert_called_once()
        chromium.close.assert_called_once()
        self.assertEqual(sessions.browsers, {})


class BrowserRegistration(unittest.TestCase):
    def load(self,browser=True,input_enabled=True,internet=True):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);root=Path(tmp.name)
        (root/'config.json').write_text(json.dumps({'workspace':str(root/'ws'),'secret':'z'*64,'enable_browser':browser,
            'enable_internet':internet,'enable_input':input_enabled}),encoding='utf-8')
        import sys,types,contextlib
        from mcp.server.request_state import RequestStateSecurity
        RequestStateSecurity.ephemeral()
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
        self.assertIn('browser_operation', names)
        self.assertNotIn('browser_action_once', names)

    def test_unified_session_inventory_uses_existing_workers_only(self):
        import anyio

        m, names = self.load()
        self.assertIn("computer_sessions", names)
        self.assertEqual(anyio.run(m.computer_sessions), {"sessions": [], "backends": {"browser": "ok"}})
        self.assertIsNone(m._BROWSER.proc)
        first = [{"session_id": "browser-one", "mode": "browser"}]
        second = [{"session_id": "desktop-one", "mode": "win32_desktop"}]

        from unittest.mock import Mock
        m._DESKTOPS = Mock()
        m._DESKTOPS.list.return_value = {"sessions": second}
        token = m._principal.set("client-a")
        try:
            with patch.object(m._BROWSER_REQUESTS, "call", return_value={"sessions": first}) as inventory:
                result = anyio.run(m.computer_sessions)
            inventory.assert_called_once_with("client-a", "sessions", start=False)
            m._DESKTOPS.list.assert_called_once_with("client-a")
        finally:
            m._principal.reset(token)
        self.assertEqual(result["sessions"], first + second)

    def test_readonly_refuses_browser_writes_before_launching(self):
        import anyio,functools
        m,names=self.load();m.RO_FLAG.touch()
        self.assertIn('browser_action',names)
        for name,args in [('browser_open',{}),('browser_navigate',{'page_id':'bad','url':'https://example.com'}),
                          ('browser_action',{'page_id':'bad','snapshot_id':'bad','ref':'e1'}),('browser_trace',{}),
                          ('browser_action_once', {'session_id':'s','page_id':'p','snapshot_id':'t','ref':'e1','operation_id':'op'})]:
            r=anyio.run(functools.partial(m.mcp.call_tool,name,args))
            self.assertTrue(r.is_error,name);self.assertIsNone(m._BROWSER.proc)


HTML='''<!doctype html><title>PCBridge browser fixture</title>
<label>Name <input aria-label="Name" id="name"></label>
<label>Password <input aria-label="Password" type="password" value="BROWSER_TEST_SECRET"></label>
<button onclick="document.getElementById('status').textContent='Saved: '+document.getElementById('name').value">Save</button>
<p style="display:none">Ready</p><p id="status">Ready</p>
<button onclick="document.getElementById('counter').textContent='Count is '+(++window.clickCount)">Increment</button>
<p id="counter">Count is 0</p><script>window.clickCount=0</script>
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
                if self.path in ("/storage", "/storage-set"):
                    body = b'<p id="state"></p><script>document.querySelector("#state").textContent=document.cookie || "empty session"</script>'
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    if self.path == "/storage-set":
                        self.send_header("Set-Cookie", "pcbridge-fixture=private; Path=/")
                    self.end_headers()
                    self.wfile.write(body)
                    return
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

    def test_explicit_session_action_receipt_prevents_second_chromium_click(self):
        from browser_requests import BrowserRequests
        from operation_receipts import OperationReceiptStore

        path = Path(self.tmp.name) / "browser-receipts.sqlite3"
        store = OperationReceiptStore(path, b"browser-fixture-key", "browser", scope_limit=512)
        requests = BrowserRequests(self.client, receipt_store=store)
        snapshot = requests.call("receipt-test", "session_open", {"url": self.url, "headless": True})
        sid, page_id = snapshot["session_id"], snapshot["page_id"]
        try:
            match = requests.call("receipt-test", "find", {"page_id": page_id, "snapshot_id": snapshot["snapshot_id"],
                                                          "text": "Increment", "role": "button"})["matches"][0]
            arguments = {"page_id": page_id, "snapshot_id": snapshot["snapshot_id"], "ref": match["ref"],
                         "expect_text": "Count is 1"}
            first = requests.act_once("receipt-test", sid, "increment", arguments)
            self.assertEqual(first["operation"]["outcome"], "verified", first)
            self.assertTrue(requests.act_once("receipt-test", sid, "increment", arguments)["replayed"])
            after = requests.call("receipt-test", "snapshot", {"page_id": page_id})
            self.assertIn("Count is 1", after["aria"])
            self.assertNotIn("Count is 2", after["aria"])
        finally:
            requests.call("receipt-test", "session_close", {"session_id": sid})
        client = C.ComputerUseClient(APP / "server" / "browser_worker.py", Path(self.tmp.name) / "receipt.log",
                                     close_at_exit=False)
        restarted = BrowserRequests(client, receipt_store=OperationReceiptStore(
            path, b"browser-fixture-key", "browser", scope_limit=512))
        replay = restarted.act_once("receipt-test", sid, "increment", arguments)
        self.assertEqual(replay["operation"], first["operation"])
        self.assertTrue(replay["replayed"])
        self.assertIsNone(client.proc)

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

    def test_session_storage_and_page_handles_are_isolated(self):
        first = self.client.call("navigate", {"page_id": self.page_id, "url": self.url + "/storage-set"})
        self.assertIn("pcbridge-fixture=private", first["aria"])
        other = self.client.call("session_open", {"url": self.url + "/storage", "headless": True})
        try:
            self.assertIn("empty session", other["aria"])
            self.assertNotIn("pcbridge-fixture", other["aria"])
            self.assertEqual(other["desktop_interference"], "no_physical_input")
            self.assertEqual(self.client.call("sessions", {"_owner": "second"})["sessions"], [])
            with self.assertRaises(C.ComputerUseError):
                self.client.call("snapshot", {"page_id": other["page_id"], "_owner": "second"})
            with self.assertRaises(C.ComputerUseError):
                self.client.call("session_close", {"session_id": other["session_id"], "_owner": "second"})
        finally:
            self.client.call("session_close", {"session_id": other["session_id"]})
        self.assertIn(self.page_id, {p["page_id"] for p in self.client.call("tabs")["pages"]})

    def test_inventory_and_close_preserve_the_other_owner(self):
        other = self.client.call("session_open", {"headless": True, "_owner": "second", "url": self.url})
        try:
            sessions = self.client.call("session_inventory", {})["sessions"]
            self.assertEqual({item["owner"] for item in sessions}, {"owner", "second"})
            entry = next(item for item in sessions if item["owner"] == "second")
            self.assertEqual((entry["pages"], entry["snapshots"]), (1, 1))
            self.assertNotIn("url", entry)
            self.assertNotIn("snapshot_id", entry)
        finally:
            self.client.call("session_close", {"session_id": other["session_id"], "_owner": "second"})
        self.assertEqual(self.client.call("sessions", {"_owner": "second"})["sessions"], [])
        self.assertIn(self.page_id, {p["page_id"] for p in self.client.call("tabs")["pages"]})

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
