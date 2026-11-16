import json
from datetime import datetime,timezone
from collections import defaultdict
from .store import connect
from .ingest import timestamp
from .identity import known_box,valid_lineage,resolve_class_map,incompatible,bay_certified
from .timeline import advance

VERSION='2'
BLOCKING={'illegal_transition','wrong_dock_or_window','quantity_not_conserved','invalid_lineage','weight_mismatch',
          'missing_hazard_class','unknown_hazard_class','missing_class_origin','unsegregated_merge','bay_not_certified','segregation_conflict'}

def load_contract(config):
    bays={}
    for bay in config.get('segregation_bays',[]):
        bays[bay['bay']]={'dock':bay['dock'],'classes':set(bay.get('classes',[])),
                          'certified_from':timestamp(bay['certified_from']),
                          'certified_until':timestamp(bay['certified_until'])}
    return (config.get('safety_policy','dock_window_exclusive'),config.get('hazard_classes',{}),bays,
            config.get('config_version','unversioned'))

def check_merge_segregation(decl,class_map,hazard_classes,bays):
    """并箱混合不兼容类别时，每个来源货物须位于获准隔离舱。"""
    problems=[];known={}
    for part in decl['parents']:
        pcs=class_map.get(part['id'],set())
        if not pcs:problems.append('missing_class_origin')
        known[part['id']]=pcs
    parents=decl['parents']
    clashing=[(a,b) for i,a in enumerate(parents) for b in parents[i+1:]
              if any(incompatible(ca,cb,hazard_classes) for ca in known.get(a['id'],()) for cb in known.get(b['id'],()))]
    if not clashing:return problems
    for part in parents:
        bay_id=part.get('bay')
        if not bay_id:problems.append('unsegregated_merge');continue
        error=bay_certified(bays,bay_id,decl['dock'],known.get(part['id'],()),decl['time'],decl['time'])
        if error:problems.append(error)
    for a,b in clashing:
        if a.get('bay') and a.get('bay')==b.get('bay'):problems.append('unsegregated_merge')
    return problems

def pair_bays_certified(order_x,order_y,classes_x,classes_y,start,end,bays,dock):
    """并行口径：双方箱单声明的隔离舱不同，且认证完整覆盖重叠窗口。"""
    bay_x,bay_y=order_x.get('bay'),order_y.get('bay')
    if not bay_x or not bay_y or bay_x==bay_y:return False
    return (bay_certified(bays,bay_x,dock,classes_x,start,end) is None
            and bay_certified(bays,bay_y,dock,classes_y,start,end) is None)

