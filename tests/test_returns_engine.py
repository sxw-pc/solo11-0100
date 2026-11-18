import copy
import json
from pathlib import Path

import pytest

from dockscan.ingest import freeze_config, import_file
from dockscan.rules import evaluate
from dockscan.report import export
from dockscan.returns import register, add_amendment, process
from dockscan.store import connect

FIX = Path(__file__).resolve().parents[1] / 'fixtures'
OLDEST = 'oldest_complete_rma_first'
MAXC = 'maximum_complete_customers'


def _seed(tmp_path, case_doc=None, events=None, config=None, case_id='return-01'):
    db = tmp_path / 'returns.db'
    cfg = config or (FIX / 'return-forward-config.json')
    ev = events or (FIX / 'return-forward-events.jsonl')
    if isinstance(ev, list):
        ev_path = tmp_path / 'events.jsonl'
        ev_path.write_text('\n'.join(json.dumps(e) for e in ev) + '\n')
        ev = ev_path
    freeze_config(db, cfg)
    import_file(db, ev)
    evaluate(db)
    if case_doc is None:
        case_fixture = FIX / 'return-case.json'
        register(db, case_id, case_fixture)
    else:
        case_fixture = tmp_path / 'case.json'
        case_fixture.write_text(json.dumps(case_doc))
        register(db, case_id, case_fixture)
    return db


def _base_case():
    return json.loads((FIX / 'return-case.json').read_text())


def _base_events():
    return [json.loads(line) for line in
            (FIX / 'return-forward-events.jsonl').read_text().splitlines()]


def _refunds(summary):
    return {r['rma_id']: r['refund_amount'] for r in summary['rmas']}


def _quarantine_codes(db, run_id):
    con = connect(db)
    rows = con.execute(
        'SELECT ref,code FROM return_issue WHERE run_id=? ORDER BY ref,code', (run_id,)).fetchall()
    con.close()
    out = {}
    for r in rows:
        out.setdefault(r['ref'], set()).add(r['code'])
    return out


def test_oldest_first_contract_matches_spec(tmp_path):
    db = _seed(tmp_path)
    s = process(db, 'return-01', OLDEST)
    assert s['resellable_units'] == ['U1', 'U2', 'U4']
    assert s['awaiting_repair_units'] == ['U3', 'U5']
    assert s['quarantined_units'] == {}
    assert _refunds(s) == {'RMA-A': 200, 'RMA-B': 0, 'RMA-C': 80, 'RMA-D': 0}
    assert s['refund_total'] == 280
    assert s['stations_used'] == 2 and s['repair_slots'] == 2
    assert s['duplicate_scans'] == 1


def test_max_customers_contract_matches_spec(tmp_path):
    db = _seed(tmp_path)
    s = process(db, 'return-01', MAXC)
    assert s['resellable_units'] == ['U3', 'U4', 'U5']
    assert s['awaiting_repair_units'] == ['U1', 'U2']
    assert _refunds(s) == {'RMA-A': 0, 'RMA-B': 120, 'RMA-C': 80, 'RMA-D': 150}
    assert s['refund_total'] == 350
    assert s['stations_used'] == 2


def test_duplicate_scan_is_one_inventory_and_one_refund(tmp_path):
    db = _seed(tmp_path)
    s = process(db, 'return-01', OLDEST)
    con = connect(db)
    run_id = s['run_id']
    # one inventory ledger row per serial, not one per scan
    rows = con.execute(
        'SELECT unit,COUNT(*) c FROM inventory_ledger WHERE run_id=? GROUP BY unit',
        (run_id,)).fetchall()
    assert {r['unit']: r['c'] for r in rows} == {u: 1 for u in ('U1', 'U2', 'U3', 'U4', 'U5')}
    refund = con.execute(
        'SELECT amount FROM refund_ledger WHERE run_id=? AND rma_id=?',
        (run_id, 'RMA-A')).fetchone()
    assert refund['amount'] == 200  # U1+U2 counted once despite the repeated U2 scan
    issues = _quarantine_codes(db, run_id)
    assert 'duplicate_scan' in issues['U2']
    trace = con.execute(
        'SELECT physical_state FROM unit_trace WHERE run_id=? AND unit=?',
        (run_id, 'U2')).fetchone()
    assert trace['physical_state'] == 'repaired'
    con.close()


