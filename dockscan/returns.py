"""Freeze authorized reverse parcels against accepted inbound evidence."""
import hashlib
import json
from pathlib import Path
from .store import connect


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
