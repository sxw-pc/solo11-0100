import json
from datetime import datetime,timezone
from collections import defaultdict
from .store import connect
from .ingest import timestamp
from .identity import valid_lineage,lineage_participants
from .timeline import (STAGES,advance,build_causal_graph,
                       detect_seq_gaps,find_cycle,find_box_lineage_cycle,
                       closure_for,linear_extensions,unresolved_same_time_pairs,
                       EXTENSION_CAP)

VERSION='2'
POLICIES=('strict','consensus')
# 阻断性问题：存在即不得放行，且不受顺序不确定性影响
HARD_BLOCKERS=frozenset(('illegal_transition','wrong_dock_or_window',
    'quantity_not_conserved','invalid_lineage','lineage_cycle',
    'no_legal_order','device_order_conflict','split_exceeds_unloaded',
    'weight_mismatch'))

def load_events(db):
    events=[]
    for row in db.execute('SELECT * FROM event ORDER BY id'):
        e=dict(row)
        e['v']=json.loads(e['payload'])
        events.append(e)
    return events

def invariant_checks(box,order,box_events):
    """与事件顺序无关的硬核对：月台/窗口、拆并箱守恒与谱系合法性。

    这些属性在任何合法顺序下都不变，因此可直接判定，不依赖排序假设。
    """
    problems=set()
    for e in box_events:
        if e['subject']!=box: continue
        t=timestamp(e['time'])
        if e['v'].get('dock')!=order['dock'] or not timestamp(order['window_start'])<=t<=timestamp(order['window_end']):
            problems.add('wrong_dock_or_window')
        if e['kind'] in ('split','merge'):
            parts=e['v'].get('children',[]) if e['kind']=='split' else e['v'].get('parents',[])
            source=int(e['v'].get('quantity',0))
            if sum(int(c['quantity']) for c in parts)!=source:
                problems.add('quantity_not_conserved')
            elif not valid_lineage(box,parts,source):
                problems.add('invalid_lineage')
    return problems

def simulate_order(order_ids,graph,orders):
    """按给定事件顺序模拟每个相关货箱的状态机与累计量。

    返回 (stage,received,weight,issues)——结论完全由事件顺序决定，
    不读取文件顺序或数据库 id。
    """
    stage={};received=defaultdict(int);weight={};issues=defaultdict(list)
    unloaded_so_far=defaultdict(int)
    for eid in order_ids:
        e=graph['nodes'][eid]; v=e['v']; box=e['subject']; kind=e['kind']
        if kind in STAGES:
            cur=stage.get(box,0)
            new,error=advance(cur,kind)
            if error: issues[box].append('illegal_transition')
            else: stage[box]=new
        if kind=='unload':
            q=int(v.get('quantity',0))
            received[box]+=q; unloaded_so_far[box]+=q
        if kind=='weight':
            weight[box]=float(v['net_kg'])
        if kind in ('split','merge'):
            parts=v.get('children',[]) if kind=='split' else v.get('parents',[])
            source=int(v.get('quantity',0))
            if sum(int(c['quantity']) for c in parts)!=source:
                issues[box].append('quantity_not_conserved')
            elif not valid_lineage(box,parts,source):
                issues[box].append('invalid_lineage')
            if kind=='split' and not any(x in ('quantity_not_conserved','invalid_lineage') for x in issues[box]):
                # 拆出的量不得超过该顺序下此前已卸量：顺序相关的数量约束
                if source>unloaded_so_far[box]:
                    issues[box].append('split_exceeds_unloaded')
    return stage,dict(received),weight,{k:sorted(set(v)) for k,v in issues.items()}

def finalize_outcome(box,order,stage,received,weight,sim_issues):
    """在某一确定顺序的模拟结果上补齐结尾核对，产出完整结论。"""
    problems=list(sim_issues.get(box,[]))
    if stage.get(box,0)<4: problems.append('missing_stage')
    got=received.get(box,0)
    if got<order['quantity']: problems.append('shortage')
    if got>order['quantity']: problems.append('overage')
    w=weight.get(box)
    if w is None: problems.append('missing_weight')
    elif abs(w-order['expected_kg'])>order['tolerance_kg']: problems.append('weight_mismatch')
    status='blocked' if any(p in HARD_BLOCKERS for p in problems) else ('pending' if problems else 'accepted')
    return {'status':status,'received':got,
            'weight':w,'stage':stage.get(box,0),
            'issues':sorted(set(problems))}

