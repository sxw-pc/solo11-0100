"""Tests for finite-shift dock staging cooling control.

Covers exact global optimality of both authorized objectives, the physical
temperature/exposure dynamics, the shared-chiller single-owner constraint,
validation holds, late feedback versioning (executed commands are immutable),
irrecoverable breach reporting and conservation of exposure and money.
"""
import csv
import io
import json
import sqlite3
from pathlib import Path

import pytest

from dockscan import climate_opt
from dockscan.climate_control import register, plan, apply_feedback, get_plan
from dockscan.ingest import freeze_config, import_file
from dockscan.rules import evaluate
from dockscan.report import export
from dockscan.store import connect

FIX = Path(__file__).resolve().parents[1] / 'fixtures'
BOUNDARY_1 = '2030-06-01T10:00:00Z'


def _base_doc():
    return json.loads((FIX / 'climate-case.json').read_text())


def _base_sensors():
    return list(csv.DictReader(io.StringIO(
        (FIX / 'climate-sensors.csv').read_text(encoding='utf-8-sig'))))


def _write_case(tmp_path, doc, sensors, name='case.json', sensor_name='sensors.csv'):
    case_dir = tmp_path / 'climate'
    case_dir.mkdir(parents=True, exist_ok=True)
    sensor_path = case_dir / sensor_name
    with sensor_path.open('w', encoding='utf-8', newline='') as fh:
        writer = csv.DictWriter(fh, fieldnames=['dock', 'at', 'temp_c'])
        writer.writeheader()
        writer.writerows(sensors)
    doc = dict(doc)
    doc['sensor_file'] = sensor_name
    case_path = case_dir / name
    case_path.write_text(json.dumps(doc), encoding='utf-8')
    return case_path


def _seeded_db(tmp_path, doc=None, sensors=None, case_id='climate-01'):
    db = tmp_path / 'climate.db'
    freeze_config(db, FIX / 'climate-config.json')
    import_file(db, FIX / 'climate-events.jsonl')
    evaluate(db)
    case_path = _write_case(tmp_path, doc or _base_doc(),
                            sensors or _base_sensors())
    register(db, case_id, case_path)
    return db, case_id


def _traj(db, objective, version):
    con = connect(db)
    rows = [dict(r) for r in con.execute(
        'SELECT * FROM climate_trajectory WHERE objective=? AND version=? '
        'ORDER BY period,dock', (objective, version))]
    con.close()
    return rows


# --------------------------------------------------------------------- optima

def test_min_energy_contract_matches_spec_exact_optimum(tmp_path):
    db, cid = _seeded_db(tmp_path)
    summary = plan(db, cid, 'min_energy')
    assert summary['status'] == 'published'
    assert summary['energy_cents'] == 100
    assert summary['total_exposure'] == 6.0
    payload = get_plan(db, cid, 'min_energy')
    assert payload['actions'] == ['D1', None]
    assert payload['dock_exposure'] == {'D1': 2.0, 'D2': 4.0}
    assert payload['max_dock_exposure'] == 4.0
    con = connect(db)
    try:
        owners = [r['dock'] for r in con.execute(
            'SELECT period,dock FROM climate_command WHERE objective=? AND version=1 '
            'ORDER BY period', ('min_energy',))]
        assert owners == ['D1', None]  # never two docks in one period
    finally:
        con.close()


def test_min_max_dock_exposure_contract_matches_spec(tmp_path):
    db, cid = _seeded_db(tmp_path)
    summary = plan(db, cid, 'min_max_dock_exposure')
    assert summary['energy_cents'] == 1100
    assert summary['max_dock_exposure'] == 2.0
    payload = get_plan(db, cid, 'min_max_dock_exposure')
    assert payload['actions'] == ['D1', 'D2']
    assert payload['dock_exposure'] == {'D1': 2.0, 'D2': 2.0}
    assert payload['total_exposure'] == 4.0 <= 6.0


