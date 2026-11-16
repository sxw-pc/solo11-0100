import json
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
CREATE TABLE IF NOT EXISTS settlement(subject TEXT PRIMARY KEY,status TEXT NOT NULL,rounding_mode TEXT,billing_unit_kg TEXT,raw_settleable_kg TEXT,billable_kg TEXT,rate_per_kg TEXT,amount TEXT,issues TEXT NOT NULL,run_id INTEGER NOT NULL REFERENCES run(id));
CREATE TABLE IF NOT EXISTS settlement_line(id INTEGER PRIMARY KEY AUTOINCREMENT,subject TEXT NOT NULL REFERENCES settlement(subject),leaf_box TEXT NOT NULL,gross_kg TEXT NOT NULL,tare_share_kg TEXT NOT NULL,settleable_kg TEXT NOT NULL,billable_kg TEXT NOT NULL,allocation TEXT NOT NULL,weight_event_id INTEGER NOT NULL REFERENCES event(id),source_line_no INTEGER NOT NULL,calibration TEXT);
"""
def connect(path):
    p=Path(path);p.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(p)
    db.row_factory=sqlite3.Row
    db.executescript(SCHEMA)
    return db


def save_settlement(db, run_id, subject, record):
    """Persist one order's settlement header and its auditable leaf lines."""
    db.execute(
        'INSERT INTO settlement VALUES (?,?,?,?,?,?,?,?,?,?)',
        (subject, record['status'], record.get('rounding_mode'),
         record.get('billing_unit_kg'), record.get('raw_settleable_kg'),
         record.get('billable_kg'), record.get('rate_per_kg'),
         record.get('amount'), json.dumps(record.get('issues', []), ensure_ascii=False), run_id))
    for line in record.get('lines', []):
        db.execute(
            'INSERT INTO settlement_line(subject,leaf_box,gross_kg,tare_share_kg,settleable_kg,billable_kg,allocation,weight_event_id,source_line_no,calibration) '
            'VALUES (?,?,?,?,?,?,?,?,?,?)',
            (subject, line['leaf_box'], str(line['gross_kg']), str(line['tare_share_kg']),
             str(line['settleable_kg']), str(line['billable_kg']),
             json.dumps(line['allocation'], ensure_ascii=False), line['weight_event_id'],
             line['source_line_no'], line.get('calibration')))
