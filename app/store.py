import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .errors import DomainError
from .manifest import canonical

SCHEMA = '''
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    generated_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sources (
    run_id TEXT NOT NULL REFERENCES runs(id),
    name TEXT NOT NULL,
    raw BLOB NOT NULL,
    PRIMARY KEY (run_id, name)
);
CREATE TABLE IF NOT EXISTS active (
    slot INTEGER PRIMARY KEY CHECK (slot = 1),
    run_id TEXT NOT NULL REFERENCES runs(id)
);
CREATE TABLE IF NOT EXISTS release_orders (
    order_id TEXT PRIMARY KEY,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS release_versions (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS release_active (
    slot INTEGER PRIMARY KEY CHECK (slot = 1),
    version_id TEXT NOT NULL REFERENCES release_versions(id)
);
'''


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript(SCHEMA)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.execute('PRAGMA foreign_keys = ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    def active(self):
        with self.connect() as db:
            row = db.execute('SELECT run_id FROM active WHERE slot = 1').fetchone()
        return row[0] if row else None

    def load(self, run_id=None):
        with self.connect() as db:
            if run_id is None:
                row = db.execute('SELECT payload FROM runs JOIN active ON runs.id = active.run_id').fetchone()
            else:
                row = db.execute('SELECT payload FROM runs WHERE id = ?', (run_id,)).fetchone()
        if row is None:
            raise DomainError('RUN_NOT_FOUND', 404)
        return json.loads(row[0])

    def sources(self, run_id):
        with self.connect() as db:
            return dict(db.execute('SELECT name, raw FROM sources WHERE run_id = ?', (run_id,)))

    def history(self):
        sql = '''SELECT runs.id, generated_at, json_extract(payload, '$.shift.name'),
                        json_extract(payload, '$.manifest.inputs.events.sha256'),
                        json_extract(payload, '$.dispatch.comparison.delta'), active.run_id IS NOT NULL
                 FROM runs LEFT JOIN active ON runs.id = active.run_id
                 ORDER BY generated_at DESC, runs.id'''
        keys = ('id', 'generated_at', 'shift', 'source_sha256', 'delta', 'active')
        with self.connect() as db:
            return [dict(zip(keys, row)) for row in db.execute(sql)]

    def publish(self, snapshot, sources, expected):
        manifest = snapshot['manifest']
        run_id = manifest['id']
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT run_id FROM active WHERE slot = 1').fetchone()
            active = row[0] if row else None
            existing = db.execute('SELECT payload FROM runs WHERE id = ?', (run_id,)).fetchone()
            if existing is not None:
                stored = json.loads(existing[0])
                comparable = dict(snapshot, manifest=dict(manifest))
                for key in ('generated_at', 'elapsed_ms'):
                    comparable['manifest'][key] = stored['manifest'][key]
                if stored != comparable:
                    raise DomainError('REPLAY_DRIFT', 409)
                if active == run_id:
                    return False
            if active != expected:
                raise DomainError('STALE_RUN', 409, current=active)
            if existing is None:
                db.execute('INSERT INTO runs VALUES (?, ?, ?)',
                           (run_id, manifest['generated_at'], canonical(snapshot)))
                db.executemany('INSERT INTO sources VALUES (?, ?, ?)',
                               [(run_id, name, raw) for name, raw in sources.items()])
            db.execute('INSERT INTO active VALUES (1, ?) ON CONFLICT(slot) DO UPDATE SET run_id = excluded.run_id',
                       (run_id,))
            return existing is None

    def release_orders(self):
        with self.connect() as db:
            rows = db.execute('SELECT order_id, payload FROM release_orders ORDER BY order_id').fetchall()
        return {order_id: json.loads(payload) for order_id, payload in rows}

    def release_active(self):
        with self.connect() as db:
            row = db.execute('SELECT version_id FROM release_active WHERE slot = 1').fetchone()
        return row[0] if row else None

    def release_version(self, version_id):
        with self.connect() as db:
            row = db.execute('SELECT payload FROM release_versions WHERE id = ?', (version_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def release_history(self):
        sql = '''SELECT v.id, v.run_id, v.generated_at, a.version_id IS NOT NULL
                 FROM release_versions v LEFT JOIN release_active a ON v.id = a.version_id
                 ORDER BY v.generated_at DESC, v.id'''
        keys = ('id', 'run', 'generated_at', 'active')
        with self.connect() as db:
            return [dict(zip(keys, row)) for row in db.execute(sql)]

    def publish_release(self, expected, orders, build):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT run_id FROM active WHERE slot = 1').fetchone()
            active = row[0] if row else None
            if active != expected:
                raise DomainError('STALE_RUN', 409, current=active)
            stored = dict(db.execute('SELECT order_id, payload FROM release_orders'))
            for order_id, payload in orders.items():
                if order_id in stored:
                    if stored[order_id] != payload:
                        raise DomainError('ORDER_CONFLICT', 409, order_id=order_id)
                else:
                    db.execute('INSERT INTO release_orders VALUES (?, ?)', (order_id, payload))
                    stored[order_id] = payload
            merged = {order_id: json.loads(payload) for order_id, payload in stored.items()}
            version = build(merged)
            row = db.execute('SELECT version_id FROM release_active WHERE slot = 1').fetchone()
            previous = row[0] if row else None
            created = db.execute('SELECT 1 FROM release_versions WHERE id = ?', (version['id'],)).fetchone() is None
            if created:
                db.execute('INSERT INTO release_versions VALUES (?, ?, ?, ?)',
                           (version['id'], expected, version['generated_at'], canonical(version)))
            db.execute('INSERT INTO release_active VALUES (1, ?) '
                       'ON CONFLICT(slot) DO UPDATE SET version_id = excluded.version_id',
                       (version['id'],))
            return version, created, previous
