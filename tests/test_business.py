import json
from decimal import Decimal
from pathlib import Path

from dockscan.ingest import freeze_config, import_file
from dockscan.rules import evaluate
from dockscan.store import connect


BASE = Path(__file__).parent.parent / 'fixtures'

def run_case(tmp_path, events):
    db = tmp_path / 'case.db'
    freeze_config(db, BASE / 'config.json')
    path = tmp_path / 'events.jsonl'
    path.write_text('\n'.join(json.dumps(e) for e in events) + '\n')
    import_file(db, path)
    result = evaluate(db)
    issues = {r['code'] for r in connect(db).execute('SELECT code FROM issue')}
    return result, issues


def fixture():
    return [json.loads(line) for line in (BASE / 'events.jsonl').read_text().splitlines()]


def test_out_of_order_file_is_reconstructed_by_time(tmp_path):
    result, issues = run_case(tmp_path, list(reversed(fixture())))
    assert result['B1'] == 'accepted'
    assert not issues


def test_wrong_dock_blocks(tmp_path):
    events = fixture()
    events[1]['dock'] = 'D2'
    result, issues = run_case(tmp_path, events)
    assert result['B1'] == 'blocked'
    assert 'wrong_dock_or_window' in issues


def test_shortage_and_missing_weight_are_distinct(tmp_path):
    events = [e for e in fixture() if e['kind'] != 'weight']
    events[1]['quantity'] = 2
    result, issues = run_case(tmp_path, events)
    assert result['B1'] == 'pending'
    assert {'shortage', 'missing_weight'} <= issues


def test_illegal_stage_cannot_be_accepted(tmp_path):
    events = fixture()
    events[0]['kind'] = 'exit'
    result, issues = run_case(tmp_path, events)
    assert result['B1'] == 'blocked'
    assert 'illegal_transition' in issues


def test_split_must_conserve_quantity(tmp_path):
    events = fixture()
    events.append({'kind': 'split', 'subject': 'B1', 'identity': 'D1:6', 'time': '2026-01-01T08:29:00Z',
                   'dock': 'D1', 'quantity': 4, 'children': [{'id': 'B1a', 'quantity': 3}]})
    result, issues = run_case(tmp_path, events)
    assert result['B1'] == 'blocked'
    assert 'quantity_not_conserved' in issues


def test_valid_merge_records_parent_lineage(tmp_path):
    events = fixture()
    events.append({'kind':'merge','subject':'B1','identity':'D1:6','time':'2026-01-01T08:29:00Z',
                   'dock':'D1','quantity':4,'parents':[{'id':'P-A','quantity':2},{'id':'P-B','quantity':2}]})
    result, issues = run_case(tmp_path, events)
    assert result['B1']=='accepted'
    assert 'quantity_not_conserved' not in issues


def test_weight_mismatch_blocks(tmp_path):
    events=fixture();events[2]['net_kg']=30
    result,issues=run_case(tmp_path,events)
    assert result['B1']=='blocked'
    assert 'weight_mismatch' in issues


def test_overage_separate_from_shortage(tmp_path):
    events=fixture();events[1]['quantity']=5
    result,issues=run_case(tmp_path,events)
    assert result['B1']=='pending'
    assert 'overage' in issues and 'shortage' not in issues


def test_unknown_box_is_reported(tmp_path):
    events=fixture();events.append({'kind':'gate','subject':'B-X','identity':'D1:9',
                                    'time':'2026-01-01T08:05:00Z','dock':'D1'})
    _,issues=run_case(tmp_path,events)
    assert 'unknown_box' in issues


def test_duplicate_appointment_subject_is_rejected(tmp_path):
    from dockscan.ingest import freeze_config
    import pytest
    cfg=json.loads((BASE/'config.json').read_text())
    cfg['boxes'].append(dict(cfg['boxes'][0]))
    p=tmp_path/'bad.json';p.write_text(json.dumps(cfg))
    with pytest.raises(ValueError,match='duplicate_or_missing_subject'):
        freeze_config(tmp_path/'bad.db',p)


def test_window_boundary_is_inclusive(tmp_path):
    events=fixture();events[0]['time']='2026-01-01T08:00:00Z'
    events[-1]['time']='2026-01-01T10:00:00Z'
    result,issues=run_case(tmp_path,events)
    assert result['B1']=='accepted'
    assert 'wrong_dock_or_window' not in issues


