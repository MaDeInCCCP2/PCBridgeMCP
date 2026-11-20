"""Isolated MTA UI Automation adapter and snapshot/ref protocol. No code execution RPC.
All COM objects stay on this process' main thread. Parent kills this process on timeout.
"""
import collections
import json
import os
import sys
import time
import uuid


class UIError(ValueError):
    def __init__(self, code, message, **details):
        super().__init__(message)
        self.code, self.details = code, details


class WindowsBackend:
    PATTERNS = {'invoke': 'iface_invoke', 'set_value': 'iface_value', 'toggle': 'iface_toggle',
                'select': 'iface_selection_item', 'expand': 'iface_expand_collapse',
                'collapse': 'iface_expand_collapse', 'scroll': 'iface_scroll'}

    def __init__(self):
        if os.name != 'nt':
            raise UIError('unsupported_platform', 'Windows UI Automation requires Windows')
        # Set before importing pywinauto/comtypes. The worker owns no GUI windows.
        sys.coinit_flags = 0  # COINIT_MULTITHREADED
        try:
            from pywinauto import Desktop
            import psutil
        except ImportError as e:
            raise UIError('ui_dependency_missing', 'Install pywinauto and comtypes using START.bat/SETTINGS.bat') from e
        import ctypes
        from ctypes import wintypes
        self.ctypes, self.wintypes, self.psutil = ctypes, wintypes, psutil
        self.u32 = ctypes.WinDLL('user32', use_last_error=True)
        self.u32.GetForegroundWindow.restype = wintypes.HWND
        self.u32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        try:
            self.u32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
            self.u32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        except (OSError, AttributeError):
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(2)
            except (OSError, AttributeError):
                pass
        self.desktop = Desktop(backend='uia')
        self.native_desktop = Desktop(backend='win32')

    def foreground(self):
        return int(self.u32.GetForegroundWindow() or 0)

    def windows(self):
        result = []
        for w in self.native_desktop.windows(visible_only=True):
            try:
                pid = w.process_id()
                result.append({'hwnd': int(w.handle), 'pid': pid, 'title': w.window_text()[:256],
                               'exe': self.psutil.Process(pid).name()})
            except Exception:
                continue
        return result

    def root(self, hwnd):
        # Resolve anew so recycled HWNDs cannot reuse old COM state.
        return self.desktop.window(handle=int(hwnd)).wrapper_object()

    def identity(self, root):
        pid = int(root.element_info.process_id)
        return (int(root.handle), pid, self.psutil.Process(pid).create_time(), self.runtime_id(root))

    def runtime_id(self, element):
        return tuple(element.element_info.element.GetRuntimeId())

    def children(self, element):
        return element.children()

    def belongs(self, element, root):
        return int(element.top_level_parent().handle) == int(root.handle)

    def blocking_dialog(self, root):
        self.u32.GetLastActivePopup.argtypes=[self.wintypes.HWND]
        self.u32.GetLastActivePopup.restype=self.wintypes.HWND
        self.u32.IsWindowVisible.argtypes=[self.wintypes.HWND]
        popup=int(self.u32.GetLastActivePopup(root.handle) or 0)
        if popup and popup!=int(root.handle) and self.u32.IsWindowVisible(popup):
            return {'window':'hwnd:'+str(popup)}
        return None

    def inspect(self, element):
        info = element.element_info
        raw = info.element
        password = bool(raw.CurrentIsPassword)
        patterns = {}
        for action, attribute in self.PATTERNS.items():
            try:
                patterns[action] = getattr(element, attribute)
            except Exception:
                continue
        enabled, visible = bool(info.enabled), bool(info.visible)
        rect = info.rectangle
        node = {'role': info.control_type, 'name': '[password]' if password else info.name[:256],
                'automation_id': (info.automation_id or '')[:128], 'enabled': enabled,
                'visible': visible, 'password': password,
                'bounds': [int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)],
                'actions': list(patterns)}
        value = patterns.get('set_value')
        if value is not None:
            try:
                node['readonly'] = bool(value.CurrentIsReadOnly)
                if node['readonly']:
                    node['actions'].remove('set_value')
                if not password:
                    node['value'] = value.CurrentValue[:512]
            except Exception:
                pass
        if 'toggle' in patterns:
            node['toggle_state'] = int(patterns['toggle'].CurrentToggleState)
        if 'expand' in patterns:
            node['expand_state'] = int(patterns['expand'].CurrentExpandCollapseState)
        if 'select' in patterns:
            node['selected'] = bool(patterns['select'].CurrentIsSelected)
        return node

    def value_equals(self,element,value):
        if element.element_info.element.CurrentIsPassword:return None
        return element.iface_value.CurrentValue==value

    def act(self, element, action, value):
        interface = getattr(element, self.PATTERNS[action])
        if action == 'invoke':
            interface.Invoke()
        elif action == 'set_value':
            interface.SetValue(value)
        elif action == 'toggle':
            interface.Toggle()
        elif action == 'select':
            interface.Select()
        elif action == 'expand':
            interface.Expand()
        elif action == 'collapse':
            interface.Collapse()
        elif action == 'scroll':
            # ScrollAmount: NoAmount=2, LargeIncrement=3, LargeDecrement=0.
            interface.Scroll(2, 3 if value == 'down' else 0)


