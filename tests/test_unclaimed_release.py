"""Release-flow tests for unclaimed pallets.

The observable contract under test:

* the custody chain must be continuous, same pallet, same original seal,
  every handoff two-party signed, ending in a seal recheck;
* exactly one exclusive contract is chosen and only its named claimant is
  released;
* verification/authorization never moves the pallet; only ``deliver`` does;
* a delivery is atomic, one-time, version-bound, and leaves a retryable
  authorization after a failed or interrupted attempt;
* reports reflect the real release and expose only digests.
"""
import json
import sqlite3
import threading
from pathlib import Path

import pytest

from dockscan import unclaimed
from dockscan.ingest import freeze_config, import_file
from dockscan.report import export
from dockscan.rules import evaluate
from dockscan.store import connect

FIX = Path(__file__).resolve().parents[1] / 'fixtures'
CASE = 'unclaimed-01'
A_CONTRACT = 'registered_consignor_release'
B_CONTRACT = 'bearer_receipt_release'


def prepare(tmp_path, config='config.json', events='events.jsonl', case_file='unclaimed-case.json',
            case_id=CASE):
    db = tmp_path / 'ledger.db'
    freeze_config(db, FIX / config)
    import_file(db, FIX / events)
    evaluate(db)
    doc = json.loads((FIX / case_file).read_text())
    unclaimed.register(db, case_id, write_case(tmp_path, doc, f'{case_id}.json'))
    return db, doc, None


def write_case(tmp_path, doc, name='case.json'):
    path = tmp_path / name
    path.write_text(json.dumps(doc))
    return path


def base_doc():
    return json.loads((FIX / 'unclaimed-case.json').read_text())


# ---------------------------------------------------------------------------
# Custody chain
# ---------------------------------------------------------------------------

def test_custody_chain_is_continuous_and_same_seal(tmp_path):
    db, doc, _ = prepare(tmp_path)
    chain = unclaimed.custody_chain(db, CASE)
    assert chain['valid'] is True
    assert chain['holder'] == 'S2'
    assert [step['kind'] for step in chain['steps']] == ['store', 'handoff', 'seal_check']
    assert {step['pallet'] for step in chain['steps']} == {'P1'}
    assert {step['seal'] for step in chain['steps']} == {'SEAL-P1'}
    assert all(step['valid_through_here'] for step in chain['steps'])


def test_missing_receiver_signature_freezes_physical_pallet(tmp_path):
    doc = base_doc()
    doc['custody'][1]['signed_by'] = ['S1']  # S2 never signed the handoff
    db = tmp_path / 'ledger.db'
    freeze_config(db, FIX / 'config.json')
    import_file(db, FIX / 'events.jsonl')
    evaluate(db)
    unclaimed.register(db, CASE, write_case(tmp_path, doc))
    status = unclaimed.status(db, CASE)
    assert status['status'] == 'blocked'
    assert unclaimed.CHAIN_SIGNATURE in status['custody_problems']
    result = unclaimed.authorize(db, CASE, 'SHIPPER-A', A_CONTRACT)
    assert result['ok'] is False
    assert unclaimed.CHAIN_SIGNATURE in result['problems']
    assert unclaimed.status(db, CASE)['status'] != 'released'


def test_unknown_custodian_freezes(tmp_path):
    doc = base_doc()
    doc['custody'][1]['from'] = 'SX'  # handoff does not start from the current holder
    db = tmp_path / 'ledger.db'
    freeze_config(db, FIX / 'config.json')
    import_file(db, FIX / 'events.jsonl')
    evaluate(db)
    unclaimed.register(db, CASE, write_case(tmp_path, doc))
    status = unclaimed.status(db, CASE)
    assert status['status'] == 'blocked'
    assert unclaimed.CHAIN_DELIVERER in status['custody_problems']


def test_seal_change_anywhere_freezes(tmp_path):
    doc = base_doc()
    doc['custody'][2]['seal'] = 'SEAL-OTHER'
    db = tmp_path / 'ledger.db'
    freeze_config(db, FIX / 'config.json')
    import_file(db, FIX / 'events.jsonl')
    evaluate(db)
    unclaimed.register(db, CASE, write_case(tmp_path, doc))
    status = unclaimed.status(db, CASE)
    assert status['status'] == 'blocked'
    assert unclaimed.CHAIN_SEAL_CHANGED in status['custody_problems']
    assert unclaimed.authorize(db, CASE, 'SHIPPER-A', A_CONTRACT)['ok'] is False


