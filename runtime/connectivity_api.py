"""Connectivity only; no recovery imports, MT5, database, or mutating routes."""
import hmac
from fastapi import FastAPI,Header,HTTPException

def create_app(token,instance="test-only"):
    if not isinstance(instance,str) or not instance:raise ValueError("Invalid instance")
    if not isinstance(token,str) or len(token)<32 or not token.isascii():raise ValueError('Invalid token')
    app=FastAPI(docs_url=None,redoc_url=None,openapi_url=None)
    @app.get('/api/recovery/state')
    def state(authorization:str|None=Header(default=None)):
        if not authorization or not authorization.isascii() or not hmac.compare_digest(authorization,'Bearer '+token):
            raise HTTPException(401,'Unauthorized')
        return {'schema_version':1,'mode':'read_only','live_trading':False,'connection_only':True,
                'can_cancel_ranges':False,'ranges':[],'positions':[],'orders':[],'loss_history':[],
                'settings':{'initial_sl_distance':'0.70','reentry_distance':'0.70'},
                'message':'Связь с VPS работает. Только проверка подключения: MT5 и торговля не запущены.'}
    @app.get('/_local_ready')
    def ready(authorization:str|None=Header(default=None)):
        if not authorization or not authorization.isascii() or not hmac.compare_digest(authorization,'Bearer '+token):raise HTTPException(401,'Unauthorized')
        return {'instance':instance}
    return app

if __name__=='__main__':
    import sys,uvicorn
    key=sys.stdin.readline().strip()
    nonce=sys.stdin.readline().strip()
    origin=sys.stdin.readline().strip()
    from pathlib import Path
    sys.path.insert(0,str(Path(__file__).resolve().parent))
    from native_web_panel import PanelAssets
    from web_session import BrowserSessions
    app=PanelAssets(BrowserSessions(create_app(key,nonce),key,origin,mode='connectivity',context=lambda:nonce),Path(__file__).resolve().parent.parent/'web')
    uvicorn.run(app,host='127.0.0.1',port=8767,access_log=False,log_level='warning',proxy_headers=True,forwarded_allow_ips='127.0.0.1')
