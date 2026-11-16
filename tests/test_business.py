import json
from pathlib import Path

import pytest

from dockscan.ingest import freeze_config, import_file
from dockscan.report import export
from dockscan.review import revoke, sign
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


# ---------- 复核签署链 ----------

def review_config(policy, reviewers=('alice', 'bob', 'carol')):
    cfg = json.loads((BASE / 'config.json').read_text())
    cfg['review'] = {'policy': policy, 'reviewers': list(reviewers)}
    return cfg


def run_review_case(tmp_path, events, policy='single', reviewers=('alice', 'bob', 'carol')):
    tmp_path.mkdir(parents=True, exist_ok=True)
    db = tmp_path / 'case.db'
    cfg_path = tmp_path / 'config.json'
    cfg_path.write_text(json.dumps(review_config(policy, reviewers)))
    freeze_config(db, cfg_path)
    path = tmp_path / 'events.jsonl'
    path.write_text('\n'.join(json.dumps(e) for e in events) + '\n')
    import_file(db, path)
    return evaluate(db), db


def shortage_events():
    events = fixture()
    events[1]['quantity'] = 2  # 卸货 2/4：短少，但称重 20kg 与预期一致，无重量冲突
    return events


def append_events(db, tmp_path, events, name='extra.jsonl'):
    path = tmp_path / name
    path.write_text('\n'.join(json.dumps(e) for e in events) + '\n')
    return import_file(db, path)


def test_single_signoff_releases_pending_shortage(tmp_path):
    result, db = run_review_case(tmp_path, shortage_events(), policy='single')
    assert result['B1'] == 'pending'
    out = sign(db, 'B1', 'alice', '核实为供应商少发，等待补货')
    assert out['status'] == 'released'
    con = connect(db)
    row = con.execute('SELECT status,details,rule_version FROM decision ORDER BY id DESC LIMIT 1').fetchone()
    detail = json.loads(row['details'])
    assert row['status'] == 'released' and detail['rule_status'] == 'pending'
    assert detail['applied_signoffs'] == [out['signoff_id']]
    # 手工签署不得删除差异 issue
    assert 'shortage' in {r['code'] for r in con.execute('SELECT code FROM issue')}
    # 签署绑定当次 run、规则版本、证据摘要与复核原因
    s = con.execute('SELECT * FROM signoff WHERE id=?', (out['signoff_id'],)).fetchone()
    bound = con.execute('SELECT * FROM decision WHERE subject=? AND run_id=?', ('B1', s['run_id'])).fetchone()
    assert bound['status'] == 'pending'  # 签署绑定的是签署当次的待核结论
    assert s['rule_version'] == bound['rule_version'] == row['rule_version']
    assert s['evidence_hash'] == bound['evidence_hash'] == detail['evidence_hash']
    assert s['reason'] == '核实为供应商少发，等待补货'
    assert s['revoked_at'] is None


def test_dual_policy_requires_two_distinct_reviewers(tmp_path):
    result, db = run_review_case(tmp_path, shortage_events(), policy='dual')
    assert result['B1'] == 'pending'
    first = sign(db, 'B1', 'alice', '第一名复核员确认短少属实')
    assert first['status'] == 'pending'  # 双签策略下一人签署后仍须待核
    second = sign(db, 'B1', 'bob', '第二名复核员复核确认')
    assert second['status'] == 'released'
    con = connect(db)
    detail = json.loads(con.execute('SELECT details FROM decision ORDER BY id DESC LIMIT 1').fetchone()['details'])
    assert sorted(detail['applied_signoffs']) == sorted([first['signoff_id'], second['signoff_id']])


def test_same_document_behaves_differently_under_each_policy(tmp_path):
    single_result, single_db = run_review_case(tmp_path / 'single', shortage_events(), policy='single')
    dual_result, dual_db = run_review_case(tmp_path / 'dual', shortage_events(), policy='dual')
    assert single_result['B1'] == dual_result['B1'] == 'pending'
    assert sign(single_db, 'B1', 'alice', '同一复核理由')['status'] == 'released'
    assert sign(dual_db, 'B1', 'alice', '同一复核理由')['status'] == 'pending'


def test_same_reviewer_cannot_sign_twice(tmp_path):
    _, db = run_review_case(tmp_path, shortage_events(), policy='dual')
    sign(db, 'B1', 'alice', '首次签署')
    with pytest.raises(ValueError, match='duplicate_signoff'):
        sign(db, 'B1', 'alice', '同一证据版本重复签署')
    assert evaluate(db)['B1'] == 'pending'  # 同一人不能凑够双签


def test_unauthorized_reviewer_and_empty_reason_rejected(tmp_path):
    _, db = run_review_case(tmp_path, shortage_events())
    with pytest.raises(ValueError, match='unauthorized_reviewer'):
        sign(db, 'B1', 'mallory', '越权签署')
    with pytest.raises(ValueError, match='empty_reason'):
        sign(db, 'B1', 'alice', '  ')


def test_blocked_decision_cannot_be_signed(tmp_path):
    events = shortage_events()
    events[2]['net_kg'] = 30  # 短少之外叠加重量冲突 → 阻断类结论
    result, db = run_review_case(tmp_path, events)
    assert result['B1'] == 'blocked'
    with pytest.raises(ValueError, match='not_pending'):
        sign(db, 'B1', 'alice', '试图放行阻断单据')


