"""Exact finite-shift cooling optimization for dock staging areas.

Pure functions only: no database or filesystem access.  A shared chiller can
serve at most one dock per period.  Temperature dynamics per dock per period:

    end_temp = start_temp + environmental_warming - allocated_cooling

Exposure at a period is ``max(0, end_temp - dock_limit)``; missing readings are
never treated as zero.  The optimizer enumerates every feasible command
sequence with dynamic programming and keeps a dominance-pruned Pareto label
set per period, so the returned plan is the exact global optimum under the
lexicographic objectives, not a per-period greedy choice.
"""

Q = 6  # decimal places used when temperature states are deduped


def _q(value):
    return round(float(value) + 0.0, Q)


def _parse_ts(text):
    from datetime import datetime
    dt = datetime.fromisoformat(str(text).replace('Z', '+00:00'))
    if dt.tzinfo is None:
        raise ValueError('timezone_required')
    return dt


def _iso(dt):
    from datetime import timezone
    return dt.astimezone(timezone.utc).isoformat()


def validate_case(doc, sensors):
    """Return ``(reasons, normalized)`` for a frozen climate case.

    ``reasons`` is empty for a publishable case.  Any non-empty list means the
    whole recommendation must be held: no partial commands are valid.
    """
    reasons = []
    docks = list(doc.get('docks') or [])
    periods = list(doc.get('periods') or [])
    limits = doc.get('limits') or {}
    if not docks or not periods or not doc.get('contracts'):
        raise ValueError('bad_climate_case')
    if len(docks) > 4 or len(set(docks)) != len(docks):
        reasons.append('case_out_of_bounds')
    if len(periods) > 8 or len({p['id'] for p in periods}) != len(periods):
        reasons.append('case_out_of_bounds')
    if [p['id'] for p in periods] != list(range(len(periods))):
        reasons.append('period_ids_not_sequential')

    offsets = set()
    initial = {}
    for row in sensors:
        dock = row.get('dock')
        if dock not in docks:
            reasons.append('sensor_unknown_dock')
            continue
        try:
            temp = float(row['temp_c'])
        except (KeyError, TypeError, ValueError):
            reasons.append('sensor_bad_temperature')
            continue
        try:
            dt = _parse_ts(row.get('at', ''))
        except ValueError:
            reasons.append('sensor_timezone_inconsistent')
            continue
        offsets.add(dt.utcoffset())
        # Sensor identity collision: the same dock reports two readings that
        # share a timestamp, identical duplicates or conflicting temperatures.
        if dock in initial:
            reasons.append('sensor_identity_conflict')
        initial[dock] = {'temp_c': temp, 'at_iso': _iso(dt)}
    for dock in docks:
        if dock not in initial:
            reasons.append('sensor_missing_initial')

    warming = []
    prices = []
    for period in periods:
        step = {}
        for dock in docks:
            value = (period.get('warming') or {}).get(dock)
            if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
                reasons.append('warming_missing_or_bad')
                step[dock] = None
            else:
                step[dock] = float(value)
        warming.append(step)
        price = period.get('energy_cents')
        if not isinstance(price, int) or isinstance(price, bool) or price < 0:
            reasons.append('energy_price_bad')
        prices.append(price if isinstance(price, int) and not isinstance(price, bool) else 0)
        try:
            offsets.add(_parse_ts(period['start']).utcoffset())
        except (KeyError, ValueError):
            reasons.append('sensor_timezone_inconsistent')

    for dock in docks:
        limit = limits.get(dock)
        if not isinstance(limit, (int, float)) or isinstance(limit, bool):
            reasons.append('dock_limit_bad')

    cooling = doc.get('cooling_per_action')
    capacity = doc.get('shared_chiller_capacity_per_period')
    cap_total = doc.get('max_total_exposure')
    if (not isinstance(cooling, (int, float)) or isinstance(cooling, bool)
            or cooling <= 0):
        reasons.append('cooling_non_physical')
    if (not isinstance(capacity, int) or isinstance(capacity, bool)
            or capacity < 1 or capacity > len(docks)):
        reasons.append('cooling_non_physical')
    if (not isinstance(cap_total, (int, float)) or isinstance(cap_total, bool)
            or cap_total < 0):
        reasons.append('exposure_cap_bad')
    if len(offsets) > 1:
        reasons.append('sensor_timezone_inconsistent')

    normalized = {
        'docks': docks,
        'periods': periods,
        'limits': {d: float(limits[d]) for d in docks
                   if isinstance(limits.get(d), (int, float))
                   and not isinstance(limits.get(d), bool)},
        'warming': warming,
        'prices': prices,
        'cooling': float(cooling) if isinstance(cooling, (int, float))
        and not isinstance(cooling, bool) and cooling > 0 else 0.0,
        'capacity': capacity if isinstance(capacity, int)
        and not isinstance(capacity, bool) and 1 <= capacity <= len(docks) else 1,
        'max_total_exposure': float(cap_total)
        if isinstance(cap_total, (int, float)) and not isinstance(cap_total, bool)
        and cap_total >= 0 else 0.0,
        'initial': {d: initial[d]['temp_c'] for d in docks if d in initial},
        'initial_at': {d: initial[d]['at_iso'] for d in docks if d in initial},
        'contracts': list(doc.get('contracts') or []),
    }
    return sorted(set(reasons)), normalized


