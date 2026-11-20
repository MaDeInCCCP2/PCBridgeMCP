import unittest
from test_computer_use import W, FakeBackend, NativeWindows, node


class Improvements(unittest.TestCase):
    def setUp(self):
        self.backend=FakeBackend();self.service=W.UIService(self.backend)

    def test_live_find_without_snapshot_and_exact_automation_id(self):
        self.backend.nodes['edit']['automation_id']='input'
        r=self.service.find_live(text='Input',role='Edit',automation_id='input',exact=True)
        self.assertEqual(r['total_matches'],1)
        self.assertFalse(r['search_truncated'])
        act=self.service.action(r['snapshot_id'],r['matches'][0]['ref'],'set_value','new')
        self.assertTrue(act['verification']['verified'])

    def test_live_search_continues_beyond_output_limit(self):
        for i in range(50):
            key='f'+str(i);self.backend.nodes[key]=node('Filler');self.backend.runtimes[key]=(100+i,)
        self.backend.children=lambda e:['f'+str(i) for i in range(50)]+['button'] if e=='root' else []
        small=self.service.snapshot(max_nodes=2)
        self.assertTrue(small['truncated'])
        found=self.service.find_live(text='Apply',max_scan=100)
        self.assertEqual(found['total_matches'],1)
        self.assertGreater(found['scanned'],50)

    def test_live_search_marks_unsearched_subtrees(self):
        r=self.service.find_live(text='missing',max_scan=1)
        self.assertTrue(r['search_truncated']);self.assertEqual(r['total_matches'],0)

    def test_diff_uses_runtime_identity_and_ref_map(self):
        old=self.service.snapshot()
        self.backend.nodes['edit']['value']='changed'
        new=self.service.snapshot_diff(old['snapshot_id'])
        self.assertEqual(len(new['changed']),1)
        self.assertEqual(new['changed'][0]['element']['value'],'changed')
        self.assertEqual(len(new['ref_map']),4)
        self.assertFalse(new['partial'])
        with self.assertRaises(W.UIError):self.service.find(old['snapshot_id'])

    def test_diff_identifies_addition_and_removal(self):
        old=self.service.snapshot()
        self.backend.nodes['new']=node('New');self.backend.runtimes['new']=(500,)
        self.backend.children=lambda e:['edit','new','check'] if e=='root' else []
        diff=self.service.snapshot_diff(old['snapshot_id'])
        self.assertEqual(diff['added'][0]['name'],'New')
        self.assertEqual(diff['removed'][0]['name'],'Apply')

    def test_filtered_capture_diff_is_partial(self):
        old=self.service.find_live(text='Input')
        diff=self.service.snapshot_diff(old['snapshot_id'])
        self.assertTrue(diff['partial'])

    def test_scenario_refinds_and_verifies_each_step(self):
        r=self.service.run_steps('',[
            {'selector':{'text':'Input','role':'Edit','exact':True},'action':'set_value','value':'hello'},
            {'selector':{'text':'Apply','role':'Button','exact':True},'expect_text':'Saved'},
            {'type':'assert','text':'Saved'}])
        self.assertTrue(r['ok']);self.assertEqual(r['completed_steps'],3)
        self.assertEqual(self.backend.nodes['edit']['value'],'hello')
        self.assertEqual(len(self.backend.called),2)

    def test_full_scenario_validation_precedes_side_effects(self):
        with self.assertRaises(W.UIError):
            self.service.run_steps('',[{'selector':{'text':'Apply'}},{'selector':{'text':123}}])
        self.assertEqual(self.backend.called,[])
        with self.assertRaises(W.UIError):
            self.service.run_steps('',[{'selector':{'text':'Apply'}},{'type':'assert','text':123}])
        self.assertEqual(self.backend.called,[])

    def test_failed_value_verification_stops_next_action(self):
        self.backend.act=lambda *args:self.backend.called.append(args)
        result=self.service.run_steps('',[{'selector':{'text':'Input'},'action':'set_value','value':'not accepted'},
                                          {'selector':{'text':'Apply'}}])
        self.assertFalse(result['ok']);self.assertEqual(result['failed_step'],0)
        self.assertEqual(len(self.backend.called),1)

    def test_scenario_stops_after_failed_expectation_without_replay(self):
        r=self.service.run_steps('',[{'selector':{'text':'Apply'},'expect_text':'Impossible','wait_seconds':0},
                                     {'selector':{'text':'Flag'},'action':'toggle'}])
        self.assertFalse(r['ok']);self.assertEqual(r['failed_step'],0)
        self.assertTrue(r['steps'][0]['executed']);self.assertEqual(len(self.backend.called),1)

    def test_scenario_refuses_ambiguous_matches(self):
        r=self.service.run_steps('',[{'selector':{'role':'Button'}}])
        self.assertTrue(r['ok'])
        self.backend.nodes['edit']['role']='Button'
        r=self.service.run_steps('',[{'selector':{'role':'Button'}}])
        self.assertFalse(r['ok']);self.assertEqual(r['error_code'],'ambiguous_element')

    def test_modal_interrupts_scenario(self):
        self.backend.blocking_dialog=lambda root:{'window':'hwnd:99'}
        r=self.service.run_steps('',[{'selector':{'text':'Apply'}}])
        self.assertFalse(r['ok']);self.assertEqual(r['error_code'],'unexpected_window')
        self.assertEqual(self.backend.called,[])

    def test_error_diagnostics_do_not_retain_input_values(self):
        snap=self.service.dispatch('snapshot',{})
        ref=next(n['ref'] for n in snap['elements'] if n['name']=='Apply')
        with self.assertRaises(W.UIError):
            self.service.dispatch('action',{'snapshot_id':snap['snapshot_id'],'ref':ref,'action':'set_value','value':'SECRET_VALUE'})
        d=self.service.dispatch('diagnose',{'snapshot_id':snap['snapshot_id'],'ref':ref})
        self.assertTrue(d['snapshot_valid']);self.assertEqual(d['last_error']['code'],'unsupported_pattern')
        self.assertNotIn('SECRET_VALUE',str(d));self.assertIn('supported',d['hint'])

    def test_timeout_before_any_action(self):
        r=self.service.run_steps('',[{'selector':{'text':'Apply'}}],timeout_seconds=0)
        self.assertFalse(r['ok']);self.assertEqual(self.backend.called,[])


class NativeImprovements(NativeWindows):
    def test_live_find_diff_scenario_and_diagnostics_on_owned_window(self):
        r=self.client.call('find_live',{'window':self.title,'text':'PCBridge input','role':'Edit','exact':True})
        self.assertEqual(r['total_matches'],1)
        d=self.client.call('snapshot_diff',{'snapshot_id':r['snapshot_id']})
        self.assertTrue(d['partial'])
        result=self.client.call('run_steps',{'window':self.title,'steps':[
            {'selector':{'text':'PCBridge input','role':'Edit','exact':True},'action':'set_value','value':'Scenario value'},
            {'selector':{'text':'PCBridge apply','role':'Button','exact':True},'expect_text':'Saved: Scenario value'},
            {'type':'assert','text':'Saved: Scenario value'}]})
        self.assertTrue(result['ok'],result);self.assertEqual(result['completed_steps'],3)
        d=self.client.call('diagnose',{})
        self.assertTrue(d['recent_operations']);self.assertNotIn('Scenario value',str(d['recent_operations']))
