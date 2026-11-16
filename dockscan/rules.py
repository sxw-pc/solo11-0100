import json
from datetime import datetime,timezone
from collections import defaultdict
from .store import connect
from .ingest import timestamp
from .identity import known_box,valid_lineage
from . import timeline

VERSION='2'
POLICIES=('strict','consensus')
BLOCKING=('illegal_transition','no_legal_order','lineage_cycle','wrong_dock_or_window',
          'quantity_not_conserved','invalid_lineage','weight_mismatch')

def _conflicted_devices(db):
    devices=set()
    for row in db.execute("SELECT detail FROM issue WHERE code='identity_conflict'"):
        detail=row['detail'];device=None
        try:
            parsed=json.loads(detail)
            if isinstance(parsed,dict): device=parsed.get('device')
        except (ValueError,TypeError):
            if isinstance(detail,str) and ':' in detail: device=detail.split(':',1)[0]
        if device: devices.add(str(device))
    return devices

def _issue_detail(code,box,ctx):
    if code=='unproven_order':
        return json.dumps({'box':box,'unordered_pairs':ctx['pairs'],'policy':ctx['policy']},ensure_ascii=False)
    if code=='divergent_order':
        return json.dumps({'box':box,'legal_orders':ctx['legal'],'policy':ctx['policy']},ensure_ascii=False)
    if code=='no_legal_order':
        return json.dumps({'box':box,'cycle_events':ctx['cycle']},ensure_ascii=False)
    if code=='sequence_gap':
        return json.dumps({'box':box,'gaps':ctx['gaps']},ensure_ascii=False)
    if code=='lineage_cycle':
        return json.dumps({'box':box,'cycles':ctx['cycles']},ensure_ascii=False)
    if code=='order_space_truncated':
        return json.dumps({'box':box,'cap':timeline.EXTENSION_CAP},ensure_ascii=False)
    if code=='identity_conflict':
        return json.dumps({'box':box,'devices':ctx['suspect']},ensure_ascii=False)
    return box