def test_wrong_child_box_quarantines_and_rma_group_pays_nothing(tmp_path):
    doc = _base_case()
    doc['parcels'][1]['source_child_box'] = 'B1a'  # paperwork claims B1a for B1b units
    db = _seed(tmp_path, case_doc=doc)
    s = process(db, 'return-01', OLDEST)
    codes = _quarantine_codes(db, s['run_id'])
    assert 'wrong_child_box' in codes['U3']
    assert 'wrong_child_box' in codes['U5']
    assert s['quarantined_units']['U3'] == ['wrong_child_box']
    # group RMA: U3 quarantined means RMA-B cannot be completed even though it is a 1-unit RMA
    assert _refunds(s)['RMA-B'] == 0
    con = connect(db)
    tr = con.execute(
        "SELECT evidence FROM unit_trace WHERE run_id=? AND unit='U3'",
        (s['run_id'],)).fetchone()
    # ownership proven by lineage event, not parcel text
    assert any('lineage:B1->B1b' in e for e in json.loads(tr['evidence']))
    con.close()


def test_unauthorized_and_untraced_unit_is_quarantined_via_amendment(tmp_path):
    db = _seed(tmp_path)
    add_amendment(db, 'return-01', FIX / 'returns' / 'amend-unauthorized-parcel.json')
    s = process(db, 'return-01', OLDEST)
    assert set(s['quarantined_units']['U9']) >= {
        'lineage_unit_untraced', 'unit_not_in_authorized_rma'}
    # an unrecognized serial can never become stock or a refund
    assert 'U9' not in s['resellable_units']
    assert s['refund_total'] == 280


def test_repair_capacity_exact_fit_and_shortage(tmp_path):
    db = _seed(tmp_path)
    s = process(db, 'return-01', OLDEST)
    assert s['stations_used'] == s['repair_slots']
    con = connect(db)
    held = con.execute(
        "SELECT unit,reasons FROM unit_trace WHERE run_id=? AND physical_state='awaiting_repair'",
        (s['run_id'],)).fetchall()
    assert {r['unit'] for r in held} == {'U3', 'U5'}
    assert all('repair_capacity_exceeded' in json.loads(r['reasons']) for r in held)
    con.close()

    doc = _base_case()
    doc['repair']['slot_count'] = 0
    db0 = _seed(tmp_path / 'zero', case_doc=doc)
    s0 = process(db0, 'return-01', OLDEST)
    assert s0['resellable_units'] == ['U4']
    assert sorted(s0['awaiting_repair_units']) == ['U1', 'U2', 'U3', 'U5']
    assert _refunds(s0) == {'RMA-A': 0, 'RMA-B': 0, 'RMA-C': 80, 'RMA-D': 0}


def test_group_rma_partial_completion_never_pays_partial(tmp_path):
    doc = _base_case()
    # U2 paperwork claims the wrong child box -> U2 must be quarantined
    doc['parcels'][0]['source_child_box'] = 'B1b'
    db = _seed(tmp_path, case_doc=doc)
    s = process(db, 'return-01', OLDEST)
    assert _refunds(s)['RMA-A'] == 0  # U1 repaired alone is not full performance
    assert any(r['rma_id'] == 'RMA-A' and r['complete'] is False for r in s['rmas'])


def test_rma_deadline_boundary_inclusive_then_exclusive(tmp_path):
    on_time, late = _base_case(), _base_case()
    for d in (on_time, late):
        d['parcels'][0]['received_at'] = '2026-01-03T12:00:00Z'
        d['parcels'][1]['received_at'] = '2026-01-03T12:00:00Z'
    late['parcels'][0]['received_at'] = '2026-01-03T12:00:01Z'

    db_ok = _seed(tmp_path / 'ok', case_doc=on_time)
    s_ok = process(db_ok, 'return-01', OLDEST)
    assert 'rma_deadline_passed' not in _quarantine_codes(db_ok, s_ok['run_id']).get('U1', set())

    db_late = _seed(tmp_path / 'late', case_doc=late)
    s_late = process(db_late, 'return-01', OLDEST)
    codes = _quarantine_codes(db_late, s_late['run_id'])
    assert 'rma_deadline_passed' in codes['U1']
    assert _refunds(s_late)['RMA-A'] == 0


