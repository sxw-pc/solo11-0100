STAGES = ('gate', 'unload', 'check', 'exit')


def advance(stage, kind):
    if kind not in STAGES:
        return stage, None
    if stage >= len(STAGES) or kind != STAGES[stage]:
        return stage, 'illegal_transition'
    return stage + 1, None
