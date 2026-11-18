import argparse, json, tempfile
from pathlib import Path
from .ingest import freeze_config, import_file
from .rules import evaluate
from .report import export
from .climate_control import register, plan, apply_feedback, get_plan
from .store import connect


def demo(workdir=None):
    root = Path(workdir or tempfile.mkdtemp(prefix='dockscan-'))
    root.mkdir(parents=True, exist_ok=True)
    db = root / 'ledger.db'
    base = Path(__file__).parent.parent / 'fixtures'
    freeze_config(db, base / 'config.json')
    import_file(db, base / 'events.jsonl')
    evaluate(db)
    return export(db, root / 'report.json')


def climate_demo(workdir=None):
    root = Path(workdir or tempfile.mkdtemp(prefix='dockscan-climate-'))
    root.mkdir(parents=True, exist_ok=True)
    db = root / 'climate.db'
    base = Path(__file__).parent.parent / 'fixtures'
    freeze_config(db, base / 'climate-config.json')
    import_file(db, base / 'climate-events.jsonl')
    evaluate(db)
    register(db, 'climate-01', base / 'climate-case.json')
    summaries = []
    for objective in ('min_energy', 'min_max_dock_exposure'):
        summaries.append(plan(db, 'climate-01', objective))
    report = export(db, root / 'climate-report.json')
    return {'db': str(db), 'report': str(report), 'plans': summaries}


def main():
    p = argparse.ArgumentParser(description='仓门扫描对账台 — 离线证据、业务决策与暂存区温控')
    sub = p.add_subparsers(dest='cmd', required=True)
    d = sub.add_parser('demo'); d.add_argument('--workdir')
    cd = sub.add_parser('climate-demo'); cd.add_argument('--workdir')
    a = sub.add_parser('ingest'); a.add_argument('--db', required=True); a.add_argument('--config', required=True); a.add_argument('events')
    reg = sub.add_parser('climate-register')
    reg.add_argument('--db', required=True); reg.add_argument('--case-id', required=True)
    reg.add_argument('--fixture', required=True)
    pl = sub.add_parser('climate-plan')
    pl.add_argument('--db', required=True); pl.add_argument('--case-id', required=True)
    pl.add_argument('--objective', required=True, choices=('min_energy', 'min_max_dock_exposure'))
    fb = sub.add_parser('climate-feedback')
    fb.add_argument('--db', required=True); fb.add_argument('--case-id', required=True)
    fb.add_argument('--objective', required=True, choices=('min_energy', 'min_max_dock_exposure'))
    fb.add_argument('--dock', required=True); fb.add_argument('--at', required=True)
    fb.add_argument('--temp-c', required=True, type=float); fb.add_argument('--source', default='feedback')
    sh = sub.add_parser('climate-show')
    sh.add_argument('--db', required=True); sh.add_argument('--case-id', required=True)
    sh.add_argument('--objective', required=True, choices=('min_energy', 'min_max_dock_exposure'))
    e = sub.add_parser('inspect'); e.add_argument('--db', required=True)
    r = sub.add_parser('report'); r.add_argument('--db', required=True); r.add_argument('--out', required=True)
    args = p.parse_args()
    if args.cmd == 'demo':
        print(demo(args.workdir))
    elif args.cmd == 'climate-demo':
        print(json.dumps(climate_demo(args.workdir), ensure_ascii=False, indent=2))
    elif args.cmd == 'ingest':
        freeze_config(args.db, args.config)
        print(import_file(args.db, args.events))
    elif args.cmd == 'climate-register':
        print(json.dumps(register(args.db, args.case_id, args.fixture), ensure_ascii=False, indent=2))
    elif args.cmd == 'climate-plan':
        print(json.dumps(plan(args.db, args.case_id, args.objective), ensure_ascii=False, indent=2))
    elif args.cmd == 'climate-feedback':
        print(json.dumps(apply_feedback(args.db, args.case_id, args.objective,
                                        args.dock, args.at, args.temp_c, args.source),
                         ensure_ascii=False, indent=2))
    elif args.cmd == 'climate-show':
        print(json.dumps(get_plan(args.db, args.case_id, args.objective), ensure_ascii=False, indent=2))
    elif args.cmd == 'inspect':
        print(json.dumps(evaluate(args.db), ensure_ascii=False, indent=2))
    elif args.cmd == 'report':
        print(export(args.db, args.out))


if __name__ == '__main__':
    main()