def test_process_is_idempotent_and_immutable(tmp_path):
    db = _seed(tmp_path)
    s1 = process(db, 'return-01', OLDEST)
    s2 = process(db, 'return-01', OLDEST)
    assert s2.get('idempotent') is True
    con = connect(db)
    assert con.execute('SELECT COUNT(*) FROM return_run').fetchone()[0] == 1
    assert con.execute('SELECT COUNT(*) FROM inventory_ledger').fetchone()[0] == 5
    assert con.execute('SELECT COUNT(*) FROM refund_ledger').fetchone()[0] == 2
    # original evidence tables are untouched
    assert con.execute('SELECT COUNT(*) FROM source').fetchone()[0] == 1
    assert con.execute("SELECT status FROM decision WHERE subject='B1'").fetchone()[0] == 'accepted'
    con.close()


def test_amendments_create_auditable_supplement_and_reversal_chain(tmp_path):
    db = _seed(tmp_path)
    s1 = process(db, 'return-01', OLDEST)
    add_amendment(db, 'return-01', FIX / 'returns' / 'amend-u4-opened-no-repair.json')
    s2 = process(db, 'return-01', OLDEST)
    add_amendment(db, 'return-01', FIX / 'returns' / 'amend-u4-seal-restored.json')
    s3 = process(db, 'return-01', OLDEST)

    con = connect(db)
    rows = con.execute(
        'SELECT id,run_id,movement,amount,settled_after,prior_id FROM refund_ledger '
        "WHERE rma_id='RMA-C' ORDER BY id").fetchall()
    assert [(r['movement'], r['amount'], r['settled_after']) for r in rows] == [
        ('refund', 80, 80), ('reversal', 80, 0), ('refund', 80, 80)]
    assert rows[1]['prior_id'] == rows[0]['id']
    assert rows[2]['prior_id'] == rows[1]['id']
    # unaffected RMA keeps exactly its original entry across all three versions
    a_rows = con.execute(
        'SELECT COUNT(*) c FROM refund_ledger WHERE rma_id=?', ('RMA-A',)).fetchone()
    assert a_rows['c'] == 1
    inv = con.execute(
        'SELECT run_id,action,prior_id FROM inventory_ledger WHERE unit=? ORDER BY id',
        ('U4',)).fetchall()
    assert [r['action'] for r in inv] == ['release', 'quarantine', 'release']
    assert inv[1]['prior_id'] == con.execute(
        'SELECT id FROM inventory_ledger WHERE run_id=? AND unit=?',
        (s1['run_id'], 'U4')).fetchone()['id']
    assert con.execute('SELECT COUNT(*) FROM return_run').fetchone()[0] == 3
    con.close()
    assert _refunds(s2)['RMA-C'] == 0
    assert _refunds(s3)['RMA-C'] == 80
    assert _refunds(s3)['RMA-A'] == 200


def test_origin_turning_blocked_quarantines_everything_and_reverses_refunds(tmp_path):
    db = _seed(tmp_path)
    process(db, 'return-01', OLDEST)
    # a later raw scan proves a weight mismatch; forward decision becomes blocked
    extra = tmp_path / 'extra.jsonl'
    extra.write_text(json.dumps({'kind': 'weight', 'subject': 'B1', 'identity': 'D1:7',
                                 'time': '2026-01-01T08:30:00Z', 'dock': 'D1', 'net_kg': 99}) + '\n')
    import_file(db, extra)
    assert evaluate(db)['B1'] == 'blocked'
    s = process(db, 'return-01', OLDEST)
    assert sorted(s['quarantined_units']) == ['U1', 'U2', 'U3', 'U4', 'U5']
    assert all('origin_not_accepted' in codes for codes in s['quarantined_units'].values())
    assert s['resellable_units'] == [] and s['refund_total'] == 0
    con = connect(db)
    # previously issued refunds are reversed, never silently overwritten
    movements = {r['movement']: r['amount'] for r in con.execute(
        'SELECT movement,amount FROM refund_ledger ORDER BY id')}
    assert movements.get('reversal') == 80
    assert con.execute('SELECT COUNT(*) FROM refund_ledger').fetchone()[0] == 4
    # original scans and the old accepted-era ledger row remain on disk
    assert con.execute('SELECT COUNT(*) FROM event').fetchone()[0] == 7
    con.close()


