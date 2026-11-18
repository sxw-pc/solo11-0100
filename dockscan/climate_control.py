"""Freeze real dock identities, local sensors, and actuator contract inputs.

The :func:`register` entry point freezes the climate case exactly as before.
On top of the frozen case this module adds publishable cooling commands:

* :func:`plan` validates the case, solves the exact global optimum per
  authorized objective and persists executable commands, per-period
  trajectories, exposure, energy and optimality evidence.
* :func:`apply_feedback` records late real sensor readings.  Executed commands
  are never rewritten; only the remaining periods are re-optimized from the
  observed starting temperatures and a new plan version is issued.

Invalid inputs hold the whole recommendation: no partial, un-executable
commands are ever published.
"""
import csv
import hashlib
import io
import json
from datetime import datetime, timezone
from pathlib import Path
from .store import connect
from . import climate_opt

SEED_SOURCE = 'seed:climate-sensors.csv'
VALID_OBJECTIVES = ('min_energy', 'min_max_dock_exposure')


def register(db_path, case_id, fixture):
    source = Path(fixture).resolve()
    raw = source.read_bytes()
    doc = json.loads(raw)
    if not case_id or not doc.get('docks') or not doc.get('periods') or not doc.get('contracts'):
        raise ValueError('bad_climate_case')
    sensor_path = (source.parent / doc['sensor_file']).resolve()
    if sensor_path.parent != source.parent:
        raise ValueError('climate_sensor_path')
    sensor_raw = sensor_path.read_bytes()
    sensors = list(csv.DictReader(io.StringIO(sensor_raw.decode('utf-8-sig'))))
    db = connect(db_path)
    try:
        with db:
            row = db.execute("SELECT value FROM meta WHERE key='config_json'").fetchone()
            if row is None:
                raise ValueError('climate_config_missing')
            config = json.loads(row['value'])
            known = {box['dock'] for box in config['boxes']}
            observed = []
            for dock in doc['docks']:
                if dock not in known or not any(s['dock'] == dock for s in sensors):
                    raise ValueError('climate_dock_unobserved')
                events = [dict(event) for event in db.execute(
                    'SELECT e.id,e.subject,e.kind,e.time,e.sha256,s.sha256 AS source_sha256 '
                    'FROM event e JOIN source s ON s.id=e.source_id '
                    'WHERE json_extract(e.payload,\'$.dock\')=? ORDER BY e.time,e.id', (dock,))]
                if not events:
                    raise ValueError('climate_dock_events_missing')
                observed.append({'dock': dock, 'events': events})
            payload = json.dumps({'fixture': str(source), 'digest': hashlib.sha256(raw).hexdigest(),
                                  'sensor_file': str(sensor_path), 'sensor_sha256': hashlib.sha256(sensor_raw).hexdigest(),
                                  'sensors': sensors, 'doc': doc, 'observed': observed},
                                 sort_keys=True, separators=(',', ':'))
            old = db.execute('SELECT payload FROM climate_case WHERE id=?', (case_id,)).fetchone()
            if old and old['payload'] != payload:
                raise ValueError('climate_case_conflict')
            db.execute('INSERT OR IGNORE INTO climate_case VALUES (?,?)', (case_id, payload))
            _seed_readings(db, case_id, sensors, hashlib.sha256(sensor_raw).hexdigest())
    finally:
        db.close()
    return {'case_id': case_id, 'docks': len(observed), 'periods': len(doc['periods'])}


def _seed_readings(db, case_id, sensors, sensor_sha):
    for row in sensors:
        try:
            dt = climate_opt._parse_ts(row['at'])
            temp_c = float(row['temp_c'])
        except (ValueError, TypeError, KeyError):
            # Unusable rows are still frozen inside climate_case.payload; the
            # planning validation rejects them instead of crashing ingestion.
            continue
        db.execute(
            'INSERT OR IGNORE INTO climate_reading(case_id,dock,at,temp_c,source,sha256) '
            'VALUES (?,?,?,?,?,?)',
            (case_id, row['dock'], climate_opt._iso(dt), temp_c,
             SEED_SOURCE, sensor_sha))


