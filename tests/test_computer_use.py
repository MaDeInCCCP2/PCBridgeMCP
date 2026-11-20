"""Pure protocol/process tests plus opt-in native tests restricted to an owned WinForms window."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch

APP = Path(__file__).resolve().parent.parent


def module(name):
    spec = importlib.util.spec_from_file_location('pcb_test_' + name, APP / 'server' / (name + '.py'))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


W, C = module('uia_worker'), module('computer_use')


def node(name, role='Button', actions=None, **extra):
    return {'role':role, 'name':name, 'enabled':True, 'visible':True, 'password':False,
            'automation_id':'', 'bounds':[0,0,100,40], 'actions':actions or ['invoke'], **extra}


class FakeBackend:
    def __init__(self):
        self.nodes = {'root':node('Test', 'Window', []), 'edit':node('Input','Edit',['set_value'],value=''),
                      'button':node('Apply'), 'check':node('Flag','CheckBox',['toggle'],toggle_state=0)}
        self.runtimes = {k:(i,) for i,k in enumerate(self.nodes)}
        self.called = []
        self.generation = 1
        self.moved = False
        self.error_after = False
        self.error_action = False

    def foreground(self): return 42
    def windows(self): return [{'hwnd':42,'title':'Test','exe':'test.exe'}]
    def root(self, hwnd): return 'root'
    def identity(self, root): return (42,123,self.generation,(0,))
    def runtime_id(self, e): return self.runtimes[e]
    def children(self, e): return ['edit','button','check'] if e == 'root' else []
    def belongs(self, e, root): return not self.moved
    def inspect(self, e):
        if self.error_after: raise RuntimeError('provider unavailable')
        return copy.deepcopy(self.nodes[e])
    def act(self, e, action, value):
        self.called.append((e,action,value))
        if self.error_action: raise RuntimeError('provider blocked')
        if action == 'set_value': self.nodes[e]['value']=value
        elif action == 'toggle': self.nodes[e]['toggle_state']=1-self.nodes[e]['toggle_state']
        elif action == 'invoke': self.nodes['root']['name']='Saved'


class Protocol(unittest.TestCase):
    def setUp(self):
        self.b = FakeBackend()
        self.s = W.UIService(self.b)
        self.snap = self.s.snapshot()

    def ref(self, name):
        return next(n['ref'] for n in self.snap['elements'] if n['name']==name)

    def act(self, name='Apply', **kwargs):
        return self.s.action(self.snap['snapshot_id'], self.ref(name), **kwargs)

    def test_snapshot_and_filtered_find(self):
        r=self.s.find(self.snap['snapshot_id'],text='inp',role='Edit')
        self.assertEqual(len(r['matches']),1)
        self.assertNotIn('bounds',r['matches'][0])
        self.assertIn('set_value',r['matches'][0]['actions'])

    def test_new_snapshot_invalidates_old_refs(self):
        self.s.snapshot()
        with self.assertRaises(W.UIError) as e: self.act()
        self.assertEqual(e.exception.code,'stale_snapshot')
        self.assertEqual(self.b.called,[])

    def test_expired_snapshot(self):
        self.s.clock=lambda:self.s.current['created']+121
        with self.assertRaises(W.UIError): self.act()
        self.assertEqual(self.b.called,[])

    def test_identity_label_and_reparent_changes_are_rejected(self):
        for change in ('process','label','runtime','parent'):
            with self.subTest(change=change):
                self.setUp()
                if change=='process':self.b.generation+=1
                elif change=='label':self.b.nodes['button']['name']='Different'
                elif change=='runtime':self.b.runtimes['button']=(999,)
                else:self.b.moved=True
                with self.assertRaises(W.UIError):self.act()
                self.assertEqual(self.b.called,[])

    def test_disabled_or_invisible_is_not_acted_on(self):
        for flag in ('enabled','visible'):
            self.b.nodes['button'][flag]=False
            with self.assertRaises(W.UIError) as e:self.act()
            self.assertEqual(e.exception.code,'element_not_ready')
            self.b.nodes['button'][flag]=True
        self.assertEqual(self.b.called,[])

    def test_unsupported_action_never_uses_coordinate_fallback(self):
        with self.assertRaises(W.UIError):self.act(action='click')
        with self.assertRaises(W.UIError):self.act(action='set_value',value='x')
        self.assertEqual(self.b.called,[])

    def test_value_verified_and_snapshot_rotated(self):
        r=self.act('Input',action='set_value',value='hello')
        self.assertTrue(r['verification']['verified'])
        self.assertNotEqual(r['snapshot']['snapshot_id'],self.snap['snapshot_id'])
        with self.assertRaises(W.UIError):self.act('Input',action='set_value',value='replayed')
        self.assertEqual(self.b.nodes['edit']['value'],'hello')

    def test_toggle_and_expected_text_verification(self):
        r=self.act('Flag',action='toggle')
        self.assertTrue(r['verification']['verified'])
        self.snap=r['snapshot']
        r=self.act(expect_text='Saved',wait_seconds=0)
        self.assertTrue(r['verification']['verified'])

    def test_action_execution_is_separate_from_goal(self):
        r=self.act(expect_text='Never appears',wait_seconds=0)
        self.assertTrue(r['ok']);self.assertTrue(r['action_executed'])
        self.assertFalse(r['verification']['verified'])

    def test_provider_failure_invalidates_refs_and_reports_unknown(self):
        self.b.error_action=True
        with self.assertRaises(W.UIError) as e:self.act()
        self.assertEqual(e.exception.details['outcome'],'unknown')
        self.assertFalse(e.exception.details['retry_action'])
        with self.assertRaises(W.UIError) as e:self.act()
        self.assertEqual(e.exception.code,'stale_snapshot')
        self.assertEqual(len(self.b.called),1)

    def test_window_replaced_after_action_is_not_observed_as_original(self):
        original=self.b.act
        def act(*args):
            original(*args);self.b.generation+=1
        self.b.act=act
        r=self.act()
        self.assertTrue(r['action_executed'])
        self.assertIn('observation_error',r)
        self.assertIsNone(self.s.current)

    def test_observation_failure_does_not_replay_executed_action(self):
        original=self.b.act
        def act(*args):
            original(*args);self.b.error_after=True
        self.b.act=act
        r=self.act()
        self.assertTrue(r['action_executed']);self.assertIn('observation_error',r)
        with self.assertRaises(W.UIError):self.act()
        self.assertEqual(len(self.b.called),1)

    def test_bounds_and_capture_limits(self):
        r=self.s.snapshot(max_nodes=2,include_bounds=True)
        self.assertEqual(len(r['elements']),2);self.assertTrue(r['truncated'])
        self.assertIn('bounds',r['elements'][0])
        r=self.s.snapshot(max_depth=0)
        self.assertEqual(len(r['elements']),1);self.assertTrue(r['truncated'])

    def test_ambiguous_window_never_chooses_arbitrary_target(self):
        self.b.windows=lambda:[{'hwnd':42,'title':'Test','exe':'test.exe'},{'hwnd':99,'title':'Test','exe':'test.exe'}]
        with self.assertRaises(W.UIError) as e:self.s.snapshot('Test')
        self.assertEqual(e.exception.code,'ambiguous_window')

    def test_wait_present_and_absent_with_incomplete_tree(self):
        self.assertTrue(self.s.wait(text='Input',timeout_seconds=0)['condition_met'])
        self.assertTrue(self.s.wait(text='missing',present=False,timeout_seconds=0)['condition_met'])
        r=self.s.wait(text='missing',present=False,timeout_seconds=0,max_nodes=1)
        self.assertFalse(r['condition_met'])

    def test_password_subtree_is_not_traversed(self):
        self.b.nodes['root']=node('[password]','Edit',['set_value'],password=True)
        r=self.s.snapshot()
        self.assertEqual(len(r['elements']),1)

    def test_unknown_operation_and_invalid_ref(self):
        with self.assertRaises(W.UIError):self.s.dispatch('exec',{})
        with self.assertRaises(W.UIError):self.s.action(self.snap['snapshot_id'],'nope')


class IsolatedWorker(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.client=C.ComputerUseClient(Path(__file__).parent/'fixtures'/'uia_fake_worker.py',Path(self.tmp.name)/'worker.log',timeout=.5)
        self.addCleanup(self.client.close)

    def test_repeated_rpc_and_structured_error(self):
        self.assertTrue(self.client.call('snapshot')['ok'])
        pid=self.client.proc.pid
        self.assertTrue(self.client.call('find')['ok']);self.assertEqual(self.client.proc.pid,pid)
        with self.assertRaises(C.ComputerUseError) as e:self.client.call('error')
        self.assertEqual(e.exception.code,'stale_snapshot')

    def test_timeout_kills_only_worker_and_next_observation_recovers(self):
        self.client.call('snapshot');old=self.client.proc
        start=time.monotonic()
        with self.assertRaises(C.ComputerUseError) as e:self.client.call('slow')
        self.assertEqual(e.exception.code,'ui_timeout');self.assertIsNotNone(old.poll())
        self.assertLess(time.monotonic()-start,5)
        self.assertTrue(self.client.call('snapshot')['ok'])

    def test_lost_action_outcome_is_unknown_without_replay(self):
        self.client.call('snapshot')
        with patch.object(self.client.messages,'get',side_effect=__import__('queue').Empty):
            with self.assertRaises(C.ComputerUseError) as e:self.client.call('action')
        self.assertEqual(e.exception.details['outcome'],'unknown');self.assertIsNone(self.client.proc)

    def test_crash_or_bad_reply_is_detected(self):
        for op in ('crash','bad'):
            with self.assertRaises(C.ComputerUseError):self.client.call(op)
            self.assertIsNone(self.client.proc)


@unittest.skipUnless(os.name=='nt', 'Windows-only tool registration')
class ToolRegistration(unittest.TestCase):
    def load(self, screen=True, input_enabled=True):
        import anyio
        tmp=tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root=Path(tmp.name)
        (root/'config.json').write_text(json.dumps({'workspace':str(root/'ws'),'secret':'x'*64,
            'enable_screen':screen,'enable_input':input_enabled}),encoding='utf-8')
        with patch.dict(os.environ,{'PCBRIDGE_ROOT':str(root)}):
            result=module('server')
        def close_log():
            result.log.removeHandler(result._log_handler)
            result._log_handler.close()
        self.addCleanup(close_log)
        if hasattr(result,'_UI'):self.addCleanup(result._UI.close)
        return result, {t.name for t in anyio.run(result.mcp.list_tools)}

    def test_observation_and_input_flags(self):
        _, names=self.load(input_enabled=False)
        self.assertTrue({'ui_snapshot','ui_find','ui_wait'}<=names)
        self.assertNotIn('ui_action',names)
        _, names=self.load(screen=False,input_enabled=False)
        self.assertNotIn('ui_snapshot',names)

    def test_readonly_and_pause_reject_before_starting_worker(self):
        import anyio
        m, names=self.load()
        self.assertIn('ui_action',names)
        m.RO_FLAG.touch()
        r=anyio.run(__import__('functools').partial(m.mcp.call_tool,'ui_action',{'snapshot_id':'bad','ref':'e1'}))
        self.assertTrue(r.isError);self.assertIsNone(m._UI.proc)
        m.PAUSE_FLAG.touch()
        r=anyio.run(__import__('functools').partial(m.mcp.call_tool,'ui_snapshot',{}))
        self.assertTrue(r.isError);self.assertIsNone(m._UI.proc)


@unittest.skipUnless(os.name=='nt' and os.environ.get('PCBRIDGE_UI_TESTS')=='1', 'opt-in native Windows UI test')
class NativeWindows(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory()
        cls.title='PCBridge UI Test '+uuid.uuid4().hex[:8]
        fixture=Path(__file__).parent/'fixtures'/'uia_form.ps1'
        cls.process=subprocess.Popen(['powershell.exe','-NoProfile','-STA','-File',str(fixture),'-Title',cls.title],
            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        cls.client=C.ComputerUseClient(APP/'server'/'uia_worker.py',Path(cls.tmp.name)/'uia.log',timeout=15)
        try:
            deadline=time.monotonic()+20
            while True:
                try:
                    cls.initial=cls.client.call('snapshot',{'window':cls.title})
                    break
                except C.ComputerUseError:
                    if time.monotonic()>deadline:raise
                    time.sleep(.2)
        except BaseException:
            cls.tearDownClass();raise

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        cls.process.terminate()
        try:cls.process.wait(timeout=5)
        except subprocess.TimeoutExpired:cls.process.kill();cls.process.wait(timeout=5)
        cls.tmp.cleanup()

    def test_owned_window_fill_invoke_toggle_wait_and_password_mask(self):
        r=self.client.call('snapshot',{'window':self.title})
        self.assertNotIn('DO_NOT_EXPOSE_UIA_TEST_SECRET',json.dumps(r))
        edit=next(n for n in r['elements'] if n['role']=='Edit' and n['name']=='PCBridge input')
        filled=self.client.call('action',{'snapshot_id':r['snapshot_id'],'ref':edit['ref'],
                                         'action':'set_value','value':'Bridge value'})
        self.assertTrue(filled['verification']['verified'],filled)
        r=filled['snapshot'];button=next(n for n in r['elements'] if n['name']=='PCBridge apply')
        clicked=self.client.call('action',{'snapshot_id':r['snapshot_id'],'ref':button['ref'],
                                          'action':'invoke','expect_text':'Saved: Bridge value','wait_seconds':3})
        self.assertTrue(clicked['verification']['verified'],clicked)
        r=clicked['snapshot'];check=next(n for n in r['elements'] if n['name']=='PCBridge flag')
        toggled=self.client.call('action',{'snapshot_id':r['snapshot_id'],'ref':check['ref'],'action':'toggle'})
        self.assertTrue(toggled['verification']['verified'],toggled)
        waited=self.client.call('wait',{'window':self.title,'text':'Saved: Bridge value','timeout_seconds':0})
        self.assertTrue(waited['condition_met'])
        with self.assertRaises(C.ComputerUseError):
            self.client.call('action',{'snapshot_id':r['snapshot_id'],'ref':check['ref'],'action':'toggle'})


if __name__=='__main__':unittest.main()
