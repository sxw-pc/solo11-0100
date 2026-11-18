"""Freeze authorized reverse parcels and replay reverse-logistics dispositions.

The forward evidence (raw scans in ``source``/``event``, the split/merge
lineage and the accepted/pending/blocked decision) is immutable.  This module
reads that evidence live, traces every returned serial back to its inbound
receipt and split child, and appends versioned inventory releases, quaran-
tines, repair orders and refund obligations.  Re-running with the same frozen
inputs is a no-op; new parcels or corrections create a new version whose
ledger rows supplement or reverse the previous ones instead of overwriting
them.
"""
import hashlib
import json
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path
from .store import connect
from .ingest import timestamp

RETURN_RULE_VERSION = '1'
CONTRACTS = ('oldest_complete_rma_first', 'maximum_complete_customers')

# Hard quarantine reason codes: the physical unit must be isolated and can
# never be resold or refunded on the word of the parcel paperwork alone.
HARD_REASONS = frozenset((
    'origin_not_accepted',
    'lineage_unit_untraced',
    'duplicate_lineage_attribution',
    'wrong_child_box',
    'unit_not_in_authorized_rma',
    'unit_claimed_by_multiple_rmas',
    'rma_deadline_passed',
    'unknown_unit_value',
    'opened_requires_authorized_repair',
    'inspection_conflict',
))

AMENDMENT_KINDS = ('add_parcel', 'correct_inspection', 'correct_parcel')


def _sha256_bytes(raw):
    return hashlib.sha256(raw).hexdigest()


def _canon(obj):
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def register(db_path, case_id, fixture):
    source = Path(fixture).resolve()
    raw = source.read_bytes()
    doc = json.loads(raw)
    if not case_id or not doc.get('rmas') or not doc.get('parcels') or not doc.get('contracts'):
        raise ValueError('bad_return_case')
    db = connect(db_path)
    try:
        with db:
            bid = doc['original_box']
            decision = db.execute('SELECT * FROM decision WHERE subject=?', (bid,)).fetchone()
            if decision is None or decision['status'] != 'accepted':
                raise ValueError('return_origin_not_accepted')
            events = [dict(row) for row in db.execute(
                'SELECT e.id,e.kind,e.time,e.sha256,s.sha256 AS source_sha256 '
                'FROM event e JOIN source s ON s.id=e.source_id '
                'WHERE e.subject=? ORDER BY e.time,e.id', (bid,))]
            lineage = [dict(row) for row in db.execute(
                'SELECT * FROM lineage WHERE parent_box=? ORDER BY child_box', (bid,))]
            payload = json.dumps({'fixture': str(source), 'digest': hashlib.sha256(raw).hexdigest(),
                                  'doc': doc, 'decision': dict(decision), 'events': events,
                                  'lineage': lineage}, sort_keys=True, separators=(',', ':'))
            old = db.execute('SELECT payload FROM return_case WHERE id=?', (case_id,)).fetchone()
            if old and old['payload'] != payload:
                raise ValueError('return_case_conflict')
            db.execute('INSERT OR IGNORE INTO return_case VALUES (?,?)', (case_id, payload))
    finally:
        db.close()
    return {'case_id': case_id, 'parcels': len(doc['parcels']),
            'rmas': len(doc['rmas']), 'lineage_edges': len(lineage)}


def _validate_amendment(kind, doc):
    if kind == 'add_parcel':
        p = doc.get('parcel')
        if not isinstance(p, dict) or not p.get('id') or not p.get('source_child_box') \
                or not p.get('received_at') or not isinstance(p.get('inspections'), list) \
                or not p['inspections']:
            raise ValueError('bad_amendment_parcel')
    elif kind == 'correct_inspection':
        if not doc.get('parcel_id') or not doc.get('unit') or not doc.get('condition'):
            raise ValueError('bad_amendment_correction')
    elif kind == 'correct_parcel':
        if not doc.get('parcel_id') or not doc.get('source_child_box'):
            raise ValueError('bad_amendment_correction')


