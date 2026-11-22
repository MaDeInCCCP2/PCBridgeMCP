"""Bounded JSON RPC to an isolated, single-threaded Windows UIA worker.
Importing this module never imports COM or touches the desktop.
"""
import atexit
import json
from pathlib import Path
import queue
import subprocess
import sys
import threading
import uuid


class ComputerUseError(ValueError):
    def __init__(self, code, message, **details):
        super().__init__(message)
        self.code, self.details = code, details


class ComputerUseClient:
    def __init__(self, worker_path, log_path, timeout=15, python=None, worker_args=None, kill_tree=False, close_at_exit=True):
        self.worker_path, self.log_path = Path(worker_path), Path(log_path)
        self.timeout = max(.1, min(float(timeout), 60))
        self.python = python or sys.executable
        self.worker_args=list(worker_args or [])
        self.kill_tree=kill_tree
        self.lock = threading.Lock()
        self.proc = self.messages = self.log = None
        if close_at_exit:
            atexit.register(self.close)

    def _start(self):
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        if self.log_path.exists() and self.log_path.stat().st_size > 2_000_000:
            self.log_path.replace(self.log_path.with_suffix('.old.log'))
        self.log = open(self.log_path, 'ab')
        try:
            self.proc = subprocess.Popen([self.python, '-u', str(self.worker_path)]+self.worker_args,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log,
                text=True, encoding='utf-8', bufsize=1,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except BaseException:
            self.log.close()
            self.log = None
            raise
        messages, proc = queue.Queue(maxsize=4), self.proc
        self.messages = messages

        def reader():
            try:
                while True:
                    line = proc.stdout.readline(2_000_000)
                    if not line:
                        break
                    try:
                        message = json.loads(line)
                    except (ValueError, TypeError):
                        message = {'protocol_error': True}
                    messages.put_nowait(message)
            except (OSError, ValueError, queue.Full):
                pass
            finally:
                try:
                    messages.put_nowait({'worker_exited': True})
                except queue.Full:
                    pass
        threading.Thread(target=reader, name='uia-rpc-reader', daemon=True).start()

    def close(self):
        # Called under the request lock, or at interpreter shutdown.
        proc, self.proc = self.proc, None
        if proc is not None:
            if proc.poll() is None:
                try:
                    if self.kill_tree:
                        import psutil
                        parent=psutil.Process(proc.pid)
                        children=parent.children(recursive=True)
                        for child in children:
                            try:child.kill()
                            except psutil.NoSuchProcess:pass
                        parent.kill()
                        psutil.wait_procs(children+[parent],timeout=2)
                    else:
                        proc.kill()
                except Exception:
                    pass
            try:
                proc.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                pass
            for stream in (proc.stdin, proc.stdout):
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass
        if self.log:
            self.log.close()
            self.log = None
        self.messages = None

    def call(self, operation, arguments=None, timeout=None, *, start=True):
        if not self.lock.acquire(timeout=2):
            raise ComputerUseError('ui_busy', 'Another UI operation is running. Wait before observing again.')
        mutating = operation in {'action','owned_action','run_steps','open','navigate','close_page','download_save','trace','session_open','session_close'}
        try:
            if self.proc is None or self.proc.poll() is not None:
                if not start:
                    return {"sessions": []}
                self.close()
                self._start()
            request_id = uuid.uuid4().hex
            request = {'id': request_id, 'operation': operation, 'arguments': arguments or {}}
            try:
                payload=json.dumps(request, ensure_ascii=False)
                if len(payload)>900000:
                    raise ComputerUseError('request_too_large','Worker request exceeds 900000 characters; split the operation before input')
                self.proc.stdin.write(payload + '\n')
                self.proc.stdin.flush()
                response = self.messages.get(timeout=timeout or self.timeout)
            except ComputerUseError:
                raise
            except (OSError, ValueError, queue.Empty) as exc:
                self.close()
                raise ComputerUseError('ui_timeout' if isinstance(exc, queue.Empty) else 'ui_disconnected',
                    'UI worker stopped responding; all old snapshots are invalid. Observe before acting again.',
                    outcome='unknown' if mutating else 'not_applicable', retry_action=False) from exc
            if not isinstance(response,dict) or response.get('id') != request_id:
                self.close()
                raise ComputerUseError('ui_worker_lost', 'UI worker exited or sent an invalid response. Take a new snapshot.',
                                       outcome='unknown' if mutating else 'not_applicable', retry_action=False)
            if not response.get('ok'):
                raise ComputerUseError(response.get('error_code', 'ui_failed'), response.get('error', 'UI operation failed'),
                                       **response.get('details', {}))
            return response['result']
        finally:
            self.lock.release()
