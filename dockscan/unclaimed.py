"""Custody-chain verification and one-time counter release for an unclaimed pallet.

The original ``register`` only froze the fixture.  This module now also:

* rebuilds and validates the continuous custody chain (store -> signed
  two-party handoff -> seal recheck), requiring every step to name the same
  pallet and the same seal;
* binds pallet/source identity to the frozen ``ingest`` rows so a release can
  only cite a pallet and scans the ledger actually knows;
* verifies exactly one exclusive pickup contract per case
  (``registered_consignor_release`` or ``bearer_receipt_release``);
* consumes the pallet's state atomically, once, on delivery, recording the
  current deliverer, claimant, credential digest, seal and source events.

Nothing here treats the original receiving ``accepted`` decision as pickup
authorization.  Issuing an authorization never moves the pallet: only
``deliver`` does, and only while the authorization still matches the current
pallet version and custody fingerprint.
"""
import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from .ingest import hash_bytes, pallet_identity, source_events, source_fingerprint, timestamp
from .store import connect

CONTRACTS = ('registered_consignor_release', 'bearer_receipt_release')

# Stable problem codes surfaced in verification and reports.
CHAIN_EMPTY = 'custody_chain_empty'
CHAIN_START = 'custody_start_not_stored'
CHAIN_GAP = 'custody_handoff_gap'
CHAIN_TIME = 'custody_time_not_increasing'
CHAIN_SIGNATURE = 'custody_signature_missing'
CHAIN_DELIVERER = 'custody_deliverer_unknown'
CHAIN_SEAL_CHANGED = 'custody_seal_changed'
CHAIN_PALLET = 'custody_pallet_mismatch'
CHAIN_SEAL_CHECK = 'custody_seal_check_missing'
IDENTITY_CONFLICT = 'pallet_identity_conflict'
ALREADY_RELEASED = 'pallet_already_released'
CONTRACT_AMBIGUOUS = 'contract_ambiguous'
CONTRACT_UNSUPPORTED = 'contract_unsupported'
CREDENTIAL_MISSING = 'credential_missing'
CREDENTIAL_MISMATCH = 'credential_mismatch'
CREDENTIAL_REUSED = 'credential_reused'
MANIFEST_MISMATCH = 'manifest_pallet_mismatch'
CLAIMANT_MISMATCH = 'claimant_mismatch'
NOT_REGISTERED = 'case_not_registered'
AUTH_NOT_FOUND = 'authorization_not_found'
AUTH_STALE = 'authorization_stale'
AUTH_CONSUMED = 'authorization_already_consumed'
AUTH_WRONG_CASE = 'authorization_wrong_case'
DELIVERER_CHANGED = 'deliverer_changed'
SEAL_MISMATCH = 'seal_mismatch'
CONTRACT_MISMATCH = 'contract_mismatch'
PALLET_BOUND_ELSEWHERE = 'pallet_bound_elsewhere'


def _now():
    return datetime.now(timezone.utc).isoformat()


def _next_time(steps):
    """A runtime step must sort strictly after the frozen chain."""
    now = datetime.now(timezone.utc)
    if steps:
        last = timestamp(steps[-1]['at'])
        if now <= last:
            now = last + timedelta(seconds=1)
    return now.isoformat()

def _digest(value):
    return 'sha256:' + hash_bytes(str(value).encode())


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def _issue(db, code, detail):
    exists = db.execute('SELECT 1 FROM issue WHERE code=? AND detail=?', (code, detail)).fetchone()
    if not exists:
        db.execute('INSERT INTO issue(code,detail) VALUES (?,?)', (code, detail))


# ---------------------------------------------------------------------------
# Custody chain
# ---------------------------------------------------------------------------

def _fixture_custody(doc):
    steps = []
    for raw in doc.get('custody') or []:
        steps.append({
            'custody_id': str(raw['id']),
            'kind': raw['kind'],
            'at': str(raw['at']),
            'from_holder': raw.get('from'),
            'to_holder': raw.get('to'),
            'by_holder': raw.get('by'),
            'seal': str(raw.get('seal') or ''),
            'signed_by': [str(x) for x in raw.get('signed_by') or []],
            'pallet': str(raw.get('pallet') or doc.get('pallet_id')),
            'origin': 'fixture',
            'event_id': None,
        })
    steps.sort(key=lambda step: (timestamp(step['at']), step['custody_id']))
    return steps