def add_amendment(db_path, case_id, fixture):
    """Freeze a later parcel or a correction against a registered case."""
    source = Path(fixture).resolve()
    raw = source.read_bytes()
    doc = json.loads(raw)
    kind = doc.get('kind')
    if kind not in AMENDMENT_KINDS:
        raise ValueError('unknown_amendment_kind')
    _validate_amendment(kind, doc)
    db = connect(db_path)
    try:
        with db:
            if db.execute('SELECT 1 FROM return_case WHERE id=?', (case_id,)).fetchone() is None:
                raise ValueError('return_case_unknown')
            digest = _sha256_bytes(raw)
            dup = db.execute(
                'SELECT seq FROM return_amendment WHERE case_id=? AND sha256=?',
                (case_id, digest)).fetchone()
            if dup:
                return {'case_id': case_id, 'seq': dup['seq'], 'kind': kind,
                        'duplicate_file': True}
            seq = db.execute(
                'SELECT COALESCE(MAX(seq),0)+1 AS next_seq FROM return_amendment WHERE case_id=?',
                (case_id,)).fetchone()['next_seq']
            db.execute('INSERT INTO return_amendment(case_id,seq,kind,fixture_path,sha256,doc,created_at)'
                       ' VALUES (?,?,?,?,?,?,?)',
                       (case_id, seq, kind, str(source), digest, _canon(doc),
                        datetime.now(timezone.utc).isoformat()))
    finally:
        db.close()
    return {'case_id': case_id, 'seq': seq, 'kind': kind, 'duplicate_file': False}


def _load_case(db, case_id):
    row = db.execute('SELECT payload FROM return_case WHERE id=?', (case_id,)).fetchone()
    if row is None:
        raise ValueError('return_case_unknown')
    return json.loads(row['payload'])


def _load_amendments(db, case_id):
    return [dict(r) for r in db.execute(
            'SELECT * FROM return_amendment WHERE case_id=? ORDER BY seq', (case_id,))]


def _assemble_parcels(case_id, doc, amendments):
    """Replay base parcels plus frozen amendments into annotated parcel records."""
    parcels, by_id = [], {}
    for p in doc['parcels']:
        rec = {k: v for k, v in p.items() if k != 'inspections'}
        rec['_inspections'] = [
            dict(row, _locator=f"return_case:{case_id}#parcel:{p['id']}/scan/{idx}",
                 _source='case', _corrected=False)
            for idx, row in enumerate(p['inspections'])]
        parcels.append(rec)
        by_id[rec['id']] = rec
    for am in amendments:
        d = json.loads(am['doc'])
        seq, kind = am['seq'], am['kind']
        am_locator = f"amendment:{seq}"
        if kind == 'add_parcel':
            p = d['parcel']
            if p['id'] in by_id:
                raise ValueError('amendment_parcel_id_conflict')
            rec = {k: v for k, v in p.items() if k != 'inspections'}
            rec['_inspections'] = [
                dict(row, _locator=f"{am_locator}#parcel:{p['id']}/scan/{idx}",
                     _source=seq, _corrected=False)
                for idx, row in enumerate(p['inspections'])]
            parcels.append(rec)
            by_id[rec['id']] = rec
        elif kind == 'correct_parcel':
            rec = by_id.get(d['parcel_id'])
            if rec is None:
                raise ValueError('amendment_target_missing')
            rec['source_child_box'] = d['source_child_box']
            rec['_box_correction'] = am_locator
        else:  # correct_inspection
            rec = by_id.get(d['parcel_id'])
            if rec is None:
                raise ValueError('amendment_target_missing')
            idx = d.get('scan')
            if idx is None:
                hits = [i for i, r in enumerate(rec['_inspections']) if r.get('unit') == d['unit']]
                if not hits:
                    raise ValueError('amendment_target_missing')
                idx = hits[0]
            try:
                target = rec['_inspections'][idx]
            except IndexError:
                raise ValueError('amendment_target_missing')
            if target.get('unit') != d['unit']:
                raise ValueError('amendment_target_missing')
            for key in ('condition', 'seal', 'repair_cost'):
                if key in d:
                    target[key] = d[key]
            target['_corrected'] = True
            target['_correction'] = f"{am_locator}#inspection:{d['parcel_id']}/{d['unit']}"
    return parcels