def evaluate(db_path,policy):
    """政策必须由操作方显式选择：strict 或 consensus，不存在静默默认。"""
    if policy not in POLICIES: raise ValueError(f'unknown_policy:{policy}')
    db=connect(db_path);config=json.loads(db.execute("SELECT value FROM meta WHERE key='config_json'").fetchone()['value'])
    orders={x['box']:x for x in config['boxes']}
    events=load_events(db)
    graph=build_causal_graph(events)
    # 序号缺口（设备级证据）
    seq_gaps=detect_seq_gaps(events)
    gap_devices={g['device'] for g in seq_gaps}
    # 货箱级谱系祖先环（仅拆箱血缘边；拆后并回同箱不算环）
    box_cycle=find_box_lineage_cycle(graph['split_box_edges'])
    # 事件级因果环 => 无合法拓扑顺序
    event_cycle=find_cycle(graph['nodes'],graph['edges'])
    results={}
    with db:
        run_id=db.execute('INSERT INTO run(started_at,status,policy) VALUES (?,?,?)',
                          (datetime.now(timezone.utc).isoformat(),'running',policy)).lastrowid
        db.execute('DELETE FROM evidence')
        db.execute('DELETE FROM decision');db.execute('DELETE FROM lineage')
        db.execute('DELETE FROM causal_edge')
        db.execute("DELETE FROM issue WHERE code NOT IN ('identity_conflict','parse_error')")
        for rec in graph['edge_records']:
            db.execute('INSERT INTO causal_edge VALUES (?,?,?,?,?)',
                       (rec['src'],rec['dst'],rec['reason'],rec['detail'],run_id))
        lineage_boxes=lineage_participants(events)
        for box in sorted(orders):
            order=orders[box]
            relevant,related_boxes=closure_for(box,graph)
            box_events=[e for e in events if e['id'] in relevant]
            problems=[]; audit={'related_boxes':sorted(related_boxes),
                                'evidence_event_ids':sorted(relevant)}
            # --- 与顺序无关的硬证据核对 ---
            hard_problems=invariant_checks(box,order,box_events)
            # 守恒合法的拆并关系写入 lineage 表
            for e in box_events:
                if e['subject']!=box or e['kind'] not in ('split','merge'): continue
                parts=e['v'].get('children',[]) if e['kind']=='split' else e['v'].get('parents',[])
                source=int(e['v'].get('quantity',0))
                if sum(int(c['quantity']) for c in parts)==source and valid_lineage(box,parts,source):
                    for part in parts:
                        parent,child=(box,str(part['id'])) if e['kind']=='split' else (str(part['id']),box)
                        db.execute('INSERT OR IGNORE INTO lineage VALUES (?,?,?,?,?)',
                                   (parent,child,int(part['quantity']),e['kind'],e['id']))
            # --- 设备序号缺口只落在使用该设备的货箱上，不污染无关收货单 ---
            box_gap_devices=sorted({e['device'] for e in box_events
                                    if e.get('device') in gap_devices})
            if box_gap_devices:
                problems.append('seq_gap')
                audit['seq_gaps']=[g for g in seq_gaps if g['device'] in box_gap_devices]
            # --- 谱系祖先环 ---
            lineage_cyclic = bool(box_cycle and (set(box_cycle) & related_boxes))
            if lineage_cyclic:
                problems.append('lineage_cycle')
                audit['lineage_cycle']={'path':box_cycle,
                    'event_ids':sorted(e['id'] for e in events
                                       if e['subject'] in set(box_cycle))}
            # --- 无合法拓扑顺序（事件级因果环，含设备时间矛盾）---
            cyclic = bool(event_cycle and (set(event_cycle) & relevant))
            if cyclic:
                problems.append('no_legal_order')
                audit['no_legal_order']={'cycle_event_ids':event_cycle}
                conflicts=[c for c in graph['device_conflicts']
                           if c['earlier_seq'] in relevant]
                if conflicts:
                    problems.append('device_order_conflict')
                    audit['device_conflicts']=conflicts
                # 环上仍按数据库呈现顺序模拟一次，仅用于描述被违反的阶段约束
                st,rc,wt,si=simulate_order(tuple(sorted(relevant)),graph,orders)
                hard_problems.update(si.get(box,()))
            # --- 同刻无因果事件对（顺序不可证明的原始证据）---
            unresolved=unresolved_same_time_pairs(events,graph['edges'],relevant)
            if unresolved: audit['unresolved_same_time_pairs']=unresolved
            # --- 枚举合法拓扑序 ---
            if cyclic or lineage_cyclic:
                legal=[]; capped=False
            elif not relevant:
                # 无任何证据：走缺证据结论，不声称无合法顺序
                legal=[]; capped=False
            else:
                legal,capped=linear_extensions(relevant,graph['edges'])
            audit['legal_order_count']=len(legal)
            outcomes=[]
            for order_ids in legal:
                st,rc,wt,si=simulate_order(order_ids,graph,orders)
                outcomes.append(finalize_outcome(box,order,st,rc,wt,si))
            distinct=[];seen_sig=set()
            for o in outcomes:
                signature=(o['status'],o['received'],o['weight'],tuple(o['issues']))
                if signature not in seen_sig:
                    seen_sig.add(signature);distinct.append(o)
            audit['distinct_outcomes']=distinct
            all_problem_set=set(problems)|hard_problems
            if any(p in HARD_BLOCKERS for p in all_problem_set):
                status='blocked'; problems=sorted(all_problem_set)
            elif capped:
                status='pending'; problems=sorted(all_problem_set|{'order_enumeration_limit'})
                audit['enumeration_cap']=EXTENSION_CAP
            elif not relevant:
                # 证据缺失：缺全部阶段，按 pending/missing_stage 处理
                status='pending'
                problems=sorted(all_problem_set|{'missing_stage'})
            elif cyclic or lineage_cyclic:
                status='blocked'
                problems=sorted(all_problem_set|{'no_legal_order'})
            elif len(legal)==1:
                status=outcomes[0]['status']
                if status!='accepted':
                    problems=sorted(set(problems)|set(outcomes[0]['issues']))
            else:
                # 存在多个与全部证据相容的合法顺序：政策分歧点
                if policy=='strict':
                    # 严格模式：合法事件顺序不唯一即暂缓，不猜测同刻先后
                    status='pending'
                    problems=sorted(set(problems)|{'order_not_provable'})
                    audit['policy']='strict'
                elif len(distinct)==1:
                    # 共识模式：每种顺序结论相同才允许该结论落地
                    status=distinct[0]['status']
                    if status!='accepted':
                        problems=sorted(set(problems)|set(distinct[0]['issues']))
                    audit['policy']='consensus'
                else:
                    status='pending'
                    problems=sorted(set(problems)|{'conclusion_diverges'})
                    audit['policy']='consensus'
                    audit['diverging_outcomes']=distinct
            # 序号缺口是证据缺失：绝不允许 accepted
            if status=='accepted' and 'seq_gap' in problems:
                status='pending'
            detail={'policy':policy,'expected':order['quantity'],
                    'received':outcomes[0]['received'] if outcomes else 0,
                    'weight':outcomes[0]['weight'] if outcomes else None,
                    'issues':sorted(set(problems)),
                    'evidence':sorted(e['id'] for e in box_events),
                    'causal_audit':audit}
            db.execute('INSERT INTO decision VALUES (?,?,?,?,?)',(box,status,json.dumps(detail,ensure_ascii=False),VERSION,run_id))
            for e in box_events:
                db.execute('INSERT OR IGNORE INTO evidence VALUES (?,?,?,?)',(box,e['id'],VERSION,run_id))
            for code in sorted(set(problems)):
                db.execute('INSERT INTO issue(code,detail,event_id) VALUES (?,?,?)',
                           (code,box,None))
            results[box]=status
        known=set(orders)|lineage_boxes
        for box in sorted({e['subject'] for e in events}-known):
            db.execute('INSERT INTO issue(code,detail) VALUES (?,?)',('unknown_box',box))
        db.execute('UPDATE run SET completed_at=?,status=? WHERE id=?',
                   (datetime.now(timezone.utc).isoformat(),'done',run_id))
    db.close();return results