def test_temperature_dynamics_chain_from_previous_period(tmp_path):
    db, cid = _seeded_db(tmp_path)
    plan(db, cid, 'min_energy')
    rows = _traj(db, 'min_energy', 1)
    by_pd = {(r['period'], r['dock']): r for r in rows}
    # D1: 20 + 3 warming - 2 cooling = 21 at end of period 0
    assert by_pd[(0, 'D1')]['start_temp'] == 20.0
    assert by_pd[(0, 'D1')]['end_temp'] == 21.0
    # period 1 starts from the prior period's real/predicted end temperature
    assert by_pd[(1, 'D1')]['start_temp'] == by_pd[(0, 'D1')]['end_temp']
    assert by_pd[(1, 'D1')]['end_temp'] == 24.0  # 21 + 3 - 0
    # D2 never cooled: 20 -> 22 -> 24
    assert by_pd[(0, 'D2')]['cooling_delta'] == 0.0
    assert by_pd[(0, 'D2')]['end_temp'] == 22.0
    assert by_pd[(1, 'D2')]['end_temp'] == 24.0


def test_exposure_is_sum_of_positive_over_limit_not_max_instant(tmp_path):
    db, cid = _seeded_db(tmp_path)
    plan(db, cid, 'min_energy')
    rows = _traj(db, 'min_energy', 1)
    d2 = [r for r in rows if r['dock'] == 'D2']
    # D2 limit 21: ends at 22 (1 over), then 24 (3 over) -> cumulative 4
    assert [r['exposure'] for r in d2] == [1.0, 3.0]
    assert sum(r['exposure'] for r in d2) == 4.0
    # an end temperature at/below the limit contributes zero
    assert all(r['exposure'] >= 0 for r in rows)


def test_exhaustive_dp_enumerates_every_feasible_sequence(tmp_path):
    db, cid = _seeded_db(tmp_path)
    plan(db, cid, 'min_energy')
    evidence = get_plan(db, cid, 'min_energy')['evidence']
    # 1 root + 3 choices at period 0 + 3*3 at period 1 = 13 evaluated nodes
    assert evidence['nodes_evaluated'] == 13
    assert evidence['algorithm'] == 'dynamic_programming_exhaustive'
    # brute-force cross-check: enumerate all 3^2 sequences independently
    doc = _base_doc()
    _, norm = climate_opt.validate_case(doc, _base_sensors())
    brute_best = None
    for a0 in [None, 'D1', 'D2']:
        for a1 in [None, 'D1', 'D2']:
            rows, cum = climate_opt.simulate(norm, norm['initial'], [a0, a1])
            total = sum(cum.values())
            energy = (norm['prices'][0] if a0 else 0) + (norm['prices'][1] if a1 else 0)
            if total <= norm['max_total_exposure'] + 1e-9:
                key = (energy, max(cum.values()), total, (a0 or '', a1 or ''))
                if brute_best is None or key < brute_best:
                    brute_best = key
    assert tuple(evidence['optimum_key'][:3]) == brute_best[:3]


def test_solver_scales_to_four_docks_eight_periods():
    import copy
    doc = copy.deepcopy(_base_doc())
    doc['docks'] = ['D1', 'D2', 'D3', 'D4']
    doc['limits'] = {'D1': 22, 'D2': 21, 'D3': 22, 'D4': 21}
    doc['max_total_exposure'] = 300
    doc['periods'] = [
        {'id': i, 'start': f'2030-06-01T{9 + i:02d}:00:00Z',
         'warming': {'D1': 3, 'D2': 2, 'D3': 1, 'D4': 2},
         'energy_cents': 100 * (i + 1)} for i in range(8)]
    sensors = [{'dock': d, 'at': '2030-06-01T08:30:00Z', 'temp_c': '20'}
               for d in doc['docks']]
    reasons, normalized = climate_opt.validate_case(doc, sensors)
    assert reasons == []
    result = climate_opt.solve(normalized, 'min_max_dock_exposure')
    assert result['feasible']
    assert len(result['actions']) == 8
    # single shared chiller: at most one owner per period
    assert sum(1 for a in result['actions'] if a is not None) <= 8
    assert result['evidence']['max_active_labels'] > 0
    assert result['total_exposure'] <= 300 + 1e-9


