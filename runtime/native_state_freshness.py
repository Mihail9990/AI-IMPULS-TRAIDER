"""Process-local raw MT5 display clock + nonce-bound GET observation age.
Never UTC, quote freshness, execution authorization, or a persisted clock.
"""
import hmac
import json
import math
import re
import time

class RawAnchor:
    def __init__(self, tick, monotonic=time.monotonic):
        self._clock=monotonic;self._anchor=None;self._last=None
        raw=getattr(tick,'time_msc',None);seconds=getattr(tick,'time',None)
        mono=self._clock()
        if (type(raw) is int and 0<raw<2**53 and type(seconds) is int and seconds>0
                and 0<=raw-seconds*1000<1000 and isinstance(mono,(int,float))
                and not isinstance(mono,bool) and math.isfinite(mono) and mono>=0):
            self._anchor=(raw,mono);self._last=mono
    def monotonic(self):
        now=self._clock()
        if (isinstance(now,bool) or not isinstance(now,(int,float)) or not math.isfinite(now)
                or now<0 or self._last is not None and now<self._last):
            self._anchor=None
            return None
        self._last=now
        return now
    def raw_now_ms(self,now):
        if self._anchor is None or now is None or now<self._anchor[1]:return None
        result=self._anchor[0]+math.floor((now-self._anchor[1])*1000)
        return result if 0<result<2**53 else None


def capture_anchor(api, scope, verify_session, monotonic=time.monotonic):
    verify_session(api,scope)
    try: tick=api.symbol_info_tick(scope['symbol'])
    except Exception: tick=None
    verify_session(api,scope)
    return RawAnchor(tick,monotonic)


class StateFreshness:
    """Outer ASGI wrapper; does not call coordinator, MT5 or any mutation on GET."""
    def __init__(self,app,anchor,token,max_body=8*1024*1024,web_armed=None):
        self.app=app;self.anchor=anchor;self.token=('Bearer '+token).encode('ascii');self.max_body=max_body;self.web_armed=web_armed
    async def __call__(self,scope,receive,send):
        if scope['type']!='http' or scope.get('method')!='GET' or scope.get('path')!='/api/recovery/state':
            return await self.app(scope,receive,send)
        start=self.anchor.monotonic()
        headers=scope.get('headers',[])
        nonces=[v for k,v in headers if k.lower()==b'x-recovery-state-nonce']
        auth=[v for k,v in headers if k.lower()==b'authorization']
        nonce=nonces[0] if len(nonces)==1 and re.fullmatch(b'[0-9a-f]{32}',nonces[0]) else None
        authorized=len(auth)==1 and hmac.compare_digest(auth[0],self.token)
        response=None; chunks=[];size=0;passthrough=False
        async def intercept(message):
            nonlocal response,size,passthrough
            if message['type']=='http.response.start':
                response=dict(message)
                response['headers']=[(k,v) for k,v in message.get('headers',[]) if k.lower() not in (b'cache-control',b'expires',b'pragma')]+[(b'cache-control',b'no-store'),(b'pragma',b'no-cache')]
                return
            if message['type']!='http.response.body':
                return await send(message)
            if passthrough:return await send(message)
            chunk=message.get('body',b'');chunks.append(chunk);size+=len(chunk)
            if size>self.max_body:
                passthrough=True;await send(response)
                await send(dict(message,body=b''.join(chunks)));chunks.clear();return
            if message.get('more_body',False):return
            body=b''.join(chunks)
            now=self.anchor.monotonic()
            if nonce is not None and authorized and response['status']==200:
                # Timing is diagnostic only. Missing raw anchor or invalid
                # monotonic cannot suppress an otherwise current nonce reply.
                age=math.ceil((now-start)*1000) if start is not None and now is not None and now>=start else None
                raw=self.anchor.raw_now_ms(now)
                try:
                    data=json.loads(body);widget=data.get('widget',{})
                    if (type(widget.get('schema_version')) is int and widget.get('schema_version')==1 and widget.get('mode')=='native'
                            and widget.get('snapshot',{}).get('status')=='fresh'):
                        data['state_freshness']=dict(version=1,nonce=nonce.decode('ascii'),age_ms=age,mt5_now_ms=raw)
                        body=json.dumps(data,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode('utf-8')
                        response['headers']=[(k,v) for k,v in response['headers'] if k.lower() not in (b'content-length',b'etag',b'content-md5')]+[(b'content-length',str(len(body)).encode())]
                except (ValueError,TypeError,AttributeError):pass
            await send(response);await send(dict(message,body=body))
        await self.app(scope,receive,intercept)