def _lineage_owners(db):
    """Map each serial to the split/merge child boxes claiming it (raw events)."""
    owners, split_events = {}, []
    for row in db.execute("SELECT * FROM event WHERE kind IN ('split','merge') ORDER BY time,id"):
        payload = json.loads(row['payload'])
        split_events.append({'id': row['id'], 'kind': row['kind'], 'sha256': row['sha256'],
                             'payload': payload})
        parts = payload.get('children', []) if row['kind'] == 'split' else payload.get('parents', [])
        for part in parts:
            for unit in part.get('units', []):
                owners.setdefault(unit, set()).add(part['id'])
    return owners, split_events


def _plan_oldest_first(rmas, slots, repair_need):
    """Contract 1: satisfy complete RMAs in longest-waiting order."""
    selected, assigned, remaining = [], [], slots
    for rma in sorted(rmas, key=lambda r: (-int(r['waiting_days']), r['id'])):
        if not rma['_completable']:
            continue
        need = repair_need[rma['id']]
        if need <= remaining:
            selected.append(rma)
            remaining -= need
            for unit in rma['_repair_units']:
                assigned.append(unit)
    return selected, assigned


def _plan_max_customers(rmas, slots, repair_need):
    """Contract 2: maximize distinct customers with complete, in-deadline RMAs."""
    feasible = [r for r in rmas if r['_completable']]
    best = None
    for size in range(len(feasible), -1, -1):
        for combo in combinations(feasible, size):
            used = sum(repair_need[r['id']] for r in combo)
            if used > slots:
                continue
            customers = len({r['customer'] for r in combo})
            ids = tuple(sorted(r['id'] for r in combo))
            candidate = (customers, size, -used, ids)
            if best is None or candidate > best[0]:
                best = (candidate, combo, used)
    selected = list(best[1])
    assigned = []
    for rma in sorted(selected, key=lambda r: r['id']):
        assigned.extend(rma['_repair_units'])
    return selected, assigned


PLANNERS = {
    'oldest_complete_rma_first': _plan_oldest_first,
    'maximum_complete_customers': _plan_max_customers,
}