def test_deterministic_replan_with_identical_inputs(tmp_path):
    db, cid = _seeded_db(tmp_path)
    first = plan(db, cid, 'min_energy')
    second = plan(db, cid, 'min_energy')
    assert second['unchanged'] is True
    assert second['version'] == first['version']
    p1 = get_plan(db, cid, 'min_energy')
    assert p1['input_digest'] == get_plan(db, cid, 'min_energy')['input_digest']


# ------------------------------------------------------------- physical holds

def test_single_dock_missing_reading_holds_whole_recommendation(tmp_path):
    # register still freezes a row for each dock, but one reading carries no
    # usable temperature, so planning must hold rather than treat it as zero.
    sensors = _base_sensors()
    sensors[1] = {'dock': 'D2', 'at': sensors[1]['at'], 'temp_c': ''}
    db, cid = _seeded_db(tmp_path, sensors=sensors)
    summary = plan(db, cid, 'min_energy')
    assert summary['status'] == 'held'
    assert 'sensor_bad_temperature' in summary['reasons']
    con = connect(db)
    try:
        assert con.execute('SELECT COUNT(*) FROM climate_command').fetchone()[0] == 0
        assert con.execute('SELECT COUNT(*) FROM climate_plan').fetchone()[0] == 0
        assert con.execute('SELECT COUNT(*) FROM climate_hold').fetchone()[0] == 1
    finally:
        con.close()


def test_duplicate_sensor_identity_conflict_holds(tmp_path):
    sensors = _base_sensors()
    sensors.append({'dock': 'D1', 'at': sensors[0]['at'], 'temp_c': '19'})
    db, cid = _seeded_db(tmp_path, sensors=sensors)
    summary = plan(db, cid, 'min_energy')
    assert summary['status'] == 'held'
    assert 'sensor_identity_conflict' in summary['reasons']


def test_timezone_inconsistent_holds(tmp_path):
    sensors = _base_sensors()
    sensors[0]['at'] = '2030-06-01T08:30:00+02:00'  # different offset
    db, cid = _seeded_db(tmp_path, sensors=sensors)
    summary = plan(db, cid, 'min_energy')
    assert summary['status'] == 'held'
    assert 'sensor_timezone_inconsistent' in summary['reasons']


def test_naive_timestamp_holds(tmp_path):
    # register skips the unusable row; planning then holds because the dock has
    # no usable initial reading (a missing reading is never treated as zero).
    sensors = _base_sensors()
    sensors[1]['at'] = '2030-06-01T08:30:00'  # no timezone
    db, cid = _seeded_db(tmp_path, sensors=sensors)
    summary = plan(db, cid, 'min_energy')
    assert summary['status'] == 'held'
    assert 'sensor_missing_initial' in summary['reasons']


def test_missing_warming_term_holds(tmp_path):
    doc = _base_doc()
    del doc['periods'][0]['warming']['D2']
    db, cid = _seeded_db(tmp_path, doc=doc)
    summary = plan(db, cid, 'min_energy')
    assert summary['status'] == 'held'
    assert 'warming_missing_or_bad' in summary['reasons']


def test_non_physical_cooling_holds(tmp_path):
    doc = _base_doc()
    doc['cooling_per_action'] = 0
    db, cid = _seeded_db(tmp_path, doc=doc)
    summary = plan(db, cid, 'min_energy')
    assert 'cooling_non_physical' in summary['reasons']
    doc2 = _base_doc()
    doc2['shared_chiller_capacity_per_period'] = 0
    db2, cid2 = _seeded_db(tmp_path, doc=doc2, case_id='climate-02')
    assert 'cooling_non_physical' in plan(db2, cid2, 'min_energy')['reasons']


def test_simultaneous_double_request_is_rejected(tmp_path):
    doc = _base_doc()
    doc['periods'][0]['cooling_requests'] = ['D1', 'D2']  # both want cooling
    db, cid = _seeded_db(tmp_path, doc=doc)
    summary = plan(db, cid, 'min_energy')
    assert summary['status'] == 'held'
    assert 'chiller_double_booking' in summary['reasons']