def test_late_scan_blocks(tmp_path):
    events=fixture();events[-1]['time']='2026-01-01T10:00:01Z'
    result,issues=run_case(tmp_path,events)
    assert result['B1']=='blocked'
    assert 'wrong_dock_or_window' in issues


def test_audit_entities_and_evidence_chain(tmp_path):
    result,_=run_case(tmp_path,fixture())
    db=connect(tmp_path/'case.db')
    for table in ('batch','subject','source','event','issue','decision','evidence','run'):
        assert db.execute(f'SELECT count(*) FROM {table}').fetchone()[0] >= 0
    assert db.execute('SELECT count(*) FROM evidence').fetchone()[0] == len(fixture())
    assert db.execute("SELECT status FROM run ORDER BY id DESC LIMIT 1").fetchone()[0]=='done'
    assert result['B1']=='accepted'


# ---------------------------------------------------------------------------
# Billing-weight settlement over split/merge lineages (three-layer fixture)
# ---------------------------------------------------------------------------

BILL_BASE = BASE / 'billing_config.json'
BILL_EVENTS = BASE / 'billing_events.jsonl'


def billing_events():
    return [json.loads(line) for line in BILL_EVENTS.read_text().splitlines()]


def run_billing(tmp_path, events=None, config_overrides=None, extra_boxes=None):
    events = billing_events() if events is None else events
    cfg = json.loads(BILL_BASE.read_text())
    if config_overrides:
        cfg['boxes'][0].update(config_overrides)
    if extra_boxes:
        cfg['boxes'].extend(extra_boxes)
    cfg_path = tmp_path / 'config.json'
    tmp_path.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False))
    db = tmp_path / 'billing.db'
    freeze_config(db, cfg_path)
    event_path = tmp_path / 'events.jsonl'
    event_path.write_text('\n'.join(json.dumps(e) for e in events) + '\n')
    import_file(db, event_path)
    result = evaluate(db)
    con = connect(db)
    issues = {r['code'] for r in con.execute('SELECT code FROM issue')}
    settlement = con.execute('SELECT * FROM settlement').fetchall()
    lines = con.execute('SELECT * FROM settlement_line ORDER BY id').fetchall()
    return result, issues, [dict(r) for r in settlement], [dict(r) for r in lines], con


def test_per_leaf_rounding_bills_each_leaf_independently(tmp_path):
    result, issues, headers, lines, _ = run_billing(tmp_path, config_overrides={'rounding_mode': 'per_leaf'})
    assert result['B1'] == 'accepted'
    assert not issues
    hdr = headers[0]
    assert hdr['status'] == 'settled' and hdr['rounding_mode'] == 'per_leaf'
    # raw settleable weight is 59.000 kg; three ceiling rounds add 1.1 kg
    assert hdr['raw_settleable_kg'] == '59.000'
    assert hdr['billable_kg'] == '60.0'
    assert hdr['amount'] == '600.00'  # 60 kg * rate 10/kg
    assert len(lines) == 3
    assert [ln['leaf_box'] for ln in lines] == ['B1-M', 'L1', 'L3']
    # the two boundary leaves: 14.45 rounds up to 15, 14.55 rounds up to 15
    by_leaf = {ln['leaf_box']: ln for ln in lines}
    assert by_leaf['L1']['settleable_kg'] == '14.450' and by_leaf['L1']['billable_kg'] == '15.0'
    assert by_leaf['L3']['settleable_kg'] == '14.550' and by_leaf['L3']['billable_kg'] == '15.0'
    assert by_leaf['B1-M']['settleable_kg'] == '30.000' and by_leaf['B1-M']['billable_kg'] == '30.0'


def test_shipment_rounding_rounds_the_consolidated_total_once(tmp_path):
    result, issues, headers, lines, _ = run_billing(tmp_path, config_overrides={'rounding_mode': 'shipment'})
    assert result['B1'] == 'accepted'
    assert not issues
    hdr = headers[0]
    assert hdr['status'] == 'settled' and hdr['rounding_mode'] == 'shipment'
    assert hdr['raw_settleable_kg'] == '59.000'
    assert hdr['billable_kg'] == '59.0'
    assert hdr['amount'] == '590.00'  # one round only -> a different carrier bill
    by_leaf = {ln['leaf_box']: ln for ln in lines}
    assert by_leaf['L1']['billable_kg'] == '14.450'
    assert by_leaf['L3']['billable_kg'] == '14.550'


