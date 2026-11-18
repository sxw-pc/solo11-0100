import sqlite3
from pathlib import Path

SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS batch(id TEXT PRIMARY KEY,config_hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS subject(id TEXT PRIMARY KEY,batch_id TEXT NOT NULL REFERENCES batch(id));
CREATE TABLE IF NOT EXISTS source(
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL UNIQUE,
    raw BLOB,
    length INTEGER NOT NULL DEFAULT 0,
    version INTEGER NOT NULL DEFAULT 1,
    created_generation INTEGER NOT NULL DEFAULT 0,
    storage TEXT NOT NULL DEFAULT 'blob'
);
CREATE TABLE IF NOT EXISTS event(id INTEGER PRIMARY KEY,source_id INTEGER NOT NULL REFERENCES source(id),line_no INTEGER NOT NULL,identity TEXT NOT NULL UNIQUE,subject TEXT NOT NULL,kind TEXT NOT NULL,time TEXT NOT NULL,payload TEXT NOT NULL,sha256 TEXT NOT NULL,UNIQUE(source_id,line_no));
CREATE TABLE IF NOT EXISTS issue(id INTEGER PRIMARY KEY,code TEXT NOT NULL,detail TEXT NOT NULL,event_id INTEGER REFERENCES event(id));
CREATE TABLE IF NOT EXISTS decision(subject TEXT PRIMARY KEY,status TEXT NOT NULL,details TEXT NOT NULL,rule_version TEXT NOT NULL,run_id INTEGER NOT NULL REFERENCES run(id));
CREATE TABLE IF NOT EXISTS evidence(subject TEXT NOT NULL REFERENCES subject(id),event_id INTEGER NOT NULL REFERENCES event(id),rule_version TEXT NOT NULL,run_id INTEGER NOT NULL REFERENCES run(id),PRIMARY KEY(subject,event_id,rule_version));
CREATE TABLE IF NOT EXISTS run(id INTEGER PRIMARY KEY,started_at TEXT NOT NULL,completed_at TEXT,status TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS lineage(parent_box TEXT NOT NULL,child_box TEXT NOT NULL,quantity INTEGER NOT NULL,kind TEXT NOT NULL,event_id INTEGER NOT NULL REFERENCES event(id),PRIMARY KEY(parent_box,child_box,event_id));
CREATE TABLE IF NOT EXISTS archive_case(id TEXT PRIMARY KEY,payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS archive_state(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS archive_generation(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    status TEXT NOT NULL,
    representation TEXT NOT NULL,
    block_bytes INTEGER NOT NULL DEFAULT 64,
    plan TEXT NOT NULL,
    created_at TEXT NOT NULL,
    published_at TEXT,
    retired_at TEXT,
    note TEXT
);
CREATE TABLE IF NOT EXISTS archive_reader(
    id TEXT PRIMARY KEY,
    generation INTEGER NOT NULL,
    pid INTEGER NOT NULL,
    opened_at TEXT NOT NULL,
    closed_at TEXT
);
CREATE TABLE IF NOT EXISTS source_manifest(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL REFERENCES source(id),
    generation_id INTEGER NOT NULL REFERENCES archive_generation(id),
    version INTEGER NOT NULL,
    representation TEXT NOT NULL,
    block_bytes INTEGER NOT NULL DEFAULT 0,
    length INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    prefix BLOB NOT NULL,
    suffix BLOB NOT NULL,
    block_count INTEGER NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(source_id,generation_id)
);
CREATE TABLE IF NOT EXISTS manifest_block(
    manifest_id INTEGER NOT NULL REFERENCES source_manifest(id),
    ordinal INTEGER NOT NULL,
    block_sha TEXT NOT NULL,
    length INTEGER NOT NULL,
    PRIMARY KEY(manifest_id,ordinal)
);
CREATE TABLE IF NOT EXISTS generation_manifest(
    generation_id INTEGER NOT NULL REFERENCES archive_generation(id),
    source_id INTEGER NOT NULL REFERENCES source(id),
    manifest_id INTEGER NOT NULL REFERENCES source_manifest(id),
    PRIMARY KEY(generation_id,source_id)
);
CREATE INDEX IF NOT EXISTS manifest_block_sha ON manifest_block(block_sha);
CREATE INDEX IF NOT EXISTS generation_manifest_source ON generation_manifest(source_id);
CREATE INDEX IF NOT EXISTS source_manifest_source ON source_manifest(source_id);
"""

_SOURCE_NEW = """
CREATE TABLE source__mig(
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL UNIQUE,
    raw BLOB,
    length INTEGER NOT NULL DEFAULT 0,
    version INTEGER NOT NULL DEFAULT 1,
    created_generation INTEGER NOT NULL DEFAULT 0,
    storage TEXT NOT NULL DEFAULT 'blob'
)
"""

_REQUIRED_SOURCE_COLUMNS = {'length', 'version', 'created_generation', 'storage'}


def _table_columns(db, table):
    return {row[1] for row in db.execute(f'PRAGMA table_info({table})')}


def _migrate_source_table(db):
    """Upgrade a pre-archive ``source`` table in place.

    The first version required ``raw BLOB NOT NULL`` and lacked storage
    metadata.  Rebuilding the table keeps every row id, so ``event.source_id``,
    ``evidence`` and report references stay valid.  Foreign keys are disabled
    only for the rebuild and the whole database is checked afterwards.
    """
    columns = _table_columns(db, 'source')
    if not columns or _REQUIRED_SOURCE_COLUMNS <= columns:
        return False
    db.execute('PRAGMA foreign_keys=OFF')
    try:
        db.execute('BEGIN IMMEDIATE')
        try:
            db.execute('DROP TABLE IF EXISTS source__mig')
            db.execute(_SOURCE_NEW)
            db.execute(
                'INSERT INTO source__mig(id,path,sha256,raw,length,version,created_generation,storage) '
                "SELECT id,path,sha256,raw,COALESCE(length(raw),0),1,0,"
                "CASE WHEN raw IS NULL THEN 'blocks' ELSE 'blob' END FROM source"
            )
            db.execute('DROP TABLE source')
            db.execute('ALTER TABLE source__mig RENAME TO source')
            db.execute('COMMIT')
        except Exception:
            db.execute('ROLLBACK')
            raise
    finally:
        db.execute('PRAGMA foreign_keys=ON')
    return True


def connect(path):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(p, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA busy_timeout=30000')
    db.execute('PRAGMA foreign_keys=ON')
    _migrate_source_table(db)
    db.executescript(SCHEMA)
    try:
        db.execute('PRAGMA journal_mode=WAL')
    except sqlite3.DatabaseError:
        pass
    return db
