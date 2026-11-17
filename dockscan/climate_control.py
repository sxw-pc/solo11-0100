"""Freeze real dock identities, local sensors, and actuator contract inputs."""
import csv
import hashlib
import io
import json
from pathlib import Path
from .store import connect


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
    finally:
        db.close()
    return {'case_id': case_id, 'docks': len(observed), 'periods': len(doc['periods'])}
