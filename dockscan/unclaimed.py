"""Freeze an unclaimed pallet, custody log, and competing pickup proofs."""
import hashlib
import json
from pathlib import Path
from .store import connect


def register(db_path, case_id, fixture):
    source = Path(fixture).resolve()
    raw = source.read_bytes()
    doc = json.loads(raw)
    if not case_id or not doc.get('pallet_id') or not doc.get('custody') or not doc.get('claims'):
        raise ValueError('bad_unclaimed_case')
    db = connect(db_path)
    try:
        with db:
            row = db.execute("SELECT value FROM meta WHERE key='config_json'").fetchone()
            if row is None:
                raise ValueError('unclaimed_config_missing')
            config = json.loads(row['value'])
            boxes = [box for box in config['boxes'] if box['pallet'] == doc['pallet_id']]
            if not boxes:
                raise ValueError('unknown_unclaimed_pallet')
            ids = [box['box'] for box in boxes]
            events = [dict(event) for event in db.execute(
                'SELECT e.id,e.subject,e.identity,e.kind,e.time,e.sha256,s.sha256 AS source_sha256 '
                'FROM event e JOIN source s ON s.id=e.source_id '
                'WHERE e.subject IN (' + ','.join('?' for _ in ids) + ') ORDER BY e.time,e.id', ids)]
            if not events:
                raise ValueError('unclaimed_source_events_missing')
            payload = json.dumps({'fixture': str(source), 'digest': hashlib.sha256(raw).hexdigest(),
                                  'doc': doc, 'boxes': boxes, 'events': events},
                                 sort_keys=True, separators=(',', ':'))
            old = db.execute('SELECT payload FROM unclaimed_case WHERE id=?', (case_id,)).fetchone()
            if old and old['payload'] != payload:
                raise ValueError('unclaimed_case_conflict')
            db.execute('INSERT OR IGNORE INTO unclaimed_case VALUES (?,?)', (case_id, payload))
    finally:
        db.close()
    return {'case_id': case_id, 'boxes': len(boxes), 'claims': len(doc['claims'])}
