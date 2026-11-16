from collections import defaultdict

STAGES = ('gate', 'unload', 'check', 'exit')
EXTENSION_CAP = 512


def advance(stage, kind):
    if kind not in STAGES:
        return stage, None
    if stage >= len(STAGES) or kind != STAGES[stage]:
        return stage, 'illegal_transition'
    return stage + 1, None


def build_edges(events, suspect_devices=frozenset()):
    """证据因果边 (前事件id, 后事件id, 依据)。

    三类证据：严格更早的时间戳、同一可信设备的序号递增、拆并箱谱系
    （split 事件先于其子箱事件；父箱事件先于 merge 事件）。
    相同时间戳不产生边；来自冲突设备的序号证据被降级，不产生边。
    """
    edges = []
    ordered = sorted(events, key=lambda e: (e['time'], e['id']))
    for i, a in enumerate(ordered):
        for b in ordered[i + 1:]:
            if a['time'] < b['time']:
                edges.append((a['id'], b['id'], 'time'))
    by_device = defaultdict(list)
    for e in events:
        if e['device'] is not None and e['seq'] is not None and e['device'] not in suspect_devices:
            by_device[e['device']].append(e)
    for group in by_device.values():
        for a in group:
            for b in group:
                if a['seq'] < b['seq']:
                    edges.append((a['id'], b['id'], 'device_seq'))
    by_subject = defaultdict(list)
    for e in events:
        by_subject[e['subject']].append(e)
    for e in events:
        payload = e['payload_dict']
        if e['kind'] == 'split':
            for child in payload.get('children', []):
                for target in by_subject.get(str(child['id']), []):
                    edges.append((e['id'], target['id'], 'lineage_split'))
        elif e['kind'] == 'merge':
            for parent in payload.get('parents', []):
                for source in by_subject.get(str(parent['id']), []):
                    edges.append((source['id'], e['id'], 'lineage_merge'))
    return edges


def reachability(events, edges):
    adj = defaultdict(set)
    for a, b, _ in edges:
        adj[a].add(b)
    reach = {}
    for e in events:
        seen = set()
        stack = list(adj.get(e['id'], ()))
        while stack:
            node = stack.pop()
            if node in seen:
                continue
            seen.add(node)
            stack.extend(adj.get(node, ()))
        reach[e['id']] = seen
    return reach


def cycle_members(reach):
    """证据图中处于环上的事件 id：证据自相矛盾，无可信拓扑顺序。"""
    return {eid for eid, targets in reach.items() if eid in targets}


def linear_extensions(ids, reach, cap=EXTENSION_CAP):
    """证据偏序（限制在 ids 上）的全部线性扩展，受 cap 截断。

    返回 (orders, truncated)；truncated 为真表示未能穷举，不能声称顺序唯一或结论共识。
    """
    ids = sorted(ids)
    idset = set(ids)
    preds = {i: {j for j in idset if j != i and i in reach.get(j, ())} for i in ids}
    orders = []
    truncated = False

    def visit(prefix, remaining):
        nonlocal truncated
        if truncated:
            return
        if not remaining:
            orders.append(tuple(prefix))
            return
        for x in sorted(remaining):
            if preds[x] & remaining:
                continue
            if len(orders) >= cap:
                truncated = True
                return
            visit(prefix + [x], remaining - {x})

    visit([], idset)
    return orders, truncated


def stage_legal(order, by_id):
    stage = 0
    for eid in order:
        kind = by_id[eid]['kind']
        if kind in STAGES:
            stage, error = advance(stage, kind)
            if error:
                return False
    return True


def conclude(order, by_id):
    """单一顺序下的阶段终点、收货数量与末次称重。"""
    stage = 0
    received = 0
    weight = None
    for eid in order:
        event = by_id[eid]
        kind = event['kind']
        payload = event['payload_dict']
        if kind in STAGES:
            stage, _ = advance(stage, kind)
        if kind == 'unload':
            received += int(payload.get('quantity', 0))
        if kind == 'weight':
            weight = float(payload['net_kg'])
    return stage, received, weight


def sequence_gaps(events):
    """每台设备已见序号中的缺口：{device: [缺失序号...]}。"""
    by_device = defaultdict(set)
    for e in events:
        if e['device'] is not None and e['seq'] is not None:
            by_device[e['device']].add(e['seq'])
    gaps = {}
    for device, seqs in by_device.items():
        missing = [s for s in range(min(seqs), max(seqs) + 1) if s not in seqs]
        if missing:
            gaps[device] = missing
    return gaps


def unordered_pairs(ids, reach):
    """ids 中证据无法证明先后的无序对。"""
    ids = sorted(ids)
    pairs = []
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            if b not in reach.get(a, ()) and a not in reach.get(b, ()):
                pairs.append((a, b))
    return pairs


def graph_cycles(graph):
    """盒级谱系图 {父: {子...}} 中的环（Tarjan 强连通分量，大小>1）。"""
    index = {}
    low = {}
    on_stack = set()
    stack = []
    counter = [0]
    cycles = []

    def strongconnect(start):
        index[start] = low[start] = counter[0]
        counter[0] += 1
        stack.append(start)
        on_stack.add(start)
        work = [(start, iter(sorted(graph.get(start, ()))))]
        while work:
            node, it = work[-1]
            descended = False
            for nxt in it:
                if nxt not in index:
                    index[nxt] = low[nxt] = counter[0]
                    counter[0] += 1
                    stack.append(nxt)
                    on_stack.add(nxt)
                    work.append((nxt, iter(sorted(graph.get(nxt, ())))))
                    descended = True
                    break
                elif nxt in on_stack:
                    low[node] = min(low[node], index[nxt])
            if descended:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index[node]:
                scc = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    scc.append(member)
                    if member == node:
                        break
                if len(scc) > 1:
                    cycles.append(sorted(scc))

    nodes = set(graph) | {c for children in graph.values() for c in children}
    for node in sorted(nodes):
        if node not in index:
            strongconnect(node)
    return cycles


def lineage_paths(graph, box, cap=16):
    """经过 box 的谱系路径（'父>子' 形式），用于审计报告。"""
    paths = set()

    def visit(path):
        if len(paths) >= cap:
            return
        if box in path and len(path) > 1:
            paths.add('>'.join(path))
        for nxt in sorted(graph.get(path[-1], ())):
            if nxt not in path:
                visit(path + [nxt])

    nodes = set(graph) | {c for children in graph.values() for c in children}
    for start in sorted(nodes):
        if len(paths) >= cap:
            break
        visit([start])
    return sorted(paths)