def simulate(normalized, start_temps, actions, observed_ends=None,
             horizon_offset=0, period_count=None):
    """Run the temperature dynamics over a horizon.

    ``actions`` is a full-length per-period list whose entries are either
    ``None`` (chiller idle) or a dock name; entries before ``horizon_offset``
    are skipped.  ``start_temps`` maps each dock to the temperature at the
    start of period ``horizon_offset``.  ``period_count`` limits how many
    periods are simulated (default: every period from ``horizon_offset``).
    ``observed_ends`` optionally maps a period index to a dock->temperature map
    of real readings; when present, observed end temperatures replace the model
    prediction and become the next period's starting point.  Returns per-period
    trajectory rows and per-dock cumulative exposure across the simulated
    (suffix) periods.
    """
    docks = normalized['docks']
    rows = []
    cumulative = {d: 0.0 for d in docks}
    temps = dict(start_temps)
    observed_ends = observed_ends or {}
    last = len(normalized['periods']) if period_count is None \
        else min(len(normalized['periods']), horizon_offset + period_count)
    for t in range(horizon_offset, last):
        period = normalized['periods'][t]
        owner = actions[t] if t < len(actions) else None
        if owner is not None and owner not in docks:
            raise ValueError('action_unknown_dock')
        step = {}
        next_temps = {}
        for dock in docks:
            start = _q(temps[dock])
            warming = normalized['warming'][t][dock]
            cooling = normalized['cooling'] if owner == dock else 0.0
            model_end = _q(start + warming - cooling)
            obs = observed_ends.get(t, {}).get(dock)
            end = _q(obs) if obs is not None else model_end
            exposure = _q(max(0.0, end - normalized['limits'][dock]))
            cumulative[dock] = _q(cumulative[dock] + exposure)
            step[dock] = {'start_temp': start, 'warming': float(warming),
                          'cooling_delta': _q(cooling), 'end_temp': end,
                          'model_end_temp': model_end,
                          'limit_c': normalized['limits'][dock],
                          'exposure': exposure,
                          'source': 'observed' if obs is not None else 'predicted'}
            next_temps[dock] = end
        rows.append({'period': period['id'], 'chiller_owner': owner,
                     'price_cents': normalized['prices'][t], 'docks': step})
        temps = next_temps
    return rows, cumulative


