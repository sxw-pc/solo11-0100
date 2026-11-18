"""Commit protocol: concurrency, crash recovery, source revision, migration."""
import json
import sqlite3
import threading
from pathlib import Path

import pytest

from dockscan.ingest import freeze_config, import_file
from dockscan.rules import evaluate
from dockscan.privacy import register, review_proposal, load_case
from dockscan.releases import publish, recover
from dockscan.report import write_artifacts
from dockscan.store import connect, immediate_transaction

FIX = Path(__file__).resolve().parents[1] / 'fixtures'
CARRIER = 'carrier_consortium_non_inference'
AUDITOR = 'internal_auditor_exact_access'
CARRIER_AUDIENCE = 'carrier_consortium'
AUDITOR_AUDIENCE = 'internal_auditor'


def _build_workspace(tmp_path, case_name='privacy-case.json',
                     events='privacy-events.jsonl',
                     config='privacy-config.json'):
    db = tmp_path / 'ledger.db'
    freeze_config(db, FIX / config)
    import_file(db, FIX / events)
    evaluate(db)
    register(db, 'privacy-01', FIX / case_name)
    return db, tmp_path / 'releases'


def test_concurrent_proposals_second_one_observed_by_first_commit(tmp_path):
    """Two proposals racing on one history boundary: once the first occupies
    its identity, the second is reviewed against it and denied when their
    combination uniquely determines a box.

    A broad three-box aggregate is occupied first; a two-box aggregate would
    then fix the remaining box by subtraction.
    """
    db, out = _build_workspace(tmp_path, 'privacy-no-prior-case.json')
    barrier = threading.Barrier(2)
    results = []
    errors = []

    def worker(group):
        try:
            decision = publish(db, out, 'privacy-01', CARRIER_AUDIENCE,
                               CARRIER, 'aggregate', group,
                               sync_barrier=barrier)
            results.append((tuple(group), decision['decision'],
                            decision.get('reason_code')))
        except Exception as exc:  # pragma: no cover - failure diagnostic
            errors.append(exc)

    t1 = threading.Thread(target=worker, args=(['B1', 'B2', 'B3'],))
    t2 = threading.Thread(target=worker, args=(['B1', 'B2'],))
    t1.start(); t2.start(); t1.join(); t2.join()
    assert not errors
    # Whichever worker acquires the publication lock first is allowed; the
    # other is then denied because the two aggregates jointly fix a box.
    decisions = sorted(d for _, d, _ in results)
    assert decisions == ['allowed', 'denied']
    denied = next(r for r in results if r[1] == 'denied')
    assert denied[2] == 'inference_violation'
    allowed_group = next(g for g, d, _ in results if d == 'allowed')
    conn = connect(db)
    committed = conn.execute(
        "SELECT COUNT(*) FROM release_ledger WHERE audience=? AND status='committed' "
        "AND is_prior=0", (CARRIER_AUDIENCE,)).fetchone()[0]
    blocked = conn.execute(
        "SELECT reason_code FROM release_ledger WHERE status='blocked'").fetchall()
    conn.close()
    assert committed == 1
    assert allowed_group in (('B1', 'B2'), ('B1', 'B2', 'B3'))
    assert {r['reason_code'] for r in blocked} == {'inference_violation'}


def test_concurrent_identical_proposals_share_one_identity(tmp_path):
    db, out = _build_workspace(tmp_path, 'privacy-no-prior-case.json')
    barrier = threading.Barrier(2)
    outcomes = []

    def worker():
        outcomes.append(publish(db, out, 'privacy-01', AUDITOR_AUDIENCE,
                                AUDITOR, 'aggregate', ['B1', 'B2'],
                                sync_barrier=barrier)['decision'])

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(outcomes) == ['allowed', 'already_published']
    conn = connect(db)
    assert conn.execute(
        "SELECT COUNT(*) FROM release_ledger WHERE is_prior=0").fetchone()[0] == 1
    conn.close()


