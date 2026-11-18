"""Disclosure review: exact linear inference over confidential box rates.

The confidential quantities are per-box freight rates (integer cents). Every
external aggregate release is an exact linear equation

    sum(rate[box] for box in members) = total

over the unknown rate vector. A box rate is *uniquely determined* by the
equations already visible to one audience iff the unit vector e(box) belongs
to their row space — equivalently, reduced row echelon form (exact rational
arithmetic) contains a row with a single non-zero entry.

The check is structural: denial records carry box identifiers and equation
member lists, never rates or totals, so rejection reasons cannot themselves
leak the protected numbers.
"""
import hashlib
import json
from fractions import Fraction
from pathlib import Path

from .store import connect, effective_history, now_iso

# Contract registry: each contract has one pre-authorized audience and an
# independently maintained authorization policy.
CONTRACTS = {
    'internal_auditor_exact_access': {
        'audience': 'internal_auditor',
        'allows_per_box': True,
        'min_group_size': 1,
        'non_inference_required': False,
    },
    'carrier_consortium_non_inference': {
        'audience': 'carrier_consortium',
        'allows_per_box': False,
        'min_group_size': 2,
        'non_inference_required': True,
    },
}


# ---------------------------------------------------------------------------
# Exact inference engine
# ---------------------------------------------------------------------------

def rref(matrix):
    """Reduced row echelon form over exact rationals. Input is a list of lists."""
    m = [list(row) for row in matrix]
    if not m:
        return m
    rows, cols = len(m), len(m[0])
    r = 0
    pivots = []
    for c in range(cols):
        if r >= rows:
            break
        pivot = next((i for i in range(r, rows) if m[i][c] != 0), None)
        if pivot is None:
            continue
        m[r], m[pivot] = m[pivot], m[r]
        scale = m[r][c]
        m[r] = [Fraction(v) / scale for v in m[r]]
        for i in range(rows):
            if i != r and m[i][c] != 0:
                factor = m[i][c]
                m[i] = [a - factor * b for a, b in zip(m[i], m[r])]
        pivots.append(c)
        r += 1
    return m, pivots


def fixed_boxes(equations, universe):
    """Return (fixed_index_set, rref_rows, nullspace_dimension).

    ``equations`` is a list of coefficient vectors (Fraction-convertible)
    aligned with ``universe``. A coordinate is fixed exactly when RREF holds a
    row whose only non-zero entry is in that column.
    """
    n = len(universe)
    matrix = [[Fraction(x) for x in eq] for eq in equations]
    if not matrix:
        return set(), [], n
    reduced, pivots = rref(matrix)
    fixed = set()
    for row in reduced:
        nonzero = [j for j, v in enumerate(row) if v != 0]
        if len(nonzero) == 1:
            fixed.add(nonzero[0])
    nullspace_dimension = n - len(pivots)
    return fixed, reduced, nullspace_dimension


def nullspace_basis(matrix, ncols):
    """Structural degrees of freedom as a {free_col: {col: coefficient}} map.

    Only zero/one coefficients are emitted — enough evidence to show which
    boxes can move together, without assigning any protected value.
    """
    if not matrix:
        return {c: {c: 1} for c in range(ncols)}
    reduced, pivots = rref(matrix)
    pivot_set = set(pivots)
    free = [c for c in range(ncols) if c not in pivot_set]
    basis = {}
    pivot_row = {p: i for i, p in enumerate(pivots)}
    for f in free:
        vector = {f: 1}
        for p in pivots:
            coefficient = -reduced[pivot_row[p]][f]
            if coefficient != 0:
                vector[p] = int(coefficient) \
                    if coefficient.denominator == 1 else str(coefficient)
        basis[f] = vector
    return basis


# ---------------------------------------------------------------------------
# Case loading and deterministic release identity
# ---------------------------------------------------------------------------