def solve(normalized, objective, start_temps=None, horizon_offset=0,
          exposure_used=None, budget_remaining=None, prefix_actions=None):
    """Exact dynamic program over the (suffix) horizon.

    At each period the chiller is either idle or assigned to one dock.  Each
    label carries quantized dock temperatures, per-dock cumulative suffix
    exposure, integer energy in cents and the chosen action path.  Labels that
    reach the identical dock-temperature state are merged by keeping the
    lexicographically best path; distinct states are all retained because a
    warmer dock is still coolable in a later period.  Every feasible action
    sequence is therefore represented by the surviving labels and the returned
    plan is the exact global optimum under the lexicographic objective.
    """
    if objective not in ('min_energy', 'min_max_dock_exposure'):
        raise ValueError('unknown_objective')
    docks = normalized['docks']
    n_periods = len(normalized['periods'])
    horizon = n_periods - horizon_offset
    if horizon < 0:
        raise ValueError('horizon_offset')
    start_temps = dict(start_temps or normalized['initial'])
    exposure_used = dict(exposure_used or {d: 0.0 for d in docks})
    if budget_remaining is None:
        budget_remaining = normalized['max_total_exposure'] - sum(exposure_used.values())
    # The DP accumulates suffix-only exposure; feasibility is checked against
    # the exposure budget still available after the executed prefix.
    suffix_cap = _q(budget_remaining)
    prefix_actions = list(prefix_actions or [])
    choices = [None] + docks

    labels = [(tuple(_q(start_temps[d]) for d in docks),
               tuple(0.0 for _ in docks), 0, ())]
    nodes = 1
    pruned = 0
    max_active = 1
    for t in range(horizon_offset, n_periods):
        candidates = []
        for temps, expos, energy, actions in labels:
            for owner in choices:
                nodes += 1
                new_temps = []
                new_expo = list(expos)
                for i, dock in enumerate(docks):
                    cooling = normalized['cooling'] if owner == dock else 0.0
                    end = _q(temps[i] + normalized['warming'][t][dock] - cooling)
                    new_temps.append(end)
                    new_expo[i] = _q(new_expo[i]
                                     + max(0.0, end - normalized['limits'][dock]))
                if _q(sum(new_expo)) > suffix_cap + 1e-9:
                    pruned += 1
                    continue
                candidates.append((tuple(new_temps), tuple(new_expo),
                                   energy + (normalized['prices'][t]
                                             if owner is not None else 0),
                                   actions + (owner,)))
        kept = _pareto(candidates, objective)
        pruned += len(candidates) - len(kept)
        labels = kept
        max_active = max(max_active, len(labels))

    evidence_base = {
        'algorithm': 'dynamic_programming_exhaustive',
        'nodes_evaluated': nodes, 'labels_pruned': pruned,
        'max_active_labels': max_active,
        'branching_factor': len(choices),
        'horizon_periods': horizon,
        'action_sequences_enumerated': nodes - 1,
        'dominance_rule': 'labels sharing the same quantized dock-temperature '
                          'state are merged keeping the lexicographically best '
                          'path; distinct states are never merged because a '
                          'warmer dock remains coolable later, so no feasible '
                          'action sequence is lost',
        'total_exposure_cap': normalized['max_total_exposure'],
        'exposure_budget_remaining': _q(budget_remaining)}
    if not labels:
        return {'feasible': False, 'objective': objective,
                'reasons': ['no_feasible_plan'], 'evidence': evidence_base}

    best = min(labels, key=lambda lab: _rank(lab, objective))
    suffix_actions = list(best[3])
    full_actions = prefix_actions + suffix_actions
    rows, suffix_cumulative = simulate(
        normalized, start_temps,
        [None] * horizon_offset + suffix_actions,
        horizon_offset=horizon_offset)
    cumulative = {d: _q(exposure_used.get(d, 0.0) + suffix_cumulative[d])
                  for d in docks}
    total_exposure = _q(sum(cumulative.values()))
    if total_exposure > normalized['max_total_exposure'] + 1e-9:
        return {'feasible': False, 'objective': objective,
                'reasons': ['no_feasible_plan'], 'evidence': evidence_base}

    trajectories = []
    for row in rows:
        for dock in docks:
            cell = row['docks'][dock]
            trajectories.append({
                'period': row['period'], 'dock': dock,
                'start_temp': cell['start_temp'], 'warming': cell['warming'],
                'cooling_delta': cell['cooling_delta'],
                'end_temp': cell['end_temp'], 'model_end_temp': cell['model_end_temp'],
                'limit_c': cell['limit_c'], 'exposure': cell['exposure'],
                'source': cell['source'], 'price_cents': row['price_cents'],
                'chiller_owner': row['chiller_owner']})
    evidence = dict(evidence_base)
    evidence['optimum_key'] = list(_rank(best, objective))
    return {
        'feasible': True, 'objective': objective, 'actions': full_actions,
        'suffix_actions': suffix_actions, 'trajectories': trajectories,
        'dock_exposure': cumulative, 'total_exposure': total_exposure,
        'max_dock_exposure': _q(max(cumulative.values())),
        'energy_cents': best[2], 'evidence': evidence}


def _pareto(candidates, objective):
    """Collapse labels to one per distinct physical state.

    Two labels with identical quantized dock temperatures are the same physical
    state for every future period: future warming, limits, cooling power and
    prices depend only on the period index and the dock, never on how the state
    was reached.  The accumulated exposure and energy differ between paths, so
    the kept path is the one with the better lexicographic objective rank (with
    a deterministic action tie break).  No other pruning is safe: a label with a
    warmer dock is not dominated while that dock can still be cooled in later
    periods and warming differs across docks.
    """
    best = {}
    for cand in candidates:
        temps, expos, energy, actions = cand
        old = best.get(temps)
        if old is None or _rank(cand, objective) < _rank(old, objective):
            best[temps] = cand
    survivors = list(best.values())
    survivors.sort(key=lambda lab: (lab[0], lab[1], lab[2], lab[3]))
    return survivors


def _rank(label, objective):
    _temps, expos, energy, actions = label
    max_expo = max(expos)
    total = sum(expos)
    # Final component is a deterministic tie break so identical inputs always
    # reproduce the same commands.
    tie = tuple(a if a is not None else '' for a in actions)
    if objective == 'min_energy':
        return (round(energy, Q), round(max_expo, Q), round(total, Q), tie)
    return (round(max_expo, Q), round(total, Q), round(energy, Q), tie)
