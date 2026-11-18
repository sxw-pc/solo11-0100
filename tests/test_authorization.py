"""Authorization edges, access records and ordinary-report isolation."""
import json
from pathlib import Path

import pytest

from dockscan.ingest import freeze_config, import_file
from dockscan.rules import evaluate
from dockscan.privacy import register, CONTRACTS
from dockscan.releases import publish, download
from dockscan.report import export
from dockscan.store import connect

FIX = Path(__file__).resolve().parents[1] / 'fixtures'
CARRIER = 'carrier_consortium_non_inference'
AUDITOR = 'internal_auditor_exact_access'
CARRIER_AUDIENCE = CONTRACTS[CARRIER]['audience']
AUDITOR_AUDIENCE = CONTRACTS[AUDITOR]['audience']


@pytest.fixture()
def workspace(tmp_path):
    db = tmp_path / 'privacy.db'
    freeze_config(db, FIX / 'privacy-config.json')
    import_file(db, FIX / 'privacy-events.jsonl')
    evaluate(db)
    register(db, 'privacy-01', FIX / 'privacy-case.json')
    return {'db': db, 'out': tmp_path / 'releases'}


def test_unknown_contract_is_denied_without_crashing(workspace):
    decision = publish(workspace['db'], workspace['out'], 'privacy-01',
                       CARRIER_AUDIENCE, 'no_such_contract', 'aggregate',
                       ['B1', 'B2'])
    assert decision['decision'] == 'denied'
    assert decision['reason_code'] == 'unknown_contract'


def test_unknown_box_is_denied(workspace):
    decision = publish(workspace['db'], workspace['out'], 'privacy-01',
                       AUDITOR_AUDIENCE, AUDITOR, 'aggregate', ['B1', 'B9'])
    assert decision['decision'] == 'denied'
    assert decision['reason_code'] == 'unknown_box'


def test_download_records_access_and_is_audience_bound(workspace):
    decision = publish(workspace['db'], workspace['out'], 'privacy-01',
                       AUDITOR_AUDIENCE, AUDITOR, 'per_box',
                       ['B1', 'B2', 'B3'])
    rid = decision['release_id']
    download(workspace['db'], rid)
    download(workspace['db'], rid)
    conn = connect(workspace['db'])
    rows = conn.execute(
        'SELECT audience,contract,detail FROM release_access ORDER BY id'
    ).fetchall()
    conn.close()
    assert len(rows) == 2
    assert all(r['audience'] == AUDITOR_AUDIENCE and
               r['contract'] == AUDITOR and r['detail'] == 'redownload'
               for r in rows)


def test_blocked_release_cannot_be_downloaded(workspace):
    blocked = publish(workspace['db'], workspace['out'], 'privacy-01',
                      CARRIER_AUDIENCE, CARRIER, 'aggregate', ['B1', 'B2'],
                      declared_sum=300)
    assert blocked['decision'] == 'denied'
    with pytest.raises(ValueError, match='release_not_available'):
        download(workspace['db'], blocked['release_id'])


def test_auditor_artifact_is_bound_to_source_version_and_hashes(workspace):
    decision = publish(workspace['db'], workspace['out'], 'privacy-01',
                       AUDITOR_AUDIENCE, AUDITOR, 'aggregate',
                       ['B1', 'B2', 'B3'])
    content = json.loads(Path(decision['json_path']).read_text(encoding='utf-8'))
    assert content['source_version'] == 'v1'
    assert len(content['source_hash']) == 64
    for evidence in content['box_evidence'].values():
        assert len(evidence['event_hash']) == 64
        assert len(evidence['source_hash']) == 64
        assert evidence['source_path'].endswith('.jsonl')


def test_carrier_artifact_omits_source_paths_and_per_box_values(workspace):
    # The only carrier-safe aggregate here is the broad three-box group,
    # identical to the seeded R1 -> already_published; build a fresh
    # no-prior workspace to actually render one carrier artifact.
    db = workspace['out'].parent / 'fresh.db'
    freeze_config(db, FIX / 'privacy-config.json')
    import_file(db, FIX / 'privacy-events.jsonl')
    evaluate(db)
    register(db, 'privacy-01', FIX / 'privacy-no-prior-case.json')
    decision = publish(db, workspace['out'].parent / 'fresh-rel',
                       'privacy-01', CARRIER_AUDIENCE, CARRIER, 'aggregate',
                       ['B1', 'B2', 'B3'])
    assert decision['decision'] == 'allowed'
    content = json.loads(Path(decision['json_path']).read_text(encoding='utf-8'))
    assert 'per_box_cents' not in content
    for evidence in content['box_evidence'].values():
        assert 'source_path' not in evidence


def test_ordinary_report_without_privacy_case_is_unchanged(tmp_path):
    # A workspace that never registered a privacy case reports raw payloads.
    db = tmp_path / 'plain.db'
    freeze_config(db, FIX / 'config.json')
    import_file(db, FIX / 'events.jsonl')
    evaluate(db)
    target = export(db, tmp_path / 'plain-report.json')
    data = json.loads(target.read_text(encoding='utf-8'))
    assert data['release_ledger'] == []
    payload = json.loads(data['event'][0]['payload'])
    assert '_redacted_fields' not in payload
