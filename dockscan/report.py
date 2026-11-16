import json
from pathlib import Path
from collections import defaultdict
from .store import connect

POLICY_LABEL={'single':'单签：一名授权复核员即可放行普通差异','dual':'双签：高风险差异需两名不同复核员分别签署'}
def _latest_decisions(decisions):
    latest={}
    for d in decisions: latest[d['subject']]=d
    return latest
def _mark_signoff_states(signoffs,latest):
    for s in signoffs:
        cur=latest.get(s['subject'])
        if s['revoked_at']: s['state']='revoked'
        elif cur and cur['evidence_hash']==s['evidence_hash'] and cur['rule_version']==s['rule_version']: s['state']='active'
        else: s['state']='superseded'
def _explain(decision,active,policy):
    detail=json.loads(decision['details']);rule=detail.get('rule_status',decision['status']);final=decision['status']
    if final=='released':
        ids=', '.join('#'+str(i) for i in detail.get('applied_signoffs',[]))
        return f'规则结论 {rule}；{policy} 策略下有效签署 {ids} 满足放行条件，转为已复核放行（差异 issue 保留不删除）'
    if rule=='pending':
        n=len({s['reviewer'] for s in active});need=1 if policy=='single' else 2
        if n: return f'规则结论 pending；{policy} 策略需 {need} 名不同复核员，当前有效签署 {n} 人，仍须待核'
        return '规则结论 pending；无有效复核签署，待核'
    if rule=='blocked': return '规则结论 blocked；阻断类差异不允许人工签署放行'
    return '规则结论 accepted；无需复核签署'
def export(db_path,out):
    db=connect(db_path)
    config=json.loads(db.execute("SELECT value FROM meta WHERE key='config_json'").fetchone()['value'])
    review=config.get('review',{});policy=review.get('policy','single');reviewers=review.get('reviewers',[])
    data={table:[dict(r) for r in db.execute(f'SELECT * FROM {table} ORDER BY 1')] for table in ('batch','subject','event','issue','decision','evidence','run','lineage','signoff')}
    data['source']=[dict(r) for r in db.execute('SELECT id,path,sha256 FROM source ORDER BY id')]
    data['config_hash']=db.execute("SELECT value FROM meta WHERE key='config_hash'").fetchone()['value']
    latest=_latest_decisions(data['decision'])
    _mark_signoff_states(data['signoff'],latest)
    data['review']={'policy':policy,'reviewers':reviewers}
    target=Path(out);target.parent.mkdir(parents=True,exist_ok=True)
    target.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
    signoffs_by=defaultdict(list)
    for s in data['signoff']: signoffs_by[s['subject']].append(s)
    decisions_by=defaultdict(list)
    for d in data['decision']: decisions_by[d['subject']].append(d)
    lines=['# 决策报告','','## 复核策略','',f"- 策略: {policy} — {POLICY_LABEL.get(policy,policy)}",f"- 授权复核员: {', '.join(reviewers) if reviewers else '（无）'}",'','## 当前结论','']
    for subject in sorted(latest):
        d=latest[subject];detail=json.loads(d['details'])
        active=[s for s in signoffs_by.get(subject,[]) if s['state']=='active']
        lines.append(f"- {subject}: {d['status']}（规则结论 {detail.get('rule_status',d['status'])}，run {d['run_id']}，规则版本 {d['rule_version']}，证据 {d['evidence_hash'][:12]}…）— {_explain(d,active,policy)}")
    lines+=['','## 签署链','']
    if not data['signoff']: lines.append('（无签署记录）')
    for subject in sorted(signoffs_by):
        lines.append(f'### {subject}')
        for s in signoffs_by[subject]:
            line=f"- #{s['id']} {s['reviewer']} 于 {s['signed_at']} 签署 — 绑定 run {s['run_id']} / 规则版本 {s['rule_version']} / 证据 {s['evidence_hash'][:12]}… — 原因: {s['reason']} — 状态: {s['state']}"
            if s['state']=='revoked': line+=f"（{s['revoked_by']} 于 {s['revoked_at']} 撤销: {s['revoke_reason']}）"
            if s['state']=='superseded': line+='（证据版本已变化，仅作历史记录，不沿用于当前结论）'
            lines.append(line)
        lines.append('')
    lines+=['## 决策历史','']
    for subject in sorted(decisions_by):
        lines.append(f'### {subject}')
        for d in decisions_by[subject]:
            detail=json.loads(d['details'])
            lines.append(f"- run {d['run_id']}: {d['status']}（规则结论 {detail.get('rule_status',d['status'])}）— 差异: {', '.join(detail.get('issues',[])) or '无'} — 证据 {d['evidence_hash'][:12]}…")
        lines.append('')
    md=target.with_suffix('.md')
    md.write_text('\n'.join(lines)+'\n',encoding='utf-8')
    db.close();return target
