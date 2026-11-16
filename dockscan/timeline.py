"""事件因果重建。

时间戳同刻的跨设备扫描之间没有可证明的先后，因此本模块绝不使用文件顺序或
数据库自增 id 决胜。先后关系（有向边）只允许来自四类有证据的约束：

1. time_order      —— 严格更早的 UTC 时间戳；
2. device_seq      —— 同一设备内单调递增的 seq；
3. stage_order     —— 同一货箱 gate→unload→check→exit 的阶段约束；
4. lineage_split / lineage_merge —— 拆箱/并箱谱系边。

图上的每条合法拓扑序都是一种与全部证据相容的事件顺序；同刻且互相不可达的
事件即“无因果关系”，其相对顺序不可声称。
"""

STAGES = ('gate', 'unload', 'check', 'exit')
STAGE_RANK = {kind: i for i, kind in enumerate(STAGES)}

# 线性扩展枚举上限：超过即视为顺序空间无法穷尽，保留显式拒绝依据
EXTENSION_CAP = 2000


def advance(stage, kind):
    if kind not in STAGES:
        return stage, None
    if stage >= len(STAGES) or kind != STAGES[stage]:
        return stage, 'illegal_transition'
    return stage + 1, None


def _lineage_parts(v, kind):
    if kind == 'split':
        return v.get('children', []), 'child'
    if kind == 'merge':
        return v.get('parents', []), 'parent'
    return [], None


def detect_seq_gaps(events):
    """按设备检查 seq 缺口。返回 [{device, missing:[...], evidence:[event id...]}]。

    缺口只作证据保留：它意味着该设备可能有未导入的扫描，任何依赖该设备同刻
    先后的结论都不应被静默接受。
    """
    by_device = {}
    for e in events:
        if e.get('device') is None or e.get('seq') is None:
            continue
        by_device.setdefault(e['device'], set()).add(int(e['seq']))
    gaps = []
    for device, seqs in sorted(by_device.items()):
        if not seqs:
            continue
        lo, hi = min(seqs), max(seqs)
        missing = [s for s in range(lo, hi + 1) if s not in seqs]
        if missing:
            evidence = sorted(e['id'] for e in events
                              if e.get('device') == device)
            gaps.append({'device': device, 'missing': missing,
                         'evidence': evidence})
    return gaps


