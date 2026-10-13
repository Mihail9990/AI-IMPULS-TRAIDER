"""Local Telegram credentials; never merge secrets into trading configuration."""
import json
import os
from pathlib import Path
import re


KEYS = ('TRADFI_TELEGRAM_TOKEN', 'TRADFI_TELEGRAM_CHAT_ID', 'TRADFI_TELEGRAM_USER_ID')


class CredentialsError(ValueError):
    """Safe to display: deliberately contains no input values or paths."""


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError()
        result[key] = value
    return result


def load_telegram_credentials(file=None, *, config_path=None):
    """Explicit environment values win per field; unset user ID defaults to chat ID."""
    try:
        values = {}
        if file is not None:
            if not isinstance(file, str) or not file.strip():
                raise ValueError()
            # Expand Windows syntax explicitly, including when tested on Linux.
            def expand(match):
                value = os.environ.get(match.group(1))
                if not value:
                    raise ValueError()
                return value
            path = Path(re.sub(r'%([^%]+)%', expand, file))
            if not path.is_absolute():
                path = (Path(config_path).resolve().parent if config_path else Path.cwd()) / path
            with path.open('r', encoding='utf-8-sig') as handle:
                raw = handle.read(16385)
            if len(raw) > 16384:
                raise ValueError()
            values = json.loads(raw, object_pairs_hook=_unique_object)
            if not isinstance(values, dict) or set(values) - set(KEYS):
                raise ValueError()
            if any(not isinstance(v, str) for v in values.values()):
                raise ValueError()
        values.update({k: os.environ[k] for k in KEYS if k in os.environ})
        token, chat = (values.get(k, '') for k in KEYS[:2])
        user = values.get(KEYS[2], chat)
        if not token or any(c.isspace() for c in token):
            raise ValueError()
        if not re.fullmatch(r'-?[0-9]+', chat) or int(chat) == 0:
            raise ValueError()
        if not re.fullmatch(r'[0-9]+', user) or int(user) <= 0:
            raise ValueError()
        return token, chat, user
    except Exception:
        raise CredentialsError('Telegram credentials unavailable or invalid; check local file and environment.') from None
