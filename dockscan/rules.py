import json
from datetime import datetime,timezone
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from .store import connect, save_settlement
from .ingest import timestamp
from .identity import (known_box, valid_lineage, build_lineage_edges,
                       duplicate_parts, divergent_references, find_cycles,
                       reachable_boxes, leaf_boxes, apportion_tare, ceiling_round)
from .timeline import advance

VERSION='2'
ROUNDING_MODES = ('per_leaf', 'shipment')
SETTLEMENT_BLOCKERS = ('lineage_cycle', 'duplicate_lineage_reference', 'calibration_expired',
                       'duplicate_weighing', 'negative_net_weight', 'weight_not_conserved',
                       'missing_leaf_gross', 'missing_parent_tare', 'missing_rounding_config')
EPS = Decimal('0.001')


def _dec(value):
    return Decimal(str(value))


def _calibration_status(payload, event_time, calibrations):
    """Return (version, ok). A weighing whose calibration version is missing or
    no longer valid at weighing time cannot support a carrier bill."""
    version = payload.get('calibration_version')
    if not version:
        return None, False
    for cal in calibrations:
        if cal.get('id') != version:
            continue
        start = timestamp(cal['valid_from'])
        end = timestamp(cal['valid_until'])
        return version, start <= event_time <= end
    return version, False


def _measured_nets(scope, weights):
    """Gross / tare / measured net per box from the retained weight events."""
    gross, tare, nets = {}, {}, {}
    for box in scope:
        for e in weights.get(box, []):
            v = e['payload']
            if 'gross_kg' in v:
                gross[box] = _dec(v['gross_kg'])
            if 'tare_kg' in v:
                tare[box] = _dec(v['tare_kg'])
        if box in gross:
            nets[box] = gross[box] - tare.get(box, Decimal('0'))
    return gross, tare, nets


def _resolve_gross(box, gross, split_children):
    """Gross of a box, or the sum of its split children's grosses when it was
    never weighed itself (intermediate contents flowing straight into a merge).
    Only split edges expand a box; merge edges join content and are never used
    here, so a box entering a merge cannot be counted twice."""
    if box in gross:
        return gross[box]
    children = split_children.get(box)
    if not children or any(child not in gross for child in children):
        return None
    return sum((gross[child] for child in children), Decimal('0'))


def _check_conservation(scope, lineage_events, gross, edges, root):
    """Gross weight must be conserved at every split and merge node.

    Packaging travels with the goods, so measured gross is additive: a split's
    children grosses must equal the parent gross, a merge's result gross must
    equal its parents' grosses. Any gap is a shortage that cannot be silently
    apportioned away, so the bill is blocked. Unweighed intermediate boxes are
    resolved from the measured grosses of their direct split children.
    """
    problems = []
    split_children = {}
    for e in edges:
        if e['kind'] == 'split' and e['parent'] in scope and e['child'] in scope:
            split_children.setdefault(e['parent'], set()).add(e['child'])
    for e in lineage_events:
        v = e['payload']
        if e['kind'] == 'split':
            parent, parts = e['subject'], v.get('children', [])
        else:
            parent, parts = None, v.get('parents', [])
            if e['subject'] not in scope:
                continue
        involved = {p['id'] for p in parts} | ({parent} if parent else set()) | ({e['subject']} if e['kind'] == 'merge' else set())
        if not involved <= scope:
            continue
        if e['kind'] == 'split':
            child_values = [_resolve_gross(p['id'], gross, split_children) for p in parts]
            if parent not in gross or any(v2 is None for v2 in child_values):
                continue
            total = sum(child_values, Decimal('0'))
            if abs(total - gross[parent]) > EPS:
                problems.append({'code': 'weight_not_conserved', 'box': parent, 'event_id': e['id'],
                                 'detail': {'children_gross': str(total), 'parent_gross': str(gross[parent])}})
        else:
            parent_values = [_resolve_gross(p['id'], gross, split_children) for p in parts]
            child = e['subject']
            if child not in gross or any(v2 is None for v2 in parent_values):
                continue
            total = sum(parent_values, Decimal('0'))
            if abs(total - gross[child]) > EPS:
                problems.append({'code': 'weight_not_conserved', 'box': child, 'event_id': e['id'],
                                 'detail': {'parents_gross': str(total), 'child_gross': str(gross[child])}})
    return problems