def test_duplicate_lineage_attribution_is_quarantined(tmp_path):
    events = _base_events()
    events.append({'kind': 'split', 'subject': 'B1', 'identity': 'D1:7',
                   'time': '2026-01-01T08:31:00Z', 'dock': 'D1', 'quantity': 1,
                   'children': [{'id': 'B1c', 'quantity': 1, 'units': ['U1']}]})
    db = _seed(tmp_path, events=events)
    s = process(db, 'return-01', OLDEST)
    codes = _quarantine_codes(db, s['run_id'])
    assert 'duplicate_lineage_attribution' in codes['U1']
    assert _refunds(s)['RMA-A'] == 0


def test_cross_parcel_duplicate_scan_does_not_double_count(tmp_path):
    db = _seed(tmp_path)
    amend = tmp_path / 'p3.json'
    amend.write_text(json.dumps({'kind': 'add_parcel', 'parcel': {
        'id': 'P3', 'source_child_box': 'B1b', 'received_at': '2026-01-02T10:00:00Z',
        'inspections': [{'unit': 'U4', 'condition': 'sealed', 'seal': 'intact'}]}}))
    add_amendment(db, 'return-01', amend)
    s = process(db, 'return-01', OLDEST)
    assert s['duplicate_scans'] == 2  # base U2 repeat plus cross-parcel U4 repeat
    con = connect(db)
    assert con.execute(
        'SELECT COUNT(*) FROM inventory_ledger WHERE unit=?', ('U4',)).fetchone()[0] == 1
    refund = con.execute(
        'SELECT SUM(amount) t FROM refund_ledger WHERE rma_id=?', ('RMA-C',)).fetchone()
    # one refund + no reversal/duplicate for RMA-C
    assert con.execute(
        'SELECT COUNT(*) c FROM refund_ledger WHERE rma_id=?', ('RMA-C',)).fetchone()['c'] == 1
    assert refund['t'] == 80
    con.close()


def test_broken_seal_routes_opened_path_and_consumes_station(tmp_path):
    doc = _base_case()
    doc['parcels'][1]['inspections'][1] = {'unit': 'U4', 'condition': 'sealed', 'seal': 'broken'}
    db = _seed(tmp_path / 'broken', case_doc=doc)
    s = process(db, 'return-01', OLDEST)
    assert 'U4' in s['awaiting_repair_units']  # both slots already spent on RMA-A
    assert _refunds(s)['RMA-C'] == 0


def test_inspection_conflict_is_quarantined(tmp_path):
    doc = _base_case()
    doc['parcels'][1]['inspections'][1] = {
        'unit': 'U4', 'condition': 'sealed', 'seal': 'intact', 'repair_cost': 10}
    db = _seed(tmp_path / 'conflict', case_doc=doc)
    s = process(db, 'return-01', OLDEST)
    assert 'inspection_conflict' in s['quarantined_units']['U4']


def test_contract_and_amendment_validation(tmp_path):
    db = _seed(tmp_path)
    with pytest.raises(ValueError, match='unknown_contract'):
        process(db, 'return-01', 'not-a-contract')
    doc = _base_case()
    doc['contracts'] = [OLDEST]
    db2 = _seed(tmp_path / 'limited', case_doc=doc)
    with pytest.raises(ValueError, match='contract_not_authorized_for_case'):
        process(db2, 'return-01', MAXC)
    bad = tmp_path / 'bad-amend.json'
    bad.write_text(json.dumps({'kind': 'add_parcel', 'parcel': {'id': 'PX'}}))
    with pytest.raises(ValueError, match='bad_amendment_parcel'):
        add_amendment(db, 'return-01', bad)
    with pytest.raises(ValueError, match='return_case_unknown'):
        add_amendment(db, 'missing-case', FIX / 'returns' / 'amend-u4-seal-restored.json')


def test_amendment_file_is_deduplicated(tmp_path):
    db = _seed(tmp_path)
    first = add_amendment(db, 'return-01', FIX / 'returns' / 'amend-u4-seal-restored.json')
    second = add_amendment(db, 'return-01', FIX / 'returns' / 'amend-u4-seal-restored.json')
    assert first['seq'] == second['seq'] and second['duplicate_file'] is True
    con = connect(db)
    assert con.execute('SELECT COUNT(*) FROM return_amendment').fetchone()[0] == 1
    con.close()


