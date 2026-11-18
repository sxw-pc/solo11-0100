"""Explicit SQLite schema migrations.

Migration history is recorded in ``meta.schema_version`` so that workspaces
created by older versions of the tool are upgraded in place. Every migration
is idempotent (``CREATE TABLE IF NOT EXISTS``) and only uses the standard
library.
"""

MIGRATIONS = [
    (
        1,
        """
        CREATE TABLE IF NOT EXISTS release_ledger(
            release_id TEXT PRIMARY KEY,
            case_id TEXT NOT NULL,
            external_ref TEXT,
            audience TEXT NOT NULL,
            contract TEXT NOT NULL,
            source_version TEXT NOT NULL,
            source_hash TEXT NOT NULL,
            kind TEXT NOT NULL,
            members_json TEXT NOT NULL,
            amounts_json TEXT NOT NULL,
            content_json TEXT,
            content_sha256 TEXT,
            json_path TEXT,
            md_path TEXT,
            status TEXT NOT NULL,
            reason_code TEXT,
            reason_detail_json TEXT,
            evidence_json TEXT,
            is_prior INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS release_identity_ux
        ON release_ledger(case_id, audience, contract, source_hash, kind,
                          members_json, amounts_json);
        CREATE TABLE IF NOT EXISTS release_access(
            id INTEGER PRIMARY KEY,
            release_id TEXT NOT NULL REFERENCES release_ledger(release_id),
            audience TEXT NOT NULL,
            contract TEXT NOT NULL,
            accessed_at TEXT NOT NULL,
            detail TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS release_event(
            id INTEGER PRIMARY KEY,
            release_id TEXT NOT NULL,
            phase TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            detail TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS release_event_ix
        ON release_event(release_id, id);
        """,
    ),
]


def apply_migrations(db):
    row = db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    current = int(row['value']) if row else 0
    for version, sql in MIGRATIONS:
        if version <= current:
            continue
        db.executescript(sql)
        db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('schema_version',?)",
                   (str(version),))
        current = version
    return current
