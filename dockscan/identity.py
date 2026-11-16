from decimal import Decimal, ROUND_CEILING


def known_box(box, orders):
    return box in orders


def valid_lineage(box, parts, quantity):
    return (sum(int(part['quantity']) for part in parts) == quantity
            and all(int(part['quantity']) > 0 and part['id'] != box for part in parts))


def ceiling_round(value, unit):
    """Round value up to the nearest whole multiple of the minimum billing unit."""
    d = value if isinstance(value, Decimal) else Decimal(str(value))
    u = unit if isinstance(unit, Decimal) else Decimal(str(unit))
    if u <= 0:
        raise ValueError('billing_unit_must_be_positive')
    return (d / u).to_integral_value(rounding=ROUND_CEILING) * u


def build_lineage_edges(events):
    """Turn split/merge events into directed parent->child edges.

    A split event's subject is the parent; a merge event's subject is the
    merged child. Every edge keeps its quantity, kind and source event id so
    bills can be traced back to the original weighing / lineage evidence.
    """
    edges = []
    for e in events:
        kind = e['kind']
        if kind == 'split':
            for part in e['payload'].get('children', []):
                edges.append({'parent': e['subject'], 'child': str(part['id']),
                              'quantity': int(part['quantity']), 'kind': kind, 'event_id': e['id']})
        elif kind == 'merge':
            for part in e['payload'].get('parents', []):
                edges.append({'parent': str(part['id']), 'child': e['subject'],
                              'quantity': int(part['quantity']), 'kind': kind, 'event_id': e['id']})
    return edges


def adjacency(edges):
    """Return (outgoing, incoming) maps of box -> neighbour boxes."""
    out, back = {}, {}
    for edge in edges:
        out.setdefault(edge['parent'], set()).add(edge['child'])
        back.setdefault(edge['child'], set()).add(edge['parent'])
    return out, back


def duplicate_parts(edges):
    """A part repeated inside one lineage event (e.g. a leaf listed twice)."""
    seen = set()
    hits = []
    for edge in edges:
        key = (edge['event_id'], edge['parent'], edge['child'])
        if key in seen:
            hits.append({'box': edge['child'], 'event_id': edge['event_id']})
        seen.add(key)
    return hits


def divergent_references(edges):
    """Boxes whose contents flow into two different lineage events.

    Every physical box may be created once and consumed once. Feeding two
    events (a child entering two merge branches, two splits of the same box,
    or the same box created by two events) is a reuse violation rather than
    something to be silently averaged away. A merge result legitimately has
    several incoming edges from the *same* merge event.
    """
    out_events, in_events = {}, {}
    for edge in edges:
        out_events.setdefault(edge['parent'], set()).add(edge['event_id'])
        in_events.setdefault(edge['child'], set()).add(edge['event_id'])
    hits = []
    for box, event_ids in out_events.items():
        if len(event_ids) > 1:
            hits.append({'box': box, 'event_ids': sorted(event_ids), 'direction': 'outgoing'})
    for box, event_ids in in_events.items():
        if len(event_ids) > 1:
            hits.append({'box': box, 'event_ids': sorted(event_ids), 'direction': 'incoming'})
    return hits


def find_cycles(edges):
    """Return the boxes participating in a directed lineage cycle, if any."""
    out, _ = adjacency(edges)
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {box: WHITE for box in set(out) | {c for cs in out.values() for c in cs}}
    cyclic = set()

    def visit(box, stack):
        color[box] = GRAY
        stack.append(box)
        for nxt in out.get(box, ()):
            if color.get(nxt) == GRAY:
                cyclic.update(stack[stack.index(nxt):])
            elif color.get(nxt) == WHITE:
                visit(nxt, stack)
        stack.pop()
        color[box] = BLACK

    for box in list(color):
        if color[box] == WHITE:
            visit(box, [])
    return sorted(cyclic)


def reachable_boxes(edges, root):
    """All boxes downstream of the order root following parent->child edges."""
    out, _ = adjacency(edges)
    seen, stack = {root}, [root]
    while stack:
        box = stack.pop()
        for nxt in out.get(box, ()):
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return seen


def leaf_boxes(edges, root):
    """Reachable boxes that never act as a parent (final shipping boxes)."""
    out, _ = adjacency(edges)
    return sorted(box for box in reachable_boxes(edges, root) if not out.get(box))


def topological_order(edges, root):
    """Top-down ordering (Kahn) restricted to the root's reachable graph."""
    out, back = adjacency(edges)
    scope = reachable_boxes(edges, root)
    indegree = {box: len(back.get(box, set()) & scope) for box in scope}
    ready = [box for box in scope if indegree[box] == 0]
    order = []
    while ready:
        box = ready.pop()
        order.append(box)
        for nxt in out.get(box, ()):
            if nxt in scope:
                indegree[nxt] -= 1
                if indegree[nxt] == 0:
                    ready.append(nxt)
    if len(order) != len(scope):
        raise ValueError('lineage_cycle')
    return order


def apportion_tare(edges, tares, root):
    """Estimate each leaf's share of ancestor packaging tare.

    At every node the accumulated tare pool plus the node's own tare is split
    across its outgoing edges in proportion to declared quantity; merge nodes
    simply receive the pools of every incoming branch. Returns
    {leaf_box: Decimal apportioned kg} and the per-node pools for auditing.
    """
    out, _ = adjacency(edges)
    q_by_pair = {(e['parent'], e['child']): e['quantity'] for e in edges}
    pools = {root: Decimal('0')}
    for box in topological_order(edges, root):
        distributable = pools.get(box, Decimal('0')) + Decimal(str(tares.get(box, 0)))
        children = out.get(box, set())
        if not children:
            pools[box] = distributable
            continue
        total_q = sum(q_by_pair[(box, child)] for child in children)
        for child in children:
            share = distributable * (Decimal(q_by_pair[(box, child)]) / Decimal(total_q))
            pools[child] = pools.get(child, Decimal('0')) + share
    return {leaf: pools[leaf] for leaf in leaf_boxes(edges, root)}, pools