def _validate_chain(pallet, seal, steps, require_seal_check=True):
    """Return (problems, current_holder).

    Every step must be on the same pallet, carry the original seal, and be
    signed by the parties the transition requires.  ``current_holder`` is the
    only custodian allowed to deliver.
    """
    problems = []
    if not steps:
        return [CHAIN_EMPTY], None
    holder = None
    previous_time = None
    for position, step in enumerate(steps):
        if step['pallet'] != pallet:
            problems.append(CHAIN_PALLET)
        if step['seal'] != seal:
            problems.append(CHAIN_SEAL_CHANGED)
        signed = set(step['signed_by'])
        if step['kind'] == 'store':
            if step['to_holder'] is None or step['to_holder'] not in signed:
                problems.append(CHAIN_SIGNATURE)
            if position != 0:
                problems.append(CHAIN_START)
            holder = step['to_holder']
        elif step['kind'] == 'handoff':
            if not step['from_holder'] or not step['to_holder']:
                problems.append(CHAIN_DELIVERER)
            elif step['from_holder'] != holder:
                problems.append(CHAIN_DELIVERER)
            if step['from_holder'] not in signed or step['to_holder'] not in signed:
                problems.append(CHAIN_SIGNATURE)
            holder = step['to_holder']
        elif step['kind'] == 'seal_check':
            if step['by_holder'] is None or step['by_holder'] not in signed:
                problems.append(CHAIN_SIGNATURE)
            elif step['by_holder'] != holder:
                problems.append(CHAIN_DELIVERER)
        elif step['kind'] == 'release':
            if not step['from_holder'] or not step['to_holder']:
                problems.append(CHAIN_DELIVERER)
            elif step['from_holder'] != holder:
                problems.append(CHAIN_DELIVERER)
            if step['from_holder'] not in signed or step['to_holder'] not in signed:
                problems.append(CHAIN_SIGNATURE)
            holder = step['to_holder']
        else:
            problems.append(CHAIN_GAP)
        if step['kind'] in ('store', 'handoff', 'release') and step['to_holder'] is None:
            problems.append(CHAIN_GAP)
        at = timestamp(step['at'])
        if previous_time is not None and at <= previous_time:
            problems.append(CHAIN_TIME)
        previous_time = at
    if require_seal_check and not any(s['kind'] == 'seal_check' for s in steps):
        problems.append(CHAIN_SEAL_CHECK)
    return sorted(set(problems)), holder


def _load_custody(db, case_id):
    rows = db.execute('SELECT * FROM unclaimed_custody WHERE case_id=? ORDER BY seq,id', (case_id,)).fetchall()
    steps = []
    for row in rows:
        steps.append({
            'seq': row['seq'], 'custody_id': row['custody_id'], 'kind': row['kind'], 'at': row['at'],
            'from_holder': row['from_holder'], 'to_holder': row['to_holder'], 'by_holder': row['by_holder'],
            'seal': row['seal'], 'signed_by': json.loads(row['signed_by']), 'pallet': row['pallet'],
            'origin': row['origin'], 'event_id': row['event_id'],
        })
    return steps


def _custody_fingerprint(case_id, steps):
    material = _canonical({'case': case_id, 'steps': [
        [s['seq'], s['custody_id'], s['kind'], s['at'], s['from_holder'], s['to_holder'],
         s['by_holder'], s['seal'], s['signed_by'], s['pallet']] for s in steps]})
    return hash_bytes(material.encode())


