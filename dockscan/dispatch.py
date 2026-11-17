"""Freeze accepted inbound boxes and outbound vehicle offers."""
import hashlib
import json
from pathlib import Path
from .store import connect


def register(db_path, case_id, fixture):
    source = Path(fixture).resolve()
    raw = source.read_bytes()
    doc = json.loads(raw)
    if not case_id or not doc.get('boxes') or not doc.get('vehicles') or not doc.get('contracts'):
        raise ValueError('bad_dispatch_case')
    db = connect(db_path)
    try:
        with db:
            boxes = []
            for box in doc['boxes']:
                bid = box['box']
                decision = db.execute('SELECT * FROM decision WHERE subject=?', (bid,)).fetchone()
                if decision is None or decision['status'] != 'accepted':
                    raise ValueError('dispatch_box_not_accepted')
                events = [dict(row) for row in db.execute(
                    'SELECT e.id,e.kind,e.time,e.sha256,s.sha256 AS source_sha256 '
                    'FROM event e JOIN source s ON s.id=e.source_id '
                    'WHERE e.subject=? ORDER BY e.time,e.id', (bid,))]
                boxes.append({'box': bid, 'decision': dict(decision), 'events': events})
            payload = json.dumps({'fixture': str(source), 'digest': hashlib.sha256(raw).hexdigest(),
                                  'doc': doc, 'boxes': boxes}, sort_keys=True, separators=(',', ':'))
            old = db.execute('SELECT payload FROM dispatch_case WHERE id=?', (case_id,)).fetchone()
            if old and old['payload'] != payload:
                raise ValueError('dispatch_case_conflict')
            db.execute('INSERT OR IGNORE INTO dispatch_case VALUES (?,?)', (case_id, payload))
    finally:
        db.close()
    return {'case_id': case_id, 'boxes': len(boxes), 'vehicles': len(doc['vehicles'])}
