"""Serve the minimal new RECOVERY panel and authenticated EA mail routes.

No broker code. Start as one local worker behind the already configured HTTPS
reverse proxy; this module doesn't provision profiles, Caddy or MT5.
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, Response

from runtime.web_session import BrowserSessions

if __package__:
    from .channel import Channel
    from .web_handoff import create_handoff_app
else:
    from channel import Channel
    from web_handoff import create_handoff_app

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
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


def create_panel_app(token: str, origin: str, channel: Channel):
    """Opt-in standalone assembly for the new native branch, not a legacy mount."""
    handoff = create_handoff_app(token, channel)
    shell = (HERE / 'runtime_panel.html').read_text(encoding='utf-8')
    fragment = (HERE / 'recovery_ranges.html').read_text(encoding='utf-8')
    if shell.count('<!-- RANGE_FORM -->') != 1:
        raise ValueError('Missing RECOVERY form slot')
    shell = shell.replace('<!-- RANGE_FORM -->', fragment, 1)
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
        ('recovery_ranges.mjs', HERE / 'recovery_ranges.mjs', 'text/javascript'),
        ('session.js', REPO / 'web' / 'session.js', 'text/javascript'),
    ):
        app.add_api_route(ASSET_ROOT + name, serve_asset(source, media),
                          methods=['GET'], include_in_schema=False)

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