def test_signoff_does_not_survive_new_events(tmp_path):
    _, db = run_review_case(tmp_path, shortage_events(), policy='single')
    sign(db, 'B1', 'alice', '放行旧证据版本')
    assert evaluate(db)['B1'] == 'released'  # 无变化的重算不影响有效签署
    append_events(db, tmp_path, [{'kind': 'remark', 'subject': 'B1', 'identity': 'D1:6',
                                  'time': '2026-01-01T08:26:00Z', 'dock': 'D1'}])
    assert evaluate(db)['B1'] == 'pending'  # 签后追加事件 → 旧签署失效，不得沿用旧放行
    con = connect(db)
    s = con.execute('SELECT * FROM signoff').fetchone()
    assert s['revoked_at'] is None  # 旧签署未删除，留作历史记录
    rows = con.execute('SELECT run_id,status FROM decision WHERE subject=? ORDER BY run_id', ('B1',)).fetchall()
    statuses = [r['status'] for r in rows]
    assert 'released' in statuses and statuses[-1] == 'pending'  # 旧 run 结论不可覆盖
    runs = con.execute('SELECT count(*) FROM run').fetchone()[0]
    assert len(rows) == runs


def test_signoff_does_not_survive_identity_conflict(tmp_path):
    _, db = run_review_case(tmp_path, shortage_events(), policy='single')
    sign(db, 'B1', 'alice', '放行')
    assert evaluate(db)['B1'] == 'released'
    conflict = dict(fixture()[0])
    conflict['kind'] = 'changed'  # 同事件身份、不同内容 → 身份冲突
    append_events(db, tmp_path, [conflict])
    assert evaluate(db)['B1'] == 'pending'
    con = connect(db)
    assert con.execute("SELECT count(*) FROM issue WHERE code='identity_conflict'").fetchone()[0] == 1


def test_signoff_does_not_survive_lineage_change(tmp_path):
    _, db = run_review_case(tmp_path, shortage_events(), policy='single')
    sign(db, 'B1', 'alice', '放行')
    assert evaluate(db)['B1'] == 'released'
    append_events(db, tmp_path, [{'kind': 'split', 'subject': 'B1', 'identity': 'D1:6',
                                  'time': '2026-01-01T08:26:00Z', 'dock': 'D1', 'quantity': 2,
                                  'children': [{'id': 'B1a', 'quantity': 1}, {'id': 'B1b', 'quantity': 1}]}])
    assert evaluate(db)['B1'] == 'pending'  # 拆并谱系变化 → 旧签署失效


def test_revoked_signoff_reverts_to_pending_and_allows_resign(tmp_path):
    _, db = run_review_case(tmp_path, shortage_events(), policy='single')
    out = sign(db, 'B1', 'alice', '首次放行')
    assert evaluate(db)['B1'] == 'released'
    revoked = revoke(db, out['signoff_id'], 'bob', '复核理由记录有误，撤销重签')
    assert revoked['status'] == 'pending'
    con = connect(db)
    row = con.execute('SELECT * FROM signoff WHERE id=?', (out['signoff_id'],)).fetchone()
    assert row['revoked_at'] and row['revoked_by'] == 'bob' and row['revoke_reason']
    with pytest.raises(ValueError, match='already_revoked'):
        revoke(db, out['signoff_id'], 'bob', '重复撤销')
    again = sign(db, 'B1', 'alice', '更正后重新签署')  # 撤销后同一人可重签同一证据版本
    assert again['status'] == 'released'


def test_report_exports_verifiable_signoff_chain(tmp_path):
    _, db = run_review_case(tmp_path, shortage_events(), policy='dual')
    first = sign(db, 'B1', 'alice', '第一签')
    revoke(db, first['signoff_id'], 'bob', '撤销第一签')
    second = sign(db, 'B1', 'alice', '撤销后重签')
    third = sign(db, 'B1', 'bob', '第二人签署')
    assert evaluate(db)['B1'] == 'released'
    out = export(db, tmp_path / 'report.json')
    data = json.loads(out.read_text(encoding='utf-8'))
    assert data['review'] == {'policy': 'dual', 'reviewers': ['alice', 'bob', 'carol']}
    states = {s['id']: s['state'] for s in data['signoff']}
    assert states[first['signoff_id']] == 'revoked'
    assert states[second['signoff_id']] == states[third['signoff_id']] == 'active'
    for s in data['signoff']:  # 逐笔可核对：run、规则版本、证据版本、身份、原因齐全
        assert s['run_id'] and s['rule_version'] and s['evidence_hash'] and s['reviewer'] and s['reason']
    assert len(data['decision']) == len(data['run'])  # 逐 run 结论历史完整保留
    assert 'shortage' in {i['code'] for i in data['issue']}  # 放行后差异 issue 仍在
    md = out.with_suffix('.md').read_text(encoding='utf-8')
    assert '签署链' in md and 'alice' in md and 'bob' in md
    assert 'revoked' in md and 'active' in md
    assert '决策历史' in md and '双签' in md


def test_invalid_review_policy_rejected(tmp_path):
    p = tmp_path / 'bad.json'
    p.write_text(json.dumps(review_config('triple')))
    with pytest.raises(ValueError, match='invalid_review_policy'):
        freeze_config(tmp_path / 'bad.db', p)


def test_cli_sign_and_revoke_entrypoints(tmp_path, monkeypatch, capsys):
    from dockscan.__main__ import main
    _, db = run_review_case(tmp_path, shortage_events(), policy='single')
    monkeypatch.setattr('sys.argv', ['dockscan', 'sign', '--db', str(db), '--subject', 'B1',
                                     '--reviewer', 'alice', '--reason', '命令行签署'])
    main()
    assert json.loads(capsys.readouterr().out)['status'] == 'released'
    monkeypatch.setattr('sys.argv', ['dockscan', 'revoke', '--db', str(db), '--signoff', '1',
                                     '--reviewer', 'bob', '--reason', '命令行撤销'])
    main()
    assert json.loads(capsys.readouterr().out)['status'] == 'pending'
