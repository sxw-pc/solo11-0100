import hashlib,json
from datetime import datetime,timezone
from pathlib import Path
from .store import connect

POLICIES=('dock_window_exclusive','certified_bays_parallel')

def hash_bytes(data): return hashlib.sha256(data).hexdigest()
def timestamp(text):
    dt=datetime.fromisoformat(text.replace('Z','+00:00'))
    if dt.tzinfo is None: raise ValueError('timezone_required')
    return dt
def validate_contract(parsed):
    """危险类别与隔离舱契约：策略合法、互斥引用自洽、隔离舱可审计。"""
    policy=parsed.get('safety_policy','dock_window_exclusive')
    if policy not in POLICIES: raise ValueError('invalid_safety_policy')
    classes=parsed.get('hazard_classes',{})
    if not isinstance(classes,dict): raise ValueError('invalid_hazard_classes')
    for name,spec in classes.items():
        for ref in spec.get('incompatible_with',[]):
            if ref not in classes: raise ValueError('unknown_class_in_compatibility')
    seen=set()
    for bay in parsed.get('segregation_bays',[]):
        bay_id=bay.get('bay')
        if not bay_id or bay_id in seen: raise ValueError('duplicate_or_missing_bay')
        seen.add(bay_id)
        if not bay.get('dock'): raise ValueError('bay_missing_dock')
        if timestamp(bay['certified_from'])>timestamp(bay['certified_until']): raise ValueError('bay_certification_inverted')
        for cls in bay.get('classes',[]):
            if cls not in classes: raise ValueError('unknown_class_in_bay')
def freeze_config(db_path,config):
    raw=Path(config).read_bytes();digest=hash_bytes(raw); db=connect(db_path)
    old=db.execute("SELECT value FROM meta WHERE key='config_hash'").fetchone()
    if old and old['value']!=digest:
        db.close()
        raise ValueError('frozen_config_conflict')
    parsed=json.loads(raw)
    validate_contract(parsed)
    subjects=parsed.get('boxes',parsed.get('stations',parsed.get('recipients',[])))
    ids=[item.get('box',item.get('id')) for item in subjects]
    if not ids or any(not item for item in ids) or len(ids)!=len(set(ids)):
        raise ValueError('duplicate_or_missing_subject')
    with db:
        old=db.execute("SELECT value FROM meta WHERE key='config_hash'").fetchone()
        if old and old['value']!=digest: raise ValueError('frozen_config_conflict')
        db.execute("INSERT OR IGNORE INTO meta VALUES ('config_hash',?)",(digest,))
        db.execute("INSERT OR IGNORE INTO meta VALUES ('config_json',?)",(raw.decode('utf-8'),))
        db.execute("INSERT OR IGNORE INTO batch VALUES ('batch-1',?)",(digest,))
        for item in subjects:
            db.execute("INSERT OR IGNORE INTO subject VALUES (?,?)",(item.get('box',item.get('id')),'batch-1'))
    db.close();return digest
def import_file(db_path,path):
    raw=Path(path).read_bytes(); digest=hash_bytes(raw);db=connect(db_path)
    if db.execute('SELECT 1 FROM source WHERE sha256=?',(digest,)).fetchone():
        db.close();return {'imported':0,'duplicate_file':True}
    count=0
    with db:
        sid=db.execute('INSERT INTO source(path,sha256,raw) VALUES (?,?,?)',(str(path),digest,raw)).lastrowid
        for line_no,line in enumerate(raw.decode('utf-8-sig').splitlines(),1):
            if not line.strip(): continue
            try:
                row=json.loads(line)
                kind=str(row['kind']); subject=str(row['subject']);ts=str(row['time'])
                normalized=timestamp(ts).astimezone(timezone.utc).isoformat()
                identity=str(row.get('identity') or f"{row['device']}:{int(row['seq'])}")
                if not kind or not subject: raise ValueError('missing_kind_or_subject')
                if kind in ('split','merge'):
                    parts=row.get('children') if kind=='split' else row.get('parents')
                    if not isinstance(parts,list) or any(not isinstance(p,dict) or not p.get('id') for p in parts):
                        raise ValueError('invalid_lineage_payload')
                canonical=json.dumps(row,sort_keys=True,ensure_ascii=False,separators=(',',':'))
                h=hash_bytes(canonical.encode())
                old=db.execute('SELECT sha256 FROM event WHERE identity=?',(identity,)).fetchone()
                if old:
                    if old['sha256']!=h: db.execute('INSERT INTO issue(code,detail) VALUES (?,?)',('identity_conflict',identity))
                    continue
                db.execute('INSERT INTO event(source_id,line_no,identity,subject,kind,time,payload,sha256) VALUES (?,?,?,?,?,?,?,?)',(sid,line_no,identity,subject,kind,normalized,canonical,h));count+=1
            except (ValueError,TypeError,KeyError,json.JSONDecodeError) as exc:
                db.execute('INSERT INTO issue(code,detail) VALUES (?,?)',('parse_error',json.dumps({'source_id':sid,'line_no':line_no,'error':str(exc),'raw_line':line},ensure_ascii=False)))
    db.close();return {'imported':count,'duplicate_file':False}
