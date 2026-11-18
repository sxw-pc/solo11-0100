import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .migrations import apply_migrations

SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS batch(id TEXT PRIMARY KEY,config_hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS subject(id TEXT PRIMARY KEY,batch_id TEXT NOT NULL REFERENCES batch(id));
CREATE TABLE IF NOT EXISTS source(id INTEGER PRIMARY KEY,path TEXT NOT NULL,sha256 TEXT NOT NULL UNIQUE,raw BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS event(id INTEGER PRIMARY KEY,source_id INTEGER NOT NULL REFERENCES source(id),line_no INTEGER NOT NULL,identity TEXT NOT NULL UNIQUE,subject TEXT NOT NULL,kind TEXT NOT NULL,time TEXT NOT NULL,payload TEXT NOT NULL,sha256 TEXT NOT NULL,UNIQUE(source_id,line_no));
CREATE TABLE IF NOT EXISTS issue(id INTEGER PRIMARY KEY,code TEXT NOT NULL,detail TEXT NOT NULL,event_id INTEGER REFERENCES event(id));
CREATE TABLE IF NOT EXISTS decision(subject TEXT PRIMARY KEY,status TEXT NOT NULL,details TEXT NOT NULL,rule_version TEXT NOT NULL,run_id INTEGER NOT NULL REFERENCES run(id));
CREATE TABLE IF NOT EXISTS evidence(subject TEXT NOT NULL REFERENCES subject(id),event_id INTEGER NOT NULL REFERENCES event(id),rule_version TEXT NOT NULL,run_id INTEGER NOT NULL REFERENCES run(id),PRIMARY KEY(subject,event_id,rule_version));
CREATE TABLE IF NOT EXISTS run(id INTEGER PRIMARY KEY,started_at TEXT NOT NULL,completed_at TEXT,status TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS lineage(parent_box TEXT NOT NULL,child_box TEXT NOT NULL,quantity INTEGER NOT NULL,kind TEXT NOT NULL,event_id INTEGER NOT NULL REFERENCES event(id),PRIMARY KEY(parent_box,child_box,event_id));
CREATE TABLE IF NOT EXISTS privacy_case(id TEXT PRIMARY KEY,payload TEXT NOT NULL);
"""
LEDGER_EFFECTIVE_STATES = ('committed', 'proposed')
LEDGER_ALL_STATES = LEDGER_EFFECTIVE_STATES + ('blocked', 'aborted')


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def connect(path):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(p, check_same_thread=False, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    db.execute('PRAGMA busy_timeout=30000')
    db.executescript(SCHEMA)
    apply_migrations(db)
    return db


@contextmanager
def immediate_transaction(db):
    """Serialize writers: BEGIN IMMEDIATE acquires the write lock up front.

    Concurrent proposal workers therefore queue instead of racing on the
    release identity unique index, and the whole review + ledger commit
    sequence sees one stable history boundary.
    """
    db.execute('BEGIN IMMEDIATE')
    try:
        yield db
        db.execute('COMMIT')
    except BaseException:
        db.execute('ROLLBACK')
        raise


def append_event(db, release_id, phase, detail):
    db.execute('INSERT INTO release_event(release_id,phase,occurred_at,detail) VALUES (?,?,?,?)',
               (release_id, phase, now_iso(), detail))


def find_ledger_identity(db, identity):
    return db.execute('SELECT * FROM release_ledger WHERE release_id=?', (identity,)).fetchone()


def find_ledger_by_tuple(db, case_id, audience, contract, source_hash, kind,
                         members_json, amounts_json):
    return db.execute(
        'SELECT * FROM release_ledger WHERE case_id=? AND audience=? AND contract=? '
        'AND source_hash=? AND kind=? AND members_json=? AND amounts_json=?',
        (case_id, audience, contract, source_hash, kind, members_json, amounts_json),
    ).fetchone()


def effective_history(db, case_id, audience):
    """All externally visible disclosures bounding one audience's inference."""
    return [dict(r) for r in db.execute(
        "SELECT * FROM release_ledger WHERE case_id=? AND audience=? "
        "AND status IN ('committed','proposed') ORDER BY created_at,release_id",
        (case_id, audience))]


def insert_ledger(db, row):
    cols = ('release_id', 'case_id', 'external_ref', 'audience', 'contract',
            'source_version', 'source_hash', 'kind', 'members_json',
            'amounts_json', 'content_json', 'content_sha256', 'json_path',
            'md_path', 'status', 'reason_code', 'reason_detail_json',
            'evidence_json', 'is_prior', 'created_at', 'updated_at')
    db.execute(
        'INSERT INTO release_ledger(' + ','.join(cols) + ') VALUES ('
        + ','.join('?' for _ in cols) + ')', tuple(row[c] for c in cols))


def update_ledger(db, release_id, **fields):
    fields['updated_at'] = now_iso()
    sets = ','.join(f'{k}=?' for k in fields)
    db.execute(f'UPDATE release_ledger SET {sets} WHERE release_id=?',
               (*fields.values(), release_id))


def record_access(db, release_id, audience, contract, detail):
    db.execute(
        'INSERT INTO release_access(release_id,audience,contract,accessed_at,detail) '
        'VALUES (?,?,?,?,?)', (release_id, audience, contract, now_iso(), detail))


def ledger_rows(db):
    return [dict(r) for r in db.execute(
        'SELECT release_id,external_ref,audience,contract,source_version,kind,'
        'status,reason_code,is_prior,created_at,updated_at,json_path,md_path '
        'FROM release_ledger ORDER BY created_at,release_id')]