def test_two_contract_modes_produce_different_carrier_bills(tmp_path):
    _, _, per_leaf, _, _ = run_billing(tmp_path / 'a', config_overrides={'rounding_mode': 'per_leaf'})
    _, _, shipment, _, _ = run_billing(tmp_path / 'b', config_overrides={'rounding_mode': 'shipment'})
    assert per_leaf[0]['billable_kg'] != shipment[0]['billable_kg']
    assert (per_leaf[0]['billable_kg'], shipment[0]['billable_kg']) == ('60.0', '59.0')
    assert per_leaf[0]['amount'] == '600.00' and shipment[0]['amount'] == '590.00'


def test_leaf_settleable_weight_conserves_against_parent_chain(tmp_path):
    _, issues, headers, lines, _ = run_billing(tmp_path, config_overrides={'rounding_mode': 'shipment'})
    assert 'weight_not_conserved' not in issues
    # leaf settleable weights sum to root gross minus every tare on the chain
    assert sum((Decimal(ln['settleable_kg']) for ln in lines), Decimal('0')) == Decimal(headers[0]['raw_settleable_kg'])
    # root gross 61.0; chain tares B1 1.2 + B1-A 0.3 + B1-B 0.5 + B1-M 0.0 = 2.0
    assert float(headers[0]['raw_settleable_kg']) == 61.0 - 2.0
    # every leaf line carries its measured gross and its apportioned tare
    assert all(Decimal(ln['gross_kg']) - Decimal(ln['tare_share_kg']) == Decimal(ln['settleable_kg'])
               for ln in lines)


def test_bill_lines_trace_back_to_leaf_parent_tare_and_source_line(tmp_path):
    _, _, headers, lines, con = run_billing(tmp_path, config_overrides={'rounding_mode': 'per_leaf'})
    by_leaf = {ln['leaf_box']: ln for ln in lines}
    l1 = by_leaf['L1']
    # source line of the original weighing in the imported JSONL
    event = con.execute('SELECT line_no, source_id FROM event WHERE id=?', (l1['weight_event_id'],)).fetchone()
    assert event['line_no'] == l1['source_line_no'] == 12
    assert l1['calibration'] == 'SC-1-v2'
    allocation = json.loads(l1['allocation'])
    # the parent tares that produced the 0.450 kg share are retained on the line
    assert allocation['parent_tare_kg'] == {'B1': '1.2', 'B1-A': '0.3', 'B1-B': '0.5', 'B1-M': '0.0'}
    assert l1['tare_share_kg'] == '0.450'  # (1.2 + 0.3) split equally across the A branch
    # reverse lookup: bill line -> weighing event -> immutable source row
    source = con.execute('SELECT path, sha256 FROM source WHERE id=?', (event['source_id'],)).fetchone()
    assert source['path'].endswith('events.jsonl') and len(source['sha256']) == 64


def test_missing_rounding_choice_blocks_the_bill(tmp_path):
    cfg = json.loads(BILL_BASE.read_text())
    del cfg['boxes'][0]['rounding_mode']
    cfg_path = tmp_path / 'config.json'; cfg_path.write_text(json.dumps(cfg))
    db = tmp_path / 'x.db'; freeze_config(db, cfg_path)
    p = tmp_path / 'events.jsonl'; p.write_text(BILL_EVENTS.read_text())
    import_file(db, p)
    result = evaluate(db)
    con = connect(db)
    assert result['B1'] == 'blocked'
    hdr = dict(con.execute('SELECT * FROM settlement').fetchone())
    assert hdr['status'] == 'blocked' and hdr['billable_kg'] is None
    assert 'missing_rounding_config' in {r['code'] for r in con.execute('SELECT code FROM issue')}


def test_unknown_rounding_mode_is_rejected_not_silently_defaulted(tmp_path):
    _, issues, headers, _, _ = run_billing(tmp_path, config_overrides={'rounding_mode': 'round_hopefully'})
    assert 'missing_rounding_config' in issues
    assert headers[0]['status'] == 'blocked' and headers[0]['billable_kg'] is None


def test_shortage_in_split_conservation_blocks_billing(tmp_path):
    events = billing_events()
    # B1-A's measured children gross no longer equals its own gross (1.0 kg lost)
    for e in events:
        if e.get('identity') == 'D1:12':
            e['gross_kg'] = 13.9
    _, issues, headers, _, _ = run_billing(tmp_path, events=events)
    assert 'weight_not_conserved' in issues
    assert headers[0]['status'] == 'blocked' and headers[0]['billable_kg'] is None