def build_causal_graph(events):
    """构建证据化因果 DAG。

    返回 dict：nodes / edges / edge_records / by_subject / lineage_rels /
    box_edges（货箱级谱系图，用于祖先环检测）。
    """
    nodes = {e['id']: e for e in events}
    edges = {eid: set() for eid in nodes}
    edge_records = []
    edge_keys = set()

    def add(u, v, reason, detail):
        if u == v or u not in nodes or v not in nodes:
            return
        # 同一对事件可由多种证据支撑（时间、设备序号、阶段、谱系）：全部保留
        key = (u, v, reason)
        if key in edge_keys:
            return
        edge_keys.add(key)
        edges[u].add(v)
        edge_records.append({'src': u, 'dst': v, 'reason': reason,
                             'detail': detail})

    by_subject = {}
    for e in events:
        by_subject.setdefault(e['subject'], []).append(e)

    # 1. 严格时间序（同刻不加边——同刻不可证先后）。
    # 只在相邻的不同时刻组之间加边：时间是全序，链式可达与全连接等价，
    # 同时让审计表保留可读的最小证据集。
    ordered = sorted(events, key=lambda e: (e['time'], e['id']))
    time_groups = []
    for e in ordered:
        if not time_groups or time_groups[-1][0] != e['time']:
            time_groups.append((e['time'], []))
        time_groups[-1][1].append(e)
    for (t0, g0), (t1, g1) in zip(time_groups, time_groups[1:]):
        for u in g0:
            for v in g1:
                add(u['id'], v['id'], 'time_order', f"{t0} < {t1}")

    # 2. 同设备 seq 序；若与时间戳矛盾则记录冲突（随后以成环暴露）
    by_device = {}
    for e in events:
        if e.get('device') is not None and e.get('seq') is not None:
            by_device.setdefault(e['device'], []).append(e)
    device_conflicts = []
    for device, dev_events in by_device.items():
        dev_sorted = sorted(dev_events, key=lambda e: int(e['seq']))
        for u, v in zip(dev_sorted, dev_sorted[1:]):
            if u['time'] <= v['time']:
                add(u['id'], v['id'], 'device_seq',
                    f"{device} seq {u['seq']} < {v['seq']}")
            else:
                device_conflicts.append({
                    'device': device,
                    'earlier_seq': u['id'], 'later_seq': v['id'],
                    'time_of_earlier_seq': u['time'],
                    'time_of_later_seq': v['time']})
                # 仍按 seq 加边：它将与 time_order 反向边成环，给出显式拒绝依据
                add(u['id'], v['id'], 'device_seq',
                    f"{device} seq {u['seq']} < {v['seq']} (time conflict)")

    # 3. 同箱阶段约束
    for subject, sub_events in by_subject.items():
        stage_events = sorted(
            (e for e in sub_events if e['kind'] in STAGE_RANK),
            key=lambda e: STAGE_RANK[e['kind']])
        for u, v in zip(stage_events, stage_events[1:]):
            add(u['id'], v['id'], 'stage_order',
                f"{subject}: {u['kind']} -> {v['kind']}")

    # 4. 拆并箱谱系边
    lineage_rels = []
    split_box_edges = set()   # 有向血缘图（仅拆箱）：回到祖先即谱系环
    relation_pairs = set()    # 无向关联图：决定证据闭包（拆后并回同箱是合法重聚，不算环）
    for e in events:
        if e['kind'] not in ('split', 'merge'):
            continue
        v = e.get('v') or {}
        parts, role = _lineage_parts(v, e['kind'])
        rel = {'event_id': e['id'], 'kind': e['kind'],
               'subject': e['subject'], 'parts': parts}
        lineage_rels.append(rel)
        if e['kind'] == 'split':
            # 拆箱事件先于每个子箱的全部已知事件
            for part in parts:
                child = str(part['id'])
                split_box_edges.add((e['subject'], child))
                relation_pairs.add(frozenset((e['subject'], child)))
                for child_event in by_subject.get(child, []):
                    add(e['id'], child_event['id'], 'lineage_split',
                        f"split {e['subject']} -> {child}")
        else:
            # 每个父箱的全部已知事件先于并箱事件
            for part in parts:
                parent = str(part['id'])
                relation_pairs.add(frozenset((parent, e['subject'])))
                for parent_event in by_subject.get(parent, []):
                    add(parent_event['id'], e['id'], 'lineage_merge',
                        f"merge {parent} -> {e['subject']}")

    return {'nodes': nodes, 'edges': edges, 'edge_records': edge_records,
            'by_subject': by_subject, 'lineage_rels': lineage_rels,
            'split_box_edges': split_box_edges,
            'relation_pairs': relation_pairs,
            'device_conflicts': device_conflicts}


def find_cycle(nodes, edges):
    """返回事件级因果图上的一个环（节点 id 列表），无环返回 None。"""
    color = {}
    stack = []

    def dfs(u):
        color[u] = 1
        stack.append(u)
        for v in sorted(edges[u]):
            if color.get(v, 0) == 0:
                found = dfs(v)
                if found:
                    return found
            elif color[v] == 1:
                return stack[stack.index(v):] + [v]
        stack.pop()
        color[u] = 2
        return None

    for u in sorted(nodes):
        if color.get(u, 0) == 0:
            found = dfs(u)
            if found:
                return found
    return None