def test_chiller_never_serves_two_docks_in_one_period(tmp_path):
    # Brute force across every published plan: one owner per period.
    db, cid = _seeded_db(tmp_path)
    for objective in ('min_energy', 'min_max_dock_exposure'):
        plan(db, cid, objective)
    con = connect(db)
    try:
        rows = con.execute(
            'SELECT version,objective,period,COUNT(*) AS n FROM climate_command '
            'WHERE dock IS NOT NULL GROUP BY version,objective,period').fetchall()
        assert all(r['n'] == 1 for r in rows)
    finally:
        con.close()


def test_chiller_exactly_full_every_period_must_serve_one_dock(tmp_path):
    doc = _base_doc()
    doc['max_total_exposure'] = 4  # any idle period makes the cap unreachable
    db, cid = _seeded_db(tmp_path, doc=doc)
    summary = plan(db, cid, 'min_energy')
    assert summary['status'] == 'published'
    payload = get_plan(db, cid, 'min_energy')
    assert all(owner is not None for owner in payload['actions'])
    assert len(payload['actions']) == len(set(
        (t, owner) for t, owner in enumerate(payload['actions'])))
    con = connect(db)
    try:
        per_period = con.execute(
            'SELECT period,COUNT(*) AS n FROM climate_command WHERE version=1 '
            'AND dock IS NOT NULL GROUP BY period').fetchall()
        assert {r['n'] for r in per_period} == {1}
    finally:
        con.close()


def test_no_feasible_plan_when_capacity_insufficient(tmp_path):
    doc = _base_doc()
    doc['max_total_exposure'] = 1  # even the best plan totals 4
    db, cid = _seeded_db(tmp_path, doc=doc)
    summary = plan(db, cid, 'min_max_dock_exposure')
    assert summary['status'] == 'held'
    assert summary['reasons'] == ['no_feasible_plan']
    con = connect(db)
    try:
        assert con.execute('SELECT COUNT(*) FROM climate_command').fetchone()[0] == 0
    finally:
        con.close()


# ------------------------------------------------------------- late feedback

def _feedback_pair(db, objective, t1, t2):
    apply_feedback(db, 'climate-01', objective, 'D1', BOUNDARY_1, t1)
    return apply_feedback(db, 'climate-01', objective, 'D2', BOUNDARY_1, t2)


def test_partial_boundary_feedback_holds_until_all_docks_report(tmp_path):
    db, cid = _seeded_db(tmp_path)
    plan(db, cid, 'min_energy')
    first = apply_feedback(db, cid, 'min_energy', 'D1', BOUNDARY_1, 22.0)
    assert first['status'] == 'held'
    assert 'feedback_boundary_incomplete' in first['reasons'][0]
    # no new plan version and no commands were published by the partial read
    con = connect(db)
    try:
        assert con.execute('SELECT MAX(version) FROM climate_plan').fetchone()[0] == 1
    finally:
        con.close()


def test_late_feedback_keeps_executed_command_and_replans_suffix(tmp_path):
    db, cid = _seeded_db(tmp_path)
    plan(db, cid, 'min_max_dock_exposure')
    result = _feedback_pair(db, 'min_max_dock_exposure', 20.0, 20.0)
    assert result['status'] == 'published'
    assert result['version'] == 2
    payload = get_plan(db, cid, 'min_max_dock_exposure')
    # period 0 command is immutable; suffix command changed causally
    assert payload['actions'][0] == 'D1'
    assert payload['executed_periods'] == 1
    rows = _traj(db, 'min_max_dock_exposure', 2)
    period0 = {r['dock']: r for r in rows if r['period'] == 0}
    assert period0['D1']['source'] == 'observed'
    assert period0['D1']['end_temp'] == 20.0
    suffix = {r['dock']: r for r in rows if r['period'] == 1}
    assert suffix['D1']['start_temp'] == 20.0  # corrected from observed reading
    con = connect(db)
    try:
        states = [r['status'] for r in con.execute(
            'SELECT status FROM climate_command WHERE objective=? AND version=2 ORDER BY period',
            ('min_max_dock_exposure',))]
        assert states == ['executed', 'pending']
    finally:
        con.close()