def test_negative_net_weight_is_blocked_not_apportioned(tmp_path):
    events = billing_events()
    for e in events:
        if e.get('identity') == 'D1:12':
            e['gross_kg'] = 0.2  # tare share 0.45 -> negative settleable net
    _, issues, headers, _, _ = run_billing(tmp_path, events=events,
                                           config_overrides={'tolerance_kg': 100})
    assert 'negative_net_weight' in issues
    assert headers[0]['status'] == 'blocked'


def test_cyclic_lineage_blocks_the_order(tmp_path):
    events = billing_events()
    # B1-A is re-merged out of its own descendant L1 -> cycle L1 -> B1-A
    events.append({'kind': 'merge', 'subject': 'B1-A', 'identity': 'D1:20',
                   'time': '2026-01-01T09:05:00Z', 'dock': 'D1', 'quantity': 1,
                   'parents': [{'id': 'L1', 'quantity': 1}]})
    _, issues, headers, _, _ = run_billing(tmp_path, events=events)
    assert 'lineage_cycle' in issues
    assert headers[0]['status'] == 'blocked' and headers[0]['billable_kg'] is None


def test_box_entering_two_merge_branches_blocks_only_that_bill(tmp_path):
    events = billing_events()
    # L2 was already consumed by merge D1:10 (into B1-M); a second merge reuses it
    events.append({'kind': 'merge', 'subject': 'B1-M2', 'identity': 'D1:21',
                   'time': '2026-01-01T09:06:00Z', 'dock': 'D1', 'quantity': 1,
                   'parents': [{'id': 'L2', 'quantity': 1}]})
    _, issues, headers, _, _ = run_billing(tmp_path, events=events)
    assert 'duplicate_lineage_reference' in issues
    assert headers[0]['status'] == 'blocked' and headers[0]['billable_kg'] is None


def test_leaf_referenced_twice_in_one_event_is_rejected(tmp_path):
    events = billing_events()
    for e in events:
        if e.get('identity') == 'D1:7':
            e['children'] = [{'id': 'L1', 'quantity': 1}, {'id': 'L1', 'quantity': 1}]
    _, issues, headers, _, _ = run_billing(tmp_path, events=events)
    assert 'duplicate_lineage_reference' in issues
    assert headers[0]['status'] == 'blocked'


def test_backfilled_gross_on_expired_calibration_is_rejected(tmp_path):
    events = billing_events()
    for e in events:
        if e.get('identity') == 'D1:12':  # L1 leaf gross, back-loaded weighing
            e['calibration_version'] = 'SC-1-v1'  # validity ended 2025-12-31
    _, issues, headers, _, _ = run_billing(tmp_path, events=events)
    assert 'calibration_expired' in issues
    assert headers[0]['status'] == 'blocked' and headers[0]['billable_kg'] is None


def test_unknown_calibration_version_is_treated_as_invalid(tmp_path):
    events = billing_events()
    for e in events:
        if e.get('identity') == 'D1:13':
            e['calibration_version'] = 'SC-GHOST'
    _, issues, headers, _, _ = run_billing(tmp_path, events=events)
    assert 'calibration_expired' in issues
    assert headers[0]['status'] == 'blocked'


def test_duplicate_weighing_of_one_box_blocks_the_bill(tmp_path):
    events = billing_events()
    events.append({'kind': 'weight', 'subject': 'L1', 'identity': 'D1:22',
                   'time': '2026-01-01T09:07:00Z', 'dock': 'D1',
                   'gross_kg': 14.9, 'calibration_version': 'SC-1-v2'})
    _, issues, headers, _, _ = run_billing(tmp_path, events=events)
    assert 'duplicate_weighing' in issues
    assert headers[0]['status'] == 'blocked'


