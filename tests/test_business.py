import json
from pathlib import Path

import pytest

from dockscan.ingest import freeze_config, import_file
from dockscan.rules import evaluate
from dockscan.store import connect
from dockscan.timeline import (build_causal_graph, detect_seq_gaps,
                               find_box_lineage_cycle, find_cycle,
                               linear_extensions, unresolved_same_time_pairs)

BASE = Path(__file__).parent.parent / 'fixtures'
CAUSAL_CONFIG = BASE / 'config_causal.json'
CAUSAL_EVENTS = BASE / 'events_causal.jsonl'


def load_jsonl(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


def run_case(tmp_path, events, policy='consensus', config=CAUSAL_CONFIG):
    db = tmp_path / f'case_{policy}.db'
    freeze_config(db, config)
    path = tmp_path / 'events.jsonl'
    path.write_text('\n'.join(json.dumps(e) for e in events) + '\n')
    import_file(db, path)
    result = evaluate(db, policy=policy)
    con = connect(db)
    issues = {r['code'] for r in con.execute('SELECT code FROM issue')}
    details = {r['subject']: json.loads(r['details'])
               for r in con.execute('SELECT subject,details FROM decision')}
    return result, issues, details, con


# ---------- 1. 确定顺序：因果唯一时两政策结论一致 ----------

def test_unique_causal_order_accepted_under_both_policies(tmp_path):
    events = load_jsonl(CAUSAL_EVENTS)
    # B2 是单设备、时间互不相同的独立收货单：自身事件的因果顺序唯一，
    # 与 B1 同刻的 gate 属于不同证据闭包，不参与 B2 的顺序枚举
    r_strict, i_strict, d_strict, _ = run_case(tmp_path, events, 'strict')
    r_cons, i_cons, d_cons, _ = run_case(tmp_path, events, 'consensus')
    assert r_strict['B2'] == r_cons['B2'] == 'accepted'
    assert d_strict['B2']['causal_audit']['related_boxes'] == ['B2']
    assert d_strict['B2']['causal_audit']['legal_order_count'] == 1
    # 同刻跨设备只出现在 B1 的谱系闭包里，不得污染 B2
    assert not d_strict['B2']['causal_audit'].get('unresolved_same_time_pairs')
    # 暂缓问题只挂在 B1 上
    b1_issues = d_strict['B1']['issues']
    assert 'order_not_provable' in b1_issues
    assert 'order_not_provable' not in d_strict['B2']['issues']


def test_original_fixture_still_deterministic(tmp_path):
    # 旧 fixture（单设备、时刻互异）在两种政策下都只有一条合法顺序
    events = load_jsonl(BASE / 'events.jsonl')
    r_s, _, d_s, _ = run_case(tmp_path, events, 'strict', config=BASE / 'config.json')
    r_c, _, d_c, _ = run_case(tmp_path, events, 'consensus', config=BASE / 'config.json')
    assert r_s['B1'] == r_c['B1'] == 'accepted'
    assert d_s['B1']['causal_audit']['legal_order_count'] == 1


# ---------- 2. 同刻等价顺序：多合法顺序但结论一致 ----------

def test_same_time_equivalent_orders_strict_holds_consensus_releases(tmp_path):
    events = load_jsonl(CAUSAL_EVENTS)
    # 严格模式：B1 含同刻跨设备事件，合法事件顺序不唯一 → 暂缓
    r_strict, issues_strict, detail_strict, con = run_case(tmp_path, events, 'strict')
    assert r_strict['B1'] == 'pending'
    assert 'order_not_provable' in issues_strict
    audit = detail_strict['B1']['causal_audit']
    assert audit['legal_order_count'] > 1
    # 报告必须列出造成差异的原始 event id，而不是猜测的顺序
    pair_ids = {(p['a']['event_id'], p['b']['event_id'])
                for p in audit['unresolved_same_time_pairs']}
    assert pair_ids == {(4, 5), (6, 7)}
    # 谱系路径可审计：拆箱 B1->B1a/B1b，并箱 B1a/B1b->B1
    lineage = {(r['parent_box'], r['child_box'], r['kind'])
               for r in con.execute('SELECT * FROM lineage')}
    assert ('B1', 'B1a', 'split') in lineage
    assert ('B1', 'B1b', 'split') in lineage
    assert ('B1a', 'B1', 'merge') in lineage
    assert ('B1b', 'B1', 'merge') in lineage
    # 同刻事件之间不得存在任何因果边（静默决胜被禁止）
    for r in con.execute('SELECT * FROM causal_edge'):
        assert not ({r['src_event'], r['dst_event']} <= {4, 5}
                    or {r['src_event'], r['dst_event']} <= {6, 7})

    # 共识模式：枚举全部合法顺序，每种顺序都满足阶段与数量守恒 → 放行
    r_cons, issues_cons, detail_cons, _ = run_case(tmp_path, events, 'consensus')
    assert r_cons['B1'] == 'accepted'
    assert 'conclusion_diverges' not in issues_cons
    ca = detail_cons['B1']['causal_audit']
    assert len(ca['distinct_outcomes']) == 1
    assert ca['distinct_outcomes'][0]['status'] == 'accepted'
    assert ca['distinct_outcomes'][0]['received'] == 4

    # 同一组 fixture 证明两种政策产生不同的真实收货决定
    assert r_strict['B1'] != r_cons['B1']


def test_file_order_and_rowid_never_break_ties(tmp_path):
    # 打乱文件行顺序（模拟不同设备日志后导入）：结论必须完全一致
    events = load_jsonl(CAUSAL_EVENTS)
    shuffled = list(reversed(events))
    r1, _, d1, c1 = run_case(tmp_path, events, 'consensus')
    r2, _, d2, c2 = run_case(tmp_path, shuffled, 'consensus')
    assert r1 == r2
    assert d1['B1']['causal_audit']['legal_order_count'] == \
           d2['B1']['causal_audit']['legal_order_count']
    # 因果边的依据分布与时间戳内容不随导入顺序改变
    def edge_shape(con):
        rows = con.execute(
            'SELECT reason, se.time AS st, de.time AS dt FROM causal_edge '
            'JOIN event se ON se.id=src_event JOIN event de ON de.id=dst_event '
            'ORDER BY 1,2,3').fetchall()
        return sorted((r['reason'], r['st'], r['dt']) for r in rows)
    assert edge_shape(c1) == edge_shape(c2)
    # 同刻无因果事件对按（箱号,动作,时刻）比较，同样与行 id 无关
    def pair_shape(detail):
        out = []
        for p in detail['B1']['causal_audit']['unresolved_same_time_pairs']:
            out.append((p['time'],
                        (p['a']['subject'], p['a']['kind']),
                        (p['b']['subject'], p['b']['kind'])))
        return sorted(out)
    assert pair_shape(d1) == pair_shape(d2)


# ---------- 3. 同刻分歧顺序：不同顺序得到不同货箱结论 ----------

def test_diverging_weight_order_pending_in_both_policies(tmp_path):
    events = load_jsonl(CAUSAL_EVENTS)
    # 让 B1 在并箱后的两次“同刻”称重给出冲突净重；同刻跨设备顺序决定
    # 哪个称重最后生效（last-writer），因此不同合法顺序得到不同重量结论
    events += [
        {'kind': 'weight', 'subject': 'B1', 'identity': 'D2:9', 'device': 'D2',
         'seq': 9, 'time': '2026-01-01T08:35:00Z', 'dock': 'D1', 'net_kg': 30},
    ]
    r_strict, issues_strict, d_strict, _ = run_case(tmp_path, events, 'strict')
    r_cons, issues_cons, d_cons, _ = run_case(tmp_path, events, 'consensus')
    assert r_strict['B1'] == 'pending'
    assert 'order_not_provable' in issues_strict
    # 共识模式：结论不唯一 → 仍 pending，并保留每个候选结论
    assert r_cons['B1'] == 'pending'
    assert 'conclusion_diverges' in issues_cons
    outcomes = d_cons['B1']['causal_audit']['diverging_outcomes']
    weights = {o['weight'] for o in outcomes}
    assert weights == {20.0, 30.0}
    statuses = {o['status'] for o in outcomes}
    assert statuses == {'accepted', 'blocked'}
    # 无关收货单不受污染
    assert r_cons['B2'] == 'accepted'


def test_diverging_quantity_on_subboxes_is_reported_with_lineage(tmp_path):
    # 同刻的卸箱与拆箱：若卸箱先于拆箱，拆出量超过该顺序下已卸量 → 阻断；
    # 若拆箱先于卸箱，则守恒合法。顺序对同一子箱产生不同数量结论。
    events = [
        {'kind': 'gate', 'subject': 'B1', 'identity': 'D1:1', 'device': 'D1',
         'seq': 1, 'time': '2026-01-01T08:05:00Z', 'dock': 'D1'},
        {'kind': 'unload', 'subject': 'B1', 'identity': 'D2:1', 'device': 'D2',
         'seq': 1, 'time': '2026-01-01T08:10:00Z', 'dock': 'D1', 'quantity': 4},
        {'kind': 'split', 'subject': 'B1', 'identity': 'D1:2', 'device': 'D1',
         'seq': 2, 'time': '2026-01-01T08:10:00Z', 'dock': 'D1', 'quantity': 4,
         'children': [{'id': 'B1a', 'quantity': 4}]},
        {'kind': 'unload', 'subject': 'B1a', 'identity': 'D3:1', 'device': 'D3',
         'seq': 1, 'time': '2026-01-01T08:20:00Z', 'dock': 'D1', 'quantity': 4},
        {'kind': 'merge', 'subject': 'B1', 'identity': 'D1:3', 'device': 'D1',
         'seq': 3, 'time': '2026-01-01T08:30:00Z', 'dock': 'D1', 'quantity': 4,
         'parents': [{'id': 'B1a', 'quantity': 4}]},
        {'kind': 'weight', 'subject': 'B1', 'identity': 'D1:4', 'device': 'D1',
         'seq': 4, 'time': '2026-01-01T08:35:00Z', 'dock': 'D1', 'net_kg': 20},
        {'kind': 'check', 'subject': 'B1', 'identity': 'D1:5', 'device': 'D1',
         'seq': 5, 'time': '2026-01-01T08:40:00Z', 'dock': 'D1'},
        {'kind': 'exit', 'subject': 'B1', 'identity': 'D1:6', 'device': 'D1',
         'seq': 6, 'time': '2026-01-01T08:45:00Z', 'dock': 'D1'},
    ]
    r_strict, issues_strict, d_strict, _ = run_case(tmp_path, events, 'strict')
    r_cons, issues_cons, d_cons, con = run_case(tmp_path, events, 'consensus')
    assert r_strict['B1'] == 'pending'
    assert 'order_not_provable' in issues_strict
    assert r_cons['B1'] == 'pending'
    assert 'conclusion_diverges' in issues_cons
    candidate_issues = {tuple(o['issues'])
                        for o in d_cons['B1']['causal_audit']['diverging_outcomes']}
    assert any('split_exceeds_unloaded' in iss for iss in candidate_issues)
    # 分歧的原始事件 id 与谱系路径都在审计证据里
    pairs = d_cons['B1']['causal_audit']['unresolved_same_time_pairs']
    assert {(p['a']['event_id'], p['b']['event_id']) for p in pairs} == {(2, 3)}
    lineage_paths = {(r['parent_box'], r['child_box'])
                     for r in con.execute('SELECT parent_box,child_box FROM lineage')}
    assert ('B1', 'B1a') in lineage_paths and ('B1a', 'B1') in lineage_paths
    # 本用例未提供 B2 的扫描：它只缺自己的证据，不继承 B1 的因果问题
    b2_issues = {r['code'] for r in
                 con.execute("SELECT code FROM issue WHERE detail='B2'")}
    assert b2_issues <= {'missing_stage'}


# ---------- 4. 谱系循环：拆并链回到祖先 ----------

def test_lineage_cycle_back_to_ancestor_blocks_with_path(tmp_path):
    events = [
        {'kind': 'gate', 'subject': 'B1', 'identity': 'D1:1', 'device': 'D1',
         'seq': 1, 'time': '2026-01-01T08:05:00Z', 'dock': 'D1'},
        {'kind': 'unload', 'subject': 'B1', 'identity': 'D1:2', 'device': 'D1',
         'seq': 2, 'time': '2026-01-01T08:10:00Z', 'dock': 'D1', 'quantity': 4},
        {'kind': 'split', 'subject': 'B1', 'identity': 'D1:3', 'device': 'D1',
         'seq': 3, 'time': '2026-01-01T08:15:00Z', 'dock': 'D1', 'quantity': 4,
         'children': [{'id': 'B1a', 'quantity': 4}]},
        {'kind': 'unload', 'subject': 'B1a', 'identity': 'D2:1', 'device': 'D2',
         'seq': 1, 'time': '2026-01-01T08:20:00Z', 'dock': 'D1', 'quantity': 4},
        # B1a 又拆回祖先 B1：拆箱血缘图成环
        {'kind': 'split', 'subject': 'B1a', 'identity': 'D2:2', 'device': 'D2',
         'seq': 2, 'time': '2026-01-01T08:25:00Z', 'dock': 'D1', 'quantity': 4,
         'children': [{'id': 'B1', 'quantity': 4}]},
        {'kind': 'merge', 'subject': 'B1', 'identity': 'D1:4', 'device': 'D1',
         'seq': 4, 'time': '2026-01-01T08:30:00Z', 'dock': 'D1', 'quantity': 4,
         'parents': [{'id': 'B1a', 'quantity': 4}]},
        {'kind': 'weight', 'subject': 'B1', 'identity': 'D1:5', 'device': 'D1',
         'seq': 5, 'time': '2026-01-01T08:35:00Z', 'dock': 'D1', 'net_kg': 20},
        {'kind': 'check', 'subject': 'B1', 'identity': 'D1:6', 'device': 'D1',
         'seq': 6, 'time': '2026-01-01T08:40:00Z', 'dock': 'D1'},
        {'kind': 'exit', 'subject': 'B1', 'identity': 'D1:7', 'device': 'D1',
         'seq': 7, 'time': '2026-01-01T08:45:00Z', 'dock': 'D1'},
    ]
    for policy in ('strict', 'consensus'):
        result, issues, details, con = run_case(tmp_path, events, policy)
        assert result['B1'] == 'blocked'
        assert 'lineage_cycle' in issues
        cycle = details['B1']['causal_audit']['lineage_cycle']
        assert set(cycle['path']) >= {'B1', 'B1a'}
        assert cycle['event_ids']  # 保留具体事件证据
        # 无关收货单不继承谱系环：B2 只缺自己的扫描证据
        b2_issues = {r['code'] for r in
                     con.execute("SELECT code FROM issue WHERE detail='B2'")}
        assert b2_issues <= {'missing_stage'}


def test_no_legal_topological_order_from_device_conflict(tmp_path):
    # 同设备 seq 说 D1:2 先于 D1:1，时间戳却说相反：事件级因果图成环
    events = load_jsonl(CAUSAL_EVENTS)
    for e in events:
        if e['identity'] == 'D1:1':
            e['time'] = '2026-01-01T08:12:00Z'  # 晚于 D1:2 的 08:10
    result, issues, details, _ = run_case(tmp_path, events, 'consensus')
    assert result['B1'] == 'blocked'
    assert 'no_legal_order' in issues
    assert 'device_order_conflict' in issues
    assert details['B1']['causal_audit']['no_legal_order']['cycle_event_ids']
    # B2 使用另一台设备，证据闭包不含该环
    assert result['B2'] == 'accepted'


# ---------- 5. 无关货箱隔离 ----------

def test_seq_gap_only_taints_boxes_using_that_device(tmp_path):
    events = load_jsonl(CAUSAL_EVENTS)
    # 在 D1 上制造一个非阶段扫描的缺口：seq=6 称重缺失，seq=7 称重存在。
    # 阶段链保持完整，因此唯一问题是设备证据缺口（pending，而非 blocked）。
    events.append({'kind': 'weight', 'subject': 'B1', 'identity': 'D1:7',
                   'device': 'D1', 'seq': 7, 'time': '2026-01-01T08:50:00Z',
                   'dock': 'D1', 'net_kg': 20})
    result, issues, details, con = run_case(tmp_path, events, 'consensus')
    assert 'seq_gap' in issues
    gap = details['B1']['causal_audit']['seq_gaps'][0]
    assert gap['device'] == 'D1' and gap['missing'] == [6]
    assert gap['evidence']  # 缺口两侧的原始 event id 被保留
    # 缺口是证据缺失：B1 不得 accepted，但也不构成阻断
    assert result['B1'] == 'pending'
    assert details['B1']['issues'] == ['seq_gap']
    # B2 只用 D5，结论与审计均不受影响
    assert result['B2'] == 'accepted'
    assert 'seq_gaps' not in details['B2']['causal_audit']


def test_unknown_box_and_known_boxes_stay_separate(tmp_path):
    events = load_jsonl(CAUSAL_EVENTS)
    events.append({'kind': 'gate', 'subject': 'B-X', 'identity': 'D9:1',
                   'device': 'D9', 'seq': 1,
                   'time': '2026-01-01T09:00:00Z', 'dock': 'D1'})
    result, issues, _, _ = run_case(tmp_path, events, 'consensus')
    assert 'unknown_box' in issues
    assert result['B2'] == 'accepted'


# ---------- 时间线单元性质 ----------

def test_timeline_helpers_keep_tie_evidence(tmp_path):
    events = load_jsonl(CAUSAL_EVENTS)
    db = tmp_path / 'u.db'
    freeze_config(db, CAUSAL_CONFIG)
    p = tmp_path / 'e.jsonl'
    p.write_text('\n'.join(json.dumps(e) for e in events) + '\n')
    import_file(db, p)
    from dockscan.rules import load_events
    raw = load_events(connect(db))
    graph = build_causal_graph(raw)
    # 同刻事件对之间无边
    assert 5 not in graph['edges'][4] and 4 not in graph['edges'][5]
    # 同设备 seq 边存在
    assert 2 in graph['edges'][1]
    # 无环
    assert find_cycle(graph['nodes'], graph['edges']) is None
    assert find_box_lineage_cycle(graph['split_box_edges']) is None
    # 缺口检测
    assert detect_seq_gaps(raw) == []
    # 全部合法顺序数量与限制枚举一致
    orders, capped = linear_extensions(set(graph['nodes']), graph['edges'])
    assert not capped and len(orders) >= 4
    pairs = unresolved_same_time_pairs(raw, graph['edges'])
    all_pairs = {(x['a']['event_id'], x['b']['event_id']) for x in pairs}
    # B1 谱系闭包内的同刻对；9/16 是 B1/B2 各自独立的同刻 gate
    assert {(4, 5), (6, 7), (9, 16)} <= all_pairs
    b1_pairs = unresolved_same_time_pairs(
        raw, graph['edges'],
        relevant={eid for eid, e in graph['nodes'].items()
                  if e['subject'] in {'B1', 'B1a', 'B1b'}})
    assert {(x['a']['event_id'], x['b']['event_id']) for x in b1_pairs} == {(4, 5), (6, 7)}


def test_unknown_policy_rejected(tmp_path):
    events = load_jsonl(CAUSAL_EVENTS)
    db = tmp_path / 'p.db'
    freeze_config(db, CAUSAL_CONFIG)
    p = tmp_path / 'e.jsonl'
    p.write_text('\n'.join(json.dumps(e) for e in events) + '\n')
    import_file(db, p)
    with pytest.raises(ValueError, match='unknown_policy'):
        evaluate(db, policy='guess')
    # 政策不可静默默认：操作方必须显式选择 strict 或 consensus
    with pytest.raises(TypeError):
        evaluate(db)


def test_report_carries_causal_audit(tmp_path):
    from dockscan.report import export
    events = load_jsonl(CAUSAL_EVENTS)
    db = tmp_path / 'r.db'
    freeze_config(db, CAUSAL_CONFIG)
    p = tmp_path / 'e.jsonl'
    p.write_text('\n'.join(json.dumps(e) for e in events) + '\n')
    import_file(db, p)
    evaluate(db, policy='strict')
    out = export(db, tmp_path / 'report.json')
    data = json.loads(out.read_text())
    assert data['causal_edge']
    reasons = {e['reason'] for e in data['causal_edge']}
    assert {'time_order', 'device_seq', 'stage_order',
            'lineage_split', 'lineage_merge'} <= reasons
    md = out.with_suffix('.md').read_text()
    assert '因果审计' in md and 'event 4' in md and 'event 5' in md
