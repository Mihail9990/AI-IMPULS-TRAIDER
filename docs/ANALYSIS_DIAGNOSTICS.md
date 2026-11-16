# Analytics removed

The analytics worker, dependencies, API and browser overlays have been removed.
There are no SMC diagnostics or analysis installation/smoke commands in this release.

The authenticated LIVE diagnostic TXT export and tab-local browser diagnostics
remain available in Settings → Logs. They describe trading state, connection,
request/parse/render outcomes and sanitized error codes; no token, password,
account identifier or raw exception text is exported.

Older analytics environments and old diagnostic files on a VPS are not deleted
by the update. Their presence does not restart analytics.
