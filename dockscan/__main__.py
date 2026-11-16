import argparse,json
from pathlib import Path
from .ingest import freeze_config,import_file
from .rules import evaluate,POLICIES
from .report import export

def demo(workdir,policy):
    root=Path(workdir);root.mkdir(parents=True,exist_ok=True)
    db=root/'ledger.db'; base=Path(__file__).parent.parent/'fixtures'
    freeze_config(db,base/'config.json');import_file(db,base/'events.jsonl');evaluate(db,policy=policy)
    return export(db,root/'report.json')
def main():
    p=argparse.ArgumentParser(description='仓门扫描对账台 — 离线证据与业务决策')
    sub=p.add_subparsers(dest='cmd',required=True)
    d=sub.add_parser('demo');d.add_argument('--workdir',required=True);d.add_argument('--policy',required=True,choices=POLICIES,help='strict=时间顺序必须可证明；consensus=全部合法顺序结论一致才放行')
    a=sub.add_parser('ingest');a.add_argument('--db',required=True);a.add_argument('--config',required=True);a.add_argument('events')
    e=sub.add_parser('inspect');e.add_argument('--db',required=True)
    e.add_argument('--policy',required=True,choices=POLICIES,help='strict=时间顺序必须可证明；consensus=全部合法顺序结论一致才放行')
    r=sub.add_parser('report');r.add_argument('--db',required=True);r.add_argument('--out',required=True)
    args=p.parse_args()
    if args.cmd=='demo': print(demo(args.workdir,policy=args.policy))
    elif args.cmd=='ingest': freeze_config(args.db,args.config);print(import_file(args.db,args.events))
    elif args.cmd=='inspect': print(json.dumps(evaluate(args.db,policy=args.policy),ensure_ascii=False,indent=2))
    elif args.cmd=='report': print(export(args.db,args.out))
if __name__=='__main__':main()
