import json
from pathlib import Path

import pytest

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
                   'dock':'D1','quantity':4,'parents':[{'id':'P-A','quantity':2,'class':'general'},{'id':'P-B','quantity':2,'class':'general'}]})
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


# ---- 危险类别与隔离舱契约 ----

SEG = BASE / 'segregation'


def run_with_config(tmp_path, config_path, events_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    db = tmp_path / 'case.db'
    freeze_config(db, config_path)
    import_file(db, events_path)
    result = evaluate(db)
    issues = {r['code'] for r in connect(db).execute('SELECT code FROM issue')}
    return db, result, issues


def write_config(tmp_path, mutate):
    cfg = json.loads((SEG / 'config_exclusive.json').read_text())
    mutate(cfg)
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / 'config.json'
    path.write_text(json.dumps(cfg))
    return path


def write_events(tmp_path, mutate):
    events = [json.loads(line) for line in (SEG / 'events.jsonl').read_text().splitlines()]
    mutate(events)
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / 'events.jsonl'
    path.write_text('\n'.join(json.dumps(e) for e in events) + '\n')
    return path


def decision_detail(db, box):
    row = connect(db).execute('SELECT details FROM decision WHERE subject=?', (box,)).fetchone()
    return json.loads(row['details'])


def test_same_counterexample_verdict_depends_on_policy(tmp_path):
    """同一反例（跨一次拆箱、一次并箱）：互斥口径双双阻断，认证隔离舱口径并行放行。"""
    _, exclusive, ex_issues = run_with_config(tmp_path / 'ex', SEG / 'config_exclusive.json', SEG / 'events.jsonl')
    assert exclusive['B-FL'] == 'blocked' and exclusive['B-OX'] == 'blocked'
    assert 'segregation_conflict' in ex_issues
    _, parallel, par_issues = run_with_config(tmp_path / 'par', SEG / 'config_parallel.json', SEG / 'events.jsonl')
    assert parallel['B-FL'] == 'accepted' and parallel['B-OX'] == 'accepted'
    assert 'segregation_conflict' not in par_issues


def test_merge_does_not_drop_source_classes(tmp_path):
    """B-OX 箱单声明 general，但并箱来源是 oxidizer：判定必须基于来源类别。"""
    db, result, _ = run_with_config(tmp_path, SEG / 'config_exclusive.json', SEG / 'events.jsonl')
    assert result['B-OX'] == 'blocked'
    detail = decision_detail(db, 'B-OX')
    assert set(detail['hazard_classes']) == {'general', 'oxidizer'}
    assert any('B-OX-A' in src for src in detail['class_origins']['oxidizer'])
    assert any('B-OX-B' in src for src in detail['class_origins']['oxidizer'])
    conflict = detail['conflicts'][0]
    assert conflict['with'] == 'B-FL'
    assert conflict['clashing_classes'] == [['flammable', 'oxidizer']]
    assert conflict['disposition'] == 'blocked'


def test_frozen_config_version_survives_rerun(tmp_path):
    """配置版本变化不得悄悄改写已完成批次的判定。"""
    db_path = tmp_path / 'old.db'
    freeze_config(db_path, SEG / 'config_exclusive.json')
    import_file(db_path, SEG / 'events.jsonl')
    first = evaluate(db_path)
    assert first['B-FL'] == 'blocked'
    with pytest.raises(ValueError, match='frozen_config_conflict'):
        freeze_config(db_path, SEG / 'config_parallel.json')
    second = evaluate(db_path)
    assert second == first
    detail = decision_detail(db_path, 'B-FL')
    assert detail['config_version'] == '2026.02-seg-ex'
    assert 'segregation_conflict' in detail['issues']
    fresh = tmp_path / 'fresh.db'
    freeze_config(fresh, SEG / 'config_parallel.json')
    import_file(fresh, SEG / 'events.jsonl')
    assert evaluate(fresh)['B-FL'] == 'accepted'


def test_missing_hazard_class_blocks(tmp_path):
    """箱单缺类别且并箱来源无类别声明：不得误报安全。"""
    config = write_config(tmp_path, lambda c: c['boxes'][1].pop('hazard_class'))
    def strip(events):
        for e in events:
            if e['kind'] == 'merge':
                for p in e['parents']:
                    p.pop('class', None)
    events = write_events(tmp_path, strip)
    _, result, issues = run_with_config(tmp_path, config, events)
    assert result['B-OX'] == 'blocked'
    assert {'missing_hazard_class', 'missing_class_origin'} <= issues


def test_unknown_hazard_class_blocks(tmp_path):
    config = write_config(tmp_path, lambda c: c['boxes'][1].update(hazard_class='radioactive'))
    _, result, issues = run_with_config(tmp_path, config, SEG / 'events.jsonl')
    assert result['B-OX'] == 'blocked'
    assert 'unknown_hazard_class' in issues


def test_bay_certification_boundary_moment(tmp_path):
    """隔离舱认证区间含端点：截止时刻恰为重叠窗口结束仍有效，提前一秒即失效。"""
    def at_boundary(cfg):
        cfg['safety_policy'] = 'certified_bays_parallel'
        bay = next(b for b in cfg['segregation_bays'] if b['bay'] == 'BAY-B')
        bay['certified_until'] = '2026-01-01T10:00:00Z'
    config = write_config(tmp_path / 'ok', at_boundary)
    _, result, _ = run_with_config(tmp_path / 'ok', config, SEG / 'events.jsonl')
    assert result['B-OX'] == 'accepted' and result['B-FL'] == 'accepted'
    def before_boundary(cfg):
        cfg['safety_policy'] = 'certified_bays_parallel'
        bay = next(b for b in cfg['segregation_bays'] if b['bay'] == 'BAY-B')
        bay['certified_until'] = '2026-01-01T09:59:59Z'
    config = write_config(tmp_path / 'out', before_boundary)
    _, result, issues = run_with_config(tmp_path / 'out', config, SEG / 'events.jsonl')
    assert result['B-OX'] == 'blocked' and result['B-FL'] == 'blocked'
    assert 'segregation_conflict' in issues


def test_unrelated_dock_is_not_affected(tmp_path):
    """不同月台的同类反例不触发隔离冲突。"""
    config = write_config(tmp_path, lambda c: c['boxes'][1].update(dock='D2'))
    def move(events):
        for e in events:
            if e['subject'] == 'B-OX':
                e['dock'] = 'D2'
    events = write_events(tmp_path, move)
    _, result, issues = run_with_config(tmp_path, config, events)
    assert result == {'B-FL': 'accepted', 'B-OX': 'accepted'}
    assert 'segregation_conflict' not in issues


def relay_split(events, children):
    events.insert(5, {'kind': 'split', 'subject': 'B-FL-1', 'identity': 'D1:113',
                      'time': '2026-01-01T08:22:00Z', 'dock': 'D1', 'quantity': 2,
                      'children': children})
    for e in events:
        if e['kind'] == 'merge':
            e['parents'] = [{'id': 'B-FL-1a', 'quantity': 2, 'bay': 'BAY-A'},
                            {'id': 'B-OX-B', 'quantity': 2, 'class': 'oxidizer', 'bay': 'BAY-B'}]


def test_lineage_relay_inherits_parent_class(tmp_path):
    """拆并链中继：二级子箱沿链继承 flammable，并箱后来源类别仍可追溯。"""
    events = write_events(tmp_path, lambda ev: relay_split(
        ev, [{'id': 'B-FL-1a', 'quantity': 1}, {'id': 'B-FL-1b', 'quantity': 1}]))
    db, result, issues = run_with_config(tmp_path, SEG / 'config_exclusive.json', events)
    assert result['B-OX'] == 'blocked' and result['B-FL'] == 'blocked'
    assert 'segregation_conflict' in issues
    rows = connect(db).execute("SELECT hazard_class, origin FROM class_origin WHERE box='B-FL-1a'").fetchall()
    assert [r['hazard_class'] for r in rows] == ['flammable']
    assert 'split:B-FL-1' in rows[0]['origin']
    detail = decision_detail(db, 'B-OX')
    assert 'flammable' in detail['hazard_classes']
    assert any('B-FL-1a' in src for src in detail['class_origins']['flammable'])
    assert 'unsegregated_merge' not in issues
    assert 'unknown_box' not in issues


def test_broken_relay_chain_cannot_attest_class(tmp_path):
    """中继边数量不守恒 → 谱系断链 → 并箱缺少类别来源，不得误报安全。"""
    events = write_events(tmp_path, lambda ev: relay_split(
        ev, [{'id': 'B-FL-1a', 'quantity': 1}, {'id': 'B-FL-1b', 'quantity': 2}]))
    _, result, issues = run_with_config(tmp_path, SEG / 'config_exclusive.json', events)
    assert result['B-OX'] == 'blocked'
    assert 'missing_class_origin' in issues
    assert 'quantity_not_conserved' in issues


def test_mixed_merge_requires_certified_bays(tmp_path):
    """并箱混合不兼容类别：缺隔离舱声明或舱位未获认证都不得放行。"""
    def no_bay(events):
        for e in events:
            if e['kind'] == 'merge':
                e['parents'] = [{'id': 'B-OX-A', 'quantity': 2, 'class': 'flammable'},
                                {'id': 'B-OX-B', 'quantity': 2, 'class': 'oxidizer'}]
    events = write_events(tmp_path, no_bay)
    _, result, issues = run_with_config(tmp_path, SEG / 'config_exclusive.json', events)
    assert result['B-OX'] == 'blocked'
    assert 'unsegregated_merge' in issues
    def with_bay(events):
        for e in events:
            if e['kind'] == 'merge':
                e['parents'] = [{'id': 'B-OX-A', 'quantity': 2, 'class': 'flammable', 'bay': 'BAY-A'},
                                {'id': 'B-OX-B', 'quantity': 2, 'class': 'oxidizer', 'bay': 'BAY-B'}]
    events = write_events(tmp_path / 'b2', with_bay)
    def shrink(cfg):
        bay = next(b for b in cfg['segregation_bays'] if b['bay'] == 'BAY-B')
        bay['certified_until'] = '2026-01-01T09:00:00Z'
    config = write_config(tmp_path / 'b2', shrink)
    _, result, issues = run_with_config(tmp_path / 'b2', config, events)
    assert result['B-OX'] == 'blocked'
    assert 'bay_not_certified' in issues


def test_report_lists_segregation_conflict_details(tmp_path):
    """报告须指出冲突类别、重叠窗口、隔离舱配置版本与影响到的源箱/子箱。"""
    from dockscan.report import export
    db, _, _ = run_with_config(tmp_path, SEG / 'config_exclusive.json', SEG / 'events.jsonl')
    out = export(db, tmp_path / 'report.json')
    data = json.loads(out.read_text(encoding='utf-8'))
    assert data['config_version'] == '2026.02-seg-ex'
    assert data['safety_policy'] == 'dock_window_exclusive'
    assert any(r['box'] == 'B-OX-A' for r in data['class_origin'])
    md = out.with_suffix('.md').read_text(encoding='utf-8')
    assert '隔离冲突' in md
    assert 'flammable' in md and 'oxidizer' in md
    assert '2026.02-seg-ex' in md
    assert '09:00:00' in md and '10:00:00' in md
    assert 'B-OX-A' in md
