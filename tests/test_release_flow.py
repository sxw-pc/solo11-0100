"""End-to-end disclosure review and release tests on the frozen privacy case."""
import json
from pathlib import Path

import pytest

from dockscan.ingest import freeze_config, import_file
from dockscan.rules import evaluate
from dockscan.privacy import register, CONTRACTS
from dockscan.releases import publish
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


def test_carrier_r2_is_denied_by_subtraction_against_prior_r1(workspace):
    decision = publish(workspace['db'], workspace['out'], 'privacy-01',
                       CARRIER_AUDIENCE, CARRIER, 'aggregate',
                       ['B1', 'B2'], declared_sum=300)
    assert decision['decision'] == 'denied'
    assert decision['reason_code'] == 'inference_violation'
    assert decision['reason_detail']['fixed_boxes'] == ['B3']
    # Denial records must not contain any protected numeric value. Only the
    # business-facing fields are scanned; identity hashes may contain digits.
    denial_fields = json.dumps(
        {'detail': decision['reason_detail'],
         'evidence': decision['evidence']}, ensure_ascii=False)
    for protected in ('100', '200', '300', '600'):
        assert protected not in denial_fields
    conn = connect(workspace['db'])
    row = conn.execute(
        "SELECT status,amounts_json FROM release_ledger WHERE reason_code='inference_violation'"
    ).fetchone()
    conn.close()
    assert row['status'] == 'blocked'
    assert json.loads(row['amounts_json']) == {'redacted': True}
    # A blocked proposal must leave no external artifact behind.
    assert not list(workspace['out'].glob('*.json')) or \
        all(p.suffix != '.json' for p in workspace['out'].iterdir())


def test_auditor_contract_allows_r2_and_per_box_evidence(workspace):
    aggregate = publish(workspace['db'], workspace['out'], 'privacy-01',
                        AUDITOR_AUDIENCE, AUDITOR, 'aggregate', ['B1', 'B2'])
    assert aggregate['decision'] == 'allowed'
    exact = publish(workspace['db'], workspace['out'], 'privacy-01',
                    AUDITOR_AUDIENCE, AUDITOR, 'per_box', ['B1', 'B2', 'B3'])
    assert exact['decision'] == 'allowed'
    content = json.loads(Path(exact['json_path']).read_text(encoding='utf-8'))
    assert content['per_box_cents'] == {'B1': 100, 'B2': 200, 'B3': 300}
    # Auditor evidence is bound to source version/hashes and full paths.
    for box, evidence in content['box_evidence'].items():
        assert evidence['source_path'].endswith('privacy-events.jsonl')
        assert evidence['event_hash'] and evidence['source_hash']
    assert content['source_version'] == 'v1'


def test_audience_isolation_same_group_different_decisions(workspace):
    carrier = publish(workspace['db'], workspace['out'], 'privacy-01',
                      CARRIER_AUDIENCE, CARRIER, 'aggregate', ['B1', 'B2'])
    auditor = publish(workspace['db'], workspace['out'], 'privacy-01',
                      AUDITOR_AUDIENCE, AUDITOR, 'aggregate', ['B1', 'B2'])
    assert carrier['decision'] == 'denied'
    assert auditor['decision'] == 'allowed'
    conn = connect(workspace['db'])
    audiences = {r['audience'] for r in conn.execute(
        "SELECT DISTINCT audience FROM release_ledger WHERE status='committed'")}
    conn.close()
    assert audiences == {AUDITOR_AUDIENCE, CARRIER_AUDIENCE}  # R1 + auditor R2


def test_carrier_cannot_receive_per_box_release(workspace):
    decision = publish(workspace['db'], workspace['out'], 'privacy-01',
                       CARRIER_AUDIENCE, CARRIER, 'per_box', ['B1'])
    assert decision['decision'] == 'denied'
    assert decision['reason_code'] == 'per_box_authorization_denied'


def test_singleton_aggregate_is_contractually_forbidden(workspace):
    # A fresh audience with no history still cannot receive a one-box group.
    decision = publish(workspace['db'], workspace['out'], 'privacy-01',
                       CARRIER_AUDIENCE, CARRIER, 'aggregate', ['B1'])
    assert decision['decision'] == 'denied'
    assert decision['reason_code'] == 'singleton_group_forbidden'


