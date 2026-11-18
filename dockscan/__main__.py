import argparse
import json
import tempfile
from pathlib import Path

from .ingest import freeze_config, import_file
from .rules import evaluate
from .report import export
from .privacy import register, CONTRACTS
from .releases import publish, recover, download
from .store import connect, ledger_rows


def demo(workdir=None):
    root = Path(workdir or tempfile.mkdtemp(prefix='dockscan-'))
    root.mkdir(parents=True, exist_ok=True)
    db = root / 'ledger.db'
    base = Path(__file__).parent.parent / 'fixtures'
    freeze_config(db, base / 'config.json')
    import_file(db, base / 'events.jsonl')
    evaluate(db)
    return export(db, root / 'report.json')


def privacy_demo(workdir=None):
    """End-to-end observable scenario for the frozen privacy case."""
    root = Path(workdir or tempfile.mkdtemp(prefix='dockscan-privacy-'))
    root.mkdir(parents=True, exist_ok=True)
    db = root / 'ledger.db'
    out = root / 'releases'
    base = Path(__file__).parent.parent / 'fixtures'
    trace = []

    freeze_config(db, base / 'privacy-config.json')
    import_file(db, base / 'privacy-events.jsonl')
    evaluate(db)
    register(db, 'privacy-01', base / 'privacy-case.json')

    carrier = CONTRACTS['carrier_consortium_non_inference']['audience']
    auditor = CONTRACTS['internal_auditor_exact_access']['audience']

    # R2 against carrier_consortium: R1 (3-box total) already visible, so the
    # 2-box aggregate would fix B3 by subtraction. Must be denied.
    r2_carrier = publish(db, out, 'privacy-01', carrier,
                         'carrier_consortium_non_inference', 'aggregate',
                         ['B1', 'B2'], declared_sum=300)
    trace.append({'step': 'R2_to_carrier_consortium',
                  'decision': r2_carrier['decision'],
                  'reason_code': r2_carrier.get('reason_code'),
                  'fixed_boxes': r2_carrier.get('evidence', {})
                  .get('fixed_boxes')})

    # Same R2 to the internal auditor: per-box authorization, allowed.
    r2_auditor = publish(db, out, 'privacy-01', auditor,
                         'internal_auditor_exact_access', 'aggregate',
                         ['B1', 'B2'])
    trace.append({'step': 'R2_to_internal_auditor',
                  'decision': r2_auditor['decision'],
                  'json_path': r2_auditor.get('json_path')})

    # Auditor per-box evidence with full source paths.
    exact = publish(db, out, 'privacy-01', auditor,
                    'internal_auditor_exact_access', 'per_box',
                    ['B1', 'B2', 'B3'])
    trace.append({'step': 'per_box_to_internal_auditor',
                  'decision': exact['decision'],
                  'json_path': exact.get('json_path')})

    # A coarser carrier aggregate that cannot be subtracted down (all three).
    coarse = publish(db, out, 'privacy-01', carrier,
                     'carrier_consortium_non_inference', 'aggregate',
                     ['B1', 'B2', 'B3'])
    trace.append({'step': 'coarse_aggregate_to_carrier_consortium',
                  'decision': coarse['decision'],
                  'reason_code': coarse.get('reason_code')})

    ordinary = export(db, root / 'report.json')
    conn = connect(db)
    try:
        ledger = ledger_rows(conn)
    finally:
        conn.close()
    summary = {'workdir': str(root), 'trace': trace,
               'ledger': ledger, 'ordinary_report': str(ordinary)}
    (root / 'privacy-demo-summary.json').write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    return summary


def main():
    p = argparse.ArgumentParser(description='仓门扫描对账台 — 离线证据与业务决策')
    sub = p.add_subparsers(dest='cmd', required=True)
    d = sub.add_parser('demo')
    d.add_argument('--workdir')
    pd = sub.add_parser('privacy-demo')
    pd.add_argument('--workdir')
    a = sub.add_parser('ingest')
    a.add_argument('--db', required=True)
    a.add_argument('--config', required=True)
    a.add_argument('events')
    e = sub.add_parser('inspect')
    e.add_argument('--db', required=True)
    r = sub.add_parser('report')
    r.add_argument('--db', required=True)
    r.add_argument('--out', required=True)
    reg = sub.add_parser('register-case')
    reg.add_argument('--db', required=True)
    reg.add_argument('--case-id', required=True)
    reg.add_argument('--fixture', required=True)
    rel = sub.add_parser('release')
    rel.add_argument('--db', required=True)
    rel.add_argument('--out-dir', required=True)
    rel.add_argument('--case-id', required=True)
    rel.add_argument('--audience', required=True)
    rel.add_argument('--contract', required=True,
                     choices=sorted(CONTRACTS))
    rel.add_argument('--kind', required=True,
                     choices=('aggregate', 'per_box'))
    rel.add_argument('--boxes', required=True,
                     help='comma-separated box ids, e.g. B1,B2')
    rel.add_argument('--declared-sum', type=int, default=None)
    rec = sub.add_parser('recover')
    rec.add_argument('--db', required=True)
    rec.add_argument('--out-dir', required=True)
    dl = sub.add_parser('download')
    dl.add_argument('--db', required=True)
    dl.add_argument('--release-id', required=True)
    led = sub.add_parser('ledger')
    led.add_argument('--db', required=True)
    args = p.parse_args()
    if args.cmd == 'demo':
        print(demo(args.workdir))
    elif args.cmd == 'privacy-demo':
        print(json.dumps(privacy_demo(args.workdir), ensure_ascii=False, indent=2))
    elif args.cmd == 'ingest':
        freeze_config(args.db, args.config)
        print(import_file(args.db, args.events))
    elif args.cmd == 'inspect':
        print(json.dumps(evaluate(args.db), ensure_ascii=False, indent=2))
    elif args.cmd == 'report':
        print(export(args.db, args.out))
    elif args.cmd == 'register-case':
        print(register(args.db, args.case_id, args.fixture))
    elif args.cmd == 'release':
        decision = publish(args.db, args.out_dir, args.case_id,
                           args.audience, args.contract, args.kind,
                           [b.strip() for b in args.boxes.split(',') if b.strip()],
                           args.declared_sum)
        print(json.dumps({k: v for k, v in decision.items() if k != 'case'},
                         ensure_ascii=False, indent=2))
    elif args.cmd == 'recover':
        print(json.dumps(recover(args.db, args.out_dir),
                         ensure_ascii=False, indent=2))
    elif args.cmd == 'download':
        print(json.dumps(download(args.db, args.release_id),
                         ensure_ascii=False, indent=2))
    elif args.cmd == 'ledger':
        conn = connect(args.db)
        try:
            print(json.dumps(ledger_rows(conn), ensure_ascii=False, indent=2))
        finally:
            conn.close()


if __name__ == '__main__':
    main()