def _persist_custody(db, case_id, pallet, steps):
    if db.execute('SELECT 1 FROM unclaimed_custody WHERE case_id=? LIMIT 1', (case_id,)).fetchone():
        return
    for seq, step in enumerate(steps, 1):
        db.execute(
            'INSERT INTO unclaimed_custody(case_id,seq,custody_id,kind,at,from_holder,to_holder,by_holder,'
            'seal,signed_by,pallet,origin,event_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (case_id, seq, step['custody_id'], step['kind'], step['at'], step['from_holder'],
             step['to_holder'], step['by_holder'], step['seal'], json.dumps(step['signed_by'], sort_keys=True),
             step['pallet'], step['origin'], step['event_id']))


def _case(db, case_id):
    row = db.execute('SELECT payload FROM unclaimed_case WHERE id=?', (case_id,)).fetchone()
    if row is None:
        raise ValueError(NOT_REGISTERED)
    return json.loads(row['payload'])


def _pallet_row(db, case_id):
    return db.execute('SELECT * FROM pallet_custody WHERE case_id=?', (case_id,)).fetchone()


def _contract_and_claims(doc, contract=None):
    """Pick exactly one executable contract, else fail closed.

    A case may declare both contract *options*; the counter must choose one
    exclusive contract per delivery attempt.  Without a choice, two declared
    contracts are ambiguous and nothing is authorized.
    """
    declared = list(doc.get('contracts') or [])
    if contract is None:
        if len(declared) != 1:
            return None, CONTRACT_AMBIGUOUS
        contract = declared[0]
    if contract not in CONTRACTS or contract not in declared:
        return None, CONTRACT_UNSUPPORTED
    basis = 'registered_consignor' if contract == 'registered_consignor_release' else 'warehouse_receipt'
    claims = [c for c in doc.get('claims') or [] if c.get('basis') == basis]
    if len(claims) != 1:
        return None, CONTRACT_AMBIGUOUS
    return (contract, claims[0]), None


def _check_credential(db, contract, claim, doc, pallet, case_id):
    """Check the claimant's proof without ever storing its reusable text."""
    if contract == 'registered_consignor_release':
        if claim.get('manifest_pallet') != pallet:
            return MANIFEST_MISMATCH, None
        if claim.get('claimant') != doc.get('registered_consignor'):
            return CLAIMANT_MISMATCH, None
        reference = _digest(_canonical({'basis': 'registered_consignor', 'claimant': claim['claimant'],
                                        'manifest_pallet': pallet}))
        return None, reference
    presented = claim.get('presented_digest')
    expected = doc.get('receipt_digest')
    if not presented or not expected:
        return CREDENTIAL_MISSING, None
    normalized = str(presented).strip().lower()
    if normalized != str(expected).strip().lower():
        return CREDENTIAL_MISMATCH, None
    # A receipt is a bearer instrument: the same digest may not have been
    # presented for any other case already.  Re-presenting it for the same
    # case is handled as "already released", not as credential reuse.
    reused = db.execute(
        "SELECT 1 FROM unclaimed_authorization WHERE credential_digest=? AND case_id<>? "
        "UNION SELECT 1 FROM unclaimed_release WHERE credential_digest=? AND case_id<>?",
        (normalized, case_id, normalized, case_id)).fetchone()
    if reused:
        return CREDENTIAL_REUSED, None
    return None, normalized


