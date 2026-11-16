import json
from datetime import datetime,timezone
from .store import connect
from .rules import evaluate

def _now(): return datetime.now(timezone.utc).isoformat()
def _review_config(db):
    row=db.execute("SELECT value FROM meta WHERE key='config_json'").fetchone()
    if not row: raise ValueError('config_missing')
    review=json.loads(row['value']).get('review',{})
    return review.get('policy','single'),review.get('reviewers',[])
def _check_reviewer(db,reviewer):
    _,reviewers=_review_config(db)
    if reviewer not in reviewers: raise ValueError('unauthorized_reviewer')
def _check_reason(reason):
    if not reason or not reason.strip(): raise ValueError('empty_reason')
def sign(db_path,subject,reviewer,reason):
    """复核员签署当前待核结论：绑定当次 run、规则版本与事件/谱系证据摘要，随后重算使结论联动。"""
    db=connect(db_path)
    _check_reviewer(db,reviewer);_check_reason(reason)
    if not db.execute('SELECT 1 FROM subject WHERE id=?',(subject,)).fetchone():
        db.close();raise ValueError('unknown_subject')
    row=db.execute('SELECT * FROM decision WHERE subject=? ORDER BY run_id DESC LIMIT 1',(subject,)).fetchone()
    if not row:
        db.close();raise ValueError('no_decision')
    detail=json.loads(row['details'])
    if detail.get('rule_status',row['status'])!='pending':
        db.close();raise ValueError('not_pending')
    if db.execute('SELECT 1 FROM signoff WHERE subject=? AND reviewer=? AND evidence_hash=? AND revoked_at IS NULL',(subject,reviewer,row['evidence_hash'])).fetchone():
        db.close();raise ValueError('duplicate_signoff')
    with db:
        signoff_id=db.execute('INSERT INTO signoff(subject,run_id,rule_version,evidence_hash,reviewer,reason,signed_at) VALUES (?,?,?,?,?,?,?)',
            (subject,row['run_id'],row['rule_version'],row['evidence_hash'],reviewer,reason,_now())).lastrowid
    db.close()
    results=evaluate(db_path)
    return {'signoff_id':signoff_id,'subject':subject,'status':results.get(subject)}
def revoke(db_path,signoff_id,reviewer,reason):
    """撤销既有签署：只追加撤销标记，签署记录保留为历史，随后重算使结论联动。"""
    db=connect(db_path)
    _check_reviewer(db,reviewer);_check_reason(reason)
    row=db.execute('SELECT * FROM signoff WHERE id=?',(signoff_id,)).fetchone()
    if not row:
        db.close();raise ValueError('signoff_not_found')
    if row['revoked_at']:
        db.close();raise ValueError('already_revoked')
    with db:
        db.execute('UPDATE signoff SET revoked_at=?,revoked_by=?,revoke_reason=? WHERE id=?',(_now(),reviewer,reason,signoff_id))
    subject=row['subject']
    db.close()
    results=evaluate(db_path)
    return {'revoked':signoff_id,'subject':subject,'status':results.get(subject)}
