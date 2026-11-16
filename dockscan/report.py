import json
from pathlib import Path
from .store import connect

def export(db_path,out):
    db=connect(db_path)
    data={table:[dict(r) for r in db.execute(f'SELECT * FROM {table} ORDER BY 1')] for table in ('batch','subject','event','issue','decision','evidence','run','lineage')}
    data['source']=[dict(r) for r in db.execute('SELECT id,path,sha256 FROM source ORDER BY id')]
    data['config_hash']=db.execute("SELECT value FROM meta WHERE key='config_hash'").fetchone()['value']
    target=Path(out);target.parent.mkdir(parents=True,exist_ok=True)
    target.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
    events_by_id={e['id']:e for e in data['event']}
    lines=['# 决策报告','']
    for d in data['decision']:
        detail=json.loads(d['details'])
        lines.append(f"- {d['subject']}: {d['status']} — {d['details']}")
        causal=detail.get('causal') or {}
        rendered=[]
        for pair in causal.get('unordered_pairs') or []:
            a,b=pair
            ea,eb=events_by_id.get(a),events_by_id.get(b)
            if ea and eb:
                rendered.append(f"#{a} {ea['identity']}({ea['kind']}@{ea['time']}) ~ #{b} {eb['identity']}({eb['kind']}@{eb['time']})")
        if rendered:
            lines.append('  - 无法证明先后的事件（未用文件顺序或自增ID决胜）: '+'; '.join(rendered))
        for device,missing in (causal.get('sequence_gaps') or {}).items():
            lines.append(f"  - 设备 {device} 序号缺口: {missing}")
        for cycle in causal.get('lineage_cycles') or []:
            lines.append(f"  - 谱系环（拆并链回到祖先）: {'>'.join(cycle)}")
        paths=causal.get('lineage_paths') or []
        if paths:
            lines.append('  - 谱系路径: '+', '.join(paths))
        lines.append(f"  - 结论政策: {detail.get('policy')}")
    md=target.with_suffix('.md')
    md.write_text('\n'.join(lines)+'\n',encoding='utf-8')
    db.close();return target