def find_box_lineage_cycle(split_box_edges):
    """拆箱血缘图（parent->child，仅含 split 边）上的祖先环，返回箱名路径，无环返回 None。

    并箱使货箱重聚属于合法操作，不计入此图；只有 split 边回到祖先才是
    “拆并链回到祖先”的非法谱系。
    """
    adj = {}
    for u, v in split_box_edges:
        adj.setdefault(u, set()).add(v)
    color = {}
    stack = []

    def dfs(u):
        color[u] = 1
        stack.append(u)
        for v in sorted(adj.get(u, ())):
            if color.get(v, 0) == 0:
                found = dfs(v)
                if found:
                    return found
            elif color[v] == 1:
                return stack[stack.index(v):] + [v]
        stack.pop()
        color[u] = 2
        return None

    for u in sorted(adj):
        if color.get(u, 0) == 0:
            found = dfs(u)
            if found:
                return found
    return None


def _reachable(edges, roots):
    seen = set()
    stack = list(roots)
    while stack:
        u = stack.pop()
        if u in seen:
            continue
        seen.add(u)
        stack.extend(edges[u])
    return seen


def closure_for(box, graph):
    """货箱的证据闭包：本箱事件 + 谱系相连货箱的全部事件。

    连通性基于无向关联图（拆箱与并箱都建立关联），因此拆后并回同箱的
    合法重聚仍共享证据闭包，但不会被误判为祖先环。
    """
    related_boxes = {box}
    changed = True
    while changed:
        changed = False
        for pair in graph['relation_pairs']:
            u, v = tuple(pair)
            if u in related_boxes and v not in related_boxes:
                related_boxes.add(v)
                changed = True
            if v in related_boxes and u not in related_boxes:
                related_boxes.add(u)
                changed = True
    return {eid for eid, e in graph['nodes'].items()
            if e['subject'] in related_boxes}, related_boxes


def linear_extensions(node_ids, edges, cap=EXTENSION_CAP):
    """枚举受限节点集合上的全部拓扑序（DFS），带截断。

    同刻无因果关系的事件在此处自然产生多种顺序——枚举本身不偏袒任何一种。
    返回 (orders, capped)；orders 为节点 id 序列的列表。
    """
    node_ids = set(node_ids)
    indeg = {u: 0 for u in node_ids}
    adj = {u: {v for v in edges[u] if v in node_ids} for u in node_ids}
    for u in node_ids:
        for v in adj[u]:
            indeg[v] += 1
    orders = []
    used = set()
    current = []

    def dfs():
        if len(orders) >= cap:
            return
        if len(current) == len(node_ids):
            orders.append(tuple(current))
            return
        ready = sorted(u for u in node_ids if u not in used and indeg[u] == 0)
        for u in ready:
            current.append(u)
            used.add(u)
            for v in adj[u]:
                indeg[v] -= 1
            dfs()
            for v in adj[u]:
                indeg[v] += 1
            used.discard(u)
            current.pop()
            if len(orders) >= cap:
                return

    dfs()
    return orders, len(orders) >= cap and len(node_ids) > 0


def unresolved_same_time_pairs(events, edges, relevant=None):
    """同刻且互相不可达的事件对——顺序不可证明的原始证据。"""
    pairs = []
    same_time = {}
    for e in events:
        if relevant is not None and e['id'] not in relevant:
            continue
        same_time.setdefault(e['time'], []).append(e)
    for ts, group in same_time.items():
        if len(group) < 2:
            continue
        for i, a in enumerate(group):
            reachable = _reachable(edges, [a['id']])
            for b in group[i + 1:]:
                if b['id'] in reachable:
                    continue
                reverse = _reachable(edges, [b['id']])
                if a['id'] in reverse:
                    continue
                pairs.append({
                    'time': ts,
                    'a': {'event_id': a['id'], 'device': a.get('device'),
                          'seq': a.get('seq'), 'subject': a['subject'],
                          'kind': a['kind']},
                    'b': {'event_id': b['id'], 'device': b.get('device'),
                          'seq': b.get('seq'), 'subject': b['subject'],
                          'kind': b['kind']}})
    return pairs