def load_case(db, case_id):
    row = db.execute('SELECT payload FROM privacy_case WHERE id=?', (case_id,)).fetchone()
    if row is None:
        raise ValueError('unknown_privacy_case')
    frozen = json.loads(row['payload'])
    doc = frozen['doc']
    records = frozen['records']
    universe = list(doc['boxes'])
    rates = {}
    record_by_box = {}
    source_hashes = set()
    for rec in records:
        payload = json.loads(rec['payload'])
        rates[rec['subject']] = int(payload['freight_rate_cents'])
        record_by_box[rec['subject']] = rec
        source_hashes.add(rec['source_sha256'])
    # Causal guard: the frozen rows must still match the immutable event/source
    # tables. A source revision makes every review fail closed.
    for rec in records:
        live = db.execute(
            'SELECT e.sha256 AS event_hash, s.sha256 AS source_hash, s.path AS source_path '
            'FROM event e JOIN source s ON s.id=e.source_id WHERE e.id=?',
            (rec['id'],)).fetchone()
        if live is None or live['event_hash'] != rec['sha256'] or \
                live['source_hash'] != rec['source_sha256']:
            raise ValueError('source_state_conflict')
        rec['source_path'] = live['source_path']
    return {
        'case_id': case_id,
        'doc': doc,
        'universe': universe,
        'rates': rates,
        'records': record_by_box,
        'source_hashes': sorted(source_hashes),
        'source_hash': hashlib.sha256(
            ''.join(sorted(source_hashes)).encode()).hexdigest(),
        'fixture_digest': frozen['digest'],
        'confidential_field': doc.get('confidential_field', 'freight_rate_cents'),
    }


def canonical_identity(case):
    """Stable identity tuple hash; callers extend it with release specifics."""
    return [case['case_id'], case['doc'].get('source_version', 'v1'),
            case['source_hash']]


