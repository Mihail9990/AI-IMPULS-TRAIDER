"""Phase 1 read-only panel assets, independent of native trade/runtime code.

Only exact asset names are served. No upload, listing, token handling, cookies,
external requests, persistence, or changes to the underlying authenticated API.
"""
from pathlib import Path

ASSETS={
    '/panel/':('index.html','text/html; charset=utf-8'),
    '/panel/panel.css':('panel.css','text/css; charset=utf-8'),
    '/panel/model.js':('model.js','text/javascript; charset=utf-8'),
    '/panel/session.js':('session.js','text/javascript; charset=utf-8'),
    '/panel/panel.js':('panel.js','text/javascript; charset=utf-8'),
    '/panel/controls.js':('controls.js','text/javascript; charset=utf-8'),
    '/panel/control-policy.mjs':('control-policy.mjs','text/javascript; charset=utf-8'),
    '/panel/recovery-actions.mjs':('recovery-actions.mjs','text/javascript; charset=utf-8'),
}
CSP=("default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
     "img-src 'none'; font-src 'none'; object-src 'none'; frame-src 'none'; "
     "frame-ancestors 'none'; base-uri 'none'; form-action 'none'; "
     "worker-src 'none'; manifest-src 'none'")
HEADERS=[(b'cache-control',b'no-store'),(b'pragma',b'no-cache'),
         (b'content-security-policy',CSP.encode('ascii')),
         (b'referrer-policy',b'no-referrer'),(b'x-content-type-options',b'nosniff'),
         (b'x-frame-options',b'DENY'),(b'cross-origin-opener-policy',b'same-origin'),
         (b'permissions-policy',b'camera=(), microphone=(), geolocation=()')]

class PanelAssets:
    def __init__(self,app,directory):
        self.app=app;self.directory=Path(directory).absolute()
    async def __call__(self,scope,receive,send):
        if scope['type']!='http' or not (scope.get('path','').startswith('/panel/') or scope.get('path')=='/panel'):
            return await self.app(scope,receive,send)
        path=scope['path'];asset=ASSETS.get(path)
        status=404;body=b'NOT_FOUND';content_type='text/plain; charset=utf-8';extra=[]
        if asset is not None:
            if scope.get('method')!='GET':
                status=405;body=b'METHOD_NOT_ALLOWED';extra=[(b'allow',b'GET')]
            else:
                name,content_type=asset
                try:
                    root=self.directory
                    candidate=root/name
                    # Reject symlink/reparse redirection in the packaged assets.
                    for item in [candidate,root,*root.parents]:
                        if item.is_symlink() or getattr(item.stat(),'st_file_attributes',0)&1024:
                            raise OSError('Asset redirection')
                    if candidate.resolve().parent!=root.resolve():raise OSError('Asset outside package')
                    with candidate.open('rb') as f:body=f.read(2*1024*1024+1)
                    if len(body)>2*1024*1024:raise OSError('Asset too large')
                    status=200
                except (OSError,ValueError):
                    status=503;body=b'PANEL_ASSET_UNAVAILABLE';content_type='text/plain; charset=utf-8'
        headers=list(HEADERS)+extra+[(b'content-type',content_type.encode('ascii')),(b'content-length',str(len(body)).encode('ascii'))]
        await send({'type':'http.response.start','status':status,'headers':headers})
        await send({'type':'http.response.body','body':body})


def connectivity_panel_config(config):
    """Add exact static/session routes; no trading POST routes."""
    marker='\n @state {'
    if config.count(marker)!=1 or config.count(' handle @state {')!=1:
        raise ValueError('Unexpected connectivity Caddy template')
    config=config.replace(marker,'''
 @panel {
  method GET
  path /panel/ /panel/panel.css /panel/model.js /panel/panel.js /panel/controls.js /panel/control-policy.mjs /panel/recovery-actions.mjs /panel/session.js
 }
 @sessionRead {
  method GET
  path /api/recovery/session/status
 }
 @sessionWrite {
  method POST
  path /api/recovery/session/login /api/recovery/session/logout
 }
 @state {''',1)
    return config.replace(' handle @state {',''' handle @panel {
  reverse_proxy 127.0.0.1:8767
 }
 handle @sessionRead {
  reverse_proxy 127.0.0.1:8767
 }
 handle @sessionWrite {
  reverse_proxy 127.0.0.1:8767
 }
 handle @state {''',1)
