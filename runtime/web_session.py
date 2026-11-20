"""Process-local browser sessions. No trade arm/confirmation is stored here.
Direct Bearer clients stay unchanged. Install outside StateFreshness/API wrappers.
"""
import asyncio
import hashlib
import hmac
import json
import re
import secrets
import time
from http.cookies import SimpleCookie, CookieError
from urllib.parse import urlsplit

COOKIE='__Host-RecoverySession'
BASE='/api/recovery/session/'
WRITE={'/api/recovery/ranges','/api/recovery/cancel','/api/recovery/lot-schedule',
       '/api/recovery/ranges/1/close-cancel','/api/recovery/ranges/2/close-cancel'}

_EA_API='/api/recovery/ea/'
_EA_SUPPORT=_EA_API+'support-settings'
_EA_WRITE={_EA_API+name for name in ('manual_wait','manual_pending_wait','manual_cancel')}
_UUID='[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}'
_EA_STATUS=re.compile(_EA_API+'commands/'+_UUID)
_EA_BOOT_QUERY=re.compile(('expected_boot='+_UUID).encode('ascii'))


class BrowserSessions:
    def __init__(self, app, token, origin, *, mode, context, clock=time.monotonic, ttl=28800, route_profile='legacy'):
        parsed=urlsplit(origin)
        if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
            raise ValueError('Fixed HTTPS origin required')
        if mode not in ('native','connectivity') or len(token)<32 or not token.isascii():raise ValueError('Invalid session configuration')
        if not 1 <= ttl <= 28800:raise ValueError('Invalid session expiry')
        if route_profile not in ('legacy','recovery-ea') or (route_profile=='recovery-ea' and mode!='native'):
            raise ValueError('Invalid route profile')
        self.route_profile=route_profile
        self.app,self.token,self.origin,self.host=app,token,origin,parsed.netloc
        self.mode,self.context,self.clock,self.ttl=mode,context,clock,ttl
        self.sessions={};self.last_context=None;self.failures=[]

    async def reply(self,send,status,data, cookie=None):
        raw=json.dumps(data,separators=(',',':')).encode()
        headers=[(b'content-type',b'application/json'),(b'cache-control',b'no-store'),(b'pragma',b'no-cache'),(b'content-length',str(len(raw)).encode())]
        if cookie:headers.append((b'set-cookie',cookie.encode('ascii')))
        await send({'type':'http.response.start','status':status,'headers':headers})
        await send({'type':'http.response.body','body':raw})

    def cookie(self,value,age):
        return f'{COOKIE}={value}; Path=/; Max-Age={age}; Secure; HttpOnly; SameSite=Strict'

    async def __call__(self,scope,receive,send):
        if scope['type']!='http':return await self.app(scope,receive,send)
        path,method=scope.get('path',''),scope.get('method','')
        is_session=path.startswith(BASE)
        headers={}
        for k,v in scope.get('headers',[]):headers.setdefault(k.lower(),[]).append(v)
        def one(name):
            values=headers.get(name,[])
            return values[0].decode('latin1') if len(values)==1 else None
        auth=one(b'authorization')
        ea_panel=self.route_profile=='recovery-ea'
        def deny(status=401):return self.reply(send,status,{'detail':'Browser session unavailable'})
        if ea_panel:
            # Public shell and assets contain no session data. Route every EA
            # API call through this browser session gate, never raw Bearer.
            if not is_session and not path.startswith(_EA_API):
                return await self.app(scope,receive,send)
            if not is_session and b'authorization' in headers:
                return await deny()
        else:
            # Legacy clients retain their explicit-Bearer behavior.
            if b'authorization' in headers and not is_session:
                return await self.app(scope,receive,send)
            if not is_session and b'cookie' not in headers:
                return await self.app(scope,receive,send)
        if scope.get('scheme')!='https':return await deny(403)
        if one(b'host')!=self.host or one(b'x-recovery-session')!='1':return await deny(403)
        origin=one(b'origin')
        fetch_site=one(b'sec-fetch-site')
        if (b'origin' in headers and origin!=self.origin) or (fetch_site is not None and fetch_site!='same-origin'):
            return await deny(403)
        if method!='GET' and origin!=self.origin:return await deny(403)
        if method!='GET' and one(b'content-type')!='application/json':return await deny(415)
        query=scope.get('query_string',b'')
        login=path==BASE+'login' and method=='POST'
        status=path==BASE+'status' and method=='GET'
        logout=path==BASE+'logout' and method=='POST'
        if ea_panel:
            poll=method=='GET' and _EA_STATUS.fullmatch(path) is not None
            if poll:
                if _EA_BOOT_QUERY.fullmatch(query) is None:return await deny(400)
            elif query:return await deny(400)
            allowed=(poll or (method=='GET' and path==_EA_API+'state')
                     or (method=='POST' and path in _EA_WRITE)
                     or (method in ('GET','PUT') and path==_EA_SUPPORT))
        else:
            if query:return await deny(400)
            allowed=method=='GET' and path=='/api/recovery/state'
            if self.mode=='native':
                allowed=allowed or (method=='GET' and re.fullmatch('/api/recovery/commands/[0-9a-fA-F-]{36}',path) is not None) or (method=='POST' and path in WRITE)
        if not (login or status or logout or allowed):return await deny(403)
        now=self.clock()
        raw_cookie=one(b'cookie')
        sid=None
        if raw_cookie:
            pieces=[x.strip().split('=',1)[0] for x in raw_cookie.split(';')]
            if pieces.count(COOKIE)!=1:return await deny()
            try:
                jar=SimpleCookie();jar.load(raw_cookie);sid=jar[COOKIE].value
            except (KeyError,ValueError,CookieError):return await deny()
        digest=hashlib.sha256(sid.encode()).hexdigest() if sid and re.fullmatch('[A-Za-z0-9_-]{43}',sid) else None
        def active(at):
            session=self.sessions.get(digest)
            return session if session and session['expires']>at else None
        def good_csrf(session):
            return hmac.compare_digest((one(b'x-recovery-csrf') or '').encode('utf-8'),session['csrf'].encode('ascii'))
        token=self.token
        if login:
            self.failures=[t for t in self.failures if t>now-60]
            if not auth or not auth.isascii() or not hmac.compare_digest(auth,'Bearer '+token):
                if len(self.failures)>=10:return await deny(429)
                self.failures.append(now);return await deny()
        else:
            session=active(now)
            if not session:return await deny()
            if (method=='POST' or (ea_panel and method=='PUT')) and not good_csrf(session):return await deny(403)
        # Only admitted clients may trigger the broker-context read; avoid blocking
        # the API event loop while the native session lock is held by the scheduler.
        checked_mode=self.mode
        try:
            current=await asyncio.to_thread(self.context)
            if not isinstance(current,str) or not current:raise ValueError('context')
        except Exception:
            self.sessions.clear();return await deny(503)
        if token!=self.token or checked_mode!=self.mode:return await deny()
        binding=(self.mode,current,hashlib.sha256(token.encode()).hexdigest())
        if binding!=self.last_context:self.sessions.clear();self.last_context=binding
        now=self.clock()
        self.sessions={k:v for k,v in self.sessions.items() if v['expires']>now}
        if login:
            if len(self.sessions)>=64 and digest not in self.sessions:return await deny(429)
            if digest:self.sessions.pop(digest,None)
            sid=secrets.token_urlsafe(32);csrf=secrets.token_urlsafe(32)
            self.sessions[hashlib.sha256(sid.encode()).hexdigest()]={'expires':now+self.ttl,'csrf':csrf}
            return await self.reply(send,200,{'authenticated':True,'mode':self.mode,'csrf':csrf,'expires_in':self.ttl},self.cookie(sid,self.ttl))
        # An overlapping logout/token change/expiry cannot revive a session.
        if active(now) is not session:return await deny()
        if status:
            return await self.reply(send,200,{'authenticated':True,'mode':self.mode,'csrf':session['csrf'],'expires_in':int(session['expires']-now)})
        if logout:
            self.sessions.pop(digest,None)
            return await self.reply(send,200,{'authenticated':False},self.cookie('',0))
        copied=dict(scope)
        copied['headers']=[(k,v) for k,v in scope['headers'] if k.lower() not in (b'cookie',b'authorization')]+[(b'authorization',('Bearer '+self.token).encode())]
        return await self.app(copied,receive,send)
