"""Dedicated Playwright browser worker. Own contexts only; never attaches to existing tabs.
RPC arguments are structured, not executable JavaScript. Python/Playwright objects stay on one thread.
"""
import collections
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import sys
import time
import urllib.parse
import uuid


class BrowserError(ValueError):
    def __init__(self,code,message,**details):
        super().__init__(message);self.code,self.details=code,details


def safe_url(url):
    try:
        p=urllib.parse.urlsplit(url)
        host=p.hostname or ''
        return urllib.parse.urlunsplit((p.scheme,host+(':'+str(p.port) if p.port else ''),p.path,'',''))
    except ValueError:return '[invalid URL]'


class BrowserService:
    def __init__(self,allow_lan=False,clock=time.monotonic,playwright_factory=None,shared_browser=None):
        self.allow_lan,self.clock=allow_lan,clock
        self.factory=playwright_factory
        self.shared_browser = shared_browser
        self.pw=self.browser=self.context=None
        self.pages={};self.active=None;self.current={};self.events=collections.deque(maxlen=200)
        self.downloads={};self.tracing=False;self.headless=None

    def check_url(self,url,websocket=False):
        if url=='about:blank' and not websocket:return
        try:p=urllib.parse.urlsplit(url)
        except ValueError:raise BrowserError('invalid_url','Invalid URL')
        allowed=('ws','wss') if websocket else ('http','https')
        if p.scheme not in allowed or not p.hostname or p.username or p.password:
            raise BrowserError('invalid_url','Use an http(s) URL without embedded credentials (or about:blank)')
        if self.allow_lan:return
        try:
            addresses=socket.getaddrinfo(p.hostname,p.port or (443 if p.scheme in ('https','wss') else 80),type=socket.SOCK_STREAM)
            if not addresses:raise OSError('No addresses')
            if any(not ipaddress.ip_address(a[4][0].split('%')[0]).is_global for a in addresses):
                raise BrowserError('lan_url_blocked','Local/private network URLs are disabled by allow_lan_urls')
        except BrowserError:raise
        except (OSError,ValueError):raise BrowserError('dns_failed','Could not resolve URL host')

    def _record(self,kind,**details):
        self.events.append({'time':round(time.time(),3),'kind':kind,**details})

    def _register(self,page):
        for key,p in self.pages.items():
            if p is page:return key
        if len([p for p in self.pages.values() if not p.is_closed()])>=10:
            page.close();return None
        key=uuid.uuid4().hex[:12];self.pages[key]=page
        page.on('console',lambda message:self._record('console',page_id=key,level=message.type,text=message.text[:1000]))
        page.on('pageerror',lambda error:self._record('page_error',page_id=key,text=str(error)[:1000]))
        page.on('requestfailed',lambda request:self._record('request_failed',page_id=key,url=safe_url(request.url),reason=str(request.failure)[:200]))
        page.on('response',lambda response:self._record('response',page_id=key,url=safe_url(response.url),status=response.status) if response.status>=400 else None)
        page.on('download',lambda download:self._download(key,download))
        page.on('dialog',lambda dialog:self._dialog(key,dialog))
        return key

    def _dialog(self,page_id,dialog):
        # Do not stall subsequent calls on an unattended JS dialog. Explicitly report the dismissal.
        self._record('dialog_dismissed',page_id=page_id,dialog_type=dialog.type,message=dialog.message[:500])
        dialog.dismiss()

    def _download(self,page_id,download):
        key=uuid.uuid4().hex
        self.downloads[key]={'object':download,'page_id':page_id,'filename':Path(download.suggested_filename).name}
        self._record('download',page_id=page_id,download_id=key,filename=self.downloads[key]['filename'])

    def _start(self,headless):
        if self.browser is not None:
            if self.headless!=bool(headless):
                raise BrowserError('browser_mode_mismatch','Headless mode is fixed for this worker session. Close/restart it before changing mode.')
            return
        try:
            if self.shared_browser:
                self.browser = self.shared_browser(bool(headless))
            else:
                if self.factory is None:
                    from playwright.sync_api import sync_playwright
                    self.pw = sync_playwright().start()
                else:
                    self.pw = self.factory()
                self.browser = self.pw.chromium.launch(headless=bool(headless))
            self.context=self.browser.new_context(accept_downloads=True,service_workers='block')
            self.context.set_default_timeout(8000)
            self.context.set_default_navigation_timeout(15000)
            self.headless=bool(headless)
            def route_request(route):
                try:
                    self.check_url(route.request.url)
                    route.continue_()
                except BrowserError as error:
                    self._record('request_blocked',url=safe_url(route.request.url),error_code=error.code)
                    route.abort('blockedbyclient')
            self.context.route('**/*',route_request)
            def websocket(ws):
                try:
                    self.check_url(ws.url,websocket=True)
                    if hasattr(ws,'connect_to_server'):ws.connect_to_server()
                    else:ws.connect()
                except BrowserError:
                    self._record('websocket_blocked',url=safe_url(ws.url));ws.close()
            self.context.route_web_socket('**/*',websocket)
            self.context.on('page',self._register)
        except BrowserError:
            self.shutdown();raise
        except Exception as error:
            self.shutdown()
            raise BrowserError('browser_start_failed','Could not start bundled Chromium. Run python -m playwright install chromium.',reason=str(error)[:500]) from error

    def _page(self,page_id=''):
        key=page_id or self.active
        page=self.pages.get(key)
        if page is None or page.is_closed():raise BrowserError('unknown_page','Page is closed or belongs to a previous worker. Use browser_tabs or browser_open.')
        return key,page

    def open(self,url='about:blank',headless=False):
        self.check_url(url);self._start(headless)
        if len([p for p in self.pages.values() if not p.is_closed()])>=10:
            raise BrowserError('page_limit','At most 10 owned pages may be open')
        try:
            page = self.context.new_page()
        except Exception as error:
            raise BrowserError('browser_page_start_failed', 'Could not open a page in the browser context.') from error
        key = self._register(page)
        self.active = key
        try:
            if url!='about:blank':page.goto(url,wait_until='domcontentloaded')
            return self.snapshot(key)
        except Exception as error:
            raise BrowserError('navigation_failed','Page was created but navigation did not complete. Inspect it before retrying.',page_id=key,url=safe_url(url),reason=str(error)[:400]) from error

    def _pump(self):
        # The sync worker sleeps on stdin between requests; one harmless protocol round-trip
        # dispatches queued browser events without depending on a later navigation/click.
        for page in list(self.pages.values()):
            if not page.is_closed():
                try:page.evaluate('0')
                except Exception:pass
                break

    def tabs(self):
        self._pump()
        result=[]
        for key,page in list(self.pages.items()):
            if not page.is_closed():
                result.append({'page_id':key,'url':safe_url(page.url),'title':page.title()[:256],'active':key==self.active})
        return {'ok':True,'pages':result,'owned_browser_only':True}

    def navigate(self,page_id,url):
        self.check_url(url);key,page=self._page(page_id);self.current.pop(key,None)
        try:
            page.goto(url,wait_until='domcontentloaded')
            return self.snapshot(key)
        except Exception as error:
            raise BrowserError('navigation_failed','Navigation outcome is uncertain. Inspect the current page.',page_id=key,outcome='unknown',reason=str(error)[:400]) from error

    def snapshot(self,page_id='',max_chars=20000,max_depth=12):
        key,page=self._page(page_id);self.current.pop(key,None)
        max_chars=max(1000,min(int(max_chars),100000));max_depth=max(1,min(int(max_depth),30))
        try:
            text=page.locator('body').aria_snapshot(mode='ai',depth=max_depth)
            # Never expose password contents even if a provider includes them in its ARIA name/value.
            for frame in page.frames:
                values=frame.locator('input[type=password]').evaluate_all('(els)=>els.map(e=>e.value).filter(Boolean)')
                for value in values:text=text.replace(value,'[password]')
            identity=page.evaluate('performance.timeOrigin')
        except Exception as error:
            raise BrowserError('browser_snapshot_failed','Could not read page accessibility. Inspect diagnostics or navigate again.',page_id=key,reason=str(error)[:400]) from error
        truncated=len(text)>max_chars
        text=text[:max_chars]
        # Discard a cut final line, including a possibly partial ref.
        if truncated and '\n' in text:text=text.rsplit('\n',1)[0]
        refs=set(re.findall(r'\[ref=([A-Za-z0-9]+)\]',text))
        token=uuid.uuid4().hex
        result={'ok':True,'page_id':key,'snapshot_id':token,'url':safe_url(page.url),'title':page.title()[:256],
                'aria':text,'truncated':truncated,'max_depth':max_depth,'expires_in_seconds':120,
                'hint':'Use refs from aria with browser_action. New snapshots/actions invalidate old refs.'}
        self.current[key]={'id':token,'created':self.clock(),'identity':identity,'refs':refs,'text':text}
        return result

    def _require(self,page_id,snapshot_id):
        key,page=self._page(page_id);cur=self.current.get(key)
        if not cur or cur['id']!=snapshot_id or self.clock()-cur['created']>120:
            raise BrowserError('stale_snapshot','Take a new browser_snapshot; old refs cannot be replayed')
        if page.evaluate('performance.timeOrigin')!=cur['identity']:
            self.current.pop(key,None);raise BrowserError('stale_page','Page navigated since snapshot. Observe again.')
        return key,page,cur

    def find(self,page_id,snapshot_id,text='',role='',limit=30):
        key,page,cur=self._require(page_id,snapshot_id)
        matches=[]
        for line in cur['text'].splitlines():
            if text and text.casefold() not in line.casefold():continue
            if role and not re.match(r'^\s*-\s*'+re.escape(role)+r'(?:\s|$)',line,re.I):continue
            refs=re.findall(r'\[ref=([A-Za-z0-9]+)\]',line)
            if refs:matches.append({'ref':refs[0],'text':line.strip()[:1500]})
        limit=max(1,min(int(limit),100))
        return {'ok':True,'page_id':key,'snapshot_id':snapshot_id,'matches':matches[:limit],
                'total_matches':len(matches),'truncated':len(matches)>limit}

    def action(self,page_id,snapshot_id,ref,action='click',value='',expect_text=''):
        if action not in ('click','fill','check','uncheck','select','press'):
            raise BrowserError('unsupported_action','Use click, fill, check, uncheck, select or press')
        if not isinstance(value,str) or len(value)>100000:raise BrowserError('invalid_value','value must be a string <=100000 characters')
        key,page,cur=self._require(page_id,snapshot_id)
        if ref not in cur['refs']:raise BrowserError('unknown_ref','Ref was not present in the captured page')
        locator=page.get_by_ref(ref)
        # Readiness failure occurs before invocation and is distinguished from an uncertain action.
        try:
            locator.wait_for(state='visible',timeout=3000)
            if not locator.is_enabled():raise BrowserError('element_not_ready','Element is disabled')
        except BrowserError:raise
        except Exception as error:raise BrowserError('stale_element','Ref is absent, detached or not visible. Observe again.') from error
        self.current.pop(key,None)
        pages_before=set(self.pages)
        popup_expected=False;popup_observed=False
        try:
            if action=='click':
                popup_expected=getattr(locator,'get_attribute',lambda _name:None)('target')=='_blank'
                if popup_expected:
                    clicked=False
                    try:
                        with page.expect_popup(timeout=5000) as popup:
                            locator.click();clicked=True
                        self._register(popup.value);popup_observed=True
                    except Exception:
                        if not clicked:raise
                        self._record('popup_not_observed',page_id=key)
                else:locator.click()
            elif action=='fill':locator.fill(value)
            elif action=='check':locator.check()
            elif action=='uncheck':locator.uncheck()
            elif action=='select':locator.select_option(value)
            else:locator.press(value)
        except Exception as error:
            self._record('action_failed',page_id=key,action=action)
            raise BrowserError('browser_action_failed','Input may have executed. Observe page and diagnostics before retrying.',outcome='unknown',retry_action=False,reason=(str(error).replace(value,'[redacted]') if value else str(error))[:400]) from error
        verified=popup_observed;reason='popup observed' if popup_observed else 'Expected popup was not observed' if popup_expected else 'No goal condition supplied'
        try:
            if expect_text:
                page.get_by_text(expect_text,exact=False).filter(visible=True).first.wait_for(state='visible',timeout=5000)
                verified=True;reason='expected text visible'
            elif action=='fill':
                verified=locator.input_value()==value;reason='value comparison'
            elif action in ('check','uncheck'):
                verified=locator.is_checked()==(action=='check');reason='checked state'
            elif action=='select':verified=locator.input_value()==value;reason='selected value'
        except Exception:
            verified=False;reason='Verification did not succeed; inspect current state'
        goal_supplied = bool(expect_text) or popup_expected or action in ('fill', 'check', 'uncheck', 'select')
        status = 'verified' if verified else 'unverified' if goal_supplied else 'dispatched'
        result={'ok':True,'action_executed':True,'action':action,
                'verification':{'verified':verified,'reason':reason,'status':status},
                'new_pages':[p for p in self.pages if p not in pages_before]}
        try:result['snapshot']=self.snapshot(key)
        except BrowserError as error:result['observation_error']=str(error)
        return result

    def wait(self,page_id='',text='',present=True,timeout_seconds=10):
        if not text:raise BrowserError('invalid_condition','Specify text')
        key,page=self._page(page_id)
        try:
            page.get_by_text(text,exact=False).filter(visible=True).first.wait_for(state='visible' if present else 'hidden',
                timeout=max(1,min(float(timeout_seconds),30)*1000))
            met=True
        except Exception:met=False
        result={'ok':met,'condition_met':met,'page_id':key}
        if not met:result.update(error='Browser condition was not observed',error_code='browser_condition_timeout')
        try:result['snapshot']=self.snapshot(key)
        except BrowserError as error:result['observation_error']=str(error)
        return result

    def diagnostics(self,page_id='',limit=30):
        self._pump()
        if page_id:self._page(page_id)
        events=[e for e in self.events if not page_id or e.get('page_id') in (None,page_id)]
        downloads=[{'download_id':key,'filename':d['filename'],'page_id':d['page_id']} for key,d in self.downloads.items()
                   if not page_id or d['page_id']==page_id]
        return {'ok':True,'events':events[-max(1,min(int(limit),100)):],'downloads':downloads,'tracing':self.tracing,
                'hint':'No headers or request bodies are returned. Browser only includes pages it created.'}

    def download_save(self,download_id,path):
        download=self.downloads.get(download_id)
        if not download:raise BrowserError('unknown_download','Download belongs to an absent or previous browser worker')
        target=Path(path)
        if not target.is_absolute():raise BrowserError('invalid_path','Controller must pass an absolute path')
        target.parent.mkdir(parents=True,exist_ok=True)
        tmp=target.with_name('.'+target.name+'.'+uuid.uuid4().hex+'.tmp')
        try:
            download['object'].save_as(str(tmp))
            os.replace(tmp,target)
        finally:tmp.unlink(missing_ok=True)
        return {'ok':True,'path':str(target),'size':target.stat().st_size,'download_id':download_id}

    def trace(self,action='start',path=''):
        if self.context is None:raise BrowserError('browser_not_started','Open an owned browser first')
        if action=='start':
            if self.tracing:raise BrowserError('trace_already_running','A trace is already running')
            self.context.tracing.start(screenshots=False,snapshots=True,sources=False);self.tracing=True
            return {'ok':True,'tracing':True}
        if action!='stop' or not path:raise BrowserError('invalid_trace','Use start, or stop with a save path')
        if not self.tracing:raise BrowserError('trace_not_running','No trace is running')
        target=Path(path)
        if not target.is_absolute():raise BrowserError('invalid_path','Trace path must be absolute')
        target.parent.mkdir(parents=True,exist_ok=True)
        tmp=target.with_name('.'+target.name+'.'+uuid.uuid4().hex+'.zip')
        try:
            self.context.tracing.stop(path=str(tmp));self.tracing=False;os.replace(tmp,target)
        finally:tmp.unlink(missing_ok=True)
        return {'ok':True,'tracing':False,'path':str(target),'size':target.stat().st_size}

    def close_page(self,page_id):
        key,page=self._page(page_id);page.close();self.current.pop(key,None)
        if self.active==key:self.active=next((k for k,p in self.pages.items() if not p.is_closed()),None)
        return {'ok':True,'closed':key,'active':self.active}

    def shutdown(self):
        for obj in (self.context, None if self.shared_browser else self.browser):
            if obj:
                try:obj.close()
                except Exception:pass
        if self.pw:
            try:self.pw.stop()
            except Exception:pass
        self.context = self.browser = self.pw = self.headless = self.active = None
        self.pages.clear()
        self.current.clear()
        self.events.clear()
        self.downloads.clear()
        self.tracing = False
        return {'ok':True}

    def dispatch(self,operation,arguments):
        handlers={'open':self.open,'tabs':self.tabs,'navigate':self.navigate,'snapshot':self.snapshot,
                  'find':self.find,'action':self.action,'wait':self.wait,'diagnostics':self.diagnostics,
                  'download_save':self.download_save,'trace':self.trace,'close_page':self.close_page,'shutdown':self.shutdown}
        if operation not in handlers:raise BrowserError('unknown_operation','Unknown browser operation')
        return handlers[operation](**arguments)