def process(db_path, case_id, contract):
    """Replay the reverse case under one disposition contract; append a version."""
    if contract not in CONTRACTS:
        raise ValueError('unknown_contract')
    db = connect(db_path)
    try:
        frozen = _load_case(db, case_id)
        doc = frozen['doc']
        if contract not in doc.get('contracts', []):
            raise ValueError('contract_not_authorized_for_case')
        amendments = _load_amendments(db, case_id)
        origin_box = doc['original_box']
        decision = db.execute('SELECT * FROM decision WHERE subject=?', (origin_box,)).fetchone()
        lineage = [dict(r) for r in db.execute(
            'SELECT * FROM lineage ORDER BY parent_box,child_box,event_id')]
        owners, split_events = _lineage_owners(db)

        digest_obj = {
            'case_digest': frozen['digest'],
            'amendments': [{'seq': a['seq'], 'kind': a['kind'], 'sha256': a['sha256'],
                            'doc': json.loads(a['doc'])} for a in amendments],
            'decision': None if decision is None else dict(decision),
            'lineage': lineage,
            'split_events': split_events,
            'rule_version': RETURN_RULE_VERSION,
        }
        input_digest = _sha256_bytes(_canon(digest_obj).encode())
        existing = db.execute(
            'SELECT id,summary FROM return_run WHERE case_id=? AND input_digest=? AND contract=?',
            (case_id, input_digest, contract)).fetchone()
        if existing is not None:
            summary = json.loads(existing['summary'])
            summary['idempotent'] = True
            return summary

        parcels = _assemble_parcels(case_id, doc, amendments)
        rmas = [dict(r) for r in doc['rmas']]
        rma_by_id = {r['id']: r for r in rmas}
        claims = {}
        for rma in rmas:
            for unit in rma['units']:
                claims.setdefault(unit, []).append(rma['id'])
        unit_values = doc.get('unit_values', {})
        slot_count = int(doc['repair']['slot_count'])

        # ---- serial-level scan reconciliation (duplicate scans count once) ----
        primary, scan_evidence, duplicate_scans = {}, [], 0
        for parcel in parcels:
            for idx, row in enumerate(parcel['_inspections']):
                unit = row.get('unit')
                ev = [row['_locator']]
                if row.get('_correction'):
                    ev.append(row['_correction'])
                scan_evidence.append((unit, parcel, row, ev))
                if unit in primary:
                    duplicate_scans += 1
                    continue
                primary[unit] = (parcel, row, ev)

        issues = []

        def add_issue(scope, ref, code, detail):
            issues.append({'scope': scope, 'ref': ref, 'code': code, 'detail': _canon(detail)})

        for unit, parcel, row, ev in scan_evidence:
            if unit in primary and primary[unit][2] != ev:
                add_issue('unit', unit, 'duplicate_scan',
                          {'parcel': parcel['id'], 'scan': row['_locator'],
                           'primary': primary[unit][0]['id']})

        # ---- per-serial eligibility against inbound evidence ----
        traces = {}
        for unit, (parcel, row, ev) in sorted(primary.items()):
            reasons, evidence = [], list(ev)
            rma_ids = claims.get(unit, [])
            rma_id = rma_ids[0] if len(rma_ids) == 1 else None
            customer = rma_by_id[rma_id]['customer'] if rma_id else None

            if decision is None or decision['status'] != 'accepted':
                reasons.append('origin_not_accepted')
                evidence.append(f"decision:{origin_box}")
            box_owners = owners.get(unit, set())
            if not box_owners:
                reasons.append('lineage_unit_untraced')
                evidence.extend(f"event:{e['id']}" for e in split_events)
            elif len(box_owners) > 1:
                reasons.append('duplicate_lineage_attribution')
                evidence.extend(f"event:{e['id']}" for e in split_events)
            child_box = next(iter(box_owners)) if len(box_owners) == 1 else None
            if child_box is not None:
                evidence.append(f"lineage:{origin_box}->{child_box}")
                if parcel['source_child_box'] != child_box:
                    reasons.append('wrong_child_box')
                    add_issue('unit', unit, 'wrong_child_box',
                              {'parcel': parcel['id'], 'parcel_box': parcel['source_child_box'],
                               'lineage_box': child_box})
            if not rma_ids:
                reasons.append('unit_not_in_authorized_rma')
            elif len(rma_ids) > 1:
                reasons.append('unit_claimed_by_multiple_rmas')
            if rma_id is not None:
                evidence.append(f"return_case:{case_id}#rma:{rma_id}")
                deadline = timestamp(rma_by_id[rma_id]['deadline'])
                if timestamp(parcel['received_at']) > deadline:
                    reasons.append('rma_deadline_passed')
            if unit not in unit_values:
                reasons.append('unknown_unit_value')

            condition = row.get('condition')
            needs_repair = False
            if condition == 'sealed' and row.get('seal') == 'intact' and 'repair_cost' not in row:
                physical = 'resellable'
            elif condition == 'sealed' and 'repair_cost' in row:
                reasons.append('inspection_conflict')
                physical = 'quarantined'
            elif condition == 'sealed':  # seal broken: treat as opened, needs repair
                needs_repair = True
                physical = 'needs_repair'
            elif condition == 'opened':
                cost = row.get('repair_cost')
                if not isinstance(cost, (int, float)) or cost <= 0:
                    reasons.append('opened_requires_authorized_repair')
                    physical = 'quarantined'
                else:
                    needs_repair = True
                    physical = 'needs_repair'
            else:
                reasons.append('inspection_conflict')
                physical = 'quarantined'

            hard = [r for r in reasons if r in HARD_REASONS]
            if hard:
                physical = 'quarantined'
                for code in reasons:
                    add_issue('unit', unit, code, {'parcel': parcel['id']})
            elif needs_repair:
                physical = 'needs_repair'
            traces[unit] = {
                'unit': unit, 'rma_id': rma_id, 'customer': customer,
                'origin_box': origin_box, 'child_box': child_box,
                'parcel_id': parcel['id'], 'scan_index': row['_locator'],
                'condition': condition, 'physical_state': physical,
                'reasons': reasons, 'evidence': evidence,
                'repair_units': needs_repair and not hard,
            }

        # ---- RMA-level completeness (group return: every unit must qualify) ----
        repair_need = {}
        for rma in rmas:
            repair_units, completable = [], True
            for unit in rma['units']:
                if unit not in traces:
                    completable = False
                    add_issue('rma', rma['id'], 'rma_unit_not_returned', {'unit': unit})
                    continue
                tr = traces[unit]
                if tr['physical_state'] == 'quarantined':
                    completable = False
                elif tr['physical_state'] == 'needs_repair':
                    repair_units.append(unit)
            rma['_completable'] = completable
            rma['_repair_units'] = sorted(repair_units)
            repair_need[rma['id']] = len(repair_units)

        # ---- finite, real repair stations under the chosen contract ----
        selected, assigned_units = PLANNERS[contract](rmas, slot_count, repair_need)
        selected_ids = {r['id'] for r in selected}
        station_of_unit = {}
        for station_no, unit in enumerate(assigned_units, start=1):
            station_of_unit[unit] = station_no
        for unit, tr in traces.items():
            if tr['physical_state'] == 'needs_repair':
                if unit in station_of_unit:
                    tr['physical_state'] = 'repaired'
                    tr['station_no'] = station_of_unit[unit]
                    tr['evidence'].append(
                        f"return_run:STATION:{case_id}:{station_of_unit[unit]}")
                else:
                    tr['physical_state'] = 'awaiting_repair'
                    tr['reasons'].append('repair_capacity_exceeded')
                    add_issue('unit', unit, 'repair_capacity_exceeded',
                              {'slots': slot_count, 'contract': contract})

        complete_rmas, refund_by_rma = {}, {}
        for rma in rmas:
            complete = rma['id'] in selected_ids
            complete_rmas[rma['id']] = complete
            refund_by_rma[rma['id']] = (
                sum(int(unit_values[u]) for u in rma['units']) if complete else 0)

        now = datetime.now(timezone.utc).isoformat()
        with db:
            run_id = db.execute(
                'INSERT INTO return_run(case_id,contract,input_digest,rule_version,started_at,'
                'completed_at,status,summary) VALUES (?,?,?,?,?,?,?,?)',
                (case_id, contract, input_digest, RETURN_RULE_VERSION, now, now,
                 'running', '{}')).lastrowid
            for station_no in range(1, slot_count + 1):
                db.execute('INSERT OR IGNORE INTO repair_station(case_id,station_no,created_run)'
                           ' VALUES (?,?,?)', (case_id, station_no, run_id))
            for unit, station_no in sorted(station_of_unit.items(), key=lambda kv: kv[1]):
                db.execute('INSERT INTO repair_order(run_id,unit,station_no,evidence) VALUES (?,?,?,?)',
                           (run_id, unit, station_no,
                            _canon(traces[unit]['evidence'])))
            for unit, tr in sorted(traces.items()):
                db.execute('INSERT INTO unit_trace(run_id,unit,rma_id,customer,origin_box,child_box,'
                           'parcel_id,scan_index,condition,physical_state,reasons,evidence)'
                           ' VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                           (run_id, unit, tr['rma_id'], tr['customer'], tr['origin_box'],
                            tr['child_box'], tr['parcel_id'], tr['scan_index'], tr['condition'],
                            tr['physical_state'], _canon(tr['reasons']), _canon(tr['evidence'])))
            for issue in issues:
                db.execute('INSERT INTO return_issue(run_id,scope,ref,code,detail) VALUES (?,?,?,?,?)',
                           (run_id, issue['scope'], issue['ref'], issue['code'], issue['detail']))

            # ---- versioned inventory ledger: only deltas append; old rows survive ----
            # The previous entry may live in any older version, not just the last run,
            # because unchanged units do not get a row on every replay.  The chain is
            # scoped to one disposition contract: a different contract is a separate
            # decision lineage and must never be linked as a prior version.
            prior_action = {}
            for r in db.execute(
                    'SELECT l.id,l.unit,l.action FROM inventory_ledger l JOIN return_run r '
                    'ON r.id=l.run_id WHERE r.case_id=? AND r.contract=? AND l.id=('
                    'SELECT MAX(l2.id) FROM inventory_ledger l2 JOIN return_run r2 '
                    'ON r2.id=l2.run_id WHERE r2.case_id=r.case_id AND r2.contract=r.contract '
                    'AND l2.unit=l.unit)',
                    (case_id, contract)):
                prior_action[r['unit']] = (r['id'], r['action'])
            for unit, tr in sorted(traces.items()):
                action = {'quarantined': 'quarantine', 'awaiting_repair': 'hold_repair'}.get(
                    tr['physical_state'], 'release')
                prev = prior_action.get(unit)
                if prev is None or prev[1] != action:
                    db.execute('INSERT INTO inventory_ledger(run_id,case_id,unit,action,prior_id,'
                               'reasons,evidence,created_at) VALUES (?,?,?,?,?,?,?,?)',
                               (run_id, case_id, unit, action,
                                None if prev is None else prev[0],
                                _canon(tr['reasons']), _canon(tr['evidence']), now))

            # ---- versioned refund ledger: supplements/reversals, never overwrite ----
            prior_settled = {}
            for r in db.execute(
                    'SELECT l.id,l.rma_id,l.settled_after FROM refund_ledger l JOIN return_run r '
                    'ON r.id=l.run_id WHERE r.case_id=? AND r.contract=? AND l.id=('
                    'SELECT MAX(l2.id) FROM refund_ledger l2 JOIN return_run r2 '
                    'ON r2.id=l2.run_id WHERE r2.case_id=r.case_id AND r2.contract=r.contract '
                    'AND l2.rma_id=l.rma_id)',
                    (case_id, contract)):
                prior_settled[r['rma_id']] = (r['id'], r['settled_after'])
            for rma in rmas:
                amount = refund_by_rma[rma['id']]
                prev = prior_settled.get(rma['id'])
                old_amount = 0 if prev is None else prev[1]
                if amount == old_amount:
                    continue
                diff = amount - old_amount
                movement = 'refund' if diff > 0 else 'reversal'
                evidence = [f"return_case:{case_id}#rma:{rma['id']}"]
                for unit in rma['units']:
                    if unit in traces:
                        evidence.extend(traces[unit]['evidence'])
                db.execute('INSERT INTO refund_ledger(run_id,case_id,rma_id,customer,movement,'
                           'amount,settled_after,prior_id,reasons,evidence,created_at)'
                           ' VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                           (run_id, case_id, rma['id'], rma['customer'], movement, abs(diff),
                            amount, None if prev is None else prev[0],
                            _canon({'complete': complete_rmas[rma['id']], 'contract': contract,
                                    'group_return': True}),
                            _canon(evidence), now))

            resellable = sorted(u for u, t in traces.items()
                                if t['physical_state'] in ('resellable', 'repaired'))
            quarantined = {u: t['reasons'] for u, t in sorted(traces.items())
                           if t['physical_state'] == 'quarantined'}
            awaiting = sorted(u for u, t in traces.items()
                              if t['physical_state'] == 'awaiting_repair')
            summary = {
                'case_id': case_id,
                'run_id': run_id,
                'contract': contract,
                'rule_version': RETURN_RULE_VERSION,
                'input_digest': input_digest,
                'repair_slots': slot_count,
                'repair_complete_by': doc['repair']['complete_by'],
                'stations_used': len(station_of_unit),
                'resellable_units': resellable,
                'quarantined_units': quarantined,
                'awaiting_repair_units': awaiting,
                'duplicate_scans': duplicate_scans,
                'rmas': [
                    {'rma_id': r['id'], 'customer': r['customer'],
                     'complete': complete_rmas[r['id']],
                     'refund_amount': refund_by_rma[r['id']],
                     'units': [{'unit': u,
                                'state': traces[u]['physical_state'] if u in traces else 'not_returned'}
                               for u in r['units']]}
                    for r in rmas],
                'refund_total': sum(refund_by_rma.values()),
                'issue_count': len(issues),
            }
            db.execute('UPDATE return_run SET status=?,completed_at=?,summary=? WHERE id=?',
                       ('done', datetime.now(timezone.utc).isoformat(),
                        json.dumps(summary, ensure_ascii=False, sort_keys=True), run_id))
    finally:
        db.close()
    return summary