def _load_case(db, case_id):
    row = db.execute('SELECT payload FROM climate_case WHERE id=?', (case_id,)).fetchone()
    if row is None:
        raise ValueError('climate_case_missing')
    frozen = json.loads(row['payload'])
    readings = [dict(r) for r in db.execute(
        'SELECT dock,at,temp_c,source,sha256 FROM climate_reading WHERE case_id=? ORDER BY at,dock',
        (case_id,))]
    return frozen, frozen['doc'], frozen['sensors'], readings


def _case_digest(frozen, readings, objective):
    body = json.dumps({'case_digest': frozen['digest'], 'sensor_sha256': frozen['sensor_sha256'],
                       'readings': readings, 'objective': objective},
                      sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(body.encode('utf-8')).hexdigest()


def _check_simultaneous_requests(doc, reasons):
    capacity = doc.get('shared_chiller_capacity_per_period')
    for period in doc.get('periods', []):
        requested = period.get('cooling_requests')
        if isinstance(requested, list) and isinstance(capacity, int) and len(requested) > capacity:
            reasons.append('chiller_double_booking')


def plan(db_path, case_id, objective):
    """Validate the frozen case and publish one optimal plan version."""
    if objective not in VALID_OBJECTIVES:
        raise ValueError('unknown_objective')
    db = connect(db_path)
    try:
        frozen, doc, sensors, readings = _load_case(db, case_id)
        digest = _case_digest(frozen, readings, objective)
        existing = db.execute(
            'SELECT version,status,payload FROM climate_plan WHERE case_id=? AND objective=? '
            'ORDER BY version DESC LIMIT 1', (case_id, objective)).fetchone()
        if existing is not None:
            stored = json.loads(existing['payload'])
            if stored.get('input_digest') == digest and stored.get('status') == 'published' \
                    and stored.get('executed_periods', 0) == 0:
                return {'case_id': case_id, 'objective': objective,
                        'version': existing['version'], 'status': 'published',
                        'unchanged': True}

        reasons, normalized = climate_opt.validate_case(doc, sensors)
        _check_simultaneous_requests(doc, reasons)
        if reasons:
            return _hold(db, case_id, objective, digest, sorted(set(reasons)),
                         normalized, readings)
        result = climate_opt.solve(normalized, objective)
        if not result['feasible']:
            return _hold(db, case_id, objective, digest, result['reasons'],
                         normalized, readings)
        version = _next_version(db, case_id, objective)
        payload = _plan_payload(case_id, objective, version, normalized, result,
                                readings, frozen, digest, executed=0)
        _persist_plan(db, case_id, objective, version, payload, result,
                      normalized, executed=0)
        return {'case_id': case_id, 'objective': objective, 'version': version,
                'status': 'published', 'unchanged': False,
                'energy_cents': result['energy_cents'],
                'total_exposure': result['total_exposure'],
                'max_dock_exposure': result['max_dock_exposure']}
    finally:
        db.close()


def _hold(db, case_id, objective, digest, reasons, normalized, readings):
    with db:
        db.execute('INSERT INTO climate_hold(case_id,objective,reasons,input_digest,created_at) '
                   'VALUES (?,?,?,?,?)',
                   (case_id, objective, json.dumps(sorted(set(reasons))), digest,
                    datetime.now(timezone.utc).isoformat()))
    return {'case_id': case_id, 'objective': objective, 'status': 'held',
            'reasons': sorted(set(reasons))}


def _next_version(db, case_id, objective):
    row = db.execute('SELECT COALESCE(MAX(version),0)+1 AS v FROM climate_plan '
                     'WHERE case_id=? AND objective=?', (case_id, objective)).fetchone()
    return row['v']


def _plan_payload(case_id, objective, version, normalized, result, readings,
                  frozen, digest, executed, deviations=None, breach=None):
    return {
        'case_id': case_id, 'objective': objective, 'version': version,
        'status': result.get('plan_status', 'published'),
        'input_digest': digest,
        'created_at': datetime.now(timezone.utc).isoformat(),
        'source_summary': {
            'fixture': frozen['fixture'], 'fixture_sha256': frozen['digest'],
            'sensor_file': frozen['sensor_file'], 'sensor_sha256': frozen['sensor_sha256'],
            'readings': [{'dock': r['dock'], 'at': r['at'], 'temp_c': r['temp_c'],
                          'source': r['source']} for r in readings],
            'dock_events': {o['dock']: [e['id'] for e in o['events']]
                            for o in frozen['observed']}},
        'docks': normalized['docks'],
        'limits': normalized['limits'],
        'executed_periods': executed,
        'actions': result['actions'],
        'dock_exposure': result['dock_exposure'],
        'total_exposure': result['total_exposure'],
        'max_dock_exposure': result['max_dock_exposure'],
        'energy_cents': result['energy_cents'],
        'deviations': deviations or [],
        'irrecoverable_breach': breach,
        'evidence': result['evidence'],
    }


def _persist_plan(db, case_id, objective, version, payload, result, normalized,
                  executed, status='published'):
    with db:
        db.execute('INSERT INTO climate_plan(case_id,objective,version,status,payload,input_digest,created_at) '
                   'VALUES (?,?,?,?,?,?,?)',
                   (case_id, objective, version, status, json.dumps(payload, ensure_ascii=False,
                                                                    sort_keys=True),
                    payload['input_digest'], payload['created_at']))
        for t, owner in enumerate(result['actions']):
            db.execute('INSERT INTO climate_command(case_id,objective,version,period,dock,'
                       'energy_cents,status) VALUES (?,?,?,?,?,?,?)',
                       (case_id, objective, version, normalized['periods'][t]['id'], owner,
                        normalized['prices'][t] if owner is not None else 0,
                        'executed' if t < executed else 'pending'))
        for cell in result['trajectories']:
            db.execute('INSERT INTO climate_trajectory(case_id,objective,version,period,dock,'
                       'start_temp,warming,cooling_delta,end_temp,limit_c,exposure,source,'
                       'price_cents,chiller_owner) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (case_id, objective, version, cell['period'], cell['dock'],
                        cell['start_temp'], cell['warming'], cell['cooling_delta'],
                        cell['end_temp'], cell['limit_c'], cell['exposure'], cell['source'],
                        cell['price_cents'], cell['chiller_owner']))


