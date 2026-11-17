"""Freeze confidential source rows and an existing aggregate release ledger."""
import hashlib
import json
from pathlib import Path
from .store import connect


def register(db_path, case_id, fixture):
    source = Path(fixture).resolve()
    raw = source.read_bytes()
    doc = json.loads(raw)
    if not case_id or not doc.get('prior_releases') or not doc.get('proposed') or not doc.get('contracts'):
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
                                  'doc': doc, 'records': records}, sort_keys=True, separators=(',', ':'))
            old = db.execute('SELECT payload FROM privacy_case WHERE id=?', (case_id,)).fetchone()
            if old and old['payload'] != payload:
                raise ValueError('privacy_case_conflict')
            db.execute('INSERT OR IGNORE INTO privacy_case VALUES (?,?)', (case_id, payload))
    finally:
        db.close()
    return {'case_id': case_id, 'boxes': len(doc['boxes']),
            'prior_releases': len(doc['prior_releases'])}