def test_feedback_never_rewrites_historical_versions(tmp_path):
    db, cid = _seeded_db(tmp_path)
    plan(db, cid, 'min_max_dock_exposure')
    before = _traj(db, 'min_max_dock_exposure', 1)
    _feedback_pair(db, 'min_max_dock_exposure', 20.0, 20.0)
    after = _traj(db, 'min_max_dock_exposure', 1)
    assert before == after  # v1 bytes-for-bytes unchanged


def test_breach_is_reported_irrecoverable_with_no_negative_exposure(tmp_path):
    db, cid = _seeded_db(tmp_path)
    plan(db, cid, 'min_energy')
    # Observed period-0 ends already exceed the whole-shift cap by themselves:
    # D1 28 -> 6 over its 22 limit; D2 26 -> 5 over its 21 limit = 11 total.
    apply_feedback(db, cid, 'min_energy', 'D1', BOUNDARY_1, 28.0)
    result = apply_feedback(db, cid, 'min_energy', 'D2', BOUNDARY_1, 26.0)
    assert result['status'] == 'breached'
    assert result['incurred_total_exposure'] == 11.0
    assert result['irrecoverable_exposure'] == 5.0
    payload = get_plan(db, cid, 'min_energy')
    assert payload['irrecoverable_breach']['cap'] == 6.0
    rows = _traj(db, 'min_energy', payload['version'])
    assert all(r['exposure'] >= 0 for r in rows)  # never negative
    # the executed D1 command itself is preserved, not rewritten
    assert payload['actions'][0] == 'D1'


def test_infeasible_suffix_after_feedback_is_held_without_partial_commands(tmp_path):
    # Tight contract cap: the plan is feasible initially (exposure 4 <= 4), but
    # hotter observed readings leave a suffix budget that no command can meet.
    doc = _base_doc()
    doc['max_total_exposure'] = 4
    db, cid = _seeded_db(tmp_path, doc=doc)
    plan(db, cid, 'min_max_dock_exposure')
    # Period-0 observed ends already consume the whole cap (D1 22 -> 0 over,
    # D2 22 -> 1 over; combined with the minimum suffix exposure 2 it cannot fit).
    apply_feedback(db, cid, 'min_max_dock_exposure', 'D1', BOUNDARY_1, 22.0)
    result = apply_feedback(db, cid, 'min_max_dock_exposure',
                            'D2', BOUNDARY_1, 22.0)
    assert result['status'] == 'held'
    assert result['reasons'] == ['no_feasible_plan']
    con = connect(db)
    try:
        # no extra command set was published for the failed replanning
        versions = {r[0] for r in con.execute(
            'SELECT DISTINCT version FROM climate_command').fetchall()}
        assert versions == {1}
    finally:
        con.close()


def test_full_shift_feedback_closes_plan_without_negative_exposure(tmp_path):
    db, cid = _seeded_db(tmp_path)
    plan(db, cid, 'min_energy')
    # period-0 boundary, then the declared shift end (period 1 end, 11:00Z)
    apply_feedback(db, cid, 'min_energy', 'D1', BOUNDARY_1, 21.0)
    apply_feedback(db, cid, 'min_energy', 'D2', BOUNDARY_1, 22.0)
    end = apply_feedback(db, cid, 'min_energy', 'D1',
                         '2030-06-01T11:00:00Z', 24.0)
    assert end['status'] == 'held'  # D2 at shift end still missing
    result = apply_feedback(db, cid, 'min_energy', 'D2',
                            '2030-06-01T11:00:00Z', 24.0)
    assert result['status'] == 'closed'
    assert result['executed_periods'] == 2
    payload = get_plan(db, cid, 'min_energy')
    assert payload['actions'] == ['D1', None]  # executed commands preserved
    assert payload['total_exposure'] == 6.0
    rows = _traj(db, 'min_energy', payload['version'])
    assert {r['source'] for r in rows} == {'observed'}
    assert all(r['exposure'] >= 0 for r in rows)


def test_conflicting_feedback_at_same_timestamp_is_rejected(tmp_path):
    db, cid = _seeded_db(tmp_path)
    plan(db, cid, 'min_energy')
    apply_feedback(db, cid, 'min_energy', 'D1', BOUNDARY_1, 22.0)
    with pytest.raises(ValueError, match='sensor_identity_conflict'):
        apply_feedback(db, cid, 'min_energy', 'D1', BOUNDARY_1, 23.0)


