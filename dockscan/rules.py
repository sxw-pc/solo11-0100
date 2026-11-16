import json
from datetime import datetime,timezone
from collections import defaultdict
from .store import connect
from .ingest import timestamp

VERSION='1'
def evaluate(db_path):
    db=connect(db_path);config=json.loads(db.execute("SELECT value FROM meta WHERE key='config_json'").fetchone()['value'])
    orders={x['box']:x for x in config['boxes']};by=defaultdict(list)
    for row in db.execute('SELECT * FROM event ORDER BY time,id'): by[row['subject']].append(dict(row))
    results={}
    with db:
        run_id=db.execute('INSERT INTO run(started_at,status) VALUES (?,?)',(datetime.now(timezone.utc).isoformat(),'running')).lastrowid
        db.execute('DELETE FROM evidence')
        db.execute('DELETE FROM decision');db.execute('DELETE FROM lineage');db.execute("DELETE FROM issue WHERE code NOT IN ('identity_conflict','parse_error')")
        for box,order in orders.items():
            events=by.pop(box,[]); problems=[];stage=0;received=0;weight=None
            for e in events:
                v=json.loads(e['payload']);kind=e['kind'];time=timestamp(e['time'])
                if v.get('dock')!=order['dock'] or not timestamp(order['window_start'])<=time<=timestamp(order['window_end']):problems.append('wrong_dock_or_window')
                if kind in ('gate','unload','check','exit'):
                    expected=('gate','unload','check','exit')[stage] if stage<4 else None
                    if kind!=expected:problems.append('illegal_transition')
                    else:stage+=1
                if kind=='unload':received+=int(v.get('quantity',0))
                if kind=='weight':weight=float(v['net_kg'])
                if kind in ('split','merge'):
                    parts=v.get('children',[]) if kind=='split' else v.get('parents',[])
                    source=int(v.get('quantity',0))
                    if sum(int(c['quantity']) for c in parts)!=source:problems.append('quantity_not_conserved')
                    elif any(int(c['quantity'])<=0 or c['id']==box for c in parts):problems.append('invalid_lineage')
                    else:
                        for part in parts:
                            parent,child=(box,part['id']) if kind=='split' else (part['id'],box)
                            db.execute('INSERT INTO lineage VALUES (?,?,?,?,?)',(parent,child,int(part['quantity']),kind,e['id']))
            if stage<4:problems.append('missing_stage')
            if received<order['quantity']:problems.append('shortage')
            if received>order['quantity']:problems.append('overage')
            if weight is None:problems.append('missing_weight')
            elif abs(weight-order['expected_kg'])>order['tolerance_kg']:problems.append('weight_mismatch')
            if db.execute("SELECT 1 FROM issue WHERE code='identity_conflict'").fetchone():problems.append('identity_conflict')
            status='blocked' if any(p in problems for p in ('illegal_transition','wrong_dock_or_window','quantity_not_conserved','invalid_lineage','weight_mismatch')) else ('pending' if problems else 'accepted')
            detail=json.dumps({'expected':order['quantity'],'received':received,'weight':weight,'issues':sorted(set(problems)),'evidence':[e['id'] for e in events]},ensure_ascii=False)
            db.execute('INSERT INTO decision VALUES (?,?,?,?)',(box,status,detail,VERSION))
            for e in events:db.execute('INSERT INTO evidence VALUES (?,?,?)',(box,e['id'],VERSION))
            for code in sorted(set(problems)):db.execute('INSERT INTO issue(code,detail) VALUES (?,?)',(code,box))
            results[box]=status
        for box in by:
            db.execute('INSERT INTO issue(code,detail) VALUES (?,?)',('unknown_box',box))
        db.execute('UPDATE run SET completed_at=?,status=? WHERE id=?',(datetime.now(timezone.utc).isoformat(),'done',run_id))
    db.close();return results
