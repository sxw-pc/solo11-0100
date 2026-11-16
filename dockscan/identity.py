def known_box(box, orders):
    return box in orders


def valid_lineage(box, parts, quantity):
    return (sum(int(part['quantity']) for part in parts) == quantity
            and all(int(part['quantity']) > 0 and part['id'] != box for part in parts))
