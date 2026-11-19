"""API factory for TEST DOUBLE pipeline only. Not used by production run scripts."""
import hmac
from fastapi import FastAPI, Header, HTTPException
from .api import Command, SetRanges
from .coordinator import Coordinator


def create_mock_pipeline_app(token,pipeline,*,market_filling=None,lifespan=None):
    if len(token)<32:raise ValueError('Token must be at least 32 characters')
    coordinator=Coordinator(pipeline,market_filling=market_filling)
    app=FastAPI(docs_url=None,redoc_url=None,openapi_url=None,lifespan=lifespan)
    app.state.coordinator=coordinator
    def auth(value):
        if not value or not hmac.compare_digest(value,'Bearer '+token):raise HTTPException(401,'Unauthorized')
    def command(key,payload):
        try:return coordinator.command(str(key),payload)
        except ValueError as exc:raise HTTPException(409,str(exc)) from exc
    @app.get('/api/recovery/state')
    def state(authorization:str|None=Header(default=None)):
        auth(authorization)
        try:return coordinator.state()
        except ValueError as exc:raise HTTPException(409,str(exc)) from exc
    @app.post('/api/recovery/ranges')
    def ranges(body:SetRanges,authorization:str|None=Header(default=None)):
        auth(authorization);p=body.model_dump(mode='json');key=p.pop('request_id')
        return command(key,dict(p,kind='ranges'))
    @app.post('/api/recovery/cancel')
    def cancel(body:Command,authorization:str|None=Header(default=None)):
        auth(authorization);return command(body.request_id,{'kind':'cancel'})
    @app.post('/api/recovery/ranges/{rid}/close-cancel')
    def close_cancel(rid:str,body:Command,authorization:str|None=Header(default=None)):
        auth(authorization);return command(body.request_id,{'kind':'close_cancel','range_id':rid})
    return app