def settle_order(db, run_id, order, config, all_events, lineage_events):
    """Build the billing-weight settlement for one order's lineage graph."""
    root = order['box']
    edges = build_lineage_edges(lineage_events)
    scope = reachable_boxes(edges, root)
    leaves = leaf_boxes(edges, root)
    weights = defaultdict(list)
    for e in all_events:
        if e['kind'] == 'weight' and e['subject'] in scope:
            weights[e['subject']].append(e)
    record = {'status': 'blocked', 'rounding_mode': order.get('rounding_mode'),
              'billing_unit_kg': order.get('billing_unit_kg') and str(order['billing_unit_kg']),
              'rate_per_kg': order.get('rate_per_kg') and str(order['rate_per_kg']),
              'issues': [], 'lines': []}
    problems = []

    # --- frozen contract must explicitly choose one rounding convention ---
    unit = order.get('billing_unit_kg')
    mode = order.get('rounding_mode')
    if unit is None:
        return None  # order is not under weight-billing settlement
    try:
        unit_d = _dec(unit)
        if unit_d <= 0:
            raise InvalidOperation
    except (InvalidOperation, TypeError):
        unit_d = None
    if unit_d is None or mode not in ROUNDING_MODES:
        problems.append({'code': 'missing_rounding_config', 'box': root,
                         'detail': {'rounding_mode': mode, 'billing_unit_kg': unit}})

    # --- structural lineage integrity: cycles and repeated references ---
    for box in find_cycles(edges):
        if box in scope:
            problems.append({'code': 'lineage_cycle', 'box': box})
    for hit in duplicate_parts(edges):
        if hit['box'] in scope:
            problems.append({'code': 'duplicate_lineage_reference', 'box': hit['box'],
                             'event_id': hit['event_id'], 'detail': {'reason': 'part_repeated_in_event'}})
    for hit in divergent_references(edges):
        if hit['box'] in scope:
            problems.append({'code': 'duplicate_lineage_reference', 'box': hit['box'],
                             'detail': {'reason': 'box_used_in_multiple_events', **hit}})

    # --- weighing evidence: one weighing per box, valid calibration ---
    calibrations = config.get('calibrations', [])
    for box, evs in weights.items():
        if len(evs) > 1:
            problems.append({'code': 'duplicate_weighing', 'box': box,
                             'detail': {'event_ids': [x['id'] for x in evs]}})
        for e in evs:
            version, ok = _calibration_status(e['payload'], timestamp(e['time']), calibrations)
            if not ok:
                problems.append({'code': 'calibration_expired', 'box': box, 'event_id': e['id'],
                                 'detail': {'calibration_version': version}})

    gross, tare, nets = _measured_nets(scope, weights)

    for leaf in leaves:
        if leaf not in gross:
            problems.append({'code': 'missing_leaf_gross', 'box': leaf})
    internal = scope - set(leaves)
    for box in internal:
        if box in weights and box not in tare:
            problems.append({'code': 'missing_parent_tare', 'box': box,
                             'detail': {'note': 'weighed box without a tare measurement'}})
    for box, net in nets.items():
        if net <= 0:
            problems.append({'code': 'negative_net_weight', 'box': box,
                             'detail': {'measured_net_kg': str(net)}})

    problems.extend(_check_conservation(scope, lineage_events, gross, edges, root))

    codes = {p['code'] for p in problems}
    for p in problems:
        db.execute('INSERT INTO issue(code,detail,event_id) VALUES (?,?,?)',
                   (p['code'], json.dumps({'subject': root, **p}, ensure_ascii=False, default=str), p.get('event_id')))
    record['issues'] = sorted(codes)
    if codes:
        save_settlement(db, run_id, root, record)
        return record

    # --- tare estimation and leaf-level settleable weights ---
    shares, pools = apportion_tare(edges, tare, root)
    lines = []
    for leaf in leaves:
        weigh = weights[leaf][0]
        settleable = gross[leaf] - shares[leaf]
        if settleable <= 0:
            problems.append({'code': 'negative_net_weight', 'box': leaf,
                             'detail': {'settleable_kg': str(settleable)}})
            continue
        if mode == 'per_leaf':
            billable = ceiling_round(settleable, unit_d)
        else:
            billable = settleable
        source_line = db.execute('SELECT line_no FROM event WHERE id=?', (weigh['id'],)).fetchone()['line_no']
        lines.append({'leaf_box': leaf, 'gross_kg': gross[leaf], 'tare_share_kg': shares[leaf],
                      'settleable_kg': settleable, 'billable_kg': billable,
                      'weight_event_id': weigh['id'], 'source_line_no': source_line,
                      'calibration': weigh['payload'].get('calibration_version'),
                      'allocation': {'tare_pool_kg': {b: str(v) for b, v in pools.items()},
                                     'parent_tare_kg': {b: str(v) for b, v in tare.items()},
                                     'leaf_gross_kg': str(gross[leaf])}})

    if problems:
        codes = {p['code'] for p in problems}
        record['issues'] = sorted(codes)
        for p in problems:
            db.execute('INSERT INTO issue(code,detail,event_id) VALUES (?,?,?)',
                       (p['code'], json.dumps({'subject': root, **p}, ensure_ascii=False, default=str), p.get('event_id')))
        save_settlement(db, run_id, root, record)
        return record

    raw_total = sum((ln['settleable_kg'] for ln in lines), Decimal('0'))
    # roll-up conservation: leaf settleable weights must equal the root gross
    # minus every packaging tare observed along the lineage chain
    chain_tare = sum(tare.values(), Decimal('0'))
    root_settleable = gross[root] - chain_tare if root in gross else None
    if root_settleable is not None and abs(raw_total - root_settleable) > EPS:
        db.execute('INSERT INTO issue(code,detail) VALUES (?,?)',
                   ('weight_not_conserved', json.dumps({'subject': root, 'box': root,
                                                        'detail': {'leaf_settleable': str(raw_total),
                                                                   'root_gross': str(gross[root]),
                                                                   'chain_tare': str(chain_tare),
                                                                   'root_settleable': str(root_settleable)}}, ensure_ascii=False)))
        record['issues'] = ['weight_not_conserved']
        save_settlement(db, run_id, root, record)
        return record

    if mode == 'per_leaf':
        billable_total = sum((ln['billable_kg'] for ln in lines), Decimal('0'))
    else:
        billable_total = ceiling_round(raw_total, unit_d)
        for ln in lines:
            ln['billable_kg'] = ln['settleable_kg']
    rate = _dec(order.get('rate_per_kg', 0))
    record.update(status='settled', raw_settleable_kg=str(raw_total),
                  billable_kg=str(billable_total), amount=str(billable_total * rate),
                  lines=lines)
    save_settlement(db, run_id, root, record)
    return record


