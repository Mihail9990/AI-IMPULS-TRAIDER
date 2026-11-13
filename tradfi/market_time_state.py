"""Durable market-time interpretation binding, checked before any controller starts.

A legacy database used native timestamps as UTC (offset zero). Never reinterpret
its candle IDs, pending intents, debt, or execution history by silently changing that
contract. Binding is metadata only; explicit migration is implemented separately in market_time_migration.
"""
import json
from .time_profile import validate_market_time_offset, validate_market_time_mode, RAW_MARKET_MODE


class MarketTimeStateError(ValueError):
    """The configured market clock would reinterpret an existing database."""


def bind_market_time_state(store, config):
    """Atomically bind this DB to one fixed offset and expected source identity.

    Even an idle bound DB cannot switch profiles: retained history and consumed
    commands may still refer to its original timestamp interpretation.
    """
    offset = validate_market_time_offset(config)
    mode = validate_market_time_mode(config)
    wanted = dict(version=1, market_time_offset_seconds=offset,
                  expected_server=config.get('expected_server'),
                  expected_login=config.get('expected_login'))
    if mode == RAW_MARKET_MODE:
        wanted.update(version=2, market_time_mode=mode, timestamp_semantics='BROKER_RAW')
    db = store.db
    try:
        db.execute('BEGIN IMMEDIATE')
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        row = (db.execute('SELECT payload FROM market_time_binding WHERE id=1').fetchone()
               if 'market_time_binding' in tables else None)
        if row is not None:
            try:
                existing = json.loads(row[0])
            except (ValueError, TypeError):
                raise MarketTimeStateError('Invalid persisted market-time binding; state preserved') from None
            authorized = (existing.get('migration_authority') == 'panel_explicit_v1'
                          and existing.get('market_time_mode') == RAW_MARKET_MODE)
            comparison = {k: v for k, v in existing.items() if k != 'migration_authority'} if authorized else existing
            if comparison != wanted:
                raise MarketTimeStateError(
                    'Market-time mode, offset or expected account/server changed for this database. '
                    'Startup blocked, including idle state, to preserve commands, debt, history and timestamps. '
                    'Restore the original profile; no automatic timestamp migration is allowed.')
        else:
            # Any durable rows count, not just active positions or a controller snapshot.
            has_state = any(db.execute('SELECT 1 FROM "' + table.replace('"', '""') + '" LIMIT 1').fetchone()
                            for table in tables if table != 'market_time_binding' and not table.startswith('sqlite_'))
            if has_state and (offset != 0 or mode == RAW_MARKET_MODE):
                raise MarketTimeStateError(
                    'Legacy database has no market-time binding and is presumed native UTC (offset 0). '
                    'Nonzero offset or raw time mode blocked to preserve commands, debt and execution history. '
                    'Restore offset 0 in the original UTC mode; no automatic timestamp migration is allowed.')
            db.execute('CREATE TABLE IF NOT EXISTS market_time_binding '
                       '(id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)')
            db.execute('INSERT INTO market_time_binding VALUES(1,?)',
                       (json.dumps(wanted, sort_keys=True, allow_nan=False),))
        db.execute('COMMIT')
    except BaseException:
        if db.in_transaction:
            db.execute('ROLLBACK')
        raise
    return wanted


def resolve_market_time_config(store, config):
    """Recover the persisted clock contract without asking for a manual offset.

    Explicit options remain demands and are checked by bind_market_time_state.
    Missing options never reinterpret existing timestamps or monetary state.
    """
    result = dict(config)
    table = store.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='market_time_binding'").fetchone()
    row = store.db.execute('SELECT payload FROM market_time_binding WHERE id=1').fetchone() if table else None
    if not row:
        # Fresh production installs use raw MT5 time. Unbound legacy rows retain
        # their old UTC-zero interpretation until the explicit panel migration.
        if ('market_time_mode' not in config and 'market_time_offset_seconds' not in config
                and config.get('time_profile') == 'bybit_live6_time_v1'):
            tables = [r[0] for r in store.db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            has_state = any(store.db.execute('SELECT 1 FROM "' + name.replace('"', '""') + '" LIMIT 1').fetchone()
                            for name in tables if not name.startswith('sqlite_') and name != 'market_time_binding')
            result['market_time_mode'] = 'fixed_utc_v1' if has_state else RAW_MARKET_MODE
        return result
    try:
        binding = json.loads(row[0])
    except (ValueError, TypeError):
        raise MarketTimeStateError('Invalid persisted market-time binding; state preserved') from None
    if binding.get('migration_authority') != 'panel_explicit_v1':
        if 'market_time_mode' not in config:
            result['market_time_mode'] = binding.get('market_time_mode', 'fixed_utc_v1')
        if 'market_time_offset_seconds' not in config:
            result['market_time_offset_seconds'] = binding.get('market_time_offset_seconds', 0)
        return result
    if (binding.get('market_time_mode') != RAW_MARKET_MODE
            or binding.get('market_time_offset_seconds') != 0
            or binding.get('timestamp_semantics') != 'BROKER_RAW'
            or any(binding.get(k) != config.get(k) for k in ('expected_server', 'expected_login'))
            or config.get('market_time_mode', RAW_MARKET_MODE) != RAW_MARKET_MODE):
        raise MarketTimeStateError('Explicit migrated market-time binding conflicts with configured source or mode')
    result.update(market_time_mode=RAW_MARKET_MODE, market_time_offset_seconds=0)
    return result
