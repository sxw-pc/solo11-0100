def known_box(box, orders, lineage_boxes=frozenset()):
    return box in orders or box in lineage_boxes


def valid_lineage(box, parts, quantity):
    return (sum(int(part['quantity']) for part in parts) == quantity
            and all(int(part['quantity']) > 0 and part['id'] != box for part in parts))


def incompatible_pairs(categories):
    """配置声明的不兼容对，取对称闭包：A 列了 B 或 B 列了 A 都算不兼容。"""
    pairs = set()
    for cat, spec in (categories or {}).items():
        for other in (spec or {}).get('incompatible_with', []):
            pairs.add((str(cat), str(other)))
            pairs.add((str(other), str(cat)))
    return frozenset(pairs)


def resolve_categories(seed, edges):
    """沿拆并谱系中继类别：拆箱子箱继承父箱类别，并箱继承全部来源类别。

    seed: {box: {category,...}} 仅含配置声明的来源类别。
    edges: [(source, product)] 守恒拆并关系（split 的父→子、merge 的父→成品）。
    返回 (categories, provenance)：provenance[box][category] = 声明该类别的配置源箱集合。
    迭代到不动点，跨多次拆并作业的链式中继也能传播。
    """
    cats = {box: set(c) for box, c in seed.items()}
    prov = {box: {c: {box} for c in cset} for box, cset in seed.items()}
    changed = True
    while changed:
        changed = False
        for source, product in edges:
            for cat in cats.get(source, ()):
                target = cats.setdefault(product, set())
                if cat not in target:
                    target.add(cat)
                    changed = True
                origins = prov.setdefault(product, {}).setdefault(cat, set())
                merged = origins | prov.get(source, {}).get(cat, set())
                if merged != origins:
                    prov[product][cat] = merged
                    changed = True
    return cats, prov


def cargo_roots(configured, edges):
    """每个箱子的货物来源根箱集合（沿谱系反向汇总）。

    同一票货物拆出的子箱、并出的成品共享来源根；来源根不相交的两箱
    才是物理上独立的两票货物，才需要隔离判定。未入谱系的箱子以自身为根。
    """
    roots = {b: {b} for b in configured}
    changed = True
    while changed:
        changed = False
        for source, product in edges:
            merged = roots.get(product, set()) | (roots.get(source) or {source})
            if merged != roots.get(product, set()):
                roots[product] = merged
                changed = True
    return roots


def categories_conflict(cats_a, cats_b, pairs):
    """两组类别之间实际命中的不兼容对（去重、排序，便于审计）。"""
    return sorted({(a, b) for a in cats_a for b in cats_b if (a, b) in pairs})