def _occupy_proposed(db, out, members=('B1', 'B2')):
    """Build a fully valid 'proposed' ledger row the way publish phase 1 does."""
    from dockscan.report import render_release
    out.mkdir(parents=True, exist_ok=True)
    conn = connect(db)
    decision = review_proposal(conn, 'privacy-01', AUDITOR_AUDIENCE, AUDITOR,
                               'aggregate', list(members))
    assert decision['decision'] == 'allowed'
    case = decision['case']
    content = render_release(decision, case, conn, AUDITOR_AUDIENCE)
    content_text = json.dumps(content, ensure_ascii=False, indent=2,
                              sort_keys=True)
    import hashlib
    rid = decision['release_id']
    with immediate_transaction(conn):
        conn.execute(
            'INSERT INTO release_ledger(release_id,case_id,external_ref,'
            'audience,contract,source_version,source_hash,kind,members_json,'
            'amounts_json,content_json,content_sha256,json_path,md_path,status,'
            'reason_code,reason_detail_json,evidence_json,is_prior,created_at,'
            "updated_at) VALUES (?,?,NULL,?,?,?,?,?,?,?,?,?,?,?,'proposed',"
            "NULL,NULL,?,0,?,?)",
            (rid, 'privacy-01', AUDITOR_AUDIENCE, AUDITOR,
             decision['source_version'], decision['source_hash'], 'aggregate',
             json.dumps(sorted(members)),
             json.dumps(decision['amounts'], sort_keys=True),
             content_text, hashlib.sha256(content_text.encode()).hexdigest(),
             str(out / f'{rid}.json'), str(out / f'{rid}.md'),
             json.dumps(decision['evidence'], sort_keys=True),
             '2030-01-01T00:00:00+00:00', '2030-01-01T00:00:00+00:00'))
    conn.close()
    return rid


def test_crash_during_artifact_write_is_recoverable(tmp_path):
    # Crash while staging private temp files: no ledger row, no final file.
    db, out = _build_workspace(tmp_path / 'staging')

    def staging_hook(stage):
        if stage == 'after_md_tmp':
            raise RuntimeError('simulated_disk_failure')
    with pytest.raises(RuntimeError):
        publish(db, out, 'privacy-01', AUDITOR_AUDIENCE, AUDITOR,
                'aggregate', ['B1', 'B2'], crash_hook=staging_hook)
    conn = connect(db)
    assert conn.execute(
        "SELECT COUNT(*) FROM release_ledger WHERE is_prior=0").fetchone()[0] == 0
    conn.close()
    assert not any(p.suffix == '.json' for p in out.iterdir())
    # A clean retry after the staging crash works normally.
    decision = publish(db, out, 'privacy-01', AUDITOR_AUDIENCE, AUDITOR,
                       'aggregate', ['B1', 'B2'])
    assert decision['decision'] == 'allowed'
    assert recover(db, out) == []

    # Crash after the JSON os.replace, inside the commit transaction: a hard
    # kill would leave a 'proposed' row plus one final file. Simulate that
    # durable state directly, then recover() must verify and commit.
    db2, out2 = _build_workspace(tmp_path / 'replace')
    rid = _occupy_proposed(db2, out2)
    conn = connect(db2)
    row = conn.execute(
        'SELECT content_json FROM release_ledger WHERE release_id=?',
        (rid,)).fetchone()
    conn.close()
    target = out2 / f'{rid}.json'
    target.write_text(row['content_json'], encoding='utf-8')
    conn = connect(db2)
    assert conn.execute(
        "SELECT status FROM release_ledger WHERE release_id=?",
        (rid,)).fetchone()['status'] == 'proposed'
    conn.close()
    actions = recover(db2, out2)
    assert actions[0]['action'] == 'committed_after_repair'
    conn = connect(db2)
    phases = [r['phase'] for r in conn.execute(
        'SELECT phase FROM release_event ORDER BY id')]
    status = conn.execute(
        "SELECT status FROM release_ledger WHERE release_id=?",
        (rid,)).fetchone()['status']
    conn.close()
    assert status == 'committed'
    assert 'recovered_commit' in phases


