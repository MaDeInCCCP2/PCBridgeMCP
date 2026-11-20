import json
import sys
import time
for line in sys.stdin:
    request = json.loads(line)
    op = request['operation']
    if op == 'slow':
        time.sleep(20)
    if op == 'crash':
        raise SystemExit(1)
    if op == 'bad':
        print('{}', flush=True)
        continue
    if op == 'error':
        print(json.dumps({'id':request['id'],'ok':False,'error_code':'stale_snapshot','error':'Observe again','details':{}}),flush=True)
    else:
        print(json.dumps({'id':request['id'],'ok':True,'result':{'ok':True,'operation':op}}),flush=True)
