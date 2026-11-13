"""Allowlisted startup reasons. Never serialize exceptions, identities or configuration."""
import json
from datetime import datetime, timezone
from pathlib import Path

REASONS = {
 'CONFIG_JSON': 'Некорректный JSON в service.json. Проверьте кавычки, запятые и скобки в Блокноте.',
 'CONFIG_READ': 'Не удалось прочитать файл настроек. Проверьте наличие файла и права доступа.',
 'CONFIG_BACKEND': 'backend должен быть mt5_direct_v1.',
 'CONFIG_FLAGS': 'Флаги должны быть JSON true/false, без кавычек.',
 'CONFIG_LOGIN': 'expected_login должен быть положительным целым числом без кавычек.',
 'CONFIG_IDENTITY': 'Проверьте terminal_path, expected_server и symbol=XAUUSD.s.',
 'CONFIG_MAGIC': 'magic должен быть положительным целым числом допустимого диапазона.',
 'WEB_PASSWORD': 'Проверьте web.password: минимум 5 локально, 32 при публичном доступе; переменная окружения имеет приоритет.',
 'WEB_CONFIG': 'Проверьте web: объект, enabled=true/false, loopback host, порт 1024–65535 и HTTPS public_origin без пути.',
 'MT5_PACKAGE': 'Пакет MetaTrader5 не загрузился в окружении установленной версии.',
 'MT5_INITIALIZE': 'MT5 initialize не выполнен. Проверьте terminal_path, запущенный терминал и права Windows.',
 'MT5_DISCONNECTED': 'Терминал или сведения счёта недоступны. Откройте MT5 и проверьте подключение.',
 'MT5_PATH_MISMATCH': 'SDK подключился к другому каталогу MT5. Проверьте terminal_path.',
 'MT5_ACCOUNT_MISMATCH': 'Счёт или сервер MT5 не совпадает с настройками. Значения намеренно не выводятся.',
 'MT5_CURRENCY_MODE': 'Счёт не удовлетворяет требованиям валюты UST/режима позиций. Проверьте тип счёта.',
 'MT5_SYMBOL': 'XAUUSD.s отсутствует или не показан в Обзоре рынка MT5.',
 'MT5_SPEC_INCOMPLETE': 'Спецификация XAUUSD.s неполная или содержит неположительные contract/tick/volume/point. Дождитесь загрузки данных MT5; запуск не разрешён этой проверкой.',
 'MT5_SPEC_LIMITS': 'Спецификация XAUUSD.s содержит недопустимые stops/freeze. Значения не подменяются.',
 'NUMERIC_DATA': 'Некорректное или неконечное числовое значение. Проверьте этап отказа; исходные значения не выводятся.',
 'MT5_SPEC_NUMERIC': 'Числовые поля данных MT5 некорректны или не конечны. Данные не подменяются.',
 'MT5_RAW_TICK_IDENTITY': 'Дополнительные поля тика MT5 некорректны. Проверка не меняет котировку.',
 'MONOTONIC_CLOCK': 'Монотонные часы процесса недопустимы. Системное время не изменялось.',
 'MT5_QUOTE': 'Котировка отсутствует или некорректна.',
 'MARKET_TIME_STATE': 'Профиль времени или ожидаемый счёт/сервер отличается от сохранённой базы. Запуск остановлен даже без открытых позиций: команды, долг, история и курсор сохранены. Для старой базы без привязки допустим только offset 0. Верните прежний профиль; не удаляйте базу и не сдвигайте сохранённые метки времени.',
 'TIME_PROFILE': 'Не выполнены требования профиля времени. Диагностика не меняет часы или допуски.',
 'PORT_BUSY': 'Порт панели занят. Не запускайте второй сервис; проверьте уже работающий процесс.',
 'LOCK_BUSY': 'Сервис, обновление или другая диагностика уже использует установку/счёт/состояние.',
 'STATE_GUARD': 'Сохранённое состояние или установленная версия несовместимы. Не удаляйте базу; нужна проверка.',
 'STARTUP_UNKNOWN': 'Неопознанный отказ запуска. Запустите «Диагностика запуска». Исходный текст скрыт для защиты секретов.',
}
_EXACT = {
 'Direct MT5 backend config is required':'CONFIG_BACKEND',
 'Explicit mt5_direct_v1 backend required':'CONFIG_BACKEND',
 'Execution flags must be JSON booleans':'CONFIG_FLAGS',
 'Explicit positive expected_login is required for a persistent service':'CONFIG_LOGIN',
 'expected_login must be a positive integer or null for read-only inspection':'CONFIG_LOGIN',
 'Explicit expected_server and symbol are required for execution':'CONFIG_IDENTITY',
 'Explicit terminal path, server and XAUUSD.s required':'CONFIG_IDENTITY',
 'Invalid strategy magic':'CONFIG_MAGIC',
 'Install the Windows MetaTrader5 requirements in this Python environment':'MT5_PACKAGE',
 'MT5 initialize failed':'MT5_INITIALIZE',
 'MT5 terminal unavailable/disconnected':'MT5_DISCONNECTED',
 'Unexpected MT5 terminal path':'MT5_PATH_MISMATCH',
 'Unexpected MT5 account/server':'MT5_ACCOUNT_MISMATCH',
 'MT5 account changed':'MT5_ACCOUNT_MISMATCH',
 'UST hedging account required':'MT5_CURRENCY_MODE',
 'XAUUSD.s must already be visible in Market Watch':'MT5_SYMBOL',
 'Invalid executable quote':'MT5_QUOTE',
 'Incomplete/nonpositive live symbol specification':'MT5_SPEC_INCOMPLETE',
 'Invalid stop/freeze levels':'MT5_SPEC_LIMITS',
 'Invalid decimal':'NUMERIC_DATA',
 'Non-finite decimal':'NUMERIC_DATA',
 'Invalid native tick identity':'MT5_RAW_TICK_IDENTITY',
 'INVALID_MONOTONIC_CLOCK':'MONOTONIC_CLOCK',
 'Web server requires loopback':'WEB_CONFIG', 'Invalid loopback port':'WEB_CONFIG',
 'web must be a configuration object':'WEB_CONFIG', 'web.enabled must be boolean':'WEB_CONFIG',
 'web должен быть объектом настроек.':'WEB_CONFIG',
 'Managed release is not current':'STATE_GUARD',
 'Managed state missing or moved; automatic reset forbidden':'STATE_GUARD',
}