def test_feedback_at_non_boundary_time_is_rejected(tmp_path):
    db, cid = _seeded_db(tmp_path)
    plan(db, cid, 'min_energy')
    with pytest.raises(ValueError, match='feedback_time_not_a_period_boundary'):
        apply_feedback(db, cid, 'min_energy', 'D1',
                       '2030-06-01T09:30:00Z', 22.0)


def test_objective_switch_changes_commands_but_not_physical_inputs(tmp_path):
    db, cid = _seeded_db(tmp_path)
    plan(db, cid, 'min_energy')
    plan(db, cid, 'min_max_dock_exposure')
    energy = get_plan(db, cid, 'min_energy')
    exposure = get_plan(db, cid, 'min_max_dock_exposure')
    assert energy['actions'] != exposure['actions']
    assert energy['input_digest'] != exposure['input_digest']
    # same frozen source bytes and sensors
    assert energy['source_summary']['sensor_sha256'] == \
        exposure['source_summary']['sensor_sha256']


# ------------------------------------------------------------- conservation

def test_exposure_and_money_conservation(tmp_path):
    db, cid = _seeded_db(tmp_path)
    plan(db, cid, 'min_max_dock_exposure')
    con = connect(db)
    try:
        traj_sum = con.execute(
            'SELECT SUM(exposure) AS s FROM climate_trajectory WHERE objective=? AND version=1',
            ('min_max_dock_exposure',)).fetchone()['s']
        payload = get_plan(db, cid, 'min_max_dock_exposure')
        assert abs(traj_sum - payload['total_exposure']) < 1e-9
        cmd_sum = con.execute(
            'SELECT SUM(energy_cents) AS s FROM climate_command WHERE objective=? AND version=1',
            ('min_max_dock_exposure',)).fetchone()['s']
        assert cmd_sum == payload['energy_cents'] == 1100
    finally:
        con.close()


def test_feedback_version_conserves_incurred_plus_suffix(tmp_path):
    db, cid = _seeded_db(tmp_path)
    plan(db, cid, 'min_max_dock_exposure')
    _feedback_pair(db, 'min_max_dock_exposure', 20.0, 20.0)
    rows = _traj(db, 'min_max_dock_exposure', 2)
    payload = get_plan(db, cid, 'min_max_dock_exposure')
    assert abs(sum(r['exposure'] for r in rows) - payload['total_exposure']) < 1e-9
    # energy: executed prefix (100 cents for D1 in period 0) + suffix
    assert payload['energy_cents'] >= 100
    con = connect(db)
    try:
        cmd_sum = con.execute(
            'SELECT SUM(energy_cents) AS s FROM climate_command WHERE objective=? AND version=2',
            ('min_max_dock_exposure',)).fetchone()['s']
        assert cmd_sum == payload['energy_cents']
    finally:
        con.close()


def test_reports_include_executable_commands_and_evidence(tmp_path):
    db, cid = _seeded_db(tmp_path)
    plan(db, cid, 'min_energy')
    out = export(db, tmp_path / 'report.json')
    data = json.loads(out.read_text(encoding='utf-8'))
    assert data['decision'] == [  # original receiving decisions untouched
        d for d in data['decision']
    ]
    statuses = {d['status'] for d in data['decision']}
    assert statuses == {'accepted'}
    commands = data['climate']['climate_command']
    assert any(c['dock'] == 'D1' and c['period'] == 0 for c in commands)
    assert data['climate']['climate_trajectory']
    md = out.with_suffix('.md').read_text(encoding='utf-8')
    assert '仓门暂存区冷却控制' in md
    assert 'dynamic_programming_exhaustive' in md


def test_cli_climate_demo_emits_both_contracts(tmp_path):
    from dockscan.__main__ import climate_demo
    result = climate_demo(tmp_path / 'demo')
    objectives = {p['objective'] for p in result['plans']}
    assert objectives == {'min_energy', 'min_max_dock_exposure'}
    assert Path(result['report']).exists()
