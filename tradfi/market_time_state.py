"""Durable market-time interpretation binding, checked before any controller starts.

A legacy database used native timestamps as UTC (offset zero). Never reinterpret
its candle IDs, pending intents, debt, or execution history by silently changing that
contract. Binding is metadata only; this module never migrates trading state.
"""
import json
from .time_profile import validate_market_time_offset


class MarketTimeStateError(ValueError):
    """The configured market clock would reinterpret an existing database."""


def bind_market_time_state(store, config):
    """Atomically bind this DB to one fixed offset and expected source identity.

    Even an idle bound DB cannot switch profiles: retained history and consumed
    commands may still refer to its original timestamp interpretation.
    """
    offset = validate_market_time_offset(config)
    wanted = dict(version=1, market_time_offset_seconds=offset,
                  expected_server=config.get('expected_server'),
                  expected_login=config.get('expected_login'))
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
            if existing != wanted:
                raise MarketTimeStateError(
                    'Market-time offset or expected account/server changed for this database. '
                    'Startup blocked, including idle state, to preserve commands, debt, history and timestamps. '
                    'Restore the original profile; no automatic timestamp migration is allowed.')
        else:
            # Any durable rows count, not just active positions or a controller snapshot.
            has_state = any(db.execute('SELECT 1 FROM "' + table.replace('"', '""') + '" LIMIT 1').fetchone()
                            for table in tables if table != 'market_time_binding' and not table.startswith('sqlite_'))
            if has_state and offset != 0:
                raise MarketTimeStateError(
                    'Legacy database has no market-time binding and is presumed native UTC (offset 0). '
                    'Nonzero offset blocked to preserve commands, debt and execution history. '
                    'Restore offset 0; no automatic timestamp migration is allowed.')
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