def test_proposed_row_left_by_external_kill_is_recovered(tmp_path):
    """A hard kill between occupation and finalize leaves a 'proposed' row;
    recover() re-renders the frozen bytes and commits deterministically."""
    db, out = _build_workspace(tmp_path)
    rid = _occupy_proposed(db, out)
    actions = recover(db, out)
    assert actions == [{'release_id': rid,
                        'action': 'committed_after_repair',
                        'detail': 'artifacts_rerendered'}]
    conn = connect(db)
    assert conn.execute(
        "SELECT status FROM release_ledger WHERE release_id=?",
        (rid,)).fetchone()['status'] == 'committed'
    conn.close()


def test_committed_row_with_deleted_file_is_re_rendered(tmp_path):
    db, out = _build_workspace(tmp_path)
    decision = publish(db, out, 'privacy-01', AUDITOR_AUDIENCE, AUDITOR,
                       'aggregate', ['B1', 'B2'])
    Path(decision['json_path']).unlink()
    Path(decision['md_path']).unlink()
    actions = recover(db, out)
    assert actions[0]['detail'] == 'artifacts_rerendered'
    conn = connect(db)
    row = conn.execute(
        "SELECT status FROM release_ledger WHERE release_id=?",
        (decision['release_id'],)).fetchone()
    conn.close()
    assert row['status'] == 'committed'
    assert Path(decision['json_path']).exists()


def test_multi_group_simultaneous_inference_is_detected(tmp_path):
    db, out = _build_workspace(tmp_path, 'privacy-no-prior-case.json')
    # Three pair-sums over three boxes uniquely determine every box.
    first = publish(db, out, 'privacy-01', CARRIER_AUDIENCE, CARRIER,
                    'aggregate', ['B1', 'B2'])
    second = publish(db, out, 'privacy-01', CARRIER_AUDIENCE, CARRIER,
                     'aggregate', ['B2', 'B3'])
    third = publish(db, out, 'privacy-01', CARRIER_AUDIENCE, CARRIER,
                    'aggregate', ['B1', 'B3'])
    assert first['decision'] == 'allowed'
    assert second['decision'] == 'allowed'
    assert third['decision'] == 'denied'
    assert third['reason_detail']['fixed_boxes'] == ['B1', 'B2', 'B3']


def test_source_revision_causally_changes_release_decision(tmp_path):
    db, out = _build_workspace(tmp_path)
    # Simulate a source revision: replace the immutable event row backing the
    # case with a different payload under a new source file.
    conn = connect(db)
    with immediate_transaction(conn):
        original = conn.execute(
            "SELECT id,source_id,line_no,identity,subject,kind,time FROM event "
            "WHERE subject='B1' AND kind='weight'").fetchone()
        new_payload = json.dumps({
            'kind': 'weight', 'subject': 'B1', 'identity': 'D1:3',
            'time': '2030-06-01T08:15:00Z', 'dock': 'D1', 'net_kg': 50,
            'freight_rate_cents': 101, 'carrier': 'K1'}, sort_keys=True)
        new_hash = __import__('hashlib').sha256(new_payload.encode()).hexdigest()
        sid = conn.execute(
            "INSERT INTO source(path,sha256,raw) VALUES (?,?,?)",
            ('revised.jsonl', 'f' * 64, b'revised')).lastrowid
        conn.execute('UPDATE event SET source_id=?,payload=?,sha256=? WHERE id=?',
                     (sid, new_payload, new_hash, original['id']))
    conn.close()
    with pytest.raises(ValueError, match='source_state_conflict'):
        review_proposal(connect(db), 'privacy-01', AUDITOR_AUDIENCE,
                        AUDITOR, 'aggregate', ['B1', 'B2'])
    # publish fails closed through the same guard.
    with pytest.raises(ValueError, match='source_state_conflict'):
        publish(db, out, 'privacy-01', AUDITOR_AUDIENCE, AUDITOR,
                'aggregate', ['B1', 'B2'])


