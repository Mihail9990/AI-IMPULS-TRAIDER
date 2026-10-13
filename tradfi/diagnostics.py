"""Offline export from SQLite. No gateway calls, secrets or trading mutations."""
import argparse
import json
from pathlib import Path
import sqlite3


def export(database, destination):
    source = Path(database).resolve()
    if not source.is_file():
        raise ValueError('State database does not exist')
    with sqlite3.connect(source.as_uri() + '?mode=ro', uri=True) as db:
        row = db.execute('SELECT payload FROM snapshot WHERE id=1').fetchone()
        state = json.loads(row[0]) if row else {}
        state['account'] = '<redacted>'
        audit = [{'seq': seq, 'event': event, 'detail': json.loads(payload)} for seq, event, payload in
                 db.execute('SELECT seq,event,payload FROM audit ORDER BY seq DESC LIMIT 500')]
    Path(destination).write_text(json.dumps({'state': state, 'audit': audit}, indent=2), encoding='utf-8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database'); parser.add_argument('destination')
    args = parser.parse_args(); export(args.database, args.destination)
