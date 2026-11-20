"""Serve the minimal new RECOVERY panel and authenticated EA mail routes.

No broker code. Start as one local worker behind the already configured HTTPS
reverse proxy; this module doesn't provision profiles, Caddy or MT5.
"""
from __future__ import annotations

import hmac
from importlib import import_module
import os
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from runtime.web_session import BrowserSessions

if __package__:
    from .channel import Channel
    from .web_handoff import create_handoff_app
else:
    from channel import Channel
    from web_handoff import create_handoff_app

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
SETTINGS = HERE.parent / 'settings'
# The source package's directory includes a hyphen; load via canonical module
# identity so the settings store can be explicitly injected by its owner.
support_settings_router = import_module(
    'design.recovery-ea.settings.support_routes').support_settings_router
CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
SECURITY_HEADERS = {
    'Cache-Control': 'no-store',
    'Content-Security-Policy': CSP,
    'Referrer-Policy': 'no-referrer',
    'X-Content-Type-Options': 'nosniff',
}
ASSET_ROOT = '/recovery/assets/'


def create_panel_app(token: str, origin: str, channel: Channel, *, support_store=None):
    """Opt-in standalone assembly for the new native branch, not a legacy mount."""
    handoff = create_handoff_app(token, channel)
    shell = (HERE / 'runtime_panel.html').read_text(encoding='utf-8')
    fragment = (HERE / 'recovery_manual.html').read_text(encoding='utf-8')
    for slot, html in (
        ('<!-- MANUAL_FORM -->', fragment),
        ('<!-- SUPPORT_FORM -->', (SETTINGS / 'support_controls.html').read_text(encoding='utf-8')),
    ):
        if shell.count(slot) != 1:
            raise ValueError('Missing RECOVERY form slot')
        shell = shell.replace(slot, html, 1)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, redirect_slashes=False)

    @app.get('/recovery', include_in_schema=False)
    def home():
        return HTMLResponse(shell, headers=SECURITY_HEADERS)

    def serve_asset(source, media_type):
        # Exact fixed paths only; never serve profile or repository directories.
        data = source.read_bytes()

        def response():
            return Response(data, media_type=media_type, headers=SECURITY_HEADERS)

        return response

    for name, source, media in (
        ('runtime_panel.mjs', HERE / 'runtime_panel.mjs', 'text/javascript'),
        ('runtime_panel.css', HERE / 'runtime_panel.css', 'text/css'),
        ('recovery_manual.mjs', HERE / 'recovery_manual.mjs', 'text/javascript'),
        ('support_controls.mjs', SETTINGS / 'support_controls.mjs', 'text/javascript'),
        ('support_panel.mjs', SETTINGS / 'support_panel.mjs', 'text/javascript'),
        ('session.js', REPO / 'web' / 'session.js', 'text/javascript'),
    ):
        app.add_api_route(ASSET_ROOT + name, serve_asset(source, media),
                          methods=['GET'], include_in_schema=False)

    def require_injected_header(request: Request):
        # The opt-in outer session guard has already verified cookie / CSRF and
        # discarded any browser Authorization. Defense in depth on this route.
        values = request.headers.getlist('Authorization')
        if (len(values) != 1 or not values[0].isascii()
                or not hmac.compare_digest(values[0], 'Bearer ' + token)):
            raise HTTPException(401, 'Unauthorized')

    if support_store is None:
        disabled = APIRouter(dependencies=[Depends(require_injected_header)])

        @disabled.api_route('/api/recovery/ea/support-settings', methods=['GET', 'PUT'])
        def no_authority():
            # Never fake an idle state from the channel read-only projection.
            return JSONResponse({'detail': 'Настройка порога пока недоступна.'}, status_code=503,
                                headers={'Cache-Control': 'no-store'})

        app.include_router(disabled)
    else:
        app.include_router(support_settings_router(support_store, require_injected_header))

    # The handoff must keep its own Bearer middleware. BrowserSessions injects
    # that header only after verifying the same-origin browser cookie and CSRF.
    app.mount('/', handoff)
    return BrowserSessions(app, token, origin, mode='native',
                           context=lambda: channel.scope_id, route_profile='recovery-ea')


def app_from_env():
    """Uvicorn factory; values come from a private server-side launcher."""
    return create_panel_app(os.environ['RECOVERY_PANEL_KEY'],
                            os.environ['RECOVERY_PANEL_ORIGIN'],
                            Channel(os.environ['RECOVERY_CHANNEL_ROOT'],
                                    os.environ['RECOVERY_SCOPE_ID']))
