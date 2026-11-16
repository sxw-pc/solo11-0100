"""箱号身份、拆并谱系与危险类别继承的纯函数。"""


def known_box(box, orders):
    return box in orders


def valid_lineage(box, parts, quantity):
    return (sum(int(part['quantity']) for part in parts) == quantity
            and all(int(part['quantity']) > 0 and part['id'] != box for part in parts))


def incompatible(class_a, class_b, hazard_classes):
    """两个危险类别是否不兼容；任一方向声明隔离即视为不兼容。"""
    if class_a == class_b:
        return False
    spec_a = hazard_classes.get(class_a, {})
    spec_b = hazard_classes.get(class_b, {})
    return (class_b in spec_a.get('incompatible_with', [])
            or class_a in spec_b.get('incompatible_with', []))


def resolve_class_map(declared, splits, merges):
    """沿拆并谱系不动点传播危险类别。

    declared: {box: [(class, origin), ...]} 配置或事件直接声明的类别。
    splits:   [(parent, child, event_id)] 拆箱边，子箱继承父箱类别。
    merges:   [(parent, merged, event_id)] 并箱边，来源类别并入合并箱，
              不允许只按合并后箱号丢掉来源类别。
    返回 (classes, origins)：classes[box] 为类别集合，
    origins[box][class] 为可审计的来源说明列表。
    谱系中继（拆后再拆、并后再拆）沿链传导；中间边缺失则下游无来源。
    """
    classes = {}
    origins = {}

    def inherit(box, cls, origin):
        slot = origins.setdefault(box, {}).setdefault(cls, [])
        if origin not in slot:
            slot.append(origin)
        if cls not in classes.setdefault(box, set()):
            classes[box].add(cls)
            return True
        return False

    for box, entries in declared.items():
        for cls, origin in entries:
            inherit(box, cls, origin)
    changed = True
    while changed:
        changed = False
        for parent, child, event_id in splits:
            for cls in sorted(classes.get(parent, ())):
                changed |= inherit(child, cls, f"split:{parent}@event:{event_id}")
        for parent, merged, event_id in merges:
            for cls in sorted(classes.get(parent, ())):
                changed |= inherit(merged, cls, f"merge:{parent}@event:{event_id}")
    return classes, origins


def bay_certified(bays, bay_id, dock, classes_needed, start, end):
    """隔离舱在 [start, end] 内是否经配置认证：已定义、月台一致、
    认证区间完整覆盖（边界含端点）、获准全部所需类别。
    通过返回 None，否则返回 'bay_not_certified'。"""
    bay = bays.get(bay_id)
    if bay is None or bay['dock'] != dock:
        return 'bay_not_certified'
    if not (bay['certified_from'] <= start and end <= bay['certified_until']):
        return 'bay_not_certified'
    if not set(classes_needed) <= set(bay['classes']):
        return 'bay_not_certified'
    return None