class UIService:
    def __init__(self, backend, clock=time.monotonic, ttl=120):
        self.backend, self.clock, self.ttl = backend, clock, ttl
        self.current = None
        self.events=collections.deque(maxlen=50)
        self.last_error=None

    def _choose(self, window):
        if not window:
            hwnd = self.backend.foreground()
            if not hwnd:
                raise UIError('window_not_found', 'No foreground window. Use list_windows and hwnd:<number>.')
            return hwnd
        if window.lower().startswith('hwnd:'):
            try:
                hwnd = int(window[5:].strip(), 0)
                if hwnd <= 0:
                    raise ValueError()
                return hwnd
            except ValueError:
                raise UIError('invalid_window', 'Use hwnd:<positive decimal or hex number>')
        windows = self.backend.windows()
        exact = [w for w in windows if w['title'].casefold() == window.casefold()]
        candidates = exact or [w for w in windows if window.casefold() in w['title'].casefold()
                              or window.casefold().removesuffix('.exe') == w['exe'].casefold().removesuffix('.exe')]
        if len(candidates) != 1:
            raise UIError('ambiguous_window' if candidates else 'window_not_found',
                          'Select one window using hwnd:<number>.', windows=(candidates or windows)[:30])
        return candidates[0]['hwnd']

    @staticmethod
    def _fingerprint(node):
        return (node['role'], node['name'], node.get('automation_id', ''), node.get('password', False))

    def snapshot(self, window='', max_nodes=200, max_depth=8, include_bounds=False):
        max_nodes, max_depth = max(1, min(int(max_nodes), 500)), max(0, min(int(max_depth), 15))
        self.current = None  # partial/failed captures must not leave previous refs usable
        hwnd = self._choose(window)
        root = self.backend.root(hwnd)
        identity = self.backend.identity(root)
        snapshot_id = uuid.uuid4().hex
        refs, nodes, errors = {}, [], 0
        pending, visited, truncated = collections.deque([(root, None, 0)]), set(), False
        # Bound both traversal and returned output, including text/unnamed nodes.
        while pending and len(nodes) < max_nodes:
            element, parent, depth = pending.popleft()
            try:
                runtime = self.backend.runtime_id(element)
                if runtime in visited:
                    continue
                visited.add(runtime)
                node = self.backend.inspect(element)
                ref = 'e' + str(len(nodes) + 1)
                refs[ref] = {'element': element, 'runtime': runtime, 'fingerprint': self._fingerprint(node)}
                public = {k: v for k, v in node.items() if include_bounds or k != 'bounds'}
                public.update(ref=ref, parent=parent, depth=depth)
                nodes.append(public)
                if node.get('password'):
                    continue  # password providers may expose text descendants; do not inspect them
                if depth < max_depth:
                    children = self.backend.children(element)
                    # Do not enqueue unbounded lists from a huge provider.
                    capacity = max_nodes - len(nodes) - len(pending)
                    if len(children) > max(0, capacity):
                        truncated = True
                    pending.extend((child, ref, depth + 1) for child in children[:max(0, capacity)])
                elif self.backend.children(element):
                    truncated = True
            except Exception:
                errors += 1
        truncated = truncated or bool(pending)
        if not nodes:
            raise UIError('ui_unavailable', 'This window exposes no readable UIA nodes. Use screenshot/OCR.')
        result = {'ok': True, 'snapshot_id': snapshot_id, 'window': 'hwnd:' + str(hwnd),
                  'expires_in_seconds': self.ttl, 'elements': nodes, 'truncated': truncated,
                  'unreadable_nodes': errors, 'backend': 'windows-uia',
                  'hint': 'Use ui_find on this snapshot, then ui_action with snapshot_id and ref. Screenshots/OCR remain available.'}
        self.current = {'id': snapshot_id, 'created': self.clock(), 'hwnd': hwnd, 'root_identity': identity,
                        'refs': refs, 'result': result, 'options': {'max_nodes': max_nodes, 'max_depth': max_depth,
                                                               'include_bounds': include_bounds}}
        return result

    def _require(self, snapshot_id):
        cur = self.current
        if cur is None or cur['id'] != snapshot_id or self.clock() - cur['created'] > self.ttl:
            raise UIError('stale_snapshot', 'Snapshot expired, worker restarted or a newer snapshot exists. Call ui_snapshot.')
        return cur

    @staticmethod
    def matches(node, text='', role=''):
        return (not role or node['role'].casefold() == role.casefold()) and (not text or
            text.casefold() in (node.get('name', '') + ' ' + node.get('value', '')).casefold())

    def find(self, snapshot_id, text='', role='', limit=30):
        cur = self._require(snapshot_id)
        matches = [n for n in cur['result']['elements'] if self.matches(n, text, role)]
        limit = max(1, min(int(limit), 100))
        return {'ok': True, 'snapshot_id': snapshot_id, 'matches': matches[:limit],
                'total_matches': len(matches), 'truncated': len(matches) > limit,
                'snapshot_truncated': cur['result']['truncated'],
                'hint': 'Search covers the captured snapshot only; increase snapshot depth/nodes if needed.'}

    def wait(self, window='', text='', role='', present=True, timeout_seconds=10, max_nodes=200):
        if not text and not role:
            raise UIError('invalid_condition', 'Specify text or role to wait for')
        timeout_seconds = max(0, min(float(timeout_seconds), 30))
        hwnd = self._choose(window)
        identity = self.backend.identity(self.backend.root(hwnd))
        deadline = self.clock() + timeout_seconds
        while True:
            if self.backend.identity(self.backend.root(hwnd)) != identity:
                raise UIError('stale_window', 'Window identity changed while waiting')
            snapshot = self.snapshot('hwnd:' + str(hwnd), max_nodes=max_nodes)
            hits = [n for n in snapshot['elements'] if n['visible'] and self.matches(n, text, role)]
            if bool(hits) == bool(present):
                # An incomplete tree cannot prove an element is absent.
                if present or not snapshot['truncated'] and not snapshot['unreadable_nodes']:
                    return {'ok': True, 'condition_met': True, 'matches': hits[:30], 'snapshot': snapshot}
            if self.clock() >= deadline:
                return {'ok': False, 'error': 'UI condition was not observed before timeout',
                        'error_code': 'ui_condition_timeout', 'condition_met': False, 'snapshot': snapshot}
            time.sleep(.15)

    def action(self, snapshot_id, ref, action='invoke', value='', expect_text='', wait_seconds=2):
        supported = {'invoke', 'set_value', 'toggle', 'select', 'expand', 'collapse', 'scroll'}
        if action not in supported:
            raise UIError('unsupported_action', 'Supported actions: ' + ', '.join(sorted(supported)))
        if not isinstance(value, str) or len(value) > 100000:
            raise UIError('invalid_value', 'value must be a string no longer than 100000 characters')
        if action == 'scroll' and value not in ('up', 'down'):
            raise UIError('invalid_value', 'scroll value must be up or down')
        cur = self._require(snapshot_id)
        saved = cur['refs'].get(ref)
        if saved is None:
            raise UIError('unknown_ref', 'Element ref is not in this snapshot')
        try:
            root = self.backend.root(cur['hwnd'])
            if self.backend.identity(root) != cur['root_identity']:
                raise UIError('stale_window', 'Window/process identity changed. Observe again.')
            element = saved['element']
            node = self.backend.inspect(element)
            if self.backend.runtime_id(element) != saved['runtime'] or self._fingerprint(node) != saved['fingerprint']:
                raise UIError('stale_element', 'Element identity or label changed. Observe again.')
            if not self.backend.belongs(element, root):
                raise UIError('stale_element', 'Element no longer belongs to the captured window')
            if not node['enabled'] or not node['visible']:
                raise UIError('element_not_ready', 'Element must be enabled and visible')
            if action not in node['actions']:
                raise UIError('unsupported_pattern', 'Element does not support this semantic action. Use another element or screenshot/OCR.',
                              supported_actions=node['actions'])
        except UIError:
            raise
        except Exception as e:
            raise UIError('stale_element', 'Element is no longer accessible. Observe again.') from e
        before = cur['result']['elements']
        self.current = None  # invalidate before invoking; a failed/partial action cannot be replayed with old refs
        try:
            self.backend.act(element, action, value)
        except Exception as e:
            raise UIError('ui_action_failed', 'UI provider failed during the action. Observe before retrying.',
                          outcome='unknown', retry_action=False) from e
        verification = {'verified': False, 'reason': 'No goal condition was supplied'}
        try:
            after_node = self.backend.inspect(element)
            if action == 'set_value' and not node.get('password'):
                verified=getattr(self.backend,'value_equals',lambda _element,expected:after_node.get('value')==expected)(element,value)
                verification = {'verified': verified is True, 'reason': 'value comparison'}
            elif action == 'toggle':
                verification = {'verified': after_node.get('toggle_state') != node.get('toggle_state'), 'reason': 'toggle state comparison'}
            elif action in ('expand', 'collapse'):
                verification = {'verified': after_node.get('expand_state') == (1 if action == 'expand' else 0), 'reason': 'expand/collapse state'}
            elif action == 'select':
                verification = {'verified': after_node.get('selected') is True, 'reason': 'selection state'}
        except Exception:
            pass
        try:
            if self.backend.identity(self.backend.root(cur['hwnd'])) != cur['root_identity']:
                raise UIError('stale_window', 'Target window changed after the action')
            if expect_text:
                observed = self.wait('hwnd:' + str(cur['hwnd']), text=expect_text,
                                     timeout_seconds=min(10, max(0, float(wait_seconds))),
                                     max_nodes=cur['options']['max_nodes'])
                after = observed['snapshot']
                verification = {'verified': observed['condition_met'], 'reason': 'expected text visible'}
            else:
                after = self.snapshot('hwnd:' + str(cur['hwnd']), **cur['options'])
        except Exception as e:
            return {'ok': True, 'action_executed': True, 'action': action,
                    'verification': verification, 'observation_error': str(e),
                    'hint': 'Action completed but observation failed. Do not replay; take a new snapshot.'}
        return {'ok': True, 'action_executed': True, 'action': action, 'verification': verification,
                'ui_changed': before != after['elements'], 'snapshot': after,
                'hint': 'Use the NEW snapshot_id/refs for the next action; action execution alone does not prove the goal.'}

    def find_live(self, window='', text='', role='', automation_id='', exact=False,
                  limit=30, max_scan=3000, max_depth=30):
        """Search beyond snapshot output limits; retain only matches as actionable refs."""
        if not text and not role and not automation_id:
            raise UIError('invalid_selector', 'Specify text, role or automation_id')
        max_scan, max_depth, limit = max(1,min(int(max_scan),5000)), max(0,min(int(max_depth),50)), max(1,min(int(limit),100))
        self.current = None
        hwnd=self._choose(window);root=self.backend.root(hwnd);identity=self.backend.identity(root)
        pending=collections.deque([(root,0)]);seen=set();refs={};nodes=[];scanned=errors=matches=0;truncated=False
        while pending and scanned<max_scan:
            element,depth=pending.popleft()
            try:
                runtime=self.backend.runtime_id(element)
                if runtime in seen:continue
                seen.add(runtime);scanned+=1
                node=self.backend.inspect(element)
                name_matches=(not text or (node['name'].casefold()==text.casefold() if exact else self.matches(node,text)))
                if name_matches and (not role or node['role'].casefold()==role.casefold()) and (not automation_id or node.get('automation_id')==automation_id):
                    matches+=1
                    if len(nodes)<limit:
                        ref='e'+str(len(nodes)+1)
                        refs[ref]={'element':element,'runtime':runtime,'fingerprint':self._fingerprint(node)}
                        nodes.append({**{k:v for k,v in node.items() if k!='bounds'},'ref':ref,'depth':depth,'parent':None})
                if node.get('password'):continue
                children=self.backend.children(element)
                if depth>=max_depth:
                    truncated=truncated or bool(children)
                else:
                    capacity=max(0,max_scan-scanned-len(pending))
                    truncated=truncated or len(children)>capacity
                    pending.extend((c,depth+1) for c in children[:capacity])
            except Exception:
                errors+=1
        truncated=truncated or bool(pending)
        snapshot_id=uuid.uuid4().hex
        result={'ok':True,'snapshot_id':snapshot_id,'window':'hwnd:'+str(hwnd),'matches':nodes,
                'total_matches':matches,'scanned':scanned,'search_truncated':truncated,
                'matches_truncated':matches>limit,'unreadable_nodes':errors,'expires_in_seconds':self.ttl}
        self.current={'id':snapshot_id,'created':self.clock(),'hwnd':hwnd,'root_identity':identity,'refs':refs,
                      'result':{'elements':nodes,'truncated':truncated,'unreadable_nodes':errors,'filtered':True},
                      'options':{'max_nodes':200,'max_depth':8,'include_bounds':False}}
        return result

    def snapshot_diff(self, snapshot_id, max_nodes=200, max_depth=8):
        old=self._require(snapshot_id)
        old_keys={old['refs'][n['ref']]['runtime']:n for n in old['result']['elements']}
        hwnd=old['hwnd']
        if self.backend.identity(self.backend.root(hwnd))!=old['root_identity']:
            raise UIError('stale_window','Cannot compare different window/process identities')
        current=self.snapshot('hwnd:'+str(hwnd),max_nodes=max_nodes,max_depth=max_depth)
        cur=self.current
        new_keys={cur['refs'][n['ref']]['runtime']:n for n in current['elements']}
        plain=lambda n:{k:v for k,v in n.items() if k not in ('ref','parent','depth')}
        added=[n for key,n in new_keys.items() if key not in old_keys]
        removed=[{'old_ref':n['ref'],'role':n['role'],'name':n['name']} for key,n in old_keys.items() if key not in new_keys]
        changed=[{'old_ref':old_keys[key]['ref'],'element':n} for key,n in new_keys.items()
                 if key in old_keys and plain(n)!=plain(old_keys[key])]
        # Surviving refs are remapped without re-sending unchanged node contents.
        refs={old_keys[key]['ref']:n['ref'] for key,n in new_keys.items() if key in old_keys}
        partial=bool(old['result'].get('filtered') or old['result'].get('truncated') or old['result'].get('unreadable_nodes') or current['truncated'] or current['unreadable_nodes'])
        return {'ok':True,'base_snapshot_id':snapshot_id,'snapshot_id':current['snapshot_id'],'window':current['window'],
                'added':added,'removed':removed,'changed':changed,'ref_map':refs,'partial':partial,
                'hint':'All old refs are invalid; use ref_map/new refs. Partial captures cannot prove removals.'}

    HINTS={
        'stale_snapshot':'Take ui_snapshot or ui_find_live; use its new snapshot_id and refs.',
        'stale_element':'Re-find the element by role/name/automation_id; do not reuse the ref.',
        'stale_window':'List windows again and explicitly choose hwnd:<number>.',
        'ambiguous_window':'Select an exact hwnd from the reported candidate windows.',
        'ambiguous_element':'Narrow role/name/automation_id until exactly one visible element matches.',
        'incomplete_search':'Increase max_scan/max_depth or narrow to a smaller window; do not choose an arbitrary match.',
        'element_not_ready':'Wait for the control to become visible and enabled; inspect for a dialog.',
        'unexpected_window':'A modal popup is active. Observe that window and handle it before continuing.',
        'unsupported_pattern':'Use a supported semantic action or inspect with screenshot/OCR.',
        'ui_action_failed':'Outcome may be unknown. Observe current state before any further input.',
        'ui_condition_timeout':'Inspect the current UI and condition; action execution does not prove the goal.',
    }

    def diagnose(self, snapshot_id='', ref='', limit=20):
        report={'ok':True,'recent_operations':list(self.events)[-max(1,min(int(limit),50)):],
                'snapshot_valid':False,'last_error':self.last_error}
        cur=self.current
        if cur and (not snapshot_id or snapshot_id==cur['id']) and self.clock()-cur['created']<=self.ttl:
            try:
                root=self.backend.root(cur['hwnd'])
                report['snapshot_valid']=self.backend.identity(root)==cur['root_identity']
                blocker=getattr(self.backend,'blocking_dialog',lambda _root:None)(root)
                if blocker:report['blocking_window']=blocker
                if ref and report['snapshot_valid']:
                    saved=cur['refs'].get(ref)
                    if not saved:report['target_error']='unknown_ref'
                    else:
                        node=self.backend.inspect(saved['element'])
                        report['target']={k:v for k,v in node.items() if k!='value'}
                        report['target']['identity_matches']=self.backend.runtime_id(saved['element'])==saved['runtime'] and self._fingerprint(node)==saved['fingerprint']
            except Exception:
                report['snapshot_valid']=False
        report['hint']=self.HINTS.get((self.last_error or {}).get('code'),'Observe the target, check readiness and supported actions before input.')
        return report

    def run_steps(self, window, steps, timeout_seconds=30):
        if not isinstance(steps,list) or not 1<=len(steps)<=20:
            raise UIError('invalid_steps','Supply 1..20 structured steps')
        supported={'invoke','set_value','toggle','select','expand','collapse','scroll'}
        # Validate the whole plan before the first side effect.
        for step in steps:
            if not isinstance(step,dict) or step.get('type','action') not in ('action','wait','assert'):
                raise UIError('invalid_steps','Step type must be action, wait or assert')
            if step.get('type','action')=='action':
                selector=step.get('selector')
                if not isinstance(selector,dict) or not any(selector.get(k) for k in ('text','role','automation_id')):
                    raise UIError('invalid_selector','Action steps need a selector with text, role or automation_id')
                if any(not isinstance(selector.get(k,''),str) for k in ('text','role','automation_id')) or ('exact' in selector and not isinstance(selector['exact'],bool)):
                    raise UIError('invalid_selector','Selector text/role/automation_id must be strings; exact must be boolean')
                if set(selector)-{'text','role','automation_id','exact'}:
                    raise UIError('invalid_selector','Unknown selector field')
                if step.get('action','invoke') not in supported:
                    raise UIError('unsupported_action','Unknown scenario action')
                if not isinstance(step.get('value',''),str) or len(step.get('value',''))>100000:
                    raise UIError('invalid_value','Scenario values must be strings <=100000 characters')
                if step.get('action')=='scroll' and step.get('value') not in ('up','down'):
                    raise UIError('invalid_value','Scroll requires up/down')
            elif not step.get('text') and not step.get('role'):
                raise UIError('invalid_condition','wait/assert steps need text or role')
            if any(not isinstance(step.get(k,''),str) for k in ('text','role','expect_text')) or ('present' in step and not isinstance(step['present'],bool)):
                raise UIError('invalid_steps','Condition text/role must be strings and present must be boolean')
            for key in ('timeout_seconds','wait_seconds'):
                if key in step:
                    try:
                        number=float(step[key])
                        if not 0<=number<=30:raise ValueError()
                    except (TypeError,ValueError):raise UIError('invalid_steps',f'{key} must be between 0 and 30')
        deadline=self.clock()+max(0,min(float(timeout_seconds),45))
        hwnd=self._choose(window);target='hwnd:'+str(hwnd);identity=self.backend.identity(self.backend.root(hwnd));reports=[]
        for index,step in enumerate(steps):
            executed=False
            try:
                if self.clock()>=deadline:raise UIError('scenario_timeout','Scenario deadline reached')
                root=self.backend.root(hwnd)
                if self.backend.identity(root)!=identity:raise UIError('stale_window','Scenario target changed')
                blocker=getattr(self.backend,'blocking_dialog',lambda _root:None)(root)
                if blocker:raise UIError('unexpected_window','Modal window interrupted scenario',blocking_window=blocker)
                kind=step.get('type','action')
                if kind in ('wait','assert'):
                    result=self.wait(target,text=step.get('text',''),role=step.get('role',''),present=step.get('present',True),
                                     timeout_seconds=0 if kind=='assert' else min(float(step.get('timeout_seconds',5)),max(0,deadline-self.clock())))
                    if not result['condition_met']:raise UIError('ui_condition_timeout','Scenario condition was not observed')
                    reports.append({'step':index,'type':kind,'ok':True,'executed':False})
                else:
                    found=self.find_live(target,**step['selector'],limit=2)
                    hits=[n for n in found['matches'] if n['visible']]
                    if found['search_truncated'] or found['unreadable_nodes']:
                        raise UIError('incomplete_search','Search could not establish a unique target')
                    if found['total_matches']!=1 or len(hits)!=1:
                        raise UIError('ambiguous_element' if found['total_matches']>1 else 'element_not_found',
                                      'Scenario requires exactly one visible match',total_matches=found['total_matches'])
                    if self.clock()>=deadline:raise UIError('scenario_timeout','Deadline reached during target search')
                    result=self.action(found['snapshot_id'],hits[0]['ref'],step.get('action','invoke'),step.get('value',''),
                                       expect_text=step.get('expect_text',''),wait_seconds=min(float(step.get('wait_seconds',2)),max(0,deadline-self.clock())))
                    executed=bool(result.get('action_executed'))
                    verified=result.get('verification',{}).get('verified',False)
                    reports.append({'step':index,'type':'action','action':step.get('action','invoke'),'ok':True,
                                    'executed':executed,'verified':verified})
                    if result.get('observation_error'):raise UIError('observation_failed','Action executed but follow-up observation failed',outcome='executed')
                    verify_required=bool(step.get('expect_text')) or (step.get('action') in ('set_value','toggle','select','expand','collapse') and not hits[0].get('password'))
                    if verify_required and not verified:raise UIError('ui_condition_timeout','Action executed but expected result was not observed',outcome='executed')
            except Exception as error:
                code=getattr(error,'code','ui_failed')
                if not reports or reports[-1]['step']!=index:
                    reports.append({'step':index,'type':step.get('type','action'),'ok':False,'executed':executed})
                reports[-1]['ok']=False
                return {'ok':False,'error':str(error),'error_code':code,'failed_step':index,
                        'completed_steps':index,'steps':reports,'outcome':getattr(error,'details',{}).get('outcome','executed' if executed else 'not_executed'),
                        'hint':self.HINTS.get(code,'Inspect UI before deciding whether to continue; completed steps are not rolled back.'),
                        'details':getattr(error,'details',{})}
        return {'ok':True,'completed_steps':len(steps),'steps':reports,'snapshot_id':self.current['id'] if self.current else None,
                'hint':'Scenario finished; old refs are invalid. There is no automatic undo for application actions.'}

    def dispatch(self, operation, arguments):
        handlers = {'snapshot': self.snapshot, 'find': self.find, 'wait': self.wait, 'action': self.action,
                    'find_live':self.find_live,'snapshot_diff':self.snapshot_diff,'diagnose':self.diagnose,'run_steps':self.run_steps}
        if operation not in handlers:
            raise UIError('unknown_operation', 'Unknown UI operation')
        started=self.clock()
        try:
            result=handlers[operation](**arguments)
            self.events.append({'operation':operation,'ok':result.get('ok',True),'seconds':round(self.clock()-started,3)})
            if not result.get('ok',True):
                self.last_error={'code':result.get('error_code','ui_failed'),'operation':operation,'hint':result.get('hint','Inspect current UI')}
            return result
        except UIError as error:
            hint=self.HINTS.get(error.code,'Inspect the target and current state before retrying input.')
            error.details.setdefault('hint',hint)
            self.last_error={'code':error.code,'operation':operation,'hint':hint}
            self.events.append({'operation':operation,'ok':False,'code':error.code,'seconds':round(self.clock()-started,3)})
            raise


def main():
    # stdout is protocol-only; warnings and diagnostics go to stderr.
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stdin.reconfigure(encoding='utf-8')
    service = None
    for line in sys.stdin:
        request = {}
        try:
            if len(line) > 1_000_000:
                raise UIError('request_too_large', 'UI request exceeds 1 MB')
            request = json.loads(line)
            if not isinstance(request, dict):
                request = {}
                raise UIError('invalid_request', 'UI request must be an object')
            if not isinstance(request.get('arguments', {}), dict):
                raise UIError('invalid_request', 'UI arguments must be an object')
            if service is None:
                service = UIService(WindowsBackend())
            result = service.dispatch(request['operation'], request.get('arguments') or {})
            response = {'id': request['id'], 'ok': True, 'result': result}
        except Exception as e:
            response = {'id': request.get('id'), 'ok': False, 'error_code': getattr(e, 'code', 'ui_failed'),
                        'error': str(e), 'details': getattr(e, 'details', {})}
        print(json.dumps(response, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
