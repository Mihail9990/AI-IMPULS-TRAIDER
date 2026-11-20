"""Optional authenticated route factory for the web worker, no automatic mount."""
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

if __package__:
    from .support_store import SettingsConflict, SettingsUnavailable, SupportStore, unique_json_pairs
else:
    from support_store import SettingsConflict, SettingsUnavailable, SupportStore, unique_json_pairs


def support_settings_router(store: SupportStore, require_panel_session) -> APIRouter:
    """require_panel_session must enforce session auth and mutation CSRF policy."""
    if not isinstance(store, SupportStore) or not callable(require_panel_session):
        raise TypeError("authoritative settings store and auth dependency required")
    router = APIRouter(dependencies=[Depends(require_panel_session)])
    path = "/api/recovery/ea/support-settings"

    def response(payload, status=200):
        return JSONResponse(payload, status_code=status, headers={"Cache-Control": "no-store"})

    @router.get(path)
    def get_support_settings():
        try:
            return response(store.projection())
        except SettingsUnavailable:
            return response({"detail": "Настройки сопровождения недоступны."}, 503)

    @router.put(path)
    async def save_support_settings(request: Request):
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise HTTPException(415, "JSON required", headers={"Cache-Control": "no-store"})
        data = bytearray()
        async for chunk in request.stream():
            if len(data) + len(chunk) > 4096:
                raise HTTPException(413, "Settings body too large", headers={"Cache-Control": "no-store"})
            data.extend(chunk)
        try:
            body = json.loads(data.decode("utf-8"), object_pairs_hook=unique_json_pairs)
            return response(await run_in_threadpool(store.save, body))
        except SettingsConflict:
            return response({"detail": "Состояние изменилось. Обнови настройки."}, 409)
        except SettingsUnavailable:
            return response({"detail": "Настройки сопровождения недоступны."}, 503)
        except (ValueError, UnicodeError, RecursionError):
            return response({"detail": "Неверные значения или шаг цены."}, 422)

    return router