def evaluate(db_path):
    db=connect(db_path);config=json.loads(db.execute("SELECT value FROM meta WHERE key='config_json'").fetchone()['value'])
    policy,hazard_classes,bays,config_version=load_contract(config)
    orders={x['box']:x for x in config['boxes']};by=defaultdict(list)
    for row in db.execute('SELECT * FROM event ORDER BY time,id'): by[row['subject']].append(dict(row))
    results={}
    with db:
        run_id=db.execute('INSERT INTO run(started_at,status) VALUES (?,?)',(datetime.now(timezone.utc).isoformat(),'running')).lastrowid
        db.execute('DELETE FROM evidence')
        db.execute('DELETE FROM decision');db.execute('DELETE FROM lineage');db.execute('DELETE FROM class_origin')
        db.execute("DELETE FROM issue WHERE code NOT IN ('identity_conflict','parse_error')")
        # 第一遍：跨全部箱号收集拆并谱系（含非预约的中继箱），只有守恒合法的关系入谱
        splits=[];merges=[];declared=defaultdict(list);merge_decls=defaultdict(list);lineage_problems=defaultdict(list)
        for e in db.execute('SELECT * FROM event ORDER BY time,id'):
            kind=e['kind']
            if kind not in ('split','merge'):continue
            v=json.loads(e['payload']);subject=e['subject']
            try:
                parts=v.get('children',[]) if kind=='split' else v.get('parents',[])
                source=int(v.get('quantity',0));total=sum(int(c['quantity']) for c in parts)
            except (AttributeError,TypeError,ValueError,KeyError):
                lineage_problems[subject].append('invalid_lineage');continue
            if total!=source:lineage_problems[subject].append('quantity_not_conserved');continue
            if not valid_lineage(subject,parts,source):lineage_problems[subject].append('invalid_lineage');continue
            for part in parts:
                parent,child=(subject,part['id']) if kind=='split' else (part['id'],subject)
                db.execute('INSERT INTO lineage VALUES (?,?,?,?,?)',(parent,child,int(part['quantity']),kind,e['id']))
                (splits if kind=='split' else merges).append((parent,child,e['id']))
            if kind=='merge':
                merge_decls[subject].append({'event_id':e['id'],'time':timestamp(e['time']),'dock':v.get('dock'),'parents':parts})
                for part in parts:
                    if part.get('class'):declared[part['id']].append((str(part['class']),f"event:{e['id']}"))
        for box,order in orders.items():
            if order.get('hazard_class'):declared[box].append((str(order['hazard_class']),f"config:{box}"))
        class_map,origins=resolve_class_map(declared,splits,merges)
        for box,by_class in origins.items():
            for cls,sources in by_class.items():
                for src in sources:db.execute('INSERT OR IGNORE INTO class_origin VALUES (?,?,?)',(box,cls,src))
        # 第二遍：预约箱常规判定与危险类别契约
        base={}
        for box,order in orders.items():
            events=by.pop(box,[]);problems=[];stage=0;received=0;weight=None
            for e in events:
                v=json.loads(e['payload']);kind=e['kind'];time=timestamp(e['time'])
                if v.get('dock')!=order['dock'] or not timestamp(order['window_start'])<=time<=timestamp(order['window_end']):problems.append('wrong_dock_or_window')
                if kind in ('gate','unload','check','exit'):
                    stage,error=advance(stage,kind)
                    if error:problems.append(error)
                if kind=='unload':received+=int(v.get('quantity',0))
                if kind=='weight':weight=float(v['net_kg'])
            problems+=lineage_problems.get(box,[])
            if stage<4:problems.append('missing_stage')
            if received<order['quantity']:problems.append('shortage')
            if received>order['quantity']:problems.append('overage')
            if weight is None:problems.append('missing_weight')
            elif abs(weight-order['expected_kg'])>order['tolerance_kg']:problems.append('weight_mismatch')
            box_classes=class_map.get(box,set())
            if not box_classes:problems.append('missing_hazard_class')
            elif any(cls not in hazard_classes for cls in box_classes):problems.append('unknown_hazard_class')
            for decl in merge_decls.get(box,[]):problems+=check_merge_segregation(decl,class_map,hazard_classes,bays)
            if db.execute("SELECT 1 FROM issue WHERE code='identity_conflict'").fetchone():problems.append('identity_conflict')
            base[box]={'problems':problems,'received':received,'weight':weight,'events':events}
        # 第三遍：同月台预约窗口重叠的不兼容类别对，按本批安全策略判定同时作业范围
        conflicts=defaultdict(list);boxes=list(orders)
        for i,box_x in enumerate(boxes):
            for box_y in boxes[i+1:]:
                order_x,order_y=orders[box_x],orders[box_y]
                if order_x['dock']!=order_y['dock']:continue
                start=max(timestamp(order_x['window_start']),timestamp(order_y['window_start']))
                end=min(timestamp(order_x['window_end']),timestamp(order_y['window_end']))
                if start>end:continue
                classes_x={c for c in class_map.get(box_x,set()) if c in hazard_classes}
                classes_y={c for c in class_map.get(box_y,set()) if c in hazard_classes}
                clashes=sorted({(a,b) for a in classes_x for b in classes_y if incompatible(a,b,hazard_classes)})
                if not clashes:continue
                cleared=(policy=='certified_bays_parallel'
                         and pair_bays_certified(order_x,order_y,classes_x,classes_y,start,end,bays,order_x['dock']))
                disposition='parallel_cleared' if cleared else 'blocked'
                for subj,peer,self_classes,peer_classes in ((box_x,box_y,classes_x,classes_y),(box_y,box_x,classes_y,classes_x)):
                    conflicts[subj].append({'with':peer,'dock':order_x['dock'],
                        'overlap_start':start.isoformat(),'overlap_end':end.isoformat(),
                        'clashing_classes':[list(pair) for pair in clashes],
                        'self_classes':sorted(self_classes),'peer_classes':sorted(peer_classes),
                        'self_bay':orders[subj].get('bay'),'peer_bay':orders[peer].get('bay'),
                        'policy':policy,'config_version':config_version,'disposition':disposition,
                        'self_sources':origins.get(subj,{}),'peer_sources':origins.get(peer,{})})
                if not cleared:
                    base[box_x]['problems'].append('segregation_conflict')
                    base[box_y]['problems'].append('segregation_conflict')
        for box,info in base.items():
            problems=info['problems']
            status='blocked' if any(p in BLOCKING for p in problems) else ('pending' if problems else 'accepted')
            detail=json.dumps({'expected':orders[box]['quantity'],'received':info['received'],'weight':info['weight'],
                'issues':sorted(set(problems)),'evidence':[e['id'] for e in info['events']],
                'config_version':config_version,'safety_policy':policy,
                'hazard_classes':sorted(class_map.get(box,())),'class_origins':origins.get(box,{}),
                'conflicts':conflicts.get(box,[])},ensure_ascii=False)
            db.execute('INSERT INTO decision VALUES (?,?,?,?,?)',(box,status,detail,VERSION,run_id))
            for e in info['events']:db.execute('INSERT INTO evidence VALUES (?,?,?,?)',(box,e['id'],VERSION,run_id))
            for code in sorted(set(problems)):db.execute('INSERT INTO issue(code,detail) VALUES (?,?)',(code,box))
            results[box]=status
        for subject,probs in lineage_problems.items():
            if subject not in orders:
                for code in sorted(set(probs)):db.execute('INSERT INTO issue(code,detail) VALUES (?,?)',(code,subject))
        members={endpoint for edge in splits+merges for endpoint in edge[:2]}
        for box in by:
            if box not in members and not known_box(box,orders):db.execute('INSERT INTO issue(code,detail) VALUES (?,?)',('unknown_box',box))
        db.execute('UPDATE run SET completed_at=?,status=? WHERE id=?',(datetime.now(timezone.utc).isoformat(),'done',run_id))
    db.close();return results
