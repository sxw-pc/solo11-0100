import sqlite3
from pathlib import Path

SCHEMA="""
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS batch(id TEXT PRIMARY KEY,config_hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS subject(id TEXT PRIMARY KEY,batch_id TEXT NOT NULL REFERENCES batch(id));
CREATE TABLE IF NOT EXISTS source(id INTEGER PRIMARY KEY,path TEXT NOT NULL,sha256 TEXT NOT NULL UNIQUE,raw BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS event(id INTEGER PRIMARY KEY,source_id INTEGER NOT NULL REFERENCES source(id),line_no INTEGER NOT NULL,identity TEXT NOT NULL UNIQUE,subject TEXT NOT NULL,kind TEXT NOT NULL,time TEXT NOT NULL,payload TEXT NOT NULL,sha256 TEXT NOT NULL,device TEXT,seq INTEGER,UNIQUE(source_id,line_no));
CREATE TABLE IF NOT EXISTS issue(id INTEGER PRIMARY KEY,code TEXT NOT NULL,detail TEXT NOT NULL,event_id INTEGER REFERENCES event(id));
CREATE TABLE IF NOT EXISTS decision(subject TEXT PRIMARY KEY,status TEXT NOT NULL,details TEXT NOT NULL,rule_version TEXT NOT NULL,run_id INTEGER NOT NULL REFERENCES run(id));
CREATE TABLE IF NOT EXISTS evidence(subject TEXT NOT NULL REFERENCES subject(id),event_id INTEGER NOT NULL REFERENCES event(id),rule_version TEXT NOT NULL,run_id INTEGER NOT NULL REFERENCES run(id),PRIMARY KEY(subject,event_id,rule_version));
CREATE TABLE IF NOT EXISTS run(id INTEGER PRIMARY KEY,started_at TEXT NOT NULL,completed_at TEXT,status TEXT NOT NULL,policy TEXT);
CREATE TABLE IF NOT EXISTS lineage(parent_box TEXT NOT NULL,child_box TEXT NOT NULL,quantity INTEGER NOT NULL,kind TEXT NOT NULL,event_id INTEGER NOT NULL REFERENCES event(id),PRIMARY KEY(parent_box,child_box,event_id));
-- 证据化因果边：每条边都注明依据类型，绝不来自文件顺序或 SQLite 自增 id
CREATE TABLE IF NOT EXISTS causal_edge(src_event INTEGER NOT NULL REFERENCES event(id),dst_event INTEGER NOT NULL REFERENCES event(id),reason TEXT NOT NULL,detail TEXT NOT NULL,run_id INTEGER NOT NULL REFERENCES run(id),PRIMARY KEY(src_event,dst_event,reason,run_id));
"""
def connect(path):
    p=Path(path);p.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(p)
    db.row_factory=sqlite3.Row
    db.executescript(SCHEMA)
    _migrate(db)
    return db

def _migrate(db):
    """为旧库补齐新列，保留来源与原始事件不可变的约定。"""
    cols={r['name'] for r in db.execute('PRAGMA table_info(event)')}
    if 'device' not in cols: db.execute('ALTER TABLE event ADD COLUMN device TEXT')
    if 'seq' not in cols: db.execute('ALTER TABLE event ADD COLUMN seq INTEGER')
    run_cols={r['name'] for r in db.execute('PRAGMA table_info(run)')}
    if 'policy' not in run_cols: db.execute('ALTER TABLE run ADD COLUMN policy TEXT')
    issue_cols={r['name'] for r in db.execute('PRAGMA table_info(issue)')}
    if 'event_id' not in issue_cols: db.execute('ALTER TABLE issue ADD COLUMN event_id INTEGER REFERENCES event(id)')
    db.commit()