def test_seal_check_is_required(tmp_path):
    doc = base_doc()
    doc['custody'] = doc['custody'][:2]
    db = tmp_path / 'ledger.db'
    freeze_config(db, FIX / 'config.json')
    import_file(db, FIX / 'events.jsonl')
    evaluate(db)
    unclaimed.register(db, CASE, write_case(tmp_path, doc))
    assert unclaimed.CHAIN_SEAL_CHECK in unclaimed.status(db, CASE)['custody_problems']


def test_manifest_pallet_mismatch_is_identity_conflict(tmp_path):
    doc = base_doc()
    doc['pallet_id'] = 'P-DOES-NOT-EXIST'
    db = tmp_path / 'ledger.db'
    freeze_config(db, FIX / 'config.json')
    import_file(db, FIX / 'events.jsonl')
    evaluate(db)
    with pytest.raises(ValueError, match='unknown_unclaimed_pallet'):
        unclaimed.register(db, CASE, write_case(tmp_path, doc))


# ---------------------------------------------------------------------------
# Exclusive contracts
# ---------------------------------------------------------------------------

def test_registered_consignor_contract_releases_shipper_only(tmp_path):
    db, _, _ = prepare(tmp_path)
    result = unclaimed.authorize(db, CASE, 'BEARER-B', A_CONTRACT)
    assert result['ok'] is False and unclaimed.CLAIMANT_MISMATCH in result['problems']
    auth = unclaimed.authorize(db, CASE, 'SHIPPER-A', A_CONTRACT)
    assert auth['ok'] is True
    release = unclaimed.deliver(db, CASE, auth['authorization'])
    assert release['ok'] is True
    assert release['release']['claimant'] == 'SHIPPER-A'
    assert release['release']['deliverer'] == 'S2'
    assert release['release']['contract'] == A_CONTRACT


def test_bearer_contract_releases_bearer_only(tmp_path):
    db, _, _ = prepare(tmp_path, case_file='unclaimed-case-bearer.json')
    result = unclaimed.authorize(db, CASE, 'SHIPPER-A', B_CONTRACT)
    assert result['ok'] is False and unclaimed.CLAIMANT_MISMATCH in result['problems']
    auth = unclaimed.authorize(db, CASE, 'BEARER-B', B_CONTRACT)
    assert auth['ok'] is True
    release = unclaimed.deliver(db, CASE, auth['authorization'])
    assert release['ok'] is True
    assert release['release']['claimant'] == 'BEARER-B'
    assert release['release']['seal'] == 'SEAL-P1'
    assert release['release']['source_event_ids'] == [1, 2, 3, 4, 5]


def test_both_contracts_are_ambiguous_without_explicit_choice(tmp_path):
    db, _, _ = prepare(tmp_path)
    assert unclaimed.status(db, CASE)['contract_problem'] == unclaimed.CONTRACT_AMBIGUOUS
    result = unclaimed.authorize(db, CASE, 'SHIPPER-A')
    assert result['ok'] is False
    # An explicit exclusive contract resolves the ambiguity.
    assert unclaimed.authorize(db, CASE, 'SHIPPER-A', A_CONTRACT)['ok'] is True


def test_verify_claimants_shows_exclusivity(tmp_path):
    db, _, _ = prepare(tmp_path)
    matrix = unclaimed.verify_claimants(db, CASE)['claims']
    assert matrix[A_CONTRACT]['SHIPPER-A']['eligible'] is True
    assert matrix[A_CONTRACT]['BEARER-B']['eligible'] is False
    assert matrix[B_CONTRACT]['BEARER-B']['eligible'] is True
    assert matrix[B_CONTRACT]['SHIPPER-A']['eligible'] is False
    # Verification alone never moves the pallet.
    assert unclaimed.status(db, CASE)['status'] == 'in_custody'
    assert unclaimed.status(db, CASE)['release'] is None


