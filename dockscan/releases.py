"""Recoverable release protocol.

Commit protocol per release identity (all SQLite writes are serialized with
BEGIN IMMEDIATE):

0. A read-only preliminary review decides whether work is possible and the
   exact artifact bytes are staged into uniquely-named temp files. No ledger
   row and no final path exists at this point.
1. Under one write lock: re-review against the audience's full effective
   history (both 'committed' and 'proposed' rows bound inference), occupy the
   deterministic release identity as ``proposed`` with the exact bytes frozen
   in the row, os.replace the staged files to their final paths, and flip the
   row to ``committed`` — all in one transaction.
2. A competing worker either committed its occupation first (the second worker
   sees that effective row and is denied, or gets 'already_published') or is
   still queued behind BEGIN IMMEDIATE. Two individually-safe overlapping
   proposals can therefore never both occupy identities.

Crash windows are always ledger-anchored and reconciled by ``recover``:

* crash during phase 0: only a private temp file remains, no ledger row
* hard kill after the row is inserted but before/around os.replace: a
  'proposed' row exists; recover() re-renders the frozen bytes and commits
  (no final file can exist without the row, so there are never orphan files)
"""
import fcntl
import hashlib
import json
import os
import threading
from contextlib import contextmanager
from pathlib import Path

from .privacy import review_proposal
from .report import render_release, write_artifacts, stage_temp_artifacts
from .store import (connect, immediate_transaction, append_event,
                    find_ledger_identity, update_ledger, record_access,
                    now_iso)


class ArtifactWriteError(RuntimeError):
    pass


class _CleanupOnly(Exception):
    """Internal control flow: roll back the transaction and delete temp files."""
    def __init__(self, tmp_json, tmp_md, decision=None):
        super().__init__('cleanup_only')
        self.tmp_json = tmp_json
        self.tmp_md = tmp_md
        self.decision = decision


# One in-process reentrant lock per database path. Combined with an OS file
# lock on <db>.publish.lock, this makes the whole review -> occupation ->
# commit section mutually exclusive across threads and processes, closing the
# read-then-write window in which two overlapping proposals could both pass
# inference review against the same history boundary.
_THREAD_LOCKS = {}
_LOCK_GUARD = threading.Lock()


def _thread_lock(db_path):
    key = str(Path(db_path).resolve())
    with _LOCK_GUARD:
        lock = _THREAD_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _THREAD_LOCKS[key] = lock
        return lock