def apply_feedback(db_path, case_id, objective, dock, at, temp_c, source='feedback'):
    """Record a late real reading and re-optimize the unexecuted periods.

    Executed commands stay immutable.  If the incurred exposure already breaks
    the contract cap the new version is marked ``breached`` and explicitly
    reports the amount that cannot be recovered; future negative exposure is
    never invented.
    """
    if objective not in VALID_OBJECTIVES:
        raise ValueError('unknown_objective')
    db = connect(db_path)
    try:
        frozen, doc, sensors, _ = _load_case(db, case_id)
        if dock not in doc['docks']:
            raise ValueError('feedback_unknown_dock')
        dt = climate_opt._parse_ts(at)
        at_iso = climate_opt._iso(dt)
        temp_c = float(temp_c)
        raw = json.dumps({'dock': dock, 'at': at_iso, 'temp_c': temp_c,
                          'source': source}, sort_keys=True).encode('utf-8')
        with db:
            old = db.execute('SELECT temp_c FROM climate_reading WHERE case_id=? AND dock=? AND at=?',
                             (case_id, dock, at_iso)).fetchone()
            if old is not None:
                if abs(old['temp_c'] - temp_c) > 1e-9:
                    raise ValueError('sensor_identity_conflict')
            else:
                db.execute('INSERT INTO climate_reading(case_id,dock,at,temp_c,source,sha256) '
                           'VALUES (?,?,?,?,?,?)',
                           (case_id, dock, at_iso, temp_c, source,
                            hashlib.sha256(raw).hexdigest()))
        frozen, doc, sensors, readings = _load_case(db, case_id)
        digest = _case_digest(frozen, readings, objective)
        reasons, normalized = climate_opt.validate_case(doc, sensors)
        _check_simultaneous_requests(doc, reasons)
        if reasons:
            return _hold(db, case_id, objective, digest, sorted(set(reasons)),
                         normalized, readings)

        latest = db.execute('SELECT version,payload FROM climate_plan WHERE case_id=? AND objective=? '
                            'ORDER BY version DESC LIMIT 1', (case_id, objective)).fetchone()
        if latest is None:
            raise ValueError('plan_missing_feedback_requires_plan')
        prev = json.loads(latest['payload'])
        boundary = _boundary_index(doc, at_iso)  # start index of the next period
        executed = max(prev.get('executed_periods', 0),
                       min(boundary, len(doc['periods'])))
        prefix_actions = prev['actions'][:executed]

        observed = {t: {} for t in range(executed)}
        missing = []
        for t in range(executed):
            boundary_iso = _end_boundary_at(doc, t)
            for d in normalized['docks']:
                match = next((r for r in readings
                              if r['dock'] == d and r['at'] == boundary_iso), None)
                if match is None:
                    missing.append(f'{d}@period{t}')
                else:
                    observed[t][d] = match['temp_c']
        if missing:
            return _hold(db, case_id, objective, digest,
                         ['feedback_boundary_incomplete:' + ','.join(sorted(missing))],
                         normalized, readings)

        prefix_rows, prefix_cum = climate_opt.simulate(
            normalized, normalized['initial'], prefix_actions, observed,
            period_count=executed)
        deviations = []
        for row in prefix_rows:
            for d in normalized['docks']:
                cell = row['docks'][d]
                if cell['source'] == 'observed':
                    deviations.append({
                        'period': row['period'], 'dock': d,
                        'predicted_end_temp': cell['model_end_temp'],
                        'observed_end_temp': cell['end_temp'],
                        'delta': climate_opt._q(cell['end_temp'] - cell['model_end_temp'])})
        incurred_total = climate_opt._q(sum(prefix_cum.values()))
        version = _next_version(db, case_id, objective)

        if incurred_total > normalized['max_total_exposure'] + 1e-9:
            breach = {'incurred_total_exposure': incurred_total,
                      'cap': normalized['max_total_exposure'],
                      'irrecoverable_exposure': climate_opt._q(
                          incurred_total - normalized['max_total_exposure'])}
            result = _prefix_only_result(normalized, prefix_rows, prefix_cum,
                                         prefix_actions, executed)
            payload = _plan_payload(case_id, objective, version, normalized, result,
                                    readings, frozen, digest, executed, deviations, breach)
            _persist_plan(db, case_id, objective, version, payload, result,
                          normalized, executed, status='breached')
            return {'case_id': case_id, 'objective': objective, 'version': version,
                    'status': 'breached', **breach}

        # Feedback at the end of the final period: no suffix remains, close the
        # plan from the fully observed trajectory instead of re-solving.
        if executed >= len(normalized['periods']):
            result = _prefix_only_result(normalized, prefix_rows, prefix_cum,
                                         prefix_actions, executed)
            payload = _plan_payload(case_id, objective, version, normalized, result,
                                    readings, frozen, digest, executed, deviations)
            _persist_plan(db, case_id, objective, version, payload, result,
                          normalized, executed, status='closed')
            return {'case_id': case_id, 'objective': objective, 'version': version,
                    'status': 'closed', 'executed_periods': executed,
                    'energy_cents': result['energy_cents'],
                    'total_exposure': result['total_exposure'],
                    'max_dock_exposure': result['max_dock_exposure'],
                    'deviations': deviations}

        current_temps = {d: observed[executed - 1][d] for d in normalized['docks']}
        budget_left = normalized['max_total_exposure'] - incurred_total
        suffix = climate_opt.solve(normalized, objective, start_temps=current_temps,
                                   horizon_offset=executed,
                                   exposure_used=prefix_cum,
                                   budget_remaining=budget_left,
                                   prefix_actions=prefix_actions)
        if not suffix['feasible']:
            return _hold(db, case_id, objective, digest, ['no_feasible_plan'],
                         normalized, readings)
        merged = _merge_prefix_suffix(normalized, prefix_rows, prefix_actions,
                                      suffix, executed)
        payload = _plan_payload(case_id, objective, version, normalized, merged,
                                readings, frozen, digest, executed, deviations)
        _persist_plan(db, case_id, objective, version, payload, merged,
                      normalized, executed)
        return {'case_id': case_id, 'objective': objective, 'version': version,
                'status': 'published', 'executed_periods': executed,
                'energy_cents': merged['energy_cents'],
                'total_exposure': merged['total_exposure'],
                'max_dock_exposure': merged['max_dock_exposure'],
                'deviations': deviations}
    finally:
        db.close()