def evaluate(db_path,policy='strict'):
    if policy not in POLICIES: raise ValueError('unknown_policy')
    db=connect(db_path);config=json.loads(db.execute("SELECT value FROM meta WHERE key='config_json'").fetchone()['value'])
    orders={x['box']:x for x in config['boxes']}
    events=[dict(r) for r in db.execute('SELECT * FROM event ORDER BY id')]
    for e in events: e['payload_dict']=json.loads(e['payload'])
    by=defaultdict(list)
    for e in events: by[e['subject']].append(e)
    suspect=_conflicted_devices(db)
    gaps=timeline.sequence_gaps(events)
    edges=timeline.build_edges(events,suspect)
    reach=timeline.reachability(events,edges)
    cycle_events=timeline.cycle_members(reach)
    lineage_rows=[];lineage_problems=defaultdict(list)
    for e in events:
        v=e['payload_dict'];kind=e['kind']
        if kind not in ('split','merge'): continue
        parts=v.get('children',[]) if kind=='split' else v.get('parents',[])
        source=int(v.get('quantity',0))
        if sum(int(c['quantity']) for c in parts)!=source: lineage_problems[e['subject']].append('quantity_not_conserved')
        elif not valid_lineage(e['subject'],parts,source): lineage_problems[e['subject']].append('invalid_lineage')
        else:
            for part in parts:
                parent,child=(e['subject'],part['id']) if kind=='split' else (part['id'],e['subject'])
                lineage_rows.append((str(parent),str(child),int(part['quantity']),kind,e['id']))
    box_graph=defaultdict(set)
    for parent,child,_,_,_ in lineage_rows: box_graph[parent].add(child)
    lineage_cycles=timeline.graph_cycles(box_graph)
    boxes_on_cycles={b for cyc in lineage_cycles for b in cyc}
    results={}
    with db:
        run_id=db.execute('INSERT INTO run(started_at,status) VALUES (?,?)',(datetime.now(timezone.utc).isoformat(),'running')).lastrowid
        db.execute('DELETE FROM evidence')
        db.execute('DELETE FROM decision');db.execute('DELETE FROM lineage');db.execute("DELETE FROM issue WHERE code NOT IN ('identity_conflict','parse_error')")
        for row in lineage_rows: db.execute('INSERT INTO lineage VALUES (?,?,?,?,?)',row)
        for box,order in orders.items():
            box_events=by.pop(box,[]);by_id={e['id']:e for e in box_events};ids=sorted(by_id)
            problems=list(lineage_problems.get(box,()))
            for e in box_events:
                v=e['payload_dict'];moment=timestamp(e['time'])
                if v.get('dock')!=order['dock'] or not timestamp(order['window_start'])<=moment<=timestamp(order['window_end']):problems.append('wrong_dock_or_window')
            box_cycle=[i for i in ids if i in cycle_events]
            if box_cycle: problems.append('no_legal_order')
            extensions,truncated=timeline.linear_extensions(ids,reach)
            legal=[o for o in extensions if timeline.stage_legal(o,by_id)]
            if box in boxes_on_cycles: problems.append('lineage_cycle')
            if not legal and not box_cycle: problems.append('illegal_transition')
            if truncated: problems.append('order_space_truncated')
            conclusions=[timeline.conclude(o,by_id) for o in legal]
            certain=len(set(conclusions))<=1
            if policy=='strict':
                if not truncated and len(extensions)>1: problems.append('unproven_order')
            elif not truncated and legal and not certain: problems.append('divergent_order')
            received=sum(int(e['payload_dict'].get('quantity',0)) for e in box_events if e['kind']=='unload')
            stage=conclusions[0][0] if legal else 0
            weight=conclusions[0][2] if (legal and certain) else None
            has_weight=any(e['kind']=='weight' for e in box_events)
            if stage<4:problems.append('missing_stage')
            if received<order['quantity']:problems.append('shortage')
            if received>order['quantity']:problems.append('overage')
            if not has_weight:problems.append('missing_weight')
            elif weight is not None and abs(weight-order['expected_kg'])>order['tolerance_kg']:problems.append('weight_mismatch')
            box_devices={e['device'] for e in box_events if e['device'] is not None}
            box_gaps={d:gaps[d] for d in sorted(box_devices) if d in gaps}
            if box_gaps: problems.append('sequence_gap')
            box_suspect=sorted(box_devices&suspect)
            if box_suspect: problems.append('identity_conflict')
            status='blocked' if any(p in BLOCKING for p in problems) else ('pending' if problems else 'accepted')
            pairs=timeline.unordered_pairs(ids,reach)
            detail=json.dumps({'expected':order['quantity'],'received':received,'weight':weight,
                'weight_uncertain':has_weight and weight is None,
                'issues':sorted(set(problems)),'evidence':ids,'policy':policy,
                'causal':{'unordered_pairs':[list(p) for p in pairs],
                          'extension_count':len(extensions),'legal_order_count':len(legal),'truncated':truncated,
                          'suspect_devices':box_suspect,'sequence_gaps':box_gaps,
                          'lineage_paths':timeline.lineage_paths(box_graph,box),
                          'cycle_events':box_cycle,
                          'lineage_cycles':[c for c in lineage_cycles if box in c]}},ensure_ascii=False)
            db.execute('INSERT INTO decision VALUES (?,?,?,?,?)',(box,status,detail,VERSION,run_id))
            for e in box_events:db.execute('INSERT INTO evidence VALUES (?,?,?,?)',(box,e['id'],VERSION,run_id))
            ctx={'pairs':[list(p) for p in pairs],'legal':[list(o) for o in legal[:8]],'cycle':box_cycle,
                 'gaps':box_gaps,'cycles':[c for c in lineage_cycles if box in c],'suspect':box_suspect,'policy':policy}
            for code in sorted(set(problems)):db.execute('INSERT INTO issue(code,detail) VALUES (?,?)',(code,_issue_detail(code,box,ctx)))
            results[box]=status
        for box in by:
            if not known_box(box,orders):db.execute('INSERT INTO issue(code,detail) VALUES (?,?)',('unknown_box',box))
        db.execute('UPDATE run SET completed_at=?,status=? WHERE id=?',(datetime.now(timezone.utc).isoformat(),'done',run_id))
    db.close();return results
