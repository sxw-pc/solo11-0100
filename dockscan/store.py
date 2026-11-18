import sqlite3
from pathlib import Path

SCHEMA="""
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
CREATE TABLE IF NOT EXISTS unclaimed_case(id TEXT PRIMARY KEY,payload TEXT NOT NULL);

-- Frozen custody chain, rebuilt from the fixture and extended only by signed runtime handovers.
CREATE TABLE IF NOT EXISTS unclaimed_custody(
    id INTEGER PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES unclaimed_case(id),
    seq INTEGER NOT NULL,
    custody_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    at TEXT NOT NULL,
    from_holder TEXT,
    to_holder TEXT,
    by_holder TEXT,
    seal TEXT NOT NULL,
    signed_by TEXT NOT NULL,
    pallet TEXT,
    origin TEXT NOT NULL DEFAULT 'fixture',
    event_id INTEGER REFERENCES event(id),
    UNIQUE(case_id,custody_id),
    UNIQUE(case_id,seq)
);
-- The physical pallet state.  holder/status/version move only through the release engine.
CREATE TABLE IF NOT EXISTS pallet_custody(
    pallet TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    holder TEXT,
    status TEXT NOT NULL,
    seal TEXT NOT NULL,
    version INTEGER NOT NULL,
    reason TEXT,
    source_fingerprint TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    release_id TEXT
);
-- A verification is an authorization bound to one pallet version / custody fingerprint.
CREATE TABLE IF NOT EXISTS unclaimed_authorization(
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    contract TEXT NOT NULL,
    claim_id TEXT NOT NULL,
    claimant TEXT NOT NULL,
    credential_digest TEXT,
    seal TEXT NOT NULL,
    deliverer TEXT NOT NULL,
    custody_version INTEGER NOT NULL,
    custody_fingerprint TEXT NOT NULL,
    source_event_ids TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    consumed_at TEXT
);
-- One row per case is the one-time physical delivery.  case_id is UNIQUE on purpose.
CREATE TABLE IF NOT EXISTS unclaimed_release(
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL UNIQUE,
    pallet TEXT NOT NULL,
    contract TEXT NOT NULL,
    claim_id TEXT NOT NULL,
    claimant TEXT NOT NULL,
    deliverer TEXT NOT NULL,
    credential_digest TEXT,
    seal TEXT NOT NULL,
    custody_version INTEGER NOT NULL,
    custody_fingerprint TEXT NOT NULL,
    source_event_ids TEXT NOT NULL,
    released_at TEXT NOT NULL,
    authorization_id TEXT NOT NULL
);
"""
def connect(path):
    p=Path(path);p.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(p,timeout=10)
    db.row_factory=sqlite3.Row
    db.execute('PRAGMA busy_timeout=10000')
    db.executescript(SCHEMA)
    return db