def _status_payload(case_id, row, problems, contract_info, doc, checks, chain_valid=None):
    contract = contract_info[0] if contract_info else None
    return {
        'case_id': case_id,
        'pallet': doc.get('pallet_id'),
        'seal': doc.get('seal'),
        'status': row['status'] if row else 'missing',
        'holder': row['holder'] if row else None,
        'version': row['version'] if row else None,
        'release_id': row['release_id'] if row else None,
        'contract': contract,
        'declared_contracts': list(doc.get('contracts') or []),
        'custody_valid': (not problems) if chain_valid is None else chain_valid,
        'custody_problems': problems,
        'checks': checks,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def register(db_path, case_id, fixture):
    """Freeze the fixture, its ingest identity, custody chain, and pallet state."""
    source = Path(fixture).resolve()
    raw = source.read_bytes()
    doc = json.loads(raw)
    if not case_id or not doc.get('pallet_id') or not doc.get('custody') or not doc.get('claims'):
        raise ValueError('bad_unclaimed_case')
    pallet = str(doc['pallet_id'])
    seal = str(doc.get('seal') or '')
    if not seal:
        raise ValueError('bad_unclaimed_case')
    db = connect(db_path)
    try:
        with db:
            boxes = pallet_identity(db, pallet)
            ids = [box['box'] for box in boxes]
            events = source_events(db, ids)
            if not events:
                raise ValueError('unclaimed_source_events_missing')
            steps = _fixture_custody(doc)
            chain_problems, holder = _validate_chain(pallet, seal, steps)
            payload = _canonical({'fixture': str(source), 'digest': hashlib.sha256(raw).hexdigest(),
                                  'doc': doc, 'boxes': boxes, 'events': events})
            old = db.execute('SELECT payload FROM unclaimed_case WHERE id=?', (case_id,)).fetchone()
            if old and old['payload'] != payload:
                raise ValueError('unclaimed_case_conflict')
            bound = db.execute('SELECT case_id FROM pallet_custody WHERE pallet=? AND case_id<>?',
                               (pallet, case_id)).fetchone()
            if bound:
                raise ValueError(PALLET_BOUND_ELSEWHERE)
            db.execute('INSERT OR IGNORE INTO unclaimed_case VALUES (?,?)', (case_id, payload))
            _persist_custody(db, case_id, pallet, steps)
            existing = _pallet_row(db, case_id)
            if existing is None:
                status = 'blocked' if chain_problems else 'in_custody'
                reason = chain_problems[0] if chain_problems else None
                db.execute(
                    'INSERT INTO pallet_custody(pallet,case_id,holder,status,seal,version,reason,'
                    'source_fingerprint,updated_at,release_id) VALUES (?,?,?,?,?,?,?,?,?,NULL)',
                    (pallet, case_id, holder, status, seal, 1, reason,
                     source_fingerprint(pallet, events), _now()))
    finally:
        db.close()
    return {'case_id': case_id, 'boxes': len(boxes), 'claims': len(doc['claims'])}


def verify(db_path, case_id, claimant=None, contract=None):
    """Verify custody and the exclusive contract; never moves the pallet.

    Returns a summary with ``custody_valid``, the chosen ``contract`` and, when
    a claimant is supplied, whether that claimant is eligible.  Problems never
    raise: they are reported so the counter must refuse rather than guess.
    """
    db = connect(db_path)
    try:
        with db:
            state = _verification_state(db, case_id, contract)
            doc, pallet, row = state['doc'], state['pallet'], state['row']
            contract_info, contract_error = state['contract_info'], state['contract_error']
            identity_ok = state['identity_ok']
            if not identity_ok:
                _issue(db, IDENTITY_CONFLICT, case_id)
            checks = {}
            chosen = None
            if claimant is not None and contract_info:
                active_contract, claim = contract_info
                if claim.get('claimant') == claimant:
                    error, digest = _check_credential(db, active_contract, claim, doc, pallet, case_id)
                    checks = {'credential_valid': error is None, 'problem': error,
                              'credential_digest': digest}
                    chosen = {'contract': active_contract, 'claimant': claimant, 'claim_id': claim['id'],
                              'credential_digest': digest}
                else:
                    checks = {'credential_valid': False, 'problem': CLAIMANT_MISMATCH,
                              'credential_digest': None}
            elif claimant is not None:
                checks = {'credential_valid': False, 'problem': contract_error, 'credential_digest': None}
            chain_ok = not state['chain_problems']
            lifecycle_ok = row is not None and row['status'] != 'released'
            checks['eligible'] = bool(checks.get('credential_valid') and chain_ok and lifecycle_ok
                                      and identity_ok)
            if not lifecycle_ok and checks.get('credential_valid'):
                checks['problem'] = ALREADY_RELEASED
            problems = list(state['chain_problems'])
            if contract_error:
                problems.append(contract_error)
            if not identity_ok:
                problems.append(IDENTITY_CONFLICT)
            if row is not None and row['status'] == 'released':
                problems.append(ALREADY_RELEASED)
            result = _status_payload(case_id, row, problems, contract_info, doc, checks,
                                     chain_valid=not state['chain_problems'])
            result['eligible'] = checks.get('eligible', False)
            result['claimant'] = claimant
            result['chosen'] = chosen
            result['identity_valid'] = identity_ok
            result['contract_problem'] = contract_error
            return result
    finally:
        db.close()


def verify_claimants(db_path, case_id, claimants=None):
    """Evaluate every claimant under both exclusive contracts, side by side.

    This is what demonstrates the exclusivity: under the registered-consignor
    contract SHIPPER-A is eligible and BEARER-B is not; under the bearer
    contract the reverse.  Neither result authorizes anything by itself.
    """
    db = connect(db_path)
    try:
        frozen = _case(db, case_id)
        doc = frozen['doc']
        names = claimants or sorted({c.get('claimant') for c in doc.get('claims') or []})
        outcomes = {}
        for contract in CONTRACTS:
            outcomes[contract] = {
                name: verify(db_path, case_id, name, contract) for name in names}
        return {'case_id': case_id, 'claims': outcomes}
    finally:
        db.close()


def _verification_state(db, case_id, contract=None):
    """Shared read-only verification used by authorize/status/report."""
    frozen = _case(db, case_id)
    doc = frozen['doc']
    pallet = str(doc['pallet_id'])
    seal = str(doc.get('seal') or '')
    steps = _load_custody(db, case_id)
    chain_problems, holder = _validate_chain(pallet, seal, steps)
    row = _pallet_row(db, case_id)
    contract_info, contract_error = _contract_and_claims(doc, contract)
    events = source_events(db, sorted(box['box'] for box in frozen['boxes']))
    identity_ok = bool(events) and source_fingerprint(pallet, events) == row['source_fingerprint'] \
        if row is not None else False
    return {'frozen': frozen, 'doc': doc, 'pallet': pallet, 'seal': seal, 'steps': steps,
            'chain_problems': chain_problems, 'holder': holder, 'row': row,
            'contract_info': contract_info, 'contract_error': contract_error,
            'identity_ok': identity_ok, 'events': events}


def _blockers(state):
    problems = list(state['chain_problems'])
    if state['contract_error']:
        problems.append(state['contract_error'])
    if not state['identity_ok']:
        problems.append(IDENTITY_CONFLICT)
    if state['row'] is None:
        problems.append(NOT_REGISTERED)
    elif state['row']['status'] == 'released':
        problems.append(ALREADY_RELEASED)
    elif state['holder'] != state['row']['holder']:
        problems.append(CHAIN_DELIVERER)
    return sorted(set(problems))


def authorize(db_path, case_id, claimant, contract=None):
    """Authorize one named claimant under the chosen exclusive contract.

    The authorization is bound to the current pallet version and custody
    fingerprint.  It is not a delivery: ``deliver`` still re-checks both.
    """
    db = connect(db_path)
    try:
        with db:
            state = _verification_state(db, case_id, contract)
            problems = _blockers(state)
            if problems:
                for code in problems:
                    _issue(db, code, case_id)
                return {'ok': False, 'problems': problems, 'authorization': None}
            contract_name, claim = state['contract_info']
            if claim.get('claimant') != claimant:
                return {'ok': False, 'problems': [CLAIMANT_MISMATCH], 'authorization': None}
            error, digest = _check_credential(db, contract_name, claim, state['doc'], state['pallet'], case_id)
            if error:
                _issue(db, error, case_id)
                return {'ok': False, 'problems': [error], 'authorization': None}
            existing = db.execute(
                "SELECT * FROM unclaimed_authorization WHERE case_id=? AND claim_id=? AND state='active'",
                (case_id, claim['id'])).fetchone()
            if existing:
                return {'ok': True, 'problems': [], 'authorization': existing['id'], 'reused': True}
            auth_id = f'{case_id}:{claim["id"]}:v{state["row"]["version"]}'
            db.execute(
                'INSERT INTO unclaimed_authorization(id,case_id,contract,claim_id,claimant,credential_digest,seal,'
                'deliverer,custody_version,custody_fingerprint,source_event_ids,state,created_at,consumed_at) '
                'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)',
                (auth_id, case_id, contract_name, claim['id'], claimant, digest, state['seal'], state['holder'],
                 state['row']['version'], _custody_fingerprint(case_id, state['steps']),
                 _canonical([e['id'] for e in state['events']]), 'active', _now()))
            return {'ok': True, 'problems': [], 'authorization': auth_id, 'reused': False}
    finally:
        db.close()


def record_handoff(db_path, case_id, custody_id, from_holder, to_holder, seal, signed_by):
    """Append a runtime two-party shift handover to the frozen chain.

    A handover is only accepted from the current holder, signed by both
    parties, on the same pallet and original seal.  It advances the pallet
    version and invalidates every outstanding authorization by construction.
    """
    db = connect(db_path)
    try:
        with db:
            state = _verification_state(db, case_id)
            problems = list(state['chain_problems'])
            if from_holder != state['holder'] or state['row'] is None or state['row']['holder'] != state['holder']:
                problems.append(CHAIN_DELIVERER)
            if seal != state['seal']:
                problems.append(CHAIN_SEAL_CHANGED)
            if not to_holder or from_holder not in set(signed_by) or to_holder not in set(signed_by):
                problems.append(CHAIN_SIGNATURE)
            if state['row'] is not None and state['row']['status'] == 'released':
                problems.append(ALREADY_RELEASED)
            if problems:
                return {'ok': False, 'problems': sorted(set(problems))}
            at = _next_time(state['steps'])
            seq = int(db.execute('SELECT COALESCE(MAX(seq),0)+1 FROM unclaimed_custody WHERE case_id=?',
                                 (case_id,)).fetchone()[0])
            db.execute(
                'INSERT INTO unclaimed_custody(case_id,seq,custody_id,kind,at,from_holder,to_holder,by_holder,'
                'seal,signed_by,pallet,origin,event_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,NULL)',
                (case_id, seq, custody_id, 'handoff', at, from_holder, to_holder, None, seal,
                 json.dumps([from_holder, to_holder], sort_keys=True), state['pallet'], 'runtime'))
            db.execute("UPDATE unclaimed_authorization SET state='stale' WHERE case_id=? AND state='active'",
                       (case_id,))
            db.execute('UPDATE pallet_custody SET holder=?,version=version+1,updated_at=? WHERE case_id=?',
                       (to_holder, at, case_id))
            version = db.execute('SELECT version FROM pallet_custody WHERE case_id=?', (case_id,)).fetchone()['version']
            return {'ok': True, 'problems': [], 'holder': to_holder, 'version': version}
    finally:
        db.close()


def record_seal_check(db_path, case_id, custody_id, holder, seal):
    """Append a runtime seal recheck; the seal must still be the original."""
    db = connect(db_path)
    try:
        with db:
            state = _verification_state(db, case_id)
            problems = list(state['chain_problems'])
            if seal != state['seal']:
                problems.append(CHAIN_SEAL_CHANGED)
            if state['row'] is None:
                problems.append(NOT_REGISTERED)
            elif holder != state['row']['holder'] or holder != state['holder']:
                problems.append(CHAIN_DELIVERER)
            elif state['row']['status'] == 'released':
                problems.append(ALREADY_RELEASED)
            if problems:
                return {'ok': False, 'problems': sorted(set(problems))}
            at = _next_time(state['steps'])
            seq = int(db.execute('SELECT COALESCE(MAX(seq),0)+1 FROM unclaimed_custody WHERE case_id=?',
                                 (case_id,)).fetchone()[0])
            db.execute(
                'INSERT INTO unclaimed_custody(case_id,seq,custody_id,kind,at,from_holder,to_holder,by_holder,'
                'seal,signed_by,pallet,origin,event_id) VALUES (?,?,?,?,?,NULL,NULL,?,?,?,?,?,NULL)',
                (case_id, seq, custody_id, 'seal_check', at, holder, seal,
                 json.dumps([holder], sort_keys=True), state['pallet'], 'runtime'))
            # A recheck after authorization bumps the version; the counter must
            # re-verify rather than deliver on a now-older authorization.
            db.execute("UPDATE unclaimed_authorization SET state='stale' WHERE case_id=? AND state='active'",
                       (case_id,))
            db.execute('UPDATE pallet_custody SET version=version+1,updated_at=? WHERE case_id=?',
                       (at, case_id))
            version = db.execute('SELECT version FROM pallet_custody WHERE case_id=?', (case_id,)).fetchone()['version']
            return {'ok': True, 'problems': [], 'holder': holder, 'version': version}
    finally:
        db.close()


def deliver(db_path, case_id, authorization_id, deliverer=None, contract=None, commit_hook=None):
    """Atomically consume the pallet and record one physical release.

    The whole read-check-write runs inside one ``BEGIN IMMEDIATE`` transaction
    so two concurrent callers cannot both see an active authorization.  A
    failed check rolls everything back and leaves the authorization retryable;
    a success permanently changes the pallet to ``released``.

    ``commit_hook`` is a test seam: it runs after the writes but before
    ``COMMIT``.  If it raises, the whole transaction rolls back, proving an
    interrupted delivery leaves no half-applied state.
    """
    db = connect(db_path)
    db.isolation_level = None
    problems = []
    release = None
    try:
        db.execute('BEGIN IMMEDIATE')
        try:
            auth = db.execute('SELECT * FROM unclaimed_authorization WHERE id=?', (authorization_id,)).fetchone()
            if auth is None:
                problems.append(AUTH_NOT_FOUND)
            else:
                try:
                    frozen = _case(db, case_id)
                except ValueError:
                    problems.append(NOT_REGISTERED)
                    frozen = None
                if frozen is None:
                    pass
                elif auth['case_id'] != case_id:
                    problems.append(AUTH_WRONG_CASE)
                elif auth['state'] == 'consumed':
                    problems.append(AUTH_CONSUMED)
                elif contract is not None and auth['contract'] != contract:
                    problems.append(CONTRACT_MISMATCH)
                else:
                    doc = frozen['doc']
                    pallet = str(doc['pallet_id'])
                    seal = str(doc.get('seal') or '')
                    steps = _load_custody(db, case_id)
                    chain_problems, holder = _validate_chain(pallet, seal, steps)
                    row = db.execute('SELECT * FROM pallet_custody WHERE case_id=?', (case_id,)).fetchone()
                    events = source_events(db, sorted(box['box'] for box in frozen['boxes']))
                    identity_ok = row is not None and bool(events) and \
                        source_fingerprint(pallet, events) == row['source_fingerprint']
                    if not identity_ok:
                        problems.append(IDENTITY_CONFLICT)
                    if chain_problems:
                        problems.extend(chain_problems)
                    if row is None:
                        problems.append(NOT_REGISTERED)
                    elif row['status'] == 'released':
                        problems.append(ALREADY_RELEASED)
                    if row is not None:
                        if auth['seal'] != row['seal']:
                            problems.append(SEAL_MISMATCH)
                        if auth['custody_version'] != row['version']:
                            problems.append(AUTH_STALE)
                        if auth['custody_fingerprint'] != _custody_fingerprint(case_id, steps):
                            problems.append(AUTH_STALE)
                        if auth['deliverer'] != holder or (deliverer is not None and deliverer != holder):
                            problems.append(DELIVERER_CHANGED)
            if problems:
                db.execute('ROLLBACK')
            else:
                label = deliverer if deliverer is not None else auth['deliverer']
                release_id = f'{case_id}:release'
                now = _now()
                consumed = db.execute(
                    "UPDATE unclaimed_authorization SET state='consumed',consumed_at=? "
                    "WHERE id=? AND state='active'", (now, authorization_id))
                if consumed.rowcount != 1:
                    db.execute('ROLLBACK')
                    problems.append(AUTH_CONSUMED)
                else:
                    changed = db.execute(
                        "UPDATE pallet_custody SET holder=?,status='released',version=version+1,"
                        "reason=NULL,updated_at=?,release_id=? WHERE case_id=? AND status<>'released'",
                        (auth['claimant'], now, release_id, case_id))
                    if changed.rowcount != 1:
                        db.execute('ROLLBACK')
                        problems.append(ALREADY_RELEASED)
                    else:
                        db.execute(
                            'INSERT INTO unclaimed_release(id,case_id,pallet,contract,claim_id,claimant,deliverer,'
                            'credential_digest,seal,custody_version,custody_fingerprint,source_event_ids,'
                            'released_at,authorization_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                            (release_id, case_id, pallet, auth['contract'], auth['claim_id'], auth['claimant'],
                             label, auth['credential_digest'], auth['seal'], auth['custody_version'] + 1,
                             auth['custody_fingerprint'], auth['source_event_ids'], now, authorization_id))
                        seq = int(db.execute('SELECT COALESCE(MAX(seq),0)+1 FROM unclaimed_custody WHERE case_id=?',
                                             (case_id,)).fetchone()[0])
                        chain_at = _next_time(steps)
                        db.execute(
                            'INSERT INTO unclaimed_custody(case_id,seq,custody_id,kind,at,from_holder,to_holder,'
                            'by_holder,seal,signed_by,pallet,origin,event_id) '
                            'VALUES (?,?,?,?,?,?,?,NULL,?,?,?,?,NULL)',
                            (case_id, seq, f'{release_id}:step', 'release', chain_at, label, auth['claimant'],
                             auth['seal'], json.dumps(sorted({label, auth['claimant']})), pallet, 'release'))
                        if commit_hook is not None:
                            commit_hook(db)
                        db.execute('COMMIT')
                        release = {
                            'release_id': release_id, 'case_id': case_id, 'pallet': pallet,
                            'contract': auth['contract'], 'claimant': auth['claimant'], 'deliverer': label,
                            'credential_digest': auth['credential_digest'], 'seal': auth['seal'],
                            'source_event_ids': json.loads(auth['source_event_ids']), 'released_at': now,
                        }
        except Exception:
            try:
                db.execute('ROLLBACK')
            except sqlite3.Error:
                pass
            raise
    finally:
        db.close()
    if problems:
        return {'ok': False, 'problems': sorted(set(problems)), 'release': None}
    return {'ok': True, 'problems': [], 'release': release}