def test_unreturned_rma_unit_creates_no_stock_or_refund(tmp_path):
    doc = _base_case()
    doc['parcels'][1]['inspections'] = [
        r for r in doc['parcels'][1]['inspections'] if r['unit'] != 'U3']
    db = _seed(tmp_path / 'missing', case_doc=doc)
    s = process(db, 'return-01', MAXC)
    codes = _quarantine_codes(db, s['run_id'])
    assert codes.get('RMA-B') == {'rma_unit_not_returned'}
    assert _refunds(s)['RMA-B'] == 0
    assert 'U3' not in s['resellable_units'] and 'U3' not in s['quarantined_units']


def test_correct_parcel_amendment_reconciles_wrong_child_box(tmp_path):
    doc = _base_case()
    doc['parcels'][1]['source_child_box'] = 'B1a'  # misdeclared on the parcel
    db = _seed(tmp_path / 'wrongbox', case_doc=doc)
    s1 = process(db, 'return-01', OLDEST)
    assert 'wrong_child_box' in s1['quarantined_units']['U3']
    amend = tmp_path / 'fix-box.json'
    amend.write_text(json.dumps({'kind': 'correct_parcel', 'parcel_id': 'P2',
                                 'source_child_box': 'B1b'}))
    add_amendment(db, 'return-01', amend)
    s2 = process(db, 'return-01', OLDEST)
    assert 'U3' not in s2['quarantined_units']
    con = connect(db)
    assert con.execute(
        "SELECT kind FROM return_amendment WHERE case_id='return-01'").fetchone()['kind'] \
        == 'correct_parcel'
    con.close()


def test_ledger_chain_stays_within_one_contract(tmp_path):
    db = _seed(tmp_path)
    process(db, 'return-01', OLDEST)          # run 1
    process(db, 'return-01', MAXC)            # run 2, a separate decision lineage
    add_amendment(db, 'return-01', FIX / 'returns' / 'amend-u4-opened-no-repair.json')
    process(db, 'return-01', OLDEST)          # run 3 must chain onto run 1 only
    con = connect(db)
    by_rma = {}
    for r in con.execute(
            'SELECT l.rma_id,l.movement,l.prior_id,r.contract FROM refund_ledger l '
            'JOIN return_run r ON r.id=l.run_id ORDER BY l.id'):
        by_rma.setdefault(r['rma_id'], []).append(dict(r))
    # RMA-C: oldest run 1 refund, max run 2 refund, oldest run 3 reversal whose prior
    # must point at the run-1 row (same contract), never at the run-2 row.
    c_rows = by_rma['RMA-C']
    assert [r['movement'] for r in c_rows] == ['refund', 'refund', 'reversal']
    oldest_first = c_rows[0]
    assert c_rows[2]['prior_id'] == con.execute(
        'SELECT id FROM refund_ledger ORDER BY id LIMIT 1 OFFSET 1').fetchone()['id']
    assert c_rows[2]['contract'] == OLDEST
    assert c_rows[1]['contract'] == MAXC and c_rows[1]['prior_id'] is None
    # RMA-A exists only in the oldest lineage and is unchanged by run 3, so it
    # keeps a single row; the max contract never created an RMA-A obligation.
    assert [r['contract'] for r in by_rma['RMA-A']] == [OLDEST]
    con.close()


def test_report_exports_versioned_ledger_and_evidence_paths(tmp_path):
    db = _seed(tmp_path)
    process(db, 'return-01', OLDEST)
    out = export(db, tmp_path / 'report.json')
    data = json.loads(out.read_text())
    assert {'return_run', 'refund_ledger', 'inventory_ledger', 'unit_trace',
            'repair_order', 'return_issue', 'repair_station'} <= set(data)
    summary = data['returns_summary'][0]
    assert summary['resellable_units'] == ['U1', 'U2', 'U4']
    refund_evidence = json.loads(summary['refund_ledger'][0]['evidence'])
    assert any(e.startswith('return_case:return-01#rma:') for e in refund_evidence)
    repair = data['repair_order'][0]
    assert json.loads(repair['evidence'])  # every repair points at locatable evidence
    md = out.with_suffix('.md').read_text()
    assert '可再售序列号' in md and 'RMA-A' in md