def test_tainted_order_does_not_block_an_unrelated_order(tmp_path):
    events = billing_events()
    # taint B1: re-merge L2 into a second branch
    events.append({'kind': 'merge', 'subject': 'B1-M2', 'identity': 'D1:23',
                   'time': '2026-01-01T09:08:00Z', 'dock': 'D1', 'quantity': 1,
                   'parents': [{'id': 'L2', 'quantity': 1}]})
    # independent order B2: one leaf, its own calibration-valid weighing
    events += [
        {'kind': 'gate', 'subject': 'B2', 'identity': 'D2:1', 'time': '2026-01-01T08:05:00Z', 'dock': 'D2'},
        {'kind': 'unload', 'subject': 'B2', 'identity': 'D2:2', 'time': '2026-01-01T08:10:00Z', 'dock': 'D2', 'quantity': 1},
        {'kind': 'weight', 'subject': 'B2', 'identity': 'D2:3', 'time': '2026-01-01T08:15:00Z', 'dock': 'D2',
         'gross_kg': 10.0, 'tare_kg': 1.0, 'net_kg': 9.0, 'calibration_version': 'SC-1-v2'},
        {'kind': 'check', 'subject': 'B2', 'identity': 'D2:4', 'time': '2026-01-01T08:20:00Z', 'dock': 'D2'},
        {'kind': 'exit', 'subject': 'B2', 'identity': 'D2:5', 'time': '2026-01-01T08:25:00Z', 'dock': 'D2'},
    ]
    b2 = {'box': 'B2', 'order': 'O2', 'pallet': 'P2', 'quantity': 1, 'dock': 'D2',
          'window_start': '2026-01-01T08:00:00Z', 'window_end': '2026-01-01T10:00:00Z',
          'expected_kg': 9.0, 'tolerance_kg': 1, 'billing_unit_kg': 1.0,
          'rate_per_kg': 10.0, 'rounding_mode': 'shipment'}
    result, issues, headers, lines, con = run_billing(tmp_path, events=events, extra_boxes=[b2])
    assert result['B1'] == 'blocked'
    assert result['B2'] == 'accepted'
    by_subject = {h['subject']: h for h in headers}
    assert by_subject['B1']['status'] == 'blocked' and by_subject['B1']['billable_kg'] is None
    assert by_subject['B2']['status'] == 'settled'
    assert by_subject['B2']['billable_kg'] == '9.0' and by_subject['B2']['amount'] == '90.00'
    b2_lines = con.execute('SELECT * FROM settlement_line WHERE subject=?', ('B2',)).fetchall()
    assert len(b2_lines) == 1 and dict(b2_lines[0])['leaf_box'] == 'B2'


def test_multi_layer_lineage_persists_every_edge_and_leaf(tmp_path):
    _, _, headers, lines, con = run_billing(tmp_path, config_overrides={'rounding_mode': 'shipment'})
    edges = con.execute('SELECT parent_box, child_box, kind FROM lineage ORDER BY parent_box, child_box').fetchall()
    pairs = {(r['parent_box'], r['child_box']) for r in edges}
    # three layers: B1 -> A/B -> L1,L2,L3,X -> M
    assert ('B1', 'B1-A') in pairs and ('B1', 'B1-B') in pairs
    assert ('B1-A', 'L1') in pairs and ('B1-A', 'L2') in pairs
    assert ('B1-B', 'L3') in pairs and ('B1-B', 'B1-X') in pairs
    assert ('L2', 'B1-M') in pairs and ('B1-X', 'B1-M') in pairs
    assert len(lines) == 3  # L1, L3 and the merged leaf B1-M; L2/X are not leaves


def test_re_evaluation_is_idempotent_and_rebuilds_bill_lines(tmp_path):
    events = billing_events()
    cfg = json.loads(BILL_BASE.read_text())
    cfg_path = tmp_path / 'config.json'; cfg_path.write_text(json.dumps(cfg))
    db = tmp_path / 'x.db'; freeze_config(db, cfg_path)
    event_path = tmp_path / 'events.jsonl'
    event_path.write_text('\n'.join(json.dumps(e) for e in events))
    import_file(db, event_path)
    first = evaluate(db)
    con = connect(db)
    lines_after_first = con.execute('SELECT count(*) FROM settlement_line').fetchone()[0]
    issues_after_first = con.execute('SELECT count(*) FROM issue').fetchone()[0]
    second = evaluate(db)  # must clear and rebuild, not violate child->header FK
    assert first == second == {'B1': 'accepted'}
    con = connect(db)
    assert con.execute('SELECT count(*) FROM settlement_line').fetchone()[0] == lines_after_first
    assert con.execute('SELECT count(*) FROM issue').fetchone()[0] == issues_after_first
    assert con.execute('SELECT count(*) FROM settlement').fetchone()[0] == 1
    assert con.execute('SELECT count(*) FROM run').fetchone()[0] == 2