def status(db_path, case_id):
    """Read-only view of the pallet, chain, contract and any release."""
    db = connect(db_path)
    try:
        state = _verification_state(db, case_id)
        row = state['row']
        chain_problems = state['chain_problems']
        extra = []
        if not state['identity_ok']:
            extra.append(IDENTITY_CONFLICT)
        lifecycle = [ALREADY_RELEASED] if row is not None and row['status'] == 'released' else []
        result = _status_payload(case_id, row, sorted(set(chain_problems + extra)),
                                 state['contract_info'], state['doc'], {}, chain_valid=not chain_problems)
        result['lifecycle_problems'] = lifecycle
        result['contract_problem'] = state['contract_error']
        result['identity_valid'] = state['identity_ok']
        result['authorizations'] = [dict(r) for r in db.execute(
            'SELECT * FROM unclaimed_authorization WHERE case_id=? ORDER BY created_at,id', (case_id,))]
        released = db.execute('SELECT * FROM unclaimed_release WHERE case_id=?', (case_id,)).fetchone()
        result['release'] = dict(released) if released else None
        return result
    finally:
        db.close()


def custody_chain(db_path, case_id):
    """Report the frozen chain with a per-step validity verdict."""
    db = connect(db_path)
    try:
        frozen = _case(db, case_id)
        doc = frozen['doc']
        pallet = str(doc['pallet_id'])
        seal = str(doc.get('seal') or '')
        steps = _load_custody(db, case_id)
        problems, holder = _validate_chain(pallet, seal, steps)
        rows = []
        for index, step in enumerate(steps):
            prefix_problems, _ = _validate_chain(pallet, seal, steps[:index + 1], require_seal_check=False)
            rows.append({**step, 'valid_through_here': not prefix_problems})
        return {'case_id': case_id, 'pallet': pallet, 'seal': seal, 'holder': holder,
                'valid': not problems, 'problems': problems, 'steps': rows}
    finally:
        db.close()
