"""Reload this exact bridge after a source/config update, with a verified supervisor identity.
Run --check first. Does not stop any other copy or any user application.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import psutil

ROOT=Path(__file__).resolve().parent.parent
STATUS=ROOT/'logs'/'reload-status.json'


def status(state,**details):
    STATUS.write_text(json.dumps({'state':state,'time':time.time(),**details}),encoding='utf-8')


def verified_supervisor():
    pids=json.loads((ROOT/'logs'/'pids.json').read_text(encoding='utf-8'))
    process=psutil.Process(int(pids['supervisor']))
    expected=(ROOT/'server'/'supervisor.py').resolve()
    if not any(Path(arg).resolve()==expected for arg in process.cmdline() if arg.lower().endswith('.py')):
        raise RuntimeError('Supervisor does not belong to the selected bridge folder')
    server=psutil.Process(int(pids['server']))
    if server.ppid()!=process.pid:raise RuntimeError('Server has a different supervisor')
    expected_server=(ROOT/'server'/'server.py').resolve()
    if not any(Path(arg).resolve()==expected_server for arg in server.cmdline() if arg.lower().endswith('.py')):
        raise RuntimeError('Server does not belong to this bridge folder')
    return process


def main():
    process=verified_supervisor()
    if '--check' in sys.argv:
        print('Verified target supervisor and server; no processes changed.')
        return
    status('scheduled')
    time.sleep(4)  # let the caller return before the current MCP server is shut down
    if not process.is_running():raise RuntimeError('Supervisor changed before reload')
    status('stopping')
    (ROOT/'logs'/'stop.flag').write_text('reload',encoding='utf-8')
    process.wait(timeout=40)
    (ROOT/'logs'/'stop.flag').unlink(missing_ok=True)
    status('starting')
    with open(ROOT/'logs'/'reload-supervisor.log','ab') as log:
        child=subprocess.Popen([sys.executable,str(ROOT/'server'/'supervisor.py')],cwd=str(ROOT),
            stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    status('started',supervisor=child.pid)


if __name__=='__main__':
    try:main()
    except Exception as error:
        status('failed',error=str(error))
        raise SystemExit(1)
