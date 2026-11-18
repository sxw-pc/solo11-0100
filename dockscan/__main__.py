import argparse
import json
import tempfile
from pathlib import Path
from .ingest import freeze_config, import_file
from .rules import evaluate
from .report import export
from .store import connect
from . import unclaimed

DEFAULT_CASE = 'unclaimed-01'


def _base():
    return Path(__file__).parent.parent / 'fixtures'


def demo(workdir=None, contract='registered_consignor_release'):
    root = Path(workdir or tempfile.mkdtemp(prefix='dockscan-'))
    root.mkdir(parents=True, exist_ok=True)
    db = root / 'ledger.db'
    base = _base()
    freeze_config(db, base / 'config.json')
    import_file(db, base / 'events.jsonl')
    evaluate(db)
    unclaimed.register(db, DEFAULT_CASE, base / 'unclaimed-case.json')
    check = unclaimed.verify_claimants(db, DEFAULT_CASE)
    claimant = 'SHIPPER-A' if contract == 'registered_consignor_release' else 'BEARER-B'
    authorization = unclaimed.authorize(db, DEFAULT_CASE, claimant, contract)
    if authorization['ok']:
        unclaimed.deliver(db, DEFAULT_CASE, authorization['authorization'])
    report = export(db, root / 'report.json')
    print(json.dumps({'contract': contract, 'claimant': claimant,
                      'authorized': authorization['ok'],
                      'problems': authorization['problems'],
                      'custody_valid': unclaimed.custody_chain(db, DEFAULT_CASE)['valid'],
                      'eligible': {name: check['claims'][contract][name]['eligible']
                                   for name in check['claims'][contract]}},
                     ensure_ascii=False))
    return report


def main():
    p = argparse.ArgumentParser(description='仓门扫描对账台 — 离线证据与业务决策')
    sub = p.add_subparsers(dest='cmd', required=True)
    d = sub.add_parser('demo')
    d.add_argument('--workdir')
    d.add_argument('--contract', choices=unclaimed.CONTRACTS, default='registered_consignor_release')
    a = sub.add_parser('ingest')
    a.add_argument('--db', required=True)
    a.add_argument('--config', required=True)
    a.add_argument('events')
    e = sub.add_parser('inspect')
    e.add_argument('--db', required=True)
    r = sub.add_parser('report')
    r.add_argument('--db', required=True)
    r.add_argument('--out', required=True)
    # Unclaimed pallet workflow
    u = sub.add_parser('unclaimed-register')
    u.add_argument('--db', required=True)
    u.add_argument('--case', required=True)
    u.add_argument('fixture')
    v = sub.add_parser('unclaimed-verify')
    v.add_argument('--db', required=True)
    v.add_argument('--case', required=True)
    v.add_argument('--claimant')
    v.add_argument('--contract', choices=unclaimed.CONTRACTS)
    au = sub.add_parser('unclaimed-authorize')
    au.add_argument('--db', required=True)
    au.add_argument('--case', required=True)
    au.add_argument('--claimant', required=True)
    au.add_argument('--contract', required=True, choices=unclaimed.CONTRACTS)
    de = sub.add_parser('unclaimed-deliver')
    de.add_argument('--db', required=True)
    de.add_argument('--case', required=True)
    de.add_argument('--authorization', required=True)
    de.add_argument('--deliverer')
    ho = sub.add_parser('unclaimed-handoff')
    ho.add_argument('--db', required=True)
    ho.add_argument('--case', required=True)
    ho.add_argument('--id', required=True)
    ho.add_argument('--from', dest='from_holder', required=True)
    ho.add_argument('--to', dest='to_holder', required=True)
    ho.add_argument('--seal', required=True)
    ho.add_argument('--signed-by', required=True, help='comma separated; must include both parties')
    sc = sub.add_parser('unclaimed-seal-check')
    sc.add_argument('--db', required=True)
    sc.add_argument('--case', required=True)
    sc.add_argument('--id', required=True)
    sc.add_argument('--holder', required=True)
    sc.add_argument('--seal', required=True)
    st = sub.add_parser('unclaimed-status')
    st.add_argument('--db', required=True)
    st.add_argument('--case', required=True)
    cc = sub.add_parser('unclaimed-custody')
    cc.add_argument('--db', required=True)
    cc.add_argument('--case', required=True)
    args = p.parse_args()
    if args.cmd == 'demo':
        print(demo(args.workdir, args.contract))
    elif args.cmd == 'ingest':
        freeze_config(args.db, args.config)
        print(json.dumps(import_file(args.db, args.events), ensure_ascii=False))
    elif args.cmd == 'inspect':
        print(json.dumps(evaluate(args.db), ensure_ascii=False, indent=2))
    elif args.cmd == 'report':
        print(export(args.db, args.out))
    elif args.cmd == 'unclaimed-register':
        print(json.dumps(unclaimed.register(args.db, args.case, args.fixture), ensure_ascii=False))
    elif args.cmd == 'unclaimed-verify':
        print(json.dumps(unclaimed.verify(args.db, args.case, args.claimant, args.contract),
                         ensure_ascii=False, indent=2))
    elif args.cmd == 'unclaimed-authorize':
        print(json.dumps(unclaimed.authorize(args.db, args.case, args.claimant, args.contract),
                         ensure_ascii=False, indent=2))
    elif args.cmd == 'unclaimed-deliver':
        print(json.dumps(unclaimed.deliver(args.db, args.case, args.authorization, args.deliverer),
                         ensure_ascii=False, indent=2))
    elif args.cmd == 'unclaimed-handoff':
        signed = [s.strip() for s in args.signed_by.split(',') if s.strip()]
        print(json.dumps(unclaimed.record_handoff(args.db, args.case, args.id, args.from_holder,
                                                  args.to_holder, args.seal, signed), ensure_ascii=False))
    elif args.cmd == 'unclaimed-seal-check':
        print(json.dumps(unclaimed.record_seal_check(args.db, args.case, args.id, args.holder, args.seal),
                         ensure_ascii=False))
    elif args.cmd == 'unclaimed-status':
        print(json.dumps(unclaimed.status(args.db, args.case), ensure_ascii=False, indent=2))
    elif args.cmd == 'unclaimed-custody':
        print(json.dumps(unclaimed.custody_chain(args.db, args.case), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