def _boundary_index(doc, at_iso):
    # Period starts are boundaries 0..n-1; an optional ``period_end`` timestamp
    # closes the final period (boundary n = end of the whole shift).
    starts = [climate_opt._iso(climate_opt._parse_ts(p['start']))
              for p in doc['periods']]
    for idx, start in enumerate(starts):
        if at_iso == start:
            return idx
    if doc.get('period_end'):
        end_iso = climate_opt._iso(climate_opt._parse_ts(doc['period_end']))
        if at_iso == end_iso:
            return len(starts)
    raise ValueError('feedback_time_not_a_period_boundary')


def _end_boundary_at(doc, period_index):
    # End-of-period timestamp: the next period start, or the declared shift end.
    starts = [climate_opt._iso(climate_opt._parse_ts(p['start']))
              for p in doc['periods']]
    if period_index + 1 < len(starts):
        return starts[period_index + 1]
    if doc.get('period_end'):
        return climate_opt._iso(climate_opt._parse_ts(doc['period_end']))
    raise ValueError('shift_end_not_declared')


def _flatten_rows(normalized, rows):
    trajectories = []
    for row in rows:
        for dock in normalized['docks']:
            cell = row['docks'][dock]
            trajectories.append({
                'period': row['period'], 'dock': dock,
                'start_temp': cell['start_temp'], 'warming': cell['warming'],
                'cooling_delta': cell['cooling_delta'],
                'end_temp': cell['end_temp'], 'model_end_temp': cell['model_end_temp'],
                'limit_c': cell['limit_c'], 'exposure': cell['exposure'],
                'source': cell['source'], 'price_cents': row['price_cents'],
                'chiller_owner': row['chiller_owner']})
    return trajectories


