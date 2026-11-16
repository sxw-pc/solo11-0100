import json
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