@contextmanager
def publication_lock(db_path):
    lock_path = str(db_path) + '.publish.lock'
    Path(lock_path).parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, 'a+')
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        with _thread_lock(db_path):
            yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _hash_text(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _render_markdown(content):
    lines = [f"# 发布物 {content['release_id']}", '',
             f"- 受众: {content['audience']}",
             f"- 合同: {content['contract']}",
             f"- 类型: {content['kind']}",
             f"- 来源版本: {content['source_version']}",
             f"- 来源哈希: {content['source_hash']}",
             f"- 成员: {', '.join(content['members'])}", '']
    if content['kind'] == 'aggregate':
        lines.append(f"- 聚合总额(分): {content['aggregate_total_cents']}")
    else:
        lines.append('- 逐箱费率(分，仅内部审计员授权):')
        for box, value in content['per_box_cents'].items():
            lines.append(f"  - {box}: {value}")
    lines += ['', '## 逐箱来源证据']
    for box, evidence in content['box_evidence'].items():
        path = evidence.get('source_path')
        path_part = f" source_path={path}" if path else ''
        lines.append(f"- {box}: event_id={evidence['event_id']} "
                     f"event_hash={evidence['event_hash']}{path_part} "
                     f"source_hash={evidence['source_hash']}")
    lines += ['', '## 不可推断证据']
    ev = content['inference_evidence']
    lines += [f"- 历史方程组数: {len(ev['history_equations'])}",
              f"- 零空间维数(发布前): {ev['nullspace_dimension_before']}",
              f"- 零空间维数(发布后): {ev['nullspace_dimension_after']}",
              f"- 已被唯一确定的箱: {', '.join(ev['fixed_boxes']) or '无'}",
              f"- 自由度: {json.dumps(ev['degrees_of_freedom'], ensure_ascii=False)}",
              '']
    return '\n'.join(lines)


def _insert_blocked(db, decision, case_id, members):
    release_id = decision['release_id']
    existing = find_ledger_identity(db, release_id)
    if existing is not None:
        return existing['release_id']
    db.execute(
        'INSERT INTO release_ledger(release_id,case_id,external_ref,audience,'
        'contract,source_version,source_hash,kind,members_json,amounts_json,'
        'content_json,content_sha256,json_path,md_path,status,reason_code,'
        'reason_detail_json,evidence_json,is_prior,created_at,updated_at) '
        "VALUES (?,?,NULL,?,?,?,?,?,?,?,NULL,NULL,NULL,NULL,'blocked',?,?,"
        "?,0,?,?)",
        (release_id, case_id, decision['audience'], decision['contract'],
         decision.get('source_version', 'v1'),
         decision.get('source_hash', ''), decision['kind'],
         json.dumps(sorted(members), separators=(',', ':')),
         json.dumps({'redacted': True}, sort_keys=True, separators=(',', ':')),
         decision['reason_code'],
         json.dumps(decision['reason_detail'], ensure_ascii=False, sort_keys=True),
         json.dumps(decision.get('evidence', {}), sort_keys=True,
                    separators=(',', ':')),
         now_iso(), now_iso()))
    append_event(db, release_id, 'blocked', decision['reason_code'])
    return release_id


def _delete_tmps(*paths):
    for tmp in paths:
        try:
            Path(tmp).unlink()
        except FileNotFoundError:
            pass


def _fresh_immediate(db_path):
    """A brand-new connection already inside BEGIN IMMEDIATE.

    A new connection always starts from the latest committed snapshot; reusing
    a connection whose previous transaction ended can keep a stale SQLite
    snapshot and miss rows another process committed in between.
    """
    db = connect(db_path)
    db.execute('BEGIN IMMEDIATE')
    return db


def _commit(db):
    db.execute('COMMIT')
    db.close()


def _rollback(db):
    try:
        db.execute('ROLLBACK')
    finally:
        db.close()


def publish(db_path, out_dir, case_id, audience, contract, kind, members,
            declared_sum=None, crash_hook=None, sync_barrier=None):
    """Review, occupy identity, write artifacts and commit. Crash-safe.

    The review -> occupation -> commit section runs under one cross-thread /
    cross-process publication lock, so the inference boundary observed by a
    review always includes every earlier worker's occupied identity.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    members = sorted(members)
    if sync_barrier is not None:
        sync_barrier.wait(timeout=30)

    with publication_lock(db_path):
        # Phase 1 (write lock): review against all effective history.
        db = _fresh_immediate(db_path)
        try:
            decision = review_proposal(db, case_id, audience, contract,
                                       kind, members, declared_sum)
            if decision['decision'] == 'denied':
                if 'release_id' in decision:
                    _insert_blocked(db, decision, case_id, members)
                _commit(db)
                return decision
            if decision['decision'] == 'already_published':
                _commit(db)
                return decision
            release_id = decision['release_id']
            case = decision['case']
            content = render_release(decision, case, db, audience)
            content_text = json.dumps(content, ensure_ascii=False, indent=2,
                                      sort_keys=True)
            md_text = _render_markdown(content)
            content_hash = _hash_text(content_text)
            json_path = str(out_dir / f'{release_id}.json')
            md_path = str(out_dir / f'{release_id}.md')
            evidence_text = json.dumps(decision['evidence'], sort_keys=True,
                                       separators=(',', ':'))
            _commit(db)
        except BaseException:
            _rollback(db)
            raise

        # Phase 2 (lock still held): stage uniquely-named private temp files.
        try:
            tmp_json, tmp_md = stage_temp_artifacts(
                out_dir, release_id, content_text, md_text)
            if crash_hook is not None:
                crash_hook('after_md_tmp')
        except BaseException as exc:
            db = _fresh_immediate(db_path)
            try:
                append_event(db, release_id, 'write_interrupted',
                             type(exc).__name__)
                _commit(db)
            except BaseException:
                _rollback(db)
            raise

        # Phase 3 (write lock): re-review, occupy identity, atomically
        # publish and commit. No other worker can interleave.
        db = _fresh_immediate(db_path)
        try:
            decision = review_proposal(db, case_id, audience, contract,
                                       kind, members, declared_sum)
            if decision['decision'] == 'denied':
                if 'release_id' in decision:
                    _insert_blocked(db, decision, case_id, members)
                raise _CleanupOnly(tmp_json, tmp_md)
            if decision['decision'] == 'already_published':
                raise _CleanupOnly(tmp_json, tmp_md, decision)

            ts = now_iso()
            db.execute(
                'INSERT INTO release_ledger(release_id,case_id,external_ref,'
                'audience,contract,source_version,source_hash,kind,'
                'members_json,amounts_json,content_json,content_sha256,'
                'json_path,md_path,status,reason_code,reason_detail_json,'
                "evidence_json,is_prior,created_at,updated_at) VALUES ("
                "?,?,NULL,?,?,?,?,?,?,?,?,?,?,?,'proposed',NULL,NULL,?,0,?,?)",
                (release_id, case_id, audience, contract,
                 decision['source_version'], decision['source_hash'], kind,
                 json.dumps(members, separators=(',', ':')),
                 json.dumps(decision['amounts'], sort_keys=True,
                            separators=(',', ':')),
                 content_text, content_hash, json_path, md_path,
                 evidence_text, ts, ts))
            append_event(db, release_id, 'proposed',
                         json.dumps({'json_path': json_path}))
            os.replace(tmp_json, json_path)
            if crash_hook is not None:
                crash_hook('after_json_replace')
            os.replace(tmp_md, md_path)
            update_ledger(db, release_id, status='committed')
            append_event(db, release_id, 'committed',
                         json.dumps({'content_sha256': content_hash}))
            _commit(db)
        except _CleanupOnly as cleanup:
            _rollback(db)
            _delete_tmps(cleanup.tmp_json, cleanup.tmp_md)
            if cleanup.decision is not None:
                return cleanup.decision
            return decision
        except BaseException:
            _rollback(db)
            _delete_tmps(tmp_json, tmp_md)
            raise

        decision['status'] = 'committed'
        decision['json_path'] = json_path
        decision['md_path'] = md_path
        decision['content_sha256'] = content_hash
        return decision


def recover(db_path, out_dir):
    """Resolve every non-terminal ledger row and repair missing artifacts."""
    db = connect(db_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    actions = []
    try:
        rows = db.execute(
            "SELECT * FROM release_ledger WHERE status IN ('proposed','committed') "
            "ORDER BY created_at,release_id").fetchall()
        for raw_row in rows:
            row = dict(raw_row)
            rid = row['release_id']
            json_path = Path(row['json_path']) if row['json_path'] else \
                out_dir / f'{rid}.json'
            md_path = Path(row['md_path']) if row['md_path'] else \
                out_dir / f'{rid}.md'
            # Prior external disclosures were frozen into the ledger at
            # registration time and never had local artifacts; they must not
            # be treated as interrupted writes.
            if row['is_prior'] and not row['json_path']:
                continue
            intact = (_intact(json_path, row['content_sha256']) and
                      md_path.exists())
            if intact:
                if row['status'] == 'proposed':
                    with immediate_transaction(db):
                        update_ledger(db, rid, status='committed')
                        append_event(db, rid, 'recovered_commit',
                                     'files_intact_after_interruption')
                    actions.append({'release_id': rid,
                                    'action': 'committed_after_repair',
                                    'detail': 'files_intact'})
                continue
            if not row['content_json']:
                with immediate_transaction(db):
                    update_ledger(db, rid, status='aborted')
                    append_event(db, rid, 'aborted', 'no_content_to_restore')
                actions.append({'release_id': rid, 'action': 'aborted',
                                'detail': 'no_content_to_restore'})
                continue
            try:
                content = json.loads(row['content_json'])
                md_text = _render_markdown(content)
                write_artifacts(str(json_path), str(md_path),
                                row['content_json'], md_text)
            except BaseException as exc:  # render/disk still broken
                with immediate_transaction(db):
                    if row['status'] == 'proposed':
                        update_ledger(db, rid, status='aborted')
                        append_event(db, rid, 'aborted', type(exc).__name__)
                        actions.append({'release_id': rid, 'action': 'aborted',
                                        'detail': type(exc).__name__})
                    else:
                        append_event(db, rid, 'repair_failed',
                                     type(exc).__name__)
                        actions.append({'release_id': rid,
                                        'action': 'committed_repair_pending',
                                        'detail': type(exc).__name__})
                continue
            with immediate_transaction(db):
                update_ledger(db, rid, status='committed',
                              json_path=str(json_path), md_path=str(md_path))
                append_event(db, rid, 'recovered_commit',
                             'artifacts_rerendered_from_ledger')
            actions.append({'release_id': rid,
                            'action': 'committed_after_repair',
                            'detail': 'artifacts_rerendered'})
        return actions
    finally:
        db.close()


def _intact(path, expected_hash):
    try:
        text = Path(path).read_text(encoding='utf-8')
    except OSError:
        return False
    return _hash_text(text) == expected_hash


def download(db_path, release_id):
    """Re-serve an already published artifact and leave an access record."""
    db = connect(db_path)
    try:
        row = find_ledger_identity(db, release_id)
        if row is None or row['status'] != 'committed':
            raise ValueError('release_not_available')
        with immediate_transaction(db):
            record_access(db, release_id, row['audience'], row['contract'],
                          'redownload')
        return {'release_id': release_id, 'audience': row['audience'],
                'json_path': row['json_path'], 'md_path': row['md_path'],
                'content_sha256': row['content_sha256']}
    finally:
        db.close()