def test_revised_source_workspace_produces_different_identity(tmp_path):
    """A corrected source in a new workspace changes the identity hash."""
    db1, out1 = _build_workspace(tmp_path / 'ws1')
    first = publish(db1, out1, 'privacy-01', AUDITOR_AUDIENCE, AUDITOR,
                    'aggregate', ['B1', 'B2'])

    revised_events = tmp_path / 'revised-events.jsonl'
    lines = []
    for line in (FIX / 'privacy-events.jsonl').read_text(encoding='utf-8').splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get('subject') == 'B1' and row.get('kind') == 'weight':
            row['freight_rate_cents'] = 150
        lines.append(json.dumps(row, ensure_ascii=False))
    revised_events.write_text('\n'.join(lines) + '\n', encoding='utf-8')

    db2 = tmp_path / 'ws2' / 'ledger.db'
    freeze_config(db2, FIX / 'privacy-config.json')
    import_file(db2, revised_events)
    evaluate(db2)
    register(db2, 'privacy-01', FIX / 'privacy-no-prior-case.json')
    second = publish(db2, tmp_path / 'ws2' / 'releases', 'privacy-01',
                     AUDITOR_AUDIENCE, AUDITOR, 'aggregate', ['B1', 'B2'])
    assert first['source_hash'] != second['source_hash']
    assert first['release_id'] != second['release_id']


def test_legacy_database_is_migrated_on_connect(tmp_path):
    # Build a database with the pre-migration table set only.
    legacy = tmp_path / 'legacy.db'
    raw = sqlite3.connect(legacy)
    raw.executescript("""
    CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
    CREATE TABLE batch(id TEXT PRIMARY KEY,config_hash TEXT NOT NULL);
    CREATE TABLE subject(id TEXT PRIMARY KEY,batch_id TEXT NOT NULL);
    CREATE TABLE source(id INTEGER PRIMARY KEY,path TEXT NOT NULL,
        sha256 TEXT NOT NULL UNIQUE,raw BLOB NOT NULL);
    CREATE TABLE event(id INTEGER PRIMARY KEY,source_id INTEGER NOT NULL,
        line_no INTEGER NOT NULL,identity TEXT NOT NULL UNIQUE,subject TEXT NOT NULL,
        kind TEXT NOT NULL,time TEXT NOT NULL,payload TEXT NOT NULL,sha256 TEXT NOT NULL);
    CREATE TABLE issue(id INTEGER PRIMARY KEY,code TEXT NOT NULL,detail TEXT NOT NULL);
    CREATE TABLE run(id INTEGER PRIMARY KEY,started_at TEXT NOT NULL,
        completed_at TEXT,status TEXT NOT NULL);
    CREATE TABLE decision(subject TEXT PRIMARY KEY,status TEXT NOT NULL,
        details TEXT NOT NULL,rule_version TEXT NOT NULL,run_id INTEGER NOT NULL);
    CREATE TABLE evidence(subject TEXT NOT NULL,event_id INTEGER NOT NULL,
        rule_version TEXT NOT NULL,run_id INTEGER NOT NULL);
    CREATE TABLE lineage(parent_box TEXT NOT NULL,child_box TEXT NOT NULL,
        quantity INTEGER NOT NULL,kind TEXT NOT NULL,event_id INTEGER NOT NULL);
    CREATE TABLE privacy_case(id TEXT PRIMARY KEY,payload TEXT NOT NULL);
    """)
    raw.commit(); raw.close()
    db = connect(legacy)
    version = db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    assert version is not None and int(version['value']) >= 1
    db.execute("SELECT release_id,status FROM release_ledger LIMIT 0").fetchall()
    db.execute("SELECT id FROM release_access LIMIT 0").fetchall()
    db.close()


def test_recovery_after_persistent_write_failure_aborts_proposed(tmp_path, monkeypatch):
    db, out = _build_workspace(tmp_path)
    # Leave a valid occupied 'proposed' row (hard-kill scenario), then make
    # every write attempt fail permanently: recovery must abort, never
    # silently claim committed.
    rid = _occupy_proposed(db, out)

    def broken_write(*args, **kwargs):
        raise OSError('disk_permanently_gone')

    monkeypatch.setattr('dockscan.releases.write_artifacts', broken_write)
    actions = recover(db, out)
    assert actions == [{'release_id': rid, 'action': 'aborted',
                        'detail': 'OSError'}]
    conn = connect(db)
    assert conn.execute(
        "SELECT status FROM release_ledger WHERE release_id=?",
        (rid,)).fetchone()['status'] == 'aborted'
    conn.close()