def test_duplicate_receipt_digest_is_rejected_across_cases(tmp_path):
    db = tmp_path / 'ledger.db'
    freeze_config(db, FIX / 'config-two-pallets.json')
    import_file(db, FIX / 'events-two-pallets.jsonl')
    evaluate(db)
    unclaimed.register(db, 'case-p1', write_case(tmp_path, base_doc(), 'p1.json'))
    p2 = json.loads((FIX / 'unclaimed-case-p2.json').read_text())
    unclaimed.register(db, 'case-p2', write_case(tmp_path, p2, 'p2.json'))
    auth = unclaimed.authorize(db, 'case-p1', 'BEARER-B', B_CONTRACT)
    assert auth['ok'] is True
    assert unclaimed.deliver(db, 'case-p1', auth['authorization'])['ok'] is True
    duplicate = unclaimed.authorize(db, 'case-p2', 'BEARER-D', B_CONTRACT)
    assert duplicate['ok'] is False
    assert unclaimed.CREDENTIAL_REUSED in duplicate['problems']


def test_wrong_receipt_digest_is_rejected(tmp_path):
    doc = base_doc()
    doc['contracts'] = [B_CONTRACT]
    doc['claims'][1]['presented_digest'] = 'sha256:' + '0' * 64
    db = tmp_path / 'ledger.db'
    freeze_config(db, FIX / 'config.json')
    import_file(db, FIX / 'events.jsonl')
    evaluate(db)
    unclaimed.register(db, CASE, write_case(tmp_path, doc))
    result = unclaimed.authorize(db, CASE, 'BEARER-B', B_CONTRACT)
    assert result['ok'] is False and unclaimed.CREDENTIAL_MISMATCH in result['problems']


def test_manifest_pallet_must_match_for_consignor(tmp_path):
    doc = base_doc()
    doc['contracts'] = [A_CONTRACT]
    doc['claims'][0]['manifest_pallet'] = 'P9'
    db = tmp_path / 'ledger.db'
    freeze_config(db, FIX / 'config.json')
    import_file(db, FIX / 'events.jsonl')
    evaluate(db)
    unclaimed.register(db, CASE, write_case(tmp_path, doc))
    result = unclaimed.authorize(db, CASE, 'SHIPPER-A', A_CONTRACT)
    assert result['ok'] is False and unclaimed.MANIFEST_MISMATCH in result['problems']


# ---------------------------------------------------------------------------
# One-time delivery and versions
# ---------------------------------------------------------------------------

def test_successful_delivery_is_one_time(tmp_path):
    db, _, _ = prepare(tmp_path)
    auth = unclaimed.authorize(db, CASE, 'SHIPPER-A', A_CONTRACT)
    first = unclaimed.deliver(db, CASE, auth['authorization'])
    assert first['ok'] is True
    second = unclaimed.deliver(db, CASE, auth['authorization'])
    assert second['ok'] is False and unclaimed.AUTH_CONSUMED in second['problems']
    status = unclaimed.status(db, CASE)
    assert status['status'] == 'released'
    assert status['holder'] == 'SHIPPER-A'
    conn = connect(db)
    try:
        assert conn.execute('SELECT COUNT(*) FROM unclaimed_release').fetchone()[0] == 1
    finally:
        conn.close()


def test_concurrent_double_release_yields_exactly_one(tmp_path):
    db, _, _ = prepare(tmp_path)
    auth = unclaimed.authorize(db, CASE, 'SHIPPER-A', A_CONTRACT)
    results = []
    barrier = threading.Barrier(2)

    def worker():
        barrier.wait()
        try:
            results.append(unclaimed.deliver(db, CASE, auth['authorization']))
        except sqlite3.OperationalError as exc:  # pragma: no cover - defensive
            results.append({'ok': False, 'problems': [str(exc)], 'release': None})

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sum(1 for r in results if r['ok']) == 1
    conn = connect(db)
    try:
        assert conn.execute('SELECT COUNT(*) FROM unclaimed_release').fetchone()[0] == 1
    finally:
        conn.close()


