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
CREATE TABLE IF NOT EXISTS return_case(id TEXT PRIMARY KEY,payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS return_amendment(id INTEGER PRIMARY KEY,case_id TEXT NOT NULL REFERENCES return_case(id),seq INTEGER NOT NULL,kind TEXT NOT NULL,fixture_path TEXT NOT NULL,sha256 TEXT NOT NULL,doc TEXT NOT NULL,created_at TEXT NOT NULL,UNIQUE(case_id,seq));
CREATE TABLE IF NOT EXISTS return_run(id INTEGER PRIMARY KEY,case_id TEXT NOT NULL REFERENCES return_case(id),contract TEXT NOT NULL,input_digest TEXT NOT NULL,rule_version TEXT NOT NULL,started_at TEXT NOT NULL,completed_at TEXT NOT NULL,status TEXT NOT NULL,summary TEXT NOT NULL,UNIQUE(case_id,input_digest,contract));
CREATE TABLE IF NOT EXISTS repair_station(case_id TEXT NOT NULL,station_no INTEGER NOT NULL,created_run INTEGER NOT NULL REFERENCES return_run(id),PRIMARY KEY(case_id,station_no));
CREATE TABLE IF NOT EXISTS unit_trace(run_id INTEGER NOT NULL REFERENCES return_run(id),unit TEXT NOT NULL,rma_id TEXT,customer TEXT,origin_box TEXT NOT NULL,child_box TEXT,parcel_id TEXT,scan_index INTEGER,condition TEXT,physical_state TEXT NOT NULL,reasons TEXT NOT NULL,evidence TEXT NOT NULL,PRIMARY KEY(run_id,unit));
CREATE TABLE IF NOT EXISTS repair_order(run_id INTEGER NOT NULL REFERENCES return_run(id),unit TEXT NOT NULL,station_no INTEGER NOT NULL,evidence TEXT NOT NULL,PRIMARY KEY(run_id,unit));
CREATE TABLE IF NOT EXISTS inventory_ledger(id INTEGER PRIMARY KEY,run_id INTEGER NOT NULL REFERENCES return_run(id),case_id TEXT NOT NULL,unit TEXT NOT NULL,action TEXT NOT NULL,prior_id INTEGER REFERENCES inventory_ledger(id),reasons TEXT NOT NULL,evidence TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS refund_ledger(id INTEGER PRIMARY KEY,run_id INTEGER NOT NULL REFERENCES return_run(id),case_id TEXT NOT NULL,rma_id TEXT NOT NULL,customer TEXT NOT NULL,movement TEXT NOT NULL,amount INTEGER NOT NULL,settled_after INTEGER NOT NULL,prior_id INTEGER REFERENCES refund_ledger(id),reasons TEXT NOT NULL,evidence TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS return_issue(id INTEGER PRIMARY KEY,run_id INTEGER NOT NULL REFERENCES return_run(id),scope TEXT NOT NULL,ref TEXT NOT NULL,code TEXT NOT NULL,detail TEXT NOT NULL);
"""
def connect(path):
    p=Path(path);p.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(p)
    db.row_factory=sqlite3.Row
    db.executescript(SCHEMA)
    return db