def evaluate(db_path):
    db=connect(db_path);config=json.loads(db.execute("SELECT value FROM meta WHERE key='config_json'").fetchone()['value'])
    orders={x['box']:x for x in config['boxes']};by=defaultdict(list)
    all_events=[]
    for row in db.execute('SELECT * FROM event ORDER BY time,id'):
        e=dict(row); e['payload']=json.loads(e['payload'])
        by[row['subject']].append(e); all_events.append(e)
    lineage_events=[e for e in all_events if e['kind'] in ('split','merge')]
    results={}
    with db:
        run_id=db.execute('INSERT INTO run(started_at,status) VALUES (?,?)',(datetime.now(timezone.utc).isoformat(),'running')).lastrowid
        db.execute('DELETE FROM evidence')
        db.execute('DELETE FROM decision');db.execute('DELETE FROM lineage');db.execute("DELETE FROM issue WHERE code NOT IN ('identity_conflict','parse_error')")
        db.execute('DELETE FROM settlement_line');db.execute('DELETE FROM settlement')
        # lineage is global: split/merge events on sub-boxes belong to no appointment
        for edge in build_lineage_edges(lineage_events):
            db.execute('INSERT OR IGNORE INTO lineage VALUES (?,?,?,?,?)',
                       (edge['parent'],edge['child'],edge['quantity'],edge['kind'],edge['event_id']))
        lineage_boxes=set()
        for e in lineage_events:
            parts=e['payload'].get('children',[]) if e['kind']=='split' else e['payload'].get('parents',[])
            lineage_boxes.add(e['subject']);lineage_boxes.update(str(p['id']) for p in parts)
        settlement_records={}
        for box,order in orders.items():
            events=by.pop(box,[]); problems=[];stage=0;received=0;weight=None
            for e in events:
                v=e['payload'];kind=e['kind'];time=timestamp(e['time'])
                if v.get('dock')!=order['dock'] or not timestamp(order['window_start'])<=time<=timestamp(order['window_end']):problems.append('wrong_dock_or_window')
                if kind in ('gate','unload','check','exit'):
                    stage,error=advance(stage,kind)
                    if error:problems.append(error)
                if kind=='unload':received+=int(v.get('quantity',0))
                if kind=='weight' and 'net_kg' in v:weight=float(v['net_kg'])
                if kind in ('split','merge'):
                    parts=v.get('children',[]) if kind=='split' else v.get('parents',[])
                    source=int(v.get('quantity',0))
                    if sum(int(c['quantity']) for c in parts)!=source:problems.append('quantity_not_conserved')
                    elif not valid_lineage(box,parts,source):problems.append('invalid_lineage')
            if stage<4:problems.append('missing_stage')
            if received<order['quantity']:problems.append('shortage')
            if received>order['quantity']:problems.append('overage')
            if weight is None:problems.append('missing_weight')
            elif abs(weight-order['expected_kg'])>order['tolerance_kg']:problems.append('weight_mismatch')
            if db.execute("SELECT 1 FROM issue WHERE code='identity_conflict'").fetchone():problems.append('identity_conflict')
            record=settle_order(db,run_id,order,config,all_events,lineage_events)
            if record is not None:
                settlement_records[box]=record
                if record['status']=='blocked':
                    problems.extend(c for c in record['issues'] if c in SETTLEMENT_BLOCKERS)
            status='blocked' if any(p in problems for p in ('illegal_transition','wrong_dock_or_window','quantity_not_conserved','invalid_lineage','weight_mismatch') + SETTLEMENT_BLOCKERS) else ('pending' if problems else 'accepted')
            detail=json.dumps({'expected':order['quantity'],'received':received,'weight':weight,'issues':sorted(set(problems)),'evidence':[e['id'] for e in events]},ensure_ascii=False)
            db.execute('INSERT INTO decision VALUES (?,?,?,?,?)',(box,status,detail,VERSION,run_id))
            for e in events:db.execute('INSERT INTO evidence VALUES (?,?,?,?)',(box,e['id'],VERSION,run_id))
            for code in sorted(set(problems)):
                if code in SETTLEMENT_BLOCKERS:continue  # already recorded with full detail by settle_order
                db.execute('INSERT INTO issue(code,detail) VALUES (?,?)',(code,box))
            results[box]=status
        for box in by:
            if not known_box(box,orders) and box not in lineage_boxes:db.execute('INSERT INTO issue(code,detail) VALUES (?,?)',('unknown_box',box))
        db.execute('UPDATE run SET completed_at=?,status=? WHERE id=?',(datetime.now(timezone.utc).isoformat(),'done',run_id))
    db.close();return results