def reason(exc):
    from .web_auth import WebAuthError
    from .time_profile import TimeProfileError
    from .storage import DurabilityError
    from .market_time_state import MarketTimeStateError
    if isinstance(exc,json.JSONDecodeError): return 'CONFIG_JSON'
    if isinstance(exc,WebAuthError): return 'WEB_PASSWORD'
    if isinstance(exc,MarketTimeStateError): return 'MARKET_TIME_STATE'
    if isinstance(exc,TimeProfileError): return 'TIME_PROFILE'
    if isinstance(exc,DurabilityError): return 'STATE_GUARD'
    if isinstance(exc,OSError) and (exc.errno in (98,10048) or getattr(exc,'winerror',None)==10048): return 'PORT_BUSY'
    # Equality to fixed literals only; the raw text is never included in output.
    return _EXACT.get(str(exc),'STARTUP_UNKNOWN')

# Match fixed code locations only, never serialize traceback text, paths or locals.
_STAGE_FRAMES = {
    ('mt5_adapter.py', 'connect'): 'MT5_CONNECT',
    ('mt5_adapter.py', '_context'): 'MT5_CONTEXT',
    ('broker.py', 'load'): 'SYMBOL_SPEC',
    ('raw_market_time.py', 'observe'): 'RAW_TICK',
    ('market_time_state.py', 'bind_market_time_state'): 'STATE_BINDING',
    ('storage.py', '__init__'): 'STATE_OPEN',
    ('engine.py', '__init__'): 'CONTROLLER_INIT',
    ('web_control.py', '__init__'): 'PANEL_INIT',
    ('web_auth.py', 'resolve_password'): 'WEB_AUTH',
    ('web_auth.py', 'validate_password'): 'WEB_AUTH',
}
STAGES = frozenset(_STAGE_FRAMES.values()) | {'UNKNOWN'}

def failure_stage(exc):
    result = 'UNKNOWN'
    tb = exc.__traceback__
    while tb is not None:
        code = tb.tb_frame.f_code
        # Basename is compared only; it is never returned to the report.
        result = _STAGE_FRAMES.get((Path(code.co_filename).name, code.co_name), result)
        tb = tb.tb_next
    return result

def write_failure(config_path,exc):
    code=reason(exc)
    names={ValueError:'ValueError',OSError:'OSError',TimeoutError:'TimeoutError',FileNotFoundError:'FileNotFoundError',PermissionError:'PermissionError',TypeError:'TypeError',RuntimeError:'RuntimeError',json.JSONDecodeError:'JSONDecodeError'}
    label=names.get(type(exc),'Exception')
    message='STARTUP '+code+'\nSTAGE '+failure_stage(exc)+'\nТип: ('+label+')\n'+REASONS[code]+'\n'
    path=Path(config_path).resolve().parent/'startup-error.txt'
    try: path.write_text(datetime.now(timezone.utc).isoformat()+'\n'+message,encoding='utf-8-sig')
    except OSError: pass
    return message
