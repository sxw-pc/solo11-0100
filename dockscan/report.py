import json
from pathlib import Path
from .store import connect

CLIMATE_TABLES = ('climate_plan', 'climate_command', 'climate_trajectory',
                  'climate_reading', 'climate_hold')


def export(db_path, out):
    db = connect(db_path)
    data = {table: [dict(r) for r in db.execute(f'SELECT * FROM {table} ORDER BY 1')]
            for table in ('batch', 'subject', 'event', 'issue', 'decision',
                          'evidence', 'run', 'lineage')}
    data['source'] = [dict(r) for r in db.execute('SELECT id,path,sha256 FROM source ORDER BY id')]
    data['config_hash'] = db.execute(
        "SELECT value FROM meta WHERE key='config_hash'").fetchone()['value']
    data['climate'] = {table: [dict(r) for r in db.execute(
        f'SELECT * FROM {table} ORDER BY 1')] for table in CLIMATE_TABLES}
    data['climate']['case'] = [dict(r) for r in db.execute(
        'SELECT id,length(payload) AS payload_bytes FROM climate_case ORDER BY id')]
    target = Path(out)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    md = target.with_suffix('.md')
    lines = ['# 决策报告', '']
    lines += [f"- {d['subject']}: {d['status']} — {d['details']}" for d in data['decision']]
    lines += ['', '## 仓门暂存区冷却控制', '']
    lines += _climate_markdown(db)
    md.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    db.close()
    return target


def _climate_markdown(db):
    plans = [dict(r) for r in db.execute(
        'SELECT case_id,objective,version,status,payload FROM climate_plan ORDER BY case_id,objective,version')]
    holds = [dict(r) for r in db.execute(
        'SELECT case_id,objective,reasons,created_at FROM climate_hold ORDER BY id')]
    if not plans and not holds:
        return ['_尚未生成温控计划。_']
    out = []
    for hold in holds:
        out.append(f"### 待核 {hold['case_id']} / {hold['objective']} ({hold['created_at']})")
        out.append('')
        out.append(f"- 不发布任何设备命令；原因：{hold['reasons']}")
        out.append('')
    for plan_row in plans:
        payload = json.loads(plan_row['payload'])
        out.append(f"### {plan_row['case_id']} / {plan_row['objective']} "
                   f"v{plan_row['version']} — {plan_row['status']}")
        out.append('')
        out.append(f"- 已执行时段数：{payload['executed_periods']}；"
                   f"总热暴露：{payload['total_exposure']}°C·时段；"
                   f"最大单仓门累计暴露：{payload['max_dock_exposure']}；"
                   f"电费：{payload['energy_cents']} 分")
        if payload.get('irrecoverable_breach'):
            b = payload['irrecoverable_breach']
            out.append(f"- ⚠ 已发生超限且无法追回：{b['irrecoverable_exposure']}°C·时段"
                       f"（已发生 {b['incurred_total_exposure']} / 上限 {b['cap']}）")
        out.append('')
        out.append('| 时段 | 冷却仓门 | 机台占用 | 电价(分) | 仓门 | 起点°C | 增温 | 冷却量 | 终点°C | 上限 | 暴露 | 来源 |')
        out.append('| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |')
        rows = db.execute('SELECT * FROM climate_trajectory WHERE case_id=? AND objective=? AND version=? '
                          'ORDER BY period,dock',
                          (plan_row['case_id'], plan_row['objective'], plan_row['version']))
        for r in rows:
            owner = r['chiller_owner'] or '空闲'
            out.append(f"| {r['period']} | {owner} | {owner} | {r['price_cents']} | "
                       f"{r['dock']} | {r['start_temp']} | {r['warming']} | {r['cooling_delta']} | "
                       f"{r['end_temp']} | {r['limit_c']} | {r['exposure']} | {r['source']} |")
        out.append('')
        ev = payload.get('evidence', {})
        out.append(f"- 最优性证据：{ev.get('algorithm')}；评估节点 {ev.get('nodes_evaluated')}；"
                   f"剪枝标签 {ev.get('labels_pruned')}；输入摘要 {payload['input_digest'][:12]}…")
        out.append('')
    return out