class BrowserContexts:
    """One Playwright driver and at most two Chromium processes per worker.

    Each (owner, session) gets its own BrowserContext and handle namespace.
    """
    def __init__(self, allow_lan=False, factory=None):
        self.allow_lan = allow_lan
        self.factory = factory
        self.pw = None
        self.browsers = {}
        self.sessions = {}
        self.active = {}

    def browser(self, headless):
        if self.pw is None:
            if self.factory is None:
                from playwright.sync_api import sync_playwright
                self.pw = sync_playwright().start()
            else:
                self.pw = self.factory()
        if headless not in self.browsers:
            self.browsers[headless] = self.pw.chromium.launch(headless=headless)
        return self.browsers[headless]

    def remove(self, owner, session_id):
        service = self.sessions.pop((owner, session_id))
        service.shutdown()
        for mode, browser in list(self.browsers.items()):
            if not any(other.browser is browser for other in self.sessions.values()):
                del self.browsers[mode]
                try:
                    browser.close()
                except Exception:
                    pass
        if self.active.get(owner) == session_id:
            alternate = next((other for o, other in self.sessions if o == owner), None)
            if alternate:
                self.active[owner] = alternate
            else:
                self.active.pop(owner, None)

    def reap(self):
        for mode, browser in list(self.browsers.items()):
            if not browser.is_connected():
                del self.browsers[mode]
                for (owner, session_id), service in list(self.sessions.items()):
                    if service.browser is browser:
                        self.remove(owner, session_id)

    def _open(self, owner, arguments, session_id, session_limit):
        key = (owner, session_id)
        created = key not in self.sessions
        if created:
            if len(self.sessions) >= 16 or sum(o == owner for o, _ in self.sessions) >= session_limit:
                raise BrowserError("browser_session_limit", "Close a browser session before opening another.")
            self.sessions[key] = BrowserService(allow_lan=self.allow_lan, shared_browser=self.browser)
        service = self.sessions[key]
        try:
            snapshot = service.open(**arguments)
        except BrowserError as error:
            if created and not service.pages:
                self.remove(owner, session_id)
            elif service.pages:
                self.active[owner] = session_id
                error.details["session_id"] = session_id
            raise
        self.active[owner] = session_id
        return {**snapshot, **self.info(session_id, service)}

    @staticmethod
    def info(session_id, service):
        return {"session_id": session_id, "mode": "browser", "state": "ready",
                "headless": service.headless, "isolation": "browser_context",
                "desktop_interference": "no_physical_input" if service.headless else "may_change_focus"}

    def dispatch(self, operation, arguments):
        self.reap()
        arguments = dict(arguments)
        owner = arguments.pop("_owner", "owner")
        session_limit = arguments.pop("_session_limit", 4)
        owned = {session_id: service for (o, session_id), service in self.sessions.items() if o == owner}
        if operation == "session_inventory":
            sessions = []
            for (principal, sid), service in self.sessions.items():
                pages = {key for key, page in service.pages.items() if not page.is_closed()}
                snapshots = sum(key in pages and service.clock() - snapshot["created"] < 120
                                for key, snapshot in service.current.items())
                sessions.append({**self.info(sid, service), "owner": principal, "pages": len(pages),
                                 "snapshots": snapshots, "active": sid == self.active.get(principal)})
            return {"sessions": sessions}
        if operation == "open":
            return self._open(owner, arguments, "default", session_limit)
        if operation == "session_open":
            try:
                result = self._open(owner, arguments, uuid.uuid4().hex, session_limit)
            except BrowserError as error:
                error.details["_live_sessions"] = list(self.sessions)
                raise
            return {**result, "_live_sessions": list(self.sessions)}
        if operation == "sessions":
            return {"ok": True, "sessions": [self.info(k, v) for k, v in owned.items()]}
        if operation == "session_close":
            session_id = arguments["session_id"]
            if session_id not in owned:
                raise BrowserError("unknown_browser_session", "Browser session is unavailable.")
            self.remove(owner, session_id)
            return {"ok": True, "closed_session_id": session_id}
        if operation == "tabs":
            pages = []
            for session_id, service in owned.items():
                pages.extend({**page, "session_id": session_id,
                              "active": page["active"] and session_id == self.active.get(owner)}
                             for page in service.tabs()["pages"])
            return {"ok": True, "pages": pages, "owned_browser_only": True}

        page_id = arguments.get("page_id")
        if operation == "owned_action":
            service = owned.get(arguments.pop("session_id"))
            if service is None:
                raise BrowserError("unknown_browser_session", "Browser session is unavailable.")
            if page_id not in service.pages:
                raise BrowserError("unknown_page", "This page does not belong to the specified browser session.")
            return service.action(**arguments)
        download_id = arguments.get("download_id")
        if page_id or download_id:
            service = next((service for service in owned.values()
                            if (page_id and page_id in service.pages)
                            or (download_id and download_id in service.downloads)), None)
        elif operation == "trace":
            service = owned.get("default")
        else:
            service = owned.get(self.active.get(owner))
        if service is None:
            if operation == "diagnostics" and not page_id:
                return {"ok": True, "events": [], "downloads": [], "tracing": False}
            code = "unknown_download" if download_id else "unknown_page"
            raise BrowserError(code, "Browser page or session is unavailable.")
        return service.dispatch(operation, arguments)

    def shutdown(self):
        for owner, session_id in list(self.sessions):
            self.remove(owner, session_id)
        for browser in self.browsers.values():
            try:
                browser.close()
            except Exception:
                pass
        if self.pw:
            self.pw.stop()


def main():
    sys.stdin.reconfigure(encoding='utf-8');sys.stdout.reconfigure(encoding='utf-8')
    service=BrowserContexts(allow_lan='--allow-lan' in sys.argv)
    try:
        for line in sys.stdin:
            request={}
            try:
                request=json.loads(line)
                if not isinstance(request,dict):request={};raise BrowserError('invalid_request','Expected an object')
                if not isinstance(request.get('arguments',{}),dict):raise BrowserError('invalid_request','Expected structured arguments')
                result=service.dispatch(request['operation'],request.get('arguments') or {})
                response={'id':request['id'],'ok':True,'result':result}
            except Exception as error:
                response={'id':request.get('id'),'ok':False,'error':str(error),'error_code':getattr(error,'code','browser_failed'),
                          'details':getattr(error,'details',{})}
            print(json.dumps(response,ensure_ascii=False),flush=True)
    finally:service.shutdown()


if __name__=='__main__':main()