def release_identity(case, audience, contract, kind, members, amounts):
    payload = {
        'case_id': case['case_id'],
        'source_version': case['doc'].get('source_version', 'v1'),
        'source_hash': case['source_hash'],
        'audience': audience,
        'contract': contract,
        'kind': kind,
        'members': sorted(members),
        'amounts': amounts,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(canonical.encode()).hexdigest(), canonical


# ---------------------------------------------------------------------------
# History -> equation system
# ---------------------------------------------------------------------------

def history_equations(rows, universe):
    index = {box: i for i, box in enumerate(universe)}
    equations, provenance = [], []
    for row in rows:
        members = json.loads(row['members_json'])
        if row['kind'] == 'per_box':
            for box in members:
                vec = [0] * len(universe)
                vec[index[box]] = 1
                equations.append(vec)
                provenance.append({'release_id': row['release_id'], 'members': [box]})
        else:
            vec = [0] * len(universe)
            for box in members:
                vec[index[box]] = 1
            equations.append(vec)
            provenance.append({'release_id': row['release_id'], 'members': sorted(members)})
    return equations, provenance


def _structural_evidence(universe, history_provenance, proposal_members,
                         before_dim, after_dim, fixed_before, fixed_after, basis):
    index = {box: i for i, box in enumerate(universe)}
    return {
        'universe': universe,
        'history_equations': [p['members'] for p in history_provenance],
        'proposal_equations': [sorted(proposal_members)],
        'nullspace_dimension_before': before_dim,
        'nullspace_dimension_after': after_dim,
        'fixed_boxes': [universe[i] for i in sorted(fixed_after)],
        'already_fixed_boxes': [universe[i] for i in sorted(fixed_before)],
        'newly_fixed_boxes': [universe[i] for i in sorted(fixed_after - fixed_before)],
        'degrees_of_freedom': {
            universe[free]: [universe[j] for j in sorted(vectors)]
            for free, vectors in basis.items()
        },
    }


# ---------------------------------------------------------------------------
# Proposal review
# ---------------------------------------------------------------------------

def review_proposal(db, case_id, audience, contract, kind, members,
                    declared_sum=None):
    """Authorize one proposal against one audience's full disclosure history.

    Returns a decision dict. Allowed decisions never embed protected values in
    rejection paths; rate-derived amounts are returned only on the allowed
    path and stay inside the local, trusted ledger.
    """
    if contract not in CONTRACTS:
        return {'decision': 'denied', 'reason_code': 'unknown_contract',
                'reason_detail': {'contract': contract},
                'audience': audience, 'contract': contract, 'kind': kind}
    policy = CONTRACTS[contract]
    if audience != policy['audience']:
        return {'decision': 'denied', 'reason_code': 'audience_contract_mismatch',
                'reason_detail': {'audience': audience, 'contract': contract,
                                  'authorized_audience': policy['audience']},
                'audience': audience, 'contract': contract, 'kind': kind}
    if kind not in ('aggregate', 'per_box'):
        return {'decision': 'denied', 'reason_code': 'unknown_release_kind',
                'reason_detail': {'kind': kind},
                'audience': audience, 'contract': contract, 'kind': kind}

    case = load_case(db, case_id)
    universe = case['universe']
    members = sorted(members)
    unknown = [b for b in members if b not in universe]
    if not members or unknown:
        return {'decision': 'denied', 'reason_code': 'unknown_box',
                'reason_detail': {'unknown_boxes': unknown},
                'audience': audience, 'contract': contract, 'kind': kind}

    if kind == 'per_box' and not policy['allows_per_box']:
        return {'decision': 'denied', 'reason_code': 'per_box_authorization_denied',
                'reason_detail': {'contract': contract, 'boxes': members},
                'audience': audience, 'contract': contract, 'kind': kind}
    if kind == 'aggregate' and len(members) < policy['min_group_size']:
        return {'decision': 'denied', 'reason_code': 'singleton_group_forbidden',
                'reason_detail': {'group_size': len(members),
                                  'minimum': policy['min_group_size']},
                'audience': audience, 'contract': contract, 'kind': kind}

    # Amounts are always derived from the frozen source rows, never trusted
    # from the requester.
    if kind == 'aggregate':
        amount = sum(case['rates'][b] for b in members)
        if declared_sum is not None and int(declared_sum) != amount:
            return {'decision': 'denied', 'reason_code': 'sum_mismatch_source',
                    'reason_detail': {'boxes': members}}
        amounts = {'total_cents': amount}
        proposal_vec = [{b: 1 for b in members}]
    else:
        amount = None
        amounts = {'per_box_cents': {b: case['rates'][b] for b in members}}
        proposal_vec = [{b: 1} for b in members]

    identity, canonical = release_identity(case, audience, contract, kind,
                                           members, amounts)

    # Exact duplicate of an effective release: idempotent re-proposal.
    existing = db.execute(
        "SELECT * FROM release_ledger WHERE release_id=? AND status IN "
        "('committed','proposed')", (identity,)).fetchone()
    if existing is not None:
        return {'decision': 'already_published', 'reason_code': 'duplicate_group',
                'release_id': identity, 'kind': kind, 'members': members,
                'audience': audience, 'contract': contract,
                'source_hash': case['source_hash']}

    history = effective_history(db, case_id, audience)
    equations, provenance = history_equations(history, universe)
    fixed_before, _, dim_before = fixed_boxes(equations, universe)

    index = {box: i for i, box in enumerate(universe)}
    extended = list(equations)
    for vec_map in proposal_vec:
        vec = [0] * len(universe)
        for box, coefficient in vec_map.items():
            vec[index[box]] = coefficient
        extended.append(vec)
    fixed_after, _, dim_after = fixed_boxes(extended, universe)
    basis = nullspace_basis(extended, len(universe))

    evidence = _structural_evidence(universe, provenance, members,
                                    dim_before, dim_after, fixed_before,
                                    fixed_after, basis)

    if policy['non_inference_required'] and fixed_after:
        evidence['rule'] = 'no_box_rate_may_be_uniquely_determined'
        return {'decision': 'denied', 'reason_code': 'inference_violation',
                'reason_detail': {'boxes': members,
                                  'fixed_boxes': evidence['fixed_boxes'],
                                  'newly_fixed_boxes': evidence['newly_fixed_boxes']},
                'evidence': evidence, 'release_id': identity,
                'source_hash': case['source_hash'],
                'audience': audience, 'contract': contract, 'kind': kind,
                'source_version': case['doc'].get('source_version', 'v1')}

    return {'decision': 'allowed', 'release_id': identity,
            'identity_canonical': canonical, 'kind': kind, 'members': members,
            'amounts': amounts, 'evidence': evidence,
            'audience': audience, 'contract': contract,
            'source_version': case['doc'].get('source_version', 'v1'),
            'source_hash': case['source_hash'], 'case': case}


# ---------------------------------------------------------------------------
# Privacy case registration (frozen rows + frozen prior disclosure history)
# ---------------------------------------------------------------------------

def register(db_path, case_id, fixture):
    source = Path(fixture).resolve()
    raw = source.read_bytes()
    doc = json.loads(raw)
    if not case_id or not doc.get('contracts') or not doc.get('boxes'):
        raise ValueError('bad_privacy_case')
    db = connect(db_path)
    try:
        with db:
            records = []
            for bid in doc['boxes']:
                rows = [dict(row) for row in db.execute(
                    'SELECT e.id,e.subject,e.payload,e.sha256,s.sha256 AS source_sha256 '
                    'FROM event e JOIN source s ON s.id=e.source_id '
                    "WHERE e.subject=? AND e.kind='weight' ORDER BY e.id", (bid,))]
                if len(rows) != 1 or 'freight_rate_cents' not in json.loads(rows[0]['payload']):
                    raise ValueError('confidential_rate_source_missing')
                records.extend(rows)
            payload = json.dumps({'fixture': str(source), 'digest': hashlib.sha256(raw).hexdigest(),
                                  'doc': doc, 'records': records},
                                 sort_keys=True, separators=(',', ':'))
            old = db.execute('SELECT payload FROM privacy_case WHERE id=?', (case_id,)).fetchone()
            if old and old['payload'] != payload:
                raise ValueError('privacy_case_conflict')
            db.execute('INSERT OR IGNORE INTO privacy_case VALUES (?,?)', (case_id, payload))

            seeded = 0
            for prior in doc.get('prior_releases', []):
                seeded += _seed_prior(db, case_id, doc, records, prior)
    finally:
        db.close()
    return {'case_id': case_id, 'boxes': len(doc['boxes']),
            'prior_releases': len(doc.get('prior_releases', []))}


def _seed_prior(db, case_id, doc, records, prior):
    audience = prior['audience']
    contract = next((cid for cid, policy in CONTRACTS.items()
                     if policy['audience'] == audience), None)
    if contract is None:
        raise ValueError('prior_release_unknown_audience')
    rates = {rec['subject']: int(json.loads(rec['payload'])['freight_rate_cents'])
             for rec in records}
    members = sorted(prior['boxes'])
    if any(b not in rates for b in members):
        raise ValueError('prior_release_unknown_box')
    if int(prior['sum_cents']) != sum(rates[b] for b in members):
        # Do not embed either number in the message.
        raise ValueError('prior_sum_conflicts_with_source')
    source_hashes = sorted({rec['source_sha256'] for rec in records})
    source_hash = hashlib.sha256(''.join(source_hashes).encode()).hexdigest()
    case = {'case_id': case_id, 'doc': doc, 'source_hash': source_hash}
    amounts = {'total_cents': int(prior['sum_cents'])}
    identity, _ = release_identity(case, audience, contract, 'aggregate',
                                   members, amounts)
    if db.execute('SELECT 1 FROM release_ledger WHERE release_id=?',
                  (identity,)).fetchone():
        return 0
    ts = now_iso()
    db.execute(
        'INSERT INTO release_ledger(release_id,case_id,external_ref,audience,'
        'contract,source_version,source_hash,kind,members_json,amounts_json,'
        'content_json,content_sha256,json_path,md_path,status,reason_code,'
        'reason_detail_json,evidence_json,is_prior,created_at,updated_at) '
        "VALUES (?,?,?,?,?,?,?,?,?,?,NULL,NULL,NULL,NULL,'committed',NULL,NULL,"
        "'{\"seeded_prior\":true}',1,?,?)",
        (identity, case_id, prior['id'], audience, contract,
         prior.get('source_version', doc.get('source_version', 'v1')),
         source_hash, 'aggregate', json.dumps(members, separators=(',', ':')),
         json.dumps(amounts, sort_keys=True, separators=(',', ':')), ts, ts))
    db.execute('INSERT INTO release_event(release_id,phase,occurred_at,detail) '
               'VALUES (?,?,?,?)',
               (identity, 'seed_prior', ts,
                json.dumps({'external_ref': prior['id'], 'audience': audience})))
    return 1