def test_concurrent_competing_claimants_only_one_is_physically_released(tmp_path):
    # Both contracts are executable against the same fixture; whichever
    # authorization wins the race may deliver, the loser must be refused.
    db, _, _ = prepare(tmp_path)
    a_auth = unclaimed.authorize(db, CASE, 'SHIPPER-A', A_CONTRACT)
    b_auth = unclaimed.authorize(db, CASE, 'BEARER-B', B_CONTRACT)
    assert a_auth['ok'] and b_auth['ok']
    results = []
    barrier = threading.Barrier(2)

    def worker(auth):
        barrier.wait()
        results.append(unclaimed.deliver(db, CASE, auth['authorization']))

    threads = [threading.Thread(target=worker, args=(auth,)) for auth in (a_auth, b_auth)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    winners = [r for r in results if r['ok']]
    assert len(winners) == 1
    conn = connect(db)
    try:
        rows = conn.execute('SELECT claimant FROM unclaimed_release').fetchall()
        assert len(rows) == 1
        assert rows[0]['claimant'] == winners[0]['release']['claimant']
    finally:
        conn.close()


def test_shift_change_after_verification_invalidates_authorization(tmp_path):
    db, _, _ = prepare(tmp_path)
    auth = unclaimed.authorize(db, CASE, 'SHIPPER-A', A_CONTRACT)
    handoff = unclaimed.record_handoff(db, CASE, 'C4', 'S2', 'S3', 'SEAL-P1', ['S2', 'S3'])
    assert handoff['ok'] is True
    refused = unclaimed.deliver(db, CASE, auth['authorization'])
    assert refused['ok'] is False
    assert unclaimed.AUTH_STALE in refused['problems']
    assert unclaimed.DELIVERER_CHANGED in refused['problems']
    assert unclaimed.status(db, CASE)['status'] == 'in_custody'
    # The new holder can re-authorize on the new version and release.
    renewed = unclaimed.authorize(db, CASE, 'SHIPPER-A', A_CONTRACT)
    assert renewed['ok'] is True
    done = unclaimed.deliver(db, CASE, renewed['authorization'])
    assert done['ok'] is True and done['release']['deliverer'] == 'S3'


def test_handoff_requires_both_signatures(tmp_path):
    db, _, _ = prepare(tmp_path)
    refused = unclaimed.record_handoff(db, CASE, 'C4', 'S2', 'S3', 'SEAL-P1', ['S2'])
    assert refused['ok'] is False and unclaimed.CHAIN_SIGNATURE in refused['problems']
    wrong_holder = unclaimed.record_handoff(db, CASE, 'C4', 'S1', 'S3', 'SEAL-P1', ['S1', 'S3'])
    assert wrong_holder['ok'] is False and unclaimed.CHAIN_DELIVERER in wrong_holder['problems']


def test_handoff_rejects_changed_seal(tmp_path):
    db, _, _ = prepare(tmp_path)
    refused = unclaimed.record_handoff(db, CASE, 'C4', 'S2', 'S3', 'SEAL-OTHER', ['S2', 'S3'])
    assert refused['ok'] is False and unclaimed.CHAIN_SEAL_CHANGED in refused['problems']


def test_unrelated_pallets_are_isolated(tmp_path):
    db = tmp_path / 'ledger.db'
    freeze_config(db, FIX / 'config-two-pallets.json')
    import_file(db, FIX / 'events-two-pallets.jsonl')
    evaluate(db)
    unclaimed.register(db, 'case-p1', write_case(tmp_path, base_doc(), 'p1.json'))
    p2 = json.loads((FIX / 'unclaimed-case-p2.json').read_text())
    unclaimed.register(db, 'case-p2', write_case(tmp_path, p2, 'p2.json'))
    auth = unclaimed.authorize(db, 'case-p1', 'SHIPPER-A', A_CONTRACT)
    assert unclaimed.deliver(db, 'case-p1', auth['authorization'])['ok'] is True
    other = unclaimed.status(db, 'case-p2')
    assert other['status'] == 'in_custody'
    assert other['release'] is None
    assert other['pallet'] == 'P2'
    assert unclaimed.authorize(db, 'case-p2', 'SHIPPER-C', A_CONTRACT)['ok'] is True


def test_authorization_bound_to_pallet_version_not_text(tmp_path):
    db, _, _ = prepare(tmp_path)
    auth = unclaimed.authorize(db, CASE, 'SHIPPER-A', A_CONTRACT)
    assert auth['reused'] is False
    recheck = unclaimed.record_seal_check(db, CASE, 'C4', 'S2', 'SEAL-P1')
    assert recheck['ok'] is True
    refused = unclaimed.deliver(db, CASE, auth['authorization'])
    assert refused['ok'] is False and unclaimed.AUTH_STALE in refused['problems']
    # Re-verifying on the new version succeeds and still releases only once.
    renewed = unclaimed.authorize(db, CASE, 'SHIPPER-A', A_CONTRACT)
    assert renewed['ok'] is True and renewed['authorization'] != auth['authorization']
    assert unclaimed.deliver(db, CASE, renewed['authorization'])['ok'] is True


def test_reauthorization_before_delivery_is_idempotent(tmp_path):
    db, _, _ = prepare(tmp_path)
    first = unclaimed.authorize(db, CASE, 'SHIPPER-A', A_CONTRACT)
    second = unclaimed.authorize(db, CASE, 'SHIPPER-A', A_CONTRACT)
    assert second['ok'] is True and second['reused'] is True
    assert second['authorization'] == first['authorization']
    conn = connect(db)
    try:
        assert conn.execute('SELECT COUNT(*) FROM unclaimed_authorization').fetchone()[0] == 1
    finally:
        conn.close()


def test_seal_recheck_rejects_broken_seal(tmp_path):
    db, _, _ = prepare(tmp_path)
    broken = unclaimed.record_seal_check(db, CASE, 'C4', 'S2', 'SEAL-BROKEN')
    assert broken['ok'] is False and unclaimed.CHAIN_SEAL_CHANGED in broken['problems']
    assert unclaimed.status(db, CASE)['status'] == 'in_custody'


def test_delivery_rejects_foreign_or_wrong_case_authorization(tmp_path):
    db, _, _ = prepare(tmp_path)
    assert unclaimed.deliver(db, CASE, 'no-such-auth')['problems'] == [unclaimed.AUTH_NOT_FOUND]
    auth = unclaimed.authorize(db, CASE, 'SHIPPER-A', A_CONTRACT)
    wrong = unclaimed.deliver(db, 'other-case', auth['authorization'])
    assert wrong['ok'] is False
    # The valid case still owns the pallet; the wrong-case attempt changed nothing.
    assert unclaimed.deliver(db, CASE, auth['authorization'])['ok'] is True


# ---------------------------------------------------------------------------
# Interruption / recovery
# ---------------------------------------------------------------------------

def test_failed_delivery_rolls_back_and_is_retryable(tmp_path):
    db, _, _ = prepare(tmp_path)
    auth = unclaimed.authorize(db, CASE, 'SHIPPER-A', A_CONTRACT)

    def interrupt(_conn):
        raise RuntimeError('power loss')

    with pytest.raises(RuntimeError):
        unclaimed.deliver(db, CASE, auth['authorization'], commit_hook=interrupt)
    state = unclaimed.status(db, CASE)
    assert state['status'] == 'in_custody'
    assert state['release'] is None
    assert state['authorizations'][0]['state'] == 'active'
    conn = connect(db)
    try:
        assert conn.execute('SELECT COUNT(*) FROM unclaimed_release').fetchone()[0] == 0
    finally:
        conn.close()
    retry = unclaimed.deliver(db, CASE, auth['authorization'])
    assert retry['ok'] is True
    assert unclaimed.status(db, CASE)['status'] == 'released'
    assert unclaimed.deliver(db, CASE, auth['authorization'])['ok'] is False


def test_released_pallet_cannot_be_reauthored(tmp_path):
    db, _, _ = prepare(tmp_path)
    auth = unclaimed.authorize(db, CASE, 'SHIPPER-A', A_CONTRACT)
    unclaimed.deliver(db, CASE, auth['authorization'])
    # Even the losing contract cannot re-authorize or move a delivered pallet.
    assert unclaimed.authorize(db, CASE, 'BEARER-B', B_CONTRACT)['ok'] is False
    assert unclaimed.verify(db, CASE, 'SHIPPER-A', A_CONTRACT)['eligible'] is False


def test_accepted_receipt_decision_is_not_pickup_authorization(tmp_path):
    db, _, _ = prepare(tmp_path)
    conn = connect(db)
    try:
        decision = conn.execute("SELECT status FROM decision WHERE subject='B1'").fetchone()['status']
    finally:
        conn.close()
    assert decision == 'accepted'
    # The pallet is still physically in custody: nothing was released.
    status = unclaimed.status(db, CASE)
    assert status['status'] == 'in_custody' and status['release'] is None
    # A claimant cannot skip a contract merely because the receipt was accepted;
    # without an explicit contract the counter is ambiguous and refuses.
    assert unclaimed.authorize(db, CASE, 'SHIPPER-A')['ok'] is False
    assert unclaimed.deliver(db, CASE, 'unclaimed-01:Q-A')['ok'] is False


def test_state_survives_reconnect_after_failed_transaction(tmp_path):
    db, _, _ = prepare(tmp_path)
    auth = unclaimed.authorize(db, CASE, 'SHIPPER-A', A_CONTRACT)

    def interrupt(_conn):
        raise RuntimeError('interrupted')

    with pytest.raises(RuntimeError):
        unclaimed.deliver(db, CASE, auth['authorization'], commit_hook=interrupt)
    # A brand-new connection sees a coherent pre-delivery state.
    path = db
    conn = connect(path)
    try:
        assert conn.execute('SELECT status FROM pallet_custody').fetchone()[0] == 'in_custody'
        assert conn.execute("SELECT state FROM unclaimed_authorization").fetchone()[0] == 'active'
        assert conn.execute('SELECT COUNT(*) FROM unclaimed_release').fetchone()[0] == 0
    finally:
        conn.close()
    assert unclaimed.deliver(db, CASE, auth['authorization'])['ok'] is True


# ---------------------------------------------------------------------------
# Report + ingest identity
# ---------------------------------------------------------------------------

def test_report_reflects_actual_release_and_hides_raw_credential(tmp_path):
    db, doc, _ = prepare(tmp_path)
    auth = unclaimed.authorize(db, CASE, 'BEARER-B', B_CONTRACT)
    unclaimed.deliver(db, CASE, auth['authorization'])
    path = export(db, tmp_path / 'report.json')
    data = json.loads(path.read_text(encoding='utf-8'))
    case = data['unclaimed'][0]
    assert case['status'] == 'released'
    assert case['delivered_to'] == 'BEARER-B'
    assert case['delivered_by'] == 'S2'
    assert case['release_evidence']['source_event_ids'] == [1, 2, 3, 4, 5]
    assert case['release_evidence']['credential_digest'] == doc['receipt_digest']
    assert doc['receipt_digest'] in json.dumps(data)
    # The receiver name / raw evidence line must not leak a reusable token.
    assert 'presented_digest' not in json.dumps(data)
    markdown = path.with_suffix('.md').read_text(encoding='utf-8')
    assert 'BEARER-B' in markdown and '已交出' in markdown
    assert case['custody']['valid'] is True


def test_ingest_source_identity_gates_registration(tmp_path):
    db = tmp_path / 'ledger.db'
    freeze_config(db, FIX / 'config.json')
    # No events imported yet: the pallet has no physical presence to prove.
    with pytest.raises(ValueError, match='unclaimed_source_events_missing'):
        unclaimed.register(db, CASE, FIX / 'unclaimed-case.json')


def test_tampered_source_identity_blocks_authorization(tmp_path):
    db, _, _ = prepare(tmp_path)
    conn = connect(db)
    try:
        conn.execute("UPDATE pallet_custody SET source_fingerprint='deadbeef'")
        conn.commit()
    finally:
        conn.close()
    state = unclaimed.status(db, CASE)
    assert state['identity_valid'] is False
    assert unclaimed.IDENTITY_CONFLICT in state['custody_problems']
    result = unclaimed.authorize(db, CASE, 'SHIPPER-A', A_CONTRACT)
    assert result['ok'] is False and unclaimed.IDENTITY_CONFLICT in result['problems']
    # Reports show the frozen verdict instead of a delivery.
    data = json.loads(export(db, tmp_path / 'r.json').read_text(encoding='utf-8'))
    assert data['unclaimed'][0]['identity_valid'] is False
    assert data['unclaimed'][0]['released'] is None


def test_runtime_handoff_keeps_chain_valid_and_orders_after_fixture(tmp_path):
    db, _, _ = prepare(tmp_path)
    result = unclaimed.record_handoff(db, CASE, 'C4', 'S2', 'S3', 'SEAL-P1', ['S2', 'S3'])
    assert result['ok'] is True
    chain = unclaimed.custody_chain(db, CASE)
    assert chain['valid'] is True
    assert chain['holder'] == 'S3'
    times = [step['at'] for step in chain['steps']]
    assert times == sorted(times)
    assert chain['steps'][-1]['origin'] == 'runtime'


def test_same_pallet_cannot_be_bound_to_two_cases(tmp_path):
    db, doc, _ = prepare(tmp_path)
    with pytest.raises(ValueError, match='pallet_bound_elsewhere'):
        unclaimed.register(db, 'other-case', write_case(tmp_path, doc, 'other.json'))


def test_cli_smoke(tmp_path, capsys):
    from dockscan.__main__ import main
    import sys
    workdir = tmp_path / 'cli'
    sys.argv = ['dockscan', 'demo', '--workdir', str(workdir)]
    main()
    report = workdir / 'report.json'
    assert report.exists()
    data = json.loads(report.read_text(encoding='utf-8'))
    assert data['unclaimed'][0]['status'] == 'released'
    sys.argv = ['dockscan', 'unclaimed-status', '--db', str(workdir / 'ledger.db'), '--case', CASE]
    main()


def test_cli_custody_verify_authorize_deliver(tmp_path, capsys):
    from dockscan.__main__ import main
    import sys

    def run(args):
        sys.argv = ['dockscan'] + args
        main()
        return json.loads(capsys.readouterr().out)

    db = tmp_path / 'cli.db'
    run(['ingest', '--db', str(db), '--config', str(FIX / 'config.json'),
         str(FIX / 'events.jsonl')])
    run(['inspect', '--db', str(db)])
    run(['unclaimed-register', '--db', str(db), '--case', CASE, str(FIX / 'unclaimed-case.json')])
    chain = run(['unclaimed-custody', '--db', str(db), '--case', CASE])
    assert chain['valid'] is True
    verified = run(['unclaimed-verify', '--db', str(db), '--case', CASE,
                    '--claimant', 'SHIPPER-A', '--contract', A_CONTRACT])
    assert verified['eligible'] is True and verified['status'] == 'in_custody'
    auth = run(['unclaimed-authorize', '--db', str(db), '--case', CASE,
                '--claimant', 'SHIPPER-A', '--contract', A_CONTRACT])
    assert auth['ok'] is True
    delivered = run(['unclaimed-deliver', '--db', str(db), '--case', CASE,
                     '--authorization', auth['authorization']])
    assert delivered['ok'] is True
    sys.argv = ['dockscan', 'report', '--db', str(db), '--out', str(tmp_path / 'r.json')]
    main()
    report = json.loads((tmp_path / 'r.json').read_text(encoding='utf-8'))
    case = report['unclaimed'][0]
    assert case['status'] == 'released' and case['delivered_to'] == 'SHIPPER-A'
    assert case['custody']['steps'][-1]['kind'] == 'release'


def test_new_scan_evidence_after_registration_freezes_release(tmp_path):
    """The frozen source identity must still describe the same scan set."""
    db, _, _ = prepare(tmp_path)
    extra = tmp_path / 'late.jsonl'
    extra.write_text(json.dumps({'kind': 'gate', 'subject': 'B1', 'identity': 'D1:99',
                                 'time': '2026-01-01T08:40:00Z', 'dock': 'D1'}) + '\n')
    import_file(db, extra)
    state = unclaimed.status(db, CASE)
    assert state['identity_valid'] is False
    assert unclaimed.authorize(db, CASE, 'SHIPPER-A', A_CONTRACT)['ok'] is False
    assert unclaimed.status(db, CASE)['status'] != 'released'


def test_report_markdown_has_release_row_and_no_raw_receipt(tmp_path):
    db, doc, _ = prepare(tmp_path)
    auth = unclaimed.authorize(db, CASE, 'SHIPPER-A', A_CONTRACT)
    unclaimed.deliver(db, CASE, auth['authorization'])
    path = export(db, tmp_path / 'report.json')
    markdown = path.with_suffix('.md').read_text(encoding='utf-8')
    assert 'release' in markdown and 'SHIPPER-A' in markdown
    assert 'presented_digest' not in markdown
    data = json.loads(path.read_text(encoding='utf-8'))
    steps = data['unclaimed'][0]['custody']['steps']
    assert steps[-1]['kind'] == 'release'
    assert steps[-1]['to_holder'] == 'SHIPPER-A'
    assert data['unclaimed'][0]['release_evidence']['seal'] == 'SEAL-P1'
