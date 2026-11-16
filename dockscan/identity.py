def known_box(box, orders):
    return box in orders


def valid_lineage(box, parts, quantity):
    return (sum(int(part['quantity']) for part in parts) == quantity
            and all(int(part['quantity']) > 0 and part['id'] != box for part in parts))


def lineage_participants(events):
    """收集出现在 split/merge 关系中的全部箱号（含子箱与父箱）。"""
    boxes = set()
    for e in events:
        if e['kind'] == 'split':
            boxes.update(str(p['id']) for p in e.get('v', {}).get('children', []))
        elif e['kind'] == 'merge':
            boxes.update(str(p['id']) for p in e.get('v', {}).get('parents', []))
    return boxes
