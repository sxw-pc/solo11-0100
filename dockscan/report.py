import json
from pathlib import Path
from .store import connect

TABLES=('batch','subject','event','issue','decision','evidence','run','lineage','causal_edge')
# 因果不确定性相关的问题码：报告必须给出造成差异的原始 event id 与谱系路径
CAUSAL_CODES=('order_not_provable','conclusion_diverges','no_legal_order',
              'lineage_cycle','seq_gap','device_order_conflict',
              'order_enumeration_limit','split_exceeds_unloaded')

def export(db_path,out):
    db=connect(db_path)
    data={table:[dict(r) for r in db.execute(f'SELECT * FROM {table} ORDER BY 1')] for table in TABLES}
    data['source']=[dict(r) for r in db.execute('SELECT id,path,sha256 FROM source ORDER BY id')]
    data['config_hash']=db.execute("SELECT value FROM meta WHERE key='config_hash'").fetchone()['value']
    target=Path(out);target.parent.mkdir(parents=True,exist_ok=True)
    target.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
    md=target.with_suffix('.md')
    md.write_text(_render_markdown(data),encoding='utf-8')
    db.close();return target

def _render_markdown(data):
    lines=['# 决策报告','']
    run=data['run'][-1] if data['run'] else None
    if run:
        lines.append(f"运行 #{run['id']}，结论政策：`{run.get('policy')}`，状态：{run['status']}")
        lines.append('')
    lines.append('## 收货结论')
    for d in data['decision']:
        detail=json.loads(d['details'])
        lines.append(f"- {d['subject']}: **{d['status']}** — 问题 {detail.get('issues',[])}；证据事件 {detail.get('evidence',[])}")
    lines.append('')
    lines.append('## 因果审计（同刻事件不使用文件顺序或数据库 id 决胜）')
    edges=data['causal_edge']
    lines.append(f"- 因果边总数：{len(edges)}（依据类型：{sorted({e['reason'] for e in edges})}）")
    for d in data['decision']:
        detail=json.loads(d['details'])
        audit=detail.get('causal_audit',{})
        if not audit: continue
        lines.append(f"### {d['subject']}")
        lines.append(f"- 谱系相关货箱：{audit.get('related_boxes',[])}；合法事件顺序数：{audit.get('legal_order_count')}")
        for pair in audit.get('unresolved_same_time_pairs',[]):
            a,b=pair['a'],pair['b']
            lines.append(f"- 同刻无因果 {pair['time']}：event {a['event_id']}（{a['device']} seq {a['seq']} {a['subject']}/{a['kind']}）"
                         f" 与 event {b['event_id']}（{b['device']} seq {b['seq']} {b['subject']}/{b['kind']}）先后不可证明")
        for gap in audit.get('seq_gaps',[]):
            lines.append(f"- 序号缺口：设备 {gap['device']} 缺 seq {gap['missing']}，相邻证据事件 {gap['evidence']}")
        if 'lineage_cycle' in audit:
            c=audit['lineage_cycle']
            lines.append(f"- 谱系祖先环：{' -> '.join(c['path'])}；涉及事件 {c['event_ids']}")
        if 'no_legal_order' in audit:
            lines.append(f"- 无合法拓扑顺序：因果环事件 {audit['no_legal_order']['cycle_event_ids']}")
        for c in audit.get('device_conflicts',[]):
            lines.append(f"- 设备时间矛盾：{c['device']} event {c['earlier_seq']} 与 {c['later_seq']}")
        shown = audit.get('diverging_outcomes') or audit.get('distinct_outcomes', [])
        for o in shown:
            lines.append(f"- 候选结论：{o['status']} received={o['received']} weight={o['weight']} issues={o['issues']}")
        if audit.get('enumeration_cap'):
            lines.append(f"- 合法顺序超过枚举上限 {audit['enumeration_cap']}，按不可穷尽处理")
    lines.append('')
    lines.append('## 问题清单')
    for i in data['issue']:
        lines.append(f"- {i['code']}: {i['detail']}")
    return '\n'.join(lines)+'\n'