def test_contract_audience_mismatch_and_cross_forwarding_denied(workspace):
    # Presenting an auditor contract to the carrier audience is not a valid
    # authorization path; no implicit cross-audience forwarding exists.
    decision = publish(workspace['db'], workspace['out'], 'privacy-01',
                       CARRIER_AUDIENCE, AUDITOR, 'aggregate', ['B1', 'B2'])
    assert decision['decision'] == 'denied'
    assert decision['reason_code'] == 'audience_contract_mismatch'


def test_declared_sum_that_disagrees_with_source_is_rejected(workspace):
    decision = publish(workspace['db'], workspace['out'], 'privacy-01',
                       CARRIER_AUDIENCE, CARRIER, 'aggregate',
                       ['B1', 'B2', 'B3'], declared_sum=599)
    assert decision['decision'] == 'denied'
    assert decision['reason_code'] == 'sum_mismatch_source'
    denial_fields = json.dumps(decision['reason_detail'])
    assert '599' not in denial_fields and '600' not in denial_fields


def test_duplicate_group_is_idempotent_and_keeps_one_identity(workspace):
    first = publish(workspace['db'], workspace['out'], 'privacy-01',
                    AUDITOR_AUDIENCE, AUDITOR, 'aggregate', ['B1', 'B2'])
    second = publish(workspace['db'], workspace['out'], 'privacy-01',
                     AUDITOR_AUDIENCE, AUDITOR, 'aggregate', ['B2', 'B1'])
    assert first['decision'] == 'allowed'
    assert second['decision'] == 'already_published'
    assert first['release_id'] == second['release_id']
    conn = connect(workspace['db'])
    count = conn.execute(
        "SELECT COUNT(*) FROM release_ledger WHERE release_id=?",
        (first['release_id'],)).fetchone()[0]
    conn.close()
    assert count == 1


def test_identity_changes_with_source_audience_and_amount(workspace):
    a = publish(workspace['db'], workspace['out'], 'privacy-01',
                AUDITOR_AUDIENCE, AUDITOR, 'aggregate', ['B1', 'B2'])
    b = publish(workspace['db'], workspace['out'], 'privacy-01',
                AUDITOR_AUDIENCE, AUDITOR, 'aggregate', ['B1', 'B2', 'B3'])
    c = publish(workspace['db'], workspace['out'], 'privacy-01',
                AUDITOR_AUDIENCE, AUDITOR, 'per_box', ['B1'])
    assert a['release_id'] != b['release_id'] != c['release_id']
    for decision in (a, b, c):
        assert len(decision['release_id']) == 64  # sha256 hex


def test_prior_r1_is_seeded_as_committed_history(workspace):
    conn = connect(workspace['db'])
    row = conn.execute(
        "SELECT * FROM release_ledger WHERE external_ref='R1'").fetchone()
    assert row['status'] == 'committed' and row['is_prior'] == 1
    assert json.loads(row['members_json']) == ['B1', 'B2', 'B3']
    # Prior rows carry no local artifact but still bound inference.
    assert row['json_path'] is None
    conn.close()


def test_ordinary_report_redacts_confidential_rates_but_keeps_other_fields(workspace):
    from dockscan.report import export
    target = export(workspace['db'], workspace['out'].parent / 'report.json')
    data = json.loads(target.read_text(encoding='utf-8'))
    weight_events = [e for e in data['event'] if e['kind'] == 'weight']
    assert len(weight_events) == 3
    for event in weight_events:
        payload = json.loads(event['payload'])
        assert 'freight_rate_cents' not in payload
        assert payload['_redacted_fields'] == ['freight_rate_cents']
        assert 'net_kg' in payload and 'carrier' in payload
    raw = target.read_text(encoding='utf-8')
    for protected in (': 100', ': 200', ': 300'):
        assert protected not in raw
    assert any(r['external_ref'] == 'R1' for r in data['release_ledger'])
