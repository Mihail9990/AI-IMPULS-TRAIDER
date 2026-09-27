"""SQLite authoritative persistence for trading state and durable work queues."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
from typing import Callable


SCHEMA_VERSION = 1


def database_path(legacy_path: str | Path) -> Path:
    path = Path(legacy_path)
    return path.with_suffix(".sqlite3") if path.suffix else Path(str(path) + ".sqlite3")


class StateStore:
    """One transactional snapshot plus indexed durable facts/commands/outbox.

    The snapshot is the authoritative representation consumed by ``CycleState``. Indexed tables
    are written in the same transaction and provide uniqueness/audit boundaries without creating
    a second independently mutable state model.
    """

    def __init__(self, legacy_path: str | Path):
        self.legacy_path = Path(legacy_path)
        self.path = database_path(legacy_path)

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
        self._schema(connection)
        return connection

    @staticmethod
    def _schema(connection: sqlite3.Connection) -> None:
        # Every schema change is applied here before a snapshot is read.  Version 1 is the first
        # SQLite format; keeping an explicit gate prevents an older binary from silently writing
        # a database created by a newer release.
        existing = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='metadata'"
        ).fetchone()
        if existing:
            row = connection.execute(
                "SELECT value FROM metadata WHERE key='schema_version'"
            ).fetchone()
            if row and int(row[0]) > SCHEMA_VERSION:
                raise RuntimeError(
                    f"State database schema {row[0]} is newer than supported {SCHEMA_VERSION}"
                )
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS state_snapshot (
                singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                revision INTEGER NOT NULL,
                payload TEXT NOT NULL,
                committed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS broker_facts (
                identity TEXT PRIMARY KEY,
                cycle_id INTEGER,
                attempt_id INTEGER,
                deal_id TEXT,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS broker_executions (
                identity TEXT PRIMARY KEY,
                deal_id TEXT NOT NULL,
                source TEXT NOT NULL,
                execution_time TEXT,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS durable_commands (
                identity TEXT PRIMARY KEY,
                cycle_id INTEGER,
                attempt_id INTEGER,
                kind TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS notification_outbox (
                identity TEXT PRIMARY KEY,
                payload TEXT NOT NULL
            );
        """)
        connection.execute(
            "INSERT INTO metadata(key,value) VALUES('schema_version',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )

    def load(self) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM state_snapshot WHERE singleton=1"
            ).fetchone()
            if row is not None:
                return json.loads(row[0])
            migrated = self._legacy_payload()
            if migrated is not None:
                self.save(migrated, connection=connection, migration=True)
                return migrated
            return None

    def _legacy_payload(self) -> dict | None:
        if not self.legacy_path.is_file():
            return None
        try:
            raw = self.legacy_path.read_bytes()
            payload = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        payload.setdefault("storage_migration", {
            "source": self.legacy_path.name,
            "sha256": hashlib.sha256(raw).hexdigest(),
        })
        return payload

    def save(self, payload: dict, *, connection: sqlite3.Connection | None = None,
             migration: bool = False, fault: Callable[[str], None] | None = None) -> None:
        own = connection is None
        db = connection or self._connect()
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        try:
            db.execute("BEGIN IMMEDIATE")
            if fault:
                fault("after_begin")
            row = db.execute(
                "SELECT revision FROM state_snapshot WHERE singleton=1"
            ).fetchone()
            revision = (int(row[0]) + 1) if row else 1
            db.execute(
                "INSERT INTO state_snapshot(singleton,revision,payload) VALUES(1,?,?) "
                "ON CONFLICT(singleton) DO UPDATE SET revision=excluded.revision, "
                "payload=excluded.payload, committed_at=CURRENT_TIMESTAMP",
                (revision, encoded),
            )
            self._replace_indexes(db, payload)
            if migration:
                migration_data = payload.get("storage_migration", {})
                db.execute(
                    "INSERT INTO metadata(key,value) VALUES('json_migration',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (json.dumps(migration_data, sort_keys=True),),
                )
            if fault:
                fault("before_commit")
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
        finally:
            if own:
                db.close()

    @staticmethod
    def _replace_indexes(db: sqlite3.Connection, payload: dict) -> None:
        db.execute("DELETE FROM broker_facts")
        db.execute("DELETE FROM broker_executions")
        for record in payload.get("deal_history", []):
            identity = str(record.get("deal_id") or "")
            if not identity:
                continue
            db.execute(
                "INSERT INTO broker_facts(identity,cycle_id,attempt_id,deal_id,payload) "
                "VALUES(?,?,?,?,?)",
                (identity, record.get("cycle_id"), record.get("attempt_id"), identity,
                 json.dumps(record, ensure_ascii=False, sort_keys=True)),
            )
            executions = list(record.get("partial_closes", []))
            if record.get("close_event_id"):
                executions.append({
                    "event_id": record.get("close_event_id"),
                    "source": record.get("close_source"),
                    "fill": record.get("close_level"),
                    "size": record.get("close_size"),
                    "execution_time": record.get("close_execution_time"),
                })
            for execution in executions:
                event_id = str(execution.get("event_id") or "")
                if not event_id:
                    continue
                db.execute(
                    "INSERT INTO broker_executions(identity,deal_id,source,execution_time,payload) "
                    "VALUES(?,?,?,?,?)",
                    (event_id, identity, str(execution.get("source") or ""),
                     execution.get("execution_time"),
                     json.dumps(execution, ensure_ascii=False, sort_keys=True)),
                )
        db.execute("DELETE FROM durable_commands")
        for direction in ("long", "short"):
            leg = payload.get(direction) or {}
            for kind, active in (
                ("TRIGGER_CREATE", leg.get("pending_trigger_create")),
                ("TRIGGER_CANCEL", leg.get("pending_trigger_cancel_unknown")),
                ("MARKET_OPEN", leg.get("pending_market_kind")),
                ("PROTECTION_UPDATE", leg.get("protection_sent_stop") is not None
                 and leg.get("protection_readback") != "ПОДТВЕРЖДЕНО"),
                ("RACE_CLOSE", leg.get("pending_race_close_unknown")
                 or leg.get("pending_race_close_reference")),
            ):
                if active:
                    identity = f"{payload.get('cycle_id', 0)}:{direction}:{kind}"
                    db.execute(
                        "INSERT INTO durable_commands(identity,cycle_id,attempt_id,kind,payload) "
                        "VALUES(?,?,?,?,?)",
                        (identity, payload.get("cycle_id"), payload.get("active_attempt_id"), kind,
                         json.dumps(leg, ensure_ascii=False, sort_keys=True)),
                    )
        if payload.get("pending_close_direction"):
            identity = f"{payload.get('cycle_id', 0)}:MARKET_CLOSE"
            db.execute(
                "INSERT INTO durable_commands(identity,cycle_id,attempt_id,kind,payload) "
                "VALUES(?,?,?,?,?)",
                (identity, payload.get("cycle_id"), payload.get("active_attempt_id"),
                 "MARKET_CLOSE", encoded_subset(payload, "pending_close")),
            )
        db.execute("DELETE FROM notification_outbox")
        for report in payload.get("report_outbox", []):
            db.execute(
                "INSERT INTO notification_outbox(identity,payload) VALUES(?,?)",
                (str(report.get("id") or report.get("key")),
                 json.dumps(report, ensure_ascii=False, sort_keys=True)),
            )

    def backup(self, destination: str | Path) -> None:
        """Create a consistent online backup, including committed WAL content."""
        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as source, sqlite3.connect(target) as output:
            source.backup(output)


def encoded_subset(payload: dict, prefix: str) -> str:
    return json.dumps(
        {key: value for key, value in payload.items() if key.startswith(prefix)},
        ensure_ascii=False, sort_keys=True,
    )


class WriterLock:
    """Process-lifetime advisory lock preventing two Bot writers for one database."""

    def __init__(self, state_path: str | Path):
        import fcntl
        self.path = Path(str(database_path(state_path)) + ".lock")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self.fd)
            self.fd = None
            raise RuntimeError(f"Another trading instance owns state database {database_path(state_path)}")

    def close(self) -> None:
        if getattr(self, "fd", None) is not None:
            os.close(self.fd)
            self.fd = None

    def __del__(self):  # pragma: no cover - interpreter shutdown path
        self.close()
