import json
from datetime import datetime,timezone
from collections import defaultdict
from .store import connect
from .ingest import timestamp,hash_bytes
from .identity import (known_box,valid_lineage,incompatible_pairs,
                       resolve_categories,cargo_roots,categories_conflict)
from .timeline import advance

VERSION='2'
BLOCKING=('illegal_transition','wrong_dock_or_window','quantity_not_conserved','invalid_lineage',
          'weight_mismatch','category_conflict','isolation_unproven','mixed_incompatible_unsegregated')

def evaluate(db_path):
    db=connect(db_path)
    row=db.execute("SELECT value FROM meta WHERE key='config_json'").fetchone()
    if row is None:
        db.close();raise ValueError('config_not_frozen')
    config_raw=row['value']
    stored=db.execute("SELECT value FROM meta WHERE key='config_hash'").fetchone()['value']
    if hash_bytes(config_raw.encode('utf-8'))!=stored:
        db.close();raise ValueError('config_tampered')
    config=json.loads(config_raw)
    orders={x['box']:x for x in config['boxes']}
    policy=config.get('policy','mutual_exclusion')
    config_version=str(config.get('version','1'))
    categories_cfg=config.get('categories') or {}
    armed=bool(categories_cfg)
    pairs=incompatible_pairs(categories_cfg)
    certified={str(d):{str(b) for b in bays} for d,bays in (config.get('isolation_bays') or {}).items()}
    config_bays={x['box']:str(x['bay']) for x in config['boxes'] if x.get('bay')}
    box_dock={x['box']:x.get('dock') for x in config['boxes']}
    rows=db.execute('SELECT * FROM event ORDER BY time,id').fetchall()
    by=defaultdict(list);spans=defaultdict(dict);event_bays=defaultdict(set)
    for r in rows:
        e=dict(r);by[e['subject']].append(e)
        payload=json.loads(e['payload']);dock=payload.get('dock')
        if dock:
            dock=str(dock);slot=spans[dock].setdefault(e['subject'],[e['time'],e['time']])
            slot[0]=min(slot[0],e['time']);slot[1]=max(slot[1],e['time'])
            if payload.get('bay'): event_bays[(e['subject'],dock)].add(str(payload['bay']))
    results={}
    with db:
        run_id=db.execute('INSERT INTO run(started_at,status) VALUES (?,?)',(datetime.now(timezone.utc).isoformat(),'running')).lastrowid
        db.execute('DELETE FROM evidence')
        db.execute('DELETE FROM decision');db.execute('DELETE FROM lineage');db.execute('DELETE FROM conflict')
        db.execute("DELETE FROM issue WHERE code NOT IN ('identity_conflict','parse_error')")
        per_box={};edges=[];split_parents=defaultdict(set);merges=[];lineage_boxes=set()
        for box,order in orders.items():
            events=by.pop(box,[]); problems=[];stage=0;received=0;weight=None
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
                            db.execute('INSERT INTO lineage VALUES (?,?,?,?,?)',(parent,child,int(part['quantity']),kind,e['id']))
                            edges.append((str(parent),str(child)))
                            if kind=='split': split_parents[str(child)].add(str(parent))
                            lineage_boxes.add(str(parent));lineage_boxes.add(str(child))
                        if kind=='merge':
                            merges.append({'box':box,'dock':v.get('dock'),'event_id':e['id'],'time':e['time'],'parents':parts})
            if stage<4:problems.append('missing_stage')
            if received<order['quantity']:problems.append('shortage')
            if received>order['quantity']:problems.append('overage')
            if weight is None:problems.append('missing_weight')
            elif abs(weight-order['expected_kg'])>order['tolerance_kg']:problems.append('weight_mismatch')
            if db.execute("SELECT 1 FROM issue WHERE code='identity_conflict'").fetchone():problems.append('identity_conflict')
            per_box[box]={'problems':problems,'received':received,'weight':weight,'events':events,'order':order}
        extra=defaultdict(list);direct_issues=set();conflict_rows=[]
        cats={};prov={};roots={};bay_of=None
        if armed:
            cats,prov=resolve_categories({x['box']:{str(x['category'])} for x in config['boxes'] if x.get('category')},edges)
            roots=cargo_roots(set(orders),edges)
            bay_memo={};bay_clash=set()
            def bay_of(box,dock,seen=()):
                key=(box,dock)
                if key in bay_memo: return bay_memo[key]
                direct=set(event_bays.get(key,()))
                declared=config_bays.get(box)
                if declared and box_dock.get(box)==dock: direct.add(declared)
                resolved=None
                if len(direct)>1: bay_clash.add(key)
                elif direct: resolved=next(iter(direct))
                elif box not in seen:
                    inherited={b for p in split_parents.get(box,()) for b in (bay_of(p,dock,seen+(box,)),) if b}
                    if len(inherited)==1: resolved=inherited.pop()
                    elif len(inherited)>1: bay_clash.add(key)
                bay_memo[key]=resolved
                return resolved
            def flag(box,code):
                if box in orders: extra[box].append(code)
                else: direct_issues.add((code,box))
            def origins(box,cset):
                return sorted({o for c in cset for o in prov.get(box,{}).get(c,set())})
            for m in merges:
                dock=str(m['dock']) if m['dock'] is not None else ''
                pcats={str(p['id']):set(cats.get(str(p['id']),())) for p in m['parents']}
                if any(not c for c in pcats.values()):
                    flag(m['box'],'category_unknown')
                    conflict_rows.append({'kind':'merge','dock':dock,'box_a':m['box'],'box_b':'',
                        'categories_a':[],'categories_b':{k:sorted(v) for k,v in pcats.items()},
                        'origins_a':[],'origins_b':{k:origins(k,v) for k,v in pcats.items()},
                        'bay_a':None,'bay_b':None,'overlap_start':m['time'],'overlap_end':m['time'],
                        'outcome':'unproven','detail':{'event_id':m['event_id'],'missing_category_source':sorted(k for k,v in pcats.items() if not v)}})
                    continue
                union=set().union(*pcats.values())
                bad=[p for p in categories_conflict(union,union,pairs) if p[0]<p[1]]
                if not bad: continue
                pbays={str(p['id']):(str(p['bay']) if p.get('bay') else bay_of(str(p['id']),dock)) for p in m['parents']}
                ok=all(pbays.values()) and all(b in certified.get(dock,set()) for b in pbays.values())
                if ok:
                    ids=[str(p['id']) for p in m['parents']]
                    for i in range(len(ids)):
                        for j in range(i+1,len(ids)):
                            if categories_conflict(pcats[ids[i]],pcats[ids[j]],pairs) and pbays[ids[i]]==pbays[ids[j]]:
                                ok=False
                if not ok: flag(m['box'],'mixed_incompatible_unsegregated')
                conflict_rows.append({'kind':'merge','dock':dock,'box_a':m['box'],'box_b':'',
                    'categories_a':sorted(union),'categories_b':{k:sorted(v) for k,v in pcats.items()},
                    'origins_a':origins(m['box'],union),'origins_b':{k:origins(k,v) for k,v in pcats.items()},
                    'bay_a':None,'bay_b':None,'overlap_start':m['time'],'overlap_end':m['time'],
                    'outcome':'segregated' if ok else 'unsegregated',
                    'detail':{'event_id':m['event_id'],'parent_bays':pbays,'incompatible':[list(p) for p in bad]}})
            for dock in sorted(spans):
                boxes=sorted(spans[dock])
                for i in range(len(boxes)):
                    for j in range(i+1,len(boxes)):
                        a,b=boxes[i],boxes[j]
                        sa,sb=spans[dock][a],spans[dock][b]
                        if not (sa[0]<=sb[1] and sb[0]<=sa[1]): continue
                        if (roots.get(a) or {a}) & (roots.get(b) or {b}): continue
                        ca,cb=cats.get(a,set()),cats.get(b,set())
                        overlap=[max(sa[0],sb[0]),min(sa[1],sb[1])]
                        base={'kind':'pair','dock':dock,'box_a':a,'box_b':b,
                              'categories_a':sorted(ca),'categories_b':sorted(cb),
                              'origins_a':origins(a,ca),'origins_b':origins(b,cb),
                              'bay_a':bay_of(a,dock),'bay_b':bay_of(b,dock),
                              'overlap_start':overlap[0],'overlap_end':overlap[1]}
                        if not ca or not cb:
                            flag(a,'category_unknown');flag(b,'category_unknown')
                            conflict_rows.append({**base,'outcome':'unproven','detail':{'missing_category_source':[x for x,c in ((a,ca),(b,cb)) if not c]}})
                            continue
                        bad=categories_conflict(ca,cb,pairs)
                        if not bad: continue
                        base['detail']={'incompatible':[list(p) for p in bad]}
                        if policy=='isolation_bays':
                            ba,bb=base['bay_a'],base['bay_b']
                            if ba and bb and ba!=bb and ba in certified.get(dock,set()) and bb in certified.get(dock,set()):
                                conflict_rows.append({**base,'outcome':'parallel_allowed'})
                            else:
                                flag(a,'isolation_unproven');flag(b,'isolation_unproven')
                                conflict_rows.append({**base,'outcome':'blocked'})
                        else:
                            flag(a,'category_conflict');flag(b,'category_conflict')
                            conflict_rows.append({**base,'outcome':'blocked'})
            for box,dock in sorted(bay_clash): flag(box,'bay_mismatch')
            for dock in sorted(spans):
                for box in sorted(spans[dock]):
                    resolved=bay_of(box,dock)
                    if resolved and resolved not in certified.get(dock,set()):
                        flag(box,'bay_uncertified')
        for box,data in per_box.items():
            order=data['order'];problems=data['problems']+extra.get(box,[])
            status='blocked' if any(p in BLOCKING for p in problems) else ('pending' if problems else 'accepted')
            detail=json.dumps({'expected':order['quantity'],'received':data['received'],'weight':data['weight'],
                'issues':sorted(set(problems)),'evidence':[e['id'] for e in data['events']],
                'categories':sorted(cats.get(box,())),'category_origins':{c:origins(box,(c,)) for c in cats.get(box,())} if armed else {},
                'bay':bay_of(box,order['dock']) if armed else None,
                'policy':policy,'config_version':config_version,'config_hash':stored,
                'conflicts':[r for r in conflict_rows if r['box_a']==box or r['box_b']==box]},ensure_ascii=False,sort_keys=True)
            db.execute('INSERT INTO decision VALUES (?,?,?,?,?)',(box,status,detail,VERSION,run_id))
            for e in data['events']:db.execute('INSERT INTO evidence VALUES (?,?,?,?)',(box,e['id'],VERSION,run_id))
            for code in sorted(set(problems)):db.execute('INSERT INTO issue(code,detail) VALUES (?,?)',(code,box))
            results[box]=status
        for code,box in sorted(direct_issues):db.execute('INSERT INTO issue(code,detail) VALUES (?,?)',(code,box))
        for r in conflict_rows:
            db.execute('INSERT INTO conflict(kind,dock,box_a,box_b,categories_a,categories_b,origins_a,origins_b,bay_a,bay_b,overlap_start,overlap_end,policy,config_version,outcome,detail,run_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (r['kind'],r['dock'],r['box_a'],r['box_b'],json.dumps(r['categories_a'],ensure_ascii=False),
                 json.dumps(r['categories_b'],ensure_ascii=False,sort_keys=True),json.dumps(r['origins_a'],ensure_ascii=False),
                 json.dumps(r['origins_b'],ensure_ascii=False,sort_keys=True),r['bay_a'],r['bay_b'],
                 r['overlap_start'],r['overlap_end'],policy,config_version,r['outcome'],
                 json.dumps(r['detail'],ensure_ascii=False,sort_keys=True),run_id))
        for box in by:
            if not known_box(box,orders,lineage_boxes):db.execute('INSERT INTO issue(code,detail) VALUES (?,?)',('unknown_box',box))
        db.execute('UPDATE run SET completed_at=?,status=? WHERE id=?',(datetime.now(timezone.utc).isoformat(),'done',run_id))
    db.close();return results