def _prefix_only_result(normalized, prefix_rows, prefix_cum, prefix_actions, executed):
    trajectories = _flatten_rows(normalized, prefix_rows)
    energy = sum(row['price_cents'] for row in prefix_rows
                 if row['chiller_owner'] is not None)
    actions = prefix_actions + [None] * (len(normalized['periods']) - executed)
    total = climate_opt._q(sum(prefix_cum.values()))
    return {'actions': actions, 'trajectories': trajectories,
            'dock_exposure': prefix_cum, 'total_exposure': total,
            'max_dock_exposure': climate_opt._q(max(prefix_cum.values())),
            'energy_cents': energy,
            'evidence': {'algorithm': 'prefix_locked_no_recovery'},
            'plan_status': 'breached'}


def _merge_prefix_suffix(normalized, prefix_rows, prefix_actions, suffix, executed):
    trajectories = _flatten_rows(normalized, prefix_rows)
    trajectories.extend(suffix['trajectories'])
    energy = sum(row['price_cents'] for row in prefix_rows
                 if row['chiller_owner'] is not None)
    energy += suffix['energy_cents']
    return {'actions': suffix['actions'], 'trajectories': trajectories,
            'dock_exposure': suffix['dock_exposure'],
            'total_exposure': suffix['total_exposure'],
            'max_dock_exposure': suffix['max_dock_exposure'],
            'energy_cents': energy,
            'evidence': {'prefix_algorithm': 'locked_executed_commands',
                         'suffix_algorithm': suffix['evidence']}}


def get_plan(db_path, case_id, objective):
    db = connect(db_path)
    try:
        row = db.execute('SELECT * FROM climate_plan WHERE case_id=? AND objective=? '
                         'ORDER BY version DESC LIMIT 1', (case_id, objective)).fetchone()
        if row is None:
            holds = [dict(r) for r in db.execute(
                'SELECT * FROM climate_hold WHERE case_id=? AND objective=? ORDER BY id DESC LIMIT 1',
                (case_id, objective))]
            return None if not holds else {'status': 'held', **holds[0]}
        return json.loads(row['payload'])
    finally:
        db.close()
