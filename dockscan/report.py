import json
from pathlib import Path
from .store import connect

def _meta(db,key,default=None):
    row=db.execute('SELECT value FROM meta WHERE key=?',(key,)).fetchone()
    return row['value'] if row else default

def export(db_path,out):
    db=connect(db_path)
    data={table:[dict(r) for r in db.execute(f'SELECT * FROM {table} ORDER BY 1')] for table in ('batch','subject','event','issue','decision','evidence','run','lineage','conflict')}
    data['source']=[dict(r) for r in db.execute('SELECT id,path,sha256 FROM source ORDER BY id')]
    data['config_hash']=_meta(db,'config_hash')
    data['config_version']=_meta(db,'config_version','1')
    data['policy']=_meta(db,'policy','mutual_exclusion')
    target=Path(out);target.parent.mkdir(parents=True,exist_ok=True)
    target.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
    lines=['# 决策报告','',f"策略: {data['policy']} — 隔离舱配置版本: {data['config_version']}",'']
    lines+=['## 箱单判定']+[f"- {d['subject']}: {d['status']} — {d['details']}" for d in data['decision']]
    lines+=['','## 类别冲突与隔离舱证据']
    if not data['conflict']:
        lines.append('- 无')
    for c in data['conflict']:
        if c['kind']=='pair':
            lines.append(f"- [{c['outcome']}] {c['dock']}: {c['box_a']} {c['categories_a']} × {c['box_b']} {c['categories_b']}"
                         f" 重叠窗口 {c['overlap_start']}—{c['overlap_end']} 隔离舱 {c['bay_a'] or '-'}/{c['bay_b'] or '-'}"
                         f" 源箱 {c['origins_a']}/{c['origins_b']} 配置版本 {c['config_version']}")
        else:
            lines.append(f"- [{c['outcome']}] {c['dock']}: 并箱 {c['box_a']} 混合 {c['categories_a']}"
                         f" 来源类别 {c['categories_b']} 隔离舱映射 {json.loads(c['detail']).get('parent_bays',{})}"
                         f" 源箱 {c['origins_a']} 配置版本 {c['config_version']}")
    md=target.with_suffix('.md')
    md.write_text('\n'.join(lines)+'\n',encoding='utf-8')
    db.close();return target
