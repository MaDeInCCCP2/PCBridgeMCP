"""Regression tests for edit transactions, restart recovery and incremental logs."""
import base64
import functools
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch

from test_server import S, WORK, call


class Transactions(unittest.TestCase):
    def setUp(self):
        self.a, self.b = (WORK / 'batch-a.txt').resolve(), (WORK / 'batch-b.txt').resolve()
        self.a.write_bytes(b'\xef\xbb\xbfalpha\r\nbeta\r\n')
        self.b.write_bytes(b'one\ntwo\n')

    def preview(self):
        return call(S.preview_file_edits, files=[
            {'path': str(self.a), 'edits': [{'old_string': 'beta', 'new_string': 'BETA\nmore'}]},
            {'path': str(self.b), 'edits': [{'old_string': 'two', 'new_string': 'TWO'}]}])

    def test_preview_apply_and_group_undo(self):
        before = [f.read_bytes() for f in (self.a, self.b)]
        p = self.preview()
        self.assertEqual([f.read_bytes() for f in (self.a, self.b)], before)
        self.assertIn('+TWO', p['files'][1]['diff'])
        r = call(S.apply_file_edits, plan_id=p['plan_id'])
        self.assertEqual(self.a.read_bytes(), b'\xef\xbb\xbfalpha\r\nBETA\r\nmore\r\n')
        self.assertEqual(self.b.read_bytes(), b'one\nTWO\n')
        self.assertTrue(call(S.apply_file_edits, plan_id=p['plan_id'])['replayed'])
        call(S.undo_change, change_id=r['change_id'])
        self.assertEqual([f.read_bytes() for f in (self.a, self.b)], before)

    def test_validation_failure_changes_nothing(self):
        with self.assertRaises(ValueError):
            call(S.preview_file_edits, files=[
                {'path': str(self.a), 'edits': [{'old_string': 'beta', 'new_string': 'new'}]},
                {'path': str(self.b), 'edits': [{'old_string': 'missing', 'new_string': 'new'}]}])
        self.assertIn(b'beta', self.a.read_bytes())

    def test_stale_preview_is_rejected_before_first_write(self):
        p = self.preview()
        self.b.write_bytes(b'external')
        with self.assertRaises(S.BridgeError) as e:
            call(S.apply_file_edits, plan_id=p['plan_id'])
        self.assertEqual(e.exception.code, 'file_conflict')
        self.assertIn(b'beta', self.a.read_bytes())
        self.assertEqual(self.b.read_bytes(), b'external')

    def test_runtime_failure_restores_earlier_files(self):
        p = self.preview()
        original = self.a.read_bytes()
        write = S.atomic_write
        def fail_second(path, data):
            if path == self.b and data == b'one\nTWO\n':
                raise OSError('injected disk failure')
            return write(path, data)
        with patch.object(S, 'atomic_write', side_effect=fail_second):
            with self.assertRaises(S.BridgeError) as e:
                call(S.apply_file_edits, plan_id=p['plan_id'])
        self.assertEqual(e.exception.details['state'], 'rolled_back')
        self.assertEqual(self.a.read_bytes(), original)
        self.assertEqual(self.b.read_bytes(), b'one\ntwo\n')

    def test_failed_rollback_preserves_external_changes(self):
        p = self.preview()
        write = S.atomic_write
        def fail_second(path, data):
            if path == self.b:
                self.a.write_bytes(b'concurrent external edit')
                raise OSError('injected')
            return write(path, data)
        with patch.object(S, 'atomic_write', side_effect=fail_second):
            with self.assertRaises(S.BridgeError) as e:
                call(S.apply_file_edits, plan_id=p['plan_id'])
        self.assertEqual(e.exception.details['state'], 'recovery_required')
        self.assertEqual(self.a.read_bytes(), b'concurrent external edit')

    def test_recover_interrupted_journal(self):
        p = self.preview()
        folder, plan = S._tx_load(p['plan_id'])
        original = self.a.read_bytes()
        plan['state'] = 'applying'
        plan['files'][0]['step'] = 'writing'
        self.a.write_bytes((folder / '0.new').read_bytes())
        S._tx_save(folder, plan)
        r = call(S.recover_file_edits, plan_id=p['plan_id'])
        self.assertEqual(r['state'], 'rolled_back')
        self.assertEqual(self.a.read_bytes(), original)

    def test_undo_detects_external_edit(self):
        p = self.preview()
        r = call(S.apply_file_edits, plan_id=p['plan_id'])
        self.b.write_bytes(b'external')
        with self.assertRaises(S.BridgeError):
            call(S.undo_change, change_id=r['change_id'])
        self.assertIn(b'BETA', self.a.read_bytes())

    def test_guarded_single_write_and_edit(self):
        version = call(S.file_info, path=str(self.b))['sha256']
        call(S.edit_file, path=str(self.b), old_string='two', new_string='TWO', expected_sha256=version)
        for fn, kw in [(S.edit_file, dict(old_string='one', new_string='ONE')),
                       (S.edit_file_multi, dict(edits=[{'old_string':'one','new_string':'ONE'}])),
                       (S.write_text_file, dict(content='bad'))]:
            with self.assertRaises(S.BridgeError):
                call(fn, path=str(self.b), expected_sha256=version, **kw)
        self.assertEqual(self.b.read_bytes(), b'one\nTWO\n')

    def test_concurrent_guarded_writers_have_one_winner(self):
        version = call(S.file_info, path=str(self.b))['sha256']
        results = []
        def writer(value):
            try:
                call(S.write_text_file, path=str(self.b), content=value, expected_sha256=version)
                results.append('ok')
            except S.BridgeError:
                results.append('conflict')
        threads = [threading.Thread(target=writer, args=(str(i),)) for i in range(2)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertCountEqual(results, ['ok', 'conflict'])

    def test_atomic_replace_failure_never_truncates_original(self):
        with patch.object(S.os, 'replace', side_effect=PermissionError('locked')):
            with self.assertRaises(PermissionError):
                S.atomic_write(self.b, b'bad')
        self.assertEqual(self.b.read_bytes(), b'one\ntwo\n')

    def test_duplicate_and_corrupt_plan(self):
        item = {'path': str(self.b), 'edits':[{'old_string':'two','new_string':'TWO'}]}
        with self.assertRaises(S.BridgeError):
            call(S.preview_file_edits, files=[item, item])
        p = self.preview()
        folder, _ = S._tx_load(p['plan_id'])
        (folder/'1.new').write_bytes(b'corrupt')
        with self.assertRaises(S.BridgeError):
            call(S.apply_file_edits, plan_id=p['plan_id'])
        self.assertIn(b'beta', self.a.read_bytes())

    def test_readonly_preview_allowed_apply_denied(self):
        S.RO_FLAG.touch()
        try:
            p = self.preview()
            with self.assertRaises(ValueError):
                call(S.apply_file_edits, plan_id=p['plan_id'])
        finally:
            S.RO_FLAG.unlink(missing_ok=True)


class PersistentJobs(unittest.TestCase):
    def start(self, code):
        r = S.start_job([sys.executable, '-u', '-c', code], str(WORK), 'regression')
        self.addCleanup(self.stop, r['job_id'])
        return r['job_id']

    def stop(self, jid):
        call(S.job_stop, job_id=jid)

    def finish(self, jid):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            r = call(S.job_status, job_id=jid)
            if r['status'] in ('completed','failed'): return r
            time.sleep(.03)
        self.fail('job did not finish')

    def test_exit_code_and_completed_recovery(self):
        jid = self.start("print('hello'); raise SystemExit(7)")
        r = self.finish(jid)
        self.assertEqual(r['exit_code'], 7)
        S._load_jobs()
        recovered = call(S.job_status, job_id=jid)
        self.assertTrue(recovered['recovered'])
        self.assertEqual(recovered['exit_code'], 7)
        self.assertEqual(recovered['status'], 'failed')

    def test_live_job_recovery_and_stop(self):
        jid = self.start('import time; time.sleep(30)')
        original = S.JOBS[jid]
        S._load_jobs()
        self.assertTrue(call(S.job_status, job_id=jid)['running'])
        r = call(S.job_stop, job_id=jid)
        self.assertTrue(r['stopped'])
        self.assertIsNotNone(original['proc'].poll())
        original['fh'].close()

    def test_job_ownership_and_quota_survive_recovery(self):
        def start_client(owner, resources=None):
            token = S._principal.set(owner)
            try:
                return S.start_job([sys.executable, "-c", "import time; time.sleep(30)"], str(WORK),
                                   "test-private-command", resources=resources)
            finally:
                S._principal.reset(token)

        with patch.dict(S.CFG, {"job_max_active_per_client": 1}):
            first = start_client("client-a", ["shared-resource"])["job_id"]
            self.addCleanup(self.stop, first)
            conflict = start_client("client-b", ["shared-resource"])
            self.assertEqual(conflict["error_code"], "resource_busy")
            self.assertNotIn("owner_job_id", conflict)
            self.assertEqual(start_client("client-a")["error_code"], "job_quota")
            second = start_client("client-b")["job_id"]
            self.addCleanup(self.stop, second)

            original = {jid: S.JOBS[jid] for jid in (first, second)}
            S._load_jobs()
            try:
                for caller, own, other in (("client-a", first, second), ("client-b", second, first)):
                    token = S._principal.set(caller)
                    try:
                        self.assertEqual([job["job_id"] for job in call(S.job_status)["jobs"]], [own])
                        for operation in (S.job_status, S.read_job_output, S.job_stop):
                            with self.assertRaises(S.BridgeError) as denied:
                                call(operation, job_id=other)
                            self.assertEqual(denied.exception.code, "unknown_job")
                        self.assertEqual(start_client(caller)["error_code"], "job_quota")
                    finally:
                        S._principal.reset(token)

                history = {job["job_id"]: job for job in S._panel_jobs()["jobs"]}
                self.assertTrue(history[first]["recovered"])
                self.assertEqual(history[second]["owner"], "client-b")
                self.assertNotIn("test-private-command", json.dumps(history))
                self.assertNotIn("resources", history[first])
                self.assertTrue(S._stop_job(first)["stopped"])
                self.assertEqual(call(S.job_status, job_id=first)["status"], "stopped")
            finally:
                for job in original.values():
                    job["fh"].close()

    def test_real_server_process_restart(self):
        # A separate launcher exits with os._exit, losing all Python state.
        import importlib.util
        root = WORK / 'restart-root'
        root.mkdir(exist_ok=True)
        (root/'config.json').write_text(json.dumps({'workspace':str(WORK),'secret':'z'*64}), encoding='utf-8')
        script = '''import importlib.util,json,os,sys
spec=importlib.util.spec_from_file_location('bridge',sys.argv[1]);s=importlib.util.module_from_spec(spec);spec.loader.exec_module(s)
j=s.start_job([sys.executable,'-u','-c',"import time;time.sleep(.5);print('survived');raise SystemExit(3)"])
print(j['job_id'],flush=True);os._exit(0)
'''
        env = dict(os.environ, PCBRIDGE_ROOT=str(root))
        launcher = subprocess.run([sys.executable,'-c',script,S.__file__],capture_output=True,env=env,timeout=15)
        self.assertEqual(launcher.returncode,0,launcher.stderr)
        jid = launcher.stdout.decode().strip()
        result = root/'logs'/'jobs'/f'{jid}.result.json'
        deadline = time.monotonic()+10
        # A newly replaced result can briefly be locked by Windows/AV. Wait for readable JSON,
        # not merely a directory entry; permanent unreadability still fails the deadline.
        data = None
        while time.monotonic()<deadline:
            try:
                data = json.loads(result.read_text(encoding='utf-8'))
                break
            except (FileNotFoundError, PermissionError, json.JSONDecodeError):
                time.sleep(.03)
        self.assertIsNotNone(data, 'worker result never became readable')
        self.assertEqual(data['exit_code'],3)
        self.assertIn('survived',(root/'logs'/f'job_{jid}.log').read_text())

    def test_unknown_and_pid_reuse_never_kills_unrelated_process(self):
        jid = self.start('import time; time.sleep(30)')
        job = S.JOBS[jid]
        saved = (job['proc'],job['create_time'])
        try:
            job.update(proc=None, create_time=job['create_time']-100)
            self.assertEqual(call(S.job_status,job_id=jid)['status'],'unknown')
            self.assertFalse(call(S.job_stop,job_id=jid)['stopped'])
            self.assertIsNone(saved[0].poll())
        finally:
            job.update(proc=saved[0],create_time=saved[1])

    def test_incremental_utf8_and_log_reset(self):
        jid = self.start("print('Привет мир')")
        self.finish(jid)
        cursor, chunks = '', []
        for _ in range(50):
            r = call(S.read_job_output, job_id=jid,cursor=cursor,max_bytes=4)
            chunks.append(r['text']);cursor=r['next_cursor']
            if not r['has_more']: break
        self.assertEqual(''.join(chunks).replace('\r\n','\n'),'Привет мир\n')
        self.assertEqual(call(S.read_job_output,job_id=jid,cursor=cursor)['text'],'')
        S.JOBS[jid]['log'].write_bytes(b'new')
        r = call(S.read_job_output,job_id=jid,cursor=cursor)
        self.assertTrue(r['reset']);self.assertEqual(r['text'],'new')

    def test_invalid_cursor(self):
        jid=self.start('print(1)');self.finish(jid)
        with self.assertRaises(S.BridgeError):
            call(S.read_job_output,job_id=jid,cursor='invalid!')


class Transport(unittest.TestCase):
    def mcp_call(self, name, args):
        import anyio
        return anyio.run(functools.partial(S.mcp.call_tool, name, args))

    def test_structured_success_and_error(self):
        from mcp.types import CallToolResult
        r=self.mcp_call('file_info',{'path':'missing-file.txt'})
        self.assertIsInstance(r,CallToolResult)
        self.assertTrue(r.structured_content['ok']);self.assertFalse(r.is_error)
        e=self.mcp_call('apply_file_edits',{'plan_id':'bad'})
        self.assertTrue(e.is_error);self.assertEqual(e.structured_content['error_code'],'invalid_plan')

    def test_legacy_dict_and_text_tools(self):
        from mcp.types import CallToolResult
        r=self.mcp_call('server_info',{})
        self.assertIsInstance(r,CallToolResult)
        self.assertIn('version',r.structured_content)
        (WORK/'transport.txt').write_bytes(b'hello\n')
        text=self.mcp_call('read_text_file',{'path':'transport.txt'})
        self.assertIn('hello',str(text))


if __name__ == '__main__': unittest.main()
