import hashlib,json
from datetime import datetime,timezone
from collections import defaultdict
from .store import connect
from .ingest import timestamp
from .identity import known_box,valid_lineage
from .timeline import advance

VERSION='2'
BLOCKING=('illegal_transition','wrong_dock_or_window','quantity_not_conserved','invalid_lineage','weight_mismatch')
def evidence_digest(subject,events,lineage_rows,conflict_ids,rule_version):
    payload={'subject':subject,'rule_version':rule_version,
        'events':[[e['id'],e['identity'],e['sha256']] for e in events],
        'lineage':sorted([list(r) for r in lineage_rows]),
        'identity_conflicts':sorted(conflict_ids)}
    canonical=json.dumps(payload,sort_keys=True,ensure_ascii=False,separators=(',',':'))
    return hashlib.sha256(canonical.encode()).hexdigest()
def evaluate(db_path):
    db=connect(db_path);config=json.loads(db.execute("SELECT value FROM meta WHERE key='config_json'").fetchone()['value'])
    review=config.get('review',{});policy=review.get('policy','single');needed=1 if policy=='single' else 2
    orders={x['box']:x for x in config['boxes']};by=defaultdict(list)
    for row in db.execute('SELECT * FROM event ORDER BY time,id'): by[row['subject']].append(dict(row))
    conflict_ids=[r['id'] for r in db.execute("SELECT id FROM issue WHERE code='identity_conflict' ORDER BY id")]
    results={}
    with db:
        run_id=db.execute('INSERT INTO run(started_at,status) VALUES (?,?)',(datetime.now(timezone.utc).isoformat(),'running')).lastrowid
        db.execute("DELETE FROM issue WHERE code NOT IN ('identity_conflict','parse_error')")
        for box,order in orders.items():
            events=by.pop(box,[]); problems=[];stage=0;received=0;weight=None;lineage_rows=[]
            for e in events:
                v=json.loads(e['payload']);kind=e['kind'];time=timestamp(e['time'])
                if v.get('dock')!=order['dock'] or not timestamp(order['window_start'])<=time<=timestamp(order['window_end']):problems.append('wrong_dock_or_window')
                if kind in ('gate','unload','check','exit'):
                    stage,error=advance(stage,kind)
                    if error:problems.append(error)
                if kind=='unload':received+=int(v.get('quantity',0))
                if kind=='weight':weight=float(v['net_kg'])
                if kind in ('split','merge'):
                    parts=v.get('children',[]) if kind=='split' else v.get('parents',[])
                    source=int(v.get('quantity',0))
                    if sum(int(c['quantity']) for c in parts)!=source:problems.append('quantity_not_conserved')
                    elif not valid_lineage(box,parts,source):problems.append('invalid_lineage')
                    else:
                        for part in parts:
                            parent,child=(box,part['id']) if kind=='split' else (part['id'],box)
                            lineage_rows.append((parent,child,int(part['quantity']),kind,e['id']))
                            db.execute('INSERT INTO lineage VALUES (?,?,?,?,?,?)',(*lineage_rows[-1],run_id))
            if stage<4:problems.append('missing_stage')
            if received<order['quantity']:problems.append('shortage')
            if received>order['quantity']:problems.append('overage')
            if weight is None:problems.append('missing_weight')
            elif abs(weight-order['expected_kg'])>order['tolerance_kg']:problems.append('weight_mismatch')
            if conflict_ids:problems.append('identity_conflict')
            rule_status='blocked' if any(p in problems for p in BLOCKING) else ('pending' if problems else 'accepted')
            digest=evidence_digest(box,events,lineage_rows,conflict_ids,VERSION)
            applied=[]
            if rule_status=='pending':
                rows=db.execute('SELECT id,reviewer FROM signoff WHERE subject=? AND evidence_hash=? AND rule_version=? AND revoked_at IS NULL ORDER BY id',(box,digest,VERSION)).fetchall()
                if len({r['reviewer'] for r in rows})>=needed: applied=[r['id'] for r in rows]
            status='released' if applied else rule_status
            detail=json.dumps({'expected':order['quantity'],'received':received,'weight':weight,'issues':sorted(set(problems)),'evidence':[e['id'] for e in events],'evidence_hash':digest,'rule_status':rule_status,'applied_signoffs':applied,'review_policy':policy},ensure_ascii=False)
            db.execute('INSERT INTO decision(subject,status,details,rule_version,run_id,evidence_hash) VALUES (?,?,?,?,?,?)',(box,status,detail,VERSION,run_id,digest))
            for e in events:db.execute('INSERT INTO evidence VALUES (?,?,?,?)',(box,e['id'],VERSION,run_id))
            for code in sorted(set(problems)):
                if code not in ('identity_conflict','parse_error'):db.execute('INSERT INTO issue(code,detail) VALUES (?,?)',(code,box))
            results[box]=status
        for box in by:
            if not known_box(box,orders):db.execute('INSERT INTO issue(code,detail) VALUES (?,?)',('unknown_box',box))
        db.execute('UPDATE run SET completed_at=?,status=? WHERE id=?',(datetime.now(timezone.utc).isoformat(),'done',run_id))
    db.close();return results
