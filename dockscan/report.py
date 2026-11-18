"""Offline handover reports over the frozen evidence ledger.

The JSON report is the machine-readable ledger; the Markdown report is the
counter handover sheet.  Both read the *state tables* — the pallet's current
holder, custody chain, contracts and the one-time release — so a release can
only appear here because ``dockscan.unclaimed.deliver`` actually changed them.
Neither report ever includes the reusable receipt text: only digests.
"""
import json
from pathlib import Path
from .store import connect
from . import unclaimed


def _rows(db, sql, params=()):
    return [dict(row) for row in db.execute(sql, params)]


def _case_ids(db):
    return [row['id'] for row in db.execute('SELECT id FROM unclaimed_case ORDER BY id')]


def _case_handover(db_path, case_id):
    """Assemble the verified handover view for one case."""
    status = unclaimed.status(db_path, case_id)
    chain = unclaimed.custody_chain(db_path, case_id)
    verified = unclaimed.verify_claimants(db_path, case_id)
    claimants = {}
    for contract, outcomes in verified['claims'].items():
        claimants[contract] = {
            name: {'eligible': row.get('eligible', False),
                   'credential_valid': (row.get('checks') or {}).get('credential_valid', False),
                   'problem': (row.get('checks') or {}).get('problem'),
                   'credential_digest': (row.get('checks') or {}).get('credential_digest')}
            for name, row in outcomes.items()}
    released = status.get('release')
    return {
        'case_id': case_id,
        'pallet': status.get('pallet'),
        'seal': status.get('seal'),
        'status': status.get('status'),
        'holder': status.get('holder'),
        'custody_version': status.get('version'),
        'custody_valid': status.get('custody_valid'),
        'custody_problems': status.get('custody_problems'),
        'lifecycle_problems': status.get('lifecycle_problems', []),
        'identity_valid': status.get('identity_valid'),
        'contract_problem': status.get('contract_problem'),
        'custody': chain,
        'claimants': claimants,
        'authorizations': status.get('authorizations', []),
        'released': released,
        'delivered_to': released['claimant'] if released else None,
        'delivered_by': released['deliverer'] if released else None,
        'release_contract': released['contract'] if released else None,
        'release_evidence': {
            'source_event_ids': json.loads(released['source_event_ids']) if released else [],
            'credential_digest': released['credential_digest'] if released else None,
            'seal': released['seal'] if released else None,
            'custody_version': released['custody_version'] if released else None,
            'released_at': released['released_at'] if released else None,
        },
    }


def _markdown(data):
    lines = ['# 交接报告', '']
    lines.append('## 批次决策')
    lines.append('')
    if not data['decision']:
        lines.append('- 无决策记录')
    for decision in data['decision']:
        lines.append(f"- {decision['subject']}: {decision['status']} — {decision['details']}")
    lines.append('')
    lines.append('## 待领托盘核验与柜台放行')
    lines.append('')
    if not data['unclaimed']:
        lines.append('- 无待领托盘案件')
    for case in data['unclaimed']:
        released = case['released']
        lines.append(f"### {case['case_id']} — 托盘 {case['pallet']} / 封条 {case['seal']}")
        lines.append('')
        lines.append(f"- 当前状态: `{case['status']}`；现任保管人: `{case['holder'] or '无'}`；保管链版本: {case['custody_version']}")
        lines.append(f"- 保管链有效: {'是' if case['custody_valid'] else '否'}；托盘/来源身份有效: "
                     f"{'是' if case['identity_valid'] else '否'}")
        if case['custody_problems']:
            lines.append(f"- 核验问题: {', '.join(case['custody_problems'])}")
        if case['lifecycle_problems']:
            lines.append(f"- 交付状态: {', '.join(case['lifecycle_problems'])}")
        lines.append('')
        lines.append('| 步骤 | 类型 | 时间 | 交出 | 接收 | 封条 | 签名 | 链至此处有效 |')
        lines.append('|---|---|---|---|---|---|---|---|')
        for step in case['custody']['steps']:
            lines.append(
                f"| {step['custody_id']} | {step['kind']} | {step['at']} | {step['from_holder'] or step['by_holder'] or ''} "
                f"| {step['to_holder'] or ''} | {step['seal']} | {', '.join(step['signed_by'])} "
                f"| {'是' if step['valid_through_here'] else '否'} |")
        lines.append('')
        lines.append('| 合同 | 申请人 | 凭据有效 | 当前可领取 | 问题 | 凭据摘要 |')
        lines.append('|---|---|---|---|---|---|')
        for contract, outcomes in case['claimants'].items():
            for name, outcome in outcomes.items():
                lines.append(
                    f"| {contract} | {name} | {'是' if outcome['credential_valid'] else '否'} "
                    f"| {'是' if outcome['eligible'] else '否'} | {outcome['problem'] or '—'} "
                    f"| {outcome['credential_digest'] or '—'} |")
        lines.append('')
        if released:
            lines.append(f"- **已交出**: 领取人 `{released['claimant']}`，交付人 `{released['deliverer']}`，"
                         f"合同 `{released['contract']}`，凭据摘要 `{released['credential_digest']}`，"
                         f"封条 `{released['seal']}`，来源事件 {released['source_event_ids']}")
        else:
            lines.append('- **未交出**: 实物仍由现任保管人控制，柜台不得交货。')
        lines.append('')
    return '\n'.join(lines) + '\n'


def export(db_path, out):
    db = connect(db_path)
    try:
        data = {table: _rows(db, f'SELECT * FROM {table} ORDER BY 1')
                for table in ('batch', 'subject', 'event', 'issue', 'decision', 'evidence', 'run', 'lineage',
                              'unclaimed_custody', 'pallet_custody', 'unclaimed_authorization', 'unclaimed_release')}
        data['source'] = _rows(db, 'SELECT id,path,sha256 FROM source ORDER BY id')
        row = db.execute("SELECT value FROM meta WHERE key='config_hash'").fetchone()
        data['config_hash'] = row['value'] if row else None
        case_ids = _case_ids(db)
    finally:
        db.close()
    data['unclaimed'] = [_case_handover(db_path, case_id) for case_id in case_ids]
    target = Path(out)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    md = target.with_suffix('.md')
    md.write_text(_markdown(data), encoding='utf-8')
    return target
