import argparse,json,tempfile
from pathlib import Path
from .ingest import freeze_config,import_file
from .rules import evaluate
from .report import export
from .returns import register, add_amendment, process, CONTRACTS
from .store import connect

def demo(workdir=None):
    root=Path(workdir or tempfile.mkdtemp(prefix='dockscan-'));root.mkdir(parents=True,exist_ok=True)
    db=root/'ledger.db'; base=Path(__file__).parent.parent/'fixtures'
    freeze_config(db,base/'config.json');import_file(db,base/'events.jsonl');evaluate(db)
    return export(db,root/'report.json')

def returns_demo(workdir=None):
    root=Path(workdir or tempfile.mkdtemp(prefix='dockscan-returns-'));root.mkdir(parents=True,exist_ok=True)
    db=root/'returns.db'; base=Path(__file__).parent.parent/'fixtures'
    freeze_config(db,base/'return-forward-config.json')
    import_file(db,base/'return-forward-events.jsonl')
    evaluate(db)
    register(db,'return-01',base/'return-case.json')
    summaries=[process(db,'return-01',c) for c in CONTRACTS]
    report=export(db,root/'return-report.json')
    return {'db':str(db),'report':str(report),'summaries':summaries}

def main():
    p=argparse.ArgumentParser(description='仓门扫描对账台 — 离线证据与业务决策')
    sub=p.add_subparsers(dest='cmd',required=True)
    d=sub.add_parser('demo');d.add_argument('--workdir')
    a=sub.add_parser('ingest');a.add_argument('--db',required=True);a.add_argument('--config',required=True);a.add_argument('events')
    e=sub.add_parser('inspect');e.add_argument('--db',required=True)
    r=sub.add_parser('report');r.add_argument('--db',required=True);r.add_argument('--out',required=True)
    rd=sub.add_parser('returns-demo');rd.add_argument('--workdir')
    rr=sub.add_parser('return-register');rr.add_argument('--db',required=True);rr.add_argument('--case',required=True);rr.add_argument('fixture')
    ra=sub.add_parser('return-amend');ra.add_argument('--db',required=True);ra.add_argument('--case',required=True);ra.add_argument('fixture')
    rp=sub.add_parser('return-process');rp.add_argument('--db',required=True);rp.add_argument('--case',required=True)
    rp.add_argument('--contract',required=True,choices=CONTRACTS)
    args=p.parse_args()
    if args.cmd=='demo': print(demo(args.workdir))
    elif args.cmd=='ingest': freeze_config(args.db,args.config);print(import_file(args.db,args.events))
    elif args.cmd=='inspect': print(json.dumps(evaluate(args.db),ensure_ascii=False,indent=2))
    elif args.cmd=='report': print(export(args.db,args.out))
    elif args.cmd=='returns-demo': print(json.dumps(returns_demo(args.workdir),ensure_ascii=False,indent=2))
    elif args.cmd=='return-register': print(json.dumps(register(args.db,args.case,args.fixture),ensure_ascii=False))
    elif args.cmd=='return-amend': print(json.dumps(add_amendment(args.db,args.case,args.fixture),ensure_ascii=False))
    elif args.cmd=='return-process': print(json.dumps(process(args.db,args.case,args.contract),ensure_ascii=False,indent=2))
if __name__=='__main__':main()
