import json
from pathlib import Path
from .store import connect

def export(db_path,out):
    db=connect(db_path)
    data={table:[dict(r) for r in db.execute(f'SELECT * FROM {table} ORDER BY 1')] for table in ('batch','subject','event','issue','decision','evidence','run','lineage','class_origin')}
    data['source']=[dict(r) for r in db.execute('SELECT id,path,sha256 FROM source ORDER BY id')]
    data['config_hash']=db.execute("SELECT value FROM meta WHERE key='config_hash'").fetchone()['value']
    config=json.loads(db.execute("SELECT value FROM meta WHERE key='config_json'").fetchone()['value'])
    data['config_version']=config.get('config_version')
    data['safety_policy']=config.get('safety_policy')
    target=Path(out);target.parent.mkdir(parents=True,exist_ok=True)
    target.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
    lines=['# 决策报告','']
    lines+= [f"- {d['subject']}: {d['status']} — {d['details']}" for d in data['decision']]
    segs=[]
    for d in data['decision']:
        for c in json.loads(d['details']).get('conflicts',[]):
            segs.append((d['subject'],c))
    if segs:
        lines+=['','## 隔离冲突','']
        for subj,c in segs:
            lines.append(f"- {subj} × {c['with']} @ {c['dock']}，重叠窗口 {c['overlap_start']}—{c['overlap_end']}，"
                         f"冲突类别 {c['clashing_classes']}，策略 {c['policy']}，隔离舱配置版本 {c['config_version']}，"
                         f"结论 {c['disposition']}，源箱/子箱来源 {c['self_sources']} / {c['peer_sources']}")
    md=target.with_suffix('.md')
    md.write_text('\n'.join(lines)+'\n',encoding='utf-8')
    db.close();return target
