"""Local web credential policy; never returns credentials in diagnostic views."""
import os
from urllib.parse import urlsplit

class WebAuthError(ValueError):pass


def validate_password(password,*,host='127.0.0.1',public_origin=None,external=False):
    if public_origin is not None:
        if not isinstance(public_origin,str):raise WebAuthError('web.public_origin должен быть HTTPS origin.')
        p=urlsplit(public_origin)
        if p.scheme!='https' or not p.hostname or p.username or p.password or p.path or p.query or p.fragment:raise WebAuthError('web.public_origin должен быть HTTPS origin без пути и учётных данных.')
    minimum=5 if host in {'127.0.0.1','::1'} and public_origin is None and not external else 32
    if not isinstance(password,str) or not password.strip() or len(password)<minimum:
        raise WebAuthError(f'Настройки (Блокнот): задайте web.password, минимум {minimum} символов. TRADFI_WEB_PASSWORD имеет приоритет, если задана.')
    try:password.encode('utf-8')
    except UnicodeError:raise WebAuthError('web.password содержит недопустимые Unicode-символы.') from None
    return password


def resolve_password(web,environ=None):
    if not isinstance(web,dict):raise WebAuthError('web должен быть объектом настроек.')
    if 'password' in web and not isinstance(web['password'],str):raise WebAuthError('web.password должен быть строкой в кавычках.')
    env=os.environ if environ is None else environ
    value=env['TRADFI_WEB_PASSWORD'] if 'TRADFI_WEB_PASSWORD' in env else web.get('password','')
    external=any(bool(v) for k,v in web.items() if 'proxy' in str(k).lower() or 'external' in str(k).lower())
    return validate_password(value,host=web.get('host','127.0.0.1'),public_origin=web.get('public_origin'),external=external)


def redact(value):
    """Recursive key-based redaction for legacy exports; web logs use stricter allowlists."""
    if isinstance(value,dict):
        return {k:('[REDACTED]' if any(part in str(k).lower() for part in ('password','passwd','secret','token','credential','authorization','cookie','api_key','apikey')) else redact(v)) for k,v in value.items()}
    if isinstance(value,list):return [redact(v) for v in value]
    return value
