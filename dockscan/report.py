"""Report rendering.

Two output surfaces:

* ``export`` — the ordinary internal hand-off report. Confidential fields are
  redacted from every event payload, and the release ledger controls what is
  included. Reports without rates are unchanged.
* ``render_release`` / ``write_artifacts`` — per-audience release artifacts
  whose exact bytes are frozen in the release ledger. Writes are atomic
  (temp file + os.replace) and a crash hook can interrupt them for recovery
  tests.
"""
import hashlib
import json
import os
import tempfile
from pathlib import Path

from .privacy import CONTRACTS
from .store import connect


def _confidential_fields(db):
    fields = set()
    # privacy_case.payload is itself a JSON document; parse directly.
    for row in db.execute('SELECT payload FROM privacy_case'):
        frozen = json.loads(row['payload'])
        fields.add(frozen['doc'].get('confidential_field',
                                     'freight_rate_cents'))
    return fields


def redact_payload(payload_text, confidential_fields):
    payload = json.loads(payload_text)
    removed = []
    for field in confidential_fields:
        if field in payload:
            del payload[field]
            removed.append(field)
    if removed:
        payload['_redacted_fields'] = sorted(removed)
    return json.dumps(payload, sort_keys=True, ensure_ascii=False,
                      separators=(',', ':'))


def export(db_path, out):
    db = connect(db_path)
    confidential = _confidential_fields(db)
    data = {table: [dict(r) for r in
                    db.execute(f'SELECT * FROM {table} ORDER BY 1')]
            for table in ('batch', 'subject', 'issue', 'decision',
                          'evidence', 'run', 'lineage')}
    events = []
    for row in db.execute('SELECT * FROM event ORDER BY id'):
        item = dict(row)
        if confidential:
            item['payload'] = redact_payload(item['payload'], confidential)
        events.append(item)
    data['event'] = events
    data['source'] = [dict(r) for r in
                      db.execute('SELECT id,path,sha256 FROM source ORDER BY id')]
    data['config_hash'] = db.execute(
        "SELECT value FROM meta WHERE key='config_hash'").fetchone()['value']
    data['release_ledger'] = [dict(r) for r in db.execute(
        'SELECT release_id,external_ref,audience,contract,source_version,'
        'source_hash,kind,status,reason_code,is_prior,created_at,updated_at '
        'FROM release_ledger ORDER BY created_at,release_id')]
    target = Path(out)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                      encoding='utf-8')
    md = target.with_suffix('.md')
    md.write_text('# 决策报告\n\n' + '\n'.join(
        f"- {d['subject']}: {d['status']} — {d['details']}"
        for d in data['decision']) + '\n', encoding='utf-8')
    db.close()
    return target


def render_release(decision, case, db, audience):
    """Build the exact, ledger-frozen artifact content for one decision."""
    contract = decision.get('contract')
    policy = CONTRACTS[contract]
    include_source_paths = policy['allows_per_box']
    members = decision['members']
    box_evidence = {}
    for box in members:
        rec = case['records'][box]
        evidence = {'event_id': rec['id'], 'event_hash': rec['sha256'],
                    'source_hash': rec['source_sha256']}
        if include_source_paths:
            evidence['source_path'] = rec.get('source_path')
        box_evidence[box] = evidence
    content = {
        'release_id': decision['release_id'],
        'case_id': case['case_id'],
        'audience': audience,
        'contract': contract,
        'kind': decision['kind'],
        'members': members,
        'source_version': decision['source_version'],
        'source_hash': decision['source_hash'],
        'box_evidence': box_evidence,
        'inference_evidence': decision['evidence'],
        'redacted_field': case['confidential_field'],
    }
    if decision['kind'] == 'aggregate':
        content['aggregate_total_cents'] = decision['amounts']['total_cents']
    else:
        content['per_box_cents'] = dict(
            sorted(decision['amounts']['per_box_cents'].items()))
    return content


def write_temp_artifacts(tmp_json, tmp_md, json_text, md_text, crash_hook=None):
    """Write both artifacts to explicit temp paths; no final file is touched."""
    tmp_json, tmp_md = Path(tmp_json), Path(tmp_md)
    for stale in (tmp_json, tmp_md):
        try:
            stale.unlink()
        except FileNotFoundError:
            pass
    with open(tmp_json, 'w', encoding='utf-8') as handle:
        handle.write(json_text)
        handle.flush()
        os.fsync(handle.fileno())
    if crash_hook is not None:
        crash_hook('after_json_tmp')
    with open(tmp_md, 'w', encoding='utf-8') as handle:
        handle.write(md_text)
        handle.flush()
        os.fsync(handle.fileno())
    if crash_hook is not None:
        crash_hook('after_md_tmp')
    return tmp_json, tmp_md


def stage_temp_artifacts(out_dir, release_id, json_text, md_text):
    """Create uniquely-named temp files in the target directory.

    Unique names let concurrent attempts for the same release identity stage
    their bytes without clobbering each other; the content is identical anyway
    because the identity hash fixes the exact artifact bytes.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp_json = tempfile.mkstemp(prefix=f'{release_id}.', suffix='.json.tmp',
                                    dir=out_dir)
    with os.fdopen(fd, 'w', encoding='utf-8') as handle:
        handle.write(json_text)
        handle.flush()
        os.fsync(handle.fileno())
    fd, tmp_md = tempfile.mkstemp(prefix=f'{release_id}.', suffix='.md.tmp',
                                  dir=out_dir)
    with os.fdopen(fd, 'w', encoding='utf-8') as handle:
        handle.write(md_text)
        handle.flush()
        os.fsync(handle.fileno())
    return tmp_json, tmp_md


def write_artifacts(json_path, md_path, json_text, md_text, crash_hook=None):
    """Atomic two-file write. Optional hook simulates disk interruption."""
    json_path, md_path = Path(json_path), Path(md_path)
    tmp_json = json_path.with_name(json_path.name + '.tmp')
    tmp_md = md_path.with_name(md_path.name + '.tmp')
    write_temp_artifacts(tmp_json, tmp_md, json_text, md_text, crash_hook)
    os.replace(tmp_json, json_path)
    if crash_hook is not None:
        crash_hook('after_json_replace')
    os.replace(tmp_md, md_path)
    if crash_hook is not None:
        crash_hook('after_md_replace')
    return json_path, md_path


def hash_text(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()
