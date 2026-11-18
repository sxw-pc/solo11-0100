import json
from pathlib import Path
from .store import connect

RETURN_TABLES = ('return_case', 'return_amendment', 'return_run', 'repair_station',
                 'repair_order', 'unit_trace', 'inventory_ledger', 'refund_ledger',
                 'return_issue')


def export(db_path, out):
    db = connect(db_path)
    data = {table: [dict(r) for r in db.execute(f'SELECT * FROM {table} ORDER BY 1')]
            for table in ('batch', 'subject', 'event', 'issue', 'decision', 'evidence',
                          'run', 'lineage')}
    data['source'] = [dict(r) for r in db.execute('SELECT id,path,sha256 FROM source ORDER BY id')]
    data['config_hash'] = db.execute(
        "SELECT value FROM meta WHERE key='config_hash'").fetchone()['value']
    for table in RETURN_TABLES:
        data[table] = [dict(r) for r in db.execute(f'SELECT * FROM {table} ORDER BY 1')]

    data['returns_summary'] = []
    for row in db.execute('SELECT * FROM return_run WHERE status=? ORDER BY id', ('done',)):
        summary = json.loads(row['summary'])
        summary['refund_ledger'] = [dict(r) for r in db.execute(
            'SELECT * FROM refund_ledger WHERE run_id=? ORDER BY id', (row['id'],))]
        summary['inventory_ledger'] = [dict(r) for r in db.execute(
            'SELECT * FROM inventory_ledger WHERE run_id=? ORDER BY id', (row['id'],))]
        summary['return_issues'] = [dict(r) for r in db.execute(
            'SELECT * FROM return_issue WHERE run_id=? ORDER BY id', (row['id'],))]
        data['returns_summary'].append(summary)

    target = Path(out)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    md = target.with_suffix('.md')
    lines = ['# 决策报告', '']
    lines += [f"- {d['subject']}: {d['status']} — {d['details']}" for d in data['decision']]
    lines += ['', '# 逆向退货处置台账', '']
    if not data['returns_summary']:
        lines.append('- （暂无退货处置版本）')
    for s in data['returns_summary']:
        lines.append(f"## run {s['run_id']} · {s['case_id']} · 合同 {s['contract']}（版本 {s['rule_version']}）")
        lines.append(f"- 可再售序列号: {', '.join(s['resellable_units']) or '无'}")
        if s['quarantined_units']:
            for unit, reasons in s['quarantined_units'].items():
                lines.append(f"- 隔离: {unit} — 原因 {', '.join(reasons)}")
        if s['awaiting_repair_units']:
            lines.append(f"- 待修(无工位): {', '.join(s['awaiting_repair_units'])}")
        lines.append(f"- 同号重复扫描次数: {s['duplicate_scans']}；占用修复工位: {s['stations_used']}/{s['repair_slots']}")
        for r in s['rmas']:
            mark = '成组合格' if r['complete'] else '不构成完整履约'
            lines.append(f"- RMA {r['rma_id']}（{r['customer']}）: {mark}，退款义务 {r['refund_amount']}")
        lines.append(f"- 退款总额: {s['refund_total']}；输入摘要: {s['input_digest'][:16]}…")
        lines.append('')
    md.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    db.close()
    return target
